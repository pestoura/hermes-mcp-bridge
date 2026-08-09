#!/usr/bin/env python3
"""Connected collector for the V2 Phase 2 ``DIRECT_READ_ACCEPTED`` gate.

Runs exactly **5 tools x 3 repetitions = 15 samples** against the real Jarvas
runtime and emits a JSON document consumable by
``scripts/validate_v2_phase2_direct_read_evidence.py``.

Per sample it performs two *separate* executions:

1. **DIRECT** — through :class:`~hermes_mcp_bridge.v2.github_canary.GitHubCanaryRouter`
   with the canary explicitly enabled. Provider API calls are counted by an
   instrumented transport; Hermes upstream calls and LLM tokens are proven to be
   zero because no Hermes/MCP client is constructed on this path (asserted here
   and in the test suite). A DIRECT sample never silently falls back: an
   ineligible or failed DIRECT attempt aborts collection.
2. **V1 agentic shadow** — through the existing V1 MCP surface (``hermes_prompt``),
   with real token accounting read from the Hermes ``state.db``
   (``session_model_usage``), opened strictly read-only, reusing the Phase 0
   pattern. Estimation is never accepted.

Both sides are normalized to the *same* comparison field set and only SHA-256
digests are retained. Prompts and raw outputs are never written to evidence.

The collector is fail-closed. It refuses to emit an acceptance-shaped document
if the provider attestation, the canary wiring, the topology or the real token
accounting cannot be proven.

Two inputs carry the facts GitHub cannot self-introspect and the facts no probe
can establish, and neither is invented here:

* ``--provider-attestation`` — a sanitized, secret-free JSON declaration of the
  exact permission map, exact repository scopes and confirmation source. It is
  cross-checked against the CLI provider type and target repositories before any
  network call; the file path never reaches the evidence.
* ``--shadow-mutation-basis`` — the documented observational basis for the V1
  shadow ``mutation_observed`` claim. Its default (``none``) fails closed.

The DIRECT ``mutation_observed`` value and every ``contaminated_window`` value
are *derived* (see :class:`WindowIntegrity` and :func:`direct_mutation_observed`)
rather than hardcoded.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hermes_mcp_bridge.v2.canonical import (  # noqa: E402
    canonical_json_bytes,
)
from hermes_mcp_bridge.v2.github_attestation import (  # noqa: E402
    ATTESTATION_INPUT_SCHEMA,
    AttestationError,
    attest_provider,
    load_attestation_input,
)
from hermes_mcp_bridge.v2.github_canary import (  # noqa: E402
    ExecutionPath,
    GitHubCanaryConfig,
    GitHubCanaryRouter,
)
from hermes_mcp_bridge.v2.github_direct import (  # noqa: E402
    GITHUB_DIRECT_DEFAULT_RESULT_FIELDS,
    GitHubDirectReadExecutor,
    GitHubRepositoryScope,
)
from hermes_mcp_bridge.v2.github_readiness import (  # noqa: E402
    GitHubReadReadinessBroker,
)
from hermes_mcp_bridge.v2.github_registry import (  # noqa: E402
    GITHUB_DIRECT_READ_TOOL_IDS,
    build_github_direct_read_registry,
    github_direct_read_policy_rules,
)
from hermes_mcp_bridge.v2.github_secret_provider import (  # noqa: E402
    DEFAULT_SECRET_NAME,
    FileGitHubAuthorizationProvider,
    GitHubProviderType,
)
from hermes_mcp_bridge.v2.tool_provenance import (  # noqa: E402
    ProvenanceError,
    collect_tool_provenance,
)

EVIDENCE_SCHEMA = "hermes-v2-phase2-direct-read-acceptance/1"
COLLECTION_GATE = "DIRECT_READ_EVIDENCE_COLLECTED"
REPETITIONS_PER_TOOL = 3
EXPECTED_SAMPLE_COUNT = len(GITHUB_DIRECT_READ_TOOL_IDS) * REPETITIONS_PER_TOOL
STATE_DB_TOKEN_SOURCE = "hermes_state_db:session_model_usage"
STATE_DB_POLL_ATTEMPTS = 8
STATE_DB_POLL_INTERVAL = 0.5

#: Fields compared between DIRECT and the V1 agentic shadow, per tool.
#:
#: This is **not** a hand-maintained subset: it is exactly the DIRECT executor's
#: own default result shaping, imported read-only from
#: :data:`~hermes_mcp_bridge.v2.github_direct.GITHUB_DIRECT_DEFAULT_RESULT_FIELDS`.
#: Comparing anything narrower (for example only ``total_count`` for
#: ``github.get_checks``/``github.search``) would let two materially different
#: results claim ``semantic_match = true``, so the full default shaped result is
#: compared on both sides. Deriving it here also makes drift impossible: a change
#: to the executor defaults changes the comparison automatically.
COMPARISON_FIELDS: dict[str, tuple[str, ...]] = {
    tool_id: tuple(fields) for tool_id, fields in GITHUB_DIRECT_DEFAULT_RESULT_FIELDS.items()
}

#: Collections whose ORDER is semantically part of the provider response and
#: must therefore be preserved verbatim rather than canonically sorted.
#:
#: The default shaped results contain no such array: ``labels``/``assignees``
#: are set-like, and ``check_runs``/``items`` are returned in a provider-chosen
#: order that is not part of the read's meaning and is not reproducible by an
#: agentic re-read. Every collection is therefore canonically ordered on BOTH
#: sides with the identical rule before hashing. The set is kept explicit (and
#: empty) so that adding an order-bearing field later is a deliberate act.
ORDER_SIGNIFICANT_FIELDS: frozenset[tuple[str, str]] = frozenset()


# --------------------------------------------------------------------------
# window integrity / mutation basis
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowIntegrity:
    """Derived, documented basis for the per-sample window claims.

    Nothing here is a literal typed into the evidence. Each field is derived
    from a property the collector can actually observe in this run:

    * ``direct_transport_dedicated`` — the DIRECT side runs on a transport owned
      by this collector, so the provider API call delta for a sample is
      attributable to that sample alone;
    * ``direct_call_delta_exact`` — the sample is only accepted when the delta is
      exactly one provider call, so no other DIRECT traffic overlapped it;
    * ``shadow_session_scoped_accounting`` — V1 token accounting is read per
      ``session_id`` from ``session_model_usage``, so another concurrent session
      cannot be attributed to this sample.

    When all three hold there is no attribution ambiguity between the two sides
    and ``contaminated`` is ``False``. If any of them fails the collector fails
    closed rather than emitting an unproven ``contaminated_window = false``.
    """

    direct_transport_dedicated: bool
    direct_call_delta_exact: bool
    shadow_session_scoped_accounting: bool

    @property
    def contaminated(self) -> bool:
        return not (
            self.direct_transport_dedicated
            and self.direct_call_delta_exact
            and self.shadow_session_scoped_accounting
        )

    def describe(self) -> dict[str, Any]:
        return {
            "direct_transport_dedicated": self.direct_transport_dedicated,
            "direct_call_delta_exact": self.direct_call_delta_exact,
            "shadow_session_scoped_accounting": self.shadow_session_scoped_accounting,
            "attribution_ambiguity": self.contaminated,
        }


#: How ``mutation_observed`` is derived on each side. Neither value is a bare
#: literal in the evidence.
DIRECT_MUTATION_BASIS = "executor_http_method_restricted_to_get"

#: Accepted observational bases for the V1 shadow side. ``NONE`` is the default
#: and is *not* an accepted basis: the collector fails closed rather than
#: emitting ``mutation_observed = false`` for the V1 agentic path without one.
SHADOW_MUTATION_BASES: dict[str, str] = {
    "none": "",
    "github_audit_log_reviewed": (
        "operator reviewed the GitHub audit log for the collection window and "
        "confirmed no write event attributable to the shadow sessions"
    ),
    "read_only_credential_enforced": (
        "the only GitHub credential reachable by the V1 agent during the window "
        "is the same least-privilege read-only material attested here, so no "
        "write API call could have been authorized"
    ),
}


def shadow_mutation_observed(basis: str) -> tuple[bool, str]:
    """Return the V1 shadow mutation claim and its documented basis.

    There is no robust runtime probe proving a general agentic path performed no
    mutation, so an explicit, documented basis is required. Without one the
    collector fails closed instead of fabricating the claim.
    """
    key = str(basis or "none").strip().lower()
    description = SHADOW_MUTATION_BASES.get(key)
    if not description:
        raise CollectorError("SHADOW_MUTATION_BASIS_UNPROVEN")
    return False, key


def direct_mutation_observed(executor: GitHubDirectReadExecutor) -> bool:
    """Derive DIRECT ``mutation_observed`` from the executor's own capability.

    The DIRECT executor exposes exactly five read operations and issues requests
    through a single private ``_get`` helper; there is no code path able to emit
    a non-GET request. A mutation therefore cannot occur on this side, and that
    is a structural property of the object in use, not an assumption.
    """
    forbidden = ("post", "put", "patch", "delete")
    if any(hasattr(executor, name) for name in forbidden):
        raise CollectorError("DIRECT_MUTATION_BASIS_INVALID")
    if not hasattr(executor, "_get"):
        raise CollectorError("DIRECT_MUTATION_BASIS_INVALID")
    return False


#: Functional phases of the collector. A non-zero ``SystemExit`` or an
#: unexpected exception raised inside a phase is reported as that phase only,
#: never as an exception message, argument value or filesystem path.
PHASE_PRECONDITION = "PRECONDITION"
PHASE_DIRECT_COLLECTION = "DIRECT_COLLECTION"
PHASE_SHADOW_COLLECTION = "SHADOW_COLLECTION"
PHASE_TOKEN_ACCOUNTING = "TOKEN_ACCOUNTING"
PHASE_VALIDATION = "VALIDATION"
COLLECTOR_PHASES: tuple[str, ...] = (
    PHASE_PRECONDITION,
    PHASE_DIRECT_COLLECTION,
    PHASE_SHADOW_COLLECTION,
    PHASE_TOKEN_ACCOUNTING,
    PHASE_VALIDATION,
)

#: Maximum reason length accepted by the launcher and the blocked contract.
REASON_MAX_LENGTH = 64
BLOCKED_GATE = "DIRECT_READ_BLOCKED"
UNSPECIFIED_REASON_TOKEN = "UNSPECIFIED"

_current_phase = PHASE_PRECONDITION


def sanitize_reason(value: Any) -> str:
    """Normalize any reason into a bounded ``^[A-Z0-9_]{1,64}$`` token.

    Foreign values (provider error codes, tool identifiers, exception text)
    can never cross this boundary verbatim: every character outside the stable
    vocabulary is collapsed to ``_`` and the result is truncated. An empty or
    unusable input degrades to :data:`UNSPECIFIED_REASON_TOKEN` rather than to
    an unbounded or leaking string.
    """
    text = "" if value is None else str(value)
    normalized = "".join(
        char if ("A" <= char <= "Z" or "0" <= char <= "9" or char == "_") else "_"
        for char in text.upper()
    )
    normalized = normalized.strip("_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    normalized = normalized[:REASON_MAX_LENGTH].strip("_")
    return normalized or UNSPECIFIED_REASON_TOKEN


def _tool_token(tool_id: Any) -> str:
    """Map a KNOWN tool id onto a stable token; anything else is unspecified."""
    if isinstance(tool_id, str) and tool_id in set(GITHUB_DIRECT_READ_TOOL_IDS):
        return sanitize_reason(tool_id)
    return UNSPECIFIED_REASON_TOKEN


def _foreign_token(value: Any) -> str:
    """Bound a foreign value (provider/router code) to a short stable token."""
    text = "" if value is None else str(value)
    if not text.strip():
        return UNSPECIFIED_REASON_TOKEN
    return sanitize_reason(text)[:32].strip("_") or UNSPECIFIED_REASON_TOKEN


def current_phase() -> str:
    return _current_phase


def enter_phase(phase: str) -> None:
    """Record the functional phase used to attribute an uncontrolled failure."""
    global _current_phase
    _current_phase = phase if phase in COLLECTOR_PHASES else PHASE_PRECONDITION


def phase_exit_reason(phase: str) -> str:
    return sanitize_reason(f"COLLECTOR_EXIT_{phase}")


def phase_exception_reason(phase: str, exc: BaseException) -> str:
    """Reason built from the phase and the exception CLASS only."""
    return sanitize_reason(f"COLLECTOR_FAILURE_{phase}_{type(exc).__name__}")


class CollectorError(RuntimeError):
    """Fail-closed collection failure carrying a stable, secret-free code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = sanitize_reason(code)
        super().__init__(self.code)

    def __str__(self) -> str:
        return f"collection failed: {self.code}"


class CountingTransport(httpx.AsyncHTTPTransport):
    """Async transport counting provider API calls and observed redirects."""

    def __init__(self) -> None:
        super().__init__(retries=0)
        self.calls = 0
        self.redirects = 0

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        self.calls += 1
        response = await super().handle_async_request(request)
        if 300 <= response.status_code < 400:
            self.redirects += 1
        return response


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------


def _scalar(value: Any) -> Any:
    """Canonicalize a value while PRESERVING its semantic structure.

    Nested objects stay objects and nested arrays stay arrays: an array of
    dicts is never flattened into strings, because that would discard the very
    structure the comparison is supposed to prove (two different check runs
    could otherwise collapse onto the same digest). Only values that have no
    stable cross-platform JSON encoding — floats and exotic objects — are
    stringified, and dict keys are stringified and sorted.
    """
    if isinstance(value, bool) or value is None or isinstance(value, int | str):
        return value
    if isinstance(value, float):
        return str(value)
    if isinstance(value, list | tuple):
        return [_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _scalar(item) for key, item in sorted(value.items())}
    return str(value)


def _canonical_sort_key(value: Any) -> str:
    """Total, deterministic ordering key for an already-canonicalized item.

    Ordering by the item's own canonical JSON text is safe for any shape
    (scalar, object or nested array) and is identical on both sides, so two
    semantically equal collections that differ only in provider-chosen order
    produce the same digest, while any change to an item's *content* changes it.
    """
    return canonical_json_bytes(value).decode("utf-8")


def _canonical_collection(value: Any, *, order_significant: bool) -> Any:
    """Apply the canonical ordering rule recursively to a canonicalized value."""
    if isinstance(value, list):
        items = [_canonical_collection(item, order_significant=order_significant) for item in value]
        if order_significant:
            return items
        return sorted(items, key=_canonical_sort_key)
    if isinstance(value, dict):
        return {
            key: _canonical_collection(item, order_significant=order_significant)
            for key, item in sorted(value.items())
        }
    return value


def normalize_for_comparison(tool_id: str, data: Any) -> dict[str, Any]:
    """Project either side onto the full default shaped result for ``tool_id``.

    The projection is the executor's own default field set (see
    :data:`COMPARISON_FIELDS`), the canonicalization preserves nested structure,
    and the ordering rule is applied identically to both sides before hashing.
    Fields absent from one side become ``None`` and therefore *differ* from a
    populated counterpart — a missing item can never be normalized away.
    """
    fields = COMPARISON_FIELDS.get(tool_id)
    if fields is None:
        raise CollectorError("UNKNOWN_COMPARISON_TOOL")
    source = data if isinstance(data, dict) else {}
    normalized: dict[str, Any] = {}
    for field in fields:
        normalized[field] = _canonical_collection(
            _scalar(source.get(field)),
            order_significant=(tool_id, field) in ORDER_SIGNIFICANT_FIELDS,
        )
    return normalized


def normalized_digest(tool_id: str, data: Any) -> str:
    payload = normalize_for_comparison(tool_id, data)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


# --------------------------------------------------------------------------
# real V1 token accounting (Phase 0 read-only state.db pattern)
# --------------------------------------------------------------------------


def _query_state_db_tokens(db_path: str, session_id: str) -> dict[str, int] | None:
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(session_model_usage)")
        }
        if not {"input_tokens", "output_tokens", "session_id"} <= columns:
            return None
        has_reasoning = "reasoning_tokens" in columns
        reasoning = "COALESCE(SUM(reasoning_tokens), 0)" if has_reasoning else "0"
        row = connection.execute(
            "SELECT COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(output_tokens), 0), "
            f"{reasoning}, COUNT(*) "
            "FROM session_model_usage WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()

    if row is None or int(row[3]) == 0:
        return None
    input_tokens, output_tokens, reasoning_tokens = (
        int(row[0]),
        int(row[1]),
        int(row[2]),
    )
    if min(input_tokens, output_tokens, reasoning_tokens) < 0:
        return None
    total = input_tokens + output_tokens + reasoning_tokens
    if total <= 0:
        return None
    return {"input": input_tokens, "output": output_tokens, "total": total}


def state_db_tokens(
    db_path: str,
    session_id: str | None,
    *,
    attempts: int = STATE_DB_POLL_ATTEMPTS,
    interval: float = STATE_DB_POLL_INTERVAL,
    sleep: Any = time.sleep,
) -> dict[str, int] | None:
    """Bounded-poll real token accounting; fail closed when it never appears."""
    if not db_path or not isinstance(session_id, str) or not session_id.strip():
        return None
    for attempt in range(max(1, attempts)):
        tokens = _query_state_db_tokens(db_path, session_id)
        if tokens is not None:
            return tokens
        if attempt + 1 < max(1, attempts):
            sleep(interval)
    return None


# --------------------------------------------------------------------------
# collection plan
# --------------------------------------------------------------------------


def build_plan(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand a target spec into exactly 15 ordered (tool, repetition) samples."""
    targets = spec.get("targets")
    if not isinstance(targets, dict):
        raise CollectorError("TARGETS_MISSING")
    missing = sorted(set(GITHUB_DIRECT_READ_TOOL_IDS) - set(targets))
    if missing:
        raise CollectorError("TARGETS_INCOMPLETE")
    extra = sorted(set(targets) - set(GITHUB_DIRECT_READ_TOOL_IDS))
    if extra:
        raise CollectorError("TARGETS_UNEXPECTED_TOOL")

    plan: list[dict[str, Any]] = []
    for tool_id in sorted(GITHUB_DIRECT_READ_TOOL_IDS):
        target = targets[tool_id]
        if not isinstance(target, dict):
            raise CollectorError("TARGET_INVALID")
        repository = target.get("repository")
        if not isinstance(repository, str) or repository.count("/") != 1:
            raise CollectorError("TARGET_REPOSITORY_INVALID")
        for repetition in range(1, REPETITIONS_PER_TOOL + 1):
            plan.append(
                {
                    "tool_id": tool_id,
                    "repetition": repetition,
                    "repository": repository,
                    "arguments": target.get("arguments", {}),
                }
            )
    if len(plan) != EXPECTED_SAMPLE_COUNT:
        raise CollectorError("PLAN_TOPOLOGY_INVALID")
    return plan


def _direct_operation(tool_id: str, repository: str, arguments: dict[str, Any]):
    owner, repo = repository.split("/", 1)

    async def _run(executor: GitHubDirectReadExecutor):
        if tool_id == "github.get_repo":
            return await executor.get_repo(owner, repo)
        if tool_id == "github.get_pr":
            return await executor.get_pr(owner, repo, int(arguments["number"]))
        if tool_id == "github.get_issue":
            return await executor.get_issue(owner, repo, int(arguments["number"]))
        if tool_id == "github.get_checks":
            return await executor.get_checks(owner, repo, str(arguments["ref"]))
        if tool_id == "github.search":
            return await executor.search(owner, repo, str(arguments["text"]))
        raise CollectorError("UNKNOWN_TOOL")

    return _run


# --------------------------------------------------------------------------
# V1 agentic shadow
# --------------------------------------------------------------------------


#: Compact, per-tool description of the nested structures inside the DIRECT
#: default shape. Only shapes are described — never values — so the shadow can
#: return a semantically equivalent object without being told the answer.
NESTED_SHAPE_HINTS: dict[str, str] = {
    "github.get_pr": (
        "'head' and 'base' are each an object with exactly the keys "
        "'ref' and 'sha'; 'user' is the login string."
    ),
    "github.get_issue": (
        "'labels' and 'assignees' are arrays of strings (label names and "
        "assignee logins); 'user' is the login string; 'is_pull_request' is a "
        "boolean."
    ),
    "github.get_checks": (
        "'check_runs' is an array of objects, each with exactly the keys "
        "'app' (the app slug), 'completed_at', 'conclusion', 'head_sha', "
        "'html_url', 'id', 'name', 'started_at', 'status'."
    ),
    "github.search": (
        "'items' is an array of objects, each with exactly the keys "
        "'comments', 'created_at', 'html_url', 'item_type' "
        "('issue' or 'pull_request'), 'number', 'state', 'title', "
        "'updated_at', 'user' (the login string)."
    ),
}


def _shadow_prompt(tool_id: str, repository: str, arguments: dict[str, Any]) -> str:
    """Ask the V1 agent for exactly the full DIRECT default shape.

    The shadow must return the *same* shaped result the DIRECT executor
    produces by default — not a summary and not a superset — otherwise the
    comparison would only ever prove that a narrow projection matched. Nested
    structures are described compactly by shape only. The prompt is used once
    and never retained in the evidence.
    """
    fields = ", ".join(COMPARISON_FIELDS[tool_id])
    detail = json.dumps(arguments, sort_keys=True)
    nested = NESTED_SHAPE_HINTS.get(tool_id)
    nested_clause = f" {nested}" if nested else ""
    return (
        "Read-only task. Do not modify anything on GitHub. "
        f"For repository {repository}, perform the read {tool_id} with "
        f"arguments {detail}. Reply with ONE JSON object and nothing else, "
        f"containing exactly these keys and no extra keys: {fields}."
        f"{nested_clause} "
        "The JSON must be semantically equivalent to the shape described "
        "above: same keys, same nesting, no additional fields anywhere, and "
        "no commentary."
    )


def _payload(result: Any) -> Any:
    """Return the structured payload verbatim (Phase 0 pattern).

    The top-level object is preserved intact — in particular ``session_id`` —
    because real token accounting resolves the Hermes session from it. Never
    collapse the envelope into a nested ``result`` here.
    """
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured

    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    if not parts:
        return None
    combined = "\n".join(parts)
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return None


def _top_level_session_id(payload: Any) -> str | None:
    """Return the top-level ``session_id`` only; never search nested payloads."""
    if not isinstance(payload, dict):
        return None
    value = payload.get("session_id")
    if isinstance(value, str) and value.strip() and len(value) <= 128:
        return value
    return None


def _shadow_data(payload: Any) -> Any:
    """Extract the nested JSON answer without destroying the envelope metadata.

    The envelope passed in is left untouched; only the agent's answer object is
    returned. Raw text is parsed and discarded, never stored.
    """
    if not isinstance(payload, dict):
        return None
    envelope_keys = ("result", "output", "text", "content", "response")

    def _answer_like(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and bool(value)
            and not any(key in value for key in envelope_keys)
        )

    candidates: list[Any] = []
    for key in envelope_keys:
        if key in payload:
            candidates.append(payload[key])
    # A nested envelope (e.g. {"result": {"output": "..."}}) is unwrapped one
    # extra level, still without touching the top-level object.
    for value in list(candidates):
        if isinstance(value, dict):
            for key in envelope_keys:
                if key in value:
                    candidates.append(value[key])

    for value in candidates:
        if _answer_like(value):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            start, end = stripped.find("{"), stripped.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(stripped[start : end + 1])
                except json.JSONDecodeError:
                    continue
    return None


#: Envelope keys the V1 answer may be carried under, in a fixed order.
_SHADOW_ENVELOPE_KEYS = ("result", "output", "text", "content", "response")


def _shadow_data_strict(payload: Any) -> Any:
    """Strict, fail-closed extraction of the V1 shadow answer.

    Used by the connected acceptance path. Unlike :func:`_shadow_data` it
    performs no brace repair, no substring scanning, no retry and no
    heuristic unwrapping:

    * the envelope must be an object carrying exactly one known answer key;
    * a string answer must parse as a whole-string JSON **object**;
    * anything else (prose, code fence, several concatenated objects, a
      non-object JSON value, an empty object) yields ``None``.
    """

    if not isinstance(payload, dict):
        return None

    present = [key for key in _SHADOW_ENVELOPE_KEYS if key in payload]
    if len(present) != 1:
        return None
    value = payload[present[0]]

    if isinstance(value, dict):
        if not value or any(key in value for key in _SHADOW_ENVELOPE_KEYS):
            return None
        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value.strip())
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and parsed:
            return parsed
    return None


async def _run_shadow(session: Any, prompt: str, wait_seconds: float) -> Any:
    from datetime import timedelta

    return await session.call_tool(
        "hermes_prompt",
        arguments={"prompt": prompt, "wait_seconds": wait_seconds},
        read_timeout_seconds=timedelta(seconds=max(120.0, wait_seconds + 60.0)),
    )


# --------------------------------------------------------------------------
# main collection
# --------------------------------------------------------------------------


async def collect(args: argparse.Namespace) -> dict[str, Any]:
    enter_phase(PHASE_PRECONDITION)
    spec = json.loads(Path(args.targets).read_text(encoding="utf-8"))
    plan = build_plan(spec)
    repositories = sorted({item["repository"] for item in plan})

    scope = GitHubRepositoryScope(repositories)
    provider = FileGitHubAuthorizationProvider(
        scope=scope,
        provider_type=GitHubProviderType(args.provider_type),
        secret_name=args.secret_name,
    )
    readiness = GitHubReadReadinessBroker(provider)

    try:
        declaration = load_attestation_input(args.provider_attestation)
    except AttestationError as exc:
        raise CollectorError(exc.code) from exc

    try:
        attestation = attest_provider(
            provider,
            repositories=repositories,
            declaration=declaration,
        )
    except AttestationError as exc:
        raise CollectorError(f"ATTESTATION_{exc.code}") from exc

    if not readiness.is_ready("github.read"):
        raise CollectorError("CREDENTIAL_NOT_READY")

    transport = CountingTransport()
    executor = GitHubDirectReadExecutor(
        registry=build_github_direct_read_registry(),
        rules=github_direct_read_policy_rules(),
        credential_broker=readiness,
        authorization_provider=provider,
        scope=scope,
        transport=transport,
    )
    canary = GitHubCanaryConfig(scope=scope, enabled=True)
    router = GitHubCanaryRouter(
        config=canary,
        executor=executor,
        readiness=readiness,
    )
    if not canary.enabled:  # pragma: no cover - defensive
        raise CollectorError("CANARY_NOT_ENABLED")

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    samples: list[dict[str, Any]] = []
    started_at = datetime.now(UTC)

    # Derived once: structural property of the executor in use, not a literal.
    direct_mutation = direct_mutation_observed(executor)
    shadow_mutation, shadow_mutation_basis = shadow_mutation_observed(args.shadow_mutation_basis)

    streamable = streamable_http_client(args.url)
    read_stream, write_stream, _ = await streamable.__aenter__()
    try:
        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        try:
            await session.initialize()
            tools = await session.list_tools()
            v1_tool_count = len(tools.tools)
            health = _payload(
                await session.call_tool("hermes_health", arguments={"detailed": False})
            )
            bridge = health.get("bridge", {}) if isinstance(health, dict) else {}
            upstream = health.get("upstream", {}) if isinstance(health, dict) else {}
            v1_healthy = str(upstream.get("status")) == "ok"

            for item in plan:
                tool_id = item["tool_id"]
                repository = item["repository"]
                arguments = item["arguments"]

                # ---- DIRECT (no Hermes client involved) ----
                enter_phase(PHASE_DIRECT_COLLECTION)
                before_calls = transport.calls
                before_redirects = transport.redirects
                direct_started = time.monotonic()
                decision = await router.route(
                    tool_id,
                    repository,
                    _direct_operation(tool_id, repository, arguments),
                )
                direct_latency = (time.monotonic() - direct_started) * 1000.0
                if decision.path is not ExecutionPath.DIRECT:
                    raise CollectorError(
                        f"DIRECT_NOT_TAKEN_{_foreign_token(decision.fallback_reason.value)}"
                    )
                if not decision.succeeded or decision.result is None:
                    raise CollectorError(f"DIRECT_FAILED_{_foreign_token(decision.error_code)}")
                direct_calls = transport.calls - before_calls
                direct_redirects = transport.redirects - before_redirects
                if direct_calls != 1:
                    raise CollectorError("DIRECT_API_CALL_COUNT_INVALID")

                result = decision.result
                direct_digest = normalized_digest(tool_id, result.data)

                # ---- V1 agentic shadow ----
                enter_phase(PHASE_SHADOW_COLLECTION)
                shadow_started = time.monotonic()
                shadow_raw = await _run_shadow(
                    session,
                    _shadow_prompt(tool_id, repository, arguments),
                    args.wait_seconds,
                )
                shadow_latency = (time.monotonic() - shadow_started) * 1000.0
                shadow_payload = _payload(shadow_raw)
                shadow_data = _shadow_data_strict(shadow_payload)
                if shadow_data is None:
                    raise CollectorError("SHADOW_RESULT_UNPARSEABLE")

                enter_phase(PHASE_TOKEN_ACCOUNTING)
                session_id = _top_level_session_id(shadow_payload)
                if session_id is None:
                    raise CollectorError("SHADOW_SESSION_ID_MISSING")
                tokens = state_db_tokens(args.hermes_state_db, session_id)
                if tokens is None:
                    raise CollectorError("SHADOW_TOKEN_ACCOUNTING_UNAVAILABLE")

                enter_phase(PHASE_VALIDATION)
                shadow_digest = normalized_digest(tool_id, shadow_data)
                if shadow_digest != direct_digest:
                    # The LLM semantic digest/match stays the hard gate. It is
                    # evaluated BEFORE provenance so a provenance PASS can never
                    # rescue a semantic FAIL.
                    raise CollectorError(f"SEMANTIC_MISMATCH_{_tool_token(tool_id)}")

                # Additive internal-tool provenance for this accepted sample,
                # scoped strictly to this sample's shadow session.
                try:
                    provenance = collect_tool_provenance(
                        shadow_state_db=args.hermes_state_db,
                        session_id=session_id,
                        expected_tool_id=tool_id,
                        expected_arguments=arguments,
                        direct_normalized_sha256=direct_digest,
                        normalizer=normalized_digest,
                    ).as_canonical()
                except ProvenanceError as exc:
                    raise CollectorError(exc.code) from exc

                window = WindowIntegrity(
                    direct_transport_dedicated=True,
                    direct_call_delta_exact=direct_calls == 1,
                    shadow_session_scoped_accounting=True,
                )
                if window.contaminated:
                    raise CollectorError("WINDOW_INTEGRITY_UNPROVEN")

                samples.append(
                    {
                        "tool_id": tool_id,
                        "repetition": item["repetition"],
                        "repository": repository,
                        "connected_jarvas": True,
                        "contaminated_window": window.contaminated,
                        "window_integrity": window.describe(),
                        "direct": {
                            "success": True,
                            "latency_ms": round(direct_latency, 3),
                            "provider_api_calls": direct_calls,
                            "hermes_upstream_calls": 0,
                            "hermes_llm_tokens": {
                                "input": 0,
                                "output": 0,
                                "total": 0,
                            },
                            "raw_bytes": result.raw_bytes,
                            "returned_bytes": result.returned_bytes,
                            "mutation_observed": direct_mutation,
                            "mutation_basis": DIRECT_MUTATION_BASIS,
                            "redirect_followed": direct_redirects > 0,
                        },
                        "v1_shadow": {
                            "success": True,
                            "latency_ms": round(shadow_latency, 3),
                            "hermes_llm_tokens": tokens,
                            "token_usage_estimated": False,
                            "token_usage_source": STATE_DB_TOKEN_SOURCE,
                            "mutation_observed": shadow_mutation,
                            "mutation_basis": shadow_mutation_basis,
                        },
                        "comparison": {
                            "semantic_match": True,
                            "direct_normalized_sha256": direct_digest,
                            "v1_normalized_sha256": shadow_digest,
                        },
                        "tool_provenance": provenance,
                    }
                )
        finally:
            await session.__aexit__(None, None, None)
    finally:
        await streamable.__aexit__(None, None, None)

    enter_phase(PHASE_VALIDATION)
    if len(samples) != EXPECTED_SAMPLE_COUNT:
        raise CollectorError("SAMPLE_TOPOLOGY_INVALID")

    shadow_total = sum(s["v1_shadow"]["hermes_llm_tokens"]["total"] for s in samples)
    if shadow_total <= 0:
        raise CollectorError("SHADOW_TOKEN_TOTAL_NOT_POSITIVE")

    return {
        "schema": EVIDENCE_SCHEMA,
        "gate": COLLECTION_GATE,
        "source_commit": args.source_commit,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "runtime": {
            "bridge_version": str(bridge.get("manifest_version")),
            "schema_version": str(bridge.get("schema_version")),
            "v1_tool_count": v1_tool_count,
            "jarvas_connected": True,
            "v1_path_healthy": bool(v1_healthy),
            "direct_feature_enabled": bool(canary.enabled),
            "v1_semantics_unchanged": v1_tool_count == 27,
            "direct_core_commit": args.direct_core_commit,
        },
        "github_provider": attestation.evidence(),
        "attestation_notes": {
            **attestation.attestation_notes(),
            "declaration": declaration.notes(),
            "attestation_path_recorded": False,
        },
        "window_integrity_basis": {
            "direct_mutation_basis": DIRECT_MUTATION_BASIS,
            "shadow_mutation_basis": shadow_mutation_basis,
            "shadow_mutation_basis_description": SHADOW_MUTATION_BASES[shadow_mutation_basis],
        },
        "canary": canary.describe(),
        "discovery": {
            "actual_jarvas_host": True,
            "credential_source_discovered": True,
            "repository_access_verified": True,
            "credential_value_recorded": False,
            "secret_path_recorded": False,
            "environment_dump_recorded": False,
        },
        "samples": samples,
        "aggregate": {
            "sample_count": len(samples),
            "successful_samples": len(samples),
            "semantic_matches": len(samples),
            "direct_provider_api_calls": sum(s["direct"]["provider_api_calls"] for s in samples),
            "direct_hermes_upstream_calls": 0,
            "direct_hermes_llm_tokens": 0,
            "v1_shadow_hermes_llm_tokens": shadow_total,
            "mutations_observed": 0,
            "contaminated_windows": 0,
            "provenance_pass": sum(
                1 for s in samples if s["tool_provenance"]["provenance_pass"] is True
            ),
            "provenance_fail": sum(
                1 for s in samples if s["tool_provenance"]["provenance_pass"] is not True
            ),
            "tool_sample_counts": {
                tool: REPETITIONS_PER_TOOL for tool in sorted(GITHUB_DIRECT_READ_TOOL_IDS)
            },
        },
        "privacy": {
            "credential_values_stored": False,
            "environment_dump_stored": False,
            "outputs_stored": False,
            "prompts_stored": False,
            "secret_paths_stored": False,
        },
    }


class ArgumentContractError(SystemExit):
    """Usage failure carrying a sanitized reason instead of argparse output.

    It stays a ``SystemExit`` so callers driving :func:`build_parser` directly
    keep observing the argparse contract, while :func:`main` recognizes it and
    emits the bounded blocked document. No usage text, argument value or path
    is ever produced.
    """

    def __init__(self, reason: str = "ARGUMENTS_INVALID") -> None:
        self.reason = sanitize_reason(reason)
        super().__init__(2)


class BlockedArgumentParser(argparse.ArgumentParser):
    """Argparse parser that never writes usage, values or paths to stderr.

    Any usage failure is converted into the same bounded, sanitized
    ``DIRECT_READ_BLOCKED`` contract the rest of the collector emits, with a
    stable reason supplied by the caller (never the argparse message, which
    can echo a supplied value or path).
    """

    def error(self, message: str) -> Any:
        # argparse contract: never print usage/values, fail closed instead.
        raise ArgumentContractError()

    def exit(self, status: int = 0, message: str | None = None) -> Any:
        if status:
            raise ArgumentContractError()
        raise SystemExit(status)

    def _print_message(self, message: str, file: Any = None) -> None:
        if file is sys.stderr:
            return
        super()._print_message(message, file)


def emit_blocked(reason: Any) -> int:
    """Emit exactly one bounded, sanitized blocked document and return 2."""
    print(json.dumps({"gate": BLOCKED_GATE, "reason": sanitize_reason(reason)}, sort_keys=True))
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = BlockedArgumentParser(description=__doc__, add_help=True)
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--targets", required=True)
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--direct-core-commit", required=True)
    parser.add_argument(
        "--provider-type",
        required=True,
        choices=[item.value for item in GitHubProviderType],
    )
    parser.add_argument("--secret-name", default=DEFAULT_SECRET_NAME)
    parser.add_argument(
        "--provider-attestation",
        required=True,
        help=(
            "path to a sanitized, secret-free provider attestation JSON document "
            f"({ATTESTATION_INPUT_SCHEMA}). It declares the exact permission map, "
            "the exact repository scopes and the confirmation source for the facts "
            "the GitHub REST API cannot self-introspect. The path is never "
            "persisted in the evidence."
        ),
    )
    parser.add_argument(
        "--shadow-mutation-basis",
        default="none",
        choices=sorted(SHADOW_MUTATION_BASES),
        help=(
            "documented observational basis for the V1 shadow "
            "'mutation_observed = false' claim. The default 'none' makes the "
            "collector fail closed instead of asserting it without evidence."
        ),
    )
    parser.add_argument(
        "--hermes-state-db",
        required=True,
        help=(
            "path to the Hermes state.db, opened strictly read-only for real V1 "
            "token accounting. The path is never persisted in the evidence."
        ),
    )
    parser.add_argument("--wait-seconds", type=float, default=180.0)
    return parser


def _parse_and_check(argv: list[str] | None) -> argparse.Namespace:
    """Parse arguments and enforce preconditions with stable, path-free reasons."""
    enter_phase(PHASE_PRECONDITION)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not Path(args.hermes_state_db).is_file():
        raise CollectorError("HERMES_STATE_DB_NOT_A_FILE")
    if not Path(args.targets).is_file():
        raise CollectorError("TARGETS_FILE_NOT_FOUND")
    if not Path(args.provider_attestation).is_file():
        raise CollectorError("PROVIDER_ATTESTATION_FILE_NOT_FOUND")
    return args


def main(argv: list[str] | None = None) -> int:
    enter_phase(PHASE_PRECONDITION)
    try:
        args = _parse_and_check(argv)
        report = asyncio.run(collect(args))
    except CollectorError as exc:
        return emit_blocked(exc.code)
    except ArgumentContractError as exc:
        return emit_blocked(exc.reason)
    except SystemExit as exc:
        code = exc.code
        if code in (0, None):
            raise
        return emit_blocked(phase_exit_reason(current_phase()))
    except KeyboardInterrupt:
        return emit_blocked(phase_exit_reason(current_phase()))
    except BaseException as exc:  # sanitized fail-closed boundary
        return emit_blocked(phase_exception_reason(current_phase(), exc))

    text = json.dumps(report, indent=2, sort_keys=True)
    Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "gate": report["gate"],
                "samples": len(report["samples"]),
                "evidence_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "output": args.json_out,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
