"""Reflection and self-check (Req 4).

This module defines :class:`Reflector`, the fourth stage of the agent loop.
After the Executor has run every executable :class:`~app.models.schemas.PlanStep`
and the drafted content has been assembled, the reflector performs a **single**
self-check pass comparing the assembled output against the original request
(Req 4.1). It asks the LLM to identify weak or missing sections and, in the same
pass, to propose revised content for them.

The reflector performs **at most one** revision pass and then stops, regardless
of any newly identified weak sections (Req 4.2). It records its findings on the
Run state and in the Run log, and emits a ``reflection`` SSE event carrying the
findings plus the titles of the sections it revised (Req 4.3).

Reflection is strictly **best-effort**: it is a quality-improving enhancement,
not a correctness requirement, so a reflection failure (an LLM error, malformed
JSON, or any other exception) must never fail the Run. On failure the reflector
swallows the error, still emits a ``reflection`` event when possible (carrying
empty findings), and returns a :class:`ReflectionResult` indicating that no
revisions were made.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.core.event_bus import EventBus
from app.core.logging import StructuredLogger
from app.models.schemas import ReflectionEvent, RunState
from app.services.llm import LLMError, LLMJSONError, LLMService

# The component name used in structured decision logs.
_COMPONENT = "reflector"

_SYSTEM_PROMPT = (
    "You are a meticulous reviewer for a service that produces polished "
    "Microsoft Word (.docx) business document deliverables. You are given the "
    "original business request and the assembled draft output. Perform a single "
    "self-check: judge how well the draft satisfies the request, identify any "
    "weak, missing, or off-target sections, and — in this same pass — propose "
    "improved replacement content for the weakest sections.\n\n"
    "Do NOT ask for another round of review; this is a one-shot pass.\n\n"
    "Respond with a single strict-JSON object matching this shape:\n"
    '{"findings": "<concise prose summary of your assessment>", '
    '"weak_sections": ["<section title>", ...], '
    '"revised_sections": [{"title": "<section title>", '
    '"content": "<improved content>"}, ...]}\n'
    "When the draft already satisfies the request well, 'weak_sections' and "
    "'revised_sections' may be empty lists."
)


class _RevisedSection(BaseModel):
    """A single revised section proposed by the reflection pass.

    Attributes:
        title: The title of the section being revised.
        content: The improved replacement content for the section.
    """

    title: str = ""
    content: str = ""


class _ReflectionOutput(BaseModel):
    """The JSON schema the reflection LLM call is constrained to (Req 4.1, 4.2).

    Attributes:
        findings: A concise prose summary of the reviewer's assessment.
        weak_sections: Titles of sections judged weak or missing.
        revised_sections: The proposed revised sections (single-pass revision).
    """

    findings: str = ""
    weak_sections: list[str] = Field(default_factory=list)
    revised_sections: list[_RevisedSection] = Field(default_factory=list)


@dataclass
class ReflectionResult:
    """The outcome of a single reflection pass (Req 4.2, 4.3).

    Attributes:
        findings: The reviewer's prose findings (empty when reflection failed or
            produced nothing).
        revised_sections: The titles of the sections that were revised in the
            single pass; empty when no revisions were made.
        revised_content: A mapping of revised section title to its improved
            content, or ``None`` when no revisions were made. Lets a caller apply
            the single-pass revisions to the assembled output if desired.
    """

    findings: str = ""
    revised_sections: list[str] = field(default_factory=list)
    revised_content: dict[str, str] | None = None


class Reflector:
    """Single-pass self-check over the assembled output (Req 4).

    A single :class:`Reflector` instance can reflect on any number of Runs; all
    per-Run state lives on the passed :class:`~app.models.schemas.RunState`.

    Attributes:
        _llm: The LLM service used to perform the reflection pass.
        _events: The per-run event bus the ``reflection`` event is published to.
        _logger: The structured logger used for decision logging (Req 15.1).
    """

    def __init__(
        self,
        llm: LLMService,
        events: EventBus,
        logger: StructuredLogger | None = None,
    ) -> None:
        """Initialize the reflector.

        Args:
            llm: The shared :class:`~app.services.llm.LLMService` used to perform
                the single reflection pass.
            events: The per-run :class:`~app.core.event_bus.EventBus` the
                ``reflection`` event is published to.
            logger: An optional :class:`~app.core.logging.StructuredLogger` for
                decision logging. When ``None``, a fresh logger is created.
        """

        self._llm = llm
        self._events = events
        self._logger = logger if logger is not None else StructuredLogger()

    async def reflect(
        self, run_state: RunState, assembled_output: str
    ) -> ReflectionResult:
        """Perform a single self-check pass over ``assembled_output`` (Req 4).

        Compares the assembled output to ``run_state.request`` via one
        :meth:`~app.services.llm.LLMService.complete_json` call constrained to
        :class:`_ReflectionOutput`. This performs **at most one** revision pass
        and then stops, regardless of any newly identified weak sections
        (Req 4.2) — there is no loop. The findings are recorded on
        ``run_state.reflection_findings`` and in the Run log, and a
        ``reflection`` SSE event carrying the findings plus the revised section
        titles is emitted (Req 4.3).

        Reflection is best-effort and never fails the Run: if the LLM call fails
        (:class:`~app.services.llm.LLMError` /
        :class:`~app.services.llm.LLMJSONError`) or anything else goes wrong, the
        error is swallowed, a ``reflection`` event with empty findings is still
        emitted when possible, and a :class:`ReflectionResult` indicating no
        revisions is returned.

        Args:
            run_state: The Run state carrying the original ``request`` and the
                ``reflection_findings`` field this method records into.
            assembled_output: The assembled draft output to review against the
                request.

        Returns:
            A :class:`ReflectionResult` with the findings and any single-pass
            revisions (empty / ``None`` when nothing was revised or reflection
            failed).
        """

        prompt = (
            "Review the assembled draft against the original request and perform "
            "your single self-check pass.\n\n"
            f"Original request:\n{run_state.request}\n\n"
            f"Assembled draft output:\n{assembled_output}"
        )

        try:
            output = await self._llm.complete_json(
                prompt, _ReflectionOutput, system=_SYSTEM_PROMPT
            )
        except (LLMJSONError, LLMError) as exc:
            # Reflection is an enhancement, not a requirement: a failed LLM call
            # must never fail the Run (Req 4, best-effort).
            return await self._handle_failure(run_state, exc)
        except Exception as exc:  # noqa: BLE001 - reflection must never fail a Run
            return await self._handle_failure(run_state, exc)

        return await self._handle_success(run_state, output)

    async def _handle_success(
        self, run_state: RunState, output: _ReflectionOutput
    ) -> ReflectionResult:
        """Record findings, emit the ``reflection`` event, build the result.

        Applies the single-pass revisions verbatim (no second pass, Req 4.2),
        records the findings on the Run state and in the Run log (Req 4.3), and
        emits a ``reflection`` event carrying the findings and revised section
        titles.

        Args:
            run_state: The owning Run state.
            output: The validated reflection output from the LLM.

        Returns:
            The :class:`ReflectionResult` describing this single pass.
        """

        findings = output.findings or ""
        # De-duplicate while preserving order; ignore entries with a blank title.
        revised_content: dict[str, str] = {}
        for section in output.revised_sections:
            title = (section.title or "").strip()
            if title:
                revised_content[title] = section.content
        revised_titles = list(revised_content)

        # Record findings on the Run state (Req 4.3). Best-effort bookkeeping.
        with contextlib.suppress(Exception):  # bookkeeping must never fail a Run
            run_state.reflection_findings = findings

        self._safe_log(
            run_state.run_id,
            "completed single-pass reflection",
            findings=findings,
            weak_sections=list(output.weak_sections),
            revised_sections=revised_titles,
        )

        await self._safe_publish(run_state, findings, revised_titles)

        return ReflectionResult(
            findings=findings,
            revised_sections=revised_titles,
            revised_content=revised_content or None,
        )

    async def _handle_failure(
        self, run_state: RunState, exc: Exception
    ) -> ReflectionResult:
        """Swallow a reflection failure and still emit a ``reflection`` event.

        Records empty findings on the Run state, logs the suppressed failure,
        and emits a ``reflection`` event with empty findings when possible so the
        stream still observes that the reflection stage ran (Req 4.3). The Run is
        never failed by reflection (Req 4).

        Args:
            run_state: The owning Run state.
            exc: The suppressed exception that caused reflection to fail.

        Returns:
            A :class:`ReflectionResult` indicating that no revisions were made.
        """

        with contextlib.suppress(Exception):  # bookkeeping must never fail a Run
            run_state.reflection_findings = ""

        self._safe_log(
            run_state.run_id,
            "reflection failed; continuing run without revisions",
            level="WARNING",
            error=str(exc) or exc.__class__.__name__,
        )

        await self._safe_publish(run_state, "", [])

        return ReflectionResult(findings="", revised_sections=[], revised_content=None)

    async def _safe_publish(
        self, run_state: RunState, findings: str, revised_sections: list[str]
    ) -> None:
        """Emit the ``reflection`` event, suppressing any error (Req 4.3).

        The event bus is designed never to raise, but a substitute or broken bus
        supplied for testing might. Either way an emission failure must never
        crash a Run: it is caught, logged best-effort, and suppressed.

        Args:
            run_state: The owning Run state (for replay buffering).
            findings: The findings to carry on the event.
            revised_sections: The titles of the revised sections.
        """

        try:
            event = ReflectionEvent(
                run_id=run_state.run_id,
                findings=findings,
                revised_sections=list(revised_sections),
            )
            await self._events.publish(event, run_state)
        except Exception as exc:  # noqa: BLE001 - emission must never fail a Run
            self._safe_log(
                run_state.run_id,
                "best-effort reflection event publish failed and was suppressed",
                level="ERROR",
                error=str(exc),
            )

    def _safe_log(
        self, run_id: str, decision: str, *, level: str = "INFO", **fields: object
    ) -> None:
        """Emit a decision log, suppressing any logger error (Req 4.3, 15.2).

        Args:
            run_id: The Run the decision belongs to.
            decision: A human-readable description of the decision.
            level: The severity level for the entry.
            **fields: Additional structured fields for the entry.
        """

        # logging must never fail a Run
        with contextlib.suppress(Exception):
            self._logger.decision(_COMPONENT, run_id, decision, level=level, **fields)
