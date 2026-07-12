"""Property-based tests for :func:`app.agent.orchestrator.derive_status`.

These tests validate three design correctness properties for the pure status
derivation function (Req 7):

- **Property 1** (Req 7.2, 7.5): the status is never falsely ``completed`` --
  if any step is ``failed`` the result is never ``completed``.
- **Property 2** (Req 7.3): ``completed`` holds *exactly* when every step is
  ``done``, an artifact exists, and the summary is non-empty.
- **Property 3** (Req 7.1, 7.2, 7.4): the status is total (always a
  :class:`RunStatus` member), deterministic (idempotent for identical inputs),
  and, when at least one step failed, resolves to ``partial`` if an artifact
  exists else ``failed``.

All property tests use Hypothesis with a minimum of 100 iterations.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.agent.orchestrator import derive_status
from app.models.schemas import PlanStep, RunStatus, StepStatus

# --- Strategies -------------------------------------------------------------

_nonempty_text = st.text(min_size=1)


@st.composite
def _plan_steps(draw: st.DrawFn) -> list[PlanStep]:
    """Generate a list of sequentially numbered steps with arbitrary statuses.

    The list length ranges from 0 (edge case: no steps) to 8. Each step is
    assigned an arbitrary :class:`StepStatus` so the generated space exercises
    all combinations of done/failed/pending/running/skipped steps.
    """

    n = draw(st.integers(min_value=0, max_value=8))
    steps: list[PlanStep] = []
    for i in range(1, n + 1):
        steps.append(
            PlanStep(
                step=i,
                task="t",
                description="d",
                expected_output="e",
                status=draw(st.sampled_from(list(StepStatus))),
            )
        )
    return steps


# A summary strategy that mixes blank/whitespace-only and non-blank strings so
# the "non-empty after strip" condition is exercised in both directions.
_summaries = st.one_of(
    st.just(""),
    st.sampled_from([" ", "\t", "\n", "   \n\t "]),
    _nonempty_text,
)


# --- Property 1 -------------------------------------------------------------


# Feature: autonomous-agent-service, Property 1: Status is never falsely "completed"
@pytest.mark.property
@settings(max_examples=100)
@given(
    steps=_plan_steps(),
    artifact_exists=st.booleans(),
    summary=_summaries,
)
def test_status_never_falsely_completed(
    steps: list[PlanStep],
    artifact_exists: bool,
    summary: str,
) -> None:
    """Property 1: a failed step never yields ``completed``.

    **Validates: Requirements 7.2, 7.5**

    For any ``(steps, artifact_exists, summary)``, if any step is ``failed``
    then :func:`derive_status` returns a value other than
    :attr:`RunStatus.COMPLETED`.
    """

    if any(s.status is StepStatus.FAILED for s in steps):
        assert derive_status(steps, artifact_exists, summary) != RunStatus.COMPLETED


# --- Property 2 -------------------------------------------------------------


# Feature: autonomous-agent-service, Property 2: "completed" holds exactly when all success conditions are met  # noqa: E501
@pytest.mark.property
@settings(max_examples=100)
@given(
    steps=_plan_steps(),
    artifact_exists=st.booleans(),
    summary=_summaries,
)
def test_completed_iff_all_success_conditions(
    steps: list[PlanStep],
    artifact_exists: bool,
    summary: str,
) -> None:
    """Property 2: ``completed`` iff all steps done, artifact exists, summary non-empty.

    **Validates: Requirements 7.3**

    :func:`derive_status` returns :attr:`RunStatus.COMPLETED` if and only if
    every step is ``done``, ``artifact_exists`` is true, and ``summary`` is
    non-empty after stripping.
    """

    all_done = all(s.status is StepStatus.DONE for s in steps)
    summary_present = bool(summary.strip())
    expected_completed = all_done and artifact_exists and summary_present

    result = derive_status(steps, artifact_exists, summary)
    assert (result == RunStatus.COMPLETED) == expected_completed


# --- Property 3 -------------------------------------------------------------


# Feature: autonomous-agent-service, Property 3: Status is total, in-enum, and deterministic
@pytest.mark.property
@settings(max_examples=100)
@given(
    steps=_plan_steps(),
    artifact_exists=st.booleans(),
    summary=_summaries,
)
def test_status_total_in_enum_and_deterministic(
    steps: list[PlanStep],
    artifact_exists: bool,
    summary: str,
) -> None:
    """Property 3: status is total, in-enum, deterministic, with failed-step rule.

    **Validates: Requirements 7.1, 7.2, 7.4**

    For any ``(steps, artifact_exists, summary)``, :func:`derive_status`
    returns exactly one :class:`RunStatus` member; calling it twice with
    identical inputs returns the identical value; and when at least one step
    failed the result is ``partial`` if an artifact exists else ``failed``.
    """

    result = derive_status(steps, artifact_exists, summary)

    # Totality / in-enum: the result is always a RunStatus member.
    assert isinstance(result, RunStatus)
    assert result in set(RunStatus)

    # Determinism / idempotence: identical inputs produce the identical value.
    assert derive_status(steps, artifact_exists, summary) == result

    # Failed-step rule (Req 7.4): partial if artifact else failed.
    if any(s.status is StepStatus.FAILED for s in steps):
        assert result == (RunStatus.PARTIAL if artifact_exists else RunStatus.FAILED)
