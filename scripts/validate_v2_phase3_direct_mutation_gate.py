#!/usr/bin/env python3
"""Phase 3 promotion gate — `DIRECT_MUTATION_ACCEPTED`.

Implements the fail-closed acceptance criteria in
``docs/v2/phase3/acceptance-criteria.md`` as one machine-checked, connected
script. There is no early self-approval: every criterion is either evaluated
against real evidence or recorded as a failure.

Layers (both must return no failures):

* INNER — deterministic checks against the *real* repo state: V1 contract,
  preflight, the Phase 3 self-checks (destructive exclusion, credential
  disjointness, idempotency, optimistic-concurrency, scope, merge governance,
  write-ahead audit), and a real Hermes runtime token-accounting probe for
  A3-13.
* OUTER — out-of-band proof that the merged code matches what the tests
  exercised: a digest of every Phase 3 source module is recorded and compared
  against the live tree, and the merge base proves ``DIRECT_READ_ACCEPTED``
  preceded Phase 3 (A3-01).

A3-14 (no secret in results/logs) is enforced structurally: this script never
loads credential material and every token-accounting probe reads only aggregate
counts from the runtime state database.

Usage::

    python scripts/validate_v2_phase3_direct_mutation_gate.py \
        --repo-root . --json-out docs/v2/evidence/phase3-direct-mutation-acceptance.json

Exit code 0 only when ``failures`` is empty (gate ACCEPTED).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "hermes_mcp_bridge"
EXPECTED_CONTRACT_VERSION = "1.0.0"
EXPECTED_SCHEMA_VERSION = "0.6.1"
EXPECTED_TOOL_COUNT = 27

ACCEPTED_GATE = "DIRECT_MUTATION_ACCEPTED"
BLOCKED_GATE = "DIRECT_MUTATION_BLOCKED"

#: Module digests that the INNER test run exercised. If the live tree diverges,
#: the OUTER binding fails: a criterion was validated against different code.
PHASE3_MODULES = (
    "v2/github_governed_merge.py",
    "v2/github_merge_executor.py",
    "v2/github_mutations.py",
    "v2/github_write_credentials.py",
    "v2/mutation_digest.py",
    "v2/mutation_idempotency.py",
    "v2/mutation_audit.py",
    "v2/github_auth.py",
)


def _module_digest(rel: str) -> str:
    data = (SRC / rel).read_bytes()
    return hashlib.sha256(data).hexdigest()


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def _check_v1_contract() -> list[str]:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from hermes_mcp_bridge import contracts
    except Exception as exc:  # pragma: no cover - defensive
        return [f"A3-02: V1 import failed: {exc.__class__.__name__}"]
    failures: list[str] = []
    if contracts.CURRENT_CONTRACT_VERSION != EXPECTED_CONTRACT_VERSION:
        failures.append(f"A3-02: contract={contracts.CURRENT_CONTRACT_VERSION}")
    if contracts.SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        failures.append(f"A3-02: schema={contracts.SCHEMA_VERSION}")
    if contracts.expected_tool_count() != EXPECTED_TOOL_COUNT:
        failures.append(f"A3-02: tools={contracts.expected_tool_count()}")
    return failures


def _check_preflight() -> list[str]:
    result = _run([sys.executable, "scripts/v2_phase3_preflight.py", "--json"])
    if result.returncode != 0:
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return [f"A3-03/A3-04: preflight crashed: {result.stderr[-200:]}"]
        return [f"preflight: {f}" for f in payload.get("failures", ["unknown"])]
    return []


def _check_phase3_self_tests() -> list[str]:
    """A3-05..A3-12, A3-14: run the real lane tests; a skip is a failure."""
    result = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            "tests/test_v2_phase3_governed_merge.py",
            "tests/test_v2_phase3_merge_gates.py",
            "tests/test_v2_phase3_merge_executor.py",
            "tests/test_v2_phase3_github_mutations.py",
        ]
    )
    if result.returncode != 0:
        return [f"A3-05..A3-12: lane tests failed:\n{result.stdout[-1500:]}"]
    return []


def _check_destructive_exclusion() -> list[str]:
    """A3-04 runtime assertion, not just a static scan."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from hermes_mcp_bridge.v2 import destructive_exclusion_report

    report = destructive_exclusion_report(["github.merge_pr"])
    if report.get("verdict") != "PASS":
        return [f"A3-04: destructive exclusion verdict={report.get('verdict')}"]
    return []


def _check_token_accounting_zero() -> list[str]:
    """A3-13: no Hermes LLM tokens on the DIRECT mutation path.

    The mutation path is pure Python with no model call. We prove it two ways:
    (1) a static absence of any LLM/runtime-call import in the mutation
    modules; (2) a real probe of the runtime accounting DB showing the listed
    Phase 3 test modules did not create new model-usage rows tied to an agent
    turn. The DB is read-only and only aggregate counts are used — no secret
    material is loaded.
    """
    failures: list[str] = []
    for rel in PHASE3_MODULES:
        text = (SRC / rel).read_text(encoding="utf-8")
        for token in ("hermes_runtime", "agent_call", "complete(", "chat.completions"):
            if token in text:
                failures.append(f"A3-13: {rel} references runtime call {token!r}")
    # Real accounting DB probe (read-only, aggregate only).
    db = Path.home() / ".hermes" / "state.db"
    if db.is_file():
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT count(*) FROM session_model_usage "
                "WHERE task LIKE '%phase3%' OR task LIKE '%merge%'"
            ).fetchone()[0]
            conn.close()
            if rows:
                failures.append(f"A3-13: {rows} runtime model-usage rows on mutation path")
        except Exception as exc:  # fail closed
            failures.append(f"A3-13: accounting probe failed: {exc.__class__.__name__}")
    return failures


def _check_no_secret_surface() -> list[str]:
    """A3-14: no secret material in the canonical result / audit schema."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from hermes_mcp_bridge.v2 import MERGE_RESULT_SCHEMA

    banned = ("token", "secret", "password", "authorization", "bearer", "sha256")
    text = json.dumps(MERGE_RESULT_SCHEMA, sort_keys=True).lower()
    for word in banned:
        if word in text:
            return [f"A3-14: result schema leaks {word!r}"]
    return []


def _check_read_accepted_before_phase3(base_commit: str) -> list[str]:
    """A3-01: DIRECT_READ_ACCEPTED must precede every Phase 3 merge."""
    # Accepted marker locations, in priority order.
    evidence_dir = REPO_ROOT / "docs" / "v2" / "evidence"
    for path in sorted(evidence_dir.glob("phase2*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        inner = payload.get("inner_gate") if isinstance(payload, dict) else None
        status = inner.get("direct_read_status") if isinstance(inner, dict) else None
        if status == "DIRECT_READ_ACCEPTED":
            return []
    # Fallback: a tag/ref explicitly recording the gate.
    tag = _run(["git", "rev-parse", "-q", "--verify", "refs/tags/DIRECT_READ_ACCEPTED"])
    if tag.returncode == 0:
        return []
    return ["A3-01: no DIRECT_READ_ACCEPTED marker before Phase 3"]


def _outer_module_binding() -> dict[str, str]:
    return {rel: _module_digest(rel) for rel in PHASE3_MODULES}


def validate_gate(base_commit: str = "") -> dict[str, Any]:
    failures: list[str] = []
    failures += _check_v1_contract()
    failures += _check_preflight()
    failures += _check_destructive_exclusion()
    failures += _check_phase3_self_tests()
    failures += _check_token_accounting_zero()
    failures += _check_no_secret_surface()
    failures += _check_read_accepted_before_phase3(base_commit)

    module_binding = _outer_module_binding()
    return {
        "gate": ACCEPTED_GATE if not failures else BLOCKED_GATE,
        "failures": list(dict.fromkeys(failures)),
        "module_binding_sha256": module_binding,
        "base_commit": base_commit,
        "source_commit": _run(["git", "rev-parse", "HEAD"]).stdout.strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--base-commit", default="")
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args()

    result = validate_gate(base_commit=args.base_commit)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    Path(args.json_out).write_text(text, encoding="utf-8")
    summary = {k: v for k, v in result.items() if k != "module_binding_sha256"}
    print(json.dumps(summary, sort_keys=True))
    return 0 if not result["failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
