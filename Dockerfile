# Base image pinned by digest (BLOCO 6A, 0.9.0).
#
# python:3.12-slim-trixie was selected over 3.11-slim-bookworm (CVE reduction:
# 6 CRITICAL / 20 HIGH -> 4 CRITICAL / 19 HIGH, scan without --ignore-unfixed)
# and over 3.13-slim-trixie (conservatism: identical container test results,
# longer field record for 3.12). 3.11/3.12/3.13 all pass the same container
# tests; host-tooling tests requiring systemd are deliberately not run here.
#
# See docs/base-image-security-0.9.0.md for the full CVE matrix and the
# rejection rationale for the other candidate bases.
ARG BASE_IMAGE=python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

# Build provenance is allow-listed and non-sensitive. CI and controlled 1.x
# builds MUST override revision/build-id/created. Defaults keep local developer
# builds possible but make them visibly non-promotable by the provenance gate.
ARG OCI_IMAGE_SOURCE=https://github.com/pestoura/hermes-mcp-bridge
ARG OCI_IMAGE_REVISION=unknown
ARG OCI_IMAGE_VERSION=1.0.0
ARG OCI_IMAGE_CREATED=unknown
ARG BRIDGE_BUILD_ID=unknown
ARG BRIDGE_SCHEMA_VERSION=0.6.1
ARG BRIDGE_CONTRACT_VERSION=1.0.0

FROM ${BASE_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM ${BASE_IMAGE} AS runtime

ARG OCI_IMAGE_SOURCE
ARG OCI_IMAGE_REVISION
ARG OCI_IMAGE_VERSION
ARG OCI_IMAGE_CREATED
ARG BRIDGE_BUILD_ID
ARG BRIDGE_SCHEMA_VERSION
ARG BRIDGE_CONTRACT_VERSION

LABEL org.opencontainers.image.source="${OCI_IMAGE_SOURCE}" \
      org.opencontainers.image.revision="${OCI_IMAGE_REVISION}" \
      org.opencontainers.image.version="${OCI_IMAGE_VERSION}" \
      org.opencontainers.image.created="${OCI_IMAGE_CREATED}" \
      io.jarvas.hermes-mcp-bridge.build-id="${BRIDGE_BUILD_ID}" \
      io.jarvas.hermes-mcp-bridge.schema-version="${BRIDGE_SCHEMA_VERSION}" \
      io.jarvas.hermes-mcp-bridge.contract-version="${BRIDGE_CONTRACT_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Runtime dependencies only: TLS trust store for the upstream HTTPS/loopback
# client. libsqlite3 ships with the base image and is required by the stdlib
# sqlite3 module used by the state registry. No systemd, no build tooling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates \
    && apt-get purge -y --auto-remove \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 bridge \
    && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin bridge \
    && mkdir -p /var/lib/hermes-mcp-bridge \
    && chown -R bridge:bridge /var/lib/hermes-mcp-bridge

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY --chown=bridge:bridge README.md ./

RUN python -m pip install --no-cache-dir --no-index /wheels/*.whl \
    && rm -rf /wheels \
    && find /usr/local -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && rm -rf /root/.cache

USER bridge:bridge

CMD ["python", "-m", "hermes_mcp_bridge.http_runner"]
