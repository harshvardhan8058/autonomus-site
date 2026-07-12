"""Property-based tests for the Pydantic v2 schemas (`app.models.schemas`).

These tests validate three design correctness properties for the data models:

- **Property 19** (Req 1.1): ``AgentRequest`` accepts a string if and only if it
  contains at least one non-whitespace character.
- **Property 4** (Req 2.1): a ``Plan`` is accepted only when its step numbers are
  sequential ``1..n`` (``n >= 2``) with non-empty content; non-sequential
  numbering is rejected.
- **Property 5** (Req 2.2): serializing a ``Plan`` to JSON and parsing it back
  reproduces an equal ``Plan``.

All property tests use Hypothesis with a minimum of 100 iterations.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.models.schemas import AgentRequest, Plan, PlanStep, StepStatus

# --- Strategies -------------------------------------------------------------

# Non-empty, non-whitespace text used for plan step content fields.
_nonempty_text = st.text(min_size=1).filter(lambda s: s.strip() != "")


@st.composite
def _sequential_plans(draw: st.DrawFn) -> Plan:
    """Generate a valid Plan with sequential steps 1..n (n >= 2).

    Each step carries non-empty ``task``, ``description``, and
    ``expected_output`` values, an arbitrary status, and an arbitrary
    dependency list drawn from valid step numbers.
    """

    n = draw(st.integers(min_value=2, max_value=8))
    steps: list[PlanStep] = []
    for i in range(1, n + 1):
        depends_on = draw(
            st.lists(st.integers(min_value=1, max_value=n), max_size=3)
        )
        steps.append(
            PlanStep(
                step=i,
                task=draw(_nonempty_text),
                description=draw(_nonempty_text),
                expected_output=draw(_nonempty_text),
                status=draw(st.sampled_from(list(StepStatus))),
                depends_on=depends_on,
            )
        )
    assumptions = draw(st.lists(st.text(), max_size=4))
    return Plan(steps=steps, assumptions=assumptions)


# --- Property 19 ------------------------------------------------------------


# Feature: autonomous-agent-service, Property 19: Request validation accepts iff non-blank
@pytest.mark.property
@settings(max_examples=200)
@given(value=st.text())
def test_agent_request_accepts_iff_non_blank(value: str) -> None:
    """Property 19: AgentRequest accepts a string iff it is non-blank.

    **Validates: Requirements 1.1**

    For any string, ``AgentRequest`` validates successfully if and only if the
    string contains at least one non-whitespace character; otherwise a
    ``ValidationError`` is raised (which the API maps to HTTP 422).
    """

    has_non_whitespace = value.strip() != ""
    if has_non_whitespace:
        req = AgentRequest(request=value)
        assert req.request == value
    else:
        with pytest.raises(ValidationError):
            AgentRequest(request=value)


# --- Property 4 -------------------------------------------------------------


# Feature: autonomous-agent-service, Property 4: Plan step numbers are sequential 1..n
@pytest.mark.property
@settings(max_examples=100)
@given(plan=_sequential_plans())
def test_plan_accepts_sequential_steps(plan: Plan) -> None:
    """Property 4 (acceptance): sequential 1..n plans are accepted.

    **Validates: Requirements 2.1**

    Any generated plan with ``n >= 2`` steps numbered exactly ``1..n`` and
    non-empty ``task``/``description``/``expected_output`` is accepted, and its
    step numbers read back as the sequence ``1..n``.
    """

    n = len(plan.steps)
    assert n >= 2
    assert [s.step for s in plan.steps] == list(range(1, n + 1))
    for s in plan.steps:
        assert s.task.strip() != ""
        assert s.description.strip() != ""
        assert s.expected_output.strip() != ""


# Feature: autonomous-agent-service, Property 4: Plan step numbers are sequential 1..n
@pytest.mark.property
@settings(max_examples=100)
@given(
    n=st.integers(min_value=2, max_value=8),
    data=st.data(),
)
def test_plan_rejects_non_sequential_steps(n: int, data: st.DataObject) -> None:
    """Property 4 (rejection): non-sequential numbering is rejected.

    **Validates: Requirements 2.1**

    For any plan of ``n >= 2`` steps whose numbering is perturbed away from the
    exact sequence ``1..n``, construction raises a ``ValidationError``.
    """

    numbers = list(range(1, n + 1))
    # Perturb the numbering so it is guaranteed not to equal 1..n in order.
    strategy = st.one_of(
        # Swap two distinct positions.
        st.tuples(
            st.integers(min_value=0, max_value=n - 1),
            st.integers(min_value=0, max_value=n - 1),
        ).filter(lambda ij: ij[0] != ij[1]),
    )
    i, j = data.draw(strategy)
    perturbed = numbers.copy()
    perturbed[i], perturbed[j] = perturbed[j], perturbed[i]
    assert perturbed != numbers  # guaranteed by distinct swap of distinct values

    steps = [
        PlanStep(
            step=num,
            task="t",
            description="d",
            expected_output="e",
        )
        for num in perturbed
    ]
    with pytest.raises(ValidationError):
        Plan(steps=steps)


# --- Property 5 -------------------------------------------------------------


# Feature: autonomous-agent-service, Property 5: Plan JSON round-trip is identity
@pytest.mark.property
@settings(max_examples=100)
@given(plan=_sequential_plans())
def test_plan_json_round_trip_is_identity(plan: Plan) -> None:
    """Property 5: Plan JSON round-trip reproduces an equal Plan.

    **Validates: Requirements 2.2**

    For any valid Plan, ``Plan.model_validate_json(plan.model_dump_json())``
    equals the original plan.
    """

    round_tripped = Plan.model_validate_json(plan.model_dump_json())
    assert round_tripped == plan
