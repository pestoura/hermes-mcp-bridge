"""Supply-chain gates for immutable 1.x image provenance."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

SCRIPT = Path("scripts/validate_image_provenance.py")


def _module():
    spec = importlib.util.spec_from_file_location("validate_image_provenance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _evidence(module, **overrides):
    labels = {
        "source": "https://github.com/pestoura/hermes-mcp-bridge",
        "revision": "a" * 40,
        "version": "1.0.0",
        "created": "2026-08-08T01:00:00Z",
        "build_id": "123.1",
        "schema_version": "0.6.1",
        "contract_version": "1.0.0",
    }
    labels.update(overrides.pop("labels", {}))
    return {
        "image_ref": overrides.pop("image_ref", "hermes-mcp-bridge:ci"),
        "image_id": overrides.pop("image_id", "sha256:" + "b" * 64),
        "repo_digests": overrides.pop("repo_digests", []),
        "labels": labels,
        **overrides,
    }


def test_validate_accepts_exact_allow_listed_provenance() -> None:
    module = _module()
    result = module.validate(
        _evidence(module),
        revision="a" * 40,
        version="1.0.0",
        build_id="123.1",
        schema_version="0.6.1",
        contract_version="1.0.0",
    )
    assert result["image_id"].startswith("sha256:")
    assert result["revision"] == "a" * 40
    assert result["tool_contract_count"] == 27
    assert set(result) == {
        "schema",
        "image_ref",
        "image_id",
        "repo_digests",
        "source",
        "revision",
        "version",
        "created",
        "build_id",
        "schema_version",
        "contract_version",
        "tool_contract_count",
    }


def test_validate_fails_closed_on_mismatch_or_unknown_build_metadata() -> None:
    module = _module()
    with pytest.raises(module.ProvenanceError, match="revision"):
        module.validate(
            _evidence(module),
            revision="c" * 40,
            version="1.0.0",
            build_id="123.1",
            schema_version="0.6.1",
            contract_version="1.0.0",
        )

    with pytest.raises(module.ProvenanceError, match="creation"):
        module.validate(
            _evidence(module, labels={"created": "unknown"}),
            revision="a" * 40,
            version="1.0.0",
            build_id="123.1",
            schema_version="0.6.1",
            contract_version="1.0.0",
        )


def test_inspect_rejects_local_tag_before_docker_call(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module,
        "_run_json",
        lambda *_args: pytest.fail("docker inspect must not run for :local"),
    )
    with pytest.raises(module.ProvenanceError, match=":local"):
        module.inspect_image("hermes-mcp-bridge:local")


def test_inspect_reads_only_identity_and_allow_listed_labels(monkeypatch) -> None:
    module = _module()
    labels = {key: f"value-{name}" for name, key in module.LABELS.items()}
    labels["secret-looking-extra-label"] = "must-not-escape"
    payload = [
        {
            "Id": "sha256:" + "d" * 64,
            "RepoDigests": ["example.invalid/bridge@sha256:" + "e" * 64],
            "Config": {
                "Labels": labels,
                "Env": ["SECRET=must-not-escape"],
                "Cmd": ["must-not-escape"],
            },
            "Mounts": [{"Source": "/must/not/escape"}],
        }
    ]
    monkeypatch.setattr(module, "_run_json", lambda *_args: payload)
    result = module.inspect_image("hermes-mcp-bridge:ci")
    rendered = repr(result)
    assert "must-not-escape" not in rendered
    assert set(result["labels"]) == set(module.LABELS)


def test_dockerfile_declares_required_provenance_labels() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    for token in (
        "org.opencontainers.image.source",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.version",
        "org.opencontainers.image.created",
        "io.jarvas.hermes-mcp-bridge.build-id",
        "io.jarvas.hermes-mcp-bridge.schema-version",
        "io.jarvas.hermes-mcp-bridge.contract-version",
    ):
        assert token in dockerfile


def test_ci_builds_and_validates_exact_provenance() -> None:
    workflow_text = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    assert workflow["jobs"]["test"]
    for token in (
        "--build-arg OCI_IMAGE_REVISION=\"${{ github.sha }}\"",
        "--build-arg BRIDGE_BUILD_ID=\"${{ github.run_id }}.${{ github.run_attempt }}\"",
        "scripts/validate_image_provenance.py",
        "--revision \"${{ github.sha }}\"",
        "image-provenance.json",
        "sbom-cyclonedx.json",
    ):
        assert token in workflow_text
