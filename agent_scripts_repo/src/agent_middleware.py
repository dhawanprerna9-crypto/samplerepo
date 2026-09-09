import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping

from agent_framework import ChatContext, FunctionInvocationContext

from src.observability import get_observability


logger = logging.getLogger("agent_middleware")


# Tracing is delegated to the provider-agnostic observability layer. Spans opened
# via ``obs.start_span`` nest automatically through the OpenTelemetry context, so
# agent and tool invocations form a single connected graph per request.
obs = get_observability()


def set_trace_context(trace_id, span_id):
    """Backward-compatible no-op.

    Trace context is now propagated through the OpenTelemetry context rather than
    a manual contextvar. Retained so existing entrypoints keep importing cleanly.
    """
    return None


def get_trace_context():
    return {}


def _truncate_text(value: object, *, max_len: int = 240) -> str:
    text = str(value)
    if len(text) <= max_len:
        return text
    return f"{text[:max_len - 3]}..."


def _serialize_arguments(arguments: object) -> str:
    try:
        if hasattr(arguments, "model_dump"):
            payload = arguments.model_dump()
        elif isinstance(arguments, Mapping):
            payload = dict(arguments)
        else:
            payload = arguments
        return _truncate_text(json.dumps(payload, default=str))
    except Exception:
        return _truncate_text(repr(arguments))


def build_logging_exception_middlewares(agent_name: str) -> list[Callable[..., Awaitable[None]]]:
    async def chat_logging_middleware(
        context: ChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        messages = getattr(context, "messages", []) or []
        stream_mode = bool(getattr(context, "stream", False))
        started_at = time.perf_counter()

        logger.info(
            "AGENT_CALL_START agent=%s stream=%s message_count=%s",
            agent_name,
            stream_mode,
            len(messages),
        )

        with obs.start_span(
            f"agent:{agent_name}", input={"messages": _truncate_text(messages)}
        ) as span:
            try:
                await call_next()
            except Exception as exc:
                logger.exception(
                    "AGENT_CALL_ERROR agent=%s error_type=%s error=%s",
                    agent_name,
                    type(exc).__name__,
                    exc,
                )
                span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
                raise

            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            result = getattr(context, "result", None)
            logger.info(
                "AGENT_CALL_END agent=%s duration_ms=%s result_type=%s",
                agent_name,
                elapsed_ms,
                type(result).__name__ if result is not None else "None",
            )
            if result is not None:
                span.update(output=_truncate_text(result))

    async def tool_exception_logging_middleware(
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        function_name = getattr(getattr(context, "function", None), "name", "unknown-tool")
        args_preview = _serialize_arguments(getattr(context, "arguments", None))
        started_at = time.perf_counter()

        logger.info(
            "TOOL_CALL_START agent=%s tool=%s args=%s",
            agent_name,
            function_name,
            args_preview,
        )

        with obs.start_span(
            f"tool:{function_name}", input={"args": args_preview}
        ) as span:
            try:
                await call_next()
                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                result_preview = _truncate_text(getattr(context, "result", ""))
                logger.info(
                    "TOOL_CALL_END agent=%s tool=%s duration_ms=%s result=%s",
                    agent_name,
                    function_name,
                    elapsed_ms,
                    result_preview,
                )
                result = getattr(context, "result", None)
                if result is not None:
                    span.update(output=_truncate_text(result))
            except TimeoutError as exc:
                logger.exception(
                    "TOOL_CALL_TIMEOUT agent=%s tool=%s error=%s",
                    agent_name,
                    function_name,
                    exc,
                )
                context.result = (
                    "Sorry for the inconvenience, this action timed out. "
                    "Please try again in a moment."
                )
                span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                logger.exception(
                    "TOOL_CALL_ERROR agent=%s tool=%s error_type=%s error=%s",
                    agent_name,
                    function_name,
                    type(exc).__name__,
                    exc,
                )
                context.result = (
                    f"An internal error occurred while executing '{function_name}'. "
                    "Please try again."
                )
                span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")

    return [chat_logging_middleware, tool_exception_logging_middleware]
