"""Reusable test app-builder and fakes for the API layer (Tasks 17.5, 17.6).

The real FastAPI application factory is implemented in Task 18. For the API
tests, :func:`build_app` assembles a lightweight app that mounts the three API
routers (``agent``, ``documents``, ``health``) and attaches the service
components to ``app.state`` exactly the way the routers' dependency helpers
(:mod:`app.api.deps`) expect. Any component not supplied is defaulted to a real
in-memory implementation (:class:`~app.core.rate_limiter.RateLimiter`,
:class:`~app.core.run_store.RunStore`, :class:`~app.core.event_bus.EventBus`,
:class:`~app.core.logging.StructuredLogger`), while the LLM-dependent components
(guardrail, orchestrator, LLM service) default to the network-free fakes defined
here.

This module is intentionally **not** named ``test_*`` so pytest does not collect
it as a test module; test files import from it (``from tests.support import
build_app``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI

from app.api import agent as agent_api
from app.api import documents as documents_api
from app.api import health as health_api
from app.core.event_bus import EventBus
from app.core.logging import StructuredLogger
from app.core.rate_limiter import RateLimiter
from app.core.run_store import RunStore
from app.models.schemas import AgentResponse, IntentClass, RunState


class FakeGuardrail:
    """A network-free guardrail returning a preconfigured intent (Req 1.3).

    Attributes:
        intent: The :class:`~app.models.schemas.IntentClass` every call returns.
        calls: The number of times :meth:`classify` was invoked.
    """

    def __init__(
        self, intent: IntentClass = IntentClass.VALID_DOCUMENT_REQUEST
    ) -> None:
        """Configure the fake guardrail.

        Args:
            intent: The intent every :meth:`classify` call should return.
        """

        self.intent = intent
        self.calls = 0

    async def classify(self, request: str) -> IntentClass:
        """Return the preconfigured intent, recording the invocation.

        Args:
            request: The request text (ignored by the fake).

        Returns:
            The configured :class:`~app.models.schemas.IntentClass`.
        """

        self.calls += 1
        return self.intent


class FakeOrchestrator:
    """A network-free orchestrator with scripted outcomes (Req 2.6, 8.1).

    The fake either returns a preconfigured :class:`AgentResponse`, raises a
    preconfigured exception (for the planning-failure path), or delegates to a
    supplied ``on_run`` coroutine for full control.

    Attributes:
        calls: The number of times :meth:`execute_run` was invoked.
    """

    def __init__(
        self,
        *,
        response: AgentResponse | None = None,
        error: Exception | None = None,
        on_run: Callable[[RunState], Awaitable[AgentResponse]] | None = None,
    ) -> None:
        """Configure the fake orchestrator.

        Args:
            response: The response returned by :meth:`execute_run` on success.
            error: An exception raised by :meth:`execute_run` when set (takes
                precedence over ``response``).
            on_run: An optional coroutine invoked with the run state; when set it
                fully drives the outcome.
        """

        self._response = response
        self._error = error
        self._on_run = on_run
        self.calls = 0

    async def execute_run(self, run_state: RunState) -> AgentResponse:
        """Execute the scripted outcome for ``run_state``.

        Args:
            run_state: The run state created by the endpoint.

        Returns:
            The configured :class:`AgentResponse`.

        Raises:
            Exception: The configured error, when one was supplied.
        """

        self.calls += 1
        if self._error is not None:
            raise self._error
        if self._on_run is not None:
            return await self._on_run(run_state)
        assert self._response is not None, "FakeOrchestrator needs response/error/on_run"
        return self._response


class FakeLLMHealth:
    """A network-free LLM stand-in exposing only :meth:`health` (Req 5.4-5.6).

    Attributes:
        backend: The backend name reported by :meth:`health`.
        reachable: Whether the backend reports as reachable.
        raise_error: When ``True``, :meth:`health` raises to exercise the
            never-raise guard in the health endpoints.
    """

    def __init__(
        self,
        *,
        backend: str = "groq",
        reachable: bool = True,
        raise_error: bool = False,
    ) -> None:
        """Configure the fake LLM health probe.

        Args:
            backend: The backend name to report.
            reachable: The reachability flag to report.
            raise_error: When ``True``, :meth:`health` raises a runtime error.
        """

        self.backend = backend
        self.reachable = reachable
        self.raise_error = raise_error

    async def health(self) -> tuple[str, bool]:
        """Return ``(backend, reachable)`` or raise when configured to.

        Returns:
            The configured ``(backend, reachable)`` tuple.

        Raises:
            RuntimeError: When ``raise_error`` is ``True``.
        """

        if self.raise_error:
            raise RuntimeError("health probe failure")
        return (self.backend, self.reachable)


def build_app(
    *,
    rate_limiter: RateLimiter | None = None,
    run_store: RunStore | None = None,
    event_bus: EventBus | None = None,
    guardrail: object | None = None,
    orchestrator: object | None = None,
    llm_service: object | None = None,
    logger: StructuredLogger | None = None,
) -> FastAPI:
    """Assemble a router-mounted FastAPI app with components on ``app.state``.

    Any component left as ``None`` is defaulted: infrastructure components to
    real in-memory implementations, and the LLM-dependent components to the
    network-free fakes in this module. This mirrors the wiring the Task 18 app
    factory performs, but with injectable fakes for testing.

    Args:
        rate_limiter: The rate limiter; defaults to a fresh :class:`RateLimiter`.
        run_store: The run store; defaults to a fresh :class:`RunStore`.
        event_bus: The event bus; defaults to a fresh :class:`EventBus`.
        guardrail: The guardrail; defaults to a valid-intent :class:`FakeGuardrail`.
        orchestrator: The orchestrator; defaults to a :class:`FakeOrchestrator`.
        llm_service: The LLM service; defaults to a reachable :class:`FakeLLMHealth`.
        logger: The structured logger; defaults to a fresh :class:`StructuredLogger`.

    Returns:
        The assembled :class:`~fastapi.FastAPI` application.
    """

    app = FastAPI()
    app.include_router(agent_api.router)
    app.include_router(documents_api.router)
    app.include_router(health_api.router)

    app.state.rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter()
    app.state.run_store = run_store if run_store is not None else RunStore()
    app.state.event_bus = event_bus if event_bus is not None else EventBus()
    app.state.guardrail = guardrail if guardrail is not None else FakeGuardrail()
    app.state.orchestrator = (
        orchestrator if orchestrator is not None else FakeOrchestrator()
    )
    app.state.llm_service = (
        llm_service if llm_service is not None else FakeLLMHealth()
    )
    app.state.logger = logger if logger is not None else StructuredLogger()
    return app
