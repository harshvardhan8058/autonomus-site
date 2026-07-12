"""Frontend checks for the mission-control console (Task 19.4).

These tests read the static frontend files as text and assert the structural
and design-token requirements from Req 11 and 12 without a browser or DOM:

- ``index.html`` presents a centered input, both example chips verbatim, and a
  one-line service description (Req 11.1, 12.1).
- The Plan_Step timeline container is present and marked hidden initially
  (Req 11.2), and the assumptions / reasoning-log / result-card containers all
  exist (Req 11.6, 11.7).
- ``styles.css`` passes a pragmatic token lint: no gradient fills, no purple
  hues, and the single amber accent is present (Req 11.8).

The lint is intentionally pragmatic: it checks for the literal ``gradient(``,
the keyword ``purple``, a small set of common purple hexes, and the amber
accent hex — enough to catch an accidental violation of the design tokens.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# The frontend directory, resolved relative to this test file.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_INDEX_HTML = _FRONTEND_DIR / "index.html"
_APP_JS = _FRONTEND_DIR / "app.js"
_STYLES_CSS = _FRONTEND_DIR / "styles.css"

# The two example requests that MUST appear verbatim as chips (Req 12.1).
_CHIP_CONCRETE = (
    "Create a project proposal for migrating our on-premise CRM to the cloud."
)
_CHIP_AMBIGUOUS = (
    "We need something for the leadership meeting next week about the new "
    "product... it should cover the important stuff, budget maybe, and the "
    "timeline isn't final. Make it look official."
)

# The single amber accent color the theme must use (Req 11.8).
_ACCENT_HEX = "#FFB000"

# Common purple hexes that must never appear (Req 11.8). Pragmatic heuristic.
_FORBIDDEN_PURPLE_HEXES = ("#800080", "#6f42c1", "#6610f2", "#9b59b6", "#a020f0")


@pytest.fixture(scope="module")
def index_html() -> str:
    """Return the text of ``frontend/index.html``."""

    return _INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def styles_css() -> str:
    """Return the text of ``frontend/styles.css``."""

    return _STYLES_CSS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File existence
# ---------------------------------------------------------------------------


def test_frontend_files_exist() -> None:
    """All three frontend assets exist (Req 11)."""

    assert _INDEX_HTML.is_file(), "missing frontend/index.html"
    assert _APP_JS.is_file(), "missing frontend/app.js"
    assert _STYLES_CSS.is_file(), "missing frontend/styles.css"


# ---------------------------------------------------------------------------
# index.html structure (Req 11.1, 11.2, 11.6, 11.7, 12.1)
# ---------------------------------------------------------------------------


def test_index_has_centered_input(index_html: str) -> None:
    """A large centered request input is present (Req 11.1)."""

    # A textarea (or input) that carries the request-input class.
    assert re.search(
        r"<(textarea|input)[^>]*class=\"[^\"]*request-input", index_html
    ), "expected a request input with the request-input class"
    # The idle panel that centers the input exists.
    assert "idle-inner" in index_html


def test_index_has_one_line_description(index_html: str) -> None:
    """A one-line service description is present (Req 11.1)."""

    assert 'class="app-description"' in index_html
    match = re.search(
        r"<p[^>]*class=\"app-description\"[^>]*>(.*?)</p>", index_html, re.DOTALL
    )
    assert match, "expected an app-description paragraph"
    assert match.group(1).strip(), "service description must not be empty"


def test_index_contains_both_example_chips_verbatim(index_html: str) -> None:
    """Both predefined example requests appear verbatim (Req 12.1)."""

    assert _CHIP_CONCRETE in index_html, "concrete CRM-migration chip missing"
    assert _CHIP_AMBIGUOUS in index_html, "ambiguous leadership-meeting chip missing"


def test_index_timeline_hidden_initially(index_html: str) -> None:
    """The Plan_Step timeline container exists and is hidden initially (Req 11.2)."""

    match = re.search(r"<div[^>]*id=\"timeline\"[^>]*>", index_html)
    assert match, "expected a timeline container"
    assert "hidden" in match.group(0), "timeline must be hidden until submit"


def test_index_has_required_containers(index_html: str) -> None:
    """Assumptions, reasoning-log, and result-card containers exist (Req 11.6, 11.7)."""

    assert 'id="assumptions-panel"' in index_html
    assert 'id="reasoning-log"' in index_html
    assert 'id="result-card"' in index_html


def test_index_links_css_and_js(index_html: str) -> None:
    """The page links styles.css and app.js (Req 11)."""

    assert "styles.css" in index_html
    assert "app.js" in index_html


# ---------------------------------------------------------------------------
# styles.css token lint (Req 11.8)
# ---------------------------------------------------------------------------


def test_styles_have_no_gradient_fills(styles_css: str) -> None:
    """The stylesheet uses no gradient fills (Req 11.8)."""

    assert "gradient(" not in styles_css.lower(), "no gradient fills allowed"


def test_styles_have_no_purple_hues(styles_css: str) -> None:
    """The stylesheet contains no purple hues (Req 11.8)."""

    lowered = styles_css.lower()
    assert "purple" not in lowered, "the keyword 'purple' must not appear"
    for hex_value in _FORBIDDEN_PURPLE_HEXES:
        assert hex_value.lower() not in lowered, f"forbidden purple hex {hex_value}"


def test_styles_use_amber_accent(styles_css: str) -> None:
    """The single amber accent color is present (Req 11.8)."""

    assert _ACCENT_HEX.lower() in styles_css.lower(), "expected amber accent #FFB000"


def test_styles_use_monospace_for_logs(styles_css: str) -> None:
    """A monospace typeface is used for the reasoning log (Req 11.8)."""

    assert "monospace" in styles_css.lower(), "reasoning log must use a monospace font"


def test_styles_support_reduced_motion(styles_css: str) -> None:
    """A prefers-reduced-motion media query disables animation (Req 11.8)."""

    assert "prefers-reduced-motion" in styles_css.lower()



# ---------------------------------------------------------------------------
# Enhanced HUD markup (live status pill, run stats, toasts, controls)
# ---------------------------------------------------------------------------


def test_index_has_backend_status_pill(index_html: str) -> None:
    """A live backend status pill is present in the HUD banner."""

    assert 'id="backend-status"' in index_html, "expected a backend-status pill"


def test_index_has_run_stats_hud(index_html: str) -> None:
    """A run-stats strip exists and is hidden until a run starts."""

    match = re.search(r"<div[^>]*id=\"run-stats\"[^>]*>", index_html)
    assert match, "expected a run-stats container"
    assert "hidden" in match.group(0), "run-stats must be hidden until submit"


def test_index_has_progress_bar(index_html: str) -> None:
    """A progress bar element reflecting completed steps exists."""

    assert 'id="progress-fill"' in index_html, "expected a progress-fill element"
    assert 'role="progressbar"' in index_html, "expected a progressbar role"


def test_index_has_char_counter(index_html: str) -> None:
    """A live character counter is present near the input."""

    assert 'id="char-counter"' in index_html, "expected a character counter"


def test_index_has_toast_container(index_html: str) -> None:
    """A toast container exists for transient notifications."""

    assert 'id="toast-container"' in index_html, "expected a toast-container"


def test_index_has_new_run_reset_control(index_html: str) -> None:
    """A New run / reset control exists to return to the idle state."""

    assert 'id="new-run-button"' in index_html, "expected a New run reset control"
    assert "New run" in index_html, "expected a visible 'New run' label"


def test_index_has_open_in_new_tab_link(index_html: str) -> None:
    """An 'Open in new tab' link (target=_blank) accompanies the download."""

    match = re.search(r"<a[^>]*id=\"open-tab-button\"[^>]*>", index_html)
    assert match, "expected an open-in-new-tab link"
    assert 'target="_blank"' in match.group(0), "open-tab link must open a new tab"


def test_index_preserves_core_hooks(index_html: str) -> None:
    """All element ids app.js relies on remain present in the markup."""

    for hook in (
        'id="request-form"',
        'id="request-input"',
        'id="submit-button"',
        'id="example-chips"',
        'id="error-banner"',
        'id="timeline"',
        'id="timeline-list"',
        'id="assumptions-panel"',
        'id="assumptions-list"',
        'id="reasoning-log"',
        'id="reasoning-output"',
        'id="result-card"',
        'id="result-status"',
        'id="result-summary"',
        'id="result-steps"',
        'id="download-button"',
    ):
        assert hook in index_html, f"missing required hook {hook}"


def test_styles_progress_fill_is_solid_amber(styles_css: str) -> None:
    """The progress fill uses the solid amber accent, never a gradient."""

    assert ".progress-fill" in styles_css, "expected a progress-fill rule"
    # Re-affirm the global no-gradient invariant after the enhancements.
    assert "gradient(" not in styles_css.lower(), "no gradient fills allowed"


def test_styles_reaffirm_tokens_after_enhancements(styles_css: str) -> None:
    """Design tokens hold after the HUD enhancements (amber/no-purple/mono)."""

    lowered = styles_css.lower()
    assert _ACCENT_HEX.lower() in lowered, "amber accent must remain present"
    assert "purple" not in lowered, "the keyword 'purple' must not appear"
    assert "monospace" in lowered, "monospace must remain present"
    assert "prefers-reduced-motion" in lowered, "reduced-motion query must remain"
    for hex_value in _FORBIDDEN_PURPLE_HEXES:
        assert hex_value.lower() not in lowered, f"forbidden purple hex {hex_value}"
