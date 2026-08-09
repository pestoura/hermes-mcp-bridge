#!/usr/bin/env python3
"""Phase 9 supply-chain evidence: pinning, provenance and SBOM shape.

This is the *repository-side* half of the control described in
``docs/v2/phase9/supply-chain-sbom.md``. The image half (Trivy scan, CycloneDX
generation, digest-pinned base image) already runs in CI; this script proves the
properties CI cannot express as a shell one-liner and records them as evidence:

* every runtime dependency carries an upper bound (no floating range),
* the Dockerfile base image is pinned by ``sha256:`` digest, not a mutable tag,
* build provenance args exist and default to a non-promotable value,
* an SBOM, when supplied, is CycloneDX with a non-empty component set, and its
  component names cover the declared runtime dependencies,
* provenance, when supplied, binds the exact source commit.

Fail-closed: a control that cannot be evaluated is a failure, never a skip.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCKERFILE = REPO_ROOT / "Dockerfile"

BASE_DIGEST = re.compile(r"^ARG BASE_IMAGE=([^\s@]+)@(sha256:[0-9a-f]{64})\s*$", re.MULTILINE)
DEP_LINE = re.compile(r'^\s*"([A-Za-z0-9._-]+)\s*([^"]*)"\s*,?\s*$', re.MULTILINE)
UPPER_BOUND = re.compile(r"<\s*\d")

REQUIRED_PROVENANCE_ARGS = (
    "OCI_IMAGE_SOURCE",
    "OCI_IMAGE_REVISION",
    "OCI_IMAGE_VERSION",
    "OCI_IMAGE_CREATED",
    "BRIDGE_BUILD_ID",
    "BRIDGE_SCHEMA_VERSION",
    "BRIDGE_CONTRACT_VERSION",
)


def _runtime_dependencies(text: str) -> list[tuple[str, str]]:
    start = text.index("dependencies = [")
    end = text.index("]", start)
    return [(name, spec) for name, spec in DEP_LINE.findall(text[start:end])]


def check_pinning(text: str) -> tuple[list[str], list[dict[str, str]]]:
    failures: list[str] = []
    recorded: list[dict[str, str]] = []
    deps = _runtime_dependencies(text)
    if not deps:
        return ["SC-01: no runtime dependencies parsed"], []
    for name, spec in deps:
        recorded.append({"name": name, "specifier": spec.strip()})
        if not spec.strip():
            failures.append(f"SC-01: {name} is unconstrained")
        elif not UPPER_BOUND.search(spec):
            failures.append(f"SC-01: {name} has no upper bound")
    return failures, recorded


def check_base_image(text: str) -> tuple[list[str], dict[str, str]]:
    match = BASE_DIGEST.search(text)
    if match is None:
        return ["SC-02: base image is not pinned by digest"], {}
    return [], {"base_image": match.group(1), "base_digest": match.group(2)}


def check_provenance_args(text: str) -> list[str]:
    missing = [arg for arg in REQUIRED_PROVENANCE_ARGS if f"ARG {arg}=" not in text]
    failures = [f"SC-03: missing provenance arg {arg}" for arg in missing]
    # A default that looks like a real revision would let an unpromotable local
    # build masquerade as a release build.
    if "ARG OCI_IMAGE_REVISION=unknown" not in text:
        failures.append("SC-03: OCI_IMAGE_REVISION default must be 'unknown'")
    if "ARG BRIDGE_BUILD_ID=unknown" not in text:
        failures.append("SC-03: BRIDGE_BUILD_ID default must be 'unknown'")
    return failures


def check_sbom(
    path: Path | None, dependencies: list[dict[str, str]]
) -> tuple[list[str], dict[str, Any]]:
    if path is None:
        return [], {"provided": False}
    if not path.is_file():
        return ["SC-04: declared SBOM missing"], {"provided": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["SC-04: SBOM unreadable"], {"provided": False}
    failures: list[str] = []
    if payload.get("bomFormat") != "CycloneDX":
        failures.append("SC-04: SBOM is not CycloneDX")
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        failures.append("SC-04: SBOM has no components")
        components = []
    names = {str(component.get("name", "")).lower() for component in components}
    for dependency in dependencies:
        canonical = dependency["name"].lower().replace("_", "-")
        if canonical not in names:
            failures.append(f"SC-05: runtime dependency absent from SBOM: {canonical}")
    return failures, {
        "provided": True,
        "component_count": len(components),
        "digest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def check_provenance_file(
    path: Path | None, source_commit: str
) -> tuple[list[str], dict[str, Any]]:
    if path is None:
        return [], {"provided": False}
    if not path.is_file():
        return ["SC-06: declared provenance missing"], {"provided": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["SC-06: provenance unreadable"], {"provided": False}
    failures: list[str] = []
    revision = str(payload.get("revision") or payload.get("OCI_IMAGE_REVISION") or "")
    if source_commit and revision and revision != source_commit:
        failures.append("SC-06: provenance revision does not bind the scanned commit")
    if not revision:
        failures.append("SC-06: provenance carries no revision")
    return failures, {
        "provided": True,
        "digest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbom", type=Path, default=None)
    parser.add_argument("--provenance", type=Path, default=None)
    parser.add_argument("--source-commit", default="")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    pyproject = PYPROJECT.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    failures: list[str] = []
    pin_failures, dependencies = check_pinning(pyproject)
    failures += pin_failures
    base_failures, base = check_base_image(dockerfile)
    failures += base_failures
    failures += check_provenance_args(dockerfile)
    sbom_failures, sbom = check_sbom(args.sbom, dependencies)
    failures += sbom_failures
    provenance_failures, provenance = check_provenance_file(args.provenance, args.source_commit)
    failures += provenance_failures

    report: dict[str, Any] = {
        "schema": "hermes-v2-phase9-supply-chain/1",
        "base_image": base,
        "dependencies": dependencies,
        "failures": sorted(failures),
        "gate": "SUPPLY_CHAIN_OK" if not failures else "SUPPLY_CHAIN_BLOCKED",
        "provenance": provenance,
        "sbom": sbom,
        "source_commit": args.source_commit,
    }
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {"gate": report["gate"], "failures": report["failures"]}, indent=2, sort_keys=True
        )
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
