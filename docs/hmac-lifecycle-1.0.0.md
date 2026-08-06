# Hermes MCP Bridge 1.0.0 — bounded HMAC lifecycle

## Objective

Provide a controlled `current`/`previous` HMAC rotation window without turning
the previous key into a second permanent credential.

The current key is the only signing key. The previous key is verification-only
and, in `production` or `security_required`, is active only inside an explicit
timezone-aware interval whose total duration cannot exceed seven days.

## Configuration contract

Secret values remain file-backed:

```text
HERMES_BRIDGE_HMAC_SECRET_FILE=/run/secrets/hermes_bridge_hmac_secret
HERMES_BRIDGE_HMAC_SECRET_PREVIOUS_FILE=/run/secrets/hermes_bridge_hmac_secret_previous
```

Non-secret rotation metadata:

```text
HERMES_BRIDGE_HMAC_KEY_ID=2026-08-key1
HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID=2026-07-key0
HERMES_BRIDGE_HMAC_PREVIOUS_VALID_FROM=2026-08-06T12:00:00Z
HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL=2026-08-08T12:00:00Z
```

Requirements in strict security modes:

- the current key is present and meets `BRIDGE_MIN_SECRET_LENGTH`;
- current and previous key material are different;
- current and previous key IDs, when both present, are different;
- a bounded previous key has its own non-empty key ID;
- validity start and deadline are both valid ISO-8601 with timezone;
- the deadline is strictly after the start;
- the interval duration is no more than seven days;
- previous metadata cannot exist without a previous key.

The seven-day maximum is a code-level safety bound and is not operator
overridable. Binding the bound to both ends of the interval prevents an
excessive future deadline from becoming valid merely because time passed.

## State model

| Previous secret | Validity interval | State | Verification |
| --- | --- | --- | --- |
| absent | absent | none | current only |
| present | current time before start | pending | current only |
| present | start <= current time < end | active | current + previous |
| present | current time >= end | expired | current only |
| present | absent in strict mode | invalid | posture not ready |
| present | partial, invalid, naive or >7 days | invalid | posture not ready |

The start is inclusive. The end is exclusive: at the exact configured deadline,
the previous key is expired and its signatures are rejected.

Expired previous material does not prevent the current key from signing. This
allows the service to remain available while operators remove the stale file,
but readiness visibly reports:

```text
previous_configured=true
previous_active=false
previous_pending=false
previous_expired=true
```

A future bounded interval is visible as `previous_pending=true`; the previous
key does not verify until the start instant.

## Rotation procedure

### 1. Prepare

1. Confirm bridge health/readiness and policy posture.
2. Confirm current key ID and source type without reading the secret.
3. Generate the new current key into a protected temporary file.
4. Copy the existing current secret file to the protected previous-secret path.
5. Set ownership to the bridge UID/GID and mode `0400` or `0600`.
6. Select a previous key ID distinct from the new current key ID.
7. Select a timezone-aware start and deadline with a duration no greater than
   seven days; prefer the shortest operationally sufficient interval.

Never print, compare in terminal output, log or commit either secret.

### 2. Activate atomically

Update the deployment inputs so that one controlled restart receives:

- new current secret file;
- old current secret as previous file;
- new current key ID;
- old key ID as previous key ID;
- explicit previous validity start;
- explicit previous validity deadline.

Run preflight before replacing the container. Preflight must validate file
existence, permissions, ownership, minimum length and non-secret metadata.

### 3. Validate pending and active states

If the configured start is still in the future, prove:

- `previous_pending=true`;
- old signatures remain rejected;
- current signing remains available.

Inside the active interval, prove:

- health/readiness is `ready`;
- current signing succeeds and reports the new current key ID;
- a fixture signed by the previous key verifies;
- new signatures never use the previous key;
- posture reports `previous_active=true` and both normalized UTC timestamps;
- no secret values or paths appear in logs, metrics or readiness.

### 4. Observe automatic expiry

At or after the deadline, without changing the secret files, prove:

- previous signatures are rejected;
- current signatures continue to succeed;
- posture reports `previous_expired=true` and `previous_active=false`;
- no restart, database migration or policy change is required for rejection.

### 5. Clean up

After expiry evidence is recorded:

1. remove the previous secret from the deployment input;
2. remove `HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID`;
3. remove `HERMES_BRIDGE_HMAC_PREVIOUS_VALID_FROM`;
4. remove `HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL`;
5. securely remove the previous secret file according to the host procedure;
6. restart through the controlled deployment path;
7. confirm `previous_configured=false` and current signing remains ready.

Do not retain expired previous keys as an informal rollback mechanism.

## Rollback considerations

A rotation rollback is permitted only inside the active interval and must be an
explicit controlled deployment. It swaps the intended current/previous roles,
assigns distinct IDs and records a new bounded interval.

After the deadline, the previous key is no longer accepted. Restoring it as a
current key is a new rotation event requiring explicit operator approval and a
new evidence trail; it must not happen automatically.

## Relaxed modes

`development`, `dev` and `test` preserve compatibility with legacy fixtures
that provide a previous key without an interval. The posture exposes this as:

```text
previous_legacy_unbounded=true
```

This compatibility path never applies to `production` or
`security_required` and must not be used as deployment guidance.

## Acceptance

The isolated lifecycle gate requires:

- current-only signing;
- rejection before the start;
- previous verification from the inclusive start until the exclusive deadline;
- rejection at the exact deadline;
- current availability after expiry;
- rejection of missing, partial, invalid, naive and excessive intervals;
- rejection of identical key material or IDs;
- non-sensitive posture and logs;
- file-backed deployment preflight in the later `deploy/1.0.0` bundle.

Decision marker:

```text
HERMES_BRIDGE_1_0_0_HMAC_LIFECYCLE_PASS
```

This marker does not authorize production deployment and does not replace the
required Hermes/RITMO single-slot acceptance.
