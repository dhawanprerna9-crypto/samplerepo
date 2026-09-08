"""
Session initialization MCP tools for generated ADK agent systems.
Pre-scaffolded — do not modify or recreate.

Provides two tools that every generated agent's MCP server exposes so that
MCP clients (Copilot, Claude, etc.) can understand and correctly use the
agent system without hallucinating capabilities:

  Initialize_Session  — returns the master capabilities document (call once per session)
  Load_Instructions   — returns per-domain module instructions (call when deeper routing guidance needed)

Both tools read from the `instructions/` directory co-located with the MCP server:
  orchestrator/reinvg_mcp_server/instructions/AGENT_CAPABILITIES.md   ← Initialize_Session reads this
  orchestrator/reinvg_mcp_server/instructions/modules/{module}.md      ← Load_Instructions reads these
"""
import logging
import uuid
from pathlib import Path
from configparser import ConfigParser
from fastmcp import Context
from core.mcp_instance import mcp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution
# File location: {repo_root}/orchestrator/reinvg_mcp_server/tools/agent_session_delegate.py
#   parents[0] = tools/
#   parents[1] = reinvg_mcp_server/
#   parents[2] = orchestrator/
#   parents[3] = repo root  (where config.ini lives)
# ---------------------------------------------------------------------------
_TOOL_FILE = Path(__file__).resolve()
_MCP_SERVER_ROOT = _TOOL_FILE.parents[1]          # reinvg_mcp_server/
_REPO_ROOT = _TOOL_FILE.parents[3]                # repo root

_INSTRUCTIONS_DIR = _MCP_SERVER_ROOT / "instructions"
_CAPABILITIES_FILE = _INSTRUCTIONS_DIR / "AGENT_CAPABILITIES.md"
_MODULES_DIR = _INSTRUCTIONS_DIR / "modules"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_config = ConfigParser()
_config.read(_REPO_ROOT / "config.ini", encoding="utf-8")


def _substitute(content: str) -> str:
    """Replace known placeholders with runtime config values."""
    agent_system_name = _config.get("mcp", "server_name", fallback="")
    mcp_url = _config.get("mcp", "server_url", fallback="")
    if agent_system_name:
        content = content.replace("{AgentSystemName}", agent_system_name)
    if mcp_url:
        content = content.replace("{MCPBaseURL}", mcp_url)
    return content


def _list_modules() -> list[str]:
    """Return sorted list of available module keys (filename stems under modules/)."""
    if not _MODULES_DIR.exists():
        return []
    return sorted(
        f.stem for f in _MODULES_DIR.iterdir()
        if f.suffix == ".md" and f.is_file()
    )


# ---------------------------------------------------------------------------
# Tool 1 — Initialize_Session
# ---------------------------------------------------------------------------

@mcp.tool(
    name="Initialize_Session",
    description=(
        "Load the complete capabilities and routing instructions for this agent system.\n\n"
        "*** CALL THIS TOOL EXACTLY ONCE PER CONVERSATION — ON THE VERY FIRST USER MESSAGE ONLY. ***\n"
        "*** NEVER call it again once instructions have been loaded into the current session.     ***\n"
        "*** If this tool has already been called in this conversation, skip it entirely and      ***\n"
        "*** follow the instructions already in context.                                          ***\n\n"
        "Trigger condition — call on the FIRST user message when it matches ANY of the following:\n\n"
        "GREETINGS / OPENING:\n"
        '- "hi", "hello", "help", "start", "what can you do", "get started"\n\n'
        "CAPABILITY QUESTIONS:\n"
        '- "what is this agent?", "what does this do?", "what do you support?"\n'
        '- "tell me your capabilities", "what tools are available?", "list your features"\n\n'
        "DOMAIN QUERIES (first message in session only — load instructions FIRST, then route):\n"
        "- Any user request that requires knowing which tool or agent to invoke\n\n"
        "Returns the full AGENT_CAPABILITIES.md document which contains:\n"
        "- What this agent system does and what domains it covers\n"
        "- Complete tool catalog grouped by capability domain\n"
        "- Query routing guide: which domain / tool handles which user intent\n"
        "- Response rendering instructions: how to format tool outputs for users\n"
        "- Available Load_Instructions module keys for deeper per-domain guidance\n\n"
        "Follow the instructions returned by this tool for ALL subsequent turns in this conversation. "
        "Do NOT re-invoke Initialize_Session on follow-up messages."
    ),
)
async def Initialize_Session(_ctx: Context) -> str:
    """
    Load the complete capabilities and routing instructions for this agent system.

    *** CALL THIS TOOL EXACTLY ONCE PER CONVERSATION — ON THE VERY FIRST USER MESSAGE ONLY. ***
    *** NEVER call it again once instructions have been loaded into the current session.     ***
    *** If this tool has already been called in this conversation, skip it entirely and      ***
    *** follow the instructions already in context.                                          ***

    Trigger condition — call on the FIRST user message when it matches ANY of the following:

    GREETINGS / OPENING:
    - "hi", "hello", "help", "start", "what can you do", "get started"

    CAPABILITY QUESTIONS:
    - "what is this agent?", "what does this do?", "what do you support?"
    - "tell me your capabilities", "what tools are available?", "list your features"

    DOMAIN QUERIES (first message in session only — load instructions FIRST, then route):
    - Any user request that requires knowing which tool or agent to invoke

    Returns the full AGENT_CAPABILITIES.md document which contains:
    - What this agent system does and what domains it covers
    - Complete tool catalog grouped by capability domain
    - Query routing guide: which domain / tool handles which user intent
    - Response rendering instructions: how to format tool outputs for users
    - Available Load_Instructions module keys for deeper per-domain guidance

    Follow the instructions returned by this tool for ALL subsequent turns in this
    conversation. Do NOT re-invoke Initialize_Session on follow-up messages.
    """
    request_id = str(uuid.uuid4())
    logger.info(f"[{request_id}] Initialize_Session called")
    try:
        if not _CAPABILITIES_FILE.exists():
            error_msg = f"Agent capabilities file not found at: {_CAPABILITIES_FILE}"
            logger.error(f"[{request_id}] {error_msg}")
            return f"[Initialize_Session] Error: {error_msg}"

        content = _CAPABILITIES_FILE.read_text(encoding="utf-8")
        content = _substitute(content)
        logger.info(f"[{request_id}] Initialize_Session | bytes={len(content)}")
        return content

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        logger.exception(f"[{request_id}] Initialize_Session failed: {error_msg}")
        return f"[Initialize_Session] Error: {error_msg}"


# ---------------------------------------------------------------------------
# Tool 2 — Load_Instructions
# ---------------------------------------------------------------------------

@mcp.tool(
    name="Load_Instructions",
    description=(
        "Load detailed instructions for a specific capability module of this agent system.\n\n"
        "Call this tool when:\n"
        "- Initialize_Session instructs you to load a module for the user's domain\n"
        "- The user's query targets a specific capability domain and you need tool-sequencing\n"
        "  or response-rendering guidance beyond what AGENT_CAPABILITIES.md provides\n"
        "- You need to clarify parameter usage or multi-step workflow order for a domain\n\n"
        "Args:\n"
        "    module: Module keyword identifying the capability domain. Must match one of\n"
        "            the keys listed under 'Loading Module-Specific Instructions' in\n"
        "            AGENT_CAPABILITIES.md (returned by Initialize_Session). Case-insensitive.\n"
        "            Module keys are lowercase snake_case domain names, e.g. 'statement_management'.\n\n"
        "Returns the full markdown instruction content for the requested module, which includes:\n"
        "- Utility agents and their individual tools for this domain\n"
        "- Step-by-step workflow sequences for common use cases\n"
        "- Response rendering instructions per tool\n"
        "- Error handling guidance\n\n"
        "On failure (unknown module, file not found) returns an error message string — never raises."
    ),
)
async def Load_Instructions(module: str) -> str:
    """
    Load detailed instructions for a specific capability module of this agent system.

    Call this tool when:
    - Initialize_Session instructs you to load a module for the user's domain
    - The user's query targets a specific capability domain and you need tool-sequencing
      or response-rendering guidance beyond what AGENT_CAPABILITIES.md provides
    - You need to clarify parameter usage or multi-step workflow order for a domain

    Available module keys are listed in the AGENT_CAPABILITIES.md returned by
    Initialize_Session. Each key maps to a super-agent capability domain (e.g.
    "statement_management", "wallet_integration"). Module keys are lowercase with
    underscores — match the snake_case domain names shown in AGENT_CAPABILITIES.md.

    Args:
        module: Module keyword identifying the capability domain. Must match one of
                the keys listed under "Loading Module-Specific Instructions" in
                AGENT_CAPABILITIES.md. Case-insensitive.

    Returns:
        str: Full markdown instruction content for the module on success, or an
             error message string (never raises) on failure.

    The returned content includes:
    - Utility agents and their individual tools for this domain
    - Step-by-step workflow sequences for common use cases
    - Response rendering instructions per tool
    - Error handling guidance
    """
    request_id = str(uuid.uuid4())
    try:
        module_key = module.strip().lower()
        available = _list_modules()

        if not available:
            logger.warning(f"[{request_id}] Load_Instructions: no modules found at {_MODULES_DIR}")
            return (
                f"[Load_Instructions] No instruction modules found. "
                f"Ensure the agent was generated with instruction files at {_MODULES_DIR}."
            )

        if module_key not in available:
            logger.warning(f"[{request_id}] Load_Instructions: unknown module '{module}'")
            available_str = ", ".join(sorted(available))
            return (
                f"[Load_Instructions] Unknown module '{module}'. "
                f"Available modules: {available_str}"
            )

        # Path traversal guard
        modules_dir_resolved = _MODULES_DIR.resolve()
        file_path = (modules_dir_resolved / f"{module_key}.md").resolve()

        if not str(file_path).startswith(str(modules_dir_resolved)):
            logger.error(f"[{request_id}] Load_Instructions: path traversal attempt for '{module}'")
            return "[Load_Instructions] Error: Access denied — resolved path is outside instructions directory."

        if not file_path.exists():
            logger.error(f"[{request_id}] Load_Instructions: file not found for '{module_key}'")
            return f"[Load_Instructions] Instruction file not found for module '{module}'."

        content = file_path.read_text(encoding="utf-8")
        content = _substitute(content)
        logger.info(f"[{request_id}] Load_Instructions: loaded '{module_key}' | bytes={len(content)}")
        return content

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        logger.exception(f"[{request_id}] Load_Instructions failed: {error_msg}")
        return f"[Load_Instructions] Unexpected error loading module '{module}': {error_msg}"
