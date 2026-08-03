FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd -g 1000 bridge \
    && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin bridge \
    && mkdir -p /var/lib/hermes-mcp-bridge \
    && chown -R bridge:bridge /var/lib/hermes-mcp-bridge

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY --chown=bridge:bridge README.md ./

RUN python -m pip install --no-cache-dir --no-index /wheels/*.whl \
    && rm -rf /wheels

USER bridge:bridge

CMD ["python", "-m", "hermes_mcp_bridge.server"]
