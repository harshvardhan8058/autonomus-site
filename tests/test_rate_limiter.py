"""Tests for the per-IP sliding-window rate limiter (`app.core.rate_limiter`).

Covers:
- Task 5.1 (unit): concrete sliding-window behavior, aging-out, per-IP
  isolation, and the best-effort ``retry_after`` contract including the case
  where it cannot be computed yet the request is still denied (Req 1.6, 1.7).
- Task 5.2 / Property 16: a Hypothesis property test asserting the limiter's
  decisions match an independent reference model of the trailing window for any
  sequence of request timestamps from a single IP, and that a denial still
  occurs even when a ``retry_after`` value cannot be computed (Req 1.6, 1.7).

All property tests use Hypothesis with a minimum of 100 iterations and an
injectable clock so the sliding window is exercised deterministically.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.rate_limiter import RateDecision, RateLimiter


class _Clock:
    """A deterministic, injectable monotonic clock for tests.

    Returns whatever value was last set via :meth:`set`, so a test can advance
    time explicitly and drive the limiter's sliding window deterministically.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def set(self, value: float) -> None:
        """Set the current time the clock reports."""

        self.now = value

    def __call__(self) -> float:
        """Return the current time in seconds."""

        return self.now


# --- Task 5.1: unit tests ---------------------------------------------------


def test_allows_up_to_limit_then_denies_at_limit() -> None:
    """At the same instant, the first ``limit`` requests pass then denial begins."""

    clock = _Clock(1000.0)
    limiter = RateLimiter(limit=3, window_seconds=60, time_fn=clock)

    assert limiter.check("1.1.1.1").allowed is True
    assert limiter.check("1.1.1.1").allowed is True
    assert limiter.check("1.1.1.1").allowed is True

    denied = limiter.check("1.1.1.1")
    assert denied.allowed is False
    assert denied.retry_after is not None
    assert denied.retry_after > 0


def test_requests_allowed_again_once_older_hits_age_out() -> None:
    """After the window passes, aged-out hits free capacity again (Req 1.6)."""

    clock = _Clock(0.0)
    limiter = RateLimiter(limit=2, window_seconds=60, time_fn=clock)

    assert limiter.check("ip").allowed is True   # t=0
    clock.set(10.0)
    assert limiter.check("ip").allowed is True   # t=10
    clock.set(20.0)
    assert limiter.check("ip").allowed is False  # t=20, two in-window hits

    # Advance past the window so the t=0 and t=10 hits age out.
    clock.set(75.0)
    assert limiter.check("ip").allowed is True   # only the t=20-era window matters


def test_retry_after_reflects_oldest_hit_ageout() -> None:
    """retry_after equals seconds until the oldest in-window hit ages out."""

    clock = _Clock(100.0)
    limiter = RateLimiter(limit=1, window_seconds=60, time_fn=clock)

    assert limiter.check("ip").allowed is True   # oldest hit at t=100
    clock.set(130.0)
    denied = limiter.check("ip")
    assert denied.allowed is False
    # Oldest hit (t=100) ages out at t=160, so retry_after == 30 at t=130.
    assert denied.retry_after == 30.0


def test_denies_even_when_retry_after_cannot_be_computed() -> None:
    """A zero limit denies every request with no computable retry_after (Req 1.7)."""

    clock = _Clock(0.0)
    limiter = RateLimiter(limit=0, window_seconds=60, time_fn=clock)

    decision = limiter.check("ip")
    assert decision.allowed is False
    assert decision.retry_after is None


def test_per_ip_isolation() -> None:
    """Each client IP has an independent window (Req 1.6)."""

    clock = _Clock(0.0)
    limiter = RateLimiter(limit=1, window_seconds=60, time_fn=clock)

    assert limiter.check("a").allowed is True
    assert limiter.check("a").allowed is False
    # A different IP is unaffected by IP "a"'s hits.
    assert limiter.check("b").allowed is True


# --- Task 5.2 / Property 16 -------------------------------------------------


# Feature: autonomous-agent-service, Property 16: Rate limiting honors the sliding window
@pytest.mark.property
@settings(max_examples=200)
@given(
    limit=st.integers(min_value=1, max_value=5),
    window_seconds=st.integers(min_value=1, max_value=30),
    gaps=st.lists(
        st.integers(min_value=0, max_value=40),
        min_size=1,
        max_size=40,
    ),
)
def test_sliding_window_matches_reference_model(
    limit: int, window_seconds: int, gaps: list[int]
) -> None:
    """Property 16: the limiter's decisions honor the trailing sliding window.

    **Validates: Requirements 1.6, 1.7**

    For any sequence of request timestamps from a single IP, the limiter denies
    a request when ``limit`` or more accepted hits fall within the trailing
    ``window_seconds`` window, and allows it once older hits age out. This is
    checked against an independent reference model that tracks the accepted-hit
    timestamps and recomputes the in-window count at each request time. When a
    request is denied, the best-effort ``retry_after`` is validated: for a
    positive limit it is a positive number of seconds bounded by the window.
    """

    clock = _Clock(0.0)
    limiter = RateLimiter(limit=limit, window_seconds=window_seconds, time_fn=clock)

    accepted: list[float] = []  # reference model of accepted hit timestamps
    now = 0.0
    for gap in gaps:
        now += float(gap)
        clock.set(now)

        # Reference: in-window accepted hits are those strictly within the window.
        in_window = [ts for ts in accepted if now - ts < window_seconds]
        expected_allowed = len(in_window) < limit

        decision = limiter.check("ip")
        assert isinstance(decision, RateDecision)
        assert decision.allowed is expected_allowed, (
            f"at t={now}: expected allowed={expected_allowed}, "
            f"got {decision.allowed}; in_window={len(in_window)} limit={limit}"
        )

        if decision.allowed:
            assert decision.retry_after is None
            accepted.append(now)
        else:
            # Denial always occurs; retry_after is best-effort. With a positive
            # limit there is an in-window hit, so it is a bounded positive value.
            if decision.retry_after is not None:
                assert 0 < decision.retry_after <= window_seconds
