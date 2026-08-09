#!/usr/bin/env python3
"""Assemble the V2 Phase 2 OUTER final acceptance evidence document.

Inputs (all produced elsewhere, never invented here):

* the inner connected evidence document (collector output);
* the inner gate result (``validate_v2_phase2_connected_gate.py`` output);
* the sanitized out-of-band terminal marker written by
  ``v2_phase2_final_out_of_band_acceptance.py execute``.

The assembler copies only sanitized, non-secret fields into the new final
schema. It repurposes no existing field: the final document is a separate
envelope consumed exclusively by
``scripts/validate_v2_phase2_final_acceptance.py``.

Missing or unparseable inputs produce a document that the final validator will
hard-block; the assembler never fabricates a state-integrity section.
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
    FINAL_EVIDENCE_SCHEMA,
    TOKEN_MEASUREMENT_MODE,
)

_SANITIZED_PROVENANCE_KEYS = (
    "schema",
    "provenance_pass",
    "canonical_tool_id",
    "authorized_tool_call_count",
    "unauthorized_tool_calls_observed",
    "normalization_profile_id",
    "arguments_shape_sha256",
    "internal_normalized_sha256",
    "direct_normalized_sha256",
    "internal_matches_direct",
    "result_size_bucket",
    "tool_call_id_stored",
    "raw_arguments_stored",
    "raw_result_stored",
    "session_id_stored",
    "message_rows_stored",
    "blockers",
)


def _load(path: str | None) -> Any:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def token_reduction_percent(direct_total: int, agentic_total: int) -> float:
    if agentic_total <= 0:
        return 0.0
    return round(100.0 * (agentic_total - direct_total) / agentic_total, 4)


def build_final_evidence(
    *,
    evidence: Any,
    inner_gate: Any,
    state_marker: Any,
) -> dict[str, Any]:
    """Build the final evidence envelope from sanitized inputs."""
    evidence = evidence if isinstance(evidence, dict) else {}
    inner_gate = inner_gate if isinstance(inner_gate, dict) else {}
    aggregate = evidence.get("aggregate") if isinstance(evidence.get("aggregate"), dict) else {}
    samples = evidence.get("samples") if isinstance(evidence.get("samples"), list) else []

    provenance: list[dict[str, Any]] = []
    for sample in samples:
        record = sample.get("tool_provenance") if isinstance(sample, dict) else None
        if not isinstance(record, dict):
            continue
        provenance.append(
            {key: record[key] for key in _SANITIZED_PROVENANCE_KEYS if key in record}
        )

    direct_total = aggregate.get("direct_hermes_llm_tokens")
    agentic_total = aggregate.get("v1_shadow_hermes_llm_tokens")
    direct_total = direct_total if isinstance(direct_total, int) else 0
    agentic_total = agentic_total if isinstance(agentic_total, int) else 0

    document: dict[str, Any] = {
        "schema": FINAL_EVIDENCE_SCHEMA,
        "source_commit": evidence.get("source_commit"),
        "inner_gate": {
            "direct_read_status": inner_gate.get("gate"),
            "failures": inner_gate.get("failures", []),
            "source_commit": inner_gate.get("source_commit"),
            "started_at": evidence.get("started_at"),
            "finished_at": evidence.get("finished_at"),
        },
        "aggregate": {
            "sample_count": aggregate.get("sample_count"),
            "successful_samples": aggregate.get("successful_samples"),
            "semantic_matches": aggregate.get("semantic_matches"),
            "provenance_pass": aggregate.get("provenance_pass"),
            "provenance_fail": aggregate.get("provenance_fail"),
            "token_measurement_mode": TOKEN_MEASUREMENT_MODE,
            "direct_total_tokens": direct_total,
            "agentic_total_tokens": agentic_total,
            "token_reduction_percent": token_reduction_percent(
                direct_total, agentic_total
            ),
            "direct_provider_api_calls": aggregate.get("direct_provider_api_calls"),
            "mutations_observed": aggregate.get("mutations_observed"),
        },
        "provenance": provenance,
        "privacy": {
            "paths_stored": False,
            "row_contents_stored": False,
            "raw_results_stored": False,
            "session_ids_stored": False,
            "salt_stored": False,
        },
    }

    if isinstance(state_marker, dict):
        state = state_marker.get("state_integrity")
        if isinstance(state, dict) and state_marker.get("state") == "COMPLETED":
            document["state_integrity"] = state
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--inner-gate", required=True)
    parser.add_argument("--state-marker")
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args(argv)

    document = build_final_evidence(
        evidence=_load(args.evidence),
        inner_gate=_load(args.inner_gate),
        state_marker=_load(args.state_marker),
    )
    text = json.dumps(document, indent=2, sort_keys=True)
    destination = Path(args.json_out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text + "\n", encoding="utf-8")
    destination.chmod(0o600)
    print(json.dumps({"schema": FINAL_EVIDENCE_SCHEMA, "written": True}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
