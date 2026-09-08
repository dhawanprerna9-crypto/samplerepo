"""
MCP delegation tool for the orchestrator agent.
IMPORTANT: Uses lazy initialization — no asyncio.run() at module level.
"""
import logging
import asyncio
import sys
import uuid
import json
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from configparser import ConfigParser
configenv = ConfigParser()
config_path = Path(__file__).parent.parent.parent.parent / "config.ini"
configenv.read(config_path)

try:
    from core.mcp_instance import mcp
except Exception:
    from fastmcp import FastMCP
    mcp = FastMCP("NetworkOpsMCPServer")

from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from orchestrator_agent.agent import get_initialized_orchestrator_agent_async, load_remote_agent_addresses

_orchestrator_agent = None
_agent_executor = None


async def _get_orchestrator():
    global _orchestrator_agent, _agent_executor
    if _orchestrator_agent is None:
        checkpointer = InMemorySaver()
        _agent_executor, _orchestrator_agent = await get_initialized_orchestrator_agent_async(checkpointer)
    return _agent_executor, _orchestrator_agent


def _preview_text(value: object, limit: int = 240) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:limit] + "..." if len(text) > limit else text


@mcp.tool(name="Orchestrator_Delegate_Tool")
async def Orchestrator_Delegate_Tool(user_query: str, thread_id: str = None) -> dict:
    """Delegate a user query to the NetworkOps Orchestrator Agent and return the output."""
    try:
        if thread_id is None:
            thread_id = str(uuid.uuid4())

        agent_executor, orchestrator_agent = await _get_orchestrator()

        try:
            updated = load_remote_agent_addresses()
            if hasattr(orchestrator_agent, "remote_addresses"):
                orchestrator_agent.remote_addresses = updated
        except Exception as e:
            logger.warning(f"Failed to refresh remote agents: {e}")

        agent_config = {"configurable": {"thread_id": thread_id}}
        response = None

        async for event in agent_executor.astream_events(
            {"messages": [HumanMessage(content=user_query)]},
            config=agent_config,
            version="v2",
        ):
            if event["event"] == "on_chain_end":
                response = event.get("data", {}).get("output")

        output = "[No output]"
        if response:
            if isinstance(response, dict) and "messages" in response:
                output = response["messages"][-1].content
            elif isinstance(response, str):
                output = response

        return {"thread_id": thread_id, "output": output}

    except Exception as e:
        return {"error": f"[Orchestrator] {type(e).__name__}: {e}", "thread_id": thread_id}
