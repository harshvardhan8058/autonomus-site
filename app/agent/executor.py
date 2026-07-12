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
import re
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

# The heading used for the synthesized executive summary section.
_EXECUTIVE_SUMMARY_TITLE = "Executive Summary"

# The maximum number of characters of accumulated content fed as context when
# synthesizing the executive summary, bounding the prompt size.
_SUMMARY_CONTEXT_MAX_CHARS = 6000

# The set of registered tool identifiers that must never surface as a
# user-visible section heading (they are internal routing names, not titles).
_KNOWN_TOOL_NAMES = frozenset(
    {_TOOL_RESEARCH, _TOOL_DRAFT_SECTION, _TOOL_GENERATE_TABLE_DATA, _TOOL_BUILD_DOCX}
)

# The maximum number of characters of a title derived from a free-form
# description, capped at a whole-word boundary (never mid-word).
_DERIVED_TITLE_MAX_CHARS = 60

# Delimiters that end the "leading clause" of a description when deriving a
# concise section title from it.
_CLAUSE_DELIMITERS = re.compile(r"[.;:\n]")

# Leading imperative verb phrases stripped from a description before deriving a
# noun-phrase heading. Ordered longest-first so multi-word phrases are matched
# before their single-word prefixes (e.g. "draft the" before "draft").
_LEADING_VERB_PHRASES: tuple[str, ...] = (
    "gather information on",
    "write the",
    "create an",
    "create a",
    "draft the",
    "generate a",
    "prepare the",
    "produce a",
    "outline the",
    "develop a",
    "draft",
    "generate",
    "compile",
    "assemble",
    "research",
)

# Natural boundaries that end the noun phrase of a derived title. A comma or any
# of these connectors terminates the heading so it reads as a clean noun phrase.
_TITLE_BOUNDARY = re.compile(r",| of the | for the | that | which | to ")

# Filler words stripped from the leading and trailing edges of a derived title.
_TITLE_FILLER_WORDS = frozenset(
    {"of", "for", "the", "and", "sections", "section"}
)


def _title_case(text: str) -> str:
    """Capitalize the first letter of each whitespace-separated word.

    Unlike :meth:`str.title`, the remainder of each word is left untouched, so
    embedded capitals and apostrophes (e.g. acronyms or ``don't``) are preserved.

    Args:
        text: The text to title-case.

    Returns:
        The title-cased text.
    """

    return " ".join(
        word[:1].upper() + word[1:] if word else word for word in text.split()
    )


def _smart_title_case(text: str) -> str:
    """Title-case ``text``, capitalizing hyphen-separated sub-words too.

    Behaves like :func:`_title_case` but also capitalizes the first letter of
    each hyphen-delimited part, so a token like ``on-premise`` becomes
    ``On-Premise`` while embedded capitals in acronyms are preserved.

    Args:
        text: The text to title-case.

    Returns:
        The hyphen-aware title-cased text.
    """

    def _cap_word(word: str) -> str:
        return "-".join(
            part[:1].upper() + part[1:] if part else part for part in word.split("-")
        )

    return " ".join(_cap_word(word) for word in text.split())


def _strip_leading_verb_phrase(text: str) -> str:
    """Strip a leading imperative verb phrase from ``text`` when present.

    Args:
        text: The clause to strip a leading verb phrase from.

    Returns:
        ``text`` with a recognized leading verb phrase removed, or the original
        text when none matches.
    """

    lowered = text.lower()
    for phrase in _LEADING_VERB_PHRASES:
        if lowered == phrase:
            return ""
        if lowered.startswith(phrase + " "):
            return text[len(phrase):].lstrip()
    return text


def _cap_at_word_boundary(text: str, max_chars: int) -> str:
    """Truncate ``text`` to at most ``max_chars`` on a whole-word boundary.

    A word is never cut in half: whole words are accumulated until the next word
    would exceed ``max_chars``. When the very first word already exceeds the
    limit it is kept in full rather than returning an empty string.

    Args:
        text: The text to cap.
        max_chars: The maximum length in characters.

    Returns:
        The capped text.
    """

    if len(text) <= max_chars:
        return text
    kept: list[str] = []
    length = 0
    for word in text.split():
        extra = len(word) + (1 if kept else 0)
        if length + extra > max_chars:
            break
        kept.append(word)
        length += extra
    return " ".join(kept) if kept else text.split()[0]


def _title_from_description(description: str) -> str:
    """Derive a concise, professional heading from a step description.

    The derivation never truncates mid-phrase. It takes the leading clause of
    ``description`` (up to the first sentence/clause delimiter), strips a leading
    imperative verb phrase (e.g. "write the", "create a"), keeps the remaining
    noun phrase up to the first natural boundary (a comma or a connector such as
    " of the " or " to "), strips leading/trailing filler words, caps the result
    at :data:`_DERIVED_TITLE_MAX_CHARS` characters on a whole-word boundary, and
    title-cases it.

    Args:
        description: The free-form step description.

    Returns:
        A concise, title-cased heading, or an empty string when ``description``
        yields no usable text.
    """

    text = " ".join((description or "").split())
    if not text:
        return ""

    # 1. Leading clause only (up to the first sentence/clause delimiter).
    clause = _CLAUSE_DELIMITERS.split(text, maxsplit=1)[0].strip() or text
    # 2. Strip a leading imperative verb phrase ("write the", "create a", ...).
    clause = _strip_leading_verb_phrase(clause)
    # 3. Keep the noun phrase up to the first natural boundary.
    clause = _TITLE_BOUNDARY.split(clause, maxsplit=1)[0].strip()
    # 4. Strip leading/trailing filler words.
    words = clause.split()
    while words and words[-1].lower().strip(".,;:!?") in _TITLE_FILLER_WORDS:
        words.pop()
    while words and words[0].lower().strip(".,;:!?") in _TITLE_FILLER_WORDS:
        words.pop(0)
    clause = " ".join(words)
    # 5. Cap at a whole-word boundary and tidy trailing punctuation.
    clause = _cap_at_word_boundary(clause, _DERIVED_TITLE_MAX_CHARS)
    clause = clause.strip().rstrip(".,;:!?-\u2013\u2014")
    return _smart_title_case(clause) if clause.strip() else ""


def humanize_section_title(task: str, description: str, index: int) -> str:
    """Build a human-readable section heading from a plan step (Req 10.1).

    Section headings must be professional, title-cased titles rather than the
    raw internal tool identifiers used for routing. When ``task`` is a
    meaningful, non-tool-name value it is title-cased (underscores become
    spaces); when ``task`` is one of the known tool names or is empty, a concise
    noun-phrase title is derived from ``description`` (without mid-phrase
    truncation); failing that, a positional ``"Section N"`` fallback is used.

    This helper is the fallback used when a step does not carry an explicit
    :attr:`~app.models.schemas.PlanStep.section_title`; callers prefer that
    planner-provided title when it is present.

    Args:
        task: The plan step's ``task`` value (may be a bare tool name).
        description: The plan step's free-form description.
        index: The 1-based position used for the ``"Section N"`` fallback.

    Returns:
        A non-empty, human-readable, title-cased section heading.
    """

    normalized_task = (task or "").strip()
    if normalized_task and normalized_task not in _KNOWN_TOOL_NAMES:
        return _title_case(normalized_task.replace("_", " "))

    derived = _title_from_description(description)
    if derived:
        return derived
    return f"Section {index}"


def section_heading_for_step(step: PlanStep, index: int) -> str:
    """Resolve the document heading for ``step`` in priority order (Req 10.1).

    The heading is chosen as:

    1. ``step.section_title`` (stripped) when non-empty — the clean,
       planner-provided professional heading;
    2. otherwise :func:`humanize_section_title` derived from the step's ``task``
       and ``description`` (never truncated mid-phrase);
    3. otherwise the positional ``"Section N"`` fallback.

    Args:
        step: The plan step to derive a heading for.
        index: The 1-based position used for the ``"Section N"`` fallback.

    Returns:
        A non-empty, human-readable section heading.
    """

    explicit = (step.section_title or "").strip()
    if explicit:
        return explicit
    return humanize_section_title(step.task or "", step.description or "", index)

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
        # Before assembling the final document, synthesize a genuine executive
        # summary from the accumulated content and place it first (Req 10.1).
        if tool_name == _TOOL_BUILD_DOCX:
            await self._synthesize_executive_summary(sections, run_state)
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
                "sections": self._sections_with_assumptions(sections, run_state),
                "output_path": self._derive_output_path(run_state),
                "title": self._derive_title(run_state),
                "prepared_by": _DEFAULT_PREPARED_BY,
            }
        # draft_section (and default fallback).
        return {
            "title": task or "Section",
            "context": description or task or run_state.request,
        }

    @staticmethod
    def _sections_with_assumptions(
        sections: list[dict[str, Any]], run_state: RunState
    ) -> list[dict[str, Any]]:
        """Return the content sections plus a "Key Assumptions" bullet section.

        When the Run recorded any planner assumptions, a dedicated
        ``{"heading": "Key Assumptions", "bullets": [...]}`` section is appended
        once after the content sections so the deliverable surfaces the agent's
        assumptions as a genuine, non-duplicate bullet list. When there are no
        assumptions the content sections are returned unchanged.

        Args:
            sections: The accumulated content section payloads.
            run_state: The owning Run state (source of the assumptions).

        Returns:
            A new list of section payloads to pass to ``build_docx``.
        """

        result = list(sections)
        assumptions = [
            str(assumption).strip()
            for assumption in (run_state.assumptions or [])
            if str(assumption).strip()
        ]
        if assumptions:
            result.append({"heading": "Key Assumptions", "bullets": assumptions})
        return result

    async def _synthesize_executive_summary(
        self, sections: list[dict[str, Any]], run_state: RunState
    ) -> None:
        """Synthesize an executive summary from the drafted content (Req 10.1).

        After all content steps have run and before ``build_docx`` assembles the
        document, this produces a genuine executive summary of the whole
        deliverable (rather than a generic one drafted before the other sections
        existed). It concatenates the accumulated sections' headings and bodies
        into a bounded context string, dispatches the existing ``draft_section``
        tool to generate the summary prose, and inserts it as the FIRST section.

        If the accumulated sections already contain an "Executive Summary"
        (case-insensitive), that section's body is replaced with the synthesized
        summary and it is moved to the front rather than duplicated.

        The synthesis is strictly best-effort: it is a no-op when there is no
        accumulated content, and any error (LLM failure, empty output) is caught
        and logged so the document is still built with the existing sections and
        the Run never fails. The sections list is mutated in place.

        Args:
            sections: The accumulated content section payloads (mutated in place).
            run_state: The owning Run state (used for decision logging).
        """

        try:
            if not sections:
                return

            # Locate any existing "Executive Summary" section for dedupe/replace.
            existing_index: int | None = None
            for index, section in enumerate(sections):
                heading = str(section.get("heading", "") or "").strip().lower()
                if heading == _EXECUTIVE_SUMMARY_TITLE.lower():
                    existing_index = index
                    break

            context = self._assemble_summary_context(sections, existing_index)
            if not context.strip():
                return

            result = await self._tools.dispatch(
                _TOOL_DRAFT_SECTION,
                title=_EXECUTIVE_SUMMARY_TITLE,
                context=context,
            )
            summary_body = (result.output if result is not None else "") or ""
            if not summary_body.strip():
                return

            if existing_index is not None:
                sections.pop(existing_index)
            sections.insert(
                0, {"heading": _EXECUTIVE_SUMMARY_TITLE, "body": summary_body}
            )
            self._safe_log(
                run_state.run_id,
                "synthesized executive summary from drafted content",
                section_count=len(sections),
            )
        except Exception as exc:  # noqa: BLE001 - synthesis must never fail a Run
            self._safe_log(
                run_state.run_id,
                "best-effort executive summary synthesis failed and was suppressed",
                level="WARNING",
                error=str(exc),
            )

    @staticmethod
    def _assemble_summary_context(
        sections: list[dict[str, Any]], skip_index: int | None = None
    ) -> str:
        """Concatenate section headings and bodies into a bounded context string.

        Args:
            sections: The accumulated content section payloads.
            skip_index: An optional index to skip (an existing "Executive
                Summary" section) so the synthesis draws only on real content.

        Returns:
            The concatenated ``heading`` + ``body`` blocks of the sections,
            truncated to :data:`_SUMMARY_CONTEXT_MAX_CHARS` characters.
        """

        parts: list[str] = []
        for index, section in enumerate(sections):
            if index == skip_index:
                continue
            heading = str(section.get("heading", "") or "").strip()
            body = str(section.get("body", "") or "").strip()
            block = "\n".join(piece for piece in (heading, body) if piece)
            if block:
                parts.append(block)
        return "\n\n".join(parts)[:_SUMMARY_CONTEXT_MAX_CHARS]

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

        The section heading is resolved by :func:`section_heading_for_step`: the
        planner-provided ``section_title`` when present, otherwise a
        human-readable, title-cased title derived from the step (never a raw tool
        identifier). The body is the tool's textual output, and a table is
        included when the result carries ``headers`` and ``rows`` (as
        ``generate_table_data`` produces).

        Args:
            step: The content step that produced ``result``.
            result: The tool result to derive a section from.
            sections: The accumulated section list to append to.
        """

        data = result.data or {}
        heading = section_heading_for_step(step, step.step)

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
