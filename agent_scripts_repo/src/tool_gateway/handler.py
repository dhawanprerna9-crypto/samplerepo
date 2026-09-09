"""
handler.py
----------
Orchestrates the full tool gateway flow.

Public entry point: ``invoke_tool_gateway``.

Flow (transport=api — QPP-style HTTP):
  1. Resolve service by name (LLM picks if not provided)
  2. Load tools associated with the service from DB(or config)
  3. LLM identifies the best tool + arguments for the user query
  4. Resolve auth profile + token (qpp-token strategy)
  5. Execute HTTP tool call

Flow (transport=streamable-http — Vertex MCP):
  1. Resolve service by name
  2. Load auth profile and resolve token (api-key strategy)
  3. Discover tools live from the MCP endpoint
  4. LLM identifies the best tool + arguments
  5. Execute MCP tool call

database_enabled flag (llmConfig.ini [appsettings]):
  True  → service, tool, and auth data are fetched from the Postgres database.
  False → service and auth data are read from llmConfig.ini sections ([Quasar],
          [VertexAI], [Backstage]); tool definitions for api-transport services
          are read from tool_gateway_config.json next to llmConfig.ini.

Failure modes:
  - Unknown service_name        → return {"status": "empty", ...}  (never raises)
  - LLM picks unknown tool      → return {"status": "empty", ...}
  - Missing auth profile        → return {"status": "empty", ...}
  - DB unavailable              → return {"status": "empty", ...}
  - Tool execution failure      → propagates (caller can wrap in try/except)

Dependencies (all bundled with the deployed agent):
  - src.db_utils._get_db_connection  → Postgres connection from llmConfig.ini [database]
  - src.litellm_factory.create_llm   → LLM for service/tool selection (via .llm_client)
  - src.secret_providers.*           → secret-store for QPP / Vertex credentials (via .auth_handler)
"""

import json
import logging
from configparser import ConfigParser
from pathlib import Path

from src.db_utils import _get_db_connection

from .repositories import ServiceRepository, ToolRepository, AuthProfileRepository, ServiceRow
from .llm_client import LLMClient
from .auth_handler import resolve_auth_token
from .executor import ToolExecutor

logger = logging.getLogger("tool_gateway.handler")


def _empty(service_name: str, message: str) -> dict:
    """Standard empty-but-not-an-exception response shape."""
    return {
        "status":  "empty",
        "service": service_name,
        "data":    None,
        "message": message,
    }

def _read_database_enabled() -> bool:
    """Read the database_enabled flag from llmConfig.ini [appsettings]."""
    cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read(_LLM_CFG_PATH)
    return cfg.getboolean("appsettings", "database_enabled", fallback=True)


def _match_service_config(service_name: str) -> tuple[str, str, str] | None:
    """
    Match service_name (case-insensitive) to a (config_section, transport, auth_type)
    tuple from _SERVICE_CONFIG_MAP. Returns None if no match.
    """
    name_lower = (service_name or "").lower().strip()
    if name_lower in _SERVICE_CONFIG_MAP:
        return _SERVICE_CONFIG_MAP[name_lower]
    for key, value in _SERVICE_CONFIG_MAP.items():
        if name_lower.startswith(key) or key.startswith(name_lower):
            return value
    return None


def _resolve_service_from_config(service_name: str) -> ServiceRow | None:
    """
    Build a ServiceRow from llmConfig.ini when database is disabled.
    Reads endpoint and transport from the matching config section.
    """
    match = _match_service_config(service_name)
    if match is None:
        return None
    section, transport, _ = match
    cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read(_LLM_CFG_PATH)
    if not cfg.has_section(section):
        return None
    endpoint = cfg.get(section, "endpoint", fallback="").strip()
    if not endpoint and section == "Backstage":
        endpoint = cfg.get(section, "mcp_url", fallback="").strip()
    return ServiceRow(
        service_uid      = None,
        name             = service_name,
        endpoint         = endpoint,
        transport        = transport,
        config           = {},
        auth_profile_uid = None,
    )


def _all_services_from_config() -> list[ServiceRow]:
    """
    Return ServiceRow objects for every service that has an endpoint configured
    in llmConfig.ini. Used for LLM-based service resolution when database is disabled.
    """
    cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read(_LLM_CFG_PATH)
    rows: list[ServiceRow] = []
    seen_sections: set[str] = set()
    for name, (section, transport, _) in _SERVICE_CONFIG_MAP.items():
        if section in seen_sections or not cfg.has_section(section):
            continue
        seen_sections.add(section)
        endpoint = cfg.get(section, "endpoint", fallback="").strip()
        if not endpoint and section == "Backstage":
            endpoint = cfg.get(section, "mcp_url", fallback="").strip()
        if endpoint:
            rows.append(ServiceRow(
                service_uid      = None,
                name             = name,
                endpoint         = endpoint,
                transport        = transport,
                config           = {},
                auth_profile_uid = None,
            ))
    return rows


def _build_auth_profile_from_config(service_name: str) -> dict | None:
    """
    Build a minimal auth_profile dict from llmConfig.ini when database is disabled.
    The auth_handler will handle actual credential resolution from config sections
    or the secret store based on is_secret_store_enabled.
    Inject rules mirror the conventions used in the DB auth_profiles table.
    """
    match = _match_service_config(service_name)
    if match is None:
        return None
    _, _, auth_type = match

    _INJECT_RULES: dict[str, list] = {
        "qpp-token":     [{"location": "header", "name": "apiToken",          "prefix": ""}],
        "api-key":       [{"location": "header", "name": "x-api-key",         "prefix": ""}],
        "backstage-mcp": [{"location": "header", "name": "x-litellm-api-key", "prefix": "Bearer "}],
    }

    return {
        "type":                    auth_type,
        "is_secret_store_enabled": True,
        "auth_config":             {},
        "inject":                  _INJECT_RULES.get(auth_type, []),
    }


def _load_tools_from_config(service_name: str) -> list[dict]:
    """
    Load tool definitions from tool_gateway_config.json for api-transport services.
    Expected format:
      { "services": { "<service_name>": { "tools": [ { "name": "...", ... } ] } } }
    """
    if not _TOOL_GW_CFG_PATH.exists():
        logger.warning("[Tool Gateway] tool_gateway_config.json not found at %s", _TOOL_GW_CFG_PATH)
        return []
    try:
        with open(_TOOL_GW_CFG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        name_lower = (service_name or "").lower().strip()
        # Also resolve alias → canonical section name (e.g. "qpp" → "Quasar")
        match = _match_service_config(service_name)
        section_lower = match[0].lower() if match else None
        for svc_key, svc_data in (data.get("services") or {}).items():
            if svc_key.lower() in (name_lower, section_lower):
                tools = svc_data.get("tools") or []
                # Flatten nested Tool_definition into each tool so the executor
                # and LLM client can access path/method/headers/inputSchema directly.
                flat_tools = []
                for tool in tools:
                    nested = tool.get("Tool_definition", {})
                    flat_tools.append({"name": tool.get("name", ""), **nested})
                return flat_tools
        logger.warning("[Tool Gateway] No tools found in tool_gateway_config.json for service '%s'", service_name)
        return []
    except Exception as exc:
        logger.warning("[Tool Gateway] Failed to load tool_gateway_config.json: %s", exc)
        return []


async def invoke_tool_gateway(
    user_query:   str,
    service_name: str  = "",
    user_id:      str  = None,
    files:        list = None,
    agent_name:   str  = None,
    tool_arg_overrides: dict | None = None,
    preferred_tool_name: str | None = None,
) -> dict:
    """
    Run the full tool gateway pipeline end-to-end.

    Args:
        user_query:   Natural language query (required).
        service_name: Optional. Known service name. If omitted, the LLM picks
                      a service from the registry. Case-insensitive lookup.
        user_id:      Optional. Injected into tool arguments if provided.
        files:        Optional. List of {"path": str, "filename": str} for multipart uploads.
        agent_name:   Optional. Forwarded to ``create_llm(agent_name=...)`` so
                      this call uses the same DB-routed model as the calling agent.
        tool_arg_overrides: Optional. Dict of argument values that deterministically
                      override whatever the LLM produced for the selected tool
                      (e.g. ``{"search_criteria": ["knn", "keyword"]}``). Only keys
                      present in the tool's bodyTemplate take effect; unknown keys are
                      harmlessly ignored by the executor. Defaults to None, in which
                      case behaviour is identical to before this parameter existed.
        preferred_tool_name: Optional. When set, bypasses LLM tool-selection for
                      api-transport (QPP) services and directly uses the named tool.
                      This is deterministic — no LLM routing call is made. Falls back
                      to LLM routing if the name is not found in the tool list. Has no
                      effect on streamable-http (MCP) services. Defaults to None.

    Returns:
        dict. Either the tool's response body, or an empty-shape
        {"status": "empty", "service": ..., "data": None, "message": ...} on
        any soft failure (unknown service, no tools, etc.).
    """
    if not user_query or not user_query.strip():
        raise ValueError("user_query cannot be empty")

    logger.info("[Tool Gateway] start | query=%r | service_hint=%r | agent=%s",
                user_query, service_name or "(LLM will resolve)", agent_name)

    database_enabled = _read_database_enabled()
    logger.info("[Tool Gateway] database_enabled=%s", database_enabled)

    if database_enabled:
        # ── Database-enabled path (original behaviour) ───────────────────
        conn = _get_db_connection()
        if conn is None:
            return _empty(service_name, "Database unavailable — check llmConfig.ini [database]")

        llm = LLMClient(agent_name=agent_name)
        executor = ToolExecutor()

        try:
            with conn.cursor() as cur:
                services_repo = ServiceRepository(cur)
                tools_repo    = ToolRepository(cur)
                auth_repo     = AuthProfileRepository(cur)

                # ── Step 1: Resolve service ──────────────────────────────────
                service_row = services_repo.get_by_name(service_name) if service_name else None

                if service_row is None and not service_name:
                    # No name given — ask the LLM to pick
                    all_services = services_repo.get_all()
                    services_for_llm = [
                        {"name": s.name, "description": (s.config or {}).get("description", "")}
                        for s in all_services
                    ]
                    try:
                        resolved_name = await llm.resolve_service(user_query, services_for_llm)
                        service_row = services_repo.get_by_name(resolved_name)
                    except Exception as e:
                        logger.warning("LLM service resolution failed: %s", e)

                if service_row is None:
                    return _empty(
                        service_name,
                        f"Service '{service_name}' not found in registry",
                    )

                logger.info("[Step 1] service resolved: %s (transport=%s)",
                            service_row.name, service_row.transport)

                transport = service_row.transport
                service_dict = {
                    "endpoint":  service_row.endpoint,
                    "name":      service_row.name,
                    "transport": transport,
                    "config":    service_row.config or {},
                }

                tool_def     = None
                tool_name    = None
                arguments    = None
                auth_profile = None
                token        = None
                cookies      = None

                if transport == "streamable-http":
                    # ── Vertex MCP path ─────────────────────────────────────
                    if not service_row.auth_profile_uid:
                        return _empty(
                            service_name,
                            f"Service '{service_row.name}' requires an auth_profile_uid",
                        )

                    auth_profile = auth_repo.get_by_uid(service_row.auth_profile_uid)
                    if auth_profile is None:
                        return _empty(service_name, "Auth profile not found for service")

                    logger.info("[Step 2] auth profile loaded (type=%s)", auth_profile.get("type"))

                    token, cookies = await resolve_auth_token(auth_profile, endpoint=service_row.endpoint)
                    logger.info("[Step 3] auth token obtained")

                    tools = await executor.discover_tools(service_dict, auth_profile, token, cookies)
                    if not tools:
                        return _empty(
                            service_name,
                            f"No tools discovered from service: {service_row.name}",
                        )
                    logger.info("[Step 4] discovered %d tools", len(tools))

                    tool_call = await llm.identify_tool(user_query, tools)
                    tool_name = tool_call.get("tool_name")
                    arguments = tool_call.get("arguments", {}) or {}
                    if user_id:
                        arguments["user_id"] = user_id

                    tool_def = next((t for t in tools if t.get("name") == tool_name), None)
                    if tool_def is None:
                        return _empty(
                            service_name,
                            f"LLM picked an unknown tool: '{tool_name}'",
                        )
                    logger.info("[Step 5] tool identified: %s", tool_name)

                else:
                    # ── QPP / api path ──────────────────────────────────────
                    tools = tools_repo.get_by_service(service_row.service_uid)
                    if not tools:
                        return _empty(
                            service_name,
                            f"No tools registered for service: {service_row.name}",
                        )
                    logger.info("[Step 2] tools from DB: %s", [t.get("name") for t in tools])

                    tool_call = await llm.identify_tool(user_query, tools)
                    tool_name = tool_call.get("tool_name")
                    arguments = tool_call.get("arguments", {}) or {}
                    if user_id:
                        arguments["user_id"] = user_id

                    tool_def = next((t for t in tools if t.get("name") == tool_name), None)
                    if tool_def is None:
                        return _empty(
                            service_name,
                            f"LLM picked an unknown tool: '{tool_name}'",
                        )
                    logger.info("[Step 3] tool identified: %s | args: %s", tool_name, arguments)

                    tool_auth_name = tool_def.get("auth_profile")
                    if tool_auth_name:
                        auth_profile = auth_repo.get_by_name(tool_auth_name)
                    elif service_row.auth_profile_uid:
                        auth_profile = auth_repo.get_by_uid(service_row.auth_profile_uid)
                    else:
                        return _empty(
                            service_name,
                            "No auth profile associated with tool or service",
                        )

                    if auth_profile is None:
                        return _empty(service_name, "Auth profile not found")

                    logger.info("[Step 4] auth profile loaded (type=%s)", auth_profile.get("type"))

                    token, cookies = await resolve_auth_token(auth_profile, endpoint=service_row.endpoint)
                    logger.info("[Step 5] auth token obtained")

                # ── Step 6: Execute tool call ────────────────────────────────
                logger.info("[Step 6] executing tool '%s' via %s", tool_name, transport)
                result = await executor.execute(
                    service      = service_dict,
                    tool_def     = tool_def,
                    auth_profile = auth_profile,
                    token        = token,
                    cookies      = cookies,
                    arguments    = arguments,
                    files        = files,
                )
                logger.info("[Tool Gateway] complete | tool=%s", tool_name)
                return result

        finally:
            try:
                conn.close()
            except Exception:
                pass
    
    else:
        # ── Config-based path (database_enabled=False) ────────────────────
        # Service, auth, and tool data are sourced from llmConfig.ini and
        # tool_gateway_config.json instead of the database.

        llm = LLMClient(agent_name=agent_name)
        executor = ToolExecutor()

        # ── Step 1: Resolve service from config ──────────────────────────
        service_row = _resolve_service_from_config(service_name) if service_name else None

        if service_row is None and not service_name:
            # No name given — ask the LLM to pick from config-defined services
            all_services = _all_services_from_config()
            services_for_llm = [
                {"name": s.name, "description": (s.config or {}).get("description", "")}
                for s in all_services
            ]
            try:
                resolved_name = await llm.resolve_service(user_query, services_for_llm)
                service_row = _resolve_service_from_config(resolved_name)
            except Exception as e:
                logger.warning("LLM service resolution failed: %s", e)

        if service_row is None:
            return _empty(
                service_name,
                f"Service '{service_name}' not found in llmConfig.ini (database_enabled=False). "
                "Ensure a matching section ([Quasar]/[VertexAI]/[Backstage]) is configured.",
            )

        logger.info("[Step 1] service resolved from config: %s (transport=%s)",
                    service_row.name, service_row.transport)

        transport = service_row.transport
        service_dict = {
            "endpoint":  service_row.endpoint,
            "name":      service_row.name,
            "transport": transport,
            "config":    service_row.config or {},
        }

        tool_def     = None
        tool_name    = None
        arguments    = None
        auth_profile = None
        token        = None
        cookies      = None

        if transport == "streamable-http":
            # ── Vertex / Backstage MCP path ──────────────────────────────
            auth_profile = _build_auth_profile_from_config(service_row.name)
            if auth_profile is None:
                return _empty(
                    service_name,
                    f"No auth config found for service '{service_row.name}' in llmConfig.ini",
                )

            logger.info("[Step 2] auth profile built from config (type=%s)", auth_profile.get("type"))

            token, cookies = await resolve_auth_token(auth_profile, endpoint=service_row.endpoint)
            logger.info("[Step 3] auth token obtained")

            tools = await executor.discover_tools(service_dict, auth_profile, token, cookies)
            if not tools:
                return _empty(
                    service_name,
                    f"No tools discovered from service: {service_row.name}",
                )
            logger.info("[Step 4] discovered %d tools", len(tools))

            tool_call = await llm.identify_tool(user_query, tools)
            tool_name = tool_call.get("tool_name")
            arguments = tool_call.get("arguments", {}) or {}
            if user_id:
                arguments["user_id"] = user_id

            tool_def = next((t for t in tools if t.get("name") == tool_name), None)
            if tool_def is None:
                return _empty(
                    service_name,
                    f"LLM picked an unknown tool: '{tool_name}'",
                )
            logger.info("[Step 5] tool identified: %s", tool_name)

        else:
            # ── QPP / api path — tools from tool_gateway_config.json ─────
            tools = _load_tools_from_config(service_row.name)
            if not tools:
                return _empty(
                    service_name,
                    f"No tools found in tool_gateway_config.json for service: {service_row.name}",
                )
            logger.info("[Step 2] tools from config: %s", [t.get("name") for t in tools])

            tool_call = await llm.identify_tool(user_query, tools)
            tool_name = tool_call.get("tool_name")
            arguments = tool_call.get("arguments", {}) or {}
            if user_id:
                arguments["user_id"] = user_id

            tool_def = next((t for t in tools if t.get("name") == tool_name), None)
            if tool_def is None:
                return _empty(
                    service_name,
                    f"LLM picked an unknown tool: '{tool_name}'",
                )
            logger.info("[Step 3] tool identified: %s | args: %s", tool_name, arguments)

            auth_profile = _build_auth_profile_from_config(service_row.name)
            if auth_profile is None:
                return _empty(
                    service_name,
                    "No auth config found for service in llmConfig.ini",
                )

            logger.info("[Step 4] auth profile built from config (type=%s)", auth_profile.get("type"))

            token, cookies = await resolve_auth_token(auth_profile, endpoint=service_row.endpoint)
            logger.info("[Step 5] auth token obtained")

        # ── Step 6: Execute tool call ────────────────────────────────────
        logger.info("[Step 6] executing tool '%s' via %s", tool_name, transport)
        result = await executor.execute(
            service      = service_dict,
            tool_def     = tool_def,
            auth_profile = auth_profile,
            token        = token,
            cookies      = cookies,
            arguments    = arguments,
            files        = files,
        )
        logger.info("[Tool Gateway] complete | tool=%s", tool_name)
        return result
