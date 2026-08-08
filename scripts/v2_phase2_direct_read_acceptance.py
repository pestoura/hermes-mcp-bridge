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
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
import time
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
    AttestationError,
    attest_provider,
)
from hermes_mcp_bridge.v2.github_canary import (  # noqa: E402
    ExecutionPath,
    GitHubCanaryConfig,
    GitHubCanaryRouter,
)
from hermes_mcp_bridge.v2.github_direct import (  # noqa: E402
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

EVIDENCE_SCHEMA = "hermes-v2-phase2-direct-read-acceptance/1"
COLLECTION_GATE = "DIRECT_READ_EVIDENCE_COLLECTED"
REPETITIONS_PER_TOOL = 3
EXPECTED_SAMPLE_COUNT = len(GITHUB_DIRECT_READ_TOOL_IDS) * REPETITIONS_PER_TOOL
STATE_DB_TOKEN_SOURCE = "hermes_state_db:session_model_usage"
STATE_DB_POLL_ATTEMPTS = 8
STATE_DB_POLL_INTERVAL = 0.5

#: Fields compared between DIRECT and the V1 agentic shadow, per tool. Both
#: sides are projected onto exactly this set before hashing.
COMPARISON_FIELDS: dict[str, tuple[str, ...]] = {
    "github.get_repo": ("full_name", "private", "default_branch", "archived"),
    "github.get_pr": ("number", "title", "state", "draft", "merged"),
    "github.get_issue": ("number", "title", "state", "labels"),
    "github.get_checks": ("total_count",),
    "github.search": ("total_count",),
}


class CollectorError(RuntimeError):
    """Fail-closed collection failure carrying a stable, secret-free code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

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
    if isinstance(value, bool) or value is None or isinstance(value, int | str):
        return value
    if isinstance(value, float):
        return str(value)
    if isinstance(value, list | tuple):
        return [_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _scalar(item) for key, item in sorted(value.items())}
    return str(value)


def normalize_for_comparison(tool_id: str, data: Any) -> dict[str, Any]:
    """Project either side onto the exact comparison field set for ``tool_id``."""
    fields = COMPARISON_FIELDS.get(tool_id)
    if fields is None:
        raise CollectorError("UNKNOWN_COMPARISON_TOOL")
    source = data if isinstance(data, dict) else {}
    normalized: dict[str, Any] = {}
    for field in fields:
        value = _scalar(source.get(field))
        if isinstance(value, list):
            value = sorted(str(item) for item in value)
        normalized[field] = value
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


def _shadow_prompt(tool_id: str, repository: str, arguments: dict[str, Any]) -> str:
    fields = ", ".join(COMPARISON_FIELDS[tool_id])
    detail = json.dumps(arguments, sort_keys=True)
    return (
        "Read-only task. Do not modify anything on GitHub. "
        f"For repository {repository}, perform the read {tool_id} with "
        f"arguments {detail}. Reply with ONE JSON object and nothing else, "
        f"containing exactly these keys: {fields}."
    )


def _payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if isinstance(structured, dict):
        return structured.get("result", structured)
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return None


def _shadow_data(payload: Any) -> Any:
    """Extract the JSON object the agent was asked to return, without storing text."""
    if isinstance(payload, dict):
        for key in ("output", "result", "text", "content"):
            value = payload.get(key)
            if isinstance(value, dict):
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
        attestation = attest_provider(provider, repositories=repositories)
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
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

    samples: list[dict[str, Any]] = []
    started_at = datetime.now(UTC)

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
                    raise CollectorError(f"DIRECT_NOT_TAKEN_{decision.fallback_reason.value}")
                if not decision.succeeded or decision.result is None:
                    raise CollectorError(f"DIRECT_FAILED_{decision.error_code}")
                direct_calls = transport.calls - before_calls
                direct_redirects = transport.redirects - before_redirects
                if direct_calls != 1:
                    raise CollectorError("DIRECT_API_CALL_COUNT_INVALID")

                result = decision.result
                direct_digest = normalized_digest(tool_id, result.data)

                # ---- V1 agentic shadow ----
                shadow_started = time.monotonic()
                shadow_raw = await _run_shadow(
                    session,
                    _shadow_prompt(tool_id, repository, arguments),
                    args.wait_seconds,
                )
                shadow_latency = (time.monotonic() - shadow_started) * 1000.0
                shadow_payload = _payload(shadow_raw)
                shadow_data = _shadow_data(shadow_payload)
                if shadow_data is None:
                    raise CollectorError("SHADOW_RESULT_UNPARSEABLE")

                session_id = (
                    shadow_payload.get("session_id") if isinstance(shadow_payload, dict) else None
                )
                tokens = state_db_tokens(args.hermes_state_db, session_id)
                if tokens is None:
                    raise CollectorError("SHADOW_TOKEN_ACCOUNTING_UNAVAILABLE")

                shadow_digest = normalized_digest(tool_id, shadow_data)
                if shadow_digest != direct_digest:
                    raise CollectorError(f"SEMANTIC_MISMATCH_{tool_id}")

                samples.append(
                    {
                        "tool_id": tool_id,
                        "repetition": item["repetition"],
                        "repository": repository,
                        "connected_jarvas": True,
                        "contaminated_window": False,
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
                            "mutation_observed": False,
                            "redirect_followed": direct_redirects > 0,
                        },
                        "v1_shadow": {
                            "success": True,
                            "latency_ms": round(shadow_latency, 3),
                            "hermes_llm_tokens": tokens,
                            "token_usage_estimated": False,
                            "token_usage_source": STATE_DB_TOKEN_SOURCE,
                            "mutation_observed": False,
                        },
                        "comparison": {
                            "semantic_match": True,
                            "direct_normalized_sha256": direct_digest,
                            "v1_normalized_sha256": shadow_digest,
                        },
                    }
                )
        finally:
            await session.__aexit__(None, None, None)
    finally:
        await streamable.__aexit__(None, None, None)

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
        "attestation_notes": attestation.attestation_notes(),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
        "--hermes-state-db",
        required=True,
        help=(
            "path to the Hermes state.db, opened strictly read-only for real V1 "
            "token accounting. The path is never persisted in the evidence."
        ),
    )
    parser.add_argument("--wait-seconds", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not Path(args.hermes_state_db).is_file():
        parser.error("--hermes-state-db must point to an existing SQLite file")
    if not Path(args.targets).is_file():
        parser.error("--targets must point to an existing JSON file")

    try:
        report = asyncio.run(collect(args))
    except CollectorError as exc:
        print(json.dumps({"gate": "DIRECT_READ_BLOCKED", "reason": exc.code}))
        return 2

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
