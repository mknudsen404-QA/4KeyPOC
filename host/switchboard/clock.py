"""Time as an injected dependency.

Nothing in this package should call time.time()/time.monotonic() directly
(other than SystemClock itself) — every caller that needs "now" receives a
Clock so tests can control time exactly instead of racing the real clock.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...  # wall-clock seconds, like time.time()

    def monotonic(self) -> float: ...  # like time.monotonic()


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Deterministic clock for tests. now() and monotonic() move together."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds
