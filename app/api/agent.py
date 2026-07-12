"""Agent endpoints: ``POST /agent`` and ``GET /agent/{run_id}/stream`` (Req 1, 2, 6, 8, 16).

This module hosts the two agent-facing endpoints:

- :func:`create_agent_run` (``POST /agent``) — the synchronous run endpoint. It
  rate-limits per client IP (Req 1.6, 1.7), validates the request body against
  the :class:`~app.models.schemas.AgentRequest` schema (Req 1.1, 1.2), screens
  intent through the :class:`~app.agent.guardrail.GuardrailValidator` (Req 1.3,
  1.4) — emitting a ``security_event`` on a malicious request (Req 1.5) — then
  creates a :class:`~app.models.schemas.RunState` and drives the run through the
  :class:`~app.agent.orchestrator.Orchestrator`, returning the finished
  :class:`~app.models.schemas.AgentResponse` (Req 8.1). A
  :class:`~app.agent.planner.PlanningError` is mapped to HTTP 503 with a
  :class:`~app.models.schemas.PlanningFailureBody` (Req 2.6).
- :func:`stream_agent_run` (``GET /agent/{run_id}/stream``) — the Server-Sent
  Events endpoint. It returns 404 without opening a stream for an unknown run
  (Req 6.3), otherwise it subscribes to the run's event bus, replays the
  buffered events, then streams live events until the terminal ``run_completed``
  event, emitting a keep-alive comment on idle and cleaning up on disconnect
  (Req 6.1, 6.2, 16.2).

The rate-limit check is performed before body validation so that an over-limit
client is rejected regardless of body shape, matching the design's request
flow. Because of that ordering the body is read and validated explicitly (rather
than via a typed path/body parameter), which also lets the endpoint return the
structured :class:`~app.models.schemas.ValidationErrorBody` shape on a schema
failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.agent.guardrail import GuardrailValidator
from app.agent.orchestrator import Orchestrator
from app.agent.planner import PlanningError
from app.api.deps import (
    get_event_bus,
    get_guardrail,
    get_logger,
    get_orchestrator,
    get_rate_limiter,
    get_run_store,
)
from app.core.event_bus import EventBus
from app.core.logging import StructuredLogger
from app.core.rate_limiter import RateDecision, RateLimiter
from app.core.run_store import RunStore
from app.models.schemas import (
    AgentEvent,
    AgentRequest,
    FieldError,
    IntentClass,
    PlanningFailureBody,
    RejectionErrorBody,
    RunNotFoundBody,
    RunState,
    SSEEventType,
    ValidationErrorBody,
)

router = APIRouter(tags=["agent"])

# The Retry-After value (in seconds) used when the rate limiter cannot compute a
# best-effort delay; the request is still rejected with HTTP 429 (Req 1.7).
_DEFAULT_RETRY_AFTER_SECONDS = 60

# The idle interval, in seconds, after which an SSE keep-alive comment is emitted
# so intermediary proxies do not close an otherwise-idle stream.
_KEEPALIVE_SECONDS = 15.0

# The explanatory message returned when the guardrail rejects a request (Req 1.4).
_REJECTION_MESSAGE = (
    "This request was rejected. The service only produces Microsoft Word (.docx) "
    "business document deliverables; please submit a request for a document."
)


# ---------------------------------------------------------------------------
# POST /agent (Req 1, 2, 8)
# ---------------------------------------------------------------------------


@router.post("/agent")
async def create_agent_run(
    request: Request,
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    guardrail: GuardrailValidator = Depends(get_guardrail),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    store: RunStore = Depends(get_run_store),
    logger: StructuredLogger = Depends(get_logger),
) -> JSONResponse:
    """Validate, screen, and execute an agent run synchronously (Req 1, 2, 8).

    The request is processed in the following order:

    1. **Rate limit** — the client IP (honoring the first ``X-Forwarded-For``
       hop) is checked against the per-IP sliding window; an over-limit request
       is rejected with HTTP 429 and a ``Retry-After`` header, still 429 even
       when the delay cannot be computed (Req 1.6, 1.7).
    2. **Schema validation** — the JSON body is validated against
       :class:`AgentRequest`; a schema failure yields HTTP 422 with a
       :class:`ValidationErrorBody` (Req 1.1, 1.2).
    3. **Guardrail** — the request intent is classified; a ``malicious`` request
       additionally emits a ``security_event`` (hashing the request rather than
       logging its payload), and any non-``valid_document_request`` intent
       yields HTTP 422 with a :class:`RejectionErrorBody` (Req 1.3, 1.4, 1.5).
    4. **Execute** — a :class:`RunState` is created and driven through the
       orchestrator; the finished :class:`~app.models.schemas.AgentResponse` is
       returned with HTTP 200 (Req 8.1). A :class:`PlanningError` is mapped to
       HTTP 503 with a :class:`PlanningFailureBody` (Req 2.6).

    Args:
        request: The incoming HTTP request.
        rate_limiter: The per-IP rate limiter (injected from app state).
        guardrail: The guardrail validator (injected from app state).
        orchestrator: The run orchestrator (injected from app state).
        store: The run store (injected from app state).
        logger: The structured logger (injected from app state).

    Returns:
        A :class:`~fastapi.responses.JSONResponse`: 200 with the agent response,
        or 422 / 429 / 503 with the corresponding structured error body.
    """

    client_ip = _extract_client_ip(request)

    # 1. Rate-limit check (before validation) — Req 1.6, 1.7.
    decision = rate_limiter.check(client_ip)
    if not decision.allowed:
        return _rate_limited_response(decision)

    # 2. Schema validation — Req 1.1, 1.2.
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed/empty JSON is a validation error
        payload = None
    try:
        agent_request = AgentRequest.model_validate(
            payload if payload is not None else {}
        )
    except ValidationError as exc:
        return _validation_error_response(exc)

    # 3. Guardrail classification — Req 1.3, 1.4, 1.5.
    intent = await guardrail.classify(agent_request.request)
    if intent is not IntentClass.VALID_DOCUMENT_REQUEST:
        if intent is IntentClass.MALICIOUS:
            _emit_security_event(logger, client_ip, agent_request.request)
        return _rejection_response(intent)

    # 4. Create the run and execute it end to end — Req 8.1, 2.6.
    run_id = uuid4().hex
    run_state = store.create(
        run_id=run_id, request=agent_request.request, client_ip=client_ip
    )
    try:
        response = await orchestrator.execute_run(run_state)
    except PlanningError as exc:
        return _planning_failure_response(run_id, exc)

    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# GET /agent/{run_id}/stream (Req 6, 16.2)
# ---------------------------------------------------------------------------


@router.get("/agent/{run_id}/stream", response_model=None)
async def stream_agent_run(
    run_id: str,
    store: RunStore = Depends(get_run_store),
    event_bus: EventBus = Depends(get_event_bus),
) -> StreamingResponse | JSONResponse:
    """Stream a run's events as Server-Sent Events (Req 6.1, 6.2, 6.3, 16.2).

    If ``run_id`` does not correspond to a known run, HTTP 404 with a
    :class:`RunNotFoundBody` is returned **without** opening a stream (Req 6.3).
    Otherwise a ``text/event-stream`` response is returned that subscribes to the
    run's event bus, first replays every buffered event (so a late subscriber
    still sees the full history), then yields live events until the terminal
    ``run_completed`` event. A keep-alive comment is emitted on idle, and the
    subscription is cleaned up on client disconnect or after the terminal event
    (Req 6.1, 16.2).

    Args:
        run_id: The identifier of the run to stream.
        store: The run store (injected from app state).
        event_bus: The event bus (injected from app state).

    Returns:
        A :class:`~fastapi.responses.StreamingResponse` for a known run, or a
        404 :class:`~fastapi.responses.JSONResponse` for an unknown run.
    """

    run_state = store.get(run_id)
    if run_state is None:
        body = RunNotFoundBody(run_id=run_id)
        return JSONResponse(status_code=404, content=body.model_dump(mode="json"))

    generator = _event_stream(run_state, event_bus, run_id)
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _event_stream(
    run_state: RunState, event_bus: EventBus, run_id: str
) -> AsyncIterator[str]:
    """Yield SSE frames for a run: replay buffered events, then stream live ones.

    Subscribes to the run's event bus and immediately snapshots the replay
    buffer (there is no ``await`` between the two, so the snapshot and the live
    queue partition the events with no overlap). Buffered events are emitted
    first; if the buffer already contains the terminal ``run_completed`` event
    (the run finished before this subscriber connected), the generator returns
    after replay. Otherwise it consumes the live queue, emitting a keep-alive
    comment whenever the queue is idle for :data:`_KEEPALIVE_SECONDS`, and
    returns after the terminal event. The subscription is always removed in the
    ``finally`` block, covering both client disconnect (cancellation /
    ``GeneratorExit``) and normal termination (Req 6.1, 6.2, 16.2).

    Args:
        run_state: The run whose buffered events are replayed.
        event_bus: The event bus to subscribe to for live events.
        run_id: The run identifier the subscription is keyed on.

    Yields:
        SSE-framed strings (event frames and keep-alive comments).
    """

    queue = await event_bus.subscribe(run_id)
    index = 0
    try:
        # Replay the buffered history first (Req 6.1, late subscriber).
        buffered = list(run_state.events)
        for event in buffered:
            yield _format_sse(event, index)
            index += 1
            if event.type is SSEEventType.RUN_COMPLETED:
                # The run already finished; replaying the buffer is sufficient
                # to terminate the stream (Req 6, already-finished case).
                return

        # Stream live events until the terminal event, with idle keep-alives.
        while True:
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=_KEEPALIVE_SECONDS
                )
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield _format_sse(event, index)
            index += 1
            if event.type is SSEEventType.RUN_COMPLETED:
                return
    finally:
        # Clean up the subscription on disconnect or after the terminal event.
        event_bus.unsubscribe(run_id, queue)


def _format_sse(event: AgentEvent, index: int) -> str:
    """Serialize an event into a single SSE frame (Req 6.2).

    The frame mirrors the event ``type`` in the ``event:`` line, carries a
    monotonic ``id:`` for ``Last-Event-ID`` resumption, and serializes the full
    event as JSON on the ``data:`` line, terminated by a blank line.

    Args:
        event: The event to serialize.
        index: The monotonic frame index used as the SSE ``id``.

    Returns:
        The formatted SSE frame string.
    """

    return (
        f"event: {event.type.value}\n"
        f"id: {index}\n"
        f"data: {event.model_dump_json()}\n\n"
    )


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _extract_client_ip(request: Request) -> str:
    """Return the client IP, honoring the first ``X-Forwarded-For`` hop (Req 1.6).

    When an ``X-Forwarded-For`` header is present, its first (left-most) hop is
    used as the originating client IP; otherwise the direct peer address is
    used, falling back to ``"unknown"`` when it cannot be determined.

    Args:
        request: The incoming HTTP request.

    Returns:
        The resolved client IP string.
    """

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first_hop = forwarded.split(",")[0].strip()
        if first_hop:
            return first_hop
    client = request.client
    if client is not None and client.host:
        return client.host
    return "unknown"


def _rate_limited_response(decision: RateDecision) -> JSONResponse:
    """Build the HTTP 429 response with a ``Retry-After`` header (Req 1.6, 1.7).

    Uses the limiter's best-effort ``retry_after`` when available, otherwise a
    documented default. The request is rejected with 429 in both cases so that
    an over-limit request is always rejected even when the delay cannot be
    computed (Req 1.7).

    Args:
        decision: The denying :class:`RateDecision`.

    Returns:
        A 429 :class:`~fastapi.responses.JSONResponse` carrying ``Retry-After``.
    """

    retry_after = decision.retry_after
    if retry_after is not None and retry_after > 0:
        header_value = str(int(math.ceil(retry_after)))
    else:
        header_value = str(_DEFAULT_RETRY_AFTER_SECONDS)
    return JSONResponse(
        status_code=429,
        content={"error": "rate_limited", "retry_after": header_value},
        headers={"Retry-After": header_value},
    )


def _validation_error_response(exc: ValidationError) -> JSONResponse:
    """Build the HTTP 422 :class:`ValidationErrorBody` response (Req 1.2).

    Args:
        exc: The Pydantic validation error raised while parsing the body.

    Returns:
        A 422 :class:`~fastapi.responses.JSONResponse` identifying the invalid
        fields.
    """

    fields: list[FieldError] = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ()) if part != "body"]
        field = ".".join(location) or "body"
        fields.append(
            FieldError(field=field, message=str(error.get("msg", "invalid value")))
        )
    body = ValidationErrorBody(fields=fields)
    return JSONResponse(status_code=422, content=body.model_dump(mode="json"))


def _rejection_response(intent: IntentClass) -> JSONResponse:
    """Build the HTTP 422 :class:`RejectionErrorBody` response (Req 1.4).

    Args:
        intent: The intent classification that caused the rejection.

    Returns:
        A 422 :class:`~fastapi.responses.JSONResponse` explaining the rejection.
    """

    body = RejectionErrorBody(reason=intent, message=_REJECTION_MESSAGE)
    return JSONResponse(status_code=422, content=body.model_dump(mode="json"))


def _planning_failure_response(run_id: str, exc: PlanningError) -> JSONResponse:
    """Build the HTTP 503 :class:`PlanningFailureBody` response (Req 2.6).

    Args:
        run_id: The identifier of the run whose planning failed.
        exc: The planning error carrying the reason and retry history.

    Returns:
        A 503 :class:`~fastapi.responses.JSONResponse` with the run id, reason,
        and retry history.
    """

    body = PlanningFailureBody(
        run_id=run_id, reason=exc.reason, retry_history=exc.retry_history
    )
    return JSONResponse(status_code=503, content=body.model_dump(mode="json"))


def _emit_security_event(
    logger: StructuredLogger, client_ip: str, request_text: str
) -> None:
    """Emit a ``security_event`` for a malicious request (Req 1.5).

    The request is recorded as a SHA-256 hash rather than as its verbatim
    payload, so the malicious content is never persisted to logs (property P18).

    Args:
        logger: The structured logger to emit through.
        client_ip: The IP address of the rejected client.
        request_text: The request text to hash (never logged verbatim).
    """

    request_hash = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
    logger.security_event(
        client_ip=client_ip,
        request_hash=request_hash,
        reason=IntentClass.MALICIOUS.value,
    )
