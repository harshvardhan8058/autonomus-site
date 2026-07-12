"""Tests for the in-memory per-run state store (`app.core.run_store`).

Covers Task 6.1: creating a Run's state, retrieving it by ``run_id``, updating
it in place, returning ``None`` for unknown ids, and isolating state strictly by
``run_id`` so concurrent Runs never corrupt one another (Req 16.1, 16.4).
"""

from __future__ import annotations

from app.core.run_store import RunStore
from app.models.schemas import RunState, RunStatus


def test_create_returns_pending_runstate_with_fields() -> None:
    """create() returns a fresh RunState seeded from its arguments (Req 16.1)."""

    store = RunStore()
    state = store.create("run-1", request="write a proposal", client_ip="10.0.0.1")

    assert isinstance(state, RunState)
    assert state.run_id == "run-1"
    assert state.request == "write a proposal"
    assert state.client_ip == "10.0.0.1"
    assert state.status is RunStatus.PENDING


def test_get_returns_created_state() -> None:
    """get() returns the exact state instance created for a run_id (Req 16.1)."""

    store = RunStore()
    created = store.create("run-1", request="req", client_ip="ip")

    assert store.get("run-1") is created


def test_get_unknown_run_returns_none() -> None:
    """get() returns None for an unknown run_id (Req 16.4)."""

    store = RunStore()
    assert store.get("does-not-exist") is None


def test_update_persists_mutations() -> None:
    """update() persists a mutated RunState under its run_id (Req 16.1)."""

    store = RunStore()
    state = store.create("run-1", request="req", client_ip="ip")

    state.status = RunStatus.COMPLETED
    state.summary = "all done"
    store.update(state)

    reloaded = store.get("run-1")
    assert reloaded is not None
    assert reloaded.status is RunStatus.COMPLETED
    assert reloaded.summary == "all done"


def test_state_is_isolated_per_run_id() -> None:
    """Distinct runs keep independent state; mutating one never affects another (Req 16.1)."""

    store = RunStore()
    a = store.create("run-a", request="req-a", client_ip="ip-a")
    b = store.create("run-b", request="req-b", client_ip="ip-b")

    a.status = RunStatus.FAILED
    a.summary = "a failed"
    store.update(a)

    fetched_b = store.get("run-b")
    assert fetched_b is b
    assert fetched_b.status is RunStatus.PENDING
    assert fetched_b.summary == ""
    assert fetched_b.request == "req-b"


def test_update_of_new_run_id_registers_it() -> None:
    """update() with an unseen run_id registers that state (idempotent store)."""

    store = RunStore()
    state = RunState(run_id="run-z", request="req", client_ip="ip")

    store.update(state)

    assert store.get("run-z") is state
