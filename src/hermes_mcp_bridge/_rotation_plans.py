"""Process-local plan registry providing unforgeable, single-use plan tokens.

A RotationPlan is built by ``plan_rotation`` and registered here with a CSPRNG
nonce-based proof bound to the canonical content (key, absolute paths, source
digests, new-value digest, active-runs/health snapshot, created_at, expiry and
nonce). The proof is NOT derived from ``new_value`` alone, so a manually built
plan cannot be replayed or forged.

The registry is thread-safe and process-local. Each plan is single-use and has a
short TTL: ``apply_rotation`` consumes it atomically. Cross-process reuse is
only permitted when an HMAC secret (HERMES_BRIDGE_HMAC_SECRET) is configured;
without it, a plan produced in another process must fail closed. The new secret
value and raw digests are never persisted here.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass

PLAN_TTL_SECONDS = 120
_HMAC_ENV = "HERMES_BRIDGE_HMAC_SECRET"


@dataclass
class _PlanRecord:
    proof: str
    created_at: float
    expires_at: float
    consumed: bool = False


_registry: dict[str, _PlanRecord] = {}
_registry_lock = threading.Lock()


def _hmac_secret() -> str | None:
    val = os.environ.get(_HMAC_ENV)
    return val if val else None


def _content_proof(
    *,
    key: str,
    new_value: str | None,
    changed_paths: list[str],
    source_digests: list[str],
    active_runs: int,
    requires_restart: bool,
    nonce: str,
    created_at: float,
) -> str:
    """Bind the proof to the canonical plan content, not new_value alone."""
    h = hashlib.sha256()
    h.update(b"rotation-plan-v1\n")
    h.update(f"key={key}\n".encode())
    for p in sorted(changed_paths):
        h.update(f"path={p}\n".encode())
    for d in sorted(source_digests):
        h.update(f"src={d}\n".encode())
    # digest of new_value only, never the value
    if new_value is None:
        h.update(b"new=none\n")
    else:
        h.update(f"new={hashlib.sha256(new_value.encode()).hexdigest()}\n".encode())
    h.update(f"runs={active_runs}\n".encode())
    h.update(f"restart={int(requires_restart)}\n".encode())
    h.update(f"nonce={nonce}\n".encode())
    h.update(f"created_at={created_at:.6f}\n".encode())
    secret = _hmac_secret()
    if secret:
        h.update(f"hmac={hashlib.sha256(secret.encode()).hexdigest()}\n".encode())
    else:
        h.update(b"hmac=none\n")
    return h.hexdigest()[:32]


def register_plan(
    *,
    key: str,
    new_value: str | None,
    changed_paths: list[str],
    source_digests: list[str],
    active_runs: int,
    requires_restart: bool,
) -> tuple[str, str]:
    """Register a plan and return ``(plan_token, nonce)``."""
    nonce = secrets.token_hex(16)
    created_at = time.monotonic()
    proof = _content_proof(
        key=key,
        new_value=new_value,
        changed_paths=changed_paths,
        source_digests=source_digests,
        active_runs=active_runs,
        requires_restart=requires_restart,
        nonce=nonce,
        created_at=created_at,
    )
    with _registry_lock:
        _registry[proof] = _PlanRecord(
            proof=proof,
            created_at=created_at,
            expires_at=created_at + PLAN_TTL_SECONDS,
        )
    return proof, nonce


def verify_and_consume(
    *,
    plan_token: str,
    key: str,
    new_value: str | None,
    changed_paths: list[str],
    source_digests: list[str],
    active_runs: int,
    requires_restart: bool,
    nonce: str | None,
) -> None:
    """Verify a plan token and consume it atomically.

    Raises ValueError if the token is unknown, expired, already consumed, or does
    not match the canonical content (i.e. plan was manually built or altered).
    """
    now = time.monotonic()
    with _registry_lock:
        rec = _registry.get(plan_token)
        if rec is None:
            raise ValueError("plan token unknown: plan was forged or produced elsewhere")
        if rec.consumed:
            raise ValueError("plan token already consumed (single-use)")
        if now > rec.expires_at:
            _registry.pop(plan_token, None)
            raise ValueError("plan token expired")
        recomputed = _content_proof(
            key=key,
            new_value=new_value,
            changed_paths=changed_paths,
            source_digests=source_digests,
            active_runs=active_runs,
            requires_restart=requires_restart,
            nonce=nonce or "",
            created_at=rec.created_at,
        )
        if recomputed != plan_token:
            raise ValueError("plan token mismatch: plan content altered")
        rec.consumed = True


def purge_expired() -> int:
    now = time.monotonic()
    removed = 0
    with _registry_lock:
        for k in [k for k, v in _registry.items() if now > v.expires_at]:
            _registry.pop(k, None)
            removed += 1
    return removed
