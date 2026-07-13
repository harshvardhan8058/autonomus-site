"""Pydantic v2 models and enums for the Autonomous Agent Service.

This module is the single source of truth for the data contracts shared across
the service (Req 13.1). It defines:

- The service enums (:class:`RunStatus`, :class:`StepStatus`,
  :class:`IntentClass`, :class:`SSEEventType`), all ``str, Enum`` so they
  serialize as plain strings in JSON and SSE payloads.
- The request / plan / response models (:class:`AgentRequest`,
  :class:`PlanStep`, :class:`Plan`, :class:`AgentResponse`) (Req 1.1, 2.1, 2.2,
  8.1).
- The discriminated Server-Sent-Event models and the :data:`AgentEvent`
  discriminated union keyed on ``type`` (Req 6.1, 6.2).
- The per-run in-memory record :class:`RunState` (Req 16.1).
- The error / health response bodies used by the API layer (Req 1.2, 1.4, 2.6,
  5.4, 5.5, 6.3, 9.4, 16.4).

All models carry full type hints and docstrings (Req 13.2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunStatus(str, Enum):
    """The outcome status of a Run (Req 7.1).

    The value is computed exactly once at Run end by ``derive_status`` and is
    never one of these five members outside that set.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class StepStatus(str, Enum):
    """The lifecycle status of a single :class:`PlanStep`.

    ``SKIPPED`` is a UI-derived terminal state for steps that were never
    executed because a Run terminated while they remained pending (Req 11.5).
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class IntentClass(str, Enum):
    """The intent classification returned by the Guardrail_Validator (Req 1.3)."""

    VALID_DOCUMENT_REQUEST = "valid_document_request"
    MALICIOUS = "malicious"
    NON_DOCUMENT = "non_document"


class SSEEventType(str, Enum):
    """The seven structured event types emitted on the SSE stream (Req 6.1)."""

    PLANNING_STARTED = "planning_started"
    PLAN_CREATED = "plan_created"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    REFLECTION = "reflection"
    RUN_COMPLETED = "run_completed"


# ---------------------------------------------------------------------------
# Request / Plan / Response models
# ---------------------------------------------------------------------------


class AgentRequest(BaseModel):
    """The request body accepted by ``POST /agent`` (Req 1.1).

    The ``request`` field must be a non-blank string: it is required to have at
    least one character by the schema and at least one non-whitespace character
    by the validator, so a blank or whitespace-only body yields HTTP 422
    (Req 1.1, 1.2, property P19).

    Attributes:
        request: The natural-language business request to process.
    """

    request: str = Field(min_length=1, description="Natural-language business request")

    @field_validator("request")
    @classmethod
    def not_blank(cls, v: str) -> str:
        """Reject requests that contain only whitespace.

        Args:
            v: The candidate request string.

        Returns:
            The unmodified request string when it contains a non-whitespace
            character.

        Raises:
            ValueError: If the request is empty or whitespace-only.
        """

        if not v.strip():
            raise ValueError("request must not be empty or whitespace")
        return v


class PlanStep(BaseModel):
    """A single unit of a :class:`Plan` (Req 2.1).

    Attributes:
        step: 1-based sequential step number; defaults to ``0`` and is always
            normalized to its 1-based position by the :class:`Plan` validator,
            so a missing or arbitrary value never fails validation.
        task: Short task name for the step; the internal action/tool intent that
            the executor routes on (for example ``research`` or ``draft_section``).
            Defaults to empty (the executor routes empty tasks to
            ``draft_section``).
        description: Human-readable description of what the step does; defaults
            to empty so a minor omission never fails validation.
        expected_output: Description of the step's expected output; defaults to
            empty so a minor omission never fails validation.
        section_title: Optional, concise, professional, title-case document
            section heading (2-6 words) that appears as the heading of this
            step's section in the rendered Word document. It is distinct from
            :attr:`task` (the internal action/tool intent): when provided it is
            used verbatim as the section heading, and it may be empty for
            assembly steps (for example ``build_docx``) that produce no heading.
        status: Lifecycle status; defaults to :attr:`StepStatus.PENDING`.
        output_summary: Summary of the step's produced output once completed.
        error: Error message recorded when the step fails.
        depends_on: Step numbers this step depends on before it can execute.
    """

    step: int = Field(default=0, ge=0, description="1-based step number (normalized)")
    task: str = ""
    description: str = ""
    expected_output: str = ""
    section_title: str = Field(
        default="",
        description=(
            "Concise, professional, title-case document-section heading rendered "
            "in the .docx; distinct from the internal 'task' intent and may be "
            "empty for assembly steps"
        ),
    )
    status: StepStatus = StepStatus.PENDING
    output_summary: str | None = None
    error: str | None = None
    depends_on: list[int] = Field(
        default_factory=list, description="Step numbers this step needs"
    )


class Plan(BaseModel):
    """A structured, multi-step plan produced by the Planner (Req 2.1, 2.2).

    A plan contains at least two steps; their numbers are normalized to exactly
    ``1..n`` in list order by the validator (Req 2.1, property P4), plus any
    assumptions the Planner made when resolving an ambiguous request (Req 2.3).

    Attributes:
        steps: The ordered plan steps; at least two are required.
        assumptions: Explicit assumptions the Planner made.
    """

    steps: list[PlanStep] = Field(min_length=2, description="At least two steps")
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def steps_sequential(self) -> Plan:
        """Normalize step numbers to exactly ``1, 2, ..., n`` in list order.

        Rather than rejecting a plan whose incoming numbering is out of order,
        duplicated, or missing, this reassigns each step's ``.step`` to its
        1-based position in the list. This preserves the "sequential ``1..n``"
        guarantee (property P4) while tolerating real LLM output.

        Returns:
            The validated plan instance with normalized step numbers.
        """

        for index, step in enumerate(self.steps, start=1):
            step.step = index
        return self


class AgentResponse(BaseModel):
    """The synchronous body returned by ``POST /agent`` when a Run finishes.

    Carries the full Run result contract (Req 8.1): the identifier, the derived
    status, the full plan with per-step status and output summaries, the
    assumptions, resolved clarifications, a summary, and the document URL when
    an artifact exists.

    Attributes:
        run_id: The unique identifier of the Run.
        status: The final derived Run status.
        plan: The full plan including per-step status and output summaries.
        assumptions: Assumptions the Planner made for this Run.
        clarifications_resolved: Clarifications the agent resolved autonomously.
        summary: A human-readable summary of the deliverable.
        document_url: Download URL for the ``.docx`` artifact when it exists.
    """

    run_id: str
    status: RunStatus
    plan: Plan
    assumptions: list[str] = Field(default_factory=list)
    clarifications_resolved: list[str] = Field(default_factory=list)
    summary: str = ""
    document_url: str | None = None


# ---------------------------------------------------------------------------
# SSE event models (discriminated union keyed on ``type``)
# ---------------------------------------------------------------------------


class BaseEvent(BaseModel):
    """Common fields carried by every SSE event (Req 6.2).

    Attributes:
        run_id: The Run the event belongs to.
        type: The event type discriminator.
        timestamp: UTC timestamp when the event was created.
    """

    run_id: str
    type: SSEEventType
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )


class PlanningStartedEvent(BaseEvent):
    """Emitted when the Planner begins producing a plan."""

    type: Literal[SSEEventType.PLANNING_STARTED] = SSEEventType.PLANNING_STARTED


class PlanCreatedEvent(BaseEvent):
    """Emitted once a plan has been created (Req 6.1).

    Attributes:
        plan: The created plan.
        assumptions: Assumptions enumerated during planning.
    """

    type: Literal[SSEEventType.PLAN_CREATED] = SSEEventType.PLAN_CREATED
    plan: Plan
    assumptions: list[str] = Field(default_factory=list)


class StepStartedEvent(BaseEvent):
    """Emitted when a plan step transitions to ``running`` (Req 3.3).

    Attributes:
        step: The step number that started.
        task: The task name of the step that started.
    """

    type: Literal[SSEEventType.STEP_STARTED] = SSEEventType.STEP_STARTED
    step: int
    task: str


class StepCompletedEvent(BaseEvent):
    """Emitted when a plan step completes successfully (Req 3.3).

    Attributes:
        step: The step number that completed.
        output_summary: Summary of the step's output.
    """

    type: Literal[SSEEventType.STEP_COMPLETED] = SSEEventType.STEP_COMPLETED
    step: int
    output_summary: str


class StepFailedEvent(BaseEvent):
    """Emitted when a plan step fails (Req 3.3, 3.4).

    Attributes:
        step: The step number that failed.
        error: The recorded error message.
    """

    type: Literal[SSEEventType.STEP_FAILED] = SSEEventType.STEP_FAILED
    step: int
    error: str


class ReflectionEvent(BaseEvent):
    """Emitted after the Reflector's single-pass self-check (Req 4.3).

    Attributes:
        findings: The Reflector's findings.
        revised_sections: Titles of sections that were revised.
    """

    type: Literal[SSEEventType.REFLECTION] = SSEEventType.REFLECTION
    findings: str
    revised_sections: list[str] = Field(default_factory=list)


class RunCompletedEvent(BaseEvent):
    """Emitted once as the terminal event when a Run finishes (Req 6.1).

    Attributes:
        status: The final derived Run status.
        summary: A human-readable summary of the deliverable.
        document_url: Download URL for the artifact when it exists.
    """

    type: Literal[SSEEventType.RUN_COMPLETED] = SSEEventType.RUN_COMPLETED
    status: RunStatus
    summary: str
    document_url: str | None = None


AgentEvent = Annotated[
    PlanningStartedEvent
    | PlanCreatedEvent
    | StepStartedEvent
    | StepCompletedEvent
    | StepFailedEvent
    | ReflectionEvent
    | RunCompletedEvent,
    Field(discriminator="type"),
]
"""Discriminated union of all SSE events, keyed on the ``type`` field."""


# ---------------------------------------------------------------------------
# RunState (stored per run_id)
# ---------------------------------------------------------------------------


class RunState(BaseModel):
    """The in-memory record the RunStore keeps for each Run (Req 16.1).

    Holds everything the response, the stream, and the document endpoints need,
    isolated per ``run_id``.

    Attributes:
        run_id: The unique identifier of the Run.
        request: The original natural-language request.
        client_ip: The client IP that submitted the request.
        status: The current Run status; starts at :attr:`RunStatus.PENDING`.
        plan: The plan once produced, else ``None``.
        assumptions: Assumptions the Planner made.
        clarifications_resolved: Clarifications resolved autonomously.
        summary: A human-readable summary of the deliverable.
        document_path: Filesystem path of the artifact once built.
        document_url: Download URL once the artifact exists.
        reflection_findings: The Reflector's recorded findings.
        events: Replay buffer of events published for this Run (Req 6).
        created_at: UTC timestamp when the Run was created.
        finished_at: UTC timestamp when the Run finished, else ``None``.
    """

    run_id: str
    request: str
    client_ip: str
    status: RunStatus = RunStatus.PENDING
    plan: Plan | None = None
    assumptions: list[str] = Field(default_factory=list)
    clarifications_resolved: list[str] = Field(default_factory=list)
    summary: str = ""
    document_path: Path | None = None
    document_url: str | None = None
    reflection_findings: str | None = None
    events: list[AgentEvent] = Field(default_factory=list)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
    finished_at: datetime | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


# ---------------------------------------------------------------------------
# Health & error models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """The body returned by ``/health`` and ``/health/ready`` (Req 5.4, 5.5).

    Attributes:
        status: Overall service health indicator.
        llm_backend: The active backend name (``groq`` | ``ollama`` | ``unknown``).
        backend_ready: Whether an LLM backend is resolved and ready.
        detail: Explanatory string when the backend is unresolved (Req 5.5).
    """

    status: str = "ok"
    llm_backend: str
    backend_ready: bool
    detail: str | None = None


class FieldError(BaseModel):
    """A single field-level validation error (Req 1.2).

    Attributes:
        field: The name of the invalid field.
        message: A human-readable explanation of the failure.
    """

    field: str
    message: str


class ValidationErrorBody(BaseModel):
    """The 422 body returned on Pydantic schema-validation failure (Req 1.2).

    Attributes:
        error: A stable error code (``validation_error``).
        fields: The list of field-level errors.
    """

    error: str = "validation_error"
    fields: list[FieldError]


class RejectionErrorBody(BaseModel):
    """The 422 body returned on guardrail rejection (Req 1.4).

    Attributes:
        error: A stable error code (``request_rejected``).
        reason: The intent classification that caused the rejection.
        message: An explanatory message stating the service produces documents.
    """

    error: str = "request_rejected"
    reason: IntentClass
    message: str


class RetryAttempt(BaseModel):
    """A single recorded retry attempt in a planning-failure history (Req 2.6).

    Attributes:
        backend: The backend the attempt targeted.
        attempt: The 1-based attempt number.
        error: The error encountered on this attempt.
        delay_seconds: The backoff delay applied before this attempt.
    """

    backend: str
    attempt: int
    error: str
    delay_seconds: float


class PlanningFailureBody(BaseModel):
    """The 503 body returned when planning fails on all backends (Req 2.6).

    Attributes:
        error: A stable error code (``planning_failed``).
        run_id: The Run whose planning failed.
        reason: A human-readable failure reason.
        retry_history: The recorded retry attempts across backends.
    """

    error: str = "planning_failed"
    run_id: str
    reason: str
    retry_history: list[RetryAttempt]


class DocumentNotFoundBody(BaseModel):
    """The 404 body returned when a document artifact is unavailable (Req 9.4).

    Attributes:
        error: A stable error code (``document_not_found``).
        reason: Why the document is unavailable.
    """

    error: str = "document_not_found"
    reason: Literal["unknown_run", "in_progress", "failed_no_document"]


class RunNotFoundBody(BaseModel):
    """The 404 body returned when a ``run_id`` is unknown (Req 6.3, 16.4).

    Attributes:
        error: A stable error code (``run_not_found``).
        run_id: The unknown Run identifier that was requested.
    """

    error: str = "run_not_found"
    run_id: str
