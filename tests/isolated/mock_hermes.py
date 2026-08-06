#!/usr/bin/env python3
"""Minimal read-only Hermes API mock for isolated container acceptance.

The mock intentionally implements only health and capability discovery. Any
mutation or unknown route is rejected and recorded as a bounded JSON event.
No request header or body is logged.
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HOST = "0.0.0.0"
PORT = int(os.environ.get("MOCK_HERMES_PORT", "8642"))
EXPECTED_TOKEN = os.environ.get("MOCK_HERMES_TOKEN", "isolated-api-key-not-secret")

ALLOWED_GET_PATHS = frozenset({"/health", "/health/detailed", "/v1/capabilities"})


def _emit(event: dict[str, Any]) -> None:
    print(json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)


class Handler(BaseHTTPRequestHandler):
    """Serve a finite read-only API and reject every mutation."""

    server_version = "HermesIsolatedMock/1.0"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _authorized(self) -> bool:
        if self.path == "/health" and self.headers.get("Authorization") is None:
            return True
        return self.headers.get("Authorization") == f"Bearer {EXPECTED_TOKEN}"

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        _emit(
            {
                "event": "mock.request",
                "method": "GET",
                "path_class": self.path
                if self.path in ALLOWED_GET_PATHS
                else "unknown",
                "authorized": self._authorized(),
            }
        )
        if not self._authorized():
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": {"message": "unauthorized"}},
            )
            return
        if self.path in {"/health", "/health/detailed"}:
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "active_api_runs": 0,
                    "mode": "isolated-read-only",
                },
            )
            return
        if self.path == "/v1/capabilities":
            self._json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "canonical": {
                        "source": "isolated-hermes-mock",
                        "read_only": True,
                    },
                    "run_submission": False,
                    "run_status": True,
                    "events": False,
                    "stop": False,
                    "sessions": False,
                },
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def _reject_mutation(self) -> None:
        _emit(
            {
                "event": "mock.mutation_rejected",
                "method": self.command,
                "path_class": "mutation",
            }
        )
        self._json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": {"message": "isolated mock is read-only"}},
        )

    do_POST = _reject_mutation  # type: ignore[assignment]
    do_PUT = _reject_mutation  # type: ignore[assignment]
    do_PATCH = _reject_mutation  # type: ignore[assignment]
    do_DELETE = _reject_mutation  # type: ignore[assignment]


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    _emit({"event": "mock.ready", "port": PORT, "read_only": True})
    server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
