"""Langfuse (v3, OpenTelemetry-based) observability provider.

Loaded on demand by :class:`observability.ObservabilityManager` when ``langfuse``
appears in the ``[observability] providers`` list of ``config.ini``.

This module is fully self-contained: it implements the provider protocol expected
by the manager (``setup`` / ``start_span`` / ``update_trace`` / ``inject_context`` /
``use_remote_context`` / ``flush``) without importing the facade, so backends can
be added or removed simply by dropping a file in this package.

Langfuse v3 is built on OpenTelemetry; the legacy v2 API (``langfuse.trace()`` /
``trace.span()`` / ``trace.update()``) has been removed. We therefore use
``start_as_current_span`` (which nests via the OTEL context) and propagate across
A2A hops with the standard W3C ``traceparent`` carrier.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("observability.langfuse")


_SUPPRESSED_SPAN_PREFIXES = (
    "tools/list",           # MCP tool discovery — startup noise
    "GET /.well-known/",    # A2A card resolution HTTP spans
    "POST /.well-known/",
    "a2a.",                 # All A2A SDK framework spans
)

def _is_suppressed(span_name: str) -> bool:
    return any(span_name.startswith(p) for p in _SUPPRESSED_SPAN_PREFIXES)

_HAS_FILTERING = True
try:
    from opentelemetry.sdk.trace import SpanProcessor
    from opentelemetry.sdk.trace.export import ReadableSpan

    class _FilteringSpanProcessor(SpanProcessor):
        """Drops framework-internal spans before they reach the Langfuse exporter."""

        def __init__(self, delegate: SpanProcessor):
            self._delegate = delegate

        def on_start(self, span, parent_context=None):
            self._delegate.on_start(span, parent_context=parent_context)

        def on_end(self, span: ReadableSpan):
            if not _is_suppressed(span.name):
                self._delegate.on_end(span)

        def shutdown(self):
            self._delegate.shutdown()

        def force_flush(self, timeout_millis: int = 30000):
            return self._delegate.force_flush(timeout_millis)

except ImportError:
    _HAS_FILTERING = False


class _NoSpan:
    """Span handle returned when a span could not be started."""

    def update(self, **_kwargs: Any) -> None:
        return None

    def end(self) -> None:
        return None


class _LangfuseSpan:
    """Wraps a Langfuse OTEL span behind the manager's span-handle contract."""

    def __init__(self, span: Any):
        self._span = span

    def update(self, *, output=None, metadata=None, level=None, status_message=None, usage=None) -> None:
        kwargs: dict[str, Any] = {}
        if output is not None:
            kwargs["output"] = output
        if metadata is not None:
            kwargs["metadata"] = metadata
        if level is not None:
            kwargs["level"] = level
        if status_message is not None:
            kwargs["status_message"] = status_message
        if usage is not None:
            kwargs["usage_details"] = usage   # Langfuse v3 OTEL key (Fix 5)
        if kwargs:
            self._span.update(**kwargs)

    def end(self) -> None:
        self._span.end()


class _LangfuseGeneration:
    """Wraps a Langfuse OTEL generation, adding model/usage/cost to the update."""

    def __init__(self, generation: Any):
        self._generation = generation

    def update(self, *, output=None, usage=None, model=None, cost=None,
               metadata=None, level=None, status_message=None) -> None:
        kwargs: dict[str, Any] = {}
        if output is not None:
            kwargs["output"] = output
        if usage is not None:
            # Langfuse v3 records token counts via ``usage_details``.
            kwargs["usage_details"] = usage
        if cost is not None:
            kwargs["cost_details"] = cost
        if model is not None:
            kwargs["model"] = model
        if metadata is not None:
            kwargs["metadata"] = metadata
        if level is not None:
            kwargs["level"] = level
        if status_message is not None:
            kwargs["status_message"] = status_message
        if kwargs:
            self._generation.update(**kwargs)

    def end(self) -> None:
        self._generation.end()


class Provider:
    """Langfuse >= 3.x integration."""

    name = "langfuse"

    def __init__(self, settings: dict, service_name: str):
        self.settings = settings
        self.service_name = service_name
        self._client: Any = None

    def setup(self) -> None:
        public_key = self.settings.get("public_key", "")
        secret_key = self.settings.get("secret_key", "")
        host = self.settings.get("host", "")
        if not (public_key and secret_key):
            logger.warning("Langfuse enabled but public/secret keys missing; disabling.")
            return
        try:
            from langfuse import Langfuse
        except Exception:
            logger.warning("langfuse package not importable; Langfuse tracing disabled.")
            return
        # Surface credentials via env for the OTEL exporter.
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", public_key)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", secret_key)
        if host:
            os.environ.setdefault("LANGFUSE_HOST", host)
        try:
            self._client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host or None,
            )
            logger.info("Langfuse tracing enabled (host=%s)", host or "default")

            # Auto-instrument the OpenAI SDK so every LLM call emits a child OTEL
            # span with model name, token counts and latency
            try:
                from opentelemetry.instrumentation.openai import OpenAIInstrumentor
                OpenAIInstrumentor(enrich_assistant=True).instrument()
                logger.info("Langfuse: OpenAI OTEL instrumentation enabled (LLM call spans active).")
            except ImportError:
                logger.debug("opentelemetry-instrumentation-openai not installed; LLM call spans disabled.")
            except Exception:
                logger.debug("OpenAI OTEL instrumentation failed.", exc_info=True)

            # Wrap the active span processor with the noise filter
            if _HAS_FILTERING:
                try:
                    from opentelemetry import trace as otel_trace
                    provider = otel_trace.get_tracer_provider()
                    if hasattr(provider, "_active_span_processor"):
                        raw = provider._active_span_processor
                        provider._active_span_processor = _FilteringSpanProcessor(raw)
                        logger.info("Langfuse: framework-noise span filter installed.")
                except Exception:
                    logger.debug("Could not install span filter.", exc_info=True)

        except Exception:
            logger.warning("Langfuse client initialisation failed; tracing disabled.", exc_info=True)
            self._client = None

    @contextmanager
    def start_span(self, name, *, input=None, metadata=None):
        if self._client is None:
            yield _NoSpan()
            return
        try:
            with self._client.start_as_current_span(
                name=name,
                input=input,
                metadata=metadata,
            ) as span:
                yield _LangfuseSpan(span)
        except Exception:
            logger.debug("Langfuse start_span failed", exc_info=True)
            yield _NoSpan()

    @contextmanager
    def start_generation(self, name, *, model=None, input=None, model_parameters=None, metadata=None):
        if self._client is None:
            yield _NoSpan()
            return
        try:
            kwargs: dict[str, Any] = {"name": name, "input": input}
            if model is not None:
                kwargs["model"] = model
            if model_parameters is not None:
                kwargs["model_parameters"] = model_parameters
            if metadata is not None:
                kwargs["metadata"] = metadata
            with self._client.start_as_current_generation(**kwargs) as generation:
                yield _LangfuseGeneration(generation)
        except Exception:
            logger.debug("Langfuse start_generation failed", exc_info=True)
            yield _NoSpan()

    def update_trace(self, *, session_id=None, user_id=None, input=None, output=None, metadata=None, tags=None, usage=None) -> None:
        if self._client is None:
            return
        kwargs: dict[str, Any] = {}
        if session_id is not None:
            kwargs["session_id"] = session_id
        if user_id is not None:
            kwargs["user_id"] = user_id
        if input is not None:
            kwargs["input"] = input
        if output is not None:
            kwargs["output"] = output
        if metadata is not None:
            kwargs["metadata"] = metadata
        if tags is not None:
            kwargs["tags"] = tags
        if usage is not None:
            kwargs["usage_details"] = usage
        if not kwargs:
            return
        try:
            self._client.update_current_trace(**kwargs)
        except Exception:
            logger.debug("Langfuse update_current_trace failed", exc_info=True)

    def inject_context(self, carrier: dict) -> dict:
        try:
            from opentelemetry.propagate import inject

            inject(carrier)
        except Exception:
            logger.debug("traceparent inject failed", exc_info=True)
        return carrier

    @contextmanager
    def use_remote_context(self, carrier: dict):
        token = None
        try:
            from opentelemetry import context as otel_context
            from opentelemetry.propagate import extract

            parent = extract(carrier)
            token = otel_context.attach(parent)
        except Exception:
            logger.debug("traceparent extract failed", exc_info=True)
            token = None
        try:
            yield
        finally:
            if token is not None:
                try:
                    from opentelemetry import context as otel_context

                    otel_context.detach(token)
                except Exception:
                    logger.debug("traceparent detach failed", exc_info=True)

    def flush(self) -> None:
        if self._client is not None:
            try:
                self._client.flush()
            except Exception:
                logger.debug("Langfuse flush failed", exc_info=True)
