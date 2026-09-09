"""LiteLLM-backed chat client for the Microsoft Agent Framework (MAF).

Routes agent LLM calls through ``litellm.acompletion`` using the connection that
``src.litellm_factory.create_llm`` resolves (LiteLLM proxy *or* direct DB-based
provider, with guardrails / fallbacks / retries), instead of talking to a LiteLLM
proxy through the OpenAI *Responses* API.

Why this client exists
----------------------
- ``agent_framework.openai.OpenAIChatClient`` (the previous default) targets the
  OpenAI **Responses** API (``POST /responses``). A LiteLLM proxy does not always
  expose that endpoint, so agent calls could fail even when the proxy is healthy.
- ``OpenAIChatCompletionClient`` targets the **Chat Completions** API
  (``POST /chat/completions``) — the exact wire format ``litellm`` also speaks.
  We subclass it to inherit, unchanged, all of its request/response conversion,
  the tool-calling loop, middleware and telemetry, and override **only the
  transport** so the actual call goes through ``litellm.acompletion``.
- Because the connection is resolved by ``create_llm`` / ``LiteLLMChat``, routing
  behaviour (proxy vs. direct, guardrails, fallbacks, retries, timeouts) stays
  identical to the rest of the generated project (e.g. the tool gateway).

This file is MAF-specific. It does not modify ``litellm_factory.py`` (which is
shared with the LangGraph and ADK generators) — it only consumes it.
"""

from __future__ import annotations

import os

from collections.abc import Awaitable, Mapping, Sequence
from typing import Any

# Skip remote model-cost-map fetch — avoids timeout warnings in air-gapped deployments.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm
from agent_framework import ChatResponse, ChatResponseUpdate, Message, ResponseStream
from agent_framework.openai import OpenAIChatCompletionClient
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk

from src.litellm_factory import LiteLLMChat, create_llm


class LiteLLMChatClient(OpenAIChatCompletionClient):
    """Chat-Completions-compatible ``BaseChatClient`` whose transport is LiteLLM.

    Everything about request/response shaping (message conversion, tool schema,
    ``tool_choice`` handling, streaming updates, the function-calling loop,
    middleware, telemetry) is inherited from ``OpenAIChatCompletionClient``.
    Only ``_inner_get_response`` is overridden so the HTTP call is made by
    ``litellm.acompletion`` using the connection resolved by ``create_llm``.
    """

    def __init__(
        self,
        llm: LiteLLMChat | None = None,
        *,
        agent_name: str | None = None,
    ) -> None:
        """Build the client from a resolved ``LiteLLMChat`` connection.

        Args:
            llm: A pre-resolved ``LiteLLMChat``. When omitted, ``create_llm`` is
                called (optionally with ``agent_name`` for DB-based per-agent
                model routing).
            agent_name: Optional agent name used for ``AIModelConfig`` routing when
                ``llm`` is not supplied.
        """
        self._litellm: LiteLLMChat = llm or create_llm(agent_name=agent_name)

        # Initialise the OpenAI-compatible base with the resolved model. The
        # AsyncOpenAI client constructed here is never used for transport (we
        # override ``_inner_get_response``), but ``api_key`` / ``base_url`` must be
        # present so construction succeeds and ``self.model`` is populated for the
        # inherited option-preparation logic.
        super().__init__(
            model=self._litellm.model,
            api_key=(self._litellm.api_key or "litellm-managed"),
            base_url=(self._litellm.api_base or "https://litellm.invalid/v1"),
        )

    def _litellm_transport_params(self) -> dict[str, Any]:
        """Assemble the LiteLLM connection kwargs (``api_base``, ``api_key``,
        ``api_version``, ``fallbacks``, retries/timeout, and proxy ``guardrails``
        via ``extra_body``).

        Reuses ``LiteLLMChat._build_params`` so the exact same routing/guardrail
        rules as the rest of the project apply. ``messages`` / ``tools`` are never
        produced here; they are layered on from the inherited OpenAI option prep.
        """
        params = self._litellm._build_params()
        params.pop("messages", None)  # defensive: _build_params never sets this
        return params

    def _inner_get_response(  # type: ignore[override]
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        stream: bool = False,
        **kwargs: Any,
    ) -> Awaitable[ChatResponse] | ResponseStream[ChatResponseUpdate, ChatResponse]:
        # Inherited conversion: builds OpenAI-format messages, tools, tool_choice,
        # model, response_format, max_completion_tokens, etc. Then merge the
        # LiteLLM transport params (agent-framework options win on any overlap).
        options_dict = self._prepare_options(messages, options)
        call_kwargs = self._litellm_transport_params()
        call_kwargs.update(options_dict)

        if stream:
            call_kwargs["stream_options"] = {"include_usage": True}

            async def _stream() -> Any:
                response = await litellm.acompletion(stream=True, **call_kwargs)
                async for chunk in response:
                    # litellm returns OpenAI-compatible chunks; normalise to the
                    # openai SDK type so the inherited parser's isinstance checks hold.
                    oai_chunk = ChatCompletionChunk.model_validate(chunk.model_dump())
                    if len(oai_chunk.choices) == 0 and oai_chunk.usage is None:
                        continue
                    yield self._parse_response_update_from_openai(oai_chunk)

            return self._build_response_stream(
                _stream(), response_format=options.get("response_format")
            )

        async def _get_response() -> ChatResponse:
            response = await litellm.acompletion(stream=False, **call_kwargs)
            oai_response = ChatCompletion.model_validate(response.model_dump())
            return self._parse_response_from_openai(oai_response, options)

        return _get_response()

    def service_url(self) -> str:
        return self._litellm.api_base or "litellm"
