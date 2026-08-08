# V2 GitHub App runtime credential

This runbook provisions the short-lived GitHub App installation credential used
by the Phase 2 `github.read` DIRECT acceptance path.

## Security boundary

The GitHub App private key belongs on the Jarvas runtime only. It must never be
committed, pasted into an issue/PR, stored in acceptance evidence, printed in
logs, or passed as a bare environment variable.

The mint helper:

- requires the private-key file to be a regular non-symlink file with no
  group/other permissions;
- signs a short-lived RS256 GitHub App JWT locally with `openssl`;
- discovers the installation attached to the exact target repository;
- fails closed unless the installation permission map is exactly
  `checks/issues/metadata/pull_requests = read` and repository selection is
  `selected`;
- requests a token scoped to exactly one repository and exactly the four required
  read permissions;
- verifies the mint response before persisting anything;
- atomically writes the `ghs_` installation token with mode `0600`;
- atomically writes the sanitized Phase 2 provider attestation from the same mint
  response;
- never prints or returns the private key, App JWT, installation token, or secret
  filesystem paths.

GitHub installation tokens are short-lived, so the helper is safe to invoke again
for rotation. The V2 authorization provider reads the token file on demand and
therefore observes a rotated file without caching token material.

## Preconditions

1. GitHub App installed with **Only select repositories** on the exact target.
2. Exact repository permissions:
   - Checks: Read-only
   - Issues: Read-only
   - Metadata: Read-only
   - Pull requests: Read-only
3. Private key downloaded from the App settings and transferred directly to the
   Jarvas host.
4. A private runtime directory exists with mode `0700`.
5. The PEM inside that directory has mode `0600`.
6. `openssl` is available on Jarvas.
7. Use the GitHub App Client ID as `--issuer` where available; GitHub also accepts
   the App ID as JWT issuer.

## Mint

From a clean checkout of the accepted `main` commit on Jarvas:

```bash
python scripts/v2_github_app_mint.py \
  --issuer '<GITHUB_APP_CLIENT_ID>' \
  --private-key '<PRIVATE_0600_PEM>' \
  --repository 'pestoura/hermes-mcp-bridge' \
  --token-out '<PRIVATE_0700_DIR>/github-direct.token' \
  --attestation-out '<PRIVATE_0700_DIR>/github-direct-attestation.json'
```

Success prints only a sanitized record beginning with:

```text
GITHUB_APP_INSTALLATION_TOKEN_MINTED
```

The token output is then exposed to the connected collector through the existing
secret-file convention:

```bash
export BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE='<PRIVATE_0700_DIR>/github-direct.token'
```

Do not use a bare `BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN` value; the V2 provider
rejects it by design.

The generated attestation is passed directly to:

```text
scripts/v2_phase2_direct_read_acceptance.py --provider-attestation ...
```

No manual permission claim is required when the attestation was generated from
the verified installation-token mint response.

## Rotation

Re-running the mint command atomically replaces the token and attestation only
after GitHub has returned and the helper has verified:

- provider token family is `ghs_`;
- exact permission map;
- selected repository mode;
- exact single-repository set;
- future timezone-aware expiry.

A failed mint leaves no newly accepted token or attestation and returns a stable,
secret-free error code.

## Phase 2 acceptance boundary

Mint success alone does **not** promote `DIRECT_READ_ACCEPTED`. The connected
collector must still complete exactly 15 samples (five tools × three
repetitions), prove zero Hermes/LLM use on DIRECT, obtain real V1 token
accounting, establish a genuine V1 non-mutation basis, and pass the fail-closed
validator. Phase 3 must not start before that gate is accepted.
