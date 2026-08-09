# Supply Chain and SBOM

>
> **V2 · PHASE 9 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires `HYBRID_ACCEPTED` and all Phase 7 integrations in the production cut
> accepted. Nothing here declares production readiness.

| Control | Requirement |
|---|---|
| SBOM | Generated per release artifact (CycloneDX or SPDX), retained, hashed and indexed with the release |
| Dependency pinning | Fully pinned with hashes; no floating ranges in the release lock |
| Vulnerability scan | Dependency scan executed with a recorded database version; unresolved High/Critical requires an explicit, time-boxed, recorded exception |
| Scanner availability | A scan that could not fetch its database is `scanned=false` and does **not** satisfy the control |
| Image scan | If an image ships: base image pinned by digest, non-root user, minimal packages, scan recorded |
| Provenance | Build provenance recorded: source commit SHA, builder identity, build inputs; artifacts referenced by immutable digest, never by mutable tag |
| Reproducibility | Two builds of the same commit produce identical dependency sets; artifact digest differences must be explainable |
| Third-party plugins | Not supported in V2; the provider allow-list is in-repo (ADR-0024) |
| License review | Recorded from the SBOM; incompatible licenses block the cut |

Retention: SBOM, scan reports and provenance are retained alongside the
acceptance evidence with SHA-256 digests in the evidence index.
