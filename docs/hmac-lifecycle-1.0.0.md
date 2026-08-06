# Hermes MCP Bridge 1.0.0 — bounded HMAC lifecycle

## Objective

Provide a controlled `current`/`previous` HMAC rotation window without turning
the previous key into a second permanent credential.

The current key is the only signing key. The previous key is verification-only
and, in `production` or `security_required`, is active only until an explicit
UTC deadline no more than seven days in the future.

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
HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL=2026-08-08T12:00:00Z
```

Requirements in strict security modes:

- the current key is present and meets `BRIDGE_MIN_SECRET_LENGTH`;
- current and previous key material are different;
- current and previous key IDs, when both present, are different;
- an active previous key has its own non-empty key ID;
- the previous deadline is valid ISO-8601 and includes a timezone;
- the deadline is no more than seven days from the current instant;
- previous metadata cannot exist without a previous key.

The seven-day maximum is a code-level safety bound and is not operator
overridable.

## State model

| Previous secret | Deadline | State | Verification |
| --- | --- | --- | --- |
| absent | absent | none | current only |
| present | future and within seven days | active | current + previous |
| present | reached or past | expired | current only |
| present | absent in strict mode | invalid | posture not ready |
| present | invalid/naive/far future | invalid | posture not ready |

The deadline is exclusive: at the exact configured instant, the previous key is
expired and its signatures are rejected.

Expired previous material does not prevent the current key from signing. This
allows the service to remain available while operators remove the stale file,
but readiness visibly reports:

```text
previous_configured=true
previous_active=false
previous_expired=true
```

## Rotation procedure

### 1. Prepare

1. Confirm bridge health/readiness and policy posture.
2. Confirm current key ID and source type without reading the secret.
3. Generate the new current key into a protected temporary file.
4. Copy the existing current secret file to the protected previous-secret path.
5. Set ownership to the bridge UID/GID and mode `0400` or `0600`.
6. Select a previous key ID distinct from the new current key ID.
7. Select a UTC deadline no more than seven days ahead; prefer the shortest
   operationally sufficient window.

Never print, compare in terminal output, log or commit either secret.

### 2. Activate atomically

Update the deployment inputs so that one controlled restart receives:

- new current secret file;
- old current secret as previous file;
- new current key ID;
- old key ID as previous key ID;
- explicit previous validity deadline.

Run preflight before replacing the container. Preflight must validate file
existence, permissions, ownership, minimum length and non-secret metadata.

### 3. Validate the active window

After restart, prove:

- health/readiness is `ready`;
- current signing succeeds and reports the new current key ID;
- a fixture signed by the previous key verifies before the deadline;
- new signatures never use the previous key;
- posture reports `previous_active=true` and the normalized UTC deadline;
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
3. remove `HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL`;
4. securely remove the previous secret file according to the host procedure;
5. restart through the controlled deployment path;
6. confirm `previous_configured=false` and current signing remains ready.

Do not retain expired previous keys as an informal rollback mechanism.

## Rollback considerations

A rotation rollback is permitted only inside the active grace window and must
be an explicit controlled deployment. It swaps the intended current/previous
roles, assigns distinct IDs and sets a new bounded deadline.

After the deadline, the previous key is no longer accepted. Restoring it as a
current key is a new rotation event requiring explicit operator approval and a
new evidence trail; it must not happen automatically.

## Relaxed modes

`development`, `dev` and `test` preserve compatibility with legacy fixtures
that provide a previous key without a deadline. The posture exposes this as:

```text
previous_legacy_unbounded=true
```

This compatibility path never applies to `production` or
`security_required` and must not be used as deployment guidance.

## Acceptance

The isolated lifecycle gate requires:

- current-only signing;
- previous verification before deadline;
- rejection at the exact deadline;
- current availability after expiry;
- rejection of missing, invalid, naive and excessive deadlines;
- rejection of identical key material or IDs;
- non-sensitive posture and logs;
- file-backed deployment preflight in the later `deploy/1.0.0` bundle.

Decision marker:

```text
HERMES_BRIDGE_1_0_0_HMAC_LIFECYCLE_PASS
```

This marker does not authorize production deployment and does not replace the
required Hermes/RITMO single-slot acceptance.
