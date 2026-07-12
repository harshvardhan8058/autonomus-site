"""Per-run Server-Sent-Events (SSE) fan-out bus for the Autonomous Agent Service.

This module defines :class:`EventBus`, the in-process pub/sub primitive that
carries :data:`~app.models.schemas.AgentEvent` instances from the Orchestrator
to the SSE stream endpoint. Each Run has its own set of subscriber queues keyed
by ``run_id``, so an event is delivered only to subscribers of the Run it
belongs to (Req 16.2). This per-run keying is what guarantees that a subscriber
watching Run A never receives Run B's events (property P11, Req 16.1).

The bus does two things when an event is published:

1. Appends the event to the Run's replay buffer on its
   :class:`~app.models.schemas.RunState` (when the Run state is supplied), so a
   late subscriber that opens the stream after some events already fired can
   replay the full history (Req 6.1).
2. Fans the event out to every live subscriber queue registered for
   ``event.run_id`` only (Req 16.2).

Publishing is designed to **never raise to the caller**: the Orchestrator emits
events as a side effect of doing real work, so a slow or broken subscriber must
never be able to fail a Run (availability over observability). Delivery is
therefore best-effort and any per-queue error is swallowed.
"""

from __future__ import annotations

import asyncio
import contextlib

from app.models.schemas import AgentEvent, RunState


class EventBus:
    """In-process, per-run SSE event fan-out (Req 6.1, 16.1, 16.2).

    The bus maintains, for each ``run_id``, a list of :class:`asyncio.Queue`
    subscriber queues. Publishing an event appends it to the owning Run's replay
    buffer (when provided) and delivers it only to that Run's subscribers, which
    keeps SSE streams isolated per Run (Req 16.2).

    Attributes:
        _queues: Mapping of ``run_id`` to the list of live subscriber queues for
            that Run.
    """

    def __init__(self) -> None:
        """Initialize the bus with no subscribers."""

        self._queues: dict[str, list[asyncio.Queue[AgentEvent]]] = {}

    async def publish(
        self, event: AgentEvent, run_state: RunState | None = None
    ) -> None:
        """Buffer and fan out an event to its Run's subscribers (Req 6.1, 16.2).

        The event is first appended to ``run_state.events`` (the replay buffer)
        when a Run state is supplied, so late subscribers can replay it. It is
        then delivered to every live subscriber queue registered for
        ``event.run_id`` only, never to subscribers of any other Run
        (Req 16.2).

        This method never raises to the caller: appending to the replay buffer
        and delivering to each queue are performed on a best-effort basis and
        any error is suppressed, so publishing an event can never fail a Run
        (Req 6.1).

        Args:
            event: The concrete :data:`~app.models.schemas.AgentEvent` union
                instance to publish. Its ``run_id`` determines the target Run.
            run_state: The owning Run's state, whose ``events`` replay buffer the
                event is appended to. When ``None``, no buffering is performed
                and the event is only fanned out.
        """

        run_id = event.run_id

        if run_state is not None:
            # observability must never fail a Run
            with contextlib.suppress(Exception):
                run_state.events.append(event)

        for queue in list(self._queues.get(run_id, ())):
            # a broken subscriber must not fail a Run
            with contextlib.suppress(Exception):
                queue.put_nowait(event)

    async def subscribe(self, run_id: str) -> asyncio.Queue[AgentEvent]:
        """Register and return a fresh subscriber queue for ``run_id``.

        Each caller receives its own queue, so multiple concurrent subscribers
        to the same Run each get an independent copy of every event.

        Args:
            run_id: The Run whose events the subscriber wants to receive.

        Returns:
            A new :class:`asyncio.Queue` registered to receive only events whose
            ``run_id`` matches (Req 16.2).
        """

        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._queues.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[AgentEvent]) -> None:
        """Remove a subscriber queue and drop the Run entry when it is empty.

        Called when a client disconnects or after the terminal event is
        delivered. Removing the last subscriber for a Run also removes the Run's
        entry so the bus does not retain empty per-Run structures (cleanup).

        Args:
            run_id: The Run the queue was subscribed to.
            queue: The subscriber queue to remove. Unknown queues are ignored.
        """

        queues = self._queues.get(run_id)
        if queues is None:
            return
        with contextlib.suppress(ValueError):
            queues.remove(queue)
        if not queues:
            self._queues.pop(run_id, None)
