"""Loopback-only Prometheus exporter.

Security posture:

* Disabled by default (``BRIDGE_METRICS_ENABLED=1`` to enable).
* Binds to ``127.0.0.1`` by default. Binding to a non-loopback address is
  refused unless ``BRIDGE_METRICS_ALLOW_REMOTE=1`` is set *and* a bearer token
  is configured via ``BRIDGE_METRICS_TOKEN`` — loopback alone is never treated
  as sufficient authorization for remote exposure.
* Only ``GET /metrics`` and ``GET /healthz`` are served; everything else is 404.
* The handler is cheap: it renders the in-memory registry, does no I/O and no
  database access.
"""

from __future__ import annotations

import ipaddress
import os
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .metrics import CONTENT_TYPE, get_registry

ENV_ENABLED = "BRIDGE_METRICS_ENABLED"
ENV_HOST = "BRIDGE_METRICS_HOST"
ENV_PORT = "BRIDGE_METRICS_PORT"
ENV_ALLOW_REMOTE = "BRIDGE_METRICS_ALLOW_REMOTE"
ENV_TOKEN = "BRIDGE_METRICS_TOKEN"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9464


class MetricsExporterError(RuntimeError):
    """Raised when the exporter configuration is unsafe."""


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def metrics_enabled() -> bool:
    return _truthy(ENV_ENABLED)


def exporter_host() -> str:
    return os.environ.get(ENV_HOST, DEFAULT_HOST).strip() or DEFAULT_HOST


def exporter_port() -> int:
    raw = os.environ.get(ENV_PORT, "").strip()
    try:
        port = int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in {"localhost", "localhost.localdomain"}


def validate_binding(host: str) -> None:
    """Fail closed when a remote binding is not explicitly and safely enabled."""

    if is_loopback(host):
        return
    if not _truthy(ENV_ALLOW_REMOTE):
        raise MetricsExporterError(
            "remote metrics binding refused: set BRIDGE_METRICS_ALLOW_REMOTE=1 "
            "and BRIDGE_METRICS_TOKEN, and front it with TLS/authz"
        )
    if not os.environ.get(ENV_TOKEN, "").strip():
        raise MetricsExporterError(
            "remote metrics binding refused: BRIDGE_METRICS_TOKEN is required"
        )


def _authorized(header: str | None) -> bool:
    token = os.environ.get(ENV_TOKEN, "").strip()
    if not token:
        # No token configured: only reachable because binding validation
        # already restricted us to loopback.
        return True
    if not header or not header.lower().startswith("bearer "):
        return False
    import hmac

    return hmac.compare_digest(header.split(" ", 1)[1].strip(), token)


class MetricsHandler(BaseHTTPRequestHandler):
    server_version = "hermes-mcp-bridge-metrics"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        return None  # never log request lines (may contain identifiers)

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/healthz":
                self._respond(200, b"ok\n", "text/plain; charset=utf-8")
                return
            if path != "/metrics":
                self._respond(404, b"not found\n", "text/plain; charset=utf-8")
                return
            if not _authorized(self.headers.get("Authorization")):
                self._respond(401, b"unauthorized\n", "text/plain; charset=utf-8")
                return
            body = get_registry().render().encode("utf-8")
            self._respond(200, body, CONTENT_TYPE)
        except Exception:  # pragma: no cover - exporter must not crash the bridge
            with suppress(Exception):
                self._respond(500, b"error\n", "text/plain; charset=utf-8")


class MetricsExporter:
    """Background HTTP exporter with an explicit lifecycle."""

    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        self.host = host or exporter_host()
        self.port = exporter_port() if port is None else port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> MetricsExporter:
        validate_binding(self.host)
        server = ThreadingHTTPServer((self.host, self.port), MetricsHandler)
        server.daemon_threads = True
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(
            target=server.serve_forever, name="bridge-metrics", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # pragma: no cover
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None


_exporter: MetricsExporter | None = None


def start_exporter_if_enabled() -> MetricsExporter | None:
    """Start the exporter when enabled; never raises on failure."""

    global _exporter
    if not metrics_enabled() or _exporter is not None:
        return _exporter
    try:
        _exporter = MetricsExporter().start()
    except Exception:
        _exporter = None
    return _exporter


def exporter_status() -> dict[str, Any]:
    """Non-sensitive exporter status for health output."""

    host = exporter_host()
    return {
        "enabled": metrics_enabled(),
        "running": _exporter is not None and _exporter.running,
        "exporter": "prometheus-text",
        "bind_scope": "loopback" if is_loopback(host) else "remote",
        "auth_required": bool(os.environ.get(ENV_TOKEN, "").strip()),
        "path": "/metrics",
    }
