"""Shared pytest fixtures for the Autonomous Agent Service test suite.

This module provides a reusable **fake LLM backend** (Task 7.2) that implements
the :class:`app.services.llm.LLMBackend` transport protocol without performing
any network calls. The fake is fully scriptable: callers configure a sequence of
canned text/JSON responses and/or a number of leading failures (or an
always-fail mode), and the fake records how many times it was invoked so tests
can assert retry/fallback bounds (Req 5.3, 2.4, properties P6-P8).

Exposed fixtures / helpers:

- :class:`FakeLLMBackend` — the scriptable fake transport class (importable by
  any test module: ``from tests.conftest import FakeLLMBackend``).
- ``fake_backend`` — a factory fixture that builds :class:`FakeLLMBackend`
  instances.
- ``no_sleep`` — a no-op async sleep so backoff never introduces real delays.
- ``make_llm_service`` — a factory fixture that wires an :class:`LLMService`
  with injected fake backends and the no-op sleep.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

import pytest

from app.core.config import Settings
from app.services.llm import LLMService


class FakeLLMBackend:
    """A scriptable, network-free fake implementing the ``LLMBackend`` protocol.

    The fake can be configured to fail a fixed number of leading calls (then
    succeed), to always fail, and/or to return a scripted sequence of canned
    responses. Every :meth:`complete` invocation increments :attr:`call_count`,
    letting tests assert exact retry/fallback behavior.

    Attributes:
        name: The backend name (``"groq"`` or ``"ollama"``) used for chain
            ordering and health reporting.
        call_count: The number of times :meth:`complete` has been invoked.
        health_calls: The number of times :meth:`health` has been invoked.
        reachable: The value returned by :meth:`health`.
    """

    def __init__(
        self,
        name: str = "groq",
        *,
        responses: Sequence[str] | None = None,
        response: str | None = None,
        fail_times: int = 0,
        always_fail: bool = False,
        exc_factory: Callable[[], Exception] | None = None,
        reachable: bool = True,
    ) -> None:
        """Configure the fake backend.

        Args:
            name: The backend name; drives chain ordering (``"groq"`` primary).
            responses: An ordered list of canned completions. Each successful
                call pops the next item; the final item repeats once exhausted.
            response: Convenience for a single repeated canned completion.
            fail_times: The number of leading calls that raise before the first
                successful response.
            always_fail: When ``True``, every call raises (never succeeds).
            exc_factory: A factory producing the exception raised on a failing
                call. Defaults to a :class:`RuntimeError`.
            reachable: The value returned by :meth:`health`.
        """

        self.name = name
        if responses is not None:
            self._responses: list[str] = list(responses)
        elif response is not None:
            self._responses = [response]
        else:
            self._responses = [""]
        self._fail_times = fail_times
        self._always_fail = always_fail
        self._exc_factory = exc_factory or (
            lambda: RuntimeError(f"fake {name} backend failure")
        )
        self.reachable = reachable
        self.call_count = 0
        self.health_calls = 0

    async def complete(
        self,
        *,
        prompt: str,
        system: str | None,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Return the next canned completion, or raise per the fail policy."""

        self.call_count += 1
        if self._always_fail:
            raise self._exc_factory()
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._exc_factory()
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    async def health(self) -> bool:
        """Return the configured reachability flag."""

        self.health_calls += 1
        return self.reachable


@pytest.fixture
def fake_backend() -> Callable[..., FakeLLMBackend]:
    """Return a factory that builds :class:`FakeLLMBackend` instances.

    Returns:
        A callable forwarding its arguments to :class:`FakeLLMBackend`.
    """

    def _make(name: str = "groq", **kwargs: object) -> FakeLLMBackend:
        return FakeLLMBackend(name, **kwargs)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def no_sleep() -> Callable[[float], Awaitable[None]]:
    """Return a no-op async sleep so retry backoff introduces no real delay.

    Returns:
        An async callable accepting a delay and returning immediately.
    """

    async def _sleep(_delay: float) -> None:
        return None

    return _sleep


@pytest.fixture
def make_llm_service(
    no_sleep: Callable[[float], Awaitable[None]],
) -> Callable[..., LLMService]:
    """Return a factory that wires an :class:`LLMService` with fake backends.

    The factory accepts optional ``groq`` and ``ollama`` fake backends and an
    optional ``settings`` override, always injecting the no-op sleep so tests
    never wait on backoff delays.

    Returns:
        A callable producing a configured :class:`LLMService`.
    """

    def _make(
        *,
        settings: Settings | None = None,
        groq: FakeLLMBackend | None = None,
        ollama: FakeLLMBackend | None = None,
        groq_api_key: str = "test-key",
        resolve: bool = True,
    ) -> LLMService:
        if settings is None:
            settings = Settings(GROQ_API_KEY=groq_api_key)
        return LLMService(
            settings,
            groq_backend=groq if groq is not None else FakeLLMBackend("groq"),
            ollama_backend=ollama if ollama is not None else FakeLLMBackend("ollama"),
            sleep=no_sleep,
            resolve=resolve,
        )

    return _make
