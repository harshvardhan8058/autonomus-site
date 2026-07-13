"""LLM service with Groq-primary / Ollama-fallback backends (Req 5, 2.4, 2.5).

This module defines :class:`LLMService`, the single choke point for every LLM
call in the Autonomous Agent Service. It is responsible for:

- **Backend selection** — Groq is the primary backend whenever ``GROQ_API_KEY``
  is set; otherwise the service uses the local Ollama backend (Req 5.1, 5.2).
- **Resilient completions** — every call is wrapped with exponential-backoff
  retries (at most :data:`Settings.LLM_MAX_RETRIES` attempts per backend) and a
  Groq -> Ollama fallback (Req 5.3, 2.4, 2.5).
- **JSON-constrained completions** — :meth:`LLMService.complete_json` parses the
  model output into a Pydantic schema, running a JSON-repair pass on malformed
  output before retrying, and raising :class:`LLMJSONError` only after repair
  and retries fail on every backend (Req 2.4, property P6).
- **Health probing** — :meth:`LLMService.health` reports the active backend name
  and whether it is reachable, powering ``/health`` and ``/health/ready``
  (Req 5.4, 5.6).

Backends are accessed through the small :class:`LLMBackend` transport protocol,
so tests inject a fake backend and exercise the retry / fallback / repair logic
without any network calls (see ``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from app.core.config import Settings

SchemaT = TypeVar("SchemaT", bound=BaseModel)
_T = TypeVar("_T")

# Base delay (seconds) for the exponential-backoff schedule. The Nth retry waits
# ``_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)`` seconds. The sleep function is
# injectable so tests can substitute a no-op and avoid real delays.
_BACKOFF_BASE_SECONDS = 0.5

# Class-name fragments that identify a connection/timeout style failure. These
# are matched (case-sensitively as substrings) against the exception class name
# and every class name in its cause/context chain so the check works without
# importing the ``groq`` or ``httpx`` SDKs (they raise SDK-specific types).
_CONNECTION_ERROR_CLASS_FRAGMENTS = (
    "ConnectError",
    "ConnectTimeout",
    "APIConnectionError",
    "ConnectionError",
    "ReadTimeout",
    "Timeout",
)

# Lowercased message substrings that identify a connection/timeout failure even
# when the exception type is generic (for example a bare ``RuntimeError`` whose
# message describes a failed connection).
_CONNECTION_ERROR_MESSAGE_FRAGMENTS = (
    "all connection attempts failed",
    "connection attempts failed",
    "connection refused",
    "failed to establish",
    "name or service not known",
    "timed out",
    "timeout",
)


def _is_connection_error(exc: BaseException) -> bool:
    """Return whether ``exc`` looks like a connection/timeout failure (Req 5.3).

    A connection error will not be fixed by retrying, so the retry loop uses this
    predicate to fast-fail such attempts (and the circuit breaker trips on them).
    The detection is deliberately defensive and SDK-agnostic: it walks the
    exception together with its ``__cause__`` / ``__context__`` chain and returns
    ``True`` if any linked exception has a class name containing a known
    connection fragment (e.g. ``APIConnectionError``, ``ConnectTimeout``) or a
    message containing a known connection substring (e.g. "all connection
    attempts failed", "connection refused", "timed out").

    Args:
        exc: The exception to classify.

    Returns:
        ``True`` if the exception (or one of its linked causes) is a
        connection/timeout style failure; ``False`` otherwise.
    """

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        class_name = type(current).__name__
        if any(fragment in class_name for fragment in _CONNECTION_ERROR_CLASS_FRAGMENTS):
            return True
        message = str(current).lower()
        if any(
            fragment in message for fragment in _CONNECTION_ERROR_MESSAGE_FRAGMENTS
        ):
            return True
        # Follow the explicit cause first, then the implicit context.
        current = current.__cause__ or current.__context__
    return False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Raised when an LLM completion fails on all backends after retries.

    Carries the human-readable reason and, when available, the underlying cause
    of the final failure (accessible via ``__cause__``).
    """


class LLMJSONError(LLMError):
    """Raised when a JSON-constrained completion cannot yield schema-valid output.

    This is raised by :meth:`LLMService.complete_json` **only** after the
    JSON-repair pass and all retries have failed on every backend (Req 2.4,
    property P6). It signals a clean failure: the service never returns a
    partially-parsed or schema-invalid object.
    """


# ---------------------------------------------------------------------------
# Backend transport seam
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMBackend(Protocol):
    """Pluggable transport for a single LLM backend (Groq, Ollama, or a fake).

    Implementations perform the raw text completion and a lightweight
    reachability probe. All retry, fallback, and JSON-repair logic lives in
    :class:`LLMService`, so a backend only needs to translate a prompt into a
    completion string and report whether it is reachable.

    Attributes:
        name: The stable backend name (``"groq"`` or ``"ollama"``).
    """

    name: str

    async def complete(
        self,
        *,
        prompt: str,
        system: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        """Produce a completion for ``prompt``.

        Args:
            prompt: The user prompt.
            system: An optional system instruction.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            json_mode: When ``True``, request the backend's native JSON /
                structured-output mode so the model is constrained to emit a
                single valid JSON object. When ``False`` (default), a free-form
                completion is produced.

        Returns:
            The completion text.

        Raises:
            Exception: Any transport/backend error; the service treats a raised
                exception as a failed attempt eligible for retry/fallback.
        """
        ...

    async def health(self) -> bool:
        """Return whether the backend is currently reachable."""
        ...


class GroqBackend:
    """Groq free-tier transport using the ``groq`` async SDK (Req 5.1).

    Uses the model configured in :data:`Settings.GROQ_MODEL` (default
    ``llama-3.3-70b-versatile``). The SDK client is created lazily so that
    constructing an :class:`LLMService` never requires a key to be present.

    Attributes:
        name: Always ``"groq"``.
    """

    name = "groq"

    def __init__(self, settings: Settings) -> None:
        """Initialize the Groq backend.

        Args:
            settings: The service settings providing the API key, model, and
                per-call timeout.
        """

        self._settings = settings
        self._client: object | None = None

    def _get_client(self) -> object:
        """Return a lazily-created ``AsyncGroq`` client.

        Returns:
            The Groq async client instance.
        """

        if self._client is None:
            import httpx
            from groq import AsyncGroq

            # Use a short CONNECT timeout so an unreachable backend fails fast,
            # while keeping the long read/write/pool timeout for generation. The
            # groq SDK accepts an ``httpx.Timeout`` for its ``timeout=`` param.
            timeout = httpx.Timeout(
                connect=float(self._settings.LLM_CONNECT_TIMEOUT_SECONDS),
                read=float(self._settings.LLM_TIMEOUT_SECONDS),
                write=float(self._settings.LLM_TIMEOUT_SECONDS),
                pool=float(self._settings.LLM_TIMEOUT_SECONDS),
            )
            self._client = AsyncGroq(
                api_key=self._settings.GROQ_API_KEY,
                timeout=timeout,
            )
        return self._client

    async def complete(
        self,
        *,
        prompt: str,
        system: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        """Produce a completion via the Groq chat-completions API.

        When ``json_mode`` is ``True``, ``response_format={"type":
        "json_object"}`` is passed so Groq constrains the model to emit a single
        valid JSON object. Groq requires the literal token ``json`` to appear in
        the messages when this mode is on; the JSON-only system directive built
        by :meth:`LLMService._json_system_prompt` guarantees this.
        """

        client = self._get_client()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, object] = {
            "model": self._settings.GROQ_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = await client.chat.completions.create(  # type: ignore[attr-defined]
            **kwargs,
        )
        return response.choices[0].message.content or ""

    async def health(self) -> bool:
        """Probe Groq reachability by listing available models."""

        client = self._get_client()
        await client.models.list()  # type: ignore[attr-defined]
        return True


class OllamaBackend:
    """Local Ollama transport over HTTP using ``httpx`` (Req 5.2).

    Calls the Ollama ``/api/generate`` endpoint at
    :data:`Settings.OLLAMA_BASE_URL` with the model
    :data:`Settings.OLLAMA_MODEL`.

    Attributes:
        name: Always ``"ollama"``.
    """

    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        """Initialize the Ollama backend.

        Args:
            settings: The service settings providing the base URL, model, and
                per-call timeout.
        """

        self._settings = settings

    @property
    def _base_url(self) -> str:
        """Return the Ollama base URL without a trailing slash."""

        return self._settings.OLLAMA_BASE_URL.rstrip("/")

    async def complete(
        self,
        *,
        prompt: str,
        system: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        """Produce a completion via the Ollama ``/api/generate`` endpoint.

        When ``json_mode`` is ``True``, ``"format": "json"`` is added to the
        request payload so Ollama constrains the model to emit valid JSON.
        """

        import httpx

        payload: dict[str, object] = {
            "model": self._settings.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        timeout = httpx.Timeout(
            connect=float(self._settings.LLM_CONNECT_TIMEOUT_SECONDS),
            read=float(self._settings.LLM_TIMEOUT_SECONDS),
            write=float(self._settings.LLM_TIMEOUT_SECONDS),
            pool=float(self._settings.LLM_TIMEOUT_SECONDS),
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._base_url}/api/generate", json=payload
            )
            response.raise_for_status()
            data = response.json()
        return str(data.get("response", ""))

    async def health(self) -> bool:
        """Probe Ollama reachability via the ``/api/tags`` endpoint."""

        import httpx

        timeout = httpx.Timeout(
            connect=float(self._settings.LLM_CONNECT_TIMEOUT_SECONDS),
            read=float(self._settings.LLM_TIMEOUT_SECONDS),
            write=float(self._settings.LLM_TIMEOUT_SECONDS),
            pool=float(self._settings.LLM_TIMEOUT_SECONDS),
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{self._base_url}/api/tags")
        return response.status_code == 200


# ---------------------------------------------------------------------------
# LLM service
# ---------------------------------------------------------------------------


class LLMService:
    """Resilient LLM facade with backend selection, retries, and JSON repair.

    The service resolves its active backend from :data:`Settings.GROQ_API_KEY`
    (Groq when set, else Ollama), wraps every call in exponential-backoff
    retries with a Groq -> Ollama fallback, and offers a JSON-constrained
    completion that repairs malformed model output before failing cleanly.

    Attributes:
        _settings: The service settings.
        _groq: The Groq backend transport.
        _ollama: The Ollama backend transport.
        _sleep: The (injectable) async sleep used between retries.
        _resolved: Whether the active backend has been resolved.
        _backend_name: The resolved active-backend name, or ``"unknown"``.
        _clock: The (injectable) monotonic clock used by the circuit breaker.
        _breaker_open_until: The monotonic timestamp until which the circuit
            breaker is open (the network is skipped in favor of the offline
            fallback). ``0.0`` means the breaker is closed.
        _breaker_cooldown: How long (seconds) the breaker stays open once tripped.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        groq_backend: LLMBackend | None = None,
        ollama_backend: LLMBackend | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
        resolve: bool = True,
    ) -> None:
        """Initialize the LLM service.

        Args:
            settings: The service settings driving backend selection, model
                names, retry count, and timeouts.
            groq_backend: An optional Groq transport override. When ``None``, a
                real :class:`GroqBackend` is created. Tests inject a fake here.
            ollama_backend: An optional Ollama transport override. When ``None``,
                a real :class:`OllamaBackend` is created.
            sleep: An optional async sleep function used between retries. When
                ``None``, :func:`asyncio.sleep` is used. Tests inject a no-op to
                avoid real delays.
            clock: An optional monotonic clock (returning seconds) used by the
                circuit breaker. When ``None``, :func:`time.monotonic` is used.
                Tests inject a fake clock to drive the cooldown deterministically.
            resolve: When ``True`` (default), the active backend is resolved
                eagerly at construction so :attr:`active_backend` is immediately
                deterministic. When ``False``, :attr:`active_backend` reports
                ``"unknown"`` until :meth:`resolve_backend` is called, modeling
                the unresolved-backend health state (Req 5.5).
        """

        self._settings = settings
        self._groq: LLMBackend = (
            groq_backend if groq_backend is not None else GroqBackend(settings)
        )
        self._ollama: LLMBackend = (
            ollama_backend if ollama_backend is not None else OllamaBackend(settings)
        )
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else asyncio.sleep
        )
        self._clock: Callable[[], float] = (
            clock if clock is not None else time.monotonic
        )
        self._breaker_open_until: float = 0.0
        self._breaker_cooldown = float(
            settings.LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS
        )
        self._resolved = False
        self._backend_name = "unknown"
        if resolve:
            self.resolve_backend()

    # -- backend selection --------------------------------------------------

    def resolve_backend(self) -> str:
        """Resolve and cache the active backend name from settings (Req 5.1, 5.2).

        Returns:
            The resolved active-backend name (``"groq"`` or ``"ollama"``).
        """

        self._backend_name = "groq" if self._settings.GROQ_API_KEY else "ollama"
        self._resolved = True
        return self._backend_name

    @property
    def active_backend(self) -> str:
        """The active backend name.

        Returns ``"groq"`` if and only if ``GROQ_API_KEY`` is set and the
        backend has been resolved, ``"ollama"`` when no key is set, and
        ``"unknown"`` while the backend remains unresolved (Req 5.1, 5.2, 5.5,
        property P8).
        """

        if not self._resolved:
            return "unknown"
        return self._backend_name

    def _backend_chain(self) -> list[LLMBackend]:
        """Return the ordered backends to try for a call (primary first).

        When Groq is active, the chain is ``[groq, ollama]`` so that an
        exhausted Groq backend falls back to Ollama (Req 2.5, 5.3, property P7).
        When Ollama is active (no Groq key), the chain is ``[ollama]``.

        Returns:
            The ordered list of backends to attempt.
        """

        if self.active_backend == "groq":
            return [self._groq, self._ollama]
        return [self._ollama]

    def _backend_by_name(self, name: str) -> LLMBackend | None:
        """Return the backend transport for a name, or ``None`` if unknown."""

        if name == "groq":
            return self._groq
        if name == "ollama":
            return self._ollama
        return None

    # -- circuit breaker ----------------------------------------------------

    def _breaker_is_open(self) -> bool:
        """Return whether the breaker is currently open (skip the network).

        Returns:
            ``True`` while the current time is before the breaker's open-until
            deadline; ``False`` once the cooldown has elapsed (or it was reset).
        """

        return self._clock() < self._breaker_open_until

    def _breaker_trip(self) -> None:
        """Open the breaker for the configured cooldown starting now.

        Called after a call fails on every backend so subsequent calls in the
        run skip the network and use the offline fallback until the cooldown
        window elapses.
        """

        self._breaker_open_until = self._clock() + self._breaker_cooldown

    def _breaker_reset(self) -> None:
        """Close the breaker so subsequent calls attempt the network again.

        Called after any successful completion so a recovered backend is used
        immediately (the breaker never suppresses a reachable backend).
        """

        self._breaker_open_until = 0.0

    # -- public completion API ---------------------------------------------

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        offline_fallback: Callable[[], str] | None = None,
    ) -> str:
        """Return a free-form completion with retries and Groq -> Ollama fallback.

        Each backend is attempted with up to :data:`Settings.LLM_MAX_RETRIES`
        exponential-backoff attempts; if the primary backend is exhausted, the
        secondary backend is attempted before failing (Req 5.3).

        When every backend fails and ``offline_fallback`` is provided, the
        callable is invoked and its (already-valid) result is returned instead of
        raising :class:`LLMError` — the deterministic offline degradation path.
        When ``offline_fallback`` is ``None`` the method raises exactly as before.

        Args:
            prompt: The user prompt.
            system: An optional system instruction.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            offline_fallback: An optional synchronous callable producing a
                completion string to return when all backends fail. When
                ``None`` (default), the method raises on total failure.

        Returns:
            The completion text.

        Raises:
            LLMError: If every backend fails after all retries and no
                ``offline_fallback`` was provided.
        """

        # Short-circuit while the breaker is open: a fallback-capable caller
        # skips the network entirely and returns the offline value instantly.
        if offline_fallback is not None and self._breaker_is_open():
            return offline_fallback()

        async def call(backend: LLMBackend) -> str:
            return await backend.complete(
                prompt=prompt,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=False,
            )

        try:
            result = await self._run_with_fallback(call)
        except LLMError as exc:
            self._breaker_trip()
            return self._apply_offline_fallback(offline_fallback, exc)
        except Exception as exc:  # noqa: BLE001 - normalize to the public error
            self._breaker_trip()
            error = LLMError(f"LLM completion failed on all backends: {exc}")
            error.__cause__ = exc
            return self._apply_offline_fallback(offline_fallback, error)
        self._breaker_reset()
        return result

    async def complete_json(
        self,
        prompt: str,
        schema: type[SchemaT],
        *,
        system: str | None = None,
        offline_fallback: Callable[[], SchemaT] | None = None,
    ) -> SchemaT:
        """Return a completion parsed into ``schema``, repairing malformed JSON.

        For each attempt the raw model output is parsed against ``schema``; on a
        parse failure a JSON-repair pass (:meth:`_repair_json`) is applied and
        the parse retried. If the attempt still fails it is retried under the
        backoff/fallback policy. :class:`LLMJSONError` is raised only after
        repair and all retries fail on every backend (Req 2.4, property P6).

        When every backend fails and ``offline_fallback`` is provided, the
        callable is invoked and its (already-valid) schema instance is returned
        instead of raising :class:`LLMJSONError` — the deterministic offline
        degradation path. When ``offline_fallback`` is ``None`` the method raises
        exactly as before.

        Args:
            prompt: The user prompt.
            schema: The Pydantic model the output must validate against.
            system: An optional system instruction; a JSON-only directive is
                appended so the model is steered toward emitting pure JSON.
            offline_fallback: An optional synchronous callable producing a valid
                ``schema`` instance to return when all backends fail. When
                ``None`` (default), the method raises on total failure.

        Returns:
            A validated instance of ``schema``.

        Raises:
            LLMJSONError: If no backend yields schema-valid output after repair
                and retries and no ``offline_fallback`` was provided.
        """

        json_system = self._json_system_prompt(system)

        # Short-circuit while the breaker is open: a fallback-capable caller
        # skips the network entirely and returns the offline value instantly.
        if offline_fallback is not None and self._breaker_is_open():
            return offline_fallback()

        async def call(backend: LLMBackend) -> SchemaT:
            raw = await backend.complete(
                prompt=prompt,
                system=json_system,
                temperature=0.0,
                max_tokens=2048,
                json_mode=True,
            )
            return self._parse_json(raw, schema)

        try:
            result = await self._run_with_fallback(call)
        except LLMJSONError as exc:
            self._breaker_trip()
            return self._apply_offline_fallback(offline_fallback, exc)
        except Exception as exc:  # noqa: BLE001 - normalize to the JSON error
            self._breaker_trip()
            error = LLMJSONError(
                f"failed to obtain schema-valid JSON from any backend: {exc}"
            )
            error.__cause__ = exc
            return self._apply_offline_fallback(offline_fallback, error)
        self._breaker_reset()
        return result

    @staticmethod
    def _apply_offline_fallback(
        offline_fallback: Callable[[], _T] | None,
        error: LLMError,
    ) -> _T:
        """Return ``offline_fallback()`` on total failure, or re-raise ``error``.

        When no ``offline_fallback`` is provided the original ``error`` is raised
        unchanged, preserving the historical failure contract. When a fallback is
        provided it is invoked to produce an already-valid value; if the fallback
        itself raises, the original LLM ``error`` is raised instead so a genuine
        backend failure is never masked by a bug in the fallback.

        Args:
            offline_fallback: The optional synchronous fallback callable.
            error: The normalized LLM error to raise when no fallback succeeds.

        Returns:
            The value produced by ``offline_fallback``.

        Raises:
            LLMError: The original ``error`` when no fallback is provided or the
                fallback itself raises.
        """

        if offline_fallback is None:
            raise error
        try:
            return offline_fallback()
        except Exception:  # noqa: BLE001 - never mask the real backend failure
            raise error from None

    async def health(self) -> tuple[str, bool]:
        """Return ``(backend_name, reachable)`` for the active backend (Req 5.4, 5.6).

        Returns:
            A tuple of the active backend name and whether it is reachable. When
            the backend is unresolved, returns ``("unknown", False)`` (Req 5.5).
        """

        name = self.active_backend
        backend = self._backend_by_name(name)
        if backend is None:
            return ("unknown", False)
        try:
            reachable = await backend.health()
        except Exception:  # noqa: BLE001 - health probing must never raise
            reachable = False
        return (name, bool(reachable))

    # -- internal helpers ---------------------------------------------------

    async def _run_with_fallback(
        self, call: Callable[[LLMBackend], Awaitable[SchemaT]]
    ) -> SchemaT:
        """Run ``call`` across the backend chain, each with backoff (Req 2.5, 5.3).

        Attempts the primary backend with :meth:`_with_backoff`; on exhaustion,
        attempts the next backend in the chain. Raises the last error only after
        every backend has been exhausted.

        Args:
            call: A coroutine factory taking a backend and producing a result.

        Returns:
            The first successful result.

        Raises:
            Exception: The last error encountered once every backend is exhausted.
        """

        last_exc: BaseException | None = None
        for backend in self._backend_chain():
            try:
                return await self._with_backoff(lambda b=backend: call(b))
            except Exception as exc:  # noqa: BLE001 - try the next backend
                last_exc = exc
                continue
        assert last_exc is not None  # chain is never empty
        raise last_exc

    async def _with_backoff(
        self, coro_factory: Callable[[], Awaitable[SchemaT]]
    ) -> SchemaT:
        """Invoke ``coro_factory`` with bounded exponential-backoff retries.

        The maximum number of attempts is :data:`Settings.LLM_MAX_RETRIES`
        (default 3, floored at 1). Between attempts the service sleeps for an
        exponentially growing delay using the injectable sleep function
        (Req 5.3, 2.4, property P7). Connection/timeout failures (detected by
        :func:`_is_connection_error`) are re-raised immediately without sleeping,
        since retrying an unreachable backend only wastes the backoff schedule.

        Args:
            coro_factory: A zero-argument coroutine factory to invoke per attempt.

        Returns:
            The first successful result.

        Raises:
            Exception: The last error after all attempts are exhausted.
        """

        max_attempts = max(1, int(self._settings.LLM_MAX_RETRIES))
        last_exc: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await coro_factory()
            except Exception as exc:  # noqa: BLE001 - retry until exhausted
                last_exc = exc
                # A connection/timeout failure will not be fixed by retrying, so
                # re-raise immediately instead of burning the backoff schedule.
                if _is_connection_error(exc):
                    raise
                if attempt < max_attempts:
                    await self._sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        assert last_exc is not None  # at least one attempt always runs
        raise last_exc

    def _parse_json(self, raw: str, schema: type[SchemaT]) -> SchemaT:
        """Parse ``raw`` into ``schema``, repairing malformed JSON on failure.

        First attempts a direct parse; on failure applies :meth:`_repair_json`
        and re-parses. Raises :class:`LLMJSONError` when both attempts fail so
        the caller never receives a partial or invalid object (property P6).

        Args:
            raw: The raw model output.
            schema: The Pydantic model to validate against.

        Returns:
            A validated instance of ``schema``.

        Raises:
            LLMJSONError: If neither the direct parse nor the repaired parse
                yields a schema-valid instance.
        """

        try:
            return schema.model_validate_json(raw)
        except (ValidationError, ValueError):
            pass

        repaired = self._repair_json(raw)
        try:
            return schema.model_validate_json(repaired)
        except (ValidationError, ValueError) as exc:
            snippet = re.sub(r"\s+", " ", raw).strip()[:200]
            raise LLMJSONError(
                f"model output was not schema-valid JSON: {snippet}"
            ) from exc

    def _json_system_prompt(self, system: str | None) -> str:
        """Build the system prompt for JSON-constrained completions.

        Args:
            system: The caller-supplied system instruction, if any.

        Returns:
            The system instruction augmented with a JSON-only directive.
        """

        directive = (
            "You must respond with a single valid JSON object only. "
            "Do not include markdown code fences, commentary, or trailing text. "
            "Output json only."
        )
        if system:
            return f"{system}\n\n{directive}"
        return directive

    @staticmethod
    def _repair_json(raw: str) -> str:
        """Best-effort repair of malformed JSON text (Req 2.4).

        The repair pass strips Markdown code fences, extracts the first balanced
        ``{...}`` or ``[...]`` region, and removes trailing commas before object
        and array terminators. When no balanced region is found (for example a
        truncated response), the fence-stripped text is returned so that the
        subsequent parse fails cleanly.

        Args:
            raw: The raw model output.

        Returns:
            A repaired JSON candidate string.
        """

        text = raw.strip()

        # Strip Markdown code fences such as ```json ... ``` (or bare ``` ... ```).
        fence = re.search(r"```(?:[a-zA-Z0-9_-]*)?\s*(.*?)\s*```", text, re.DOTALL)
        if fence is not None:
            text = fence.group(1).strip()
        else:
            # Remove a leading/trailing fence that was not balanced by the regex.
            text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()

        extracted = LLMService._extract_balanced(text)
        if extracted is not None:
            text = extracted

        # Remove trailing commas before a closing brace/bracket: ``{"a":1,}`` etc.
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        return text.strip()

    @staticmethod
    def _extract_balanced(text: str) -> str | None:
        """Extract the first balanced ``{...}`` or ``[...]`` region from ``text``.

        The scan is string-aware: braces and brackets inside JSON string
        literals (and escaped quotes) are ignored. Returns ``None`` when there
        is no opening delimiter or no matching close (for example truncated
        output).

        Args:
            text: The candidate text to scan.

        Returns:
            The balanced substring, or ``None`` if none is found.
        """

        start = None
        for i, ch in enumerate(text):
            if ch in "{[":
                start = i
                break
        if start is None:
            return None

        open_ch = text[start]
        close_ch = "}" if open_ch == "{" else "]"
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return None
