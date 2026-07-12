"""Structured, availability-first logging for the Autonomous Agent Service.

This module defines :class:`StructuredLogger`, the single logging entry point
used by the Planner, Executor, Reflector, and the API layer. It emits
structured JSON log entries and is designed so that **observability never
compromises availability** (Req 15): if emitting a log entry fails, the logger
attempts a single best-effort fallback write to standard error and then
suppresses the error so the calling component keeps running.

Two structured entry kinds are supported:

- :meth:`StructuredLogger.decision` records a Planner/Executor/Reflector
  decision with the component name, the ``run_id``, the decision text, a UTC
  timestamp, a severity level, and any extra structured fields (Req 15.1, 15.2).
- :meth:`StructuredLogger.security_event` records the rejection of a malicious
  request with a UTC timestamp, the client IP, a **hash** of the request, and
  the rejection reason — never the verbatim malicious payload (Req 1.5).

The underlying sink is injectable so that tests can supply a failing sink to
verify the graceful-degradation contract (property P17).
"""

from __future__ import annotations

import contextlib
import json
import sys
from datetime import UTC, datetime
from typing import Any, Protocol, TextIO


class Sink(Protocol):
    """A structured-log sink: a callable that consumes one serialized entry.

    A sink receives a single already-serialized JSON string (one log entry, no
    trailing newline) and is responsible for writing it wherever appropriate
    (stdout, a file, a log aggregator, etc.). A sink MAY raise; the
    :class:`StructuredLogger` is responsible for containing any such failure.
    """

    def __call__(self, entry: str) -> None:  # pragma: no cover - structural only
        """Consume a single serialized JSON log entry."""
        ...


def _default_sink(entry: str) -> None:
    """Write a serialized log entry to standard output as one line.

    Args:
        entry: The already-serialized JSON log entry.
    """

    sys.stdout.write(entry + "\n")
    sys.stdout.flush()


class StructuredLogger:
    """Emit structured JSON logs without ever breaking a Run (Req 15).

    The logger serializes each entry to JSON and hands it to an injectable
    sink. If the sink (or serialization) raises, the logger performs a single
    best-effort fallback write to standard error and then suppresses the error
    so that logging failures degrade observability without affecting
    availability (Req 15.2).

    Attributes:
        _sink: The primary sink that consumes serialized entries.
        _stderr: The stream used for the single best-effort fallback write.
    """

    def __init__(
        self,
        sink: Sink | None = None,
        *,
        stderr: TextIO | None = None,
    ) -> None:
        """Initialize the logger.

        Args:
            sink: The primary log sink. When ``None``, entries are written to
                standard output, one JSON object per line.
            stderr: The stream used for the single best-effort fallback write
                when the primary sink fails. Defaults to :data:`sys.stderr`.
        """

        self._sink: Sink = sink if sink is not None else _default_sink
        self._stderr: TextIO = stderr if stderr is not None else sys.stderr

    def decision(
        self,
        component: str,
        run_id: str,
        decision: str,
        *,
        level: str = "INFO",
        **fields: Any,
    ) -> None:
        """Emit a structured log entry for an agent decision (Req 15.1).

        Records the component name, the ``run_id``, the decision text, a UTC
        timestamp, a severity level, and any extra structured ``fields``. If
        emitting the entry fails for any reason, a single best-effort fallback
        write to standard error is attempted and the error is suppressed so the
        calling component continues unaffected (Req 15.2, property P17).

        Args:
            component: The component making the decision (e.g. ``"planner"``,
                ``"executor"``, ``"reflector"``).
            run_id: The identifier of the Run the decision belongs to.
            decision: A human-readable description of the decision.
            level: The severity level for the entry (defaults to ``"INFO"``).
            **fields: Additional structured fields to include in the entry.
                Reserved keys (``event``, ``component``, ``run_id``,
                ``decision``, ``timestamp``, ``level``) take precedence over
                any collision in ``fields``.
        """

        entry: dict[str, Any] = {
            "event": "decision",
            "component": component,
            "run_id": run_id,
            "decision": decision,
            "timestamp": self._now(),
            "level": level,
        }
        # Extra structured fields are added underneath the reserved keys so
        # that a caller-supplied field can never overwrite the core contract.
        for key, value in fields.items():
            entry.setdefault(key, value)
        self._emit(entry)

    def security_event(
        self,
        client_ip: str,
        request_hash: str,
        reason: str,
    ) -> None:
        """Emit a structured ``security_event`` for a rejected request (Req 1.5).

        Records a UTC timestamp, the client IP, a **hash** of the request, and
        the rejection reason. The verbatim request payload is deliberately
        never included so a malicious payload is not persisted to logs
        (property P18). As with :meth:`decision`, an emission failure triggers a
        single best-effort stderr fallback and is then suppressed (Req 15.2).

        Args:
            client_ip: The IP address of the client whose request was rejected.
            request_hash: A hash of the request standing in for the payload.
            reason: The reason the request was rejected (e.g. ``"malicious"``).
        """

        entry: dict[str, Any] = {
            "event": "security_event",
            "timestamp": self._now(),
            "client_ip": client_ip,
            "request_hash": request_hash,
            "reason": reason,
            "level": "WARNING",
        }
        self._emit(entry)

    def _emit(self, entry: dict[str, Any]) -> None:
        """Serialize and write one entry, containing any failure (Req 15.2).

        On any exception during serialization or the primary sink write, a
        single best-effort fallback write to standard error is attempted and
        every error is suppressed so logging never propagates to the caller.

        Args:
            entry: The structured entry to serialize and emit.
        """

        try:
            serialized = json.dumps(entry, default=str)
            self._sink(serialized)
        except Exception:  # noqa: BLE001 - availability must never be sacrificed
            self._fallback(entry)

    def _fallback(self, entry: dict[str, Any]) -> None:
        """Attempt a single best-effort stderr write; never raise (Req 15.2).

        Args:
            entry: The structured entry that failed to emit through the sink.
        """

        # the fallback itself must never raise
        with contextlib.suppress(Exception):
            self._stderr.write(f"[structured-logger-fallback] {entry!r}\n")

    @staticmethod
    def _now() -> str:
        """Return the current UTC time as an ISO-8601 string."""

        return datetime.now(UTC).isoformat()
