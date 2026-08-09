"""BLOCO 6B coverage: policy loader, enforcement, approvals, HMAC, readiness."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from hermes_mcp_bridge import policy as policy_mod
from hermes_mcp_bridge import signing
from hermes_mcp_bridge.approvals import (
    ApprovalConsumedError,
    ApprovalExpiredError,
    ApprovalRecord,
    ApprovalRegistry,
    ApprovalStaleError,
    ApprovalStatus,
    _utcnow,
)
from hermes_mcp_bridge.contracts import CURRENT_CONTRACT_VERSION, expected_tool_count
from hermes_mcp_bridge.models import Plan, PlanStatus
from hermes_mcp_bridge.plans import (
    ApprovalAdapterError,
    plan_approval_from_record,
    validate_approval,
)
from hermes_mcp_bridge.policy import (
    BUILTIN_SAFE_POLICY,
    DecisionType,
    PolicyError,
    classify_action,
    evaluate_policy,
    load_policy,
    validate_policy_document,
)
from hermes_mcp_bridge.protocol import (
    PolicyEvaluationInput,
    ResultManifest,
    TrustLabel,
)
from hermes_mcp_bridge.provenance import build_result_manifest, verify_result_manifest
from hermes_mcp_bridge.secretfiles import describe_secret, read_secret

REPO_ROOT = Path(__file__).resolve().parents[1]
STRONG_KEY = "k" * 48
OTHER_KEY = "j" * 48


@pytest.fixture(autouse=True)
def _clean_policy_cache():
    policy_mod.reset_policy_cache()
    yield
    policy_mod.reset_policy_cache()


def _registry(db_path: str) -> ApprovalRegistry:
    registry = ApprovalRegistry(db_path)
    registry.initialize()
    return registry


# ---------------------------------------------------------------- loader ----


def test_loader_precedence_inline_beats_file(tmp_path: Path) -> None:
    file_policy = {"name": "from-file", "read_only_actions": ["a"], "mutating_actions": []}
    path = tmp_path / "p.json"
    path.write_text(json.dumps(file_policy), encoding="utf-8")
    env = {
        "BRIDGE_POLICY_JSON": json.dumps(
            {"name": "from-inline", "read_only_actions": ["b"], "mutating_actions": []}
        ),
        "BRIDGE_POLICY_PATH": str(path),
    }
    loaded = load_policy(env)
    assert loaded.valid and loaded.source == "inline" and loaded.name == "from-inline"


def test_loader_precedence_file_beats_builtin(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps({"name": "from-file", "read_only_actions": ["a"], "mutating_actions": []}),
        encoding="utf-8",
    )
    loaded = load_policy({"BRIDGE_POLICY_PATH": str(path)})
    assert loaded.valid and loaded.source == "file" and loaded.name == "from-file"


def test_loader_falls_back_to_builtin_safe_policy() -> None:
    loaded = load_policy({})
    assert loaded.valid
    assert loaded.source == "builtin"
    assert loaded.name == "builtin-safe"
    assert loaded.policy_hash


def test_loader_invalid_inline_json_is_fail_closed() -> None:
    loaded = load_policy({"BRIDGE_POLICY_JSON": "{not json"})
    assert not loaded.valid
    assert loaded.policy is None
    with pytest.raises(PolicyError):
        loaded.require()


def test_loader_invalid_schema_is_fail_closed() -> None:
    loaded = load_policy({"BRIDGE_POLICY_JSON": json.dumps({"read_only_actions": "nope"})})
    assert not loaded.valid


def test_loader_missing_configured_file_is_fail_closed(tmp_path: Path) -> None:
    loaded = load_policy({"BRIDGE_POLICY_PATH": str(tmp_path / "absent.json")})
    assert not loaded.valid
    assert loaded.source == "file"
    assert "missing" in (loaded.error or "")


def test_permissive_empty_policy_is_rejected() -> None:
    with pytest.raises(PolicyError):
        validate_policy_document(
            {
                "read_only_actions": [],
                "mutating_actions": [],
                "unknown_action_decision": "ALLOW",
            }
        )


def test_policy_cannot_declare_action_both_ways() -> None:
    with pytest.raises(PolicyError):
        validate_policy_document({"read_only_actions": ["x"], "mutating_actions": ["x"]})


def test_versioned_production_policy_is_valid_and_secret_free() -> None:
    path = REPO_ROOT / "config" / "policies" / "production.json"
    raw = path.read_text(encoding="utf-8")
    document = json.loads(raw)
    normalized = validate_policy_document(document)
    assert normalized["unknown_action_decision"] == "DENY"
    lowered = raw.lower()
    for marker in ("secret", "token", "password", "key=", "hmac"):
        assert marker not in lowered
    loaded = load_policy({"BRIDGE_POLICY_PATH": str(path)})
    assert loaded.valid and loaded.source == "file"


# ------------------------------------------------------- classification ----


def test_single_source_of_truth_covers_the_whole_tool_contract() -> None:
    declared = set(BUILTIN_SAFE_POLICY["read_only_actions"]) | set(
        BUILTIN_SAFE_POLICY["mutating_actions"]
    )
    from hermes_mcp_bridge.contracts import required_tools

    tools = required_tools(CURRENT_CONTRACT_VERSION)
    assert expected_tool_count(CURRENT_CONTRACT_VERSION) == 27
    assert not (tools - declared), "every contract tool must be classified"


def test_readonly_tools_are_allowed_by_the_builtin_policy() -> None:
    for action in ("hermes_health", "hermes_readiness", "hermes_capabilities"):
        result = evaluate_policy(
            PolicyEvaluationInput(action=action, trust_label=TrustLabel.USER_INSTRUCTION)
        )
        assert result.decision == DecisionType.ALLOW, action


def test_mutating_tools_require_approval() -> None:
    for action in ("hermes_lock_acquire", "hermes_saga_compensate", "hermes_stop"):
        result = evaluate_policy(
            PolicyEvaluationInput(action=action, trust_label=TrustLabel.USER_INSTRUCTION)
        )
        assert result.decision == DecisionType.REQUIRE_APPROVAL, action


def test_unknown_action_is_denied() -> None:
    result = evaluate_policy(PolicyEvaluationInput(action="hermes_do_whatever"))
    assert result.decision == DecisionType.DENY


def test_classify_action_returns_none_for_unknown() -> None:
    assert classify_action("nope", validate_policy_document(BUILTIN_SAFE_POLICY)) is None


# --------------------------------------------------------- enforcement ----


def test_enforcement_denies_on_evaluation_error(monkeypatch) -> None:
    from hermes_mcp_bridge import server

    monkeypatch.setattr(
        server,
        "evaluate_policy",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    blocked = server._enforce_policy("hermes_health")
    assert blocked is not None
    assert "denied" in str(blocked.get("error", "")).lower()


def test_enforcement_denies_on_unknown_decision(monkeypatch) -> None:
    from hermes_mcp_bridge import server

    monkeypatch.setattr(server, "_policy_decision_from_inputs", lambda *_a, **_k: ("WEIRD", "n/a"))
    blocked = server._enforce_policy("hermes_health")
    assert blocked is not None
    assert "denied" in str(blocked.get("error", "")).lower()


def test_readonly_prompt_and_submit_are_not_blocked() -> None:
    from hermes_mcp_bridge import server

    for action in ("hermes_prompt", "hermes_submit"):
        assert server._enforce_policy(action, trust_label="user_instruction") is None


def test_untrusted_envelope_requires_approval_on_prompt() -> None:
    from hermes_mcp_bridge import server

    blocked = server._enforce_policy("hermes_prompt", trust_label="untrusted_content")
    assert blocked is not None
    assert blocked["metadata"]["policy"]["approval_required"] is True


def test_unparseable_trust_label_is_treated_as_untrusted() -> None:
    from hermes_mcp_bridge import server

    decision, _ = server._policy_decision_from_inputs(
        "hermes_prompt", trust_label="totally-made-up"
    )
    assert decision == "REQUIRE_APPROVAL"


# ------------------------------------------------------------ approvals ----


def _approved(registry: ApprovalRegistry, approval_id: str, **kwargs) -> ApprovalRecord:
    record = ApprovalRecord(
        approval_id=approval_id,
        action=kwargs.pop("action", "hermes_execute_approved_plan"),
        resource=kwargs.pop("resource", "plan-1"),
        resource_fingerprint=kwargs.pop("resource_fingerprint", "fp-1"),
        principal="user-1",
        decision=ApprovalStatus.APPROVED,
        created_at=_utcnow().isoformat(),
        **kwargs,
    )
    return registry.create(record)


def test_consume_marks_expired_and_rejects(tmp_path: Path) -> None:
    registry = _registry(str(tmp_path / "a.sqlite3"))
    past = _utcnow().replace(year=_utcnow().year - 1).isoformat()
    _approved(registry, "a-exp", expires_at=past)
    with pytest.raises(ApprovalExpiredError):
        registry.consume("a-exp", "fp-1")
    assert registry.get("a-exp").decision == ApprovalStatus.EXPIRED


def test_consume_requires_fingerprint_for_mutating(tmp_path: Path) -> None:
    registry = _registry(str(tmp_path / "b.sqlite3"))
    _approved(registry, "a-nofp", resource_fingerprint=None, resource=None)
    with pytest.raises(ApprovalStaleError):
        registry.consume("a-nofp", None, require_fingerprint=True)


def test_consume_rejects_wrong_fingerprint(tmp_path: Path) -> None:
    registry = _registry(str(tmp_path / "c.sqlite3"))
    _approved(registry, "a-badfp")
    with pytest.raises(ApprovalStaleError):
        registry.consume("a-badfp", "fp-other")


def test_consume_rejects_action_mismatch(tmp_path: Path) -> None:
    registry = _registry(str(tmp_path / "d.sqlite3"))
    _approved(registry, "a-action")
    with pytest.raises(ApprovalStaleError):
        registry.consume("a-action", "fp-1", expected_action="hermes_lock_acquire")


def test_consume_is_single_use(tmp_path: Path) -> None:
    registry = _registry(str(tmp_path / "e.sqlite3"))
    _approved(registry, "a-once")
    assert registry.consume("a-once", "fp-1").decision == ApprovalStatus.CONSUMED
    with pytest.raises(ApprovalConsumedError):
        registry.consume("a-once", "fp-1")


def test_concurrent_consume_only_one_winner(tmp_path: Path) -> None:
    registry = _registry(str(tmp_path / "f.sqlite3"))
    _approved(registry, "a-race")
    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            registry.consume("a-race", "fp-1")
            outcomes.append("ok")
        except (ApprovalConsumedError, sqlite3.OperationalError):
            outcomes.append("blocked")
        except Exception:
            outcomes.append("blocked")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1


# ------------------------------------------------- plan approval adapter ----


def _plan() -> Plan:
    plan = Plan(plan_id="plan-1", title="t", status=PlanStatus.APPROVED)
    plan.plan_hash = "a" * 64
    return plan


def test_plan_approval_adapter_maps_registry_record() -> None:
    record = ApprovalRecord(
        approval_id="approval-1",
        action="hermes_execute_approved_plan",
        resource="plan-1",
        resource_fingerprint="fp",
        decision=ApprovalStatus.APPROVED,
        created_at=_utcnow().isoformat(),
        metadata_sanitized={"plan_id": "plan-1", "plan_hash": "a" * 64},
    )
    adapted = plan_approval_from_record(record)
    assert adapted.plan_id == "plan-1"
    assert adapted.status == "approved"
    assert validate_approval(adapted, _plan()) == []


def test_plan_approval_adapter_errors_when_binding_missing() -> None:
    record = ApprovalRecord(
        approval_id="approval-2",
        action="hermes_execute_approved_plan",
        decision=ApprovalStatus.APPROVED,
        created_at=_utcnow().isoformat(),
        metadata_sanitized={},
    )
    with pytest.raises(ApprovalAdapterError) as excinfo:
        plan_approval_from_record(record)
    payload = excinfo.value.as_error_payload()
    assert payload["error"] == "approval_binding_invalid"
    assert payload["approval_id"] == "approval-2"


def test_plan_approval_rejects_consumed_record() -> None:
    record = ApprovalRecord(
        approval_id="approval-3",
        action="hermes_execute_approved_plan",
        decision=ApprovalStatus.CONSUMED,
        created_at=_utcnow().isoformat(),
        consumed_at=_utcnow().isoformat(),
        metadata_sanitized={"plan_id": "plan-1", "plan_hash": "a" * 64},
    )
    errors = validate_approval(plan_approval_from_record(record), _plan())
    assert any("consumed" in error for error in errors)


# ------------------------------------------------------- secrets / HMAC ----


def test_secret_file_wins_over_env_and_is_whitespace_stripped(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text(f"  {STRONG_KEY}\n", encoding="utf-8")
    env = {"HERMES_BRIDGE_HMAC_SECRET": "env-value", "HERMES_BRIDGE_HMAC_SECRET_FILE": str(path)}
    assert read_secret("HERMES_BRIDGE_HMAC_SECRET", env) == STRONG_KEY
    assert describe_secret("HERMES_BRIDGE_HMAC_SECRET", env).source_type == "file"


def test_secret_file_permissions_are_operator_controlled(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text(STRONG_KEY, encoding="utf-8")
    os.chmod(path, 0o400)
    env = {"HERMES_BRIDGE_HMAC_SECRET_FILE": str(path)}
    assert read_secret("HERMES_BRIDGE_HMAC_SECRET", env) == STRONG_KEY
    assert oct(path.stat().st_mode)[-3:] == "400"


def test_unreadable_secret_file_is_not_configured(tmp_path: Path) -> None:
    env = {"HERMES_BRIDGE_HMAC_SECRET_FILE": str(tmp_path / "absent")}
    assert read_secret("HERMES_BRIDGE_HMAC_SECRET", env) is None
    described = describe_secret("HERMES_BRIDGE_HMAC_SECRET", env)
    assert described.configured is False
    assert str(tmp_path) not in json.dumps(described.summary())


def test_secret_value_is_not_cached_between_reads(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text(STRONG_KEY, encoding="utf-8")
    env = {"HERMES_BRIDGE_HMAC_SECRET_FILE": str(path)}
    assert read_secret("HERMES_BRIDGE_HMAC_SECRET", env) == STRONG_KEY
    path.write_text(OTHER_KEY, encoding="utf-8")
    assert read_secret("HERMES_BRIDGE_HMAC_SECRET", env) == OTHER_KEY


def test_short_key_is_rejected() -> None:
    posture = signing.signing_posture({"HERMES_BRIDGE_HMAC_SECRET": "short"})
    assert not posture.ok
    assert "minimum" in (posture.error or "")


def test_missing_key_in_production_is_fail_closed() -> None:
    posture = signing.signing_posture({"BRIDGE_SECURITY_MODE": "production"})
    assert posture.required is True
    assert not posture.ok
    with pytest.raises(signing.SigningConfigError):
        signing.assert_signing_ready({"BRIDGE_SECURITY_MODE": "production"})


def test_security_required_mode_is_also_strict() -> None:
    posture = signing.signing_posture({"BRIDGE_SECURITY_MODE": "security_required"})
    assert posture.required is True and not posture.ok


def test_test_mode_allows_unsigned_but_reports_it() -> None:
    posture = signing.signing_posture({"BRIDGE_SECURITY_MODE": "test"})
    assert posture.required is False
    assert posture.ok
    assert posture.configured is False


def test_sign_verify_roundtrip_and_tamper_detection(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_SECRET", STRONG_KEY)
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_KEY_ID", "key-2026-08")
    monkeypatch.delenv("HERMES_BRIDGE_HMAC_SECRET_PREVIOUS", raising=False)
    status, digest, key_id = signing.sign("payload")
    assert status == signing.SIGNATURE_ALGORITHM and key_id == "key-2026-08"
    assert signing.verify("payload", digest)
    assert not signing.verify("payload-tampered", digest)
    assert not signing.verify("payload", digest[:-1] + ("0" if digest[-1] != "0" else "1"))


def test_previous_key_verifies_during_rotation_grace(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_SECRET", OTHER_KEY)
    _status, old_digest, _kid = signing.sign("payload")

    monkeypatch.setenv("HERMES_BRIDGE_HMAC_SECRET", STRONG_KEY)
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_SECRET_PREVIOUS", OTHER_KEY)
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID", "key-2026-05")
    assert signing.verify("payload", old_digest)

    # The previous key is verify-only: new signatures use the current key.
    _status, new_digest, _kid = signing.sign("payload")
    assert new_digest != old_digest
    posture = signing.signing_posture()
    assert posture.previous_verifier is True
    assert posture.previous_key_id == "key-2026-05"


def test_previous_key_removed_ends_grace(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_SECRET", OTHER_KEY)
    _status, old_digest, _kid = signing.sign("payload")
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_SECRET", STRONG_KEY)
    monkeypatch.delenv("HERMES_BRIDGE_HMAC_SECRET_PREVIOUS", raising=False)
    assert not signing.verify("payload", old_digest)


def test_canonical_bypass_is_closed() -> None:
    from hermes_mcp_bridge.protocol import _canonical_json_hash as protocol_hash
    from hermes_mcp_bridge.provenance import _canonical_json_hash as provenance_hash

    forged = {"__canonical__": "deadbeef", "status": "completed"}
    for hasher in (protocol_hash, provenance_hash):
        assert hasher(forged) != "deadbeef"
        assert len(hasher(forged)) == 64


def test_manifest_signature_verifies_and_detects_tamper(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_SECRET", STRONG_KEY)
    manifest = build_result_manifest(execution_id="exec-1", session_id="s-1", status="completed")
    assert manifest.signature_status == signing.SIGNATURE_ALGORITHM
    assert verify_result_manifest(manifest)

    tampered = ResultManifest(**{**manifest.model_dump(), "status": "failed"})
    assert not verify_result_manifest(tampered)


# ---------------------------------------------------------- readiness ----


def test_security_posture_reports_no_secret_values(monkeypatch, tmp_path: Path) -> None:
    from hermes_mcp_bridge import server

    secret_path = tmp_path / "hmac"
    secret_path.write_text(STRONG_KEY, encoding="utf-8")
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_SECRET_FILE", str(secret_path))
    monkeypatch.setenv("HERMES_BRIDGE_HMAC_KEY_ID", "key-a")
    monkeypatch.setenv("BRIDGE_SECURITY_MODE", "production")
    policy_mod.reset_policy_cache()

    posture = server._security_posture({"status": "up"})
    blob = json.dumps(posture)
    assert STRONG_KEY not in blob
    assert str(secret_path) not in blob
    assert posture["hmac"]["source_type"] == "file"
    assert posture["hmac"]["key_id"] == "key-a"
    assert posture["policy"]["source"] in {"builtin", "file", "inline"}
    assert "default_policy_source" not in blob
    assert posture["status"] == "ready"


def test_security_posture_not_ready_on_invalid_policy(monkeypatch) -> None:
    from hermes_mcp_bridge import server

    monkeypatch.setenv("BRIDGE_POLICY_JSON", "{broken")
    monkeypatch.setenv("BRIDGE_SECURITY_MODE", "test")
    policy_mod.reset_policy_cache()
    posture = server._security_posture({"status": "up"})
    assert posture["status"] == "not_ready"
    assert "policy" in posture["failing"]


def test_security_posture_not_ready_without_key_in_production(monkeypatch) -> None:
    from hermes_mcp_bridge import server

    monkeypatch.delenv("HERMES_BRIDGE_HMAC_SECRET", raising=False)
    monkeypatch.delenv("HERMES_BRIDGE_HMAC_SECRET_FILE", raising=False)
    monkeypatch.setenv("BRIDGE_SECURITY_MODE", "production")
    policy_mod.reset_policy_cache()
    posture = server._security_posture({"status": "up"})
    assert posture["status"] == "not_ready"
    assert "hmac" in posture["failing"]


def test_security_posture_not_ready_when_approvals_down(monkeypatch) -> None:
    from hermes_mcp_bridge import server

    monkeypatch.setenv("BRIDGE_SECURITY_MODE", "test")
    policy_mod.reset_policy_cache()
    posture = server._security_posture({"status": "down"})
    assert posture["status"] == "not_ready"
    assert "approval_registry" in posture["failing"]


@pytest.mark.asyncio
async def test_readiness_includes_security_posture() -> None:
    from hermes_mcp_bridge import server

    payload = await server.hermes_readiness()
    assert "security_posture" in payload["components"]
    assert payload["schema_version"] == "0.6.1"
    assert payload["contract_version"] == CURRENT_CONTRACT_VERSION


def test_tool_surface_stays_at_27_without_approval_consume() -> None:
    from hermes_mcp_bridge.server import server_tool_names

    names = server_tool_names()
    assert len(names) == 27
    assert "hermes_approval_consume" not in names


# ----------------------------------------------------- compose secrets ----


def test_compose_declares_file_backed_secrets_without_values() -> None:
    raw = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    assert "HERMES_API_KEY_FILE" in raw
    assert "HERMES_BRIDGE_HMAC_SECRET_FILE" in raw
    assert "HERMES_BRIDGE_HMAC_SECRET_PREVIOUS_FILE" in raw
    assert "secrets:" in raw
    # env compatibility preserved: still loads .env, no inline secret values.
    assert "env_file:" in raw
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "HMAC_SECRET" in stripped and ":" in stripped:
            value = stripped.split(":", 1)[1].strip().strip('"')
            assert value.startswith("${") or value == ""


def test_env_example_has_no_real_secret_values() -> None:
    raw = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "BRIDGE_POLICY_PATH" in raw
    assert "HERMES_BRIDGE_HMAC_SECRET_FILE" in raw
    for line in raw.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if "FILE" in name or name.endswith("_KEY_ID") or name == "BRIDGE_MIN_SECRET_LENGTH":
            continue
        if any(marker in name for marker in ("SECRET", "TOKEN", "KEY")):
            assert "replace-with" in value or value == "", line


def test_no_secret_material_leaks_into_policy_or_signing_summaries(tmp_path) -> None:
    secret = tmp_path / "s"
    secret.write_text(STRONG_KEY, encoding="utf-8")
    env = {
        "HERMES_BRIDGE_HMAC_SECRET_FILE": str(secret),
        "HERMES_BRIDGE_HMAC_KEY_ID": "kid",
    }
    blob = json.dumps(signing.signing_posture(env).summary())
    assert STRONG_KEY not in blob and str(secret) not in blob

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump({"name": "p", "read_only_actions": ["a"], "mutating_actions": []}, handle)
        policy_path = handle.name
    try:
        summary = json.dumps(load_policy({"BRIDGE_POLICY_PATH": policy_path}).summary())
        assert policy_path not in summary
    finally:
        os.unlink(policy_path)
