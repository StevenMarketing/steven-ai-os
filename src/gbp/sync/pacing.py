"""Token-bucket pacing for the live GBP read path (Issue #110).

Google's default Business Profile quota is 300 queries per minute; this bucket
is deliberately well under it (``DEFAULT_QPM = 240``) so a burst plus the
retry/backoff in :class:`~gbp.client.http_client.HttpClient` never trips the
limit. Both the clock and the sleep are injectable, so tests drive pacing
directly and never really wait.

The class exposes ``acquire()`` -- the same one-method contract
:class:`~gbp.client.quota.QuotaLimiter` offers -- so it can be handed to
:class:`~gbp.client.gbp_client.GbpClient` unchanged.

Standard library only, no network.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

__all__ = ["TokenBucket", "DEFAULT_QPM"]

#: Comfortably below the 300 QPM default, leaving headroom for retries.
DEFAULT_QPM = 240


class TokenBucket:
    """Classic token bucket: ``qpm`` tokens per minute, burst = one minute."""

    def __init__(self, qpm: int = DEFAULT_QPM, *, burst: Optional[int] = None,
                 clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None) -> None:
        if qpm < 1:
            raise ValueError("qpm must be >= 1")
        if qpm >= 300:
            raise ValueError("qpm must stay well under Google's 300 QPM default")
        self.qpm = int(qpm)
        self.capacity = float(burst if burst is not None else qpm)
        self._rate = self.qpm / 60.0  # tokens per second
        self._clock: Callable[[], float] = clock or time.monotonic
        self._sleep: Callable[[float], None] = sleep or time.sleep
        self._tokens = self.capacity
        self._last = self._clock()
        #: Number of calls that actually passed the bucket.
        self.calls = 0
        #: Total seconds the bucket asked the caller to wait (test introspection).
        self.waited = 0.0

    def _refill(self, now: float) -> None:
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now

    def would_wait(self) -> bool:
        """True when :meth:`acquire` would have to wait for a token."""
        return self._tokens < 1.0

    def acquire(self) -> None:
        """Take one token, sleeping via the injected sleep when none is free."""
        for _ in range(1000):  # bounded: a misbehaving clock cannot hang us
            now = self._clock()
            self._refill(now)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self.calls += 1
                return
            wait = (1.0 - self._tokens) / self._rate
            self.waited += wait
            self._sleep(wait)
        raise RuntimeError("TokenBucket.acquire could not obtain a token")
