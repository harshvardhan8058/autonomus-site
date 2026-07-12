"""In-memory, per-IP sliding-window rate limiter for the Autonomous Agent Service.

This module defines :class:`RateLimiter`, the component the API layer consults
before processing a request, and :class:`RateDecision`, the small value object
it returns. The limiter enforces a per-client-IP sliding window: while the
number of requests from a single IP within the trailing window is at or above
the configured limit, further requests from that IP are denied so the API layer
can respond with HTTP 429 and a ``Retry-After`` header (Req 1.6).

The ``retry_after`` value is **best-effort**: it is the number of seconds until
the oldest in-window hit ages out of the window. If it cannot be computed the
request is still denied, so the API layer always rejects an over-limit request
even when it cannot produce a ``Retry-After`` header value (Req 1.7).

The limiter uses a monotonic clock by default and accepts an injectable time
function so that the sliding-window behavior can be tested deterministically.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RateDecision:
    """The outcome of a single :meth:`RateLimiter.check` call.

    Attributes:
        allowed: ``True`` when the request is within the limit and may proceed;
            ``False`` when it is at or over the limit and must be rejected with
            HTTP 429 (Req 1.6, 1.7).
        retry_after: Best-effort number of seconds until the oldest in-window
            hit ages out of the trailing window, or ``None`` when it cannot be
            computed. When ``allowed`` is ``True`` this is always ``None``.
    """

    allowed: bool
    retry_after: float | None


class RateLimiter:
    """Per-IP sliding-window rate limiter backed by timestamp deques (Req 1.6).

    For each client IP the limiter keeps a :class:`collections.deque` of the
    monotonic timestamps at which its allowed requests occurred. On each
    :meth:`check`, timestamps older than the trailing window are evicted; if the
    number of remaining in-window hits is at or above ``limit`` the request is
    denied, otherwise the new hit is recorded and the request is allowed.

    The clock is injectable to make the sliding-window behavior deterministically
    testable; by default it is :func:`time.monotonic`, which never moves
    backwards.

    Attributes:
        _limit: Maximum number of in-window requests allowed per client IP.
        _window_seconds: Size of the trailing sliding window in seconds.
        _time_fn: Zero-argument callable returning the current time in seconds.
        _hits: Mapping of client IP to a deque of that IP's in-window hit
            timestamps.
    """

    def __init__(
        self,
        limit: int = 10,
        window_seconds: int = 60,
        *,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the rate limiter.

        Args:
            limit: Maximum number of requests allowed from a single client IP
                within the trailing window. Defaults to ``10`` (Req 1.6).
            window_seconds: Size of the trailing sliding window in seconds.
                Defaults to ``60`` (Req 1.6).
            time_fn: Zero-argument callable returning the current time in
                seconds. Defaults to :func:`time.monotonic` so the window is
                measured against a monotonic clock; a custom function may be
                injected for deterministic testing.
        """

        self._limit = limit
        self._window_seconds = window_seconds
        self._time_fn = time_fn
        self._hits: dict[str, deque[float]] = {}

    def check(self, client_ip: str) -> RateDecision:
        """Register and evaluate a request from ``client_ip`` (Req 1.6, 1.7).

        Evicts timestamps older than the trailing window, then decides:

        - If the count of in-window hits is at or above ``limit``, the request
          is **denied** (``allowed=False``). A best-effort ``retry_after`` is
          computed as the seconds until the oldest in-window hit ages out; if it
          cannot be computed, ``retry_after`` is ``None`` but the request is
          still denied (Req 1.7).
        - Otherwise the new hit timestamp is recorded and the request is
          **allowed** (``allowed=True``, ``retry_after=None``).

        Args:
            client_ip: The IP address of the requesting client.

        Returns:
            A :class:`RateDecision` describing whether the request is allowed
            and, when denied, the best-effort retry-after delay.
        """

        now = self._time_fn()
        hits = self._hits.get(client_ip)
        if hits is None:
            hits = deque()
            self._hits[client_ip] = hits

        self._evict_stale(hits, now)

        if len(hits) >= self._limit:
            return RateDecision(allowed=False, retry_after=self._retry_after(hits, now))

        hits.append(now)
        return RateDecision(allowed=True, retry_after=None)

    def _evict_stale(self, hits: deque[float], now: float) -> None:
        """Drop timestamps that have aged out of the trailing window.

        A timestamp ``ts`` is in-window when ``now - ts < window_seconds`` and is
        evicted otherwise. Because timestamps are appended in non-decreasing
        order, stale entries are always at the left of the deque.

        Args:
            hits: The deque of hit timestamps for a client IP (mutated in place).
            now: The current time in seconds.
        """

        cutoff = now - self._window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

    def _retry_after(self, hits: deque[float], now: float) -> float | None:
        """Compute the best-effort seconds until the oldest hit ages out.

        Args:
            hits: The deque of in-window hit timestamps for a client IP.
            now: The current time in seconds.

        Returns:
            The number of seconds until the oldest in-window hit leaves the
            window (always positive for an in-window hit), or ``None`` when it
            cannot be computed (for example when there is no recorded hit).
        """

        try:
            oldest = hits[0]
        except IndexError:
            return None
        retry_after = (oldest + self._window_seconds) - now
        if retry_after <= 0:
            return None
        return retry_after
