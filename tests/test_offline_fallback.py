"""Integration tests for the deterministic offline fallback path.

These tests assemble the REAL agent stack (real Planner, Executor, ToolRegistry,
Reflector, DocumentBuilder, RunStore, EventBus, Orchestrator) with only the LLM
backend transport faked, and drive it through the API app. Both LLM backends are
scripted to ALWAYS fail, modeling the situation where every backend (Groq and
Ollama) is unreachable.

Two behaviors are covered:

- **Offline fallback ENABLED** — planning and the content tools degrade to
  deterministic templated content, so the run still completes with a real,
  downloadable ``.docx`` on disk (graceful degradation).
- **Offline fallback DISABLED** — planning fails on all backends, the
  orchestrator marks the run failed, and the API maps the resulting
  :class:`PlanningError` to an HTTP 503 ``PlanningFailureBody``.
"""

from __future__ import annotations

import io

from docx import Document
from starlette.testclient import TestClient

from app.agent.executor import Executor
from app.agent.guardrail import GuardrailValidator
from app.agent.orchestrator import Orchestrator
from app.agent.planner import Planner
from app.agent.reflector import Reflector
from app.agent.tools import build_default_registry
from app.core.config import Settings
from app.core.event_bus import EventBus
from app.core.logging import StructuredLogger
from app.core.run_store import RunStore
from app.models.schemas import RunStatus
from app.services.docx_builder import DocumentBuilder
from app.services.llm import LLMService
from tests.conftest import FakeLLMBackend
from tests.support import FakeGuardrail, build_app


async def _no_sleep(_delay: float) -> None:
    """A no-op async sleep so retry backoff never introduces real delay."""

    return None


def _build_failing_stack(
    *,
    run_store: RunStore,
    event_bus: EventBus,
    logger: StructuredLogger,
    enable_offline_fallback: bool,
):
    """Assemble the real Orchestrator with ALWAYS-failing LLM backends.

    Only the LLM backend transport is faked; every other component is real. Both
    backends always raise, so the run exercises the total-backend-failure path.

    Returns:
        A ``(orchestrator, guardrail, llm_service)`` tuple.
    """

    settings = Settings(GROQ_API_KEY="test-key")
    llm_service = LLMService(
        settings,
        groq_backend=FakeLLMBackend("groq", always_fail=True),
        ollama_backend=FakeLLMBackend("ollama", always_fail=True),
        sleep=_no_sleep,
    )

    doc_builder = DocumentBuilder(settings.THEME_COLOR, logger)
    registry = build_default_registry(
        llm_service, doc_builder, enable_offline_fallback=enable_offline_fallback
    )

    executor = Executor(registry, event_bus, logger, max_retries=0)
    reflector = Reflector(llm_service, event_bus, logger)
    planner = Planner(
        llm_service, logger, enable_offline_fallback=enable_offline_fallback
    )
    guardrail = GuardrailValidator(llm_service)
    orchestrator = Orchestrator(
        guardrail,
        planner,
        executor,
        reflector,
        doc_builder,
        run_store,
        event_bus,
        logger,
    )
    return orchestrator, guardrail, llm_service


def test_offline_fallback_enabled_produces_docx_when_all_backends_fail(
    tmp_path, monkeypatch
) -> None:
    """With offline fallback enabled, a fully-failing LLM still yields a .docx.

    Every LLM backend fails, but the enabled deterministic fallback lets the run
    complete (``completed`` or ``partial``) with a non-null ``document_url`` and
    a real Word document written to disk.
    """

    monkeypatch.chdir(tmp_path)

    run_store = RunStore()
    event_bus = EventBus()
    logger = StructuredLogger()
    orchestrator, _guardrail, llm_service = _build_failing_stack(
        run_store=run_store,
        event_bus=event_bus,
        logger=logger,
        enable_offline_fallback=True,
    )

    # Use a fake guardrail so intent classification (which also uses the failing
    # LLM) does not reject the request before the run starts.
    app = build_app(
        run_store=run_store,
        event_bus=event_bus,
        guardrail=FakeGuardrail(),
        orchestrator=orchestrator,
        llm_service=llm_service,
        logger=logger,
    )
    client = TestClient(app)

    response = client.post(
        "/agent",
        json={
            "request": (
                "Create a project proposal for migrating our on-premise CRM to "
                "the cloud."
            )
        },
    )
    assert response.status_code == 200
    body = response.json()

    run_id = body["run_id"]
    assert body["status"] in {RunStatus.COMPLETED.value, RunStatus.PARTIAL.value}
    assert body["document_url"] == f"/documents/{run_id}.docx"
    # The offline plan surfaced its degraded-mode assumption.
    assert any("no live llm backend" in a.lower() for a in body["assumptions"])

    # The document is served and reopens as a valid Word document.
    doc_response = client.get(body["document_url"])
    assert doc_response.status_code == 200
    document = Document(io.BytesIO(doc_response.content))
    assert document.paragraphs  # non-empty document

    # The artifact genuinely exists on disk.
    persisted = run_store.get(run_id)
    assert persisted is not None
    assert persisted.document_path is not None and persisted.document_path.exists()


def test_offline_fallback_disabled_returns_503_when_all_backends_fail(
    tmp_path, monkeypatch
) -> None:
    """With offline fallback disabled, a fully-failing LLM yields a 503 body."""

    monkeypatch.chdir(tmp_path)

    run_store = RunStore()
    event_bus = EventBus()
    logger = StructuredLogger()
    orchestrator, _guardrail, llm_service = _build_failing_stack(
        run_store=run_store,
        event_bus=event_bus,
        logger=logger,
        enable_offline_fallback=False,
    )

    app = build_app(
        run_store=run_store,
        event_bus=event_bus,
        guardrail=FakeGuardrail(),
        orchestrator=orchestrator,
        llm_service=llm_service,
        logger=logger,
    )
    client = TestClient(app)

    response = client.post(
        "/agent", json={"request": "Create a proposal for our CRM migration."}
    )
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "planning_failed"
    assert body["retry_history"]
