"""Run orchestration for the Autonomous Agent Service.

This module hosts two things:

- The pure status-derivation function :func:`derive_status`, which computes the
  final :class:`~app.models.schemas.RunStatus` for a Run from its terminal state
  (Req 7). The derivation is intentionally isolated as a pure, deterministic
  function so it can never overstate success (it never reports ``completed`` when
  any step failed) and so it is amenable to property-based testing (Req 7.2, 7.5,
  design properties P1-P3).
- The :class:`Orchestrator`, which wires the Planner -> Executor -> Reflector
  loop end to end: it drives a :class:`~app.models.schemas.RunState` through
  planning, step execution, reflection, document assembly, and a single
  status derivation, publishing the milestone SSE events along the way and
  returning the synchronous :class:`~app.models.schemas.AgentResponse`
  (Req 3.1, 4.1, 7.2, 8.1, 16.1).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.executor import Executor, humanize_section_title
from app.agent.guardrail import GuardrailValidator
from app.agent.planner import Planner, PlanningError
from app.agent.reflector import Reflector
from app.core.event_bus import EventBus
from app.core.logging import StructuredLogger
from app.core.run_store import RunStore
from app.models.schemas import (
    AgentResponse,
    PlanCreatedEvent,
    PlanningStartedEvent,
    PlanStep,
    RunCompletedEvent,
    RunState,
    RunStatus,
    StepStatus,
)
from app.services.docx_builder import DocumentBuilder


def derive_status(
    steps: list[PlanStep],
    artifact_exists: bool,
    summary: str,
) -> RunStatus:
    """Compute the final Run status from terminal Run state (Req 7.1-7.5).

    This is a pure, deterministic function: for identical inputs it always
    returns the identical :class:`RunStatus`, and it has no side effects. It is
    intended to be called exactly once at Run end (Req 7.2), after which the
    computed value is left unmodified.

    The status is derived by the following rules, evaluated in order:

    1. ``completed`` -- every step is :attr:`StepStatus.DONE` **and**
       ``artifact_exists`` is ``True`` **and** ``summary`` is non-empty after
       stripping surrounding whitespace (Req 7.3). Because this requires every
       step to be ``done``, it can never be reached when any step is
       ``failed`` (Req 7.5, property P1).
    2. When at least one step is :attr:`StepStatus.FAILED`, the result is
       ``partial`` if a usable document artifact exists, otherwise ``failed``
       (Req 7.4). A failed step therefore always yields a status other than
       ``completed`` (Req 7.5).
    3. When no step failed but the ``completed`` preconditions are unmet (for
       example a missing summary or a step still pending) and an artifact
       exists, the result is ``partial``.
    4. Otherwise (no usable document and not completable), the result is
       ``failed``.

    Args:
        steps: The plan steps at Run end, each carrying its terminal
            :class:`StepStatus`.
        artifact_exists: Whether a usable document artifact was produced and is
            retrievable (defined as ``document_path is not None and
            document_path.exists()``).
        summary: The generated summary text; considered present only when it
            contains at least one non-whitespace character.

    Returns:
        Exactly one member of :class:`RunStatus`.
    """

    summary_present = bool(summary.strip())
    all_done = all(step.status is StepStatus.DONE for step in steps)
    any_failed = any(step.status is StepStatus.FAILED for step in steps)

    # Rule 1: success is only reported when every precondition holds.
    if all_done and artifact_exists and summary_present:
        return RunStatus.COMPLETED

    # Rule 2: a failed step never yields ``completed`` (property P1).
    if any_failed:
        return RunStatus.PARTIAL if artifact_exists else RunStatus.FAILED

    # Rule 3: no failure, but not fully complete -- usable document => partial.
    if artifact_exists:
        return RunStatus.PARTIAL

    # Rule 4: no usable document and not completable.
    return RunStatus.FAILED



# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

# The component name used in structured decision logs.
_COMPONENT = "orchestrator"

# The directory (relative to the working directory) that a best-effort fallback
# document is written to when the plan produced no ``.docx`` artifact.
_OUTPUT_DIR = "generated"

# The cover-page "prepared by" line used for a fallback-built document.
_DEFAULT_PREPARED_BY = "Autonomous Agent Service"

# The default document title used when no request text is available.
_DEFAULT_TITLE = "Business Deliverable"

# The maximum number of characters of a derived document title / summary excerpt.
_TITLE_MAX_CHARS = 120


class Orchestrator:
    """Wire the Planner -> Executor -> Reflector loop for a Run (Req 3, 4, 7, 8, 16).

    A single :class:`Orchestrator` instance can execute any number of Runs; all
    per-Run state lives on the passed :class:`~app.models.schemas.RunState`,
    keeping concurrently processed Runs isolated by ``run_id`` (Req 16.1). The
    orchestrator emits the milestone SSE events (``planning_started`` ->
    ``plan_created`` -> per-step events -> ``reflection`` -> ``run_completed``)
    through the per-run :class:`~app.core.event_bus.EventBus`, computes the final
    status exactly once via :func:`derive_status` (Req 7.2), persists the Run
    through the :class:`~app.core.run_store.RunStore`, and returns the
    synchronous :class:`~app.models.schemas.AgentResponse` (Req 8.1).

    Attributes:
        _validator: The guardrail validator (held for completeness; intent
            classification is performed at the API boundary before a Run starts).
        _planner: The planner that produces the multi-step plan.
        _executor: The executor that runs the plan's steps through tools.
        _reflector: The reflector that performs a single self-check pass.
        _doc_builder: The document builder used for a best-effort fallback build.
        _store: The run store the Run state is persisted through.
        _events: The per-run event bus milestone events are published to.
        _logger: The structured logger used for decision logging (Req 15.1).
    """

    def __init__(
        self,
        validator: GuardrailValidator,
        planner: Planner,
        executor: Executor,
        reflector: Reflector,
        doc_builder: DocumentBuilder,
        store: RunStore,
        events: EventBus,
        logger: StructuredLogger | None = None,
    ) -> None:
        """Initialize the orchestrator.

        Args:
            validator: The :class:`~app.agent.guardrail.GuardrailValidator`.
                Intent classification runs at the API boundary before a Run is
                created; the validator is retained here for completeness and
                future use.
            planner: The :class:`~app.agent.planner.Planner` used to produce the
                plan for the Run.
            executor: The :class:`~app.agent.executor.Executor` that runs the
                plan's steps through registered tools.
            reflector: The :class:`~app.agent.reflector.Reflector` that performs
                the single self-check pass over the assembled output.
            doc_builder: The :class:`~app.services.docx_builder.DocumentBuilder`
                used only for a best-effort fallback build when the plan produced
                no document artifact.
            store: The :class:`~app.core.run_store.RunStore` the Run state is
                persisted through.
            events: The per-run :class:`~app.core.event_bus.EventBus` milestone
                events are published to.
            logger: An optional :class:`~app.core.logging.StructuredLogger` for
                decision logging. When ``None``, a fresh logger is created.
        """

        self._validator = validator
        self._planner = planner
        self._executor = executor
        self._reflector = reflector
        self._doc_builder = doc_builder
        self._store = store
        self._events = events
        self._logger = logger if logger is not None else StructuredLogger()

    async def execute_run(self, run_state: RunState) -> AgentResponse:
        """Drive ``run_state`` end to end and return its response (Req 3, 4, 7, 8, 16).

        The Run proceeds through the following stages, all mutating only the
        supplied ``run_state`` so concurrent Runs stay isolated by ``run_id``
        (Req 16.1):

        1. Mark the Run ``running`` and emit ``planning_started``.
        2. Produce the plan via the planner, attach it to the Run, copy its
           assumptions onto the Run, and emit ``plan_created``. If planning fails
           on all backends the Run is marked ``failed`` and persisted, then the
           :class:`~app.agent.planner.PlanningError` is re-raised for the API
           layer to map to HTTP 503 (Req 2.6).
        3. Execute the plan's steps through the executor, which emits the
           per-step lifecycle events and records the document path when a
           ``build_docx`` step runs (Req 3).
        4. Assemble the drafted output and run the single reflection pass
           (Req 4). Reflection is best-effort and never fails the Run.
        5. Best-effort: when no document artifact was produced, attempt to build
           one from the assembled sections so a deliverable exists when possible.
        6. Generate a deterministic, non-empty summary and record it on the Run.
        7. Compute ``artifact_exists`` and the ``document_url``, then call
           :func:`derive_status` exactly once to set the final status and record
           ``finished_at`` (Req 7.2).
        8. Persist the Run and emit the terminal ``run_completed`` event.
        9. Build and return the :class:`~app.models.schemas.AgentResponse`
           (Req 8.1).

        Args:
            run_state: The Run state to execute. It must carry the ``run_id`` and
                the original ``request``; the orchestrator mutates it in place.

        Returns:
            The :class:`~app.models.schemas.AgentResponse` describing the finished
            Run.

        Raises:
            PlanningError: If plan generation fails on all backends. The Run is
                marked ``failed`` and persisted before the error is re-raised.
        """

        # Stage 1: begin the Run and announce planning (Req 6.1).
        run_state.status = RunStatus.RUNNING
        self._store.update(run_state)
        await self._safe_publish(
            PlanningStartedEvent(run_id=run_state.run_id), run_state
        )
        self._log(run_state.run_id, "run started; planning", level="INFO")

        # Stage 2: produce the plan (Req 2). A planning failure marks the Run
        # failed, persists it, then re-raises for the API layer (Req 2.6).
        try:
            plan = await self._planner.make_plan(
                run_state.request, run_id=run_state.run_id
            )
        except PlanningError:
            run_state.status = RunStatus.FAILED
            run_state.finished_at = datetime.now(UTC)
            self._store.update(run_state)
            self._log(
                run_state.run_id,
                "planning failed on all backends; run marked failed",
                level="ERROR",
            )
            raise

        run_state.plan = plan
        run_state.assumptions = list(plan.assumptions)
        self._store.update(run_state)
        await self._safe_publish(
            PlanCreatedEvent(
                run_id=run_state.run_id,
                plan=plan,
                assumptions=list(plan.assumptions),
            ),
            run_state,
        )
        self._log(
            run_state.run_id,
            "plan created",
            step_count=len(plan.steps),
            assumption_count=len(plan.assumptions),
        )

        # Stage 3: execute the plan's steps through tools (Req 3). The executor
        # emits per-step events and records the document path on success.
        await self._executor.run(run_state)

        # Stage 4: single-pass reflection over the assembled output (Req 4).
        assembled_output = self._assemble_output(run_state)
        await self._reflector.reflect(run_state, assembled_output)

        # Stage 5: best-effort fallback document build (defensive, never fatal).
        self._maybe_build_fallback_document(run_state)

        # Stage 6: deterministic, non-empty summary (needed for ``completed``).
        artifact_exists = self._artifact_exists(run_state)
        run_state.summary = self._generate_summary(run_state, artifact_exists)

        # Stage 7: derive the final status exactly once (Req 7.2).
        run_state.document_url = (
            f"/documents/{run_state.run_id}.docx" if artifact_exists else None
        )
        plan_steps = run_state.plan.steps if run_state.plan is not None else []
        run_state.status = derive_status(
            plan_steps, artifact_exists, run_state.summary
        )
        run_state.finished_at = datetime.now(UTC)

        # Stage 8: persist and announce completion (Req 6.1, 16.1).
        self._store.update(run_state)
        await self._safe_publish(
            RunCompletedEvent(
                run_id=run_state.run_id,
                status=run_state.status,
                summary=run_state.summary,
                document_url=run_state.document_url,
            ),
            run_state,
        )
        self._log(
            run_state.run_id,
            "run completed",
            status=run_state.status.value,
            artifact_exists=artifact_exists,
        )

        # Stage 9: build the synchronous response (Req 8.1).
        return AgentResponse(
            run_id=run_state.run_id,
            status=run_state.status,
            plan=run_state.plan,
            assumptions=list(run_state.assumptions),
            clarifications_resolved=list(run_state.clarifications_resolved),
            summary=run_state.summary,
            document_url=run_state.document_url,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _artifact_exists(run_state: RunState) -> bool:
        """Return whether a usable document artifact exists for the Run (Req 7).

        Mirrors the definition used by :func:`derive_status`: an artifact exists
        when ``document_path`` is set and the file is present on disk.

        Args:
            run_state: The Run state to inspect.

        Returns:
            ``True`` when ``run_state.document_path`` is set and exists.
        """

        path = run_state.document_path
        try:
            return path is not None and path.exists()
        except OSError:  # pragma: no cover - defensive filesystem guard
            return False

    def _assemble_output(self, run_state: RunState) -> str:
        """Concatenate the executed steps' content into one review string (Req 4.1).

        Each completed step contributes a small titled block built from its task
        name and recorded output summary, giving the reflector a single view of
        the assembled deliverable to compare against the original request.

        Args:
            run_state: The Run state whose plan steps are assembled.

        Returns:
            The assembled output text, possibly empty when no step produced
            content.
        """

        plan = run_state.plan
        if plan is None:
            return ""
        parts: list[str] = []
        for step in sorted(plan.steps, key=lambda s: s.step):
            summary = (step.output_summary or "").strip()
            if step.status is StepStatus.DONE and summary:
                heading = (step.task or f"Step {step.step}").strip()
                parts.append(f"## {heading}\n{summary}")
        return "\n\n".join(parts)

    def _generate_summary(
        self, run_state: RunState, artifact_exists: bool
    ) -> str:
        """Build a concise, deterministic, non-empty Run summary (Req 8.1).

        The summary is derived purely from the Run's terminal state (step counts
        and whether a document exists) so it is stable and requires no extra LLM
        call. It is always non-empty, which is a precondition for a ``completed``
        status (Req 7.3).

        Args:
            run_state: The finished Run state.
            artifact_exists: Whether a usable document artifact was produced.

        Returns:
            A single-line human-readable summary string.
        """

        plan = run_state.plan
        steps = plan.steps if plan is not None else []
        total = len(steps)
        done = sum(1 for s in steps if s.status is StepStatus.DONE)
        failed = sum(1 for s in steps if s.status is StepStatus.FAILED)

        document_clause = (
            "a downloadable Word document was produced"
            if artifact_exists
            else "no document artifact was produced"
        )
        failure_clause = f", {failed} failed" if failed else ""
        return (
            f"Completed {done} of {total} planned step(s){failure_clause}; "
            f"{document_clause}."
        )

    def _maybe_build_fallback_document(self, run_state: RunState) -> None:
        """Best-effort: build a document when none was produced (defensive).

        When the plan produced no ``.docx`` artifact (no ``build_docx`` step, or
        that step failed) but at least one content step completed, assemble the
        completed sections into a document so a deliverable exists when possible.
        This is strictly best-effort: any failure (including a failing document
        builder) is caught and logged, and never fails the Run. When an artifact
        already exists, this is a no-op.

        Args:
            run_state: The Run state a fallback document may be built for.
        """

        if self._artifact_exists(run_state):
            return
        plan = run_state.plan
        if plan is None:
            return

        try:
            sections = self._collect_sections(plan.steps)
            if not sections:
                return
            output_path = Path(_OUTPUT_DIR) / f"agent-run-{run_state.run_id}.docx"
            built = self._doc_builder.build(
                title=self._derive_title(run_state),
                prepared_by=_DEFAULT_PREPARED_BY,
                sections=sections,
                output_path=output_path,
            )
            run_state.document_path = Path(built)
            self._log(
                run_state.run_id,
                "built fallback document from assembled sections",
                section_count=len(sections),
            )
        except Exception as exc:  # noqa: BLE001 - fallback must never fail a Run
            self._log(
                run_state.run_id,
                "best-effort fallback document build failed and was suppressed",
                level="WARNING",
                error=str(exc),
            )

    @staticmethod
    def _collect_sections(steps: list[PlanStep]) -> list[dict[str, Any]]:
        """Collect section payloads from completed content steps.

        Args:
            steps: The plan steps to derive sections from.

        Returns:
            An ordered list of ``{"heading": ..., "body": ...}`` section mappings
            for each completed step that produced an output summary. Headings are
            human-readable, title-cased titles (never raw tool identifiers),
            consistent with the Executor's section accumulation.
        """

        sections: list[dict[str, Any]] = []
        for step in sorted(steps, key=lambda s: s.step):
            summary = (step.output_summary or "").strip()
            if step.status is StepStatus.DONE and summary:
                heading = humanize_section_title(
                    step.task or "", step.description or "", step.step
                )
                sections.append({"heading": heading, "body": summary})
        return sections

    @staticmethod
    def _derive_title(run_state: RunState) -> str:
        """Derive a single-line document title from the Run's request.

        Args:
            run_state: The Run state the title is derived from.

        Returns:
            A single-line title truncated to a reasonable length, falling back to
            a default when no usable request text is available.
        """

        request = (run_state.request or "").strip()
        if not request:
            return _DEFAULT_TITLE
        title = " ".join(request.splitlines()[0].split())
        if not title:
            return _DEFAULT_TITLE
        if len(title) > _TITLE_MAX_CHARS:
            title = title[: _TITLE_MAX_CHARS - 1].rstrip() + "\u2026"
        return title

    async def _safe_publish(self, event: Any, run_state: RunState) -> None:
        """Publish an event, suppressing any error (best-effort — Req 6.1).

        The event bus is designed never to raise, but a substitute or broken bus
        supplied for testing might. Either way an emission failure must never
        crash a Run: it is caught, logged best-effort, and suppressed.

        Args:
            event: The SSE event to publish.
            run_state: The owning Run state (for replay buffering).
        """

        try:
            await self._events.publish(event, run_state)
        except Exception as exc:  # noqa: BLE001 - emission must never fail a Run
            self._log(
                run_state.run_id,
                "best-effort event publish failed and was suppressed",
                level="ERROR",
                error=str(exc),
            )

    def _log(
        self, run_id: str, decision: str, *, level: str = "INFO", **fields: Any
    ) -> None:
        """Emit a decision log, suppressing any logger error (Req 15.2).

        Args:
            run_id: The Run the decision belongs to.
            decision: A human-readable description of the decision.
            level: The severity level for the entry.
            **fields: Additional structured fields for the entry.
        """

        # logging must never fail a Run
        with contextlib.suppress(Exception):
            self._logger.decision(_COMPONENT, run_id, decision, level=level, **fields)
