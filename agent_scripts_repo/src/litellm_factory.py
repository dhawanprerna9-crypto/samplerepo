"""
LiteLLM Factory — unified LLM interface for generated agents.
Supports both Proxy Gateway routing and direct DB-based model resolution
with external secret store integration (AWS, Azure, GCP).
"""

import os
import logging
import json
from typing import Optional, Dict, Any, List
from configparser import ConfigParser
from pathlib import Path

# Skip remote model-cost-map fetch — avoids timeout warnings in air-gapped or
# network-restricted deployments. LiteLLM falls back to its bundled local copy.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.callbacks import CallbackManagerForLLMRun, AsyncCallbackManagerForLLMRun
from pydantic import Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load configuration from llmConfig.ini
# ---------------------------------------------------------------------------
_config = ConfigParser(inline_comment_prefixes=("#", ";"))
_config_path = Path(__file__).parent.parent / "llmConfig.ini"

USE_PROXY = False
PROXY_URL = ""
PROXY_KEY = ""
PROXY_MODEL = ""
DEFAULT_TIMEOUT = 600
NUM_RETRIES = 3
GUARDRAILS = []
DATABASE_ENABLED = False

if _config_path.exists():
    _config.read(_config_path)
    USE_PROXY = _config.getboolean("litellm_gateway", "use_proxy", fallback=False)
    PROXY_URL = _config.get("litellm_gateway", "proxy_url", fallback="")
    PROXY_KEY = _config.get("litellm_gateway", "key", fallback="")
    PROXY_MODEL = _config.get("litellm_gateway", "model", fallback="gpt-4.1")
    DEFAULT_TIMEOUT = _config.getfloat("appsettings", "timeout_seconds", fallback=600)
    NUM_RETRIES = _config.getint("appsettings", "num_retries", fallback=3)
    GUARDRAILS = [g.strip() for g in _config.get("litellm_gateway", "guardrails", fallback="").split(",") if g.strip()] if _config.has_option("litellm_gateway", "guardrails") else []
    DATABASE_ENABLED = _config.getboolean("appsettings", "database_enabled", fallback=False)

# ---------------------------------------------------------------------------
# Configure LiteLLM defaults
# ---------------------------------------------------------------------------
def configure_litellm():
    litellm.num_retries = NUM_RETRIES
    litellm.request_timeout = DEFAULT_TIMEOUT


configure_litellm()


# ---------------------------------------------------------------------------
# LiteLLMChat — LangChain-compatible chat model backed by litellm.completion
# ---------------------------------------------------------------------------
class LiteLLMChat(BaseChatModel):
    model: str = Field(description="LiteLLM model string")
    temperature: float = Field(default=0.7)
    max_tokens: Optional[int] = Field(default=None)
    api_key: Optional[str] = Field(default=None)
    api_base: Optional[str] = Field(default=None)
    api_version: Optional[str] = Field(default=None)
    fallbacks: Optional[List[str]] = Field(default=None)
    num_retries: Optional[int] = Field(default=None)
    timeout: Optional[float] = Field(default=None)
    guardrails: Optional[List[str]] = Field(default=None, description="List of LiteLLM guardrail names to apply to the request")
    extra_params: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        arbitrary_types_allowed = True

    def _convert_message_to_dict(self, message: BaseMessage) -> Dict[str, Any]:
        if isinstance(message, HumanMessage):
            return {"role": "user", "content": message.content}
        elif isinstance(message, AIMessage):
            msg_dict = {"role": "assistant", "content": message.content}
            if hasattr(message, "tool_calls") and message.tool_calls:
                import json as json_module
                converted = []
                for tc in message.tool_calls:
                    if isinstance(tc, dict):
                        tool_call = {"id": tc.get("id", ""), "type": "function", "function": {"name": tc.get("name", ""), "arguments": tc.get("args", {})}}
                    else:
                        tool_call = {"id": getattr(tc, "id", ""), "type": "function", "function": {"name": getattr(tc, "name", ""), "arguments": getattr(tc, "args", {})}}
                    if isinstance(tool_call["function"]["arguments"], dict):
                        tool_call["function"]["arguments"] = json_module.dumps(tool_call["function"]["arguments"])
                    converted.append(tool_call)
                msg_dict["tool_calls"] = converted
            return msg_dict
        elif isinstance(message, SystemMessage):
            return {"role": "system", "content": message.content}
        elif isinstance(message, ToolMessage):
            return {"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id, "name": message.name}
        else:
            return {"role": "user", "content": str(message.content)}

    def _convert_messages(self, messages: List[BaseMessage]) -> List[Dict[str, Any]]:
        return [self._convert_message_to_dict(msg) for msg in messages]

    def _build_params(self, **kwargs) -> Dict[str, Any]:
        params = {"model": self.model, "temperature": kwargs.get("temperature", self.temperature)}
        if self.api_base:
            params["api_base"] = self.api_base
        if self.api_key:
            params["api_key"] = self.api_key
        if self.max_tokens:
            params["max_tokens"] = kwargs.get("max_tokens", self.max_tokens)
        if self.api_version:
            params["api_version"] = self.api_version
        if self.fallbacks:
            params["fallbacks"] = self.fallbacks
        if self.num_retries is not None:
            params["num_retries"] = self.num_retries
        if self.timeout is not None:
            params["timeout"] = self.timeout
        
        # Guardrails are a LiteLLM *proxy* feature (resolved by name from the
        # proxy's config.yaml). They are only sent when routing through the
        # proxy; direct SDK calls to Azure/Bedrock/etc. never use them.
        if USE_PROXY:
            effective_guardrails = self.guardrails or GUARDRAILS
            if effective_guardrails:
                params.setdefault("extra_body", {})["guardrails"] = effective_guardrails
        
        params.update(self.extra_params)
        params.update(kwargs)
        if "tool_choice" in params and isinstance(params["tool_choice"], dict):
            if params["tool_choice"].get("type") == "tool_call":
                params["tool_choice"]["type"] = "function"
        return params

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        litellm_messages = self._convert_messages(messages)
        params = self._build_params(**kwargs)
        params["messages"] = litellm_messages
        if stop:
            params["stop"] = stop

        response = litellm.completion(**params)

        choice = response.choices[0]
        message_content = choice.message.content or ""
        ai_message = AIMessage(content=message_content)

        if hasattr(choice.message, "tool_calls") and choice.message.tool_calls:
            import json as json_module
            tool_calls = []
            for tc in choice.message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json_module.loads(args)
                    except Exception:
                        args = {}
                elif not isinstance(args, dict):
                    args = {}
                tool_calls.append({"name": tc.function.name, "args": args, "id": tc.id})
            ai_message.tool_calls = tool_calls

        generation_info = {"finish_reason": choice.finish_reason, "model": response.model}
        if hasattr(response, "usage"):
            generation_info["token_usage"] = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return ChatResult(generations=[ChatGeneration(message=ai_message, generation_info=generation_info)])

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        litellm_messages = self._convert_messages(messages)
        params = self._build_params(**kwargs)
        params["messages"] = litellm_messages
        if stop:
            params["stop"] = stop

        response = await litellm.acompletion(**params)

        choice = response.choices[0]
        message_content = choice.message.content or ""

        import json as json_module
        tool_calls = []
        if hasattr(choice.message, "tool_calls") and choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    try:
                        args = json_module.loads(args)
                    except Exception:
                        args = {}
                elif not isinstance(args, dict):
                    args = {}
                tool_calls.append({"name": tc.function.name, "args": args, "id": tc.id, "type": "tool_call"})

        ai_message = AIMessage(content=message_content, tool_calls=tool_calls)

        generation_info = {"finish_reason": choice.finish_reason, "model": response.model}
        if hasattr(response, "usage"):
            generation_info["token_usage"] = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return ChatResult(generations=[ChatGeneration(message=ai_message, generation_info=generation_info)])

    @property
    def _llm_type(self) -> str:
        return "litellm-native"

    def bind_tools(self, tools: List[Any], **kwargs) -> "LiteLLMChat":
        from langchain_core.utils.function_calling import convert_to_openai_tool
        tool_dicts = [convert_to_openai_tool(tool) for tool in tools]
        for td in tool_dicts:
            if "type" not in td or td["type"] != "function":
                td["type"] = "function"
        extra_params = self.extra_params.copy()
        extra_params["tools"] = tool_dicts
        if "tool_choice" in kwargs:
            tc = kwargs.pop("tool_choice")
            if isinstance(tc, dict) and tc.get("type") == "tool_call":
                tc["type"] = "function"
            extra_params["tool_choice"] = tc
        extra_params.update(kwargs)
        return LiteLLMChat(
            model=self.model, temperature=self.temperature, max_tokens=self.max_tokens,
            api_key=self.api_key, api_base=self.api_base, api_version=self.api_version,
            fallbacks=self.fallbacks, num_retries=self.num_retries, timeout=self.timeout,
            guardrails=self.guardrails,
            extra_params=extra_params,
        )


# ---------------------------------------------------------------------------
# LiteLLMFactory — supports proxy, DB-based, and direct modes
# ---------------------------------------------------------------------------
class LiteLLMFactory:
    """Factory class to create LiteLLM instances from configuration."""

    @staticmethod
    def _build_model_string_from_details(details: Any) -> str:
        """Convert an AIModelDetails record into the LiteLLM model string."""
        provider = (details.provider or "").lower().replace(" ", "")
        deployment = details.deployment_name or details.model_name
        if provider in ("azure", "azureopenai"):
            return f"azure/{deployment}"
        elif provider in ("bedrock", "awsbedrock", "amazonbedrock"):
            return f"bedrock/{deployment}"
        elif provider in ("vertex", "vertexai", "gcpvertex", "googlevertex"):
            return f"vertex_ai/{deployment}"
        else:
            return deployment

    @staticmethod
    def create_from_db_details(
        details: Any,
        temperature: Optional[float] = None,
        fallbacks: Optional[List[str]] = None,
        num_retries: Optional[int] = None,
        timeout: Optional[float] = None,
        guardrails: Optional[List[str]] = None,
    ) -> "LiteLLMChat":
        """Create LiteLLMChat from AIModelDetails (DB record) with secret resolution."""
        from .db_utils import fetch_ai_model_details, resolve_secrets_for_model

        # Resolve secrets from external store if configured
        details = resolve_secrets_for_model(details)
        api_key: Optional[str] = details.api_key
        model_str = LiteLLMFactory._build_model_string_from_details(details)

        # Build endpoint fallbacks from other active endpoints for the same model
        all_endpoints = fetch_ai_model_details(details.model_name)
        endpoint_fallbacks: List[str] = [
            LiteLLMFactory._build_model_string_from_details(ep)
            for ep in all_endpoints
            if ep is not details
        ]

        # Build model-level fallbacks
        model_fallbacks: List[str] = []
        if fallbacks:
            for fb in fallbacks:
                fb_endpoints = fetch_ai_model_details(fb)
                if fb_endpoints:
                    model_fallbacks.append(
                        LiteLLMFactory._build_model_string_from_details(fb_endpoints[0])
                    )
                else:
                    model_fallbacks.append(fb)

        resolved_fallbacks: Optional[List[str]] = (
            endpoint_fallbacks + model_fallbacks
        ) or None

        temp = temperature if temperature is not None else 0.7
        logger.info(
            f"[LiteLLM Factory] Direct DB connection | model='{model_str}'"
            f" | endpoint={details.endpoint} | fallbacks={resolved_fallbacks}"
        )
        return LiteLLMChat(
            model=model_str,
            temperature=temp,
            api_key=api_key,
            api_base=details.endpoint,
            api_version=details.api_version,
            fallbacks=resolved_fallbacks,
            num_retries=num_retries,
            timeout=timeout,
            guardrails=guardrails,
        )


# ---------------------------------------------------------------------------
# Factory function — single entry point for all agents
# ---------------------------------------------------------------------------
def create_llm(
    model_name: Optional[str] = None,
    agent_name: Optional[str] = None,
    use_case: str = "default",
    temperature: Optional[float] = None,
    fallbacks: Optional[List[str]] = None,
    num_retries: Optional[int] = None,
    timeout: Optional[float] = None,
    guardrails: Optional[List[str]] = None,
) -> LiteLLMChat:
    """
    Create an LLM instance.

    Resolution order:
      1. If agent_name is given, look up primary model + fallbacks from AIModelConfig DB table.
      2. If use_proxy=true in llmConfig.ini, route through LiteLLM proxy gateway.
      3. Otherwise, resolve model from AIModelDetails DB table (direct connection with secrets).

    Args:
        model_name: Model name override
        agent_name: Agent name for DB model routing lookup
        use_case: Use case identifier for DB lookup (default: 'default')
        temperature: Temperature for generation
        fallbacks: List of fallback models
        num_retries: Number of retries for failed requests
        timeout: Request timeout in seconds
        guardrails: List of LiteLLM guardrail names to apply (proxy mode only)
    """
    logger.info(
        f"[LiteLLM Factory] Creating LLM | agent_name={agent_name}"
        f" | use_case={use_case} | model_name={model_name}"
    )

    # Step 1: DB-based agent→model routing
    if agent_name and DATABASE_ENABLED:
        try:
            from .db_utils import fetch_ai_model_config
            db_primary, db_fallbacks = fetch_ai_model_config(agent_name, use_case)
        except Exception as _e:
            logger.warning(f"DB model config lookup failed: {_e}")
            db_primary, db_fallbacks = None, []

        if db_primary:
            logger.info(
                f"[LiteLLM Factory] DB model config | agent='{agent_name}'"
                f" | primary='{db_primary}' fallbacks={db_fallbacks}"
            )
            # Override model_name with DB-configured primary
            model_name = db_primary
            if db_fallbacks and not fallbacks:
                fallbacks = db_fallbacks

        resolved_model = model_name or PROXY_MODEL
        logger.info(f"Step 1: Database Enabled Route -Routing via DB| model={resolved_model} | proxy_url={PROXY_URL}")
    else:
        resolved_model = PROXY_MODEL
        logger.info(f"Step 1: Database Disabled Route -Routing via PROXY| model={resolved_model} | proxy_url={PROXY_URL}")

    # Step 2: Proxy mode
    if USE_PROXY:
        proxy_api_key = PROXY_KEY or os.getenv("LITELLM_PROXY_API_KEY", "")
        if not proxy_api_key:
            raise ValueError("Proxy key not set in llmConfig.ini or LITELLM_PROXY_API_KEY env var")

        logger.info(f" Step 2: Routing via LiteLLM proxy | model={resolved_model} | proxy_url={PROXY_URL}")
        return LiteLLMChat(
            model=resolved_model,
            temperature=temperature if temperature is not None else 0.7,
            api_key=proxy_api_key,
            api_base=PROXY_URL,
            fallbacks=fallbacks,
            num_retries=num_retries,
            timeout=timeout,
            guardrails=guardrails,
        )

    # Step 3: Direct DB-based connection with secret resolution
    details = None
    if DATABASE_ENABLED:
        try:
           logger.info(f"Step 3: Database Enabled Route -Routing via DB| model={resolved_model}")
           from .db_utils import fetch_ai_model_details
           all_details = fetch_ai_model_details(resolved_model)
           details = all_details[0] if all_details else None
        except Exception as _e:
            logger.warning(f"AIModelDetails fetch failed: {_e}")
            details = None

    if details:
        logger.info(
            f"[LiteLLM Factory] Using AIModelDetails for model='{resolved_model}'"
            f" | provider={details.provider} | deployment={details.deployment_name}"
        )
        return LiteLLMFactory.create_from_db_details(
            details=details,
            temperature=temperature,
            fallbacks=fallbacks,
            num_retries=num_retries,
            timeout=timeout,
            guardrails=guardrails,
        )

    # Step 4: Fallback — direct call with model name only
    logger.info(f"[LiteLLM Factory] Step 4 : No DB config found, direct call | model={resolved_model}")
    return LiteLLMChat(
        model=resolved_model,
        temperature=temperature if temperature is not None else 0.7,
        fallbacks=fallbacks,
        api_key=PROXY_KEY,
        api_base=PROXY_URL,
        num_retries=num_retries,
        timeout=timeout,
        guardrails=guardrails,
    )
