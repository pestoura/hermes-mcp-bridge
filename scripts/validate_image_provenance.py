#!/usr/bin/env python3
"""Validate and optionally retain sanitized Hermes MCP Bridge image provenance.

The validator reads only Docker image identity and an explicit allow-list of
non-sensitive labels. It never inspects container environment, mounts, command
arguments, secrets or application payloads.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

LABELS = {
    "source": "org.opencontainers.image.source",
    "revision": "org.opencontainers.image.revision",
    "version": "org.opencontainers.image.version",
    "created": "org.opencontainers.image.created",
    "build_id": "io.jarvas.hermes-mcp-bridge.build-id",
    "schema_version": "io.jarvas.hermes-mcp-bridge.schema-version",
    "contract_version": "io.jarvas.hermes-mcp-bridge.contract-version",
}

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ProvenanceError(RuntimeError):
    """Image provenance is missing, ambiguous or inconsistent."""


def _run_json(*args: str) -> Any:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ProvenanceError("docker image inspect failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProvenanceError("docker image inspect returned invalid JSON") from exc


def inspect_image(image: str) -> dict[str, Any]:
    normalized = image.strip()
    if not normalized:
        raise ProvenanceError("image reference is required")
    if normalized.endswith(":local") or normalized == "local":
        raise ProvenanceError("ambiguous :local image references are not promotable")

    payload = _run_json("docker", "image", "inspect", normalized)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ProvenanceError("docker image inspect returned an unexpected shape")
    item = payload[0]
    config = item.get("Config")
    if not isinstance(config, dict):
        raise ProvenanceError("image Config is missing")
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        labels = {}

    image_id = str(item.get("Id") or "").strip()
    if not image_id.startswith("sha256:") or len(image_id) <= len("sha256:"):
        raise ProvenanceError("immutable image ID is missing")

    repo_digests = item.get("RepoDigests")
    digests = sorted(
        {
            str(value)
            for value in (repo_digests if isinstance(repo_digests, list) else [])
            if isinstance(value, str) and "@sha256:" in value
        }
    )

    return {
        "image_ref": normalized,
        "image_id": image_id,
        "repo_digests": digests,
        "labels": {name: str(labels.get(key) or "") for name, key in LABELS.items()},
    }


def validate(
    evidence: dict[str, Any],
    *,
    revision: str,
    version: str,
    build_id: str,
    schema_version: str,
    contract_version: str,
) -> dict[str, Any]:
    revision = revision.strip().lower()
    if not _SHA_RE.fullmatch(revision):
        raise ProvenanceError("expected revision must be an exact 40-character Git SHA")

    labels = evidence["labels"]
    expected = {
        "revision": revision,
        "version": version,
        "build_id": build_id,
        "schema_version": schema_version,
        "contract_version": contract_version,
    }
    for field, value in expected.items():
        if not value or labels.get(field) != value:
            raise ProvenanceError(f"image provenance mismatch: {field}")

    if not labels.get("source", "").startswith("https://github.com/"):
        raise ProvenanceError("image source label is missing or unexpected")
    created = labels.get("created", "")
    if not created or created == "unknown":
        raise ProvenanceError("image creation label is not promotable")

    # The evidence document is deliberately sanitized and bounded. RepoDigests
    # may be empty for a purely local CI image; production promotion must pin
    # the image ID and, when a registry is used, its immutable repo digest.
    return {
        "schema": "hermes-mcp-bridge-image-provenance/v1",
        "image_ref": evidence["image_ref"],
        "image_id": evidence["image_id"],
        "repo_digests": evidence["repo_digests"],
        "source": labels["source"],
        "revision": labels["revision"],
        "version": labels["version"],
        "created": created,
        "build_id": labels["build_id"],
        "schema_version": labels["schema_version"],
        "contract_version": labels["contract_version"],
        "tool_contract_count": 27,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--schema-version", required=True)
    parser.add_argument("--contract-version", required=True)
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    evidence = validate(
        inspect_image(args.image),
        revision=args.revision,
        version=args.version,
        build_id=args.build_id,
        schema_version=args.schema_version,
        contract_version=args.contract_version,
    )
    rendered = json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        output = Path(args.output)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
