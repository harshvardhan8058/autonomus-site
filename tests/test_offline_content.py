"""Tests for the deterministic offline content generators (`app.services.offline_content`).

These tests cover the network-free, deterministic fallback content used when
every LLM backend is unreachable:

- :func:`default_plan` returns a valid :class:`~app.models.schemas.Plan` (at
  least two sequential steps) for arbitrary and empty requests, and records the
  honest offline-origin assumption.
- :func:`offline_table` returns rows that are rectangular and aligned to the
  headers.
- :func:`offline_section` / :func:`offline_research` return non-empty strings.

The property tests use Hypothesis at a minimum of 100 iterations.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.schemas import Plan
from app.services.offline_content import (
    default_plan,
    extract_topic,
    offline_research,
    offline_section,
    offline_table,
)

# The real tool names the offline plan's steps must dispatch on.
_TOOL_NAMES = {"research", "draft_section", "generate_table_data", "build_docx"}


# ---------------------------------------------------------------------------
# default_plan
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(request=st.text(max_size=200))
def test_default_plan_is_valid_for_arbitrary_requests(request: str) -> None:
    """``default_plan`` yields a valid Plan (>=2 sequential steps) for any input."""

    plan = default_plan(request)

    assert isinstance(plan, Plan)
    assert len(plan.steps) >= 2
    # Steps are numbered exactly 1..n in order (enforced by the Plan validator).
    assert [s.step for s in plan.steps] == list(range(1, len(plan.steps) + 1))
    # Every step uses a real tool name the executor can dispatch on.
    assert all(s.task in _TOOL_NAMES for s in plan.steps)
    # The honest offline-origin note is always recorded first.
    assert plan.assumptions
    assert "no live llm backend was reachable" in plan.assumptions[0].lower()


def test_default_plan_valid_for_empty_request() -> None:
    """``default_plan`` produces a valid plan even for an empty request."""

    plan = default_plan("")
    assert isinstance(plan, Plan)
    assert len(plan.steps) >= 2
    # A coherent plan includes at least one table step and a final assembly step.
    tasks = [s.task for s in plan.steps]
    assert "generate_table_data" in tasks
    assert tasks[-1] == "build_docx"


def test_default_plan_weaves_topic_from_request() -> None:
    """The derived topic is woven into the plan so it reads tailored."""

    plan = default_plan("Create a cloud migration proposal for our CRM system")
    joined = " ".join(
        f"{s.description} {s.section_title}" for s in plan.steps
    ).lower()
    assert "cloud migration proposal" in joined


# ---------------------------------------------------------------------------
# extract_topic
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(request=st.text(max_size=200))
def test_extract_topic_always_non_empty(request: str) -> None:
    """``extract_topic`` always returns a non-empty single-line string."""

    topic = extract_topic(request)
    assert isinstance(topic, str)
    assert topic.strip()
    assert "\n" not in topic


# ---------------------------------------------------------------------------
# offline_table
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(spec=st.text(max_size=120))
def test_offline_table_rows_are_rectangular(spec: str) -> None:
    """``offline_table`` returns rows aligned to the headers (rectangular)."""

    headers, rows = offline_table(spec)

    assert isinstance(headers, list)
    assert headers  # non-empty header row
    assert rows  # at least one data row
    for row in rows:
        assert isinstance(row, list)
        assert len(row) == len(headers)


# ---------------------------------------------------------------------------
# offline_section / offline_research
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    title=st.text(max_size=60),
    context=st.text(max_size=120),
)
def test_offline_section_returns_non_empty_prose(title: str, context: str) -> None:
    """``offline_section`` returns a non-empty string for any title/context."""

    body = offline_section(title, context)
    assert isinstance(body, str)
    assert body.strip()


def test_offline_section_distinct_output_per_heading() -> None:
    """Different headings yield distinct, heading-relevant framing (not boilerplate)."""

    overview = offline_section("Overview", "Introduce the CRM migration.")
    recommendations = offline_section(
        "Recommendations", "Recommend a migration approach for the CRM."
    )
    considerations = offline_section(
        "Key Considerations", "Outline risks and trade-offs of the migration."
    )

    # All three outputs must differ from one another.
    assert overview != recommendations
    assert overview != considerations
    assert recommendations != considerations

    # Each references its own heading and its intent-specific framing.
    assert "Overview" in overview
    assert "orients the reader" in overview
    assert "Recommendations" in recommendations
    assert "actionable recommendations" in recommendations
    assert "Key Considerations" in considerations
    assert "trade-offs and risks" in considerations


def test_offline_section_non_empty_for_empty_inputs() -> None:
    """``offline_section`` stays non-empty even for empty title and context."""

    assert offline_section("", "").strip()


def test_offline_section_weaves_context() -> None:
    """The supplied context text is woven into the section prose."""

    body = offline_section("Overview", "focus on data residency requirements")
    assert "data residency requirements" in body


@settings(max_examples=100, deadline=None)
@given(topic=st.text(max_size=80))
def test_offline_research_returns_non_empty_briefing(topic: str) -> None:
    """``offline_research`` returns a non-empty briefing for any topic."""

    briefing = offline_research(topic)
    assert isinstance(briefing, str)
    assert briefing.strip()


@pytest.mark.parametrize("topic", ["cloud migration", "", "   "])
def test_offline_research_examples(topic: str) -> None:
    """Spot-check that the briefing is substantive for representative topics."""

    briefing = offline_research(topic)
    assert len(briefing) > 40
