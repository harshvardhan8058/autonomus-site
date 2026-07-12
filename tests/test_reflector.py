"""Tests for the Reflector (`app.agent.reflector`).

Covers Task 14.2 (unit): single-pass reflection and best-effort degradation
(Req 4.1, 4.2, 4.3):

- (a) When the fake backend returns a reflection with weak sections and revised
  content, :meth:`Reflector.reflect` performs at most one pass (the LLM is
  invoked exactly once), records the findings on the ``RunState``, emits exactly
  one ``reflection`` event, and returns the revised sections.
- (b) When the fake backend fails on all backends, :meth:`Reflector.reflect`
  does NOT raise, still emits a ``reflection`` event, records a no-revision
  result, and leaves the Run unaffected.

All LLM interactions go through the scriptable :class:`FakeLLMBackend` from
``tests/conftest.py`` — no network calls are made.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from app.agent.reflector import Reflector
from app.core.event_bus import EventBus
from app.models.schemas import RunState, SSEEventType
from app.services.llm import LLMService
from tests.conftest import FakeLLMBackend


def _new_run_state(run_id: str = "run-reflect") -> RunState:
    """Build a fresh :class:`RunState` for reflection tests."""

    return RunState(
        run_id=run_id,
        request="Create a project proposal for migrating our CRM to the cloud.",
        client_ip="127.0.0.1",
    )


async def test_reflect_records_findings_emits_event_and_returns_revisions(
    make_llm_service: Callable[..., LLMService],
) -> None:
    """A successful reflection is single-pass, records findings, emits one event.

    Validates: Requirements 4.1, 4.2, 4.3
    """

    reflection_json = json.dumps(
        {
            "findings": "The cost section is thin and the timeline is missing.",
            "weak_sections": ["Cost Analysis", "Timeline"],
            "revised_sections": [
                {"title": "Cost Analysis", "content": "Detailed three-year TCO..."},
                {"title": "Timeline", "content": "Phase 1 (weeks 1-4)..."},
            ],
        }
    )
    groq = FakeLLMBackend("groq", response=reflection_json)
    ollama = FakeLLMBackend("ollama", response=reflection_json)
    llm = make_llm_service(groq=groq, ollama=ollama)

    bus = EventBus()
    state = _new_run_state()
    reflector = Reflector(llm, bus)

    result = await reflector.reflect(state, assembled_output="A short draft.")

    # At most one revision pass: the primary backend is invoked exactly once and
    # the fallback backend is never reached (no loop, Req 4.2).
    assert groq.call_count == 1
    assert ollama.call_count == 0

    # Findings recorded on the run state (Req 4.3).
    assert state.reflection_findings == (
        "The cost section is thin and the timeline is missing."
    )

    # Exactly one reflection event emitted, carrying findings + revised titles.
    reflection_events = [
        e for e in state.events if e.type is SSEEventType.REFLECTION
    ]
    assert len(reflection_events) == 1
    event = reflection_events[0]
    assert event.run_id == state.run_id
    assert event.findings == state.reflection_findings
    assert event.revised_sections == ["Cost Analysis", "Timeline"]

    # The returned result exposes the single-pass revisions.
    assert result.findings == state.reflection_findings
    assert result.revised_sections == ["Cost Analysis", "Timeline"]
    assert result.revised_content == {
        "Cost Analysis": "Detailed three-year TCO...",
        "Timeline": "Phase 1 (weeks 1-4)...",
    }


async def test_reflect_does_not_raise_when_all_backends_fail(
    make_llm_service: Callable[..., LLMService],
) -> None:
    """Reflection failure never fails the Run; a no-revision result is returned.

    Validates: Requirements 4.1, 4.2, 4.3
    """

    groq = FakeLLMBackend("groq", always_fail=True)
    ollama = FakeLLMBackend("ollama", always_fail=True)
    llm = make_llm_service(groq=groq, ollama=ollama)

    bus = EventBus()
    state = _new_run_state("run-reflect-fail")
    # Snapshot fields that must be unaffected by a failed reflection.
    status_before = state.status
    plan_before = state.plan
    summary_before = state.summary
    reflector = Reflector(llm, bus)

    # Must not raise despite every backend failing.
    result = await reflector.reflect(state, assembled_output="A short draft.")

    # A reflection event is still emitted (with empty findings) so the stream
    # observes that the reflection stage ran (Req 4.3).
    reflection_events = [
        e for e in state.events if e.type is SSEEventType.REFLECTION
    ]
    assert len(reflection_events) == 1
    assert reflection_events[0].findings == ""
    assert reflection_events[0].revised_sections == []

    # A no-revision result is returned.
    assert result.findings == ""
    assert result.revised_sections == []
    assert result.revised_content is None

    # The Run is otherwise unaffected.
    assert state.reflection_findings == ""
    assert state.status is status_before
    assert state.plan is plan_before
    assert state.summary == summary_before
