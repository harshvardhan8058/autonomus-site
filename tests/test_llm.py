"""Property-based tests for the LLM service (`app.services.llm`).

Covers the three LLM correctness properties from the design, each implemented as
a single Hypothesis property test with a minimum of 100 iterations and driven
entirely through the network-free fake backend (see ``tests/conftest.py``):

- Task 7.3 / Property 6: JSON repair yields schema-valid output or a clean
  failure (Req 2.4).
- Task 7.4 / Property 7: LLM calls are bounded and fall back Groq -> Ollama
  (Req 2.4, 2.5, 5.3).
- Task 7.5 / Property 8: backend selection follows the API key (Req 5.1, 5.2).

Because the service's completion API is asynchronous and backed by
:class:`asyncio.Queue`-free coroutines, each generated example is driven through
:func:`asyncio.run`.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from app.core.config import Settings
from app.services.llm import LLMError, LLMJSONError, LLMService
from tests.conftest import FakeLLMBackend

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _noop_sleep(_delay: float) -> None:
    """A no-op async sleep so backoff introduces no real delay."""

    return None


class _Target(BaseModel):
    """A tiny target schema used to exercise JSON parsing/repair (Property 6)."""

    name: str
    value: int
    tags: list[str]


# A "safe" text alphabet that avoids characters which would interfere with the
# JSON structure or the repair heuristics (quotes, braces, backslashes,
# backticks). This keeps Property 6 focused on the repair mechanics rather than
# adversarial escaping.
_safe_text = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_",
    min_size=0,
    max_size=15,
)


@st.composite
def _targets(draw: st.DrawFn) -> _Target:
    """Draw a valid :class:`_Target` instance with JSON-safe field values."""

    return _Target(
        name=draw(_safe_text),
        value=draw(st.integers(min_value=-1000, max_value=1000)),
        tags=draw(st.lists(_safe_text, max_size=3)),
    )


def _add_trailing_comma(valid_json: str) -> str:
    """Insert a trailing comma before the final closing brace of an object."""

    idx = valid_json.rfind("}")
    if idx == -1:
        return valid_json
    return valid_json[:idx] + "," + valid_json[idx:]


@st.composite
def _malformed_json(draw: st.DrawFn) -> tuple[_Target, str, str]:
    """Draw a target object and a malformed JSON string wrapping it.

    Returns:
        A ``(target, raw, mode)`` tuple where ``raw`` is a malformed rendering
        of ``target`` and ``mode`` describes the corruption applied.
    """

    target = draw(_targets())
    valid = target.model_dump_json()
    mode = draw(
        st.sampled_from(
            ["fences", "trailing_comma", "fences_and_comma", "prefixed", "truncation"]
        )
    )
    if mode == "fences":
        raw = f"```json\n{valid}\n```"
    elif mode == "trailing_comma":
        raw = _add_trailing_comma(valid)
    elif mode == "fences_and_comma":
        raw = f"```\n{_add_trailing_comma(valid)}\n```"
    elif mode == "prefixed":
        raw = f"Sure, here is the JSON you requested:\n{valid}"
    else:  # truncation
        cut = draw(st.integers(min_value=1, max_value=max(1, len(valid) - 1)))
        raw = valid[:cut]
    return target, raw, mode


# ---------------------------------------------------------------------------
# Task 7.3 / Property 6
# ---------------------------------------------------------------------------


# Feature: autonomous-agent-service, Property 6: JSON repair yields schema-valid output or a clean failure  # noqa: E501
@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(data=_malformed_json())
def test_property_6_json_repair_valid_or_clean_failure(
    data: tuple[_Target, str, str],
) -> None:
    """Property 6: repair returns schema-valid output or raises ``LLMJSONError``.

    **Validates: Requirements 2.4**

    For any malformed JSON (code fences, trailing commas, truncation, or leading
    commentary) wrapping a valid target-schema object, ``complete_json`` either
    returns an instance that validates against the schema, or raises the
    designated clean failure (:class:`LLMJSONError`) — never a partially-parsed
    or schema-invalid object. For the recoverable corruptions the repaired
    instance additionally round-trips back to the original target.
    """

    target, raw, mode = data

    async def _run() -> None:
        settings_ = Settings(GROQ_API_KEY="test-key")
        # Both backends return the same malformed payload so the outcome depends
        # solely on whether the repair pass can recover a schema-valid object.
        groq = FakeLLMBackend("groq", response=raw)
        ollama = FakeLLMBackend("ollama", response=raw)
        svc = LLMService(
            settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
        )

        try:
            result = await svc.complete_json("prompt", _Target)
        except LLMJSONError:
            # A clean failure is always an acceptable outcome (property P6).
            return

        # When a value is returned it must be a schema-valid instance and must
        # itself re-validate (never partial/invalid).
        assert isinstance(result, _Target)
        _Target.model_validate_json(result.model_dump_json())

        # Recoverable corruptions must reproduce the original object exactly.
        if mode != "truncation":
            assert result == target

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Task 7.4 / Property 7
# ---------------------------------------------------------------------------


# Feature: autonomous-agent-service, Property 7: LLM calls are bounded and fall back
@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(
    groq_fail=st.integers(min_value=0, max_value=6),
    ollama_fail=st.integers(min_value=0, max_value=6),
)
def test_property_7_calls_bounded_and_fall_back(
    groq_fail: int, ollama_fail: int
) -> None:
    """Property 7: at most 3 attempts per backend, with Groq -> Ollama fallback.

    **Validates: Requirements 2.4, 2.5, 5.3**

    For any sequence of simulated leading failures, the primary (Groq) backend
    is attempted at most ``LLM_MAX_RETRIES`` (3) times and, once it exhausts its
    retries, the secondary (Ollama) backend is attempted (also at most 3 times)
    before a final failure is raised.
    """

    async def _run() -> None:
        settings_ = Settings(GROQ_API_KEY="test-key")  # LLM_MAX_RETRIES defaults to 3
        max_retries = settings_.LLM_MAX_RETRIES
        groq = FakeLLMBackend("groq", response="GROQ_OK", fail_times=groq_fail)
        ollama = FakeLLMBackend("ollama", response="OLLAMA_OK", fail_times=ollama_fail)
        svc = LLMService(
            settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
        )

        groq_succeeds = groq_fail < max_retries
        ollama_succeeds = ollama_fail < max_retries

        if groq_succeeds:
            result = await svc.complete("prompt")
            assert result == "GROQ_OK"
            # Groq succeeded on the (groq_fail+1)-th attempt; Ollama untouched.
            assert groq.call_count == groq_fail + 1
            assert ollama.call_count == 0
        elif ollama_succeeds:
            result = await svc.complete("prompt")
            assert result == "OLLAMA_OK"
            # Groq exhausted its retries, then Ollama was attempted and succeeded.
            assert groq.call_count == max_retries
            assert ollama.call_count == ollama_fail + 1
        else:
            with pytest.raises(LLMError):
                await svc.complete("prompt")
            # Both backends exhausted their bounded retries before final failure.
            assert groq.call_count == max_retries
            assert ollama.call_count == max_retries

        # Universal bound: never more than max_retries attempts on either backend.
        assert groq.call_count <= max_retries
        assert ollama.call_count <= max_retries

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Task 7.5 / Property 8
# ---------------------------------------------------------------------------


# Feature: autonomous-agent-service, Property 8: Backend selection follows the API key
@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(
    api_key=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ ",
        max_size=40,
    )
)
def test_property_8_backend_selection_follows_api_key(api_key: str) -> None:
    """Property 8: ``active_backend`` is ``groq`` iff ``GROQ_API_KEY`` is set.

    **Validates: Requirements 5.1, 5.2**

    For any environment configuration, the resolved active backend is ``groq``
    if and only if ``GROQ_API_KEY`` is a non-empty value; otherwise it is
    ``ollama``.
    """

    settings_ = Settings(GROQ_API_KEY=api_key)
    svc = LLMService(
        settings_,
        groq_backend=FakeLLMBackend("groq"),
        ollama_backend=FakeLLMBackend("ollama"),
        sleep=_noop_sleep,
    )

    if api_key:  # non-empty -> Groq is the active/primary backend
        assert svc.active_backend == "groq"
    else:  # unset/empty -> automatic Ollama fallback
        assert svc.active_backend == "ollama"


# ---------------------------------------------------------------------------
# Focused example-based unit tests (complement the properties above)
# ---------------------------------------------------------------------------


def test_active_backend_unknown_until_resolved() -> None:
    """A deferred-resolution service reports ``unknown`` until resolved (Req 5.5)."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    svc = LLMService(
        settings_,
        groq_backend=FakeLLMBackend("groq"),
        ollama_backend=FakeLLMBackend("ollama"),
        sleep=_noop_sleep,
        resolve=False,
    )
    assert svc.active_backend == "unknown"
    assert svc.resolve_backend() == "groq"
    assert svc.active_backend == "groq"


async def test_complete_json_parses_clean_output() -> None:
    """A well-formed JSON response parses on the first attempt without fallback."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    payload = _Target(name="cloud-migration", value=3, tags=["a", "b"])
    groq = FakeLLMBackend("groq", response=payload.model_dump_json())
    ollama = FakeLLMBackend("ollama")
    svc = LLMService(
        settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )

    result = await svc.complete_json("prompt", _Target)

    assert result == payload
    assert groq.call_count == 1
    assert ollama.call_count == 0


async def test_health_reports_active_backend_reachability() -> None:
    """``health`` returns the active backend name and its reachability (Req 5.4)."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    groq = FakeLLMBackend("groq", reachable=True)
    ollama = FakeLLMBackend("ollama", reachable=False)
    svc = LLMService(
        settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )

    name, reachable = await svc.health()

    assert name == "groq"
    assert reachable is True
    assert groq.health_calls == 1


async def test_complete_json_invokes_backend_with_json_mode() -> None:
    """``complete_json`` calls the backend with native JSON mode enabled."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    payload = _Target(name="x", value=1, tags=[])
    groq = FakeLLMBackend("groq", response=payload.model_dump_json())
    ollama = FakeLLMBackend("ollama")
    svc = LLMService(
        settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )

    await svc.complete_json("prompt", _Target)

    assert groq.last_json_mode is True


async def test_complete_uses_free_form_mode() -> None:
    """Free-form ``complete`` calls the backend with ``json_mode=False``."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    groq = FakeLLMBackend("groq", response="hello")
    ollama = FakeLLMBackend("ollama")
    svc = LLMService(
        settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )

    await svc.complete("prompt")

    assert groq.last_json_mode is False


def test_repair_json_strips_fences_and_trailing_commas() -> None:
    """The repair helper recovers a fenced object with a trailing comma."""

    raw = '```json\n{"name": "x", "value": 1, "tags": [],}\n```'
    repaired = LLMService._repair_json(raw)
    assert re.match(r"^\{.*\}$", repaired, re.DOTALL)
    assert ",}" not in repaired
    assert "```" not in repaired



# ---------------------------------------------------------------------------
# Offline fallback (deterministic degradation when all backends fail)
# ---------------------------------------------------------------------------


async def test_complete_returns_offline_fallback_when_all_backends_fail() -> None:
    """``complete`` returns the fallback value (no raise) when every backend fails."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    groq = FakeLLMBackend("groq", always_fail=True)
    ollama = FakeLLMBackend("ollama", always_fail=True)
    svc = LLMService(
        settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )

    result = await svc.complete("prompt", offline_fallback=lambda: "OFFLINE")

    assert result == "OFFLINE"


async def test_complete_json_returns_offline_fallback_when_all_backends_fail() -> None:
    """``complete_json`` returns the fallback instance when every backend fails."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    groq = FakeLLMBackend("groq", always_fail=True)
    ollama = FakeLLMBackend("ollama", always_fail=True)
    svc = LLMService(
        settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )
    sentinel = _Target(name="offline", value=0, tags=[])

    result = await svc.complete_json("prompt", _Target, offline_fallback=lambda: sentinel)

    assert result is sentinel


async def test_offline_fallback_not_called_when_backend_succeeds() -> None:
    """A successful backend never invokes the offline fallback."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    groq = FakeLLMBackend("groq", response="LIVE")
    ollama = FakeLLMBackend("ollama")
    svc = LLMService(
        settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )

    called = False

    def _fallback() -> str:
        nonlocal called
        called = True
        return "OFFLINE"

    result = await svc.complete("prompt", offline_fallback=_fallback)

    assert result == "LIVE"
    assert called is False


async def test_complete_still_raises_without_offline_fallback() -> None:
    """Without a fallback, total backend failure still raises ``LLMError``."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    groq = FakeLLMBackend("groq", always_fail=True)
    ollama = FakeLLMBackend("ollama", always_fail=True)
    svc = LLMService(
        settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )

    with pytest.raises(LLMError):
        await svc.complete("prompt")


async def test_complete_json_still_raises_without_offline_fallback() -> None:
    """Without a fallback, total backend failure still raises ``LLMJSONError``."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    groq = FakeLLMBackend("groq", always_fail=True)
    ollama = FakeLLMBackend("ollama", always_fail=True)
    svc = LLMService(
        settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )

    with pytest.raises(LLMJSONError):
        await svc.complete_json("prompt", _Target)


async def test_offline_fallback_error_does_not_mask_backend_failure() -> None:
    """When the fallback itself raises, the original LLM error is surfaced."""

    settings_ = Settings(GROQ_API_KEY="test-key")
    groq = FakeLLMBackend("groq", always_fail=True)
    ollama = FakeLLMBackend("ollama", always_fail=True)
    svc = LLMService(
        settings_, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )

    def _broken_fallback() -> str:
        raise ValueError("fallback bug")

    with pytest.raises(LLMError):
        await svc.complete("prompt", offline_fallback=_broken_fallback)
