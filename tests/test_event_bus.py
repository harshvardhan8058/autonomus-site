"""Tests for the per-run SSE event bus (`app.core.event_bus`).

Covers:
- Task 6.2 (unit): concrete subscribe / publish / unsubscribe behavior, replay
  buffering onto ``RunState``, empty-run cleanup, and the guarantee that
  ``publish`` never raises to the caller (Req 6.1, 16.1, 16.2).
- Task 6.3 / Property 10: every event published to a Run's stream is
  well-formed and typed — its ``type`` is one of the seven
  :class:`SSEEventType` values and it carries a non-empty ``run_id``, a
  ``type``, a ``timestamp``, and the payload fields required for that type
  (Req 6.1, 6.2).
- Task 6.4 / Property 11: SSE streams are isolated per run — with events
  interleaved across multiple concurrent runs, a subscriber to a given
  ``run_id`` receives only events whose ``run_id`` matches (Req 16.1, 16.2).

Property tests use Hypothesis with a minimum of 100 iterations. The event bus is
async and backed by :class:`asyncio.Queue`, so each generated example is driven
through :func:`asyncio.run` (pytest-asyncio is used for the example-based async
unit tests).
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.event_bus import EventBus
from app.models.schemas import (
    AgentEvent,
    Plan,
    PlanCreatedEvent,
    PlanningStartedEvent,
    PlanStep,
    ReflectionEvent,
    RunCompletedEvent,
    RunState,
    RunStatus,
    SSEEventType,
    StepCompletedEvent,
    StepFailedEvent,
    StepStartedEvent,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_run_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-",
    min_size=1,
    max_size=12,
)
_nonempty_text = st.text(min_size=1, max_size=20)


@st.composite
def _plans(draw: st.DrawFn) -> Plan:
    """Build a schema-valid :class:`Plan` with sequential 1..n steps (n >= 2)."""

    n = draw(st.integers(min_value=2, max_value=4))
    steps = [
        PlanStep(
            step=i,
            task=draw(_nonempty_text),
            description=draw(_nonempty_text),
            expected_output=draw(_nonempty_text),
        )
        for i in range(1, n + 1)
    ]
    assumptions = draw(st.lists(_nonempty_text, max_size=3))
    return Plan(steps=steps, assumptions=assumptions)


@st.composite
def _events(draw: st.DrawFn, rid_strategy: st.SearchStrategy[str] = _run_ids) -> AgentEvent:
    """Build one arbitrary, well-formed :data:`AgentEvent` for a drawn run_id.

    Args:
        draw: Hypothesis draw function.
        rid_strategy: Strategy producing the ``run_id`` the event is tagged with;
            defaults to arbitrary run ids. Callers pass a constrained strategy
            (e.g. ``st.sampled_from(ids)``) to interleave events across a known
            set of concurrent runs.
    """

    rid = draw(rid_strategy)
    etype = draw(st.sampled_from(list(SSEEventType)))

    if etype is SSEEventType.PLANNING_STARTED:
        return PlanningStartedEvent(run_id=rid)
    if etype is SSEEventType.PLAN_CREATED:
        plan = draw(_plans())
        return PlanCreatedEvent(run_id=rid, plan=plan, assumptions=list(plan.assumptions))
    if etype is SSEEventType.STEP_STARTED:
        return StepStartedEvent(
            run_id=rid, step=draw(st.integers(min_value=1, max_value=20)), task=draw(_nonempty_text)
        )
    if etype is SSEEventType.STEP_COMPLETED:
        return StepCompletedEvent(
            run_id=rid,
            step=draw(st.integers(min_value=1, max_value=20)),
            output_summary=draw(_nonempty_text),
        )
    if etype is SSEEventType.STEP_FAILED:
        return StepFailedEvent(
            run_id=rid,
            step=draw(st.integers(min_value=1, max_value=20)),
            error=draw(_nonempty_text),
        )
    if etype is SSEEventType.REFLECTION:
        return ReflectionEvent(
            run_id=rid,
            findings=draw(_nonempty_text),
            revised_sections=draw(st.lists(_nonempty_text, max_size=3)),
        )
    # RUN_COMPLETED
    return RunCompletedEvent(
        run_id=rid,
        status=draw(st.sampled_from(list(RunStatus))),
        summary=draw(st.text(max_size=20)),
        document_url=draw(st.one_of(st.none(), _nonempty_text)),
    )


@st.composite
def _multi_run_events(draw: st.DrawFn) -> tuple[list[str], list[AgentEvent]]:
    """Draw a set of distinct run ids and events interleaved across them."""

    ids = draw(st.lists(_run_ids, min_size=2, max_size=4, unique=True))
    events = draw(st.lists(_events(st.sampled_from(ids)), min_size=1, max_size=25))
    return ids, events


_ALL_TYPE_VALUES = {t.value for t in SSEEventType}


def _assert_well_formed(event: AgentEvent) -> None:
    """Assert a single emitted event satisfies Property 10.

    Every event's ``type`` is one of the seven :class:`SSEEventType` values and
    it carries a non-empty ``run_id``, a ``type``, a ``timestamp``, and the
    payload fields required for its specific type.
    """

    assert isinstance(event.type, SSEEventType)
    assert event.type.value in _ALL_TYPE_VALUES
    assert isinstance(event.run_id, str) and len(event.run_id) > 0
    assert isinstance(event.timestamp, datetime)

    t = event.type
    if t is SSEEventType.PLAN_CREATED:
        assert isinstance(event.plan, Plan)
        assert len(event.plan.steps) >= 2
        assert isinstance(event.assumptions, list)
    elif t is SSEEventType.STEP_STARTED:
        assert isinstance(event.step, int) and event.step >= 1
        assert isinstance(event.task, str) and len(event.task) > 0
    elif t is SSEEventType.STEP_COMPLETED:
        assert isinstance(event.step, int) and event.step >= 1
        assert isinstance(event.output_summary, str) and len(event.output_summary) > 0
    elif t is SSEEventType.STEP_FAILED:
        assert isinstance(event.step, int) and event.step >= 1
        assert isinstance(event.error, str) and len(event.error) > 0
    elif t is SSEEventType.REFLECTION:
        assert isinstance(event.findings, str) and len(event.findings) > 0
        assert isinstance(event.revised_sections, list)
    elif t is SSEEventType.RUN_COMPLETED:
        assert isinstance(event.status, RunStatus)
        assert isinstance(event.summary, str)
        assert event.document_url is None or isinstance(event.document_url, str)
    # PLANNING_STARTED carries only the base fields, already asserted above.


def _new_state(run_id: str) -> RunState:
    """Create a minimal :class:`RunState` for buffering events in tests."""

    return RunState(run_id=run_id, request="req", client_ip="127.0.0.1")


# ---------------------------------------------------------------------------
# Task 6.3 / Property 10
# ---------------------------------------------------------------------------


# Feature: autonomous-agent-service, Property 10: Every emitted SSE event is well-formed and typed
@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(events=st.lists(_events(), min_size=1, max_size=15))
def test_property_10_events_well_formed(events: list[AgentEvent]) -> None:
    """Property 10: every published event is well-formed and typed.

    **Validates: Requirements 6.1, 6.2**

    For any Run, every event delivered to a subscriber's queue and every event
    recorded in the Run's replay buffer has a ``type`` within the seven allowed
    :class:`SSEEventType` values and carries a non-empty ``run_id``, a ``type``,
    a ``timestamp``, and the payload fields required for that event type.
    """

    async def _run() -> None:
        bus = EventBus()
        by_run: dict[str, list[AgentEvent]] = {}
        for ev in events:
            by_run.setdefault(ev.run_id, []).append(ev)

        states = {rid: _new_state(rid) for rid in by_run}
        queues = {rid: await bus.subscribe(rid) for rid in by_run}

        for ev in events:
            await bus.publish(ev, states[ev.run_id])

        # Every fanned-out event is well-formed.
        for queue in queues.values():
            while not queue.empty():
                _assert_well_formed(queue.get_nowait())

        # Every buffered (replayable) event is well-formed and preserved in order.
        for rid, state in states.items():
            assert state.events == by_run[rid]
            for buffered in state.events:
                _assert_well_formed(buffered)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Task 6.4 / Property 11
# ---------------------------------------------------------------------------


# Feature: autonomous-agent-service, Property 11: SSE streams are isolated per run
@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(data=_multi_run_events())
def test_property_11_streams_isolated_per_run(
    data: tuple[list[str], list[AgentEvent]],
) -> None:
    """Property 11: a subscriber receives only events for its own run.

    **Validates: Requirements 16.1, 16.2**

    With events interleaved across multiple concurrently active runs, each
    subscriber's queue contains exactly the events whose ``run_id`` matches its
    subscription — in publish order — and never any other run's events.
    """

    ids, events = data

    async def _run() -> None:
        bus = EventBus()
        states = {rid: _new_state(rid) for rid in ids}
        queues = {rid: await bus.subscribe(rid) for rid in ids}

        for ev in events:
            await bus.publish(ev, states[ev.run_id])

        for rid in ids:
            received: list[AgentEvent] = []
            queue = queues[rid]
            while not queue.empty():
                received.append(queue.get_nowait())

            expected = [ev for ev in events if ev.run_id == rid]
            assert received == expected
            for ev in received:
                assert ev.run_id == rid

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Task 6.2 unit tests
# ---------------------------------------------------------------------------


async def test_subscribe_and_publish_delivers_event() -> None:
    """A subscriber receives an event published for its run (Req 16.2)."""

    bus = EventBus()
    state = _new_state("run-1")
    queue = await bus.subscribe("run-1")

    event = PlanningStartedEvent(run_id="run-1")
    await bus.publish(event, state)

    delivered = queue.get_nowait()
    assert delivered is event
    # The event is also appended to the run's replay buffer.
    assert state.events == [event]


async def test_publish_only_reaches_matching_run() -> None:
    """Publishing to one run never reaches another run's subscriber (Req 16.2)."""

    bus = EventBus()
    q_a = await bus.subscribe("run-a")
    q_b = await bus.subscribe("run-b")

    await bus.publish(StepStartedEvent(run_id="run-a", step=1, task="research"))

    assert q_a.qsize() == 1
    assert q_b.empty()


async def test_publish_without_run_state_only_fans_out() -> None:
    """When no run state is supplied, the event is delivered but not buffered."""

    bus = EventBus()
    queue = await bus.subscribe("run-1")

    event = ReflectionEvent(run_id="run-1", findings="looks good")
    await bus.publish(event)

    assert queue.get_nowait() is event


async def test_publish_never_raises_with_no_subscribers() -> None:
    """Publishing to a run with no subscribers is a safe no-op (Req 6.1)."""

    bus = EventBus()
    state = _new_state("run-x")
    # Must not raise even though nobody is listening.
    await bus.publish(RunCompletedEvent(run_id="run-x", status=RunStatus.COMPLETED,
                                        summary="done"), state)
    assert len(state.events) == 1


async def test_publish_never_raises_when_subscriber_queue_is_broken() -> None:
    """A failing subscriber queue never propagates an error to the caller (Req 6.1)."""

    bus = EventBus()
    queue = await bus.subscribe("run-1")

    def _boom(_item: object) -> None:
        raise RuntimeError("queue is broken")

    # Simulate a broken/slow subscriber whose delivery raises.
    queue.put_nowait = _boom  # type: ignore[method-assign]

    # publish must swallow the error rather than fail the caller (the Run).
    await bus.publish(PlanningStartedEvent(run_id="run-1"))


async def test_unsubscribe_removes_queue_and_cleans_up_empty_run() -> None:
    """Unsubscribing the last queue drops the run entry entirely (cleanup)."""

    bus = EventBus()
    queue = await bus.subscribe("run-1")

    bus.unsubscribe("run-1", queue)

    # After removing the only subscriber, publishing reaches no one and the
    # internal per-run structure has been cleaned up.
    assert "run-1" not in bus._queues


async def test_unsubscribe_keeps_other_subscribers() -> None:
    """Unsubscribing one queue leaves the run's other subscribers intact."""

    bus = EventBus()
    q1 = await bus.subscribe("run-1")
    q2 = await bus.subscribe("run-1")

    bus.unsubscribe("run-1", q1)
    await bus.publish(PlanningStartedEvent(run_id="run-1"))

    assert q1.empty()
    assert q2.qsize() == 1


async def test_unsubscribe_unknown_queue_is_safe() -> None:
    """Unsubscribing a queue that was never registered is a no-op."""

    bus = EventBus()
    stray: asyncio.Queue = asyncio.Queue()
    # Should not raise for an unknown run or unknown queue.
    bus.unsubscribe("nope", stray)
