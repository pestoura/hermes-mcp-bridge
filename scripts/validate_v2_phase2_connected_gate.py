#!/usr/bin/env python3
"""Strict Phase 2 promotion gate: connected evidence + isolated V1 shadow proof.

The original connected evidence validator remains a required component. This
promotion gate adds the mechanically verified `read_only_credential_enforced`
proof so that a bare mutation-basis string can never promote Phase 2.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hermes_mcp_bridge.v2.shadow_isolation import validate_shadow_isolation  # noqa: E402

LEGACY_VALIDATOR = ROOT / "scripts" / "validate_v2_phase2_direct_read_evidence.py"
ACCEPTED_GATE = "DIRECT_READ_ACCEPTED"
BLOCKED_GATE = "DIRECT_READ_BLOCKED"
REQUIRED_SHADOW_BASIS = "read_only_credential_enforced"


def _load_legacy_validator() -> Any:
    spec = importlib.util.spec_from_file_location("phase2_connected_validator", LEGACY_VALIDATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("CONNECTED_VALIDATOR_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_json(path: Path, failure: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(failure) from exc
    if not isinstance(payload, dict):
        raise ValueError(failure)
    return payload


def validate_gate(
    connected: dict[str, Any],
    shadow_isolation: dict[str, Any],
) -> list[str]:
    failures = list(_load_legacy_validator().validate_evidence(connected))

    basis = connected.get("window_integrity_basis")
    shadow_basis = basis.get("shadow_mutation_basis") if isinstance(basis, dict) else None
    if shadow_basis != REQUIRED_SHADOW_BASIS:
        failures.append("shadow_isolation_basis_not_enforced")

    source_commit = connected.get("source_commit")
    provider = connected.get("github_provider")
    scopes = provider.get("repository_scopes") if isinstance(provider, dict) else None
    if not isinstance(source_commit, str) or not isinstance(scopes, list):
        failures.append("shadow_isolation_binding_unavailable")
    else:
        failures.extend(
            validate_shadow_isolation(
                shadow_isolation,
                repositories=scopes,
                source_commit=source_commit,
            )
        )
    return list(dict.fromkeys(failures))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence")
    parser.add_argument("--shadow-isolation", required=True)
    parser.add_argument("--json-out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        connected = _load_json(Path(args.evidence), "connected_evidence_unreadable")
        shadow = _load_json(Path(args.shadow_isolation), "shadow_isolation_unreadable")
        failures = validate_gate(connected, shadow)
    except (ValueError, RuntimeError) as exc:
        failures = [str(exc)]
        connected = {}

    gate = ACCEPTED_GATE if not failures else BLOCKED_GATE
    result = {
        "failures": failures,
        "gate": gate,
        "source_commit": connected.get("source_commit"),
    }
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    Path(args.json_out).write_text(text, encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
