"""Bounded exponential backoff with deterministic, seedable jitter."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

#: Never wait longer than this regardless of configuration or Retry-After.
MAX_SLEEP_SECONDS = 300.0
#: Retry-After values above this are treated as invalid (clamped, not honoured).
MAX_RETRY_AFTER_SECONDS = 300.0


@dataclass(frozen=True)
class BackoffPolicy:
    """Bounded exponential backoff.

    ``jitter_ratio`` is the fraction of the base delay that may be subtracted
    (full-jitter is intentionally avoided so the schedule stays monotonic and
    assertable). With ``jitter_ratio=0`` the schedule is fully deterministic.
    """

    base_seconds: float = 0.5
    multiplier: float = 2.0
    max_seconds: float = 30.0
    max_attempts: int = 5
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.base_seconds <= 0:
            raise ValueError("base_seconds must be > 0")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if self.max_seconds <= 0 or self.max_seconds > MAX_SLEEP_SECONDS:
            raise ValueError(f"max_seconds must be in (0, {MAX_SLEEP_SECONDS}]")
        if self.max_attempts < 1 or self.max_attempts > 20:
            raise ValueError("max_attempts must be in [1, 20]")
        if not (0.0 <= self.jitter_ratio <= 1.0):
            raise ValueError("jitter_ratio must be in [0, 1]")

    def base_delay(self, attempt: int) -> float:
        """Un-jittered delay for a 1-based attempt number, bounded."""

        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        raw = self.base_seconds * (self.multiplier ** (attempt - 1))
        return min(raw, self.max_seconds, MAX_SLEEP_SECONDS)

    def delay(
        self,
        attempt: int,
        *,
        rng: random.Random | None = None,
        retry_after: float | None = None,
    ) -> float:
        """Delay for ``attempt``, honouring a validated ``Retry-After``.

        A valid ``retry_after`` wins over the computed backoff but is still
        bounded by :data:`MAX_SLEEP_SECONDS`.
        """

        base = self.base_delay(attempt)
        if self.jitter_ratio and rng is not None:
            base -= base * self.jitter_ratio * rng.random()
        base = max(0.0, base)
        if retry_after is not None and retry_after >= 0:
            base = max(base, min(retry_after, MAX_RETRY_AFTER_SECONDS))
        return min(base, MAX_SLEEP_SECONDS)

    def schedule(self, *, rng: random.Random | None = None) -> list[float]:
        """Full delay schedule for the configured attempts (test/doc helper)."""

        return [self.delay(i, rng=rng) for i in range(1, self.max_attempts + 1)]


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header into bounded seconds.

    Returns ``None`` when the header is absent, malformed, negative or beyond
    :data:`MAX_RETRY_AFTER_SECONDS`. Both delta-seconds and HTTP-date forms are
    supported.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(int(text))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        reference = now or datetime.now(UTC)
        seconds = (parsed - reference).total_seconds()
    if seconds < 0:
        return None
    if seconds > MAX_RETRY_AFTER_SECONDS:
        return None
    return seconds
