"""Reusable Word (``.docx``) document builder (Req 10, 14.3-14.5).

This module defines :class:`DocumentBuilder`, the reusable component that turns a
set of section payloads into a polished Microsoft Word deliverable via
``python-docx``. It satisfies the ``DocumentBuilderProtocol`` seam declared in
:mod:`app.agent.tools`, exposing a single :meth:`DocumentBuilder.build` method.

Every generated document contains (Req 10.1):

- a **cover page** with the document title (styled in the theme color), the
  generation date (formatted ``Month DD, YYYY``), and a "Prepared by" line,
  followed by a page break;
- a **table of contents** inserted as a Word ``TOC`` field, with the document
  settings ``w:updateFields`` flag set so Word refreshes the field on open;
- **styled headings** (``Heading 1``/``Heading 2``) whose run font color is the
  resolved theme color;
- **body text** paragraphs;
- at least one **formatted table** (header row plus one or more data rows, using
  the visible ``Table Grid`` style);
- at least one **bullet list** (``List Bullet`` style); and
- a **footer** containing a page-number (``PAGE``) field.

Theme-color resolution is **independent of all other configuration** (Req 14.5)
and **total / safe** (Req 14.3, 14.4): a valid 6-digit hex value (with or without
a leading ``#``) is used as-is; any unset or invalid value falls back to the
documented default :data:`DEFAULT_THEME_COLOR`, emits a structured warning through
the injected logger, and never raises.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from app.core.logging import StructuredLogger

# The documented default theme color (a deep professional blue), applied when the
# configured Theme_Color is unset or invalid (Req 14.4).
DEFAULT_THEME_COLOR = "1F4E79"

# The set of valid hexadecimal digits used to validate a theme-color string.
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# The number of hex digits in a valid 6-digit color.
_HEX_LENGTH = 6

# The date format used on the cover page ("Month DD, YYYY", e.g. "January 05, 2025").
_DATE_FORMAT = "%B %d, %Y"

# The Word style names relied upon for structural guarantees.
_TABLE_STYLE = "Table Grid"
_BULLET_STYLE = "List Bullet"

# The TOC field instruction (headings levels 1-3) inserted for the table of contents.
_TOC_INSTRUCTION = ' TOC \\o "1-3" \\h \\z \\u '


def _is_valid_hex_color(candidate: str) -> bool:
    """Return whether ``candidate`` is exactly six hexadecimal digits.

    Args:
        candidate: A color string with any surrounding whitespace and any single
            leading ``#`` already removed.

    Returns:
        ``True`` when ``candidate`` is a 6-digit hexadecimal string.
    """

    return len(candidate) == _HEX_LENGTH and all(c in _HEX_DIGITS for c in candidate)


def _normalize_hex_color(value: str) -> str | None:
    """Normalize ``value`` to a canonical 6-digit uppercase hex color.

    Strips surrounding whitespace and a single optional leading ``#``. When the
    remaining text is a valid 6-digit hex color, its uppercase form is returned;
    otherwise ``None`` signals an invalid value.

    Args:
        value: The raw theme-color string to normalize.

    Returns:
        The canonical uppercase 6-digit hex string, or ``None`` when invalid.
    """

    candidate = value.strip()
    if candidate.startswith("#"):
        candidate = candidate[1:]
    if _is_valid_hex_color(candidate):
        return candidate.upper()
    return None


class DocumentBuilder:
    """Assemble polished ``.docx`` deliverables with ``python-docx`` (Req 10).

    The builder is reusable: a single instance resolves its theme color once at
    construction and can produce any number of documents through :meth:`build`.

    Attributes:
        theme_color: The resolved, canonical 6-digit uppercase hex color applied
            to styled headings and the cover-page title.
    """

    def __init__(
        self,
        theme_color: str,
        logger: StructuredLogger | None = None,
    ) -> None:
        """Resolve the theme color independently and safely (Req 14.3-14.5).

        The provided ``theme_color`` is resolved on its own, independent of all
        other configuration (Req 14.5). A valid 6-digit hex value (with or
        without a leading ``#``) is adopted as the resolved color; any unset or
        invalid value falls back to :data:`DEFAULT_THEME_COLOR`, emits a
        structured warning through ``logger`` (when provided), and never raises
        (Req 14.4).

        Args:
            theme_color: The configured Theme_Color value to resolve.
            logger: An optional structured logger used to emit a warning when the
                configured color is invalid. When ``None``, no warning is emitted
                but resolution still falls back safely.
        """

        self._logger = logger
        normalized = _normalize_hex_color(theme_color) if isinstance(theme_color, str) else None
        if normalized is None:
            if self._logger is not None:
                self._logger.decision(
                    component="document_builder",
                    run_id="-",
                    decision="invalid or unset THEME_COLOR; applying documented default",
                    level="WARNING",
                    invalid_value=theme_color,
                    default_theme_color=DEFAULT_THEME_COLOR,
                )
            self.theme_color: str = DEFAULT_THEME_COLOR
        else:
            self.theme_color = normalized

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(
        self,
        *,
        title: str,
        prepared_by: str,
        sections: Sequence[Mapping[str, Any]],
        output_path: Path,
    ) -> Path:
        """Assemble a ``.docx`` deliverable and return the written path (Req 10.1).

        The produced document always contains a cover page, a table-of-contents
        field, at least one styled heading, at least one formatted table, at
        least one bullet list, and a footer page-number field. Missing keys in a
        section mapping are tolerated, and required structural elements are
        synthesized when the supplied sections do not provide them, so the
        structural guarantees hold for any input.

        Args:
            title: The document title rendered on the cover page.
            prepared_by: The cover-page "Prepared by" line value.
            sections: The ordered section payloads to render. Each mapping may
                provide ``heading`` (str), ``level`` (1 or 2), ``body`` (str),
                ``bullets`` (list of str), and ``table``
                (``{"headers": [...], "rows": [[...]]}``); all keys are optional.
            output_path: The filesystem path the document is written to; parent
                directories are created as needed.

        Returns:
            The path of the written ``.docx`` artifact (equal to ``output_path``).
        """

        document = Document()

        # Compute the full ordered list of headings the document will render
        # (real sections plus any synthesized "Summary"/"Key Points" sections)
        # BEFORE the TOC is written, so the table of contents lists the actual
        # document structure rather than a placeholder (Req 10.1).
        toc_entries = self._compute_toc_entries(sections)

        self._add_cover_page(document, title=title, prepared_by=prepared_by)
        self._add_table_of_contents(document, toc_entries)

        has_table = False
        has_bullets = False
        for section in sections:
            rendered_table, rendered_bullets = self._add_section(document, section)
            has_table = has_table or rendered_table
            has_bullets = has_bullets or rendered_bullets

        # Guarantee at least one formatted table exists overall (Req 10.1).
        if not has_table:
            self._add_summary_table(document, sections)

        # Guarantee at least one bullet list exists overall (Req 10.1).
        if not has_bullets:
            self._add_default_bullets(document, sections)

        self._add_footer_page_number(document)
        self._enable_update_fields(document)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(output_path))
        return output_path

    # ------------------------------------------------------------------
    # Cover page / TOC
    # ------------------------------------------------------------------

    def _add_cover_page(
        self, document: Document, *, title: str, prepared_by: str
    ) -> None:
        """Render the cover page and a trailing page break (Req 10.1).

        Args:
            document: The document being assembled.
            title: The document title, styled in the theme color.
            prepared_by: The value for the "Prepared by" line.
        """

        title_paragraph = document.add_paragraph()
        title_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title_run = title_paragraph.add_run(title)
        title_run.bold = True
        title_run.font.size = Pt(28)
        title_run.font.color.rgb = RGBColor.from_string(self.theme_color)

        date_paragraph = document.add_paragraph()
        date_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        date_paragraph.add_run(datetime.now().strftime(_DATE_FORMAT))

        prepared_paragraph = document.add_paragraph()
        prepared_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        prepared_paragraph.add_run(f"Prepared by: {prepared_by}")

        document.add_page_break()

    def _compute_toc_entries(
        self, sections: Sequence[Mapping[str, Any]]
    ) -> list[tuple[str, int]]:
        """Compute the ordered ``(heading, level)`` list the document will render.

        The result mirrors exactly what :meth:`build` renders: one entry per
        supplied section (its heading text and normalized level), followed by the
        synthesized ``Summary`` (level 2) section when no section provides a table
        and the synthesized ``Key Points`` (level 2) section when no section
        provides a bullet list. Computing this list up front lets the table of
        contents list the real document structure (Req 10.1).

        Args:
            sections: The ordered section payloads that will be rendered.

        Returns:
            The ordered list of ``(heading_text, level)`` tuples, where ``level``
            is always ``1`` or ``2``.
        """

        entries: list[tuple[str, int]] = []
        has_table = False
        has_bullets = False
        for section in sections:
            heading = str(section.get("heading", "") or "")
            level = section.get("level", 1)
            if level not in (1, 2):
                level = 1
            entries.append((heading, level))

            bullets = section.get("bullets")
            if isinstance(bullets, (list, tuple)) and bullets:
                has_bullets = True

            table = section.get("table")
            if isinstance(table, Mapping):
                headers = table.get("headers")
                rows = table.get("rows")
                if (
                    isinstance(headers, (list, tuple))
                    and headers
                    and isinstance(rows, (list, tuple))
                    and rows
                ):
                    has_table = True

        # These mirror the synthesized sections build() appends to guarantee a
        # table and a bullet list always exist (Req 10.1).
        if not has_table:
            entries.append(("Summary", 2))
        if not has_bullets:
            entries.append(("Key Points", 2))
        return entries

    def _add_table_of_contents(
        self, document: Document, entries: Sequence[tuple[str, int]]
    ) -> None:
        """Insert a heading and a real, visible Word ``TOC`` field (Req 10.1).

        The TOC is written as a proper complex field (``fldChar`` begin /
        ``instrText`` / ``fldChar`` separate / cached result / ``fldChar`` end).
        The cached "result" content is one visible paragraph per heading (the
        actual heading text, indented for level-2 entries), rendered as normal
        text runs so the table of contents is readable in every viewer — not just
        those that auto-refresh fields. The ``w:updateFields`` settings flag
        (set in :meth:`build`) still instructs Word to rebuild the real,
        page-numbered TOC when the document is opened.

        Args:
            document: The document being assembled.
            entries: The ordered ``(heading, level)`` list to render as the
                cached TOC result.
        """

        self._add_styled_heading(document, "Table of Contents", level=1)

        # Open the complex field: begin -> instruction -> separate.
        field_paragraph = document.add_paragraph()
        self._append_fld_char(field_paragraph, "begin")
        self._append_instr_text(field_paragraph, _TOC_INSTRUCTION)
        self._append_fld_char(field_paragraph, "separate")

        # Cached result: one visible paragraph per heading so the TOC is readable
        # in any viewer. The closing ``end`` fldChar is appended to the last
        # rendered paragraph (or the opening paragraph when there are no entries).
        last_paragraph = field_paragraph
        for text, level in entries:
            entry_paragraph = document.add_paragraph()
            if level == 2:
                entry_paragraph.paragraph_format.left_indent = Pt(18)
            entry_paragraph.add_run(text)
            last_paragraph = entry_paragraph

        self._append_fld_char(last_paragraph, "end")

        document.add_page_break()

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    def _add_section(
        self, document: Document, section: Mapping[str, Any]
    ) -> tuple[bool, bool]:
        """Render a single section, tolerating missing keys.

        Args:
            document: The document being assembled.
            section: The section payload mapping.

        Returns:
            A ``(rendered_table, rendered_bullets)`` tuple indicating whether this
            section contributed a formatted table and/or a bullet list.
        """

        heading = str(section.get("heading", "") or "")
        level = section.get("level", 1)
        if level not in (1, 2):
            level = 1
        self._add_styled_heading(document, heading, level=level)

        body = section.get("body")
        if body:
            document.add_paragraph(str(body))

        rendered_bullets = False
        bullets = section.get("bullets")
        if isinstance(bullets, (list, tuple)) and bullets:
            for bullet in bullets:
                document.add_paragraph(str(bullet), style=_BULLET_STYLE)
            rendered_bullets = True

        rendered_table = False
        table = section.get("table")
        if isinstance(table, Mapping):
            headers = table.get("headers")
            rows = table.get("rows")
            if (
                isinstance(headers, (list, tuple))
                and headers
                and isinstance(rows, (list, tuple))
                and rows
            ):
                self._add_table(document, headers, rows)
                rendered_table = True

        return rendered_table, rendered_bullets

    # ------------------------------------------------------------------
    # Structural helpers
    # ------------------------------------------------------------------

    def _add_styled_heading(
        self, document: Document, text: str, *, level: int
    ) -> None:
        """Add a ``Heading {level}`` whose runs use the theme color (Req 10.1).

        Args:
            document: The document being assembled.
            text: The heading text.
            level: The heading level (1 or 2).
        """

        heading = document.add_heading(text, level=level)
        theme_rgb = RGBColor.from_string(self.theme_color)
        for run in heading.runs:
            run.font.color.rgb = theme_rgb

    def _add_table(
        self,
        document: Document,
        headers: Sequence[Any],
        rows: Sequence[Sequence[Any]],
    ) -> None:
        """Add a bordered table with a header row and the supplied data rows.

        Args:
            document: The document being assembled.
            headers: The column header cells.
            rows: The data rows; extra cells beyond the header width are ignored.
        """

        column_count = len(headers)
        table = document.add_table(rows=1, cols=column_count)
        table.style = _TABLE_STYLE

        header_cells = table.rows[0].cells
        for index, header in enumerate(headers):
            header_cells[index].text = str(header)

        for row in rows:
            cells = table.add_row().cells
            for index in range(column_count):
                value = row[index] if index < len(row) else ""
                cells[index].text = str(value)

    def _add_summary_table(
        self, document: Document, sections: Sequence[Mapping[str, Any]]
    ) -> None:
        """Synthesize a small summary table so a table always exists (Req 10.1).

        Args:
            document: The document being assembled.
            sections: The section payloads used to derive summary rows.
        """

        self._add_styled_heading(document, "Summary", level=2)
        headers = ["Section", "Status"]
        rows: list[list[str]] = []
        for index, section in enumerate(sections, start=1):
            heading = str(section.get("heading", "") or f"Section {index}")
            rows.append([heading, "Included"])
        if not rows:
            rows.append(["Deliverable", "Generated"])
        self._add_table(document, headers, rows)

    def _add_default_bullets(
        self, document: Document, sections: Sequence[Mapping[str, Any]]
    ) -> None:
        """Synthesize a bullet list so a bullet list always exists (Req 10.1).

        Args:
            document: The document being assembled.
            sections: The section payloads used to derive key points.
        """

        self._add_styled_heading(document, "Key Points", level=2)
        points: list[str] = []
        for index, section in enumerate(sections, start=1):
            heading = str(section.get("heading", "") or "").strip()
            points.append(heading if heading else f"Section {index}")
        if not points:
            points.append("Deliverable generated by the Autonomous Agent Service.")
        for point in points:
            document.add_paragraph(point, style=_BULLET_STYLE)

    def _add_footer_page_number(self, document: Document) -> None:
        """Add a centered ``PAGE`` field to the primary section footer (Req 10.1).

        Args:
            document: The document being assembled.
        """

        footer = document.sections[0].footer
        paragraph = footer.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.add_run("Page ")
        self._add_field(paragraph, instruction=" PAGE ", cached_text="1")

    # ------------------------------------------------------------------
    # Low-level Word XML helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _add_field(paragraph: Any, *, instruction: str, cached_text: str = "") -> None:
        """Append a simple Word field (``w:fldSimple``) to ``paragraph``.

        Using a simple field lets ``python-docx`` embed a dynamic field (such as
        the footer ``PAGE`` number) that Word evaluates when the document is
        opened. The multi-paragraph TOC uses a complex field instead (see
        :meth:`_append_fld_char`).

        Args:
            paragraph: The paragraph to append the field to.
            instruction: The field instruction (e.g. ``" PAGE "`` or a TOC spec).
            cached_text: Placeholder text shown until Word refreshes the field.
        """

        fld_simple = OxmlElement("w:fldSimple")
        fld_simple.set(qn("w:instr"), instruction)
        run = OxmlElement("w:r")
        text_element = OxmlElement("w:t")
        text_element.text = cached_text
        run.append(text_element)
        fld_simple.append(run)
        paragraph._p.append(fld_simple)

    @staticmethod
    def _append_fld_char(paragraph: Any, fld_char_type: str) -> None:
        """Append a complex-field ``w:fldChar`` run to ``paragraph``.

        Complex fields (unlike ``w:fldSimple``) are delimited by ``begin``,
        ``separate``, and ``end`` field characters, which lets a field's cached
        result span its own runs and paragraphs — used here so the TOC result can
        be a readable, multi-paragraph list of headings.

        Args:
            paragraph: The paragraph to append the field-character run to.
            fld_char_type: The field-character type (``"begin"``, ``"separate"``
                or ``"end"``).
        """

        run = OxmlElement("w:r")
        fld_char = OxmlElement("w:fldChar")
        fld_char.set(qn("w:fldCharType"), fld_char_type)
        run.append(fld_char)
        paragraph._p.append(run)

    @staticmethod
    def _append_instr_text(paragraph: Any, instruction: str) -> None:
        """Append a ``w:instrText`` run carrying a complex-field instruction.

        Args:
            paragraph: The paragraph to append the instruction run to.
            instruction: The field instruction text (e.g. the ``TOC`` spec). It
                is written with ``xml:space="preserve"`` so its leading and
                trailing spaces are retained.
        """

        run = OxmlElement("w:r")
        instr_text = OxmlElement("w:instrText")
        instr_text.set(qn("xml:space"), "preserve")
        instr_text.text = instruction
        run.append(instr_text)
        paragraph._p.append(run)

    @staticmethod
    def _enable_update_fields(document: Document) -> None:
        """Set the ``w:updateFields`` settings flag so Word refreshes fields.

        This instructs Word to update dynamic fields (notably the TOC) the first
        time the document is opened, since ``python-docx`` does not compute field
        results itself.

        Args:
            document: The document being assembled.
        """

        settings_element = document.settings.element
        update_fields = settings_element.find(qn("w:updateFields"))
        if update_fields is None:
            update_fields = OxmlElement("w:updateFields")
            settings_element.append(update_fields)
        update_fields.set(qn("w:val"), "true")
