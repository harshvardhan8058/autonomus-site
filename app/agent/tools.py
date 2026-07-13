"""Tool registry and tool implementations for the Executor (Req 3.2).

This module defines the tool-calling layer the :class:`~app.agent.executor.Executor`
dispatches through. A :class:`ToolRegistry` maps a tool name to an async callable
(:data:`ToolFn`); each tool returns a :class:`ToolResult` carrying textual output,
an optional structured payload, and a short summary.

Four tools are provided (Req 3.2):

- ``research(topic)`` — gathers researched facts on a topic via
  :meth:`~app.services.llm.LLMService.complete`.
- ``draft_section(title, context)`` — drafts a document section via
  :meth:`~app.services.llm.LLMService.complete`.
- ``generate_table_data(spec)`` — produces structured tabular data via
  :meth:`~app.services.llm.LLMService.complete_json`, returning headers and rows
  in :attr:`ToolResult.data`.
- ``build_docx(sections, ...)`` — assembles the final ``.docx`` by delegating to
  an injected document builder and records the produced artifact path in
  :attr:`ToolResult.data`.

The document builder is accessed through the small :class:`DocumentBuilderProtocol`
seam so this module never imports the concrete ``DocumentBuilder`` (implemented in
Task 12). The :func:`build_default_registry` helper wires all four tools against a
shared :class:`~app.services.llm.LLMService` and a document builder, returning a
ready-to-use :class:`ToolRegistry`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.services.llm import LLMService
from app.services.offline_content import (
    offline_research,
    offline_section,
    offline_table,
)

# The default document title and prepared-by line used when a caller does not
# supply overrides to the ``build_docx`` tool.
_DEFAULT_TITLE = "Business Deliverable"
_DEFAULT_PREPARED_BY = "Autonomous Agent Service"

# The maximum number of characters of tool output surfaced verbatim in a summary.
_SUMMARY_MAX_CHARS = 160


# ---------------------------------------------------------------------------
# Result / error types
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """The result produced by a tool invocation.

    Attributes:
        output: The primary textual result (for example section prose or a
            research digest). Empty for tools whose value is purely structured.
        data: An optional structured payload, such as table rows for
            ``generate_table_data`` or ``{"document_path": ...}`` for
            ``build_docx``.
        summary: A short, human-readable summary suitable for a
            ``step_completed`` SSE event's ``output_summary`` field.
    """

    output: str
    data: dict[str, Any] | None = None
    summary: str = ""


class ToolError(Exception):
    """Raised when a tool cannot be resolved or a dispatch fails.

    :meth:`ToolRegistry.get` and :meth:`ToolRegistry.dispatch` raise this when a
    requested tool name is not registered.
    """


# A tool is an async callable accepting arbitrary keyword arguments and
# returning a :class:`ToolResult`.
ToolFn = Callable[..., Awaitable[ToolResult]]


# ---------------------------------------------------------------------------
# Document builder seam
# ---------------------------------------------------------------------------


@runtime_checkable
class DocumentBuilderProtocol(Protocol):
    """Structural type for the document builder used by ``build_docx``.

    Declaring the dependency as a :class:`typing.Protocol` lets this module call
    the builder without importing the concrete ``DocumentBuilder`` class
    (implemented separately in Task 12), keeping the tools layer decoupled and
    independently testable.
    """

    def build(
        self,
        *,
        title: str,
        prepared_by: str,
        sections: Sequence[Mapping[str, Any]],
        output_path: Path,
    ) -> Path:
        """Assemble a ``.docx`` deliverable and return the written path.

        Args:
            title: The document title rendered on the cover page.
            prepared_by: The cover-page "prepared by" line.
            sections: The ordered section payloads to render.
            output_path: The filesystem path the document is written to.

        Returns:
            The path of the written ``.docx`` artifact.
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class ToolRegistry:
    """A name -> async tool mapping enabling tool-calling dispatch (Req 3.2).

    The Executor registers tools by name and later dispatches plan steps to them
    by name. Unknown-tool lookups raise :class:`ToolError` so a mis-routed step
    fails cleanly rather than silently.

    Attributes:
        _tools: The backing mapping of tool name to :data:`ToolFn`.
    """

    _tools: dict[str, ToolFn] = field(default_factory=dict)

    def register(self, name: str, fn: ToolFn) -> None:
        """Register ``fn`` under ``name``, overwriting any existing entry.

        Args:
            name: The tool name used for dispatch.
            fn: The async tool callable returning a :class:`ToolResult`.
        """

        self._tools[name] = fn

    def get(self, name: str) -> ToolFn:
        """Return the tool registered under ``name``.

        Args:
            name: The tool name to look up.

        Returns:
            The registered :data:`ToolFn`.

        Raises:
            ToolError: If no tool is registered under ``name``.
        """

        try:
            return self._tools[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._tools)) or "<none>"
            raise ToolError(
                f"unknown tool {name!r}; registered tools: {known}"
            ) from exc

    def names(self) -> list[str]:
        """Return the sorted list of registered tool names."""

        return sorted(self._tools)

    async def dispatch(self, name: str, **kwargs: Any) -> ToolResult:
        """Look up and invoke the tool ``name`` with keyword arguments.

        Args:
            name: The tool name to dispatch to.
            **kwargs: The keyword arguments forwarded to the tool.

        Returns:
            The :class:`ToolResult` produced by the tool.

        Raises:
            ToolError: If no tool is registered under ``name``.
        """

        fn = self.get(name)
        return await fn(**kwargs)


# ---------------------------------------------------------------------------
# Internal schema for structured table generation
# ---------------------------------------------------------------------------


class _TableData(BaseModel):
    """Schema for the structured output of the ``generate_table_data`` tool.

    Attributes:
        headers: The column headers of the table.
        rows: The table rows; each row is a list of cell strings aligned to
            ``headers``.
    """

    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize(text: str, *, fallback: str) -> str:
    """Build a short one-line summary from ``text``.

    Collapses whitespace and truncates to :data:`_SUMMARY_MAX_CHARS` characters.
    When ``text`` is empty, ``fallback`` is returned instead.

    Args:
        text: The text to summarize.
        fallback: The summary to use when ``text`` is empty.

    Returns:
        A single-line summary string.
    """

    collapsed = " ".join(text.split())
    if not collapsed:
        return fallback
    if len(collapsed) <= _SUMMARY_MAX_CHARS:
        return collapsed
    return collapsed[: _SUMMARY_MAX_CHARS - 1].rstrip() + "\u2026"


# ---------------------------------------------------------------------------
# Tool factories
# ---------------------------------------------------------------------------


def make_research_tool(
    llm: LLMService, *, enable_offline_fallback: bool = False
) -> ToolFn:
    """Create the ``research`` tool bound to ``llm`` (Req 3.2).

    Args:
        llm: The shared LLM service used to gather researched facts.
        enable_offline_fallback: When ``True``, a deterministic offline briefing
            is produced if every LLM backend is unreachable, so research never
            fails outright.

    Returns:
        An async :data:`ToolFn` that researches a topic and returns a
        :class:`ToolResult` with the researched facts and a short summary.
    """

    system = (
        "You are a diligent research assistant supporting the creation of a "
        "polished business document. Given a topic, produce a concise, factual "
        "briefing: key facts, relevant considerations, and supporting points. "
        "Write in clear prose or bullet points. Do not fabricate precise "
        "statistics; qualify uncertain claims."
    )

    async def research(topic: str) -> ToolResult:
        """Gather researched facts on ``topic``.

        Args:
            topic: The subject to research.

        Returns:
            A :class:`ToolResult` whose ``output`` holds the researched facts and
            whose ``summary`` is a short digest.
        """

        prompt = f"Research the following topic for a business document:\n\n{topic}"
        fallback = (
            (lambda: offline_research(topic)) if enable_offline_fallback else None
        )
        output = await llm.complete(prompt, system=system, offline_fallback=fallback)
        return ToolResult(
            output=output,
            summary=_summarize(output, fallback=f"Researched: {topic}"),
        )

    return research


def make_draft_section_tool(
    llm: LLMService, *, enable_offline_fallback: bool = False
) -> ToolFn:
    """Create the ``draft_section`` tool bound to ``llm`` (Req 3.2).

    Args:
        llm: The shared LLM service used to draft section prose.
        enable_offline_fallback: When ``True``, deterministic offline prose is
            produced if every LLM backend is unreachable, so drafting never fails
            outright.

    Returns:
        An async :data:`ToolFn` that drafts a titled section from context and
        returns a :class:`ToolResult` carrying the section prose.
    """

    system = (
        "You are a professional business writer. Draft a single, well-structured "
        "document section given its title and supporting context. Produce polished "
        "prose suitable for an executive audience. Return only the section body "
        "text, without repeating the title as a heading."
    )

    async def draft_section(title: str, context: str) -> ToolResult:
        """Draft a document section titled ``title`` from ``context``.

        Args:
            title: The section title.
            context: Supporting context and facts to draw from.

        Returns:
            A :class:`ToolResult` whose ``output`` is the drafted section prose,
            with the section title recorded in ``data`` for downstream assembly.
        """

        prompt = (
            f"Section title: {title}\n\n"
            f"Context to draw from:\n{context}\n\n"
            "Write the section body."
        )
        fallback = (
            (lambda: offline_section(title, context))
            if enable_offline_fallback
            else None
        )
        output = await llm.complete(prompt, system=system, offline_fallback=fallback)
        return ToolResult(
            output=output,
            data={"title": title},
            summary=_summarize(output, fallback=f"Drafted section: {title}"),
        )

    return draft_section


def make_generate_table_data_tool(
    llm: LLMService, *, enable_offline_fallback: bool = False
) -> ToolFn:
    """Create the ``generate_table_data`` tool bound to ``llm`` (Req 3.2).

    Args:
        llm: The shared LLM service used to produce structured tabular data.
        enable_offline_fallback: When ``True``, a deterministic offline table is
            produced if every LLM backend is unreachable, so table generation
            never fails outright.

    Returns:
        An async :data:`ToolFn` that produces table headers and rows and returns
        them in :attr:`ToolResult.data` as ``{"headers": [...], "rows": [[...]]}``.
    """

    system = (
        "You generate structured tabular data for a business document. Given a "
        "specification, respond with a JSON object containing 'headers' (a list "
        "of column-header strings) and 'rows' (a list of rows, where each row is "
        "a list of cell strings aligned to the headers). Keep tables focused and "
        "presentable."
    )

    async def generate_table_data(spec: str) -> ToolResult:
        """Produce structured tabular data satisfying ``spec``.

        Args:
            spec: A description of the table to generate.

        Returns:
            A :class:`ToolResult` whose ``data`` holds ``headers`` and ``rows``.
        """

        prompt = f"Generate table data for the following specification:\n\n{spec}"

        def _offline_table() -> _TableData:
            headers, rows = offline_table(spec)
            return _TableData(headers=headers, rows=rows)

        fallback = _offline_table if enable_offline_fallback else None
        table = await llm.complete_json(
            prompt, _TableData, system=system, offline_fallback=fallback
        )
        data: dict[str, Any] = {"headers": table.headers, "rows": table.rows}
        summary = (
            f"Generated a {len(table.rows)}x{len(table.headers)} table"
            if table.headers
            else "Generated table data"
        )
        return ToolResult(output="", data=data, summary=summary)

    return generate_table_data


def make_build_docx_tool(doc_builder: DocumentBuilderProtocol) -> ToolFn:
    """Create the ``build_docx`` tool bound to ``doc_builder`` (Req 3.2).

    The tool delegates document assembly to the injected builder and records the
    produced artifact path in the returned :attr:`ToolResult.data` under
    ``document_path`` so the Executor can attach it to the Run state.

    Args:
        doc_builder: The document builder implementing
            :class:`DocumentBuilderProtocol`.

    Returns:
        An async :data:`ToolFn` that builds a ``.docx`` and returns its path.
    """

    async def build_docx(
        sections: Sequence[Mapping[str, Any]],
        *,
        output_path: str | Path,
        title: str = _DEFAULT_TITLE,
        prepared_by: str = _DEFAULT_PREPARED_BY,
    ) -> ToolResult:
        """Assemble the final ``.docx`` deliverable from ``sections``.

        Args:
            sections: The ordered section payloads to render into the document.
            output_path: The filesystem path the document is written to.
            title: The document title rendered on the cover page.
            prepared_by: The cover-page "prepared by" line.

        Returns:
            A :class:`ToolResult` whose ``data`` records the produced artifact
            path as ``{"document_path": str(path)}``.
        """

        path = doc_builder.build(
            title=title,
            prepared_by=prepared_by,
            sections=list(sections),
            output_path=Path(output_path),
        )
        return ToolResult(
            output=str(path),
            data={"document_path": str(path)},
            summary=f"Built document: {Path(path).name}",
        )

    return build_docx


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


def build_default_registry(
    llm: LLMService,
    doc_builder: DocumentBuilderProtocol,
    *,
    enable_offline_fallback: bool = False,
) -> ToolRegistry:
    """Build a :class:`ToolRegistry` with all four standard tools registered.

    Registers ``research``, ``draft_section``, and ``generate_table_data`` (each
    bound to ``llm``) and ``build_docx`` (bound to ``doc_builder``) (Req 3.2).

    Args:
        llm: The shared LLM service the content tools call.
        doc_builder: The document builder the ``build_docx`` tool delegates to.
        enable_offline_fallback: When ``True``, the three content tools generate
            deterministic offline content if every LLM backend is unreachable, so
            a deliverable is always produced (graceful degradation).

    Returns:
        A ready-to-use :class:`ToolRegistry`.
    """

    registry = ToolRegistry()
    registry.register(
        "research",
        make_research_tool(llm, enable_offline_fallback=enable_offline_fallback),
    )
    registry.register(
        "draft_section",
        make_draft_section_tool(llm, enable_offline_fallback=enable_offline_fallback),
    )
    registry.register(
        "generate_table_data",
        make_generate_table_data_tool(
            llm, enable_offline_fallback=enable_offline_fallback
        ),
    )
    registry.register("build_docx", make_build_docx_tool(doc_builder))
    return registry
