"""Property-based tests for :class:`app.services.docx_builder.DocumentBuilder`.

These tests validate two design correctness properties for the document builder:

- **Property 13** (Req 10.1): every generated ``.docx`` re-opens successfully
  with ``python-docx`` and contains the document title, a table-of-contents
  field element, at least one table, at least one bullet-list paragraph, styled
  headings, and a footer page-number (``PAGE``) field.
- **Property 15** (Req 14.3, 14.4, 14.5): theme-color resolution is safe and
  total -- for any ``THEME_COLOR`` string the resolved heading color equals the
  value when it is a valid 6-digit hex, otherwise the documented default;
  building always completes without raising, and an invalid value produces a
  structured warning.

All property tests use Hypothesis with a minimum of 100 iterations. Document
writing can be slower than pure-logic checks, so a ``deadline=None`` is used to
avoid spurious per-example timeouts.
"""

from __future__ import annotations

import json

import pytest
from docx import Document
from docx.shared import RGBColor
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.logging import StructuredLogger
from app.services.docx_builder import DEFAULT_THEME_COLOR, DocumentBuilder

# --- Strategies -------------------------------------------------------------

# Text safe for XML serialization: excludes control characters (Cc) and
# surrogates (Cs) that lxml rejects, while still exercising a wide input space.
_safe_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Cs")),
    max_size=40,
)

# Non-empty safe text for the document title so it can be located after reopen.
_title_text = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "")


@st.composite
def _sections(draw: st.DrawFn) -> list[dict]:
    """Generate an arbitrary list of section payloads.

    Sections may omit any key and may or may not carry bullet lists and tables,
    exercising the builder's tolerance of missing keys and its synthesis of
    required structural elements when inputs do not provide them.
    """

    def _table() -> st.SearchStrategy:
        return st.builds(
            lambda headers, rows: {"headers": headers, "rows": rows},
            headers=st.lists(_safe_text, min_size=1, max_size=4),
            rows=st.lists(
                st.lists(_safe_text, min_size=1, max_size=4),
                max_size=4,
            ),
        )

    section_strategy = st.fixed_dictionaries(
        {},
        optional={
            "heading": _safe_text,
            "level": st.sampled_from([1, 2, 3, 0]),
            "body": _safe_text,
            "bullets": st.lists(_safe_text, max_size=4),
            "table": _table(),
        },
    )
    return draw(st.lists(section_strategy, max_size=4))


def _valid_hex() -> st.SearchStrategy[str]:
    """Generate valid 6-digit hex colors, some with a leading ``#``."""

    base = st.text(alphabet="0123456789abcdefABCDEF", min_size=6, max_size=6)
    return st.one_of(base, base.map(lambda s: "#" + s))


def _expected_resolved(value: str) -> str:
    """Compute the expected resolved color independently of the builder.

    Mirrors the documented resolution rule: strip whitespace and one optional
    leading ``#``; if the remainder is a 6-digit hex string, its uppercase form
    is the resolved color; otherwise the documented default applies.
    """

    candidate = value.strip()
    if candidate.startswith("#"):
        candidate = candidate[1:]
    if len(candidate) == 6 and all(c in "0123456789abcdefABCDEF" for c in candidate):
        return candidate.upper()
    return DEFAULT_THEME_COLOR


# --- Property 13 ------------------------------------------------------------


# Feature: autonomous-agent-service, Property 13: Generated documents always parse and contain required structure  # noqa: E501
@pytest.mark.property
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(title=_title_text, sections=_sections())
def test_generated_documents_parse_and_contain_required_structure(
    tmp_path_factory,
    title: str,
    sections: list[dict],
) -> None:
    """Property 13: generated documents parse and contain required structure.

    **Validates: Requirements 10.1**

    For arbitrary section inputs, the ``.docx`` produced by
    :class:`DocumentBuilder` re-opens successfully with ``python-docx`` and
    contains the document title, a TOC field element, at least one table, at
    least one bullet-list paragraph, styled headings, and a footer ``PAGE`` field.
    """

    output_path = tmp_path_factory.mktemp("docx") / "deliverable.docx"

    builder = DocumentBuilder("1F4E79")
    written = builder.build(
        title=title,
        prepared_by="Autonomous Agent Service",
        sections=sections,
        output_path=output_path,
    )

    assert written == output_path
    assert written.exists()

    # Re-open the produced document; this must not raise.
    document = Document(str(written))

    # Title text is present among the body paragraphs.
    paragraph_texts = [p.text for p in document.paragraphs]
    assert title in paragraph_texts

    # A table-of-contents field element is present.
    document_xml = document.element.xml
    assert "TOC" in document_xml

    # At least one table exists.
    assert len(document.tables) >= 1

    # At least one bullet-list paragraph exists.
    bullet_paragraphs = [
        p for p in document.paragraphs if p.style is not None and p.style.name == "List Bullet"
    ]
    assert len(bullet_paragraphs) >= 1

    # At least one styled heading exists.
    heading_paragraphs = [
        p
        for p in document.paragraphs
        if p.style is not None and p.style.name.startswith("Heading")
    ]
    assert len(heading_paragraphs) >= 1

    # The footer contains a page-number (PAGE) field.
    footer_xml = document.sections[0].footer._element.xml
    assert "PAGE" in footer_xml


# --- Property 15 ------------------------------------------------------------


# Feature: autonomous-agent-service, Property 15: Theme color resolution is safe and total
@pytest.mark.property
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(color=st.one_of(_valid_hex(), st.text(max_size=12)))
def test_theme_color_resolution_is_safe_and_total(
    tmp_path_factory,
    color: str,
) -> None:
    """Property 15: theme-color resolution is safe and total.

    **Validates: Requirements 14.3, 14.4, 14.5**

    For any ``THEME_COLOR`` string, the resolved heading color equals the value
    when it is a valid 6-digit hex, otherwise the documented default; building
    always completes without raising, and an invalid value produces a structured
    warning.
    """

    expected = _expected_resolved(color)
    # Determine independently whether the input normalized to a valid hex color.
    normalized = color.strip()
    if normalized.startswith("#"):
        normalized = normalized[1:]
    input_was_valid = len(normalized) == 6 and all(
        c in "0123456789abcdefABCDEF" for c in normalized
    )

    # Capture structured log entries via an injected sink.
    entries: list[str] = []
    logger = StructuredLogger(sink=entries.append)

    builder = DocumentBuilder(color, logger)

    # Resolution equals the value when valid, else the documented default.
    assert builder.theme_color == expected
    # The resolved color is always a usable 6-digit hex (never raises here).
    RGBColor.from_string(builder.theme_color)

    # Building always completes without raising, regardless of the color input.
    output_path = tmp_path_factory.mktemp("theme") / "themed.docx"
    written = builder.build(
        title="Theme Test",
        prepared_by="Tester",
        sections=[{"heading": "Section", "body": "Body"}],
        output_path=output_path,
    )
    assert written.exists()
    Document(str(written))  # re-opens without raising

    # An invalid value produces a structured warning; a valid value does not warn.
    warnings = [
        json.loads(entry)
        for entry in entries
        if json.loads(entry).get("level") == "WARNING"
    ]
    if input_was_valid:
        assert warnings == []
    else:
        assert len(warnings) >= 1
        assert warnings[0]["component"] == "document_builder"
