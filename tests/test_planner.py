"""Unit tests for the multi-step planner (`app.agent.planner`).

These example-based tests (Task 10.2) drive :class:`Planner` through the
network-free fake LLM backend (see ``tests/conftest.py``) and assert two
behaviors:

- **Happy path** — when the backend returns a valid ``Plan`` JSON with two or
  more sequential steps and enumerated assumptions, :meth:`Planner.make_plan`
  returns a :class:`Plan` carrying exactly those steps and assumptions
  (Req 2.1, 2.2, 2.3).
- **Failure escalation** — when the fake backend fails on every backend (both
  Groq and Ollama), :meth:`Planner.make_plan` raises :class:`PlanningError`
  carrying the ``run_id``, a non-empty reason, and a non-empty retry history
  (Req 2.6).

**Validates: Requirements 2.6**
"""

from __future__ import annotations

import json

import pytest

from app.agent.planner import Planner, PlanningError
from app.core.config import Settings
from app.models.schemas import Plan
from app.services.llm import LLMService
from tests.conftest import FakeLLMBackend


async def _noop_sleep(_delay: float) -> None:
    """A no-op async sleep so retry backoff introduces no real delay."""

    return None


def _planner_with(
    *,
    groq: FakeLLMBackend,
    ollama: FakeLLMBackend,
) -> Planner:
    """Build a :class:`Planner` wired to the given fake backends.

    Args:
        groq: The scripted primary (Groq) fake backend.
        ollama: The scripted fallback (Ollama) fake backend.

    Returns:
        A planner backed by an :class:`LLMService` using the fakes.
    """

    settings = Settings(GROQ_API_KEY="test-key")
    llm = LLMService(
        settings, groq_backend=groq, ollama_backend=ollama, sleep=_noop_sleep
    )
    return Planner(llm)


def _valid_plan_json() -> str:
    """Render a valid ``Plan`` JSON payload with two steps and assumptions."""

    return json.dumps(
        {
            "steps": [
                {
                    "step": 1,
                    "task": "research",
                    "description": "Research CRM cloud-migration best practices.",
                    "expected_output": "A set of researched facts and references.",
                },
                {
                    "step": 2,
                    "task": "build_docx",
                    "description": "Assemble the drafted content into a .docx.",
                    "expected_output": "A polished Word proposal document.",
                },
            ],
            "assumptions": [
                "The target cloud provider is unspecified; assuming a major "
                "public cloud.",
                "The audience is executive leadership.",
            ],
        }
    )


async def test_make_plan_happy_path_returns_plan_with_steps_and_assumptions() -> None:
    """A valid Plan JSON yields a Plan with the given steps and assumptions (Req 2.1-2.3)."""

    payload = _valid_plan_json()
    planner = _planner_with(
        groq=FakeLLMBackend("groq", response=payload),
        ollama=FakeLLMBackend("ollama", response=payload),
    )

    plan = await planner.make_plan(
        "Create a project proposal for migrating our on-premise CRM to the cloud.",
        run_id="run-happy",
    )

    assert isinstance(plan, Plan)
    assert [s.step for s in plan.steps] == [1, 2]
    assert plan.steps[0].task == "research"
    assert plan.steps[1].task == "build_docx"
    assert len(plan.assumptions) == 2
    assert any("cloud provider" in a for a in plan.assumptions)


async def test_make_plan_escalates_to_planning_error_when_all_backends_fail() -> None:
    """When both backends always fail, make_plan raises PlanningError (Req 2.6).

    The raised error must carry the originating ``run_id``, a non-empty
    human-readable reason, and a non-empty retry history for the 503 body.
    """

    planner = _planner_with(
        groq=FakeLLMBackend("groq", always_fail=True),
        ollama=FakeLLMBackend("ollama", always_fail=True),
    )

    with pytest.raises(PlanningError) as exc_info:
        await planner.make_plan("Draft a proposal.", run_id="run-fail")

    error = exc_info.value
    assert error.run_id == "run-fail"
    assert error.reason
    assert len(error.retry_history) >= 1
    assert error.retry_history[0].error
