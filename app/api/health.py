"""Health endpoints: ``GET /health`` and ``GET /health/ready`` (Req 5.4, 5.5, 5.6).

This module exposes the two health probes:

- :func:`health` (``GET /health``) — a liveness probe that **always** returns
  HTTP 200 with a :class:`~app.models.schemas.HealthResponse` reporting the
  active LLM backend and whether it is ready. When the backend is unresolved (or
  a probe raises), it reports ``llm_backend="unknown"``, ``backend_ready=false``,
  and an explanatory ``detail`` string, and never raises (Req 5.4, 5.5).
- :func:`readiness` (``GET /health/ready``) — a readiness probe that returns
  HTTP 200 when an LLM backend is reachable and HTTP 503 when none is reachable
  (Req 5.6).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import get_llm_service
from app.models.schemas import HealthResponse
from app.services.llm import LLMService

router = APIRouter(tags=["health"])

# The backend name reported when the active backend has not been resolved.
_UNKNOWN_BACKEND = "unknown"

# The explanatory detail reported when the backend is unresolved (Req 5.5).
_UNRESOLVED_DETAIL = (
    "The LLM backend has not been resolved yet; no backend is currently ready."
)

# The explanatory detail reported when no backend is reachable (Req 5.6).
_UNREACHABLE_DETAIL = "No LLM backend is currently reachable."


@router.get("/health")
async def health(
    llm: LLMService = Depends(get_llm_service),
) -> HealthResponse:
    """Report service health and the active LLM backend, always 200 (Req 5.4, 5.5).

    Probes the LLM service for its active backend and reachability. When the
    backend is resolved, its name and readiness are reported. When the backend
    is unresolved — or the probe raises — the response reports
    ``llm_backend="unknown"``, ``backend_ready=false``, and an explanatory
    ``detail`` string. This endpoint never raises and always returns HTTP 200.

    Args:
        llm: The LLM service (injected from app state).

    Returns:
        A :class:`~app.models.schemas.HealthResponse` describing service health.
    """

    backend, reachable = await _probe(llm)
    if backend == _UNKNOWN_BACKEND:
        return HealthResponse(
            status="ok",
            llm_backend=_UNKNOWN_BACKEND,
            backend_ready=False,
            detail=_UNRESOLVED_DETAIL,
        )
    return HealthResponse(
        status="ok", llm_backend=backend, backend_ready=reachable
    )


@router.get("/health/ready")
async def readiness(
    llm: LLMService = Depends(get_llm_service),
) -> JSONResponse:
    """Report readiness: 200 when a backend is reachable, else 503 (Req 5.6).

    Args:
        llm: The LLM service (injected from app state).

    Returns:
        A 200 :class:`~fastapi.responses.JSONResponse` with the
        :class:`~app.models.schemas.HealthResponse` when a backend is reachable,
        or a 503 response when no backend is reachable.
    """

    backend, reachable = await _probe(llm)
    if reachable and backend != _UNKNOWN_BACKEND:
        body = HealthResponse(status="ok", llm_backend=backend, backend_ready=True)
        return JSONResponse(status_code=200, content=body.model_dump(mode="json"))

    reported_backend = backend if backend != _UNKNOWN_BACKEND else _UNKNOWN_BACKEND
    detail = (
        _UNRESOLVED_DETAIL if backend == _UNKNOWN_BACKEND else _UNREACHABLE_DETAIL
    )
    body = HealthResponse(
        status="unavailable",
        llm_backend=reported_backend,
        backend_ready=False,
        detail=detail,
    )
    return JSONResponse(status_code=503, content=body.model_dump(mode="json"))


async def _probe(llm: LLMService) -> tuple[str, bool]:
    """Probe the LLM backend name and reachability, never raising (Req 5.5).

    Args:
        llm: The LLM service to probe.

    Returns:
        A ``(backend_name, reachable)`` tuple. On any error the tuple is
        ``("unknown", False)`` so the health endpoints degrade gracefully.
    """

    try:
        backend, reachable = await llm.health()
    except Exception:  # noqa: BLE001 - health probing must never raise
        return (_UNKNOWN_BACKEND, False)
    return (backend, bool(reachable))
