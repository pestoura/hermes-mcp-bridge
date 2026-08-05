# Base image security decision — 0.9.0

Status: decided. Applies to `Dockerfile` and every image built from this repo
at 0.9.0 and later.

## Decision

The container base image moves from the floating tag
`python:3.11-slim-bookworm` to

```
python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
```

pinned **by digest** and referenced through a single `ARG BASE_IMAGE`, so the
builder and runtime stages can never drift apart.

CI builds and tests the package on both Python `3.11` and `3.12` via a
`strategy.matrix` with `fail-fast: false`, so a base-image bump can never
silently drop a supported interpreter from the test gate.

Nothing else in the release changes behaviour: the tool contract stays at 27
tools with `hermes_readiness` mandatory, the wire schema stays `0.6.1`, and
there is no new SQLite migration.

## Why a digest and not a tag

A tag is mutable. `python:3.11-slim-bookworm` resolved to a different image on
every rebuild, which is exactly how CVE counts drifted between builds without
any change in this repository. A digest makes the base immutable and the scan
result reproducible. `tests/test_base_image_0_9_0.py` fails if any `python:`
reference in the Dockerfile loses its `@sha256:` suffix.

## Candidate evaluation

All three candidates were built and exercised with the container test suite.

| Candidate | Tests | Result |
| --- | --- | --- |
| `python:3.11-slim-bookworm` | 584 | 584 pass, 3 host-only failures |
| `python:3.12-slim-trixie` | 584 | 584 pass, 3 host-only failures |
| `python:3.13-slim-trixie` | 584 | 584 pass, 3 host-only failures |

The three failures are identical across all bases and are **not** base-image
related: they exercise `systemctl` / `systemd-run`, which are host operational
tooling for the secret-rotation flow and are deliberately absent from the
image. They are reported, not hidden, and they remain failing in the normal
host gates.

### CVE matrix

Scanner: Trivy, `--scanners vuln --severity CRITICAL,HIGH`, **without**
`--ignore-unfixed` (i.e. counting unfixable OS CVEs too, the pessimistic view).

| Image | CRITICAL | HIGH |
| --- | --- | --- |
| 0.8.2 baseline (`python:3.11-slim-bookworm`) | 6 | 20 |
| **0.9.0 (`python:3.12-slim-trixie`, pinned)** | **4** | **19** |

Net: −2 CRITICAL, −1 HIGH (26 findings → 23), with no functional change.
Baseline and candidate were scanned with identical flags; a flag mismatch
between a stored baseline and a fresh scan previously produced a false
regression report and is not repeated here.

With `--ignore-unfixed` (the actionable view, i.e. only CVEs with a fix
available), the baseline reports 2 HIGH and the 0.9.0 candidate reports **0**.
Everything remaining on the candidate is unfixable upstream OS surface.

#### How to read these numbers: CVE **ID** vs CVE **finding**

Trivy counts *findings*, i.e. one row per `(CVE ID, package)` pair. The same
CVE ID appears as several findings when the distribution splits or renames the
binary packages built from one source package. So the counts above are finding
counts, not distinct-CVE counts, and the two must never be compared with each
other.

Checked at the CVE-**ID** level, the candidate introduces **zero new CVE IDs**
relative to the baseline: the set of distinct CVE IDs on 0.9.0 is a subset of
the baseline's. Concretely, the `liblastlog2` and `login` findings that are
absent from the baseline row list are **not** a new vulnerability — they are
the same `CVE-2026-53615` (already present in the baseline via the `util-linux`
source package) re-attributed by Trixie to the split-out binary packages. A
naive per-row diff shows them as "new"; a diff over CVE IDs correctly shows no
new exposure.

The rule applied here: a release is a regression only if it introduces a CVE
**ID** that the baseline did not carry, or increases the count of *fixable*
findings. Neither happens in 0.9.0.

### SBOM

The image SBOM grows from 126 to 171 packages. This is Trixie's finer package
granularity (the same software split across more binary packages, e.g. the
`util-linux` split above), not additional software: no new runtime dependency
is installed by this release, and the runtime stage still installs only
`ca-certificates` on top of the base.

### Rejected alternatives

- **`python:3.11-slim-bookworm` (status quo).** Bookworm carries the larger
  CVE set (6C/20H) and 3.11 is the oldest supported interpreter. Rejected:
  keeping it means keeping known CRITICALs for no benefit.
- **`python:3.13-slim-trixie`.** Test results are identical to 3.12, so it
  offers no measurable advantage today, while 3.13 has the shortest field
  record and the weakest wheel/ecosystem coverage of the three. Rejected on
  conservatism for a release whose entire purpose is risk reduction.
- **`python:3.12-alpine`.** musl instead of glibc changes the wheel resolution
  path for `pydantic-core` and would force source builds in the runtime stage.
  Rejected: a build-toolchain in the runtime image is a worse trade than the
  remaining OS CVEs.
- **Distroless.** No shell and no `apt`, which breaks the container healthcheck
  invocation pattern and the current debugging workflow. Rejected as out of
  scope for a patch-shaped security release; revisit separately.

## Hardening carried by the image

- Multi-stage build: `build-essential` exists only in the builder; the runtime
  stage installs prebuilt wheels with `--no-index`.
- `apt-get install --no-install-recommends`, then `apt-get clean` and
  `rm -rf /var/lib/apt/lists/*` in every layer that installs.
- `ca-certificates` + `update-ca-certificates` in the runtime stage so outbound
  TLS validates; SQLite comes from the stdlib against the base image library.
- Dedicated non-root `bridge` user and group (UID/GID 1000);
  `/var/lib/hermes-mcp-bridge` created and chowned before the privilege drop;
  `USER bridge:bridge` is the last instruction before `CMD`.
- No systemd, no dbus, no init-system-helpers.
- `__pycache__` and `/root/.cache` removed from the final layer.

Compose-level hardening is unchanged: `read_only: true`, `cap_drop: ALL`,
`no-new-privileges:true`, tmpfs `/tmp`, and the state volume mount.

## Verification

Static, host-runnable:

```bash
PYTHONPATH=src python -m pytest -q tests/test_base_image_0_9_0.py tests/test_packaging.py
```

Container-level (not part of this patch run): build the image, re-run the
container suite, and re-scan with the flags above before promoting.
