"""HTTP fault injection via an httpx transport (no monkeypatching)."""

from __future__ import annotations

import json
import random
from collections.abc import Iterable
from dataclasses import dataclass, field

import httpx


@dataclass(frozen=True)
class ScriptedResponse:
    """One scripted upstream reply, or a scripted transport-level failure."""

    status_code: int = 200
    json_body: dict | None = None
    text_body: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    #: ``timeout``, ``reset`` or ``None`` for a normal response.
    failure: str | None = None

    def build(self, request: httpx.Request) -> httpx.Response:
        if self.failure == "timeout":
            raise httpx.ReadTimeout("injected timeout", request=request)
        if self.failure == "reset":
            raise httpx.ReadError("injected connection reset", request=request)
        if self.failure == "connect":
            raise httpx.ConnectError("injected connect failure", request=request)
        if self.text_body is not None:
            return httpx.Response(
                self.status_code, text=self.text_body, headers=self.headers, request=request
            )
        return httpx.Response(
            self.status_code,
            json=self.json_body if self.json_body is not None else {},
            headers=self.headers,
            request=request,
        )


@dataclass
class FaultProfile:
    """Probabilistic fault profile, driven by a seeded RNG."""

    seed: int = 1337
    timeout_rate: float = 0.0
    reset_rate: float = 0.0
    status_429_rate: float = 0.0
    status_500_rate: float = 0.0
    status_502_rate: float = 0.0
    status_503_rate: float = 0.0
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        total = (
            self.timeout_rate
            + self.reset_rate
            + self.status_429_rate
            + self.status_500_rate
            + self.status_502_rate
            + self.status_503_rate
        )
        if total > 1.0:
            raise ValueError("fault rates must sum to <= 1.0")
        self._rng = random.Random(self.seed)

    def next_fault(self) -> ScriptedResponse | None:
        draw = self._rng.random()
        cursor = 0.0
        for rate, response in (
            (self.timeout_rate, ScriptedResponse(failure="timeout")),
            (self.reset_rate, ScriptedResponse(failure="reset")),
            (self.status_429_rate, self._status(429)),
            (self.status_500_rate, self._status(500)),
            (self.status_502_rate, self._status(502)),
            (self.status_503_rate, self._status(503)),
        ):
            cursor += rate
            if draw < cursor:
                return response
        return None

    def _status(self, code: int) -> ScriptedResponse:
        headers: dict[str, str] = {}
        if code in {429, 503} and self.retry_after_seconds is not None:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return ScriptedResponse(
            status_code=code,
            json_body={"error": {"message": f"injected HTTP {code}"}},
            headers=headers,
        )


class FaultyTransport(httpx.AsyncBaseTransport):
    """Async transport that replays a script and/or injects profiled faults.

    ``script`` entries are consumed in order; once exhausted, ``default`` is
    returned. When a :class:`FaultProfile` is supplied it is consulted first,
    so faults can be layered over an otherwise healthy script.
    """

    def __init__(
        self,
        *,
        script: Iterable[ScriptedResponse] | None = None,
        default: ScriptedResponse | None = None,
        profile: FaultProfile | None = None,
    ) -> None:
        self._script = list(script or [])
        self._default = default or ScriptedResponse(status_code=200, json_body={})
        self._profile = profile
        self.requests: list[tuple[str, str]] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def calls_to(self, fragment: str) -> int:
        return sum(1 for _, path in self.requests if fragment in path)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self._profile is not None:
            injected = self._profile.next_fault()
            if injected is not None:
                return injected.build(request)
        if self._script:
            return self._script.pop(0).build(request)
        return self._default.build(request)


def sse_response(body: str, *, status_code: int = 200) -> ScriptedResponse:
    """Build a scripted ``text/event-stream`` reply."""

    return ScriptedResponse(
        status_code=status_code,
        text_body=body,
        headers={"Content-Type": "text/event-stream"},
    )


def run_payload(run_id: str, status: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {"run_id": run_id, "status": status}
    payload.update(extra)
    return payload


def json_line(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))
