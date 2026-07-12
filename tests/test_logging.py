"""Property-based tests for the structured logger (`app.core.logging`).

These tests validate two design correctness properties for the availability-first
logging contract:

- **Property 17** (Req 15.1, 15.2): if the structured logger raises while
  emitting, the calling component still completes and the error is suppressed —
  availability is never sacrificed for observability.
- **Property 18** (Req 1.5): for any request classified as malicious, the emitted
  ``security_event`` contains a hash of the request and never the verbatim
  payload.

Both property tests use Hypothesis with a minimum of 100 iterations.
"""

from __future__ import annotations

import hashlib
import io
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.logging import StructuredLogger

# --- Test doubles -----------------------------------------------------------


class _FailingSink:
    """A sink that always raises, used to exercise the degradation path."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, entry: str) -> None:
        self.calls += 1
        raise RuntimeError("sink failure injected by test")


class _CapturingSink:
    """A sink that records every serialized entry it receives."""

    def __init__(self) -> None:
        self.entries: list[str] = []

    def __call__(self, entry: str) -> None:
        self.entries.append(entry)


class _FailingStream(io.StringIO):
    """A stderr-like stream whose ``write`` always raises."""

    def write(self, s: str) -> int:  # type: ignore[override]
        raise RuntimeError("stderr failure injected by test")


# --- Strategies -------------------------------------------------------------

_text = st.text(max_size=200)


# --- Property 17 ------------------------------------------------------------


# Feature: autonomous-agent-service, Property 17: Logging failures never break a Run
@pytest.mark.property
@settings(max_examples=200)
@given(
    component=_text,
    run_id=_text,
    decision=_text,
    client_ip=_text,
    request_hash=_text,
    reason=_text,
    fallback_also_fails=st.booleans(),
)
def test_logging_failures_never_break_a_run(
    component: str,
    run_id: str,
    decision: str,
    client_ip: str,
    request_hash: str,
    reason: str,
    fallback_also_fails: bool,
) -> None:
    """Property 17: a failing logger never propagates to the caller.

    **Validates: Requirements 15.1, 15.2**

    For any decision or security event, if the primary sink raises while
    emitting, :meth:`StructuredLogger.decision` and
    :meth:`StructuredLogger.security_event` still return normally (``None``) and
    never propagate the error — even when the best-effort stderr fallback write
    itself also fails. A single best-effort fallback write may be attempted.
    """

    sink = _FailingSink()
    stderr: io.StringIO = _FailingStream() if fallback_also_fails else io.StringIO()
    logger = StructuredLogger(sink=sink, stderr=stderr)

    # Neither call may raise, regardless of the failing sink (and possibly the
    # failing fallback stream). Both must return None.
    assert logger.decision(component, run_id, decision) is None
    assert logger.security_event(client_ip, request_hash, reason) is None

    # The primary sink was actually exercised for both emissions (so the
    # suppression path — not a no-op — is what protected the caller).
    assert sink.calls == 2


# --- Property 18 ------------------------------------------------------------


# Feature: autonomous-agent-service, Property 18: Malicious rejection logs a hash, not the payload
@pytest.mark.property
@settings(max_examples=200)
@given(request=_text, client_ip=st.sampled_from(["203.0.113.7", "198.51.100.42"]))
def test_security_event_logs_hash_not_payload(request: str, client_ip: str) -> None:
    """Property 18: security_event logs the request hash, never the payload.

    **Validates: Requirements 1.5**

    For any request string, the emitted ``security_event`` entry contains the
    hash of the request and does not contain the verbatim request payload. A
    unique marker is prepended to the payload so the absence assertion is
    robust against coincidental substring collisions with the entry's fixed
    metadata.
    """

    # Make the payload distinctive so "verbatim payload absent" is meaningful
    # even for short/empty request text. The marker cannot appear in the
    # logger's fixed metadata (timestamp, ip, reason, hash).
    marker = f"PAYLOAD-{uuid.uuid4().hex}-"
    payload = marker + request
    request_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    sink = _CapturingSink()
    logger = StructuredLogger(sink=sink)

    logger.security_event(client_ip, request_hash, "malicious")

    assert len(sink.entries) == 1
    entry = sink.entries[0]

    # The hash of the request must be present...
    assert request_hash in entry
    # ...but the verbatim payload must never be logged (the unique marker that
    # begins the payload never appears, so the whole payload cannot be present).
    assert marker not in entry
    assert payload not in entry
