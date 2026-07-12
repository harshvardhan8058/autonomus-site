"""Plan execution via tool calling (Req 3).

This module defines :class:`Executor`, the third stage of the agent loop. Given a
:class:`~app.models.schemas.RunState` carrying a validated
:class:`~app.models.schemas.Plan`, the executor walks the plan's steps in
ascending step order and, for each step whose dependencies are satisfied,
dispatches a registered tool through the :class:`~app.agent.tools.ToolRegistry`.

For every executed step the executor drives the lifecycle
``pending -> running -> done | failed`` and emits the matching SSE event
(``step_started`` then exactly one of ``step_completed`` / ``step_failed``)
through the per-run :class:`~app.core.event_bus.EventBus` (Req 3.1, 3.3). A tool
invocation is retried a bounded number of times on exception; if it still fails
the step is marked ``failed``, its error is recorded, and execution **continues**
with the remaining steps whose dependencies are all ``done`` — a single failed
step never halts the whole run (Req 3.4). Steps whose dependencies can never be
satisfied are left ``pending`` (the UI renders them ``skipped``, Req 11.5).

All bookkeeping (event emission, decision logging, summary recording, section
accumulation) is performed on a **best-effort** basis: any bookkeeping error is
caught and logged so that observability degrades without ever crashing a Run
(Req 3.5). Because every step's terminal status is assigned before its
bookkeeping runs, a bookkeeping failure can never leave a started step without a
terminal status.

The executor also accumulates the drafted content produced by the content tools
(``research``, ``draft_section``, ``generate_table_data``) into an ordered list
of section payloads, so that when a ``build_docx`` step runs it receives the
assembled sections to render into the final ``.docx`` deliverable.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from app.agent.tools import ToolRegistry, ToolResult
from app.core.event_bus import EventBus
from app.core.logging import StructuredLogger
from app.models.schemas import (
    Plan,
    PlanStep,
    RunState,
    StepCompletedEvent,
    StepFailedEvent,
    StepStartedEvent,
    StepStatus,
)

# The component name used in structured decision logs.
_COMPONENT = "executor"

# The number of additional attempts made after the first attempt fails, i.e. a
# tool is invoked at most ``1 + _DEFAULT_MAX_RETRIES`` times before the step is
# marked failed (Req 3.4).
_DEFAULT_MAX_RETRIES = 3

# The four standard tool names the keyword router can select from.
_TOOL_RESEARCH = "research"
_TOOL_DRAFT_SECTION = "draft_section"
_TOOL_GENERATE_TABLE_DATA = "generate_table_data"
_TOOL_BUILD_DOCX = "build_docx"

# The default document title / prepared-by line used when building the .docx and
# no better value can be derived from the Run state.
_DEFAULT_TITLE = "Business Deliverable"
_DEFAULT_PREPARED_BY = "Autonomous Agent Service"

# The maximum number of characters of a derived document title.
_TITLE_MAX_CHARS = 120

# The directory (relative to the working directory) generated documents are
# written to when the Executor derives the output path from the Run state.
_OUTPUT_DIR = "generated"

# Keyword groups used by the fallback router when ``step.task`` is not itself a
# registered tool name. Checked in this order so more specific intents win.
_ROUTER_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        _TOOL_BUILD_DOCX,
        (
            "build_docx",
            "build docx",
            "docx",
            "assemble",
            "compile",
            "word document",
            "final document",
        ),
    ),
    (
        _TOOL_GENERATE_TABLE_DATA,
        ("generate_table_data", "table", "tabular", "spreadsheet", "matrix"),
    ),
    (
        _TOOL_RESEARCH,
        ("research", "gather", "investigate", "analyze", "analyse", "explore", "study"),
    ),
    (
        _TOOL_DRAFT_SECTION,
        ("draft", "write", "section", "compose", "author", "narrative"),
    ),
)


class Executor:
    """Execute a plan's steps through registered tools (Req 3).

    The executor is constructed with the tool registry it dispatches through,
    the per-run event bus it publishes lifecycle events to, and an optional
    structured logger for decision logging. A single instance can execute any
    number of Runs; all per-Run state lives on the passed
    :class:`~app.models.schemas.RunState`.

    Attributes:
        _tools: The registry the executor dispatches plan steps through.
        _events: The per-run event bus lifecycle events are published to.
        _logger: The structured logger used for decision logging.
        _max_retries: The number of additional attempts after the first failure.
    """

    def __init__(
        self,
        tools: ToolRegistry,
        events: EventBus,
        logger: StructuredLogger | None = None,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        """Initialize the executor.

        Args:
            tools: The :class:`~app.agent.tools.ToolRegistry` the executor
                dispatches each plan step through.
            events: The per-run :class:`~app.core.event_bus.EventBus` lifecycle
                events are published to.
            logger: An optional :class:`~app.core.logging.StructuredLogger` for
                decision logging. When ``None``, a fresh logger is created.
            max_retries: The number of additional attempts made after the first
                attempt fails before a step is marked failed (defaults to 3).
        """

        self._tools = tools
        self._events = events
        self._logger = logger if logger is not None else StructuredLogger()
        self._max_retries = max(0, max_retries)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, run_state: RunState) -> None:
        """Execute the plan attached to ``run_state`` (Req 3.1, 3.3, 3.4, 3.5).

        Walks the plan's steps in ascending step order. For each step whose
        dependencies are all ``done``, the executor sets the step ``running``,
        emits a ``step_started`` event, dispatches the routed tool with bounded
        retries, and then sets the step ``done`` (emitting ``step_completed``) or
        ``failed`` (emitting ``step_failed``). A failed step never halts the run:
        the remaining dependency-satisfied steps still execute (Req 3.4). Steps
        whose dependencies can never be satisfied are left ``pending`` (Req 3.4,
        rendered ``skipped`` by the UI).

        All event emission, logging, summary recording, and section accumulation
        are best-effort: a bookkeeping error is caught and never crashes the Run,
        and it can never prevent a started step from receiving a terminal status
        (Req 3.5).

        Args:
            run_state: The Run state carrying the plan to execute and the fields
                (``document_path``) the executor updates as it runs.
        """

        plan = run_state.plan
        if plan is None:
            self._safe_log(
                run_state.run_id,
                "no plan attached to run; nothing to execute",
                level="WARNING",
            )
            return

        # Accumulated section payloads from content-producing steps, assembled
        # into the final document when a ``build_docx`` step runs.
        sections: list[dict[str, Any]] = []
        tool_names = set(self._tools.names())

        for step in sorted(plan.steps, key=lambda s: s.step):
            if not self._dependencies_satisfied(step, plan):
                # Leave the step ``pending``; its dependencies are unsatisfied
                # (a dependency failed or was itself skipped). The UI renders
                # such steps as ``skipped`` (Req 3.4, 11.5).
                self._safe_log(
                    run_state.run_id,
                    f"skipping step {step.step}: dependencies not satisfied",
                    level="WARNING",
                    step=step.step,
                    depends_on=list(step.depends_on),
                )
                continue

            await self._execute_step(step, plan, run_state, sections, tool_names)

    def _dependencies_satisfied(self, step: PlanStep, plan: Plan) -> bool:
        """Return whether every dependency of ``step`` has completed (Req 3.4).

        A step's dependencies are satisfied when every step number listed in
        :attr:`~app.models.schemas.PlanStep.depends_on` refers to a step in the
        plan whose status is :attr:`~app.models.schemas.StepStatus.DONE`. A
        dependency on a missing, failed, skipped, or not-yet-completed step is
        treated as unsatisfied so the dependent step is left ``pending``.

        Args:
            step: The plan step whose dependencies are being checked.
            plan: The plan the step belongs to.

        Returns:
            ``True`` when all of ``step``'s dependencies are ``done`` (including
            the vacuous case of no dependencies), ``False`` otherwise.
        """

        if not step.depends_on:
            return True
        status_by_number = {s.step: s.status for s in plan.steps}
        return all(
            status_by_number.get(dependency) is StepStatus.DONE
            for dependency in step.depends_on
        )

    # ------------------------------------------------------------------
    # Per-step execution
    # ------------------------------------------------------------------

    async def _execute_step(
        self,
        step: PlanStep,
        plan: Plan,
        run_state: RunState,
        sections: list[dict[str, Any]],
        tool_names: set[str],
    ) -> None:
        """Execute a single dependency-satisfied step end to end (Req 3.3).

        Sets the step ``running`` and emits ``step_started``, dispatches the
        routed tool with bounded retries, then assigns the terminal status and
        emits the terminal event. The terminal status is assigned before any
        best-effort bookkeeping so a bookkeeping failure can never leave the step
        without a terminal status (Req 3.5).

        Args:
            step: The step to execute.
            plan: The plan the step belongs to.
            run_state: The owning Run state.
            sections: The accumulated section payloads (mutated in place on
                success of a content step).
            tool_names: The set of registered tool names, used for routing.
        """

        step.status = StepStatus.RUNNING
        self._safe_log(
            run_state.run_id,
            f"executing step {step.step}: {step.task}",
            step=step.step,
            task=step.task,
        )
        await self._safe_publish(
            StepStartedEvent(run_id=run_state.run_id, step=step.step, task=step.task),
            run_state,
        )

        tool_name = self._resolve_tool_name(step, tool_names)
        kwargs = self._build_kwargs(tool_name, step, run_state, sections)
        result, error = await self._invoke_with_retry(tool_name, kwargs, run_state, step)

        if error is None:
            # Assign the terminal status first (the critical state transition),
            # then perform best-effort bookkeeping (Req 3.3, 3.5).
            step.status = StepStatus.DONE
            await self._record_success(step, tool_name, result, run_state, sections)
        else:
            step.status = StepStatus.FAILED
            await self._record_failure(step, error, run_state)

    def _resolve_tool_name(self, step: PlanStep, tool_names: set[str]) -> str:
        """Map a plan step to a registered tool name (Req 3.1).

        When ``step.task`` is itself a registered tool name it is used directly.
        Otherwise a small keyword router inspects the task and description and
        selects ``research`` / ``draft_section`` / ``generate_table_data`` /
        ``build_docx`` by keyword, defaulting to ``draft_section`` when nothing
        else matches.

        Args:
            step: The plan step to route.
            tool_names: The set of registered tool names.

        Returns:
            The name of the tool to dispatch the step to.
        """

        task = (step.task or "").strip()
        if task in tool_names:
            return task

        haystack = f"{step.task or ''} {step.description or ''}".lower()
        for tool_name, keywords in _ROUTER_KEYWORDS:
            if tool_name in tool_names and any(keyword in haystack for keyword in keywords):
                return tool_name

        # Sensible default: drafting a section is the most broadly applicable
        # content operation.
        if _TOOL_DRAFT_SECTION in tool_names:
            return _TOOL_DRAFT_SECTION
        # Fall back to the raw task name (dispatch will raise ToolError, which is
        # handled as a step failure) when the default tool is unavailable.
        return task or _TOOL_DRAFT_SECTION

    def _build_kwargs(
        self,
        tool_name: str,
        step: PlanStep,
        run_state: RunState,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the keyword arguments for dispatching ``tool_name`` (Req 3.1).

        Arguments are derived from the step's ``task``/``description`` for the
        content tools, and from the accumulated sections plus Run-state-derived
        title/prepared-by/output-path for ``build_docx``.

        Args:
            tool_name: The resolved tool name being dispatched.
            step: The plan step being executed.
            run_state: The owning Run state (source of the output path/title).
            sections: The accumulated section payloads for ``build_docx``.

        Returns:
            The keyword-argument mapping to forward to the tool.
        """

        description = (step.description or "").strip()
        task = (step.task or "").strip()

        if tool_name == _TOOL_RESEARCH:
            return {"topic": description or task}
        if tool_name == _TOOL_GENERATE_TABLE_DATA:
            return {"spec": description or task}
        if tool_name == _TOOL_BUILD_DOCX:
            return {
                "sections": list(sections),
                "output_path": self._derive_output_path(run_state),
                "title": self._derive_title(run_state),
                "prepared_by": _DEFAULT_PREPARED_BY,
            }
        # draft_section (and default fallback).
        return {
            "title": task or "Section",
            "context": description or task or run_state.request,
        }

    async def _invoke_with_retry(
        self,
        tool_name: str,
        kwargs: dict[str, Any],
        run_state: RunState,
        step: PlanStep,
    ) -> tuple[ToolResult | None, Exception | None]:
        """Dispatch a tool, retrying on exception up to the configured bound.

        The tool is invoked at most ``1 + max_retries`` times. The first
        successful invocation returns ``(result, None)``; if every attempt
        raises, ``(None, last_exception)`` is returned so the caller can mark the
        step failed (Req 3.4).

        Args:
            tool_name: The tool to dispatch to.
            kwargs: The keyword arguments to forward to the tool.
            run_state: The owning Run state (for decision logging).
            step: The step being executed (for decision logging).

        Returns:
            A ``(result, error)`` tuple: ``error`` is ``None`` on success and the
            last raised exception when all attempts fail.
        """

        last_exc: Exception | None = None
        total_attempts = self._max_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                result = await self._tools.dispatch(tool_name, **kwargs)
                return result, None
            except Exception as exc:  # noqa: BLE001 - any tool failure is retried/handled
                last_exc = exc
                self._safe_log(
                    run_state.run_id,
                    f"step {step.step} tool {tool_name!r} attempt {attempt} failed",
                    level="WARNING",
                    step=step.step,
                    tool=tool_name,
                    attempt=attempt,
                    max_attempts=total_attempts,
                    error=str(exc),
                )
        return None, last_exc

    # ------------------------------------------------------------------
    # Bookkeeping (all best-effort; never crashes the Run — Req 3.5)
    # ------------------------------------------------------------------

    async def _record_success(
        self,
        step: PlanStep,
        tool_name: str,
        result: ToolResult | None,
        run_state: RunState,
        sections: list[dict[str, Any]],
    ) -> None:
        """Record a completed step's output and emit ``step_completed``.

        Sets the step's output summary, records the built document path when the
        step ran ``build_docx``, accumulates a section payload for content tools,
        and emits the terminal ``step_completed`` event. All of this is
        best-effort: any error is caught and logged so it never crashes the Run
        (Req 3.5).

        Args:
            step: The completed step.
            tool_name: The tool that produced ``result``.
            result: The tool result, if any.
            run_state: The owning Run state.
            sections: The accumulated section payloads (appended to on success).
        """

        try:
            summary = (result.summary if result is not None else "") or ""
            step.output_summary = summary

            if result is not None:
                if tool_name == _TOOL_BUILD_DOCX:
                    self._record_document_path(result, run_state)
                else:
                    self._accumulate_section(step, result, sections)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never fail a Run
            self._safe_log(
                run_state.run_id,
                f"best-effort bookkeeping failed for completed step {step.step}",
                level="ERROR",
                step=step.step,
                error=str(exc),
            )

        await self._safe_publish(
            StepCompletedEvent(
                run_id=run_state.run_id,
                step=step.step,
                output_summary=step.output_summary or "",
            ),
            run_state,
        )
        self._safe_log(
            run_state.run_id,
            f"step {step.step} completed",
            step=step.step,
            tool=tool_name,
        )

    async def _record_failure(
        self, step: PlanStep, error: Exception, run_state: RunState
    ) -> None:
        """Record a failed step's error and emit ``step_failed`` (Req 3.4, 3.5).

        Records the error on the step (both ``error`` and ``output_summary``) and
        emits the terminal ``step_failed`` event. Bookkeeping is best-effort and
        never crashes the Run.

        Args:
            step: The failed step.
            error: The exception that caused the failure.
            run_state: The owning Run state.
        """

        message = str(error) or error.__class__.__name__
        try:
            step.error = message
            step.output_summary = f"failed: {message}"
        except Exception:  # noqa: BLE001 - bookkeeping must never fail a Run
            pass

        await self._safe_publish(
            StepFailedEvent(run_id=run_state.run_id, step=step.step, error=message),
            run_state,
        )
        self._safe_log(
            run_state.run_id,
            f"step {step.step} failed after retries",
            level="ERROR",
            step=step.step,
            error=message,
        )

    def _accumulate_section(
        self,
        step: PlanStep,
        result: ToolResult,
        sections: list[dict[str, Any]],
    ) -> None:
        """Append a section payload derived from a content step's result.

        The section heading is taken from the tool result's ``title`` (when
        provided) or the step's task/description; the body is the tool's textual
        output; and a table is included when the result carries ``headers`` and
        ``rows`` (as ``generate_table_data`` produces).

        Args:
            step: The content step that produced ``result``.
            result: The tool result to derive a section from.
            sections: The accumulated section list to append to.
        """

        data = result.data or {}
        heading = ""
        if isinstance(data, dict):
            heading = str(data.get("title") or "")
        if not heading:
            heading = (step.task or step.description or f"Step {step.step}").strip()

        section: dict[str, Any] = {"heading": heading}
        if result.output:
            section["body"] = result.output

        if isinstance(data, dict):
            headers = data.get("headers")
            rows = data.get("rows")
            if headers and rows:
                section["table"] = {"headers": headers, "rows": rows}

        sections.append(section)

    @staticmethod
    def _record_document_path(result: ToolResult, run_state: RunState) -> None:
        """Record the built ``.docx`` path from a ``build_docx`` result.

        Args:
            result: The ``build_docx`` tool result.
            run_state: The owning Run state whose ``document_path`` is set.
        """

        data = result.data or {}
        document_path = data.get("document_path") if isinstance(data, dict) else None
        if document_path:
            run_state.document_path = Path(str(document_path))

    # ------------------------------------------------------------------
    # Derivations
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_output_path(run_state: RunState) -> Path:
        """Derive the ``.docx`` output path for a Run from its ``run_id``.

        Args:
            run_state: The Run state the path is derived from.

        Returns:
            The filesystem path the document artifact is written to.
        """

        return Path(_OUTPUT_DIR) / f"agent-run-{run_state.run_id}.docx"

    @staticmethod
    def _derive_title(run_state: RunState) -> str:
        """Derive a document title from the Run's request.

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

    # ------------------------------------------------------------------
    # Best-effort helpers
    # ------------------------------------------------------------------

    async def _safe_publish(self, event: Any, run_state: RunState) -> None:
        """Publish an event, suppressing any error (best-effort — Req 3.5).

        The event bus is designed never to raise, but a substitute or broken bus
        supplied for testing might. Either way an emission failure (whether raised
        synchronously when creating the coroutine or while awaiting it) must never
        crash a Run: it is caught, logged best-effort, and suppressed.

        Args:
            event: The SSE event to publish.
            run_state: The owning Run state (for replay buffering).
        """

        try:
            await self._events.publish(event, run_state)
        except Exception as exc:  # noqa: BLE001 - emission must never fail a Run
            self._log_bookkeeping_error(run_state.run_id, "publish", exc)

    def _safe_log(
        self, run_id: str, decision: str, *, level: str = "INFO", **fields: Any
    ) -> None:
        """Emit a decision log, suppressing any logger error (Req 3.5, 15.2).

        Args:
            run_id: The Run the decision belongs to.
            decision: A human-readable description of the decision.
            level: The severity level for the entry.
            **fields: Additional structured fields for the entry.
        """

        # logging must never fail a Run
        with contextlib.suppress(Exception):
            self._logger.decision(_COMPONENT, run_id, decision, level=level, **fields)

    def _log_bookkeeping_error(
        self, run_id: str, operation: str, exc: Exception
    ) -> None:
        """Best-effort log of a suppressed bookkeeping error (Req 3.5).

        Args:
            run_id: The Run the error belongs to.
            operation: The bookkeeping operation that failed (e.g. ``"publish"``).
            exc: The suppressed exception.
        """

        self._safe_log(
            run_id,
            f"best-effort {operation} failed and was suppressed",
            level="ERROR",
            error=str(exc),
        )
