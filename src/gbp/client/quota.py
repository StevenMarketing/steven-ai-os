"""In-process queries-per-minute limiter (offline, stdlib only).

This is a deliberately simple sliding-window limiter: it keeps the timestamps
of the last calls inside a 60-second window and blocks (via an injectable
``sleep``) until the oldest one ages out. The clock and the sleep function are
both injectable, so tests never really wait.

It is *not* a distributed limiter -- it only protects a single process. That is
enough to stay under the Google Business Profile default quota (300 QPM).
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional

__all__ = ["QuotaLimiter"]

WINDOW_SECONDS = 60.0


class QuotaLimiter:
    """Sliding-window QPM limiter.

    Parameters
    ----------
    qpm:
        Maximum calls allowed per 60-second window.
    clock:
        Injectable ``() -> float`` monotonic clock source.
    sleep:
        Injectable ``(seconds) -> None`` blocking sleep.
    """

    def __init__(
        self,
        qpm: int = 300,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        if qpm < 1:
            raise ValueError("qpm must be >= 1")
        self.qpm = int(qpm)
        self._clock: Callable[[], float] = clock or time.monotonic
        self._sleep: Callable[[float], None] = sleep or time.sleep
        self._times: List[float] = []
        #: Number of calls that actually passed the limiter.
        self.calls = 0

    # -- internals --------------------------------------------------------- #
    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        self._times = [t for t in self._times if t > cutoff]

    # -- public API -------------------------------------------------------- #
    def would_wait(self) -> bool:
        """True when :meth:`acquire` would have to wait for a free slot."""
        now = self._clock()
        self._prune(now)
        return len(self._times) >= self.qpm

    def acquire(self) -> None:
        """Reserve one call, sleeping (via the injected sleep) when required."""
        for _ in range(1000):  # bounded: a misbehaving clock cannot hang us
            now = self._clock()
            self._prune(now)
            if len(self._times) < self.qpm:
                self._times.append(now)
                self.calls += 1
                return
            wait = self._times[0] + WINDOW_SECONDS - now
            if wait <= 0:
                continue
            self._sleep(wait)
        raise RuntimeError("QuotaLimiter.acquire could not obtain a slot")

    def reset(self) -> None:
        """Forget the recorded window (does not reset :attr:`calls`)."""
        self._times = []
