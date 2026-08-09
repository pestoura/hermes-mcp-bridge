"""Phase 7 provider contract: manifests, capability declarations, reason codes.

> **V2 · PHASE 7 · runtime, disabled by default behind ``PROVIDER_FEATURE_ENABLED``**

A provider joins the V2 execution gateway through a *declarative manifest* only.
The manifest is a pure value: it performs no I/O, resolves no credential and
names no secret. Everything a provider is allowed to do — which capabilities it
exposes, which credential capability each one needs, which scopes, which egress
hosts, which budgets — is enumerated here and validated at load time.

Design references (design lane ``docs/v2/phase7/*.md``): plugin boundary,
capability discovery, credential isolation, audit and policy ordering.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Any

from .canonical import canonical_hash, canonical_json_text
from .enums import IdempotencySemantics, MutationClass, SecurityTier

#: The Phase 7 runtime is never wired into the MCP surface until its gate is
#: green. V1 keeps exactly 27 tools regardless of this module being importable.
PROVIDER_FEATURE_ENABLED = False

PROVIDER_CONTRACT_VERSION = "1"
PROVIDER_MAX_CAPABILITIES = 32
PROVIDER_MAX_SCOPES = 16
PROVIDER_MAX_EGRESS_HOSTS = 8
PROVIDER_MIN_RESULT_BYTES = 1024
PROVIDER_MAX_RESULT_BYTES = 1_048_576
PROVIDER_MAX_DEADLINE_MS = 60_000

_IDENTIFIER_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9_-]*[a-z0-9])?)*$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SEGMENT_SPLIT_RE = re.compile(r"[._\-:/]")

#: Whole-word credential names. Matching is by exact segment, never substring,
#: so ``token_accounting`` and ``max_agentic_tokens`` are legitimate fields.
SECRET_SHAPED_NAMES = frozenset(
    {
        "apikey",
        "api_key",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "passwd",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)


@unique
class ProviderReason(StrEnum):
    """Closed, stable reason codes for the integration path.

    Every code is safe as a bounded metric label (no free text, no identifiers).
    """

    OK = "OK"
    # request / schema
    E_REQ_INVALID = "E-REQ-INVALID"
    # provider / capability resolution
    E_PROVIDER_UNKNOWN = "E-PROVIDER-UNKNOWN"
    E_PROVIDER_DISABLED = "E-PROVIDER-DISABLED"
    E_CAP_UNDECLARED = "E-CAP-UNDECLARED"
    E_CAP_DUPLICATE = "E-CAP-DUPLICATE"
    E_CAP_SCOPE_EXCEEDS_CREDENTIAL = "E-CAP-SCOPE-EXCEEDS-CREDENTIAL"
    E_CAP_PROBE_INCONCLUSIVE = "E-CAP-PROBE-INCONCLUSIVE"
    E_CAP_NOT_READY = "E-CAP-NOT-READY"
    E_CAP_WRITE_DISCOVERY_DENIED = "E-CAP-WRITE-DISCOVERY-DENIED"
    # scope / policy
    E_SCOPE_DENY = "E-SCOPE-DENY"
    E_POLICY_DENY = "E-POLICY-DENY"
    E_POLICY_UNAVAILABLE = "E-POLICY-UNAVAILABLE"
    # approval / idempotency
    E_APPROVAL_MISSING = "E-APPROVAL-MISSING"
    E_APPROVAL_DIGEST_MISMATCH = "E-APPROVAL-DIGEST-MISMATCH"
    E_IDEMPOTENCY_REPLAY = "E-IDEMPOTENCY-REPLAY"
    E_IDEMPOTENCY_UNAVAILABLE = "E-IDEMPOTENCY-UNAVAILABLE"
    # credentials
    E_CRED_CROSS_DOMAIN = "E-CRED-CROSS-DOMAIN"
    E_CRED_UNAVAILABLE = "E-CRED-UNAVAILABLE"
    E_CRED_REVOKED = "E-CRED-REVOKED"
    # audit
    E_AUDIT_UNAVAILABLE = "E-AUDIT-UNAVAILABLE"
    # provider execution
    E_PROVIDER_EGRESS_DENIED = "E-PROVIDER-EGRESS-DENIED"
    E_PROVIDER_REDIRECT = "E-PROVIDER-REDIRECT"
    E_PROVIDER_SHAPE = "E-PROVIDER-SHAPE"
    E_PROVIDER_RESULT_TOO_LARGE = "E-PROVIDER-RESULT-TOO-LARGE"
    E_PROVIDER_DEADLINE = "E-PROVIDER-DEADLINE"
    E_PROVIDER_FAULT = "E-PROVIDER-FAULT"
    E_PROVIDER_AUTH = "E-PROVIDER-AUTH"
    E_PROVIDER_RATE_LIMIT = "E-PROVIDER-RATE-LIMIT"
    # budget
    E_BUDGET_EXCEEDED = "E-BUDGET-EXCEEDED"
    # manifest load
    E_MANIFEST_INVALID = "E-MANIFEST-INVALID"
    E_MANIFEST_CONTRACT_MISMATCH = "E-MANIFEST-CONTRACT-MISMATCH"


@unique
class CapabilityClass(StrEnum):
    """Discovery class of a provider capability."""

    DIRECT_READ = "DIRECT_READ"
    DIRECT_WRITE = "DIRECT_WRITE"

    @property
    def is_write(self) -> bool:
        return self is CapabilityClass.DIRECT_WRITE


@unique
class ProviderStatus(StrEnum):
    """Acceptance status of an integration lane. Never inferred, only declared."""

    ACCEPTED = "ACCEPTED"
    CANDIDATE = "CANDIDATE"
    BLOCKED_UNCONFIRMED = "BLOCKED_UNCONFIRMED"


class ProviderContractError(ValueError):
    """A manifest or declaration violated a Phase 7 invariant.

    The message carries only the failing field name and a stable reason code.
    """

    def __init__(self, reason: ProviderReason, field_name: str) -> None:
        self.reason = reason
        self.field_name = field_name
        super().__init__(f"{reason.value}:{field_name}")


def _identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, field_name)
    text = value.strip().lower()
    if not text or not _IDENTIFIER_RE.fullmatch(text):
        raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, field_name)
    return text


def is_secret_shaped(name: str) -> bool:
    """True when any whole segment of ``name`` is a credential name."""
    lowered = name.strip().lower()
    if lowered in SECRET_SHAPED_NAMES:
        return True
    segments = [segment for segment in _SEGMENT_SPLIT_RE.split(lowered) if segment]
    if any(segment in SECRET_SHAPED_NAMES for segment in segments):
        return True
    # Two-segment credential names (``api_key``) survive the split above.
    for index in range(len(segments) - 1):
        if f"{segments[index]}_{segments[index + 1]}" in SECRET_SHAPED_NAMES:
            return True
    return False


def _reject_secret_shaped(value: str, *, field_name: str) -> str:
    if is_secret_shaped(value):
        raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, field_name)
    return value


def _host(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, field_name)
    text = value.strip().lower()
    if not text or "*" in text or not _HOST_RE.fullmatch(text):
        raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, field_name)
    return text


def _scope(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, field_name)
    text = value.strip()
    if not text or "*" in text or not _SCOPE_RE.fullmatch(text):
        raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, field_name)
    return text


def _bounded_int(
    value: object,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, field_name)
    if not minimum <= value <= maximum:
        raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, field_name)
    return value


@dataclass(frozen=True, slots=True)
class CapabilityDeclaration:
    """One declared provider capability, bound to exactly one typed tool."""

    capability_id: str
    capability_class: CapabilityClass
    tool_id: str
    credential_capability_id: str
    security_tier: SecurityTier
    mutation_class: MutationClass
    idempotency: IdempotencySemantics
    scopes: tuple[str, ...]
    egress_hosts: tuple[str, ...]
    max_result_bytes: int = 65_536
    deadline_ms: int = 10_000
    approval_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capability_id",
            _reject_secret_shaped(
                _identifier(self.capability_id, field_name="capability_id"),
                field_name="capability_id",
            ),
        )
        object.__setattr__(self, "tool_id", _identifier(self.tool_id, field_name="tool_id"))
        object.__setattr__(
            self,
            "credential_capability_id",
            _identifier(self.credential_capability_id, field_name="credential_capability_id"),
        )
        if not isinstance(self.capability_class, CapabilityClass):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "capability_class")
        if not isinstance(self.security_tier, SecurityTier):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "security_tier")
        if not isinstance(self.mutation_class, MutationClass):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "mutation_class")
        if not isinstance(self.idempotency, IdempotencySemantics):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "idempotency")

        scopes = tuple(_scope(item, field_name="scopes") for item in self.scopes)
        if not scopes or len(scopes) > PROVIDER_MAX_SCOPES or len(set(scopes)) != len(scopes):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "scopes")
        object.__setattr__(self, "scopes", tuple(sorted(scopes)))

        hosts = tuple(_host(item, field_name="egress_hosts") for item in self.egress_hosts)
        if not hosts or len(hosts) > PROVIDER_MAX_EGRESS_HOSTS or len(set(hosts)) != len(hosts):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "egress_hosts")
        object.__setattr__(self, "egress_hosts", tuple(sorted(hosts)))

        object.__setattr__(
            self,
            "max_result_bytes",
            _bounded_int(
                self.max_result_bytes,
                field_name="max_result_bytes",
                minimum=PROVIDER_MIN_RESULT_BYTES,
                maximum=PROVIDER_MAX_RESULT_BYTES,
            ),
        )
        object.__setattr__(
            self,
            "deadline_ms",
            _bounded_int(
                self.deadline_ms,
                field_name="deadline_ms",
                minimum=1,
                maximum=PROVIDER_MAX_DEADLINE_MS,
            ),
        )
        if not isinstance(self.approval_required, bool):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "approval_required")

        # A read capability may not carry a mutating class, and a write
        # capability may not claim READ idempotency: the class is fixed at
        # registration and can never be reinterpreted at runtime (I4).
        if self.capability_class is CapabilityClass.DIRECT_READ:
            if self.mutation_class.mutates:
                raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "mutation_class")
            if self.idempotency is not IdempotencySemantics.READ:
                raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "idempotency")
            if not self.security_tier.is_read_only_tier:
                raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "security_tier")
        else:
            if not self.mutation_class.mutates:
                raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "mutation_class")
            if self.idempotency is IdempotencySemantics.READ:
                raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "idempotency")
            if self.security_tier.is_read_only_tier:
                raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "security_tier")

    @property
    def is_write(self) -> bool:
        return self.capability_class.is_write

    def canonical(self) -> dict[str, Any]:
        return {
            "approval_required": self.approval_required,
            "capability_class": self.capability_class.value,
            "capability_id": self.capability_id,
            "credential_capability_id": self.credential_capability_id,
            "deadline_ms": self.deadline_ms,
            "egress_hosts": list(self.egress_hosts),
            "idempotency": self.idempotency.value,
            "max_result_bytes": self.max_result_bytes,
            "mutation_class": self.mutation_class.value,
            "scopes": list(self.scopes),
            "security_tier": self.security_tier.value,
            "tool_id": self.tool_id,
        }


@dataclass(frozen=True, slots=True)
class CredentialDomain:
    """The credential domain a provider owns: at most one read + one write id."""

    provider_id: str
    read_capability_id: str | None = None
    write_capability_id: str | None = None
    granted_scopes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _identifier(self.provider_id, field_name="provider_id")
        )
        for name in ("read_capability_id", "write_capability_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _identifier(value, field_name=name))
        if self.read_capability_id is None and self.write_capability_id is None:
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "credential_domain")
        granted: dict[str, tuple[str, ...]] = {}
        for key, scopes in dict(self.granted_scopes).items():
            capability = _identifier(key, field_name="granted_scopes")
            if capability not in self.capability_ids:
                raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "granted_scopes")
            values = tuple(sorted({_scope(item, field_name="granted_scopes") for item in scopes}))
            if not values or len(values) > PROVIDER_MAX_SCOPES:
                raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "granted_scopes")
            granted[capability] = values
        if set(granted) != set(self.capability_ids):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "granted_scopes")
        object.__setattr__(self, "granted_scopes", granted)

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (self.read_capability_id, self.write_capability_id)
            if value is not None
        )

    def contains(self, credential_capability_id: str) -> bool:
        return credential_capability_id in self.capability_ids

    def scope_digest(self, credential_capability_id: str) -> str:
        """SHA-256 of the granted scope-set. The scopes themselves are audit-safe,
        but the digest is what evidence records, so a widening is detectable."""
        scopes = self.granted_scopes.get(credential_capability_id, ())
        return canonical_hash({"capability": credential_capability_id, "scopes": list(scopes)})

    def canonical(self) -> dict[str, Any]:
        return {
            "capability_ids": list(self.capability_ids),
            "provider_id": self.provider_id,
            "scope_digests": {
                capability: self.scope_digest(capability) for capability in self.capability_ids
            },
        }


@dataclass(frozen=True, slots=True)
class ProviderManifest:
    """Static description of one provider. Pure value, no I/O, no credentials."""

    provider_id: str
    provider_version: str
    contract_version: str
    capabilities: tuple[CapabilityDeclaration, ...]
    credential_domain: CredentialDomain
    status: ProviderStatus = ProviderStatus.CANDIDATE

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _identifier(self.provider_id, field_name="provider_id")
        )
        version = str(self.provider_version).strip()
        if not version or len(version) > 32 or not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "provider_version")
        object.__setattr__(self, "provider_version", version)
        if self.contract_version != PROVIDER_CONTRACT_VERSION:
            raise ProviderContractError(
                ProviderReason.E_MANIFEST_CONTRACT_MISMATCH, "contract_version"
            )
        if not isinstance(self.status, ProviderStatus):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "status")

        capabilities = tuple(self.capabilities)
        if not capabilities or len(capabilities) > PROVIDER_MAX_CAPABILITIES:
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "capabilities")
        ids = [capability.capability_id for capability in capabilities]
        if len(set(ids)) != len(ids):
            raise ProviderContractError(ProviderReason.E_CAP_DUPLICATE, "capabilities")
        if self.credential_domain.provider_id != self.provider_id:
            raise ProviderContractError(ProviderReason.E_CRED_CROSS_DOMAIN, "credential_domain")

        for capability in capabilities:
            if not capability.capability_id.startswith(f"{self.provider_id}."):
                raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "capability_id")
            if not self.credential_domain.contains(capability.credential_capability_id):
                raise ProviderContractError(
                    ProviderReason.E_CRED_CROSS_DOMAIN, "credential_capability_id"
                )
            granted = set(
                self.credential_domain.granted_scopes[capability.credential_capability_id]
            )
            if not set(capability.scopes).issubset(granted):
                raise ProviderContractError(
                    ProviderReason.E_CAP_SCOPE_EXCEEDS_CREDENTIAL, capability.capability_id
                )
            if (
                capability.is_write
                and capability.credential_capability_id
                == self.credential_domain.read_capability_id
            ):
                raise ProviderContractError(
                    ProviderReason.E_CRED_CROSS_DOMAIN, capability.capability_id
                )
        object.__setattr__(
            self, "capabilities", tuple(sorted(capabilities, key=lambda item: item.capability_id))
        )

    def capability(self, capability_id: str) -> CapabilityDeclaration:
        for candidate in self.capabilities:
            if candidate.capability_id == capability_id:
                return candidate
        raise ProviderContractError(ProviderReason.E_CAP_UNDECLARED, capability_id)

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(capability.capability_id for capability in self.capabilities)

    @property
    def write_capabilities(self) -> tuple[CapabilityDeclaration, ...]:
        return tuple(capability for capability in self.capabilities if capability.is_write)

    @property
    def egress_hosts(self) -> tuple[str, ...]:
        hosts: set[str] = set()
        for capability in self.capabilities:
            hosts.update(capability.egress_hosts)
        return tuple(sorted(hosts))

    def canonical(self) -> dict[str, Any]:
        return {
            "capabilities": [capability.canonical() for capability in self.capabilities],
            "contract_version": self.contract_version,
            "credential_domain": self.credential_domain.canonical(),
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "status": self.status.value,
        }

    def manifest_digest(self) -> str:
        return canonical_hash(self.canonical())

    def canonical_json(self) -> str:
        return canonical_json_text(self.canonical())


#: Grammatical suffixes that turn a credential *name* into a reference to it.
#: ``token`` is secret material; ``token_digest`` and ``credential_capability_id``
#: are governance metadata. Stripping these before the exact-name comparison is
#: what keeps the scan from rejecting legitimate fields (the substring-scan trap).
_REFERENCE_SUFFIXES = ("_id", "_ids", "_ref", "_refs", "_digest", "_count", "_state", "_class")

#: Prefixes that unambiguously introduce materialized credential bytes in a
#: string *value*. Matched case-insensitively at the start of the value only.
_SECRET_VALUE_PREFIXES = (
    "bearer ",
    "basic ",
    "ghp_",
    "gho_",
    "github_pat_",
    "glpat-",
    "xoxb-",
    "-----begin",
)


def _key_is_secret(name: str) -> bool:
    """Exact whole-name match, after stripping a reference suffix.

    Deliberately *not* segment-splitting: ``credential_capability_id`` names a
    reference, not material, and rejecting it would make every audit record
    unwritable.
    """
    lowered = name.strip().lower()
    if lowered in SECRET_SHAPED_NAMES:
        return True
    for suffix in _REFERENCE_SUFFIXES:
        if lowered.endswith(suffix) and lowered[: -len(suffix)] in SECRET_SHAPED_NAMES:
            return False
    return False


def _value_is_secret(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in SECRET_SHAPED_NAMES:
        return True
    return any(lowered.startswith(prefix) for prefix in _SECRET_VALUE_PREFIXES)


def audit_safe(payload: Any, *, path: str = "$") -> list[str]:
    """Return the paths of secret-shaped material found in ``payload``.

    Structural whole-name matching on keys and prefix matching on values —
    never a substring scan, which would eventually reject a legitimate
    governance field such as ``max_agentic_tokens``.
    """
    findings: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if isinstance(key, str) and _key_is_secret(key):
                findings.append(f"{path}.{key}")
            findings.extend(audit_safe(value, path=f"{path}.{key}"))
    elif isinstance(payload, str):
        if _value_is_secret(payload):
            findings.append(path)
    elif isinstance(payload, Sequence) and not isinstance(payload, str | bytes):
        for index, value in enumerate(payload):
            findings.extend(audit_safe(value, path=f"{path}[{index}]"))
    return findings


def declared_capability_ids(manifests: Iterable[ProviderManifest]) -> tuple[str, ...]:
    ids: list[str] = []
    for manifest in manifests:
        ids.extend(manifest.capability_ids)
    return tuple(sorted(ids))


__all__ = [
    "PROVIDER_CONTRACT_VERSION",
    "PROVIDER_FEATURE_ENABLED",
    "PROVIDER_MAX_DEADLINE_MS",
    "PROVIDER_MAX_RESULT_BYTES",
    "SECRET_SHAPED_NAMES",
    "CapabilityClass",
    "CapabilityDeclaration",
    "CredentialDomain",
    "ProviderContractError",
    "ProviderManifest",
    "ProviderReason",
    "ProviderStatus",
    "audit_safe",
    "declared_capability_ids",
    "is_secret_shaped",
]
