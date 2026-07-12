"""FastAPI dependency helpers for the API layer.

The routers in :mod:`app.api.agent`, :mod:`app.api.documents`, and
:mod:`app.api.health` are wired to the shared service components
(:class:`~app.core.rate_limiter.RateLimiter`,
:class:`~app.core.run_store.RunStore`, :class:`~app.core.event_bus.EventBus`,
:class:`~app.agent.guardrail.GuardrailValidator`,
:class:`~app.agent.orchestrator.Orchestrator`,
:class:`~app.services.llm.LLMService`, and
:class:`~app.core.logging.StructuredLogger`) through the small dependency
helpers defined here.

Each helper reads its component from ``request.app.state``. The application
factory (Task 18) is responsible for constructing the singletons and attaching
them to ``app.state`` during startup; the tests assemble an app with fakes on
``app.state`` in the same way (see ``tests/support.py``). Keeping the lookups in
one place lets the routers depend on typed accessors rather than reaching into
``app.state`` directly, and lets tests override the components trivially.
"""

from __future__ import annotations

from fastapi import Request

from app.agent.guardrail import GuardrailValidator
from app.agent.orchestrator import Orchestrator
from app.core.event_bus import EventBus
from app.core.logging import StructuredLogger
from app.core.rate_limiter import RateLimiter
from app.core.run_store import RunStore
from app.services.llm import LLMService


def get_rate_limiter(request: Request) -> RateLimiter:
    """Return the per-IP :class:`RateLimiter` from application state.

    Args:
        request: The incoming request whose ``app.state`` holds the component.

    Returns:
        The application's :class:`~app.core.rate_limiter.RateLimiter`.
    """

    return request.app.state.rate_limiter


def get_run_store(request: Request) -> RunStore:
    """Return the :class:`RunStore` from application state.

    Args:
        request: The incoming request whose ``app.state`` holds the component.

    Returns:
        The application's :class:`~app.core.run_store.RunStore`.
    """

    return request.app.state.run_store


def get_event_bus(request: Request) -> EventBus:
    """Return the :class:`EventBus` from application state.

    Args:
        request: The incoming request whose ``app.state`` holds the component.

    Returns:
        The application's :class:`~app.core.event_bus.EventBus`.
    """

    return request.app.state.event_bus


def get_guardrail(request: Request) -> GuardrailValidator:
    """Return the :class:`GuardrailValidator` from application state.

    Args:
        request: The incoming request whose ``app.state`` holds the component.

    Returns:
        The application's :class:`~app.agent.guardrail.GuardrailValidator`.
    """

    return request.app.state.guardrail


def get_orchestrator(request: Request) -> Orchestrator:
    """Return the :class:`Orchestrator` from application state.

    Args:
        request: The incoming request whose ``app.state`` holds the component.

    Returns:
        The application's :class:`~app.agent.orchestrator.Orchestrator`.
    """

    return request.app.state.orchestrator


def get_llm_service(request: Request) -> LLMService:
    """Return the :class:`LLMService` from application state.

    Args:
        request: The incoming request whose ``app.state`` holds the component.

    Returns:
        The application's :class:`~app.services.llm.LLMService`.
    """

    return request.app.state.llm_service


def get_logger(request: Request) -> StructuredLogger:
    """Return the :class:`StructuredLogger` from application state.

    Args:
        request: The incoming request whose ``app.state`` holds the component.

    Returns:
        The application's :class:`~app.core.logging.StructuredLogger`.
    """

    return request.app.state.logger
