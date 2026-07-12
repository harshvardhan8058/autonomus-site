"""Application configuration loaded from environment variables.

This module defines :class:`Settings`, a ``pydantic-settings`` model that reads
every service configuration value from the environment with the documented
defaults from the design's configuration table (Req 14.1). It also exposes a
cached accessor, :func:`get_settings`, for singleton use by the app factory and
other components.

The :data:`Settings.THEME_COLOR` field is deliberately a plain, independently
readable value so that the ``DocumentBuilder`` can resolve the heading theme
color solely from this configuration variable, independent of all other
configuration (Req 14.5).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service configuration read from environment variables.

    Every field maps to an environment variable of the same (upper-case) name
    and carries the documented default from the design's configuration table
    (Req 14.1). Values are also loaded from a local ``.env`` file when present.

    Attributes:
        GROQ_API_KEY: Groq free-tier API key. When set, the Groq backend is used
            as primary; when empty/unset, the service falls back to Ollama.
        GROQ_MODEL: Groq model name used for the primary backend.
        OLLAMA_BASE_URL: Base URL of the local Ollama backend (fallback).
        OLLAMA_MODEL: Ollama model name used for the fallback backend.
        LLM_MAX_RETRIES: Maximum retries per backend before falling back/failing.
        LLM_TIMEOUT_SECONDS: Per-LLM-call timeout in seconds.
        HOST: Server bind host.
        PORT: Server port.
        RATE_LIMIT_MAX: Maximum requests allowed per window per client IP.
        RATE_LIMIT_WINDOW_SECONDS: Sliding-window size in seconds.
        THEME_COLOR: Heading theme color as a 6-digit hex string (no leading
            ``#``). Resolved independently by the DocumentBuilder (Req 14.5).
        DOCUMENT_PREPARED_BY: Cover-page "Prepared by" line.
        DOCUMENT_OUTPUT_DIR: Directory where generated ``.docx`` files are written.
        LOG_LEVEL: Logging verbosity (e.g. ``DEBUG``, ``INFO``, ``WARNING``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- LLM backends ---
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.1"
    LLM_MAX_RETRIES: int = 3
    LLM_TIMEOUT_SECONDS: int = 60

    # --- Server ---
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # --- Rate limiting (per client IP, sliding window) ---
    RATE_LIMIT_MAX: int = 10
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    # --- Document generation ---
    THEME_COLOR: str = "1F4E79"
    DOCUMENT_PREPARED_BY: str = "Autonomous Agent Service"
    DOCUMENT_OUTPUT_DIR: str = "./generated"

    # --- Observability ---
    LOG_LEVEL: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached, process-wide :class:`Settings` singleton.

    The result is memoized with :func:`functools.lru_cache` so that every caller
    (the app factory, services, etc.) shares one immutable configuration
    instance. Call ``get_settings.cache_clear()`` to force a reload (primarily
    useful in tests that mutate the environment).

    Returns:
        The shared :class:`Settings` instance.
    """

    return Settings()
