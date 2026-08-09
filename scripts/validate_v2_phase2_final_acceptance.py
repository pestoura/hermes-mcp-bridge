#!/usr/bin/env python3
"""Fail-closed validator for the V2 Phase 2 OUTER final acceptance evidence.

This validator is additive to ``validate_v2_phase2_direct_read_evidence.py`` and
``validate_v2_phase2_connected_gate.py``, which remain the inner
semantic/economics gate. It introduces its own schema, its own fields and its
own manifest — no existing field is repurposed.

``overall_status`` is ``ACCEPTED`` only when every strict condition documented
in ``docs/v2/phase2-final-outer-gate.md`` holds. It emits only stable sanitized
reasons; a missing or unmeasurable state-integrity document is a hard block.

The validator never reads raw stdout/stderr of the out-of-band probe and never
promotes anything from it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:  # pragma: no cover - path shim
    sys.path.insert(0, str(_SRC))

from hermes_mcp_bridge.v2.final_gate import (  # noqa: E402
    STATUS_ACCEPTED,
    final_manifest,
    validate_final_evidence,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)

    try:
        payload = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result: dict[str, Any] = {
            "schema": "hermes-v2-phase2-final-manifest/1",
            "overall_status": "BLOCKED",
            "reasons": ["final_evidence_unreadable"],
            "source_commit": None,
            "inner_gate_required": True,
            "outer_gate_required": True,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1

    failures = validate_final_evidence(payload)
    manifest = final_manifest(payload, failures)
    text = json.dumps(manifest, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        destination = Path(args.json_out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text + "\n", encoding="utf-8")
        destination.chmod(0o600)
    return 0 if manifest["overall_status"] == STATUS_ACCEPTED else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
