"""Tests for the :class:`app.agent.orchestrator.Orchestrator` (Task 15).

Covers Task 15.2 / Property 12: **the response contract is always satisfied**.
For any finished Run, the :class:`~app.models.schemas.AgentResponse` returned by
:meth:`Orchestrator.execute_run` always contains ``run_id``, ``status``, a
``plan`` (with per-step status and output summaries), ``assumptions``,
``clarifications_resolved``, and ``summary``; and ``document_url`` is non-null
**if and only if** the document artifact exists (Req 8.1).

The property drives :meth:`Orchestrator.execute_run` end to end with:

- a fake LLM backend (from ``tests/conftest.py``) whose single canned response
  is a valid :class:`~app.models.schemas.Plan` JSON. Because the other
  JSON-constrained schemas the loop parses (table data, reflection output) have
  fully-defaulted fields and ignore extra keys, the same canned response drives
  the planner, the content tools, and the reflector without any network calls;
- a fake document builder whose success is toggled by Hypothesis, so both the
  ``document_url``-null and ``document_url``-non-null cases are exercised; and
- the real :class:`~app.core.event_bus.EventBus`,
  :class:`~app.core.run_store.RunStore`, :class:`~app.agent.executor.Executor`,
  and :class:`~app.agent.reflector.Reflector`.

Property tests use Hypothesis at a minimum of 100 iterations. Each generated
example is driven through :func:`asyncio.run` because
:meth:`Orchestrator.execute_run` is asynchronous.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.agent.executor import Executor
from app.agent.orchestrator import Orchestrator
from app.agent.planner import Planner
from app.agent.reflector import Reflector
from app.agent.tools import build_default_registry
from app.core.config import Settings
from app.core.event_bus import EventBus
from app.core.run_store import RunStore
from app.models.schemas import (
    AgentResponse,
    RunStatus,
    StepStatus,
)
from app.services.llm import LLMService
from tests.conftest import FakeLLMBackend

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeDocBuilder:
    """A document builder whose success is scripted (Task 15.2).

    On success it writes a tiny real file to ``out_dir`` and returns its path so
    the produced artifact genuinely exists on disk (letting the orchestrator's
    ``artifact_exists`` check pass). On failure it raises, modeling a build that
    produces no artifact.

    Attributes:
        calls: The number of times :meth:`build` was invoked.
    """

    def __init__(self, *, succeed: bool, out_dir: Path) -> None:
        self._succeed = succeed
        self._out_dir = out_dir
        self.calls = 0

    def build(
        self,
        *,
        title: str,
        prepared_by: str,
        sections: Sequence[Mapping[str, Any]],
        output_path: Path,
    ) -> Path:
        """Write a fake artifact and return its path, or raise on failure."""

        self.calls += 1
        if not self._succeed:
            raise RuntimeError("scripted document builder failure")
        path = self._out_dir / f"artifact-{self.calls}.docx"
        path.write_bytes(b"PK\x03\x04 fake-docx")
        return path


async def _no_sleep(_delay: float) -> None:
    """A no-op async sleep so retry backoff introduces no real delay."""

    return None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_run_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=1,
    max_size=12,
)

# Content tool names the executor routes directly (task name == tool name).
_CONTENT_TOOLS = ["research", "draft_section", "generate_table_data"]


@st.composite
def _plan_specs(draw: st.DrawFn) -> dict[str, Any]:
    """Draw a valid plan specification (later serialized to canned plan JSON).

    The plan always contains at least one content step (so completed sections
    exist) and may end with a ``build_docx`` step. Steps are numbered
    sequentially from 1 with no dependencies, so every step is executed.
    """

    content_count = draw(st.integers(min_value=1, max_value=3))
    include_build = draw(st.booleans())

    steps: list[dict[str, Any]] = []
    number = 1
    for _ in range(content_count):
        task = draw(st.sampled_from(_CONTENT_TOOLS))
        steps.append(
            {
                "step": number,
                "task": task,
                "description": f"perform {task} for step {number}",
                "expected_output": f"output {number}",
            }
        )
        number += 1

    if include_build:
        steps.append(
            {
                "step": number,
                "task": "build_docx",
                "description": "assemble the final Word document",
                "expected_output": "the .docx deliverable",
            }
        )

    # A plan needs at least two steps; pad with a draft step when necessary.
    if len(steps) < 2:
        steps.append(
            {
                "step": number + 1 if include_build else number,
                "task": "draft_section",
                "description": "draft an additional section",
                "expected_output": "additional section",
            }
        )
        # Re-number to guarantee sequential 1..n ordering.
        for index, step in enumerate(steps, start=1):
            step["step"] = index

    assumptions = draw(
        st.lists(st.text(min_size=1, max_size=20), min_size=0, max_size=3)
    )
    return {"steps": steps, "assumptions": assumptions}


def _build_llm(plan_json: str) -> LLMService:
    """Build an :class:`LLMService` wired to fake backends returning ``plan_json``."""

    return LLMService(
        Settings(GROQ_API_KEY="test-key"),
        groq_backend=FakeLLMBackend("groq", response=plan_json),
        ollama_backend=FakeLLMBackend("ollama", response=plan_json),
        sleep=_no_sleep,
        resolve=True,
    )


# ---------------------------------------------------------------------------
# Task 15.2 / Property 12
# ---------------------------------------------------------------------------


# Feature: autonomous-agent-service, Property 12: The response contract is always satisfied
@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(
    run_id=_run_ids,
    plan_spec=_plan_specs(),
    doc_succeeds=st.booleans(),
)
def test_property_12_response_contract_always_satisfied(
    run_id: str,
    plan_spec: dict[str, Any],
    doc_succeeds: bool,
) -> None:
    """Property 12: the response contract is always satisfied.

    **Validates: Requirements 8.1**

    For any finished Run, the returned :class:`AgentResponse` contains
    ``run_id``, ``status``, a ``plan`` with per-step status and output summaries,
    ``assumptions``, ``clarifications_resolved``, and ``summary``; and
    ``document_url`` is non-null if and only if the document artifact exists.
    """

    plan_json = json.dumps(plan_spec)

    async def _run() -> tuple[AgentResponse, bool]:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            llm = _build_llm(plan_json)
            doc_builder = _FakeDocBuilder(succeed=doc_succeeds, out_dir=out_dir)

            bus = EventBus()
            store = RunStore()
            registry = build_default_registry(llm, doc_builder)
            planner = Planner(llm)
            # ``max_retries=0`` keeps the property fast; retry bounds are covered
            # elsewhere and are irrelevant to the response contract.
            executor = Executor(registry, bus, max_retries=0)
            reflector = Reflector(llm, bus)

            orchestrator = Orchestrator(
                validator=None,  # type: ignore[arg-type] - unused in execute_run
                planner=planner,
                executor=executor,
                reflector=reflector,
                doc_builder=doc_builder,
                store=store,
                events=bus,
            )

            run_state = store.create(
                run_id, request="Create a business deliverable.", client_ip="127.0.0.1"
            )
            response = await orchestrator.execute_run(run_state)

            # The artifact-exists truth used by the contract is the real
            # on-disk state referenced by the persisted Run.
            artifact_exists = (
                run_state.document_path is not None
                and run_state.document_path.exists()
            )
            return response, artifact_exists

    response, artifact_exists = asyncio.run(_run())

    # --- Required scalar fields are always present ---------------------------
    assert isinstance(response, AgentResponse)
    assert response.run_id == run_id
    assert isinstance(response.status, RunStatus)

    # --- Plan is present with per-step status and output summaries -----------
    assert response.plan is not None
    assert len(response.plan.steps) >= 2
    for step in response.plan.steps:
        # Every step carries a status drawn from the StepStatus enum.
        assert isinstance(step.status, StepStatus)
        # Every executed (done/failed) step carries an output summary.
        if step.status in (StepStatus.DONE, StepStatus.FAILED):
            assert step.output_summary is not None

    # --- List / summary fields are always present ----------------------------
    assert isinstance(response.assumptions, list)
    assert isinstance(response.clarifications_resolved, list)
    assert isinstance(response.summary, str)
    assert response.summary != ""

    # --- document_url is non-null iff the artifact exists (Req 8.1) ----------
    assert (response.document_url is not None) == artifact_exists
    if artifact_exists:
        assert response.document_url == f"/documents/{run_id}.docx"
