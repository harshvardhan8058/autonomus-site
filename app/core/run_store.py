"""In-memory, per-run state store for the Autonomous Agent Service.

This module defines :class:`RunStore`, the component that owns the authoritative
:class:`~app.models.schemas.RunState` record for every Run, keyed by
``run_id``. The response endpoint, the SSE stream, and the document endpoints
all read Run state through this store, and the Orchestrator mutates a Run's
state through it while a Run executes.

State is isolated strictly by ``run_id``: each Run has its own
:class:`~app.models.schemas.RunState`, so concurrently processed Runs never
observe or corrupt one another's state (Req 16.1). Looking up an unknown
``run_id`` returns ``None`` so the API layer can respond with a distinguishing
HTTP 404 body rather than leaking another Run's data (Req 16.4).

The store is a thin wrapper over an in-memory ``dict`` and is intended for the
single-process, single-event-loop deployment described in the design. Each
:class:`~app.models.schemas.RunState` is mutated only by its own Run's
coroutine, which provides isolation without additional locking.
"""

from __future__ import annotations

from app.models.schemas import RunState


class RunStore:
    """In-memory ``run_id`` to :class:`RunState` store (Req 16.1, 16.4).

    The store keeps a single :class:`~app.models.schemas.RunState` per Run,
    keyed by ``run_id``. Isolation is guaranteed by the key: a Run can only ever
    read or write its own state, and an unknown key resolves to ``None`` rather
    than to another Run's record.

    Attributes:
        _runs: Mapping of ``run_id`` to that Run's :class:`RunState`.
    """

    def __init__(self) -> None:
        """Initialize an empty store with no Runs."""

        self._runs: dict[str, RunState] = {}

    def create(self, run_id: str, request: str, client_ip: str) -> RunState:
        """Create and register a fresh :class:`RunState` for a new Run.

        The Run is stored keyed by ``run_id`` before execution begins, so the
        stream and document endpoints can resolve it immediately (Req 16.1).

        Args:
            run_id: The unique identifier to assign to the new Run.
            request: The original natural-language request for the Run.
            client_ip: The IP address of the client that submitted the request.

        Returns:
            The newly created and stored :class:`RunState`.
        """

        run_state = RunState(run_id=run_id, request=request, client_ip=client_ip)
        self._runs[run_id] = run_state
        return run_state

    def get(self, run_id: str) -> RunState | None:
        """Return the :class:`RunState` for ``run_id`` or ``None`` if unknown.

        A ``None`` result signals an unknown Run so the API layer can return a
        distinguishing HTTP 404 body (Req 6.3, 9.4, 16.4).

        Args:
            run_id: The identifier of the Run to look up.

        Returns:
            The stored :class:`RunState`, or ``None`` when no Run is registered
            under ``run_id``.
        """

        return self._runs.get(run_id)

    def update(self, run_state: RunState) -> None:
        """Persist the current state of a Run, keyed by its ``run_id``.

        The store keeps a single record per Run, so updating re-associates the
        Run's ``run_id`` with the provided (typically mutated) state, leaving
        all other Runs untouched (Req 16.1).

        Args:
            run_state: The Run state to store under ``run_state.run_id``.
        """

        self._runs[run_state.run_id] = run_state
