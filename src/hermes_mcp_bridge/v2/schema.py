"""Canonical tool schema for V2 Phase 1.

Every field that drives a security decision is a typed enum (see
:mod:`hermes_mcp_bridge.v2.enums`); free strings are limited to identifiers,
which are normalized and validated here.

Phase 1 decision: the definition is an **in-process typed model** (Pydantic v2)
plus a canonical JSON projection. Registry persistence, storage and signing
remain an open question of ADR-0004; schema migration is likewise deferred.

Secrets — what is and is not guaranteed here
--------------------------------------------

The guarantee is **structural**, not heuristic:

* :class:`ToolDefinition` and the credential models have **no field** able to
  carry a raw credential value, a secret path or an environment variable name.
  ``extra`` is ``forbid`` everywhere, so a caller cannot smuggle one in through
  an unexpected key. A tool references a *credential capability id* only, which
  is an opaque identifier resolved by the broker at runtime.
* For **structured identifiers** (:attr:`ResourceKey.selector`, ``backend``) the
  value is a dotted identifier, and rejection is by **exact segment match**
  against :data:`SENSITIVE_CREDENTIAL_NAMES` — never by arbitrary substring. A
  segment ``token`` is rejected; ``patch-compatibility`` is not, because no
  segment equals a sensitive name.
* ``description`` is **human text and is not scanned**. Substring heuristics
  over prose produce false positives ("token accounting", "compatibility") and
  prove nothing about the absence of secrets, so they are deliberately absent.

JSON Schema is allowed to *name* sensitive runtime fields — a tool may legally
declare an input property called ``token``. What it must not do is carry a
**materialized** credential in the schema itself: a non-empty ``default``,
``const``, ``example`` or ``examples`` on a clearly sensitive property is
rejected by :func:`_validate_json_schema`, recursively through ``properties``,
``$defs``, ``items`` and ``allOf``/``anyOf``/``oneOf``. There is no regex for
real token formats; the check is purely structural.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from ._models import RegistryModel
from .canonical import canonical_hash
from .enums import (
    ApprovalRequirement,
    ExecutionMode,
    IdempotencySemantics,
    MutationClass,
    ResultShaping,
    RetryClass,
    SecurityTier,
    Stability,
)
from .errors import RegistryValidationError

#: Dotted identifier: lowercase segments, no wildcards, no empty segments.
_IDENTIFIER_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9_-]*[a-z0-9])?)*$")

#: Substrings that may never appear in an identifier, because they either make
#: the identifier a permissive pattern or suggest secret material.
_WILDCARD_TOKENS = ("*", "?", "[", "]", "**")

_SEGMENT_SPLIT_RE = re.compile(r"[._\-]")

#: Property/segment names that unambiguously denote credential material.
#: Matching is by **exact name**, never substring: ``token`` matches, while
#: ``token_accounting`` or ``compatibility`` do not.
SENSITIVE_CREDENTIAL_NAMES = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "private_key",
        "credential",
    }
)

#: JSON Schema keywords that materialize a literal value into the definition.
#: A sensitive property may be *declared*, but must not carry one of these.
_MATERIALIZING_KEYWORDS = ("default", "const", "example", "examples")

#: Bounds for ``timeout_seconds``. Unbounded or non-positive timeouts are a
#: reliability and denial-of-service hazard and are rejected outright.
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3600


def normalize_identifier(raw: str, *, field: str) -> str:
    """Validate and normalize a dotted identifier.

    Normalization is ``strip()`` + lowercase. Empty values, whitespace-only
    values and any glob/wildcard token are rejected.
    """
    if not isinstance(raw, str):
        raise RegistryValidationError(f"{field} must be a string")
    value = raw.strip().lower()
    if not value:
        raise RegistryValidationError(f"{field} must not be empty")
    for token in _WILDCARD_TOKENS:
        if token in value:
            raise RegistryValidationError(f"{field} must not contain wildcard {token!r}")
    if not _IDENTIFIER_RE.fullmatch(value):
        raise RegistryValidationError(
            f"{field} must be dotted lowercase segments of [a-z0-9_-]; got a non-conforming value"
        )
    return value


def _is_sensitive_name(name: str) -> bool:
    """True when ``name`` *is* a credential name, by exact match.

    The name is compared whole and, for compound identifiers, segment by
    segment (``.``/``_``/``-`` separated), so ``auth.token`` is sensitive while
    ``token_accounting`` and ``patch_compatibility`` are not.
    """
    lowered = name.strip().lower()
    if lowered in SENSITIVE_CREDENTIAL_NAMES:
        return True
    segments = [segment for segment in _SEGMENT_SPLIT_RE.split(lowered) if segment]
    return any(segment in SENSITIVE_CREDENTIAL_NAMES for segment in segments)


def _reject_sensitive_identifier(value: str, *, field: str) -> None:
    """Reject a structured identifier whose segments name credential material.

    Exact segment matching only. Human prose must never be passed here.
    """
    if _is_sensitive_name(value):
        raise RegistryValidationError(
            f"{field} must not be named after credential material"
        )


def _is_materialized(value: Any) -> bool:
    """True when a schema keyword carries a non-empty literal value."""
    if value is None:
        return False
    if isinstance(value, str | bytes | list | tuple | dict | set):
        return len(value) > 0
    return True


def _reject_materialized_credentials(
    node: Any, *, field: str, path: str = "", sensitive: bool = False
) -> None:
    """Recursively reject materialized credential literals in a JSON Schema.

    A schema may freely *declare* a property named ``token``. What it may not
    do is pin a non-empty ``default``/``const``/``example``/``examples`` on it,
    which would persist a credential inside the registry definition.
    """
    if not isinstance(node, dict):
        return

    if sensitive:
        for keyword in _MATERIALIZING_KEYWORDS:
            if keyword in node and _is_materialized(node[keyword]):
                where = path or "<root>"
                raise RegistryValidationError(
                    f"{field} must not materialize a credential value: "
                    f"sensitive property {where!r} declares a non-empty {keyword!r}"
                )

    for container in ("properties", "$defs", "definitions", "patternProperties"):
        block = node.get(container)
        if isinstance(block, dict):
            for name, child in block.items():
                child_path = f"{path}.{name}" if path else str(name)
                child_sensitive = container in ("properties", "patternProperties") and (
                    isinstance(name, str) and _is_sensitive_name(name)
                )
                _reject_materialized_credentials(
                    child, field=field, path=child_path, sensitive=child_sensitive
                )

    items = node.get("items")
    if isinstance(items, dict):
        _reject_materialized_credentials(
            items, field=field, path=f"{path}[]", sensitive=sensitive
        )
    elif isinstance(items, list):
        for index, child in enumerate(items):
            _reject_materialized_credentials(
                child, field=field, path=f"{path}[{index}]", sensitive=sensitive
            )

    for combinator in ("allOf", "anyOf", "oneOf"):
        block = node.get(combinator)
        if isinstance(block, list):
            for index, child in enumerate(block):
                _reject_materialized_credentials(
                    child,
                    field=field,
                    path=f"{path}/{combinator}[{index}]",
                    sensitive=sensitive,
                )

    for single in ("not", "additionalProperties", "contains"):
        child = node.get(single)
        if isinstance(child, dict):
            _reject_materialized_credentials(
                child, field=field, path=f"{path}/{single}", sensitive=sensitive
            )


def _validate_json_schema(schema: Any, *, field: str) -> dict[str, Any]:
    """Phase 1 schema check: must be a JSON object describing an object.

    Additionally enforces that no clearly sensitive property materializes a
    credential literal; see :func:`_reject_materialized_credentials`.
    """
    if not isinstance(schema, dict):
        raise RegistryValidationError(f"{field} must be a JSON object")
    if not schema:
        raise RegistryValidationError(f"{field} must not be empty")
    declared = schema.get("type")
    if declared is not None and declared != "object":
        raise RegistryValidationError(f"{field} must declare type 'object'")
    for key in schema:
        if not isinstance(key, str):
            raise RegistryValidationError(f"{field} keys must be strings")
    _reject_materialized_credentials(schema, field=field)
    return schema


class RetryPolicy(RegistryModel):
    """Bounded retry policy. All durations are integer seconds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retry_class: RetryClass = RetryClass.NO_RETRY
    max_attempts: Annotated[int, Field(ge=1, le=10)] = 1
    initial_backoff_seconds: Annotated[int, Field(ge=0, le=60)] = 0
    max_backoff_seconds: Annotated[int, Field(ge=0, le=300)] = 0
    honor_retry_after: bool = True

    @model_validator(mode="after")
    def _check(self) -> RetryPolicy:
        if self.retry_class is RetryClass.NO_RETRY and self.max_attempts != 1:
            raise RegistryValidationError("NO_RETRY requires max_attempts == 1")
        if self.retry_class is not RetryClass.NO_RETRY and self.max_attempts < 2:
            raise RegistryValidationError("retryable classes require max_attempts >= 2")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise RegistryValidationError("max_backoff_seconds < initial_backoff_seconds")
        return self

    def canonical(self) -> dict[str, Any]:
        return {
            "honor_retry_after": self.honor_retry_after,
            "initial_backoff_seconds": self.initial_backoff_seconds,
            "max_attempts": self.max_attempts,
            "max_backoff_seconds": self.max_backoff_seconds,
            "retry_class": self.retry_class.value,
        }


class ResourceKey(RegistryModel):
    """Concurrency/lock key for an operation.

    ``scope`` names the resource class (``repository``, ``service``, ...).
    ``selector`` is an opaque, non-secret identifier token. It is a dotted
    identifier, and any segment that *is* a credential name (exact match) is
    rejected; it never carries credential material or a filesystem path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    selector: str = "default"

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: str) -> str:
        return normalize_identifier(value, field="resource_key.scope")

    @field_validator("selector")
    @classmethod
    def _selector(cls, value: str) -> str:
        normalized = normalize_identifier(value, field="resource_key.selector")
        _reject_sensitive_identifier(normalized, field="resource_key.selector")
        return normalized

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.selector}"

    def canonical(self) -> dict[str, Any]:
        return {"scope": self.scope, "selector": self.selector}


class ToolDefinition(RegistryModel):
    """Canonical, immutable definition of one V2 tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- identity ---------------------------------------------------------
    tool_id: str
    provider: str
    operation: str
    version: Annotated[int, Field(ge=1)] = 1

    # --- execution --------------------------------------------------------
    execution_mode: ExecutionMode = ExecutionMode.DIRECT
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]

    # --- security classification -----------------------------------------
    security_tier: SecurityTier
    read_only: bool
    mutation_class: MutationClass
    idempotency: IdempotencySemantics
    policy_action: str
    approval_requirement: ApprovalRequirement = ApprovalRequirement.NOT_REQUIRED

    # --- capability / credential references -------------------------------
    capability_id: str
    credential_capability_id: str | None = None

    # --- reliability ------------------------------------------------------
    timeout_seconds: Annotated[
        int, Field(ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS)
    ]
    retry_policy: RetryPolicy = RetryPolicy()
    resource_key: ResourceKey

    # --- presentation / lifecycle (non-secret metadata) -------------------
    result_shaping: ResultShaping = ResultShaping.UNSUPPORTED
    stability: Stability = Stability.EXPERIMENTAL
    deprecated: bool = False
    description: str = ""
    backend: str = ""

    @field_validator("tool_id", "provider", "operation", "capability_id")
    @classmethod
    def _identifiers(cls, value: str, info: Any) -> str:
        return normalize_identifier(value, field=str(info.field_name))

    @field_validator("policy_action")
    @classmethod
    def _policy_action(cls, value: str) -> str:
        return normalize_identifier(value, field="policy_action")

    @field_validator("credential_capability_id")
    @classmethod
    def _credential_capability(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_identifier(value, field="credential_capability_id")

    @field_validator("backend")
    @classmethod
    def _backend(cls, value: str) -> str:
        """``backend`` is a short identifier (``github-api``), not prose.

        Empty is allowed. When present it must be a dotted identifier, and a
        segment that *is* a credential name is rejected by exact match — so
        ``github-api`` and ``patch-compatibility`` are accepted, while
        ``vault.token`` is rejected.
        """
        text = value.strip()
        if not text:
            return ""
        normalized = normalize_identifier(text, field="backend")
        _reject_sensitive_identifier(normalized, field="backend")
        return normalized

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        """Human text. Deliberately **not** scanned for secret-like words.

        Substring heuristics over prose reject legitimate wording ("token
        accounting", "compatibility") while proving nothing about the absence
        of secrets. The real guarantee is structural: no field of this model
        can hold credential material, and extras are forbidden.
        """
        return value.strip()

    @field_validator("input_schema")
    @classmethod
    def _input_schema(cls, value: Any) -> dict[str, Any]:
        return _validate_json_schema(value, field="input_schema")

    @field_validator("output_schema")
    @classmethod
    def _output_schema(cls, value: Any) -> dict[str, Any]:
        return _validate_json_schema(value, field="output_schema")

    @model_validator(mode="after")
    def _invariants(self) -> ToolDefinition:
        if self.read_only and self.mutation_class is not MutationClass.NONE:
            raise RegistryValidationError("read_only tools must declare mutation_class NONE")
        if not self.read_only and self.mutation_class is MutationClass.NONE:
            raise RegistryValidationError("mutating tools must not declare mutation_class NONE")
        if self.read_only and not self.security_tier.is_read_only_tier:
            raise RegistryValidationError("read_only tools must be tier T0 or T1")
        if not self.read_only and self.security_tier.is_read_only_tier:
            raise RegistryValidationError("mutating tools must be tier T2 or higher")
        if self.read_only and self.idempotency is not IdempotencySemantics.READ:
            raise RegistryValidationError("read_only tools must declare idempotency READ")
        if not self.read_only and self.idempotency is IdempotencySemantics.READ:
            raise RegistryValidationError("mutating tools must not declare idempotency READ")
        if self.mutation_class.is_destructive and not self.security_tier.is_destructive:
            raise RegistryValidationError("DESTRUCTIVE mutation requires security tier T4")
        if self.security_tier.is_destructive and not self.mutation_class.is_destructive:
            raise RegistryValidationError("tier T4 requires DESTRUCTIVE mutation class")
        if not self.tool_id.startswith(f"{self.provider}."):
            raise RegistryValidationError("tool_id must be namespaced by provider")
        if (
            not self.read_only
            and self.retry_policy.retry_class is RetryClass.RETRY_SAFE
            and self.idempotency is IdempotencySemantics.NON_IDEMPOTENT
        ):
            raise RegistryValidationError("non-idempotent mutations must not declare RETRY_SAFE")
        return self

    @property
    def is_destructive(self) -> bool:
        return self.mutation_class.is_destructive or self.security_tier.is_destructive

    def canonical(self) -> dict[str, Any]:
        """Canonical, non-secret projection used for the snapshot hash."""
        return {
            "approval_requirement": self.approval_requirement.value,
            "backend": self.backend,
            "capability_id": self.capability_id,
            "credential_capability_id": self.credential_capability_id,
            "deprecated": self.deprecated,
            "description": self.description,
            "execution_mode": self.execution_mode.value,
            "idempotency": self.idempotency.value,
            "input_schema": self.input_schema,
            "mutation_class": self.mutation_class.value,
            "operation": self.operation,
            "output_schema": self.output_schema,
            "policy_action": self.policy_action,
            "provider": self.provider,
            "read_only": self.read_only,
            "resource_key": self.resource_key.canonical(),
            "result_shaping": self.result_shaping.value,
            "retry_policy": self.retry_policy.canonical(),
            "security_tier": self.security_tier.value,
            "stability": self.stability.value,
            "timeout_seconds": self.timeout_seconds,
            "tool_id": self.tool_id,
            "version": self.version,
        }

    def definition_hash(self) -> str:
        return canonical_hash(self.canonical())


SchemaKind = Literal["input", "output"]

__all__ = [
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "SENSITIVE_CREDENTIAL_NAMES",
    "ResourceKey",
    "RetryPolicy",
    "ToolDefinition",
    "normalize_identifier",
]
