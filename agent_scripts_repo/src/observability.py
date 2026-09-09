"""Provider-agnostic observability layer for generated agent projects.

This module exposes a single, config-driven facade (:class:`ObservabilityManager`,
reachable via :func:`get_observability`) that the generated super-agent,
orchestrator, utility agents and tools all call into for tracing and logging.

"""

from __future__ import annotations

import importlib
import json
import logging
import os
from abc import ABC, abstractmethod
from configparser import ConfigParser
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


logger = logging.getLogger("observability")


# ---------------------------------------------------------------------------
# Span abstraction
# ---------------------------------------------------------------------------
class SpanHandle(ABC):
    """A backend-agnostic handle to a single span/observation."""

    @abstractmethod
    def update(
        self,
        *,
        output: Any = None,
        metadata: Optional[dict] = None,
        level: Optional[str] = None,
        status_message: Optional[str] = None,
    ) -> None:
        ...

    @abstractmethod
    def end(self) -> None:
        ...


class _NoOpSpan(SpanHandle):
    def update(self, **_kwargs: Any) -> None:  # noqa: D401 - no-op
        return None

    def end(self) -> None:
        return None


class ManagedSpan(SpanHandle):
    """Fans span operations out to every backing provider span."""

    def __init__(self, handles: list[SpanHandle]):
        self._handles = handles

    def update(self, **kwargs: Any) -> None:
        for handle in self._handles:
            try:
                handle.update(**kwargs)
            except Exception:  # pragma: no cover - tracing must never break flow
                logger.debug("span.update failed", exc_info=True)

    def end(self) -> None:
        for handle in self._handles:
            try:
                handle.end()
            except Exception:  # pragma: no cover
                logger.debug("span.end failed", exc_info=True)


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------
class ObservabilityProvider(ABC):
    """Reference contract for a single observability backend.

    The built-in :class:`NoOpProvider` subclasses this. External backends live in
    the ``observability_providers`` package as standalone modules, each exposing a
    ``Provider`` class implementing the same methods (duck-typed — they need not
    import or subclass this base). See ``observability_providers/__init__.py`` and
    ``observability_providers/langfuse.py`` for the protocol and a worked example.
    """

    name: str = "base"

    def __init__(self, settings: dict[str, str], service_name: str):
        self.settings = settings
        self.service_name = service_name

    @abstractmethod
    def setup(self) -> None:
        """Initialise the client/SDK and any env vars or exporters it needs."""

    @abstractmethod
    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        input: Any = None,
        metadata: Optional[dict] = None,
    ) -> Iterator[SpanHandle]:
        """Open a span as the *current* span so nested calls attach to it."""

    @abstractmethod
    @contextmanager
    def start_generation(
        self,
        name: str,
        *,
        model: Optional[str] = None,
        input: Any = None,
        model_parameters: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ) -> Iterator[SpanHandle]:
        """Open an LLM *generation* observation (captures model, token usage, cost).

        The returned handle accepts the same ``update`` call as a span plus an
        optional ``usage`` mapping (e.g. ``{"input": .., "output": .., "total": ..}``)
        and ``model``/``cost`` details. Backends without a dedicated generation type
        may treat this as a normal span.
        """

    @abstractmethod
    def update_trace(
        self,
        *,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        input: Any = None,
        output: Any = None,
        metadata: Optional[dict] = None,
        tags: Optional[list[str]] = None,
    ) -> None:
        """Set trace-level attributes (session id, final output, etc.)."""

    @abstractmethod
    def inject_context(self, carrier: dict) -> dict:
        """Write the current trace context into ``carrier`` (W3C traceparent)."""

    @abstractmethod
    @contextmanager
    def use_remote_context(self, carrier: dict) -> Iterator[None]:
        """Re-attach a trace context extracted from an upstream ``carrier``."""

    @abstractmethod
    def flush(self) -> None:
        """Flush any buffered spans to the backend."""


# ---------------------------------------------------------------------------
# No-op provider (used when tracing is disabled or a backend fails to load)
# ---------------------------------------------------------------------------
class NoOpProvider(ObservabilityProvider):
    name = "noop"

    def setup(self) -> None:
        return None

    @contextmanager
    def start_span(self, name, *, input=None, metadata=None):
        yield _NoOpSpan()

    @contextmanager
    def start_generation(self, name, *, model=None, input=None, model_parameters=None, metadata=None):
        yield _NoOpSpan()

    def update_trace(self, **_kwargs: Any) -> None:
        return None

    def inject_context(self, carrier: dict) -> dict:
        return carrier

    @contextmanager
    def use_remote_context(self, carrier: dict):
        yield

    def flush(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Provider plugins
# ---------------------------------------------------------------------------
# Concrete providers live in the ``observability_providers`` package \u2014 one module
# per backend (e.g. ``observability_providers/langfuse.py``), each exposing a
# ``Provider`` class. They are imported lazily by name (matching the config
# ``providers`` entry), so adding a backend (langsmith, phoenix/"lizer", ...) is
# just: drop a new module in that package and list it under
# ``[observability] providers`` in config.ini \u2014 no change to this file is needed.
#
# Optional aliases map a config provider name to a different module filename
# (e.g. ``{"lizer": "phoenix"}``).
_PROVIDER_MODULE_ALIASES: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Manager — the single facade the generated code talks to
# ---------------------------------------------------------------------------
class ObservabilityManager:
    """Loads enabled providers from config and fans out tracing calls to them."""

    def __init__(self, config_path: Optional[str | Path] = None):
        self._providers: list[ObservabilityProvider] = []
        self._service_name = "agent"
        self._load(config_path)

    # -- configuration -----------------------------------------------------
    def _load(self, config_path: Optional[str | Path]) -> None:
        config = ConfigParser(inline_comment_prefixes=("#", ";"))
        path = self._discover_config(config_path)
        if path is not None and path.exists():
            config.read(path)

        requested = self._resolve_provider_names(config)
        for name in requested:
            settings = self._provider_settings(config, name)
            if not _as_bool(settings.get("enabled", "true")):
                continue
            provider = self._load_provider(name, settings)
            if provider is not None:
                self._providers.append(provider)

        if not self._providers:
            logger.info("Observability disabled — using no-op provider.")
            self._providers.append(NoOpProvider({}, self._service_name))

    def _load_provider(self, name: str, settings: dict[str, str]) -> Optional[ObservabilityProvider]:
        """Import ``observability_providers.<name>`` and instantiate its ``Provider``
        class. Returns ``None`` (and logs) if the module is missing, malformed, or
        fails to initialise — tracing must never break agent flow.
        """
        module_name = self._provider_module_name(name)
        try:
            module = importlib.import_module(module_name)
        except Exception:
            logger.warning(
                "Observability provider module '%s' not found for '%s'; skipping.",
                module_name, name, exc_info=True,
            )
            return None
        provider_cls = getattr(module, "Provider", None)
        if provider_cls is None:
            logger.warning("Provider module '%s' defines no 'Provider' class; skipping '%s'.", module_name, name)
            return None
        try:
            provider = provider_cls(settings, self._service_name)
            provider.setup()
            return provider
        except Exception:
            logger.warning("Failed to initialise provider '%s'; skipping.", name, exc_info=True)
            return None

    @staticmethod
    def _provider_module_name(name: str) -> str:
        """Resolve a provider module's import path, tolerating both flat and ``src/``
        layouts (``observability_providers.x`` vs ``src.observability_providers.x``)."""
        module = _PROVIDER_MODULE_ALIASES.get(name, name)
        package = __name__.rpartition(".")[0]
        base = package + ".observability_providers" if package else "observability_providers"
        return base + "." + module

    @staticmethod
    def _discover_config(config_path: Optional[str | Path]) -> Optional[Path]:
        """Locate config.ini, tolerating different project layouts.

        ``observability.py`` may sit in ``src/`` (config one level up) or at the
        project root (config alongside it), so a few candidate paths are tried.
        """
        if config_path:
            return Path(config_path)
        here = Path(__file__).resolve()
        candidates = [
            here.parent / "config.ini",
            here.parent.parent / "config.ini",
            here.parent.parent.parent / "config.ini",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def _resolve_provider_names(self, config: ConfigParser) -> list[str]:
        if config.has_section("observability"):
            if not config.getboolean("observability", "enabled", fallback=True):
                return []
            self._service_name = config.get("observability", "service_name", fallback="agent")
            raw = config.get("observability", "providers", fallback="langfuse")
            return [p.strip().lower() for p in raw.split(",") if p.strip()]
        # Legacy fallback: a bare [langfuse] section.
        if config.has_section("langfuse") and config.getboolean("langfuse", "enabled", fallback=False):
            return ["langfuse"]
        return []

    def _provider_settings(self, config: ConfigParser, name: str) -> dict[str, str]:
        section = f"observability.{name}"
        if config.has_section(section):
            settings = dict(config.items(section))
        elif name == "langfuse" and config.has_section("langfuse"):
            settings = dict(config.items("langfuse"))
        else:
            settings = {}

        if name == "langfuse":
            logger.debug("[observability] Attempting to overlay Langfuse keys from secret manager.")
            try:
                from src.app_settings import settings as app_settings
                kv_public = app_settings.get("langfuse-publickey")
                kv_secret = app_settings.get("langfuse-secretkey")
                if kv_public:
                    settings["public_key"] = kv_public
                    logger.info("[observability] Langfuse public_key loaded from secret manager (key=langfuse-publickey).")
                else:
                    logger.warning("[observability] Langfuse public_key (langfuse-publickey) not found in secret manager — Langfuse tracing may fail to authenticate.")
                if kv_secret:
                    settings["secret_key"] = kv_secret
                    logger.info("[observability] Langfuse secret_key loaded from secret manager (key=langfuse-secretkey).")
                else:
                    logger.warning("[observability] Langfuse secret_key (langfuse-secretkey) not found in secret manager — Langfuse tracing may fail to authenticate.")
            except Exception:
                logger.warning("[observability] Failed to load Langfuse keys from secret manager.", exc_info=True)

        return settings

    # -- public API --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return any(not isinstance(p, NoOpProvider) for p in self._providers)

    @contextmanager
    def start_span(self, name: str, *, input: Any = None, metadata: Optional[dict] = None):
        """Open a span across all providers; nested spans attach automatically."""
        with ExitStack() as stack:
            handles: list[SpanHandle] = []
            for provider in self._providers:
                try:
                    handles.append(stack.enter_context(provider.start_span(name, input=input, metadata=metadata)))
                except Exception:
                    logger.debug("provider %s start_span failed", provider.name, exc_info=True)
            yield ManagedSpan(handles)

    @contextmanager
    def start_generation(
        self,
        name: str,
        *,
        model: Optional[str] = None,
        input: Any = None,
        model_parameters: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ):
        """Open an LLM generation across all providers (captures model/usage/cost).

        The yielded handle's ``update`` accepts ``output`` plus optional ``usage``
        (token counts) and ``model``/``cost`` details, fanned out to every backend.
        """
        with ExitStack() as stack:
            handles: list[SpanHandle] = []
            for provider in self._providers:
                try:
                    handles.append(
                        stack.enter_context(
                            provider.start_generation(
                                name,
                                model=model,
                                input=input,
                                model_parameters=model_parameters,
                                metadata=metadata,
                            )
                        )
                    )
                except Exception:
                    logger.debug("provider %s start_generation failed", provider.name, exc_info=True)
            yield ManagedSpan(handles)

    def update_trace(self, **kwargs: Any) -> None:
        for provider in self._providers:
            try:
                provider.update_trace(**kwargs)
            except Exception:
                logger.debug("provider %s update_trace failed", provider.name, exc_info=True)

    def inject_context(self) -> dict:
        """Return a carrier (e.g. ``{'traceparent': ...}``) for A2A propagation."""
        carrier: dict = {}
        for provider in self._providers:
            try:
                provider.inject_context(carrier)
            except Exception:
                logger.debug("provider %s inject_context failed", provider.name, exc_info=True)
        return carrier

    @contextmanager
    def use_remote_context(self, carrier: Optional[dict]):
        """Re-attach an upstream trace context for the duration of the block."""
        carrier = carrier or {}
        with ExitStack() as stack:
            for provider in self._providers:
                try:
                    stack.enter_context(provider.use_remote_context(carrier))
                except Exception:
                    logger.debug("provider %s use_remote_context failed", provider.name, exc_info=True)
            yield

    def flush(self) -> None:
        for provider in self._providers:
            try:
                provider.flush()
            except Exception:
                logger.debug("provider %s flush failed", provider.name, exc_info=True)


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_manager: Optional[ObservabilityManager] = None


def get_observability(config_path: Optional[str | Path] = None) -> ObservabilityManager:
    """Return the process-wide :class:`ObservabilityManager` singleton."""
    global _manager
    if _manager is None:
        _manager = ObservabilityManager(config_path)
    return _manager


def configure_logging(level: str | int = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    # Keep framework noise low unless explicitly needed for deep diagnostics.
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(
        logging.WARNING
    )
    logging.getLogger("agent_framework._workflows._validation").setLevel(
        logging.WARNING
    )
    logging.getLogger("agent_framework._workflows._runner").setLevel(logging.ERROR)


def response_to_lines(response: Any) -> list[str]:
    lines: list[str] = []

    messages = getattr(response, "messages", None)
    if messages:
        for message in messages:
            text = getattr(message, "text", None)
            if not text:
                continue
            normalized_text = " ".join(str(text).split())
            author = (
                getattr(message, "author_name", None)
                or getattr(message, "role", None)
                or "assistant"
            )
            lines.append(f"{author}: {normalized_text}")

    if lines:
        return lines

    text = getattr(response, "text", None)
    if text:
        normalized_text = " ".join(str(text).split())
        author = getattr(response, "author_name", None) or "assistant"
        return [f"{author}: {normalized_text}"]

    if hasattr(response, "model_dump"):
        try:
            payload = response.model_dump()
            return [json.dumps(payload, indent=2, default=str)]
        except Exception:
            return [str(response)]

    return [str(response)]


def update_to_line(update: Any) -> str | None:
    text = getattr(update, "text", None)
    if not text:
        return None
    normalized_text = " ".join(str(text).split())
    author = getattr(update, "author_name", None) or "assistant"
    return f"{author}: {normalized_text}"
