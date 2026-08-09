# Hermes MCP Bridge — Release 2.0.0 (V2 product release)

## What 2.0.0 is

`2.0.0` is the **product/delivery version** of the V2 programme: the gated
Phases 0–9 accepted on `main`, closed by the fail-closed gate
`V2_PRODUCTION_READY`.

It is **not** a bump of the V1 wire contract. The runtime deliberately keeps:

| Field | Value | Source of truth |
| --- | --- | --- |
| `bridge_version` / `manifest_version` | `1.0.0` | `src/hermes_mcp_bridge/config.py`, `__init__.py`, `pyproject.toml` |
| `contract_version` | `1.0.0` | `contracts.CURRENT_CONTRACT_VERSION` |
| `schema_version` (wire + SQLite) | `0.6.1` | `contracts.SCHEMA_VERSION` |
| Mandatory tool surface | 27 tools | `contracts.required_tools()` |
| SQLite migration ledger | v10 | `schema_migrations` |

Changing any of those values would be a client-visible breaking change and is
explicitly **out of scope** for this release. The V2 runtime
(`src/hermes_mcp_bridge/v2/`) is an additive, isolated package: no V1 module
imports it (enforced by gate check `P9-03`).

Read this as: **release 2.0.0 delivers the V2 programme on top of an unchanged
1.0.0 V1 contract.**

## Release binding

| Item | Value |
| --- | --- |
| Release version | `2.0.0` |
| Git tag | `v2.0.0` |
| Development gate | `V2_PRODUCTION_READY`, `failures=[]` |
| Accepted development commit | `5c761688fb6a28edfa8a65e18f2547b8fa3c7fba` |
| Release commit | the squash-merge commit of the release-prep PR on `main` |

The tag is annotated and immutable; it is never moved after publication.

## Scope

### Included

- Phases 0–9 of the V2 programme, each with a recorded gate and retained
  sanitized evidence (`docs/v2/evidence/`).
- Phase 9 production hardening: bounded lifecycle drain, audit digest chain,
  failure catalogue `F-01..F-20`, continuity scenarios `C-01..C-08`, replay
  suite, rollback/rotation/restore drills, secret-scan gate, supply-chain
  evidence, runbooks.
- Release artefacts: this document, changelog entry, compatibility statement,
  evidence manifest, release checklist.

### Not included

- No new tool, no renamed tool, no removed tool.
- No wire-schema change, no SQLite migration.
- No new feature: the release-prep change set is metadata and documentation
  only.
- No new publishing mechanism. Image publication is whatever the repository's
  CI already does — see "Artifacts" below.

## Artifacts

The repository has **no image-registry publishing workflow**. `.github/workflows/ci.yml`
builds `hermes-mcp-bridge:ci` locally in the `acceptance` job, validates its
provenance, runs isolated acceptance, scans it with Trivy, generates a CycloneDX
SBOM, and retains supply-chain evidence as a **draft** GitHub release keyed by
commit SHA:

| Evidence release tag | Assets |
| --- | --- |
| `sbom-evidence-<sha>` | `sbom-cyclonedx.json`, `image-provenance.json` |
| `phase1-registry-evidence-<sha>` | `phase1-registry-acceptance.json`, `phase1-registry-gate.json` |

No registry push exists in the repository, so none is invented here. The
supported artefact set for 2.0.0 is: the annotated tag, the GitHub Release, and
the CI-retained SBOM/provenance draft releases bound to the release commit.

Operators build the runtime image locally with the pinned build-arg set (see
`docs/installation.md` and `deploy/1.0.0/`).

## Installation

New installation is unchanged from 1.0.0: follow `docs/installation.md`, then
`docs/deployment.md`. The controlled rollout bundle is `deploy/1.0.0/`
(`preflight.sh` → `deploy.sh` dry-run → dual-gated mutation → `validate.sh`),
documented in `docs/production-rollout-1.0.0.md`. That bundle is a **frozen
release artefact** and is not modified by this release.

## Upgrade

From any accepted `1.0.0` deployment:

1. Clients need **no change**: the tool surface, wire schema and version fields
   are identical. `manifest_hash` is unchanged because the contract is
   unchanged.
2. Rebuild the image from the `v2.0.0` tag using the CI build-arg set
   (`OCI_IMAGE_REVISION` = release commit, `OCI_IMAGE_VERSION=1.0.0`,
   `BRIDGE_SCHEMA_VERSION=0.6.1`, `BRIDGE_CONTRACT_VERSION=1.0.0`).
3. Roll out with `deploy/1.0.0/` as a same-version candidate refresh. Export
   `ROLLBACK_BRIDGE_VERSION=1.0.0` explicitly — the script default is `0.9.0`
   and will otherwise fail `assert_image_version`.
4. Validate with `deploy/1.0.0/validate.sh`: 27 tools, `bridge_version=1.0.0`,
   `schema_version=0.6.1`, container healthy, upstream `ok`.

From `0.9.0` or earlier: perform the documented `1.0.0` rollout first
(`docs/production-rollout-1.0.0.md`), keeping `ROLLBACK_BRIDGE_VERSION=0.9.0`.

## Rollback

Rollback is the existing, rehearsed procedure — this release adds nothing new:

- Procedure: `deploy/1.0.0/rollback.sh`, dry-run by default, dual-gated,
  requiring exact `ROLLBACK_IMAGE`, `ROLLBACK_IMAGE_ID` and
  `ROLLBACK_BRIDGE_VERSION`. Marker on success: `ROLLBACK_1_0_0: PASS`.
- SQLite is **not** reverted: schema stays `0.6.1`, so no data migration is
  undone. The pre-deploy backup is retained for evidence and emergency recovery.
- Runbook: `docs/v2/phase9/runbooks.md` section **R2 Rollback**.
- Drill evidence: rollback, credential-rotation and audit-restore drills are
  executed and asserted by the Phase 9 gate
  (`scripts/validate_v2_phase9_production_gate.py`, drill checks
  `rollback`, `credential_rotation`, `audit_restore`) and by
  `tests/test_v2_phase9_drills.py`. No new drill was performed for 2.0.0; this
  release links to that prior evidence rather than claiming a fresh one.

## Compatibility with V1

See `docs/compatibility.md` §`1.0.0 -> 2.0.0`. Summary: fully backward
compatible; zero client action required.

## Operational documentation

| Topic | Document |
| --- | --- |
| Runbooks per failure class | `docs/v2/phase9/runbooks.md` |
| Rollback drills | `docs/v2/phase9/rollback-drills.md` |
| Secret scanning | `docs/v2/phase9/secret-scanning.md` |
| Supply chain / SBOM | `docs/v2/phase9/supply-chain-sbom.md` |
| Observability contract | `docs/v2/phase9/observability.md` |
| Acceptance criteria | `docs/v2/phase9/production-acceptance.md` |
| Evidence index | `docs/v2/evidence/README.md` |
| Release evidence manifest | `docs/release-2.0.0-evidence.md` |
| Release checklist | `docs/release-2.0.0-checklist.md` |

## Known limitations

- No container registry publication: images are built locally by operators and
  by CI. Immutable *registry* digests therefore do not exist for this release;
  provenance is recorded per build via `scripts/validate_image_provenance.py`.
- Supply-chain and Phase 1 evidence releases remain **drafts** by design of the
  existing CI retention step; they are not promoted by this release.
- The traceability matrix still marks a subset of V2-FR/V2-SEC rows as partially
  covered (`docs/v2/requirements/traceability-matrix.md`). Those rows are
  programme backlog, not release blockers under the accepted gate.
