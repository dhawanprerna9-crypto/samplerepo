"""
llm_client.py
-------------
LLM API client responsible for service resolution and tool identification.

Uses src.litellm_factory.create_llm() — the same factory the rest of the
deployed agents use. Passes `agent_name` through so the caller's
AIModelConfig DB routing is preserved (just like a super agent's own LLM).
"""

import json
import logging
import asyncio
from typing import Optional

logger = logging.getLogger("tool_gateway.llm_client")


# ─────────────────────────────────────────────
# System prompts
# ─────────────────────────────────────────────

_SYSTEM_PROMPT_SERVICE = """You are a service selection engine.

You are given:
- A list of services
- Each service contains tools and tool descriptions
- A user query

Your job:

1. Select the most appropriate service for the user query.
2. Use tool descriptions inside the service to determine if the service is relevant.
3. If multiple services seem relevant, choose the best one.
4. If none are relevant, return an empty JSON object.

Return ONLY valid JSON:

{
"service_name": "name"
}

Do NOT return explanation.
"""

_SYSTEM_PROMPT_TOOL = """
    You are a tool selection and argument generation engine.

    You are given:
    - A list of tools with descriptions
    - Tool argument schemas
    - Optional service-level instructions describing API rules
    - Optional tool-level instructions describing usage rules
    - A user query

    Your job:

    1. Select the single most appropriate tool.
    2. Generate the correct arguments for the tool.
    3. Follow ALL service and tool instructions carefully.
    4. Respect any API grammar rules or DSL instructions provided.
    5. Use default argument values if the user query does not specify them.
    6. If the user query requires DSL syntax (e.g. search operators), construct the correct query string.
    7. If required arguments cannot be inferred, return an empty JSON object.

    Rules:
    - Select only one tool.
    - Do not invent arguments not defined in the schema.
    - If source of an argument is "file", expect it to be provided as multipart file upload and ignore it in the argument generation.
    - Follow argument types exactly.

    Return ONLY valid JSON:

    {
      "tool_name": "tool_name",
      "arguments": { ... }
    }

    Do NOT return explanation.

    Never modify, remove, or normalize user-provided field values.
    Preserve all characters exactly as given, including special characters like #, $, @, etc.
    """


# ─────────────────────────────────────────────
# LLM loader — uses the deployed agent's own src.litellm_factory
# ─────────────────────────────────────────────

def _load_llm(agent_name: Optional[str] = None):
    """
    Load the LLM instance via src.litellm_factory.create_llm.

    Forwards `agent_name` so the AIModelConfig table can route this tool's
    LLM call to the same model + fallback chain the calling agent uses.
    Falls back to a generic "ToolGateway" name if no agent_name is supplied.
    """
    from src.litellm_factory import create_llm
    resolved_name = agent_name or "ToolGateway"
    logger.info("Loading LLM via src.litellm_factory | agent_name=%s", resolved_name)
    return create_llm(agent_name=resolved_name)


# ─────────────────────────────────────────────
# LLM client
# ─────────────────────────────────────────────

class LLMClient:
    """
    Thin async wrapper around the LangChain-compatible LLM instance returned
    by src.litellm_factory.create_llm.

    Exposes two high-level methods used by the handler:
      - resolve_service  →  picks the best service for a user query
      - identify_tool    →  picks the best tool and generates its arguments
    """

    def __init__(self, agent_name: Optional[str] = None):
        self._agent_name = agent_name
        self._llm = None  # lazy-loaded on first call

    def _get_llm(self):
        if self._llm is None:
            self._llm = _load_llm(agent_name=self._agent_name)
        return self._llm

    async def resolve_service(self, user_query: str, services: list) -> str:
        """Asks the LLM to pick the best service for the query. Returns the service name."""
        services_summary = [
            {
                "name":         s.get("name"),
                "description":  s.get("description", ""),
                "instructions": s.get("instructions", ""),
                "transport":    s.get("transport", ""),
            }
            for s in services
        ]
        message = (
            f"User query: {user_query}\n"
            f"Available services: {json.dumps(services_summary, indent=2)}"
        )
        raw = await self._call(message, _SYSTEM_PROMPT_SERVICE)
        try:
            return json.loads(raw).get("service_name", "")
        except json.JSONDecodeError:
            logger.warning("resolve_service: LLM returned non-JSON response: %s", raw[:200])
            return ""

    async def identify_tool(self, user_query: str, tools: list) -> dict:
        """
        Asks the LLM to pick the best tool and generate its arguments.
        Returns: { "tool_name": "...", "arguments": { ... } }
        """
        tools_summary = [
            {
                "name":         t.get("name"),
                "description":  t.get("description", ""),
                "parameters":   t.get("inputSchema", {}),
                "instructions": t.get("instructions", ""),
                "endpoint":     t.get("path", ""),
            }
            for t in tools
        ]
        message = (
            f"User query: {user_query}\n"
            f"Available tools: {json.dumps(tools_summary, indent=2)}"
        )
        logger.info("identify_tool input: %s", message[:500])
        raw = await self._call(message, _SYSTEM_PROMPT_TOOL)
        logger.info("identify_tool raw response: %s", raw[:500])
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("identify_tool: LLM returned non-JSON response: %s", raw[:200])
            return {}

    async def _call(self, user_message: str, system_prompt: str) -> str:
        """Invokes the LLM and returns the stripped response string."""
        from langchain_core.messages import SystemMessage, HumanMessage

        llm = self._get_llm()
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ]
        # Run the synchronous LangChain call in a thread to avoid blocking the event loop
        response = await asyncio.to_thread(llm.invoke, messages)
        return response.content.replace("```json", "").replace("```", "").strip()
