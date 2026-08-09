# Release 2.0.0 — checklist

Fail-closed checklist for the `2.0.0` hand-off. An item is only ticked when the
stated command or artefact proves it. "Not supported by this repository" is a
legitimate terminal state and is recorded as such, not as a pass.

## Pre-merge

- [ ] Development gate `V2_PRODUCTION_READY` with `failures=[]` on the accepted
      commit — `scripts/validate_v2_phase9_production_gate.py --require-sha <sha>`.
- [ ] Release branch cut from the accepted commit; diff contains **only**
      release metadata and documentation (no `src/`, no `tests/` behaviour
      change, no `deploy/` bundle edit).
- [ ] `ruff check .` clean.
- [ ] `python -m compileall src tests scripts` clean.
- [ ] Full `pytest -q` green.
- [ ] ShellCheck rollout tests green (`test_shellcheck_clean` for 0.8.2 / 0.9.0 / 1.0.0).
- [ ] CI `test` matrix (3.11, 3.12) green.
- [ ] CI `acceptance` job green: image build, provenance validation, isolated
      acceptance, Trivy (CRITICAL/HIGH, `ignore-unfixed`), CycloneDX SBOM,
      evidence retention.
- [ ] Secret-scan blocking check green.
- [ ] V1 contract untouched: bridge `1.0.0`, schema `0.6.1`, 27 tools,
      migrations v10.

## Merge

- [ ] Squash-merge into `main` only when every required check is green.
- [ ] Record the merge commit SHA read back from the API, not from the merge
      command output.

## Tag and release

- [ ] Annotated tag `v2.0.0` created on the exact release commit.
- [ ] Tag pushed; `git rev-parse v2.0.0^{}` equals the release commit.
- [ ] GitHub Release `2.0.0` published (not draft), targeting the tag.
- [ ] Release notes carry: scope, V1 compatibility statement, upgrade steps,
      rollback pointer, evidence pointer, known limitations.

## Post-release verification

- [ ] Tag resolves to the intended commit.
- [ ] Release published and not draft.
- [ ] CI green on the release commit.
- [ ] Evidence draft releases (`sbom-evidence-<sha>`,
      `phase1-registry-evidence-<sha>`) exist for the release commit with their
      two assets each.
- [ ] Evidence digests reproduce (`sha256sum docs/v2/evidence/*.json`).
- [ ] Secret scan clean on the release commit.
- [ ] Rollback path documented and linked to executed drill evidence.
- [ ] Runtime bridge healthy where a deployment exists (not required by this
      release, which performs no deployment).

## Recorded as not supported by this repository

- Container-registry publication and immutable registry digests — no push
  workflow exists; images are built locally and in CI only.
- Signed provenance attestations (cosign / SLSA) — no signing workflow exists.
- Promotion of the CI evidence releases from draft to published — the retention
  step creates drafts by design.
