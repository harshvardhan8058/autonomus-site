"""Structure smoke tests for the FastAPI app factory (Task 18.2, Req 13.1).

These tests assert the application factory in :mod:`app.main` wires the service
together correctly:

- :func:`app.main.create_app` returns a :class:`~fastapi.FastAPI` instance.
- The agent, documents, and health routes are mounted on the application.
- The mission-control frontend is served as static files.
- The lifespan handler constructs every service singleton and attaches it to
  ``app.state`` under the names the :mod:`app.api.deps` helpers read, and the
  application answers ``GET /health`` with HTTP 200 once started.
- The mandated layered package layout exists (Req 13.1).

The tests use a :class:`~fastapi.testclient.TestClient` context manager so the
lifespan startup runs, populating ``app.state``.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app.main import create_app

# The API route paths every build of the application must expose (Req 13.1).
_EXPECTED_ROUTES = {
    "/agent",
    "/agent/{run_id}/stream",
    "/documents/{run_id}.docx",
    "/health",
    "/health/ready",
}

# The service components the lifespan handler must attach to ``app.state`` for
# the dependency helpers in :mod:`app.api.deps` to resolve (Req 13.1).
_EXPECTED_STATE_COMPONENTS = (
    "rate_limiter",
    "run_store",
    "event_bus",
    "guardrail",
    "orchestrator",
    "llm_service",
    "logger",
)

# The layered package modules mandated by the architecture (Req 13.1).
_REQUIRED_LAYERS = (
    "app.api",
    "app.agent",
    "app.services",
    "app.models",
    "app.core",
)


def _api_route_paths(app: FastAPI) -> set[str]:
    """Return the set of API route paths mounted on ``app``.

    Args:
        app: The application to inspect.

    Returns:
        The paths of every :class:`~fastapi.routing.APIRoute` on the app.
    """

    return {route.path for route in app.routes if isinstance(route, APIRoute)}


def test_create_app_returns_fastapi_instance() -> None:
    """``create_app`` returns a FastAPI application (Req 13.1)."""

    app = create_app()
    assert isinstance(app, FastAPI)


def test_expected_routes_are_mounted() -> None:
    """The agent, documents, and health routes are all mounted (Req 13.1)."""

    app = create_app()
    paths = _api_route_paths(app)
    missing = _EXPECTED_ROUTES - paths
    assert not missing, f"missing expected routes: {sorted(missing)}"


def test_frontend_is_served_as_static_files() -> None:
    """The frontend directory is mounted as static files at the root (Req 13.1)."""

    app = create_app()
    static_mounts = [
        route
        for route in app.routes
        if isinstance(route, Mount) and route.name == "frontend"
    ]
    assert static_mounts, "expected a 'frontend' static-files mount"
    assert static_mounts[0].path == ""  # a mount at "/" is normalized to ""


def test_module_level_app_is_created() -> None:
    """A module-level ``app`` exists so ``uvicorn app.main:app`` works (Req 13.1)."""

    module = importlib.import_module("app.main")
    assert isinstance(module.app, FastAPI)


def test_lifespan_populates_state_and_health_ok() -> None:
    """Startup attaches components to ``app.state`` and ``/health`` is 200 (Req 13.1)."""

    app = create_app()
    # Entering the context manager triggers the lifespan startup handler.
    with TestClient(app) as client:
        for name in _EXPECTED_STATE_COMPONENTS:
            assert hasattr(app.state, name), f"app.state missing {name!r}"

        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert "llm_backend" in body
        assert "backend_ready" in body


def test_required_layers_exist() -> None:
    """The mandated layered package modules all import (Req 13.1)."""

    for layer in _REQUIRED_LAYERS:
        module = importlib.import_module(layer)
        assert module is not None

    # Spot-check the layer files the design calls out explicitly.
    project_root = Path(__file__).resolve().parent.parent
    for relative in (
        "app/api/agent.py",
        "app/api/documents.py",
        "app/api/health.py",
        "app/agent/planner.py",
        "app/agent/executor.py",
        "app/agent/reflector.py",
        "app/agent/tools.py",
        "app/services/llm.py",
        "app/services/docx_builder.py",
        "app/models/schemas.py",
    ):
        assert (project_root / relative).is_file(), f"missing {relative}"
