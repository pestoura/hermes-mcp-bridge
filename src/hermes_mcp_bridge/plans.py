"""Plan models, validation, and policy contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from .models import (
    Plan,
    PlanApproval,
    PlanApprovalPoint,
    PlanDependency,
    PlanRisk,
    PlanStatus,
    PlanStep,
    TraceContext,
)


class PlanError(ValueError):
    """Invalid plan structure or policy violation."""


_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\\-]{0,127}$")
_PLAN_HASH_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
_APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\\-]{0,127}$")


class PlanStore:
    """SQLite-backed plan persistence."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._initialized = False

    def initialize(self) -> None:
        from .migrations import apply_migrations
        apply_migrations(self._db_path)
        self._initialized = True

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _plan_hash(plan: Plan) -> str:
        payload = json.dumps(
            plan.model_dump(mode="json"),
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def create(self, plan: Plan) -> tuple[Plan, str]:
        plan_hash = self._plan_hash(plan)
        plan.plan_hash = plan_hash
        plan.created_at = plan.created_at or self._now()
        plan.updated_at = self._now()
        import sqlite3
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.isolation_level = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO plans (
                    plan_id, title, description, version, status, steps_json,
                    dependencies_json, risks_json, approval_points_json,
                    parallel_groups_json, critical_path_json, locks_json,
                    budgets_json, provenance_json, created_at, updated_at,
                    plan_hash, policy_json, trace_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.title,
                    plan.description,
                    plan.version,
                    plan.status.value,
                    json.dumps([step.model_dump(mode="json") for step in plan.steps]),
                    json.dumps([dep.model_dump(mode="json") for dep in plan.dependencies]),
                    json.dumps([risk.model_dump(mode="json") for risk in plan.risks]),
                    json.dumps([ap.model_dump(mode="json") for ap in plan.approval_points]),
                    json.dumps(plan.parallel_groups),
                    json.dumps(plan.critical_path),
                    json.dumps(plan.locks),
                    json.dumps(plan.budgets),
                    json.dumps(plan.provenance),
                    plan.created_at,
                    plan.updated_at,
                    plan.plan_hash,
                    json.dumps(plan.policy),
                    json.dumps(plan.trace.model_dump(mode="json") if plan.trace else {}),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return plan, plan_hash

    def get(self, plan_id: str) -> Plan | None:
        import sqlite3
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.isolation_level = None
        try:
            cursor = connection.execute(
                "SELECT title, description, version, status, steps_json, "
                "dependencies_json, risks_json, approval_points_json, "
                "parallel_groups_json, critical_path_json, locks_json, "
                "budgets_json, provenance_json, created_at, updated_at, "
                "plan_hash, policy_json, trace_json FROM plans WHERE plan_id = ?",
                (plan_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_plan(plan_id, row)
        finally:
            connection.close()

    def update_status(self, plan_id: str, status: PlanStatus) -> Plan | None:
        now = self._now()
        import sqlite3
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.isolation_level = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "SELECT steps_json FROM plans WHERE plan_id = ?", (plan_id,)
            )
            row = cursor.fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return None
            connection.execute(
                "UPDATE plans SET status = ?, updated_at = ? WHERE plan_id = ?",
                (status.value, now, plan_id),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return self.get(plan_id)

    def _row_to_plan(self, plan_id: str, row: tuple[Any, ...]) -> Plan:
        (
            title,
            description,
            version,
            status,
            steps_json,
            dependencies_json,
            risks_json,
            approval_points_json,
            parallel_groups_json,
            critical_path_json,
            locks_json,
            budgets_json,
            provenance_json,
            created_at,
            updated_at,
            plan_hash,
            policy_json,
            trace_json,
        ) = row
        steps = [PlanStep(**step) for step in json.loads(steps_json or "[]")]
        dependencies = [PlanDependency(**dep) for dep in json.loads(dependencies_json or "[]")]
        risks = [PlanRisk(**risk) for risk in json.loads(risks_json or "[]")]
        approval_points = [
            PlanApprovalPoint(**ap) for ap in json.loads(approval_points_json or "[]")
        ]
        trace_data = json.loads(trace_json or "{}")
        return Plan(
            plan_id=plan_id,
            title=title,
            description=description or "",
            version=version or "1",
            status=PlanStatus(status),
            steps=steps,
            dependencies=dependencies,
            risks=risks,
            approval_points=approval_points,
            parallel_groups=json.loads(parallel_groups_json or "[]"),
            critical_path=json.loads(critical_path_json or "[]"),
            locks=json.loads(locks_json or "[]"),
            budgets=json.loads(budgets_json or "{}"),
            provenance=json.loads(provenance_json or "{}"),
            created_at=created_at,
            updated_at=updated_at,
            plan_hash=plan_hash,
            policy=json.loads(policy_json or "{}"),
            trace=TraceContext(**trace_data) if trace_data else TraceContext(),
        )


def validate_plan_structure(plan: Plan) -> list[str]:
    errors: list[str] = []
    if not _PLAN_ID_RE.fullmatch(plan.plan_id):
        errors.append("plan_id is invalid")
    step_ids = {step.step_id for step in plan.steps}
    if len(step_ids) != len(plan.steps):
        errors.append("duplicate step_id detected")
    for dep in plan.dependencies:
        if dep.step_id not in step_ids:
            errors.append("dependency references missing step")
    return errors


def validate_approval(approval: PlanApproval, plan: Plan) -> list[str]:
    errors: list[str] = []
    if not _APPROVAL_ID_RE.fullmatch(approval.approval_id):
        errors.append("approval_id is invalid")
    if not _PLAN_HASH_RE.fullmatch(approval.plan_hash):
        errors.append("approval plan_hash is invalid")
    if approval.plan_hash != plan.plan_hash:
        errors.append("approval plan_hash does not match current plan hash")
    if approval.plan_id and approval.plan_id != plan.plan_id:
        errors.append("approval plan_id does not match plan")
    if approval.status not in _CONSUMABLE_APPROVAL_STATUSES:
        errors.append(f"approval status is not usable: {approval.status}")
    if approval.consumed_at:
        errors.append("approval already consumed")
    if approval.expires_at:
        try:
            expires = datetime.fromisoformat(approval.expires_at)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= datetime.now(UTC):
                errors.append("approval is expired")
        except ValueError:
            errors.append("approval expires_at is invalid")
    return errors


#: Statuses from which a plan approval may still be consumed.
_CONSUMABLE_APPROVAL_STATUSES = frozenset({"pending", "requested", "approved"})


class ApprovalAdapterError(ValueError):
    """An ApprovalRecord cannot be interpreted as a PlanApproval."""

    def __init__(self, reason: str, *, approval_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.approval_id = approval_id

    def as_error_payload(self) -> dict[str, Any]:
        return {
            "error": "approval_binding_invalid",
            "reason": self.reason,
            "approval_id": self.approval_id,
        }


def plan_approval_from_record(record: Any) -> PlanApproval:
    """Adapt an :class:`ApprovalRecord` into a :class:`PlanApproval`.

    ``ApprovalRecord`` (registry/protocol model) and ``PlanApproval`` (plan
    model) are different shapes: the registry has ``decision``/``resource``/
    ``metadata_sanitized`` while the plan layer expects ``status``/``plan_id``/
    ``plan_hash``. The plan binding must be recorded explicitly in
    ``metadata_sanitized`` at approval-creation time; nothing is invented here.
    """

    approval_id = str(getattr(record, "approval_id", "") or "")
    metadata = getattr(record, "metadata_sanitized", None)
    if not isinstance(metadata, dict):
        raise ApprovalAdapterError("approval metadata is missing", approval_id=approval_id)

    plan_id = metadata.get("plan_id") or getattr(record, "resource", None)
    plan_hash = metadata.get("plan_hash")
    if not plan_id:
        raise ApprovalAdapterError(
            "approval is not bound to a plan_id", approval_id=approval_id
        )
    if not plan_hash:
        raise ApprovalAdapterError(
            "approval is not bound to a plan_hash", approval_id=approval_id
        )

    decision = getattr(record, "decision", None)
    status = str(getattr(decision, "value", decision) or "requested")

    return PlanApproval(
        approval_id=approval_id,
        plan_id=str(plan_id),
        plan_hash=str(plan_hash),
        status=status,
        approver=getattr(record, "principal", None),
        expires_at=getattr(record, "expires_at", None),
        consumed_at=getattr(record, "consumed_at", None),
        metadata={"approval_source": "registry"},
    )
