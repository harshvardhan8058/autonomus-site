"""Tests for the configuration loader (`app.core.config`).

Covers:
- Task 2.2: unit tests verifying documented defaults when env is unset and that
  set environment variables override those defaults (Req 14.1).
- Task 2.3 / Property 20: a Hypothesis property test asserting that every field
  the :class:`Settings` model reads from the environment is documented in
  ``.env.example`` (Req 14.2).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.config import Settings, get_settings

# --- Repository paths -------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# The documented defaults from design.md's configuration table (Req 14.1).
DOCUMENTED_DEFAULTS: dict[str, object] = {
    "GROQ_API_KEY": "",
    "GROQ_MODEL": "llama-3.3-70b-versatile",
    "OLLAMA_BASE_URL": "http://localhost:11434",
    "OLLAMA_MODEL": "llama3.1",
    "LLM_MAX_RETRIES": 3,
    "LLM_TIMEOUT_SECONDS": 60,
    "LLM_OFFLINE_FALLBACK": True,
    "HOST": "0.0.0.0",
    "PORT": 8000,
    "RATE_LIMIT_MAX": 10,
    "RATE_LIMIT_WINDOW_SECONDS": 60,
    "THEME_COLOR": "1F4E79",
    "DOCUMENT_PREPARED_BY": "Autonomous Agent Service",
    "DOCUMENT_OUTPUT_DIR": "./generated",
    "LOG_LEVEL": "INFO",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every Settings env var and disable .env loading for isolation."""

    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    # Point env_file at a nonexistent path so a developer's local .env cannot
    # leak into the "unset" defaults assertions.
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    get_settings.cache_clear()


# --- Task 2.2: unit tests ---------------------------------------------------


def test_defaults_when_env_unset(clean_env: None) -> None:
    """Unset environment -> every field takes its documented default (Req 14.1)."""

    s = Settings()
    for name, expected in DOCUMENTED_DEFAULTS.items():
        assert getattr(s, name) == expected, f"{name} default mismatch"


def test_env_overrides_applied(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set environment variables override the documented defaults (Req 14.1)."""

    overrides = {
        "GROQ_API_KEY": "test-key-123",
        "GROQ_MODEL": "custom-model",
        "OLLAMA_BASE_URL": "http://ollama.internal:9999",
        "OLLAMA_MODEL": "mistral",
        "LLM_MAX_RETRIES": "5",
        "LLM_TIMEOUT_SECONDS": "120",
        "HOST": "127.0.0.1",
        "PORT": "9000",
        "RATE_LIMIT_MAX": "25",
        "RATE_LIMIT_WINDOW_SECONDS": "30",
        "THEME_COLOR": "ABCDEF",
        "DOCUMENT_PREPARED_BY": "Acme Corp",
        "DOCUMENT_OUTPUT_DIR": "/tmp/out",
        "LOG_LEVEL": "DEBUG",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    s = Settings()

    assert s.GROQ_API_KEY == "test-key-123"
    assert s.GROQ_MODEL == "custom-model"
    assert s.OLLAMA_BASE_URL == "http://ollama.internal:9999"
    assert s.OLLAMA_MODEL == "mistral"
    assert s.LLM_MAX_RETRIES == 5
    assert s.LLM_TIMEOUT_SECONDS == 120
    assert s.HOST == "127.0.0.1"
    assert s.PORT == 9000
    assert s.RATE_LIMIT_MAX == 25
    assert s.RATE_LIMIT_WINDOW_SECONDS == 30
    assert s.THEME_COLOR == "ABCDEF"
    assert s.DOCUMENT_PREPARED_BY == "Acme Corp"
    assert s.DOCUMENT_OUTPUT_DIR == "/tmp/out"
    assert s.LOG_LEVEL == "DEBUG"


def test_theme_color_resolves_independently(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THEME_COLOR is exposed as a plain value resolvable on its own (Req 14.5)."""

    monkeypatch.setenv("THEME_COLOR", "00FF00")
    s = Settings()
    assert s.THEME_COLOR == "00FF00"


def test_get_settings_is_cached_singleton(clean_env: None) -> None:
    """get_settings() returns the same cached instance on repeated calls."""

    first = get_settings()
    second = get_settings()
    assert first is second
    assert isinstance(first, Settings)


# --- Task 2.3 / Property 20 -------------------------------------------------


def _env_example_text() -> str:
    """Read the raw .env.example contents once for property assertions."""

    return ENV_EXAMPLE.read_text(encoding="utf-8")


# Feature: autonomous-agent-service, Property 20: .env.example documents every configuration variable  # noqa: E501
@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(field_name=st.sampled_from(sorted(Settings.model_fields)))
def test_env_example_documents_every_settings_field(field_name: str) -> None:
    """Property 20: every Settings env var name appears in .env.example.

    **Validates: Requirements 14.2**

    For any field the Settings object reads from the environment, that
    variable's name must be documented in ``.env.example`` (as a
    ``NAME=`` assignment line).
    """

    text = _env_example_text()
    lines = text.splitlines()
    # The variable must appear as an assignment line "NAME=...", ignoring
    # surrounding whitespace and comment lines.
    documented = any(
        line.strip().startswith(f"{field_name}=") for line in lines
    )
    assert documented, f"{field_name} is not documented in .env.example"
