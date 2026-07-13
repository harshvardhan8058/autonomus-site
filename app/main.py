"""FastAPI application factory and process entry point (Req 13.1, 13.2).

This module wires the whole Autonomous Agent Service together. :func:`create_app`
builds the :class:`~fastapi.FastAPI` application: it mounts the ``agent``,
``documents``, and ``health`` routers, serves the mission-control single-page
frontend as static files, and registers a lifespan handler that constructs every
service singleton from :class:`~app.core.config.Settings` and attaches them to
``app.state`` exactly as the dependency helpers in :mod:`app.api.deps` expect
(``rate_limiter``, ``run_store``, ``event_bus``, ``guardrail``, ``orchestrator``,
``llm_service``, ``logger``).

A module-level ``app = create_app()`` is provided so the service can be launched
with ``uvicorn app.main:app``.

Construction is deliberately import-safe (Req 14): importing this module and
building the app performs no network I/O and requires no ``GROQ_API_KEY`` — the
:class:`~app.services.llm.LLMService` resolves its active backend purely from
configuration (selecting Ollama when no Groq key is present). The singletons are
built inside the lifespan handler so that backend resolution and startup logging
happen at application startup rather than at import time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.agent.executor import Executor
from app.agent.guardrail import GuardrailValidator
from app.agent.orchestrator import Orchestrator
from app.agent.planner import Planner
from app.agent.reflector import Reflector
from app.agent.tools import build_default_registry
from app.api import agent as agent_api
from app.api import documents as documents_api
from app.api import health as health_api
from app.core.config import Settings, get_settings
from app.core.event_bus import EventBus
from app.core.logging import StructuredLogger
from app.core.rate_limiter import RateLimiter
from app.core.run_store import RunStore
from app.services.docx_builder import DocumentBuilder
from app.services.llm import LLMService

# The component name used in structured startup decision logs.
_COMPONENT = "app"

# The directory (relative to the project root) that holds the mission-control
# single-page frontend served as static files. Resolved relative to this module
# so it is independent of the process working directory.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _load_settings() -> Settings:
    """Load service configuration, surfacing a clear error on failure (Req 14).

    Every configuration field carries a documented default, so loading normally
    succeeds even with an empty environment. Should configuration loading fail
    (for example a future required-with-no-default field is left unset), the
    error is re-raised as a clear :class:`RuntimeError` so the failure surfaces
    at startup rather than as an opaque crash.

    Returns:
        The loaded :class:`~app.core.config.Settings` singleton.

    Raises:
        RuntimeError: If configuration could not be loaded from the environment.
    """

    try:
        return get_settings()
    except Exception as exc:  # noqa: BLE001 - normalize to a clear startup error
        raise RuntimeError(
            "failed to load service configuration from the environment; "
            f"check the environment / .env file: {exc}"
        ) from exc


def _build_components(settings: Settings, logger: StructuredLogger) -> dict[str, Any]:
    """Construct the service singletons from settings (Req 13.1).

    Wires the full component graph in dependency order: the infrastructure
    primitives (rate limiter, run store, event bus), the LLM service, the
    document builder, the tool registry, and finally the agent-loop components
    (executor, reflector, planner, guardrail, orchestrator). No network I/O is
    performed — the LLM service resolves its active backend purely from
    configuration.

    Args:
        settings: The loaded service configuration.
        logger: The shared structured logger injected into components.

    Returns:
        A mapping of ``app.state`` attribute name to constructed component,
        matching the names the :mod:`app.api.deps` helpers read.
    """

    rate_limiter = RateLimiter(
        limit=settings.RATE_LIMIT_MAX,
        window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
    )
    run_store = RunStore()
    event_bus = EventBus()

    # Backend selection is derived from GROQ_API_KEY without any network call, so
    # constructing the service never requires a key or connectivity (Req 14).
    llm_service = LLMService(settings)

    doc_builder = DocumentBuilder(settings.THEME_COLOR, logger)
    registry = build_default_registry(
        llm_service,
        doc_builder,
        enable_offline_fallback=settings.LLM_OFFLINE_FALLBACK,
    )

    executor = Executor(registry, event_bus, logger)
    reflector = Reflector(llm_service, event_bus, logger)
    planner = Planner(
        llm_service,
        logger,
        enable_offline_fallback=settings.LLM_OFFLINE_FALLBACK,
    )
    guardrail = GuardrailValidator(llm_service)
    orchestrator = Orchestrator(
        guardrail,
        planner,
        executor,
        reflector,
        doc_builder,
        run_store,
        event_bus,
        logger,
    )

    return {
        "settings": settings,
        "logger": logger,
        "rate_limiter": rate_limiter,
        "run_store": run_store,
        "event_bus": event_bus,
        "llm_service": llm_service,
        "guardrail": guardrail,
        "orchestrator": orchestrator,
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Construct singletons at startup and attach them to ``app.state`` (Req 13.1).

    On startup, configuration is loaded, the shared structured logger and every
    service singleton are constructed, and each is attached to ``app.state`` under
    the name the :mod:`app.api.deps` helpers expect. The resolved active LLM
    backend is recorded with a structured startup decision log. No teardown is
    required, so the shutdown phase is a no-op.

    Args:
        app: The application whose ``state`` the singletons are attached to.

    Yields:
        Control back to the ASGI server while the application serves requests.
    """

    settings = _load_settings()
    logger = StructuredLogger()
    components = _build_components(settings, logger)
    for name, component in components.items():
        setattr(app.state, name, component)

    llm_service: LLMService = components["llm_service"]
    logger.decision(
        _COMPONENT,
        "-",
        "application startup complete",
        level="INFO",
        active_backend=llm_service.active_backend,
        rate_limit_max=settings.RATE_LIMIT_MAX,
        rate_limit_window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
        document_output_dir=settings.DOCUMENT_OUTPUT_DIR,
    )

    yield


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application (Req 13.1, 13.2).

    Assembles the application by registering the :func:`lifespan` handler,
    mounting the ``agent``, ``documents``, and ``health`` routers, and serving
    the mission-control frontend as static files. The static mount is added
    **after** the API routers so that the API routes always resolve first; the
    catch-all static mount serves ``index.html`` at the root (``GET /``) once the
    frontend is populated (Task 19). The frontend directory is served with
    ``html=True`` so that a directory request returns its ``index.html``; when the
    directory does not yet contain an ``index.html`` the root simply returns 404
    without affecting the rest of the application.

    Building the app performs no network I/O and requires no ``GROQ_API_KEY`` —
    the service singletons are constructed later, in the lifespan handler, at
    application startup.

    Returns:
        The configured :class:`~fastapi.FastAPI` application instance.
    """

    app = FastAPI(
        title="Autonomous Agent Service",
        description=(
            "Turns a natural-language business request into a polished "
            "Microsoft Word (.docx) deliverable through a Planner -> Executor "
            "-> Reflector loop."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # Mount the API routers first so their explicit routes take precedence over
    # the catch-all static mount added below (Req 13.1).
    app.include_router(agent_api.router)
    app.include_router(documents_api.router)
    app.include_router(health_api.router)

    # Serve the single-page mission-control frontend as static files, with
    # index.html served at the root. Guarded by directory existence so the app
    # still builds before the frontend is populated (Task 19).
    if _FRONTEND_DIR.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(_FRONTEND_DIR), html=True, check_dir=False),
            name="frontend",
        )

    return app


# Module-level application instance so the service can be launched with
# ``uvicorn app.main:app`` (Req 13.1).
app = create_app()
