"""
executor.py
-----------
Template rendering + tool execution.

Two supported transports:
  - api             → template-based HTTP (QPP-style)
  - streamable-http → MCP Streamable HTTP via McpToolset (Vertex-style)

``ToolExecutor.execute()`` is the single public entry point; it dispatches
to the right transport based on ``service["transport"]``.
``ToolExecutor.discover_tools()`` is the entry point for live tool
discovery on streamable-http endpoints.
"""

import json
import logging
import re
import httpx

from .auth_handler import inject_auth_into_headers

logger = logging.getLogger("tool_gateway.executor")

# Matches a value that is *entirely* an unresolved "{{placeholder}}" left over after
# template rendering (i.e. the argument was never supplied and had no schema default).
_UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\{\{.*?\}\}")


def _strip_unresolved_placeholders(body):
    """
    Drop dict entries whose value is still an unresolved ``{{placeholder}}`` after
    rendering, so a malformed literal is never sent to the downstream API.

    Only removes top-level string values that are *entirely* an unresolved
    placeholder; genuine content is left untouched. Best-effort and guarded — on any
    error the body is returned unchanged so this can never break a valid request.
    """
    if not isinstance(body, dict):
        return body
    try:
        cleaned = {}
        for key, val in body.items():
            if isinstance(val, str) and _UNRESOLVED_PLACEHOLDER_RE.fullmatch(val.strip()):
                logger.info("[api] dropping unresolved placeholder field '%s'", key)
                continue
            cleaned[key] = val
        return cleaned
    except Exception as exc:
        logger.warning("[api] placeholder cleanup skipped (non-fatal): %s", exc)
        return body


# ─────────────────────────────────────────────
# Template rendering
# ─────────────────────────────────────────────

class TemplateRenderer:
    """Recursively resolves {{key}} placeholders in strings, dicts, and lists."""

    def render(self, template, arguments: dict):
        if isinstance(template, str):
            for key, val in (arguments or {}).items():
                placeholder = f"{{{{{key}}}}}"
                # If the entire template is a single placeholder, preserve the
                # original Python type (int, float, bool, list, dict) instead
                # of coercing to string.
                if template == placeholder:
                    return val if val is not None else ""
                template = template.replace(
                    placeholder,
                    str(val) if val is not None else "",
                )
            return template
        if isinstance(template, dict):
            return {k: self.render(v, arguments) for k, v in template.items()}
        if isinstance(template, list):
            return [self.render(item, arguments) for item in template]
        return template


# ─────────────────────────────────────────────
# ToolExecutor — dispatches by transport
# ─────────────────────────────────────────────

class ToolExecutor:
    """
    Dispatches a tool call to the correct transport handler based on
    ``service["transport"]``.

    Supported transports:
      "api"             → _execute_api          (QPP-style HTTP)
      "streamable-http" → _execute_streamable_http (Vertex-style MCP)
    """

    def __init__(self, renderer: TemplateRenderer | None = None):
        self._renderer = renderer or TemplateRenderer()
        self._transport_map = {
            "api":             self._execute_api,
            "streamable-http": self._execute_streamable_http,
        }

    async def execute(
        self,
        service:      dict,
        tool_def:     dict,
        auth_profile: dict,
        token:        str | None,
        cookies:      dict | None,
        arguments:    dict,
        files:        list | None = None,
    ) -> dict:
        transport = service.get("transport", "api")
        handler = self._transport_map.get(transport)
        if not handler:
            raise ValueError(f"Unsupported transport: '{transport}'")

        logger.info("Executing tool '%s' via transport '%s'", tool_def.get("name"), transport)
        return await handler(service, tool_def, auth_profile, token, cookies, arguments, files)

    async def discover_tools(
        self,
        service:      dict,
        auth_profile: dict,
        token:        str | None,
        cookies:      dict | None,
    ) -> list:
        """
        Discover available tools from the service endpoint.

        - streamable-http: connects to MCP, calls list_tools, returns discovered tools.
        - api:             returns empty list (tools are owned by the DB).
        """
        transport = service.get("transport", "api")
        if transport == "streamable-http":
            return await self._discover_streamable_http(service, auth_profile, token, cookies)
        return []

    # ─────────────────────────────────────────
    # Transport: api (template-based HTTP)
    # ─────────────────────────────────────────

    async def _execute_api(
        self, service, tool_def, auth_profile, token, cookies, arguments, files,
    ) -> dict:
        base_url = service.get("endpoint", "").rstrip("/")
        path = self._renderer.render(tool_def.get("path", ""), arguments)
        url = f"{base_url}/{path.lstrip('/')}" if path else base_url

        headers = self._renderer.render(tool_def.get("headers", {}), arguments)
        params = self._renderer.render(tool_def.get("params", {}), arguments)
        body_type = tool_def.get("bodyType", "json")
        body_template = tool_def.get("bodyTemplate", {})
        body = self._renderer.render(body_template, arguments) if body_template else arguments
        body = _strip_unresolved_placeholders(body)

        headers = inject_auth_into_headers(headers, auth_profile, token, cookies)

        request_kwargs: dict = {
            "method":  tool_def.get("method", "POST"),
            "url":     url,
            "headers": headers,
            "params":  params,
        }

        if files:
            # Honor the tool's declared multipart field name (e.g. QPP upload expects
            # "file"); fall back to "files" when the tool does not declare fileFields.
            _file_field = (tool_def.get("fileFields") or ["files"])[0]
            request_kwargs["files"] = [
                (_file_field, (f["filename"], open(f["path"], "rb"), f.get("content_type", "application/octet-stream")))
                for f in files
            ]
            request_kwargs["data"] = body
        elif body_type == "json":
            request_kwargs["json"] = body
        elif body_type in ["x-www-form-urlencoded", "multipart"]:
            request_kwargs["data"] = body
        elif body_type == "queryParams":
            request_kwargs["params"] = {**(params or {}), **body}

        if cookies:
            request_kwargs["cookies"] = cookies

        _safe_headers = {
            k: ("***" if k.lower() in ("apitoken", "authorization", "x-api-key") else v)
            for k, v in headers.items()
        }
        logger.info("[api] %s %s | headers=%s | body_type=%s",
                    request_kwargs["method"], url, _safe_headers, body_type)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.request(**request_kwargs)

        logger.info("[api] response status=%s", resp.status_code)

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "[api] HTTP %s | url=%s | body=%s | response=%s",
                resp.status_code, url,
                json.dumps(body)[:500] if body_type == "json" else str(body)[:500],
                resp.text[:500],
            )
            raise

        try:
            return resp.json()
        except Exception:
            return {"response": resp.text}

    # ─────────────────────────────────────────
    # Transport: streamable-http (MCP)
    # ─────────────────────────────────────────

    def _build_mcp_session_manager(self, endpoint: str, auth_profile: dict, token, cookies):
        """Creates a McpToolset session manager for the given endpoint and auth."""
        from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
        headers = inject_auth_into_headers({}, auth_profile, token, cookies)
        # Add Content-Type for MCP protocol
        headers["Content-Type"] = "application/json"
        logger.debug(
            "[streamable-http] MCP session | endpoint=%s | mcp_server=%s",
            endpoint,
            headers.get("x-mcp-servers", "default")
        )
        toolset = McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=endpoint,
                headers=headers,
                timeout=150,
            )
        )
        return toolset._mcp_session_manager

    async def _discover_streamable_http(
        self, service: dict, auth_profile: dict, token, cookies,
    ) -> list:
        """Connect to an MCP endpoint and return the list of available tools."""
        endpoint = service.get("endpoint", "").rstrip("/")
        session_mgr = self._build_mcp_session_manager(endpoint, auth_profile, token, cookies)
        session = await session_mgr.create_session()
        logger.debug("streamable-http discovery session created for '%s'", service.get("name"))

        try:
            response = await session.list_tools()
        finally:
            try:
                await session_mgr.close()
            except Exception:
                logger.debug("streamable-http discovery session close raised (non-fatal)")

        tools = []
        for t in getattr(response, "tools", []):
            tools.append({
                "name":        t.name,
                "description": getattr(t, "description", ""),
                "inputSchema": getattr(t, "inputSchema", {}),
            })
        logger.info("streamable-http discovered %d tools from '%s'", len(tools), service.get("name"))
        return tools

    async def _execute_streamable_http(
        self, service, tool_def, auth_profile, token, cookies, arguments, files,
    ) -> dict:
        """Calls a single tool on an MCP streamable-http endpoint (Vertex RAG pattern)."""
        endpoint = service.get("endpoint", "").rstrip("/")
        tool_name = tool_def.get("name", "")

        session_mgr = self._build_mcp_session_manager(endpoint, auth_profile, token, cookies)
        session = await session_mgr.create_session()
        logger.debug("streamable-http session created for tool '%s'", tool_name)

        try:
            response = await session.call_tool(tool_name, arguments or {})
        finally:
            try:
                await session_mgr.close()
            except Exception:
                logger.debug("streamable-http session close raised (non-fatal)")

        if not hasattr(response, "content") or not response.content:
            raise RuntimeError(
                f"Streamable-HTTP tool '{tool_name}' returned no content: {response}"
            )

        try:
            raw_text = response.content[0].text
            return json.loads(raw_text)
        except (json.JSONDecodeError, IndexError, AttributeError):
            return {"response": str(response)}
