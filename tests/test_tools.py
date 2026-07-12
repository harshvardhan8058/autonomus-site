"""Smoke tests for the tool registry and tool implementations (Task 11.2).

These example-based tests exercise :mod:`app.agent.tools` without any network
calls: the LLM-backed tools run against the network-free fake backend (see
``tests/conftest.py``), and ``build_docx`` delegates to a tiny in-test fake
document builder implementing :class:`DocumentBuilderProtocol`.

They assert the registry contract required by Req 3.2:

- All four standard tools (``research``, ``draft_section``,
  ``generate_table_data``, ``build_docx``) resolve via
  :meth:`ToolRegistry.get`.
- An unknown tool name raises :class:`ToolError` from both ``get`` and
  ``dispatch``.
- Each tool dispatches successfully and returns a well-formed
  :class:`ToolResult`.

**Validates: Requirements 3.2**
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agent.tools import (
    DocumentBuilderProtocol,
    ToolError,
    ToolRegistry,
    ToolResult,
    build_default_registry,
)
from app.core.config import Settings
from app.services.llm import LLMService
from tests.conftest import FakeLLMBackend

_TOOL_NAMES = ["research", "draft_section", "generate_table_data", "build_docx"]


async def _noop_sleep(_delay: float) -> None:
    """A no-op async sleep so retry backoff introduces no real delay."""

    return None


class _FakeDocumentBuilder:
    """A minimal in-test document builder implementing the protocol.

    It does not write a real ``.docx``; it records the call arguments and returns
    the requested output path so ``build_docx`` can surface the artifact path.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build(
        self,
        *,
        title: str,
        prepared_by: str,
        sections: Any,
        output_path: Path,
    ) -> Path:
        self.calls.append(
            {
                "title": title,
                "prepared_by": prepared_by,
                "sections": list(sections),
                "output_path": output_path,
            }
        )
        return output_path


def _make_llm(*, table_json: str) -> LLMService:
    """Build an LLMService whose fake backends return canned content.

    The free-form ``complete`` calls (research / draft_section) receive a short
    prose string; the ``complete_json`` call (generate_table_data) receives a
    valid ``_TableData`` JSON payload. Because a single backend serves both call
    types here, the fake returns ``table_json`` for JSON calls only when it is
    the requested response — so we script a dedicated backend per test instead.
    """

    settings = Settings(GROQ_API_KEY="test-key")
    return LLMService(
        settings,
        groq_backend=FakeLLMBackend("groq", response=table_json),
        ollama_backend=FakeLLMBackend("ollama", response=table_json),
        sleep=_noop_sleep,
    )


def _registry_for_prose(response: str) -> tuple[ToolRegistry, _FakeDocumentBuilder]:
    """Build a registry whose LLM returns ``response`` for every completion."""

    settings = Settings(GROQ_API_KEY="test-key")
    llm = LLMService(
        settings,
        groq_backend=FakeLLMBackend("groq", response=response),
        ollama_backend=FakeLLMBackend("ollama", response=response),
        sleep=_noop_sleep,
    )
    builder = _FakeDocumentBuilder()
    return build_default_registry(llm, builder), builder


def test_all_four_tools_resolve_via_registry() -> None:
    """`research`, `draft_section`, `generate_table_data`, `build_docx` resolve (Req 3.2)."""

    registry, _ = _registry_for_prose("ok")

    for name in _TOOL_NAMES:
        fn = registry.get(name)
        assert callable(fn)

    assert registry.names() == sorted(_TOOL_NAMES)


def test_fake_document_builder_satisfies_protocol() -> None:
    """The in-test builder structurally satisfies DocumentBuilderProtocol."""

    assert isinstance(_FakeDocumentBuilder(), DocumentBuilderProtocol)


def test_unknown_tool_raises_tool_error_on_get() -> None:
    """Resolving an unknown tool name raises ToolError (Req 3.2)."""

    registry, _ = _registry_for_prose("ok")

    with pytest.raises(ToolError):
        registry.get("does_not_exist")


async def test_unknown_tool_raises_tool_error_on_dispatch() -> None:
    """Dispatching to an unknown tool name raises ToolError (Req 3.2)."""

    registry, _ = _registry_for_prose("ok")

    with pytest.raises(ToolError):
        await registry.dispatch("does_not_exist", foo="bar")


async def test_research_and_draft_section_dispatch() -> None:
    """The prose tools dispatch and return non-empty ToolResults."""

    registry, _ = _registry_for_prose("Some researched prose content.")

    research_result = await registry.dispatch("research", topic="cloud migration")
    assert isinstance(research_result, ToolResult)
    assert research_result.output
    assert research_result.summary

    draft_result = await registry.dispatch(
        "draft_section", title="Overview", context="facts"
    )
    assert isinstance(draft_result, ToolResult)
    assert draft_result.output
    assert draft_result.data == {"title": "Overview"}


async def test_generate_table_data_returns_structured_rows() -> None:
    """generate_table_data returns headers and rows in ToolResult.data (Req 3.2)."""

    table_json = json.dumps(
        {
            "headers": ["Phase", "Duration"],
            "rows": [["Discovery", "2 weeks"], ["Migration", "6 weeks"]],
        }
    )
    llm = _make_llm(table_json=table_json)
    registry = build_default_registry(llm, _FakeDocumentBuilder())

    result = await registry.dispatch(
        "generate_table_data", spec="migration timeline"
    )

    assert isinstance(result, ToolResult)
    assert result.data == {
        "headers": ["Phase", "Duration"],
        "rows": [["Discovery", "2 weeks"], ["Migration", "6 weeks"]],
    }


async def test_build_docx_records_document_path(tmp_path: Path) -> None:
    """build_docx delegates to the builder and records the artifact path (Req 3.2)."""

    registry, builder = _registry_for_prose("ok")
    output_path = tmp_path / "agent-run-test.docx"

    result = await registry.dispatch(
        "build_docx",
        sections=[{"title": "Intro", "body": "text"}],
        output_path=output_path,
        title="CRM Migration Proposal",
        prepared_by="Autonomous Agent Service",
    )

    assert isinstance(result, ToolResult)
    assert result.data == {"document_path": str(output_path)}
    assert len(builder.calls) == 1
    assert builder.calls[0]["title"] == "CRM Migration Proposal"
    assert builder.calls[0]["output_path"] == output_path
