"""SSE stream fault injection: truncation, invalid, duplicate, out-of-order."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


def _frame(payload: object) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


@dataclass
class SSEScript:
    """Builder for deterministic ``text/event-stream`` bodies."""

    frames: list[str] = field(default_factory=list)

    def event(self, event: str, run_id: str, **extra: object) -> SSEScript:
        payload: dict[str, object] = {"event": event, "run_id": run_id}
        payload.update(extra)
        self.frames.append(_frame(payload))
        return self

    def raw(self, text: str) -> SSEScript:
        self.frames.append(text)
        return self

    def invalid_json(self) -> SSEScript:
        self.frames.append("data: {not json\n\n")
        return self

    def comment(self, text: str = "keep-alive") -> SSEScript:
        self.frames.append(f": {text}\n\n")
        return self

    def build(self, *, truncate_at: int | None = None) -> str:
        body = "".join(self.frames)
        if truncate_at is not None:
            return body[:truncate_at]
        return body


def truncated_stream(run_id: str, *, keep_chars: int = 40) -> str:
    """A stream cut mid-frame, so the last event is unparseable."""

    script = (
        SSEScript()
        .event("run.started", run_id)
        .event("tool.started", run_id, tool="x")
        .event("run.completed", run_id, status="completed")
    )
    return script.build(truncate_at=keep_chars)


def invalid_event_stream(run_id: str) -> str:
    """A stream containing malformed and unknown frames around a valid one."""

    return (
        SSEScript()
        .invalid_json()
        .comment()
        .raw("data: 42\n\n")
        .event("weird.custom", run_id)
        .event("run.completed", run_id, status="completed")
        .build()
    )


def duplicated_events(run_id: str, *, repeats: int = 3) -> str:
    """The same terminal event repeated ``repeats`` times."""

    script = SSEScript().event("run.started", run_id)
    for _ in range(repeats):
        script.event("run.completed", run_id, status="completed")
    return script.build()


def out_of_order_events(run_id: str) -> str:
    """A terminal event followed by earlier lifecycle events."""

    return (
        SSEScript()
        .event("run.completed", run_id, status="completed")
        .event("run.started", run_id)
        .event("tool.started", run_id, tool="x")
        .build()
    )


def replay(frames: Sequence[str]) -> Iterable[str]:
    """Yield frames one at a time (helper for streaming transports)."""

    yield from frames
