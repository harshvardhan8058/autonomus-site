"""Autonomous multi-step planning (Req 2).

This module defines :class:`Planner`, the component that turns a validated
natural-language business request into a structured, multi-step
:class:`~app.models.schemas.Plan`. The planner prompts the LLM to decompose the
request into at least two sequential, tool-oriented steps and to enumerate any
assumptions it makes when the request is ambiguous (Req 2.1, 2.2, 2.3).

Planning delegates retries, JSON repair, and Groq -> Ollama fallback to
:meth:`~app.services.llm.LLMService.complete_json`, which validates the model
output against the :class:`Plan` schema. The schema's own validators enforce the
``>= 2`` steps and sequential ``1..n`` numbering guarantees, so a violating LLM
response is treated as unparseable and triggers repair/retry inside the LLM
service (Req 2.4, 2.5).

When plan generation fails on every backend, the planner raises
:class:`PlanningError`, carrying the ``run_id``, a human-readable failure
reason, and a best-effort retry history. The API layer maps this to an HTTP 503
response with a :class:`~app.models.schemas.PlanningFailureBody` (Req 2.6).
"""

from __future__ import annotations

from app.core.logging import StructuredLogger
from app.models.schemas import Plan, RetryAttempt
from app.services.llm import LLMError, LLMJSONError, LLMService

_SYSTEM_PROMPT = (
    "You are an autonomous planning agent for a service that produces polished "
    "Microsoft Word (.docx) business document deliverables. Given a business "
    "request, decompose it into an ordered, executable plan.\n\n"
    "Rules for the plan:\n"
    "- Produce AT LEAST two steps, numbered sequentially starting at 1 "
    "(1, 2, 3, ...), each building toward the final document.\n"
    "- Each step MUST include: 'step' (integer), 'task' (short name), "
    "'description' (what the step does), 'expected_output' (what the step "
    "produces), and 'section_title' (see below).\n"
    "- 'task' is the INTERNAL action/tool intent the executor dispatches on; "
    "'section_title' is the HEADING that appears in the finished Word document. "
    "For every content step, 'section_title' MUST be a concise, professional, "
    "title-case document section heading of 2-6 words (for example 'Executive "
    "Summary', 'Cloud CRM Overview', 'On-Premise vs Cloud Comparison', "
    "'Technical Requirements', 'Migration Plan', 'Cost Analysis'). Do NOT copy "
    "the verbose task/description into 'section_title'. For a build_docx or "
    "other assembly step that produces no heading, 'section_title' may be an "
    "empty string.\n"
    "- Prefer tool-oriented tasks so the executor can dispatch them. Use these "
    "tool names where appropriate: 'research' (gather facts on a topic), "
    "'draft_section' (write a document section from a title and context), "
    "'generate_table_data' (produce structured tabular data from a spec), and "
    "'build_docx' (assemble the final .docx from the drafted sections). A "
    "typical plan researches, drafts one or more sections, generates table "
    "data, then builds the document.\n"
    "- The plan MUST include, as an early content step, a 'draft_section' step "
    "whose 'section_title' is exactly 'Executive Summary' that concisely "
    "summarizes the deliverable (its purpose, scope, and key takeaways) for the "
    "reader before the detailed sections.\n"
    "- The plan MUST include at least one 'generate_table_data' step that "
    "produces a relevant comparison or data table (for example a feature, cost, "
    "or option comparison) so the finished document contains a real, meaningful "
    "table drawn from actual content rather than a fabricated placeholder.\n"
    "- If the request is ambiguous (for example an unspecified document type, "
    "audience, or scope), make reasonable decisions and enumerate EACH such "
    "decision as a string in the top-level 'assumptions' list. When the request "
    "is unambiguous, 'assumptions' may be an empty list.\n\n"
    "Respond with a single strict-JSON object matching this shape:\n"
    '{"steps": [{"step": 1, "task": "...", "section_title": "...", '
    '"description": "...", "expected_output": "..."}, ...], '
    '"assumptions": ["..."]}'
)


class PlanningError(Exception):
    """Raised when plan generation fails on all backends (Req 2.6).

    The error carries everything the API layer needs to build a
    :class:`~app.models.schemas.PlanningFailureBody` and return HTTP 503: the
    originating ``run_id`` (when known), a human-readable failure reason, and a
    best-effort history of the retry attempts that were made.

    Attributes:
        run_id: The identifier of the Run whose planning failed, or ``None`` when
            planning was attempted outside of a tracked Run.
        reason: A human-readable explanation of why planning failed.
        retry_history: The recorded retry attempts across backends. This is
            best-effort: when no structured per-attempt history is available it
            contains a single :class:`RetryAttempt` summarizing the failure.
    """

    def __init__(
        self,
        reason: str,
        *,
        run_id: str | None = None,
        retry_history: list[RetryAttempt] | None = None,
    ) -> None:
        """Initialize the planning error.

        Args:
            reason: A human-readable explanation of the failure.
            run_id: The Run identifier the failure belongs to, if any.
            retry_history: The recorded retry attempts; defaults to an empty
                list when not supplied.
        """

        super().__init__(reason)
        self.run_id: str | None = run_id
        self.reason: str = reason
        self.retry_history: list[RetryAttempt] = list(retry_history or [])


class Planner:
    """Produces a structured, multi-step :class:`Plan` from a request (Req 2).

    The planner is the second stage of the agent loop: after a request passes
    the guardrail it is decomposed into an ordered plan. Retry/backoff, JSON
    repair, and backend fallback are delegated to
    :meth:`LLMService.complete_json`; the :class:`Plan` schema's validators
    enforce the structural guarantees (``>= 2`` sequential steps).

    Attributes:
        _llm: The LLM service used to generate the plan.
        _logger: The structured logger used for decision logging (Req 15.1).
    """

    def __init__(
        self,
        llm: LLMService,
        logger: StructuredLogger | None = None,
    ) -> None:
        """Initialize the planner.

        Args:
            llm: The shared :class:`LLMService` used to generate the plan.
            logger: An optional :class:`StructuredLogger` for decision logging.
                When ``None``, a fresh :class:`StructuredLogger` is created.
        """

        self._llm = llm
        self._logger = logger if logger is not None else StructuredLogger()

    async def make_plan(self, request: str, *, run_id: str | None = None) -> Plan:
        """Produce a strict-JSON :class:`Plan` for ``request`` (Req 2.1-2.6).

        Builds a planning prompt instructing the model to decompose the request
        into at least two sequential, tool-oriented steps and to enumerate any
        assumptions for ambiguous input, then calls
        :meth:`LLMService.complete_json` constrained to the :class:`Plan`
        schema. The schema validators enforce the ``>= 2`` steps and sequential
        numbering guarantees; a violating response is treated as unparseable and
        triggers repair/retry inside the LLM service.

        When the plan contains assumptions, a structured assumption decision is
        logged via the logger (Req 15.1). When plan generation fails on every
        backend after retries and fallback are exhausted, a
        :class:`PlanningError` is raised carrying the ``run_id``, a
        human-readable reason, and a best-effort retry history (Req 2.6).

        Args:
            request: The validated natural-language business request.
            run_id: The identifier of the Run this plan belongs to, if any. It is
                propagated into a :class:`PlanningError` and decision logs.

        Returns:
            A validated :class:`Plan` with at least two sequential steps and any
            enumerated assumptions.

        Raises:
            PlanningError: If plan generation fails on all backends after
                retries and fallback are exhausted.
        """

        prompt = (
            "Create an execution plan for the following business request. "
            "Decompose it into at least two sequential, tool-oriented steps and "
            "enumerate any assumptions you make for ambiguous details.\n\n"
            f"Request:\n{request}"
        )

        try:
            plan = await self._llm.complete_json(prompt, Plan, system=_SYSTEM_PROMPT)
        except (LLMJSONError, LLMError) as exc:
            # All retries + JSON repair + Groq -> Ollama fallback have been
            # exhausted by the LLM service. Escalate as a PlanningError carrying
            # a best-effort retry history for the 503 response body (Req 2.6).
            reason = f"plan generation failed on all backends: {exc}"
            retry_history = self._best_effort_retry_history(exc)
            self._logger.decision(
                "planner",
                run_id or "unknown",
                "planning failed on all backends",
                level="ERROR",
                reason=reason,
            )
            raise PlanningError(
                reason, run_id=run_id, retry_history=retry_history
            ) from exc

        if plan.assumptions:
            # Record the autonomous assumptions the planner adopted for an
            # ambiguous request so agent behavior is traceable (Req 2.3, 15.1).
            self._logger.decision(
                "planner",
                run_id or "unknown",
                "adopted planning assumptions for ambiguous request",
                assumptions=list(plan.assumptions),
            )

        return plan

    def _best_effort_retry_history(self, exc: Exception) -> list[RetryAttempt]:
        """Build a best-effort retry history from a planning failure (Req 2.6).

        The :class:`LLMService` does not surface a structured per-attempt
        history, so this summarizes the failure as a single
        :class:`RetryAttempt` attributed to the currently active backend. This
        guarantees the :class:`PlanningError` always carries a non-empty history
        for the 503 response body.

        Args:
            exc: The exception raised when plan generation failed.

        Returns:
            A single-element list with a :class:`RetryAttempt` summarizing the
            failure.
        """

        return [
            RetryAttempt(
                backend=self._llm.active_backend,
                attempt=1,
                error=str(exc) or exc.__class__.__name__,
                delay_seconds=0.0,
            )
        ]
