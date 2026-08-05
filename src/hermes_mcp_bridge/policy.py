"""Policy engine: explicit loader + deterministic allow/deny/require-approval.

Single source of truth for:

* which actions are read-only vs mutating (:data:`BUILTIN_SAFE_POLICY`);
* how a policy document is loaded (inline JSON > file > built-in safe policy);
* how a decision is derived (fail-closed, unknown actions DENY).

The loader never logs or returns secret material. Only the source *type*, the
policy name and a stable content hash are exposed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    DecisionType,
    MutationClass,
    PolicyEvaluationInput,
    PolicyEvaluationResult,
    TrustLabel,
)
from .secretfiles import read_secret

POLICY_INLINE_ENV = "BRIDGE_POLICY_JSON"
POLICY_PATH_ENV = "BRIDGE_POLICY_PATH"
SECURITY_MODE_ENV = "BRIDGE_SECURITY_MODE"

#: Security modes where a missing/empty policy or missing HMAC key is fatal.
STRICT_MODES = frozenset({"production", "security_required"})
RELAXED_MODES = frozenset({"development", "dev", "test"})

_DECISIONS = {"ALLOW", "DENY", "REQUIRE_APPROVAL"}

#: Built-in safe policy. Explicit allow-list for genuinely read-only tools,
#: explicit mutating list for everything that changes state or executes work,
#: everything else DENY.
BUILTIN_SAFE_POLICY: dict[str, Any] = {
    "name": "builtin-safe",
    "version": "0.9.0",
    "read_only_actions": [
        "read",
        "status",
        "health",
        "list",
        "manifest",
        "readiness",
        "capabilities",
        "agent-card",
        "hermes_health",
        "hermes_readiness",
        "hermes_status",
        "hermes_wait",
        "hermes_recent_runs",
        "hermes_capabilities",
        "hermes_agent_card",
        "hermes_policy_evaluate",
        "hermes_approval_status",
        "hermes_result_manifest",
        "hermes_plan",
        "hermes_checkpoint_status",
        "hermes_saga_status",
        "hermes_lock_status",
        "hermes_quota_status",
        "hermes_prompt",
        "hermes_submit",
    ],
    "mutating_actions": [
        "hermes_stop",
        "hermes_approval_create",
        "hermes_approval_respond",
        "hermes_execute_approved_plan",
        "hermes_checkpoint_create",
        "hermes_continue",
        "hermes_saga_start",
        "hermes_saga_compensate",
        "hermes_lock_acquire",
        "hermes_lock_release",
    ],
    "deny_actions": [],
    "require_approval_actions": [],
    "unknown_action_decision": "DENY",
}


class PolicyError(Exception):
    """Invalid policy configuration (always fail-closed)."""


@dataclass(frozen=True)
class LoadedPolicy:
    """Result of a policy load attempt. Never carries secrets."""

    policy: dict[str, Any] | None
    source: str  # inline | file | builtin | none
    valid: bool
    policy_hash: str | None = None
    name: str | None = None
    error: str | None = None
    security_mode: str = "production"
    notes: list[str] = field(default_factory=list)

    def require(self) -> dict[str, Any]:
        if not self.valid or self.policy is None:
            raise PolicyError(self.error or "policy is not loaded")
        return self.policy

    def summary(self) -> dict[str, Any]:
        return {
            "loaded": self.valid,
            "valid": self.valid,
            "source": self.source,
            "name": self.name,
            "policy_hash": self.policy_hash,
            "security_mode": self.security_mode,
            "error": self.error,
        }


def security_mode(env: Mapping[str, str] | None = None) -> str:
    environ = env if env is not None else os.environ
    mode = str(environ.get(SECURITY_MODE_ENV, "production")).strip().lower()
    return mode or "production"


def is_strict_mode(env: Mapping[str, str] | None = None) -> bool:
    return security_mode(env) not in RELAXED_MODES


def _policy_hash(policy: dict[str, Any]) -> str:
    normalized = json.dumps(policy, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PolicyError(f"policy.{field_name} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PolicyError(f"policy.{field_name} must contain non-empty strings")
        out.append(item.strip().lower())
    return out


def validate_policy_document(document: Any) -> dict[str, Any]:
    """Validate and normalize a policy document. Raises :class:`PolicyError`."""

    if not isinstance(document, dict):
        raise PolicyError("policy document must be a JSON object")

    normalized: dict[str, Any] = {
        "name": str(document.get("name") or "unnamed"),
        "version": str(document.get("version") or "0"),
        "read_only_actions": _string_list(
            document.get("read_only_actions"), "read_only_actions"
        ),
        "mutating_actions": _string_list(document.get("mutating_actions"), "mutating_actions"),
        "deny_actions": _string_list(document.get("deny_actions"), "deny_actions"),
        "require_approval_actions": _string_list(
            document.get("require_approval_actions"), "require_approval_actions"
        ),
    }
    unknown = str(document.get("unknown_action_decision") or "DENY").strip().upper()
    if unknown not in _DECISIONS:
        raise PolicyError("policy.unknown_action_decision must be ALLOW/DENY/REQUIRE_APPROVAL")
    normalized["unknown_action_decision"] = unknown

    overlap = set(normalized["read_only_actions"]) & set(normalized["mutating_actions"])
    if overlap:
        raise PolicyError("policy declares the same action as read-only and mutating")

    if (
        not normalized["read_only_actions"]
        and not normalized["mutating_actions"]
        and unknown != "DENY"
    ):
        raise PolicyError("empty policy is only allowed with unknown_action_decision=DENY")
    return normalized


def load_policy(env: Mapping[str, str] | None = None) -> LoadedPolicy:
    """Load the active policy: inline JSON > file > built-in safe policy."""

    environ = env if env is not None else os.environ
    mode = security_mode(environ)
    inline = environ.get(POLICY_INLINE_ENV)
    path = environ.get(POLICY_PATH_ENV)

    if inline is not None and inline.strip():
        try:
            document = json.loads(inline)
            normalized = validate_policy_document(document)
        except PolicyError as exc:
            return LoadedPolicy(None, "inline", False, error=str(exc), security_mode=mode)
        except Exception:
            return LoadedPolicy(
                None, "inline", False, error="invalid JSON in policy inline env", security_mode=mode
            )
        return LoadedPolicy(
            normalized,
            "inline",
            True,
            policy_hash=_policy_hash(normalized),
            name=normalized["name"],
            security_mode=mode,
        )

    if path is not None and path.strip():
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
            normalized = validate_policy_document(document)
        except FileNotFoundError:
            return LoadedPolicy(
                None, "file", False, error="configured policy file is missing", security_mode=mode
            )
        except PolicyError as exc:
            return LoadedPolicy(None, "file", False, error=str(exc), security_mode=mode)
        except Exception:
            return LoadedPolicy(
                None, "file", False, error="policy file could not be parsed", security_mode=mode
            )
        return LoadedPolicy(
            normalized,
            "file",
            True,
            policy_hash=_policy_hash(normalized),
            name=normalized["name"],
            security_mode=mode,
        )

    normalized = validate_policy_document(BUILTIN_SAFE_POLICY)
    return LoadedPolicy(
        normalized,
        "builtin",
        True,
        policy_hash=_policy_hash(normalized),
        name=normalized["name"],
        security_mode=mode,
        notes=["using built-in safe policy"],
    )


_cache_lock = threading.Lock()
_cached: LoadedPolicy | None = None


def get_active_policy(*, refresh: bool = False) -> LoadedPolicy:
    global _cached
    if refresh:
        with _cache_lock:
            _cached = load_policy()
            return _cached
    if _cached is None:
        with _cache_lock:
            if _cached is None:
                _cached = load_policy()
    return _cached


def reset_policy_cache() -> None:
    global _cached
    with _cache_lock:
        _cached = None


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def classify_action(action: str, policy: dict[str, Any] | None = None) -> MutationClass | None:
    """Return the mutation class for ``action``.

    ``MutationClass.NONE`` for declared read-only actions, ``WRITE`` for
    declared mutating actions, and ``None`` when the action is unknown to the
    policy (caller must apply ``unknown_action_decision``).
    """

    active = policy if policy is not None else get_active_policy().require()
    normalized = str(action or "").strip().lower()
    if not normalized:
        return None
    if normalized in set(active.get("read_only_actions", [])):
        return MutationClass.NONE
    if normalized in set(active.get("mutating_actions", [])):
        return MutationClass.WRITE
    return None


def _trust_risk(trust_label: TrustLabel) -> str:
    if trust_label in {TrustLabel.TRUSTED_POLICY, TrustLabel.USER_INSTRUCTION}:
        return "low"
    if trust_label == TrustLabel.AGENT_PROPOSAL:
        return "medium"
    if trust_label == TrustLabel.TOOL_RESULT:
        return "low"
    if trust_label == TrustLabel.UNTRUSTED_CONTENT:
        return "high"
    return "unknown"


def evaluate_policy(
    evaluation: PolicyEvaluationInput,
    *,
    policy: dict[str, Any] | None = None,
) -> PolicyEvaluationResult:
    """Evaluate a decision. Raises :class:`PolicyError` when fail-closed."""

    if policy is None:
        loaded = get_active_policy()
        active = loaded.require()
        source = loaded.source
        policy_hash = loaded.policy_hash
    else:
        active = validate_policy_document(policy)
        source = "explicit"
        policy_hash = _policy_hash(active)

    effective_policy: dict[str, Any] = {
        "source": source,
        "policy_name": active.get("name"),
        "policy_hash": policy_hash,
    }

    action = str(evaluation.action or "").strip()
    if not action:
        return PolicyEvaluationResult(
            decision=DecisionType.DENY,
            reason="missing action",
            effective_policy=effective_policy,
        )
    action_norm = action.lower()

    declared = classify_action(action, active)
    caller_class = evaluation.mutation_class
    # The caller may escalate (declare a mutation on a read-only tool call) but
    # never de-escalate a declared mutating action.
    if declared is None or (
        declared == MutationClass.NONE
        and caller_class in {MutationClass.WRITE, MutationClass.DELETE, MutationClass.ADMIN}
    ):
        mutation_class = caller_class
    else:
        mutation_class = declared

    risk = _trust_risk(evaluation.trust_label)
    effective_policy["trust_risk"] = risk
    effective_policy["action"] = action
    effective_policy["declared_class"] = declared.value if declared else "unknown"
    effective_policy["mutation_class"] = (
        mutation_class.value if mutation_class is not None else "unknown"
    )

    if action_norm in set(active.get("deny_actions", [])):
        return PolicyEvaluationResult(
            decision=DecisionType.DENY,
            reason="deny_actions",
            effective_policy=effective_policy,
        )

    if action_norm in set(active.get("require_approval_actions", [])):
        return PolicyEvaluationResult(
            decision=DecisionType.REQUIRE_APPROVAL,
            reason="require_approval_actions",
            effective_policy=effective_policy,
            approval_required=True,
        )

    if declared is None:
        unknown = str(active.get("unknown_action_decision", "DENY")).upper()
        decision = DecisionType(unknown)
        return PolicyEvaluationResult(
            decision=decision,
            reason="unknown action for active policy",
            effective_policy=effective_policy,
            approval_required=decision == DecisionType.REQUIRE_APPROVAL,
        )

    if mutation_class in {MutationClass.WRITE, MutationClass.DELETE, MutationClass.ADMIN}:
        return PolicyEvaluationResult(
            decision=DecisionType.REQUIRE_APPROVAL,
            reason=f"mutation class {mutation_class.value} requires approval",
            effective_policy=effective_policy,
            approval_required=True,
        )

    if risk == "high":
        # Untrusted content is an envelope-level escalation: even a read-only
        # tool invocation driven by untrusted input needs an approval, because
        # the *content* is what steers the downstream agent.
        return PolicyEvaluationResult(
            decision=DecisionType.REQUIRE_APPROVAL,
            reason="high-risk trust label",
            effective_policy=effective_policy,
            approval_required=True,
        )

    return PolicyEvaluationResult(
        decision=DecisionType.ALLOW,
        reason="read-only action allowed by policy",
        effective_policy=effective_policy,
    )


def deterministic_policy_signature(
    evaluation: PolicyEvaluationInput,
    result: PolicyEvaluationResult,
) -> str:
    payload = {
        "action": evaluation.action,
        "origin_type": evaluation.origin_type,
        "project_key": evaluation.project_key,
        "resource": evaluation.resource,
        "trust_label": evaluation.trust_label.value,
        "mutation_class": evaluation.mutation_class.value,
        "principal": evaluation.principal,
        "delegation_chain": evaluation.delegation_chain,
        "decision": result.decision.value,
        "reason": result.reason,
    }
    normalized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    secret = read_secret("HERMES_BRIDGE_POLICY_SECRET") or read_secret(
        "HERMES_BRIDGE_HMAC_SECRET"
    )
    if not secret:
        return f"unsigned:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"
    return hmac.new(
        secret.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
