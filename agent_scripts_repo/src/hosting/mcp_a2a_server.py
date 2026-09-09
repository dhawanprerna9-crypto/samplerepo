"""Deployable ``FastMCP -> A2A -> Microsoft Agent Framework`` hosting.

This module exposes the Microsoft Agent Framework supervisor agent through a
two-hop protocol pipeline served by a single ASGI app on one port:

    MCP client -> /mcp   (FastMCP Streamable HTTP; single tool; Keycloak OAuthProxy)
                  |
                  v  in-process A2A JSON-RPC call (message/send, service token)
                /a2a   (A2A server wrapping the MAF agent)
                  |
                  v
         Microsoft Agent Framework agent  (``agent.run``)

Deploying this yields an MCP URL that clients connect to:

    http://<host>:<port>/mcp

and the underlying A2A endpoint (agent card + JSON-RPC):

    http://<host>:<port>/a2a/               (JSON-RPC)
    http://<host>:<port>/a2a/.well-known/agent-card.json

Keycloak auth (when [auth.keycloak] isAuthEnabled=true):
- FastMCP's OAuthProxy handles the full browser OAuth code flow for MCP clients.
- The internal MCP->A2A loopback call uses a cached client_credentials service
  token so /a2a is not reachable without a valid JWT.
- Fail-closed: any misconfiguration at startup serves 503 on every route.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import uvicorn
from agent_framework import Agent, AgentSession
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, Route
from starlette.requests import Request
from starlette.responses import JSONResponse

from keycloak_auth import (
    APPLICATION_SUFFIX,
    IS_KEYCLOAK_AUTHENABLED,
    KEYCLOAK_CLIENT_ID,
    KEYCLOAK_CLIENT_SECRET,
    KEYCLOAK_REALM,
    KEYCLOAK_URL,
    GatewayHeaderSwapMiddleware,
    PathMiddleware,
    apply_mcp_keycloak_auth,
)

from a2a.client import A2AClient, create_text_message_object
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Message,
    MessageSendParams,
    SendMessageRequest,
    Task,
)
from a2a.utils import get_message_text, new_agent_text_message

from src.tools.uploaded_files import reset_file_context, set_file_context

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8088
DEFAULT_MCP_PATH = "/mcp"
DEFAULT_A2A_PATH = "/a2a"


# ---------------------------------------------------------------------------
# Service token cache for MCP->A2A loopback calls
# ---------------------------------------------------------------------------

_svc_token: dict[str, Any] = {"token": None, "expires_at": 0.0}


async def _get_service_token() -> str | None:
    """Fetch a cached client_credentials JWT for the internal MCP->A2A loopback."""
    if not all([KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_CLIENT_ID, KEYCLOAK_CLIENT_SECRET]):
        return None
    now = time.monotonic()
    if _svc_token["token"] and now < _svc_token["expires_at"]:
        return _svc_token["token"]
    token_url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(token_url, data={
                "grant_type": "client_credentials",
                "client_id": KEYCLOAK_CLIENT_ID,
                "client_secret": KEYCLOAK_CLIENT_SECRET,
            })
        if resp.status_code == 200:
            payload = resp.json()
            token = payload.get("access_token")
            expires_in = int(payload.get("expires_in", 300))
            _svc_token.update({"token": token, "expires_at": now + expires_in - 30})
            return token
        logger.error("Service token fetch failed (%s): %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.error("Service token error: %s", exc)
    return None


# ---------------------------------------------------------------------------
# A2A layer - wrap the MAF agent as an A2A server
# ---------------------------------------------------------------------------

def _agent_text(response: Any) -> str:
    """Best-effort extraction of an agent run's final text output."""
    text = getattr(response, "text", None)
    return str(text) if text else (str(response) if response is not None else "")


class _MafAgentExecutor(AgentExecutor):
    """A2A executor that runs the Microsoft Agent Framework agent."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._sessions: dict[str, AgentSession] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        query = context.get_user_input()

        # Bind uploaded-file coordinates (if the caller attached files) so the
        # agent's list_uploaded_files / read_uploaded_file tools read them from
        # blob storage for this request. No-op when no files were attached.
        message = getattr(context, "message", None)
        metadata = getattr(message, "metadata", None) or {}
        thread_id = metadata.get("thread_id")
        conversation_id = metadata.get("conversation_id")
        file_context_token = None
        if thread_id and conversation_id:
            file_context_token = set_file_context(
                thread_id=str(thread_id),
                conversation_id=str(conversation_id),
                filenames=metadata.get("attached_files") or [],
            )

        # Reuse the same AgentSession for each context_id so the MAF framework
        # carries conversation history across turns via its InMemoryHistoryProvider.
        context_id = context.context_id or str(uuid4())
        session = self._sessions.setdefault(context_id, AgentSession())

        try:
            response = await self._agent.run(query, session=session)
        finally:
            # Always clear the per-request file binding so it never leaks into a
            # later turn/request that did not attach files.
            if file_context_token is not None:
                reset_file_context(file_context_token)

        await event_queue.enqueue_event(
            new_agent_text_message(
                _agent_text(response),
                context_id=context.context_id,
                task_id=context.task_id,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # The MAF agent runs to completion; nothing to cancel.
        raise NotImplementedError("Cancellation is not supported.")


def _build_agent_card(agent: Agent, public_url: str) -> AgentCard:
    name = getattr(agent, "name", None) or "PlatformOrchestrator"
    description = (
        getattr(agent, "description", None)
        or "Microsoft Agent Framework supervisor agent exposed over A2A."
    )
    return AgentCard(
        name=name,
        description=description,
        url=public_url,
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[
            AgentSkill(
                id="orchestrate",
                name="orchestrate",
                description="Delegate a request to the hierarchical agent platform.",
                tags=["orchestration", "supervisor"],
            )
        ],
    )


def _build_a2a_app(agent: Agent, public_url: str) -> Starlette:
    handler = DefaultRequestHandler(
        agent_executor=_MafAgentExecutor(agent),
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(
        agent_card=_build_agent_card(agent, public_url),
        http_handler=handler,
    ).build()


# ---------------------------------------------------------------------------
# FastMCP MCP layer - a Streamable HTTP FastMCP server whose tool forwards over A2A
# ---------------------------------------------------------------------------

def _a2a_result_text(result: Any) -> str:
    """Pull text out of an A2A ``message/send`` result (Message or Task)."""
    if isinstance(result, Message):
        return get_message_text(result)
    if isinstance(result, Task):
        parts: list[str] = []
        for artifact in result.artifacts or []:
            for part in artifact.parts:
                text = getattr(part.root, "text", None)
                if text:
                    parts.append(text)
        if parts:
            return "\n".join(parts)
        status_msg = getattr(result.status, "message", None)
        if status_msg is not None:
            return get_message_text(status_msg)
    return str(result)


def _build_fastmcp_server(agent: Agent, a2a_rpc_url: str) -> FastMCP:
    """FastMCP server exposing a single tool that forwards requests over A2A."""
    mcp = FastMCP(
        name=getattr(agent, "name", None) or "PlatformOrchestrator",
        instructions="Forwards requests to the agent platform over A2A.",
    )

    # Maps caller-supplied session_id -> stable A2A context_id so that multiple
    # tool calls within the same conversation are grouped as one A2A context.
    _context_ids: dict[str, str] = {}

    @mcp.tool()
    async def ask_platform_orchestrator(
        request: str,
        session_id: str = "",
        thread_id: str = "",
        conversation_id: str = "",
        attached_files: list[str] | None = None,
    ) -> str:
        """Delegate a request to the hierarchical agent platform.

        Pass thread_id + conversation_id (and attached_files) to let the agent
        read files the user uploaded to blob storage for this conversation.
        """
        query = request.strip()
        sid = session_id.strip() or None
        tid = thread_id.strip() or None
        cid = conversation_id.strip() or None
        files = attached_files or []
        if isinstance(files, str):
            files = [files]

        # Append a file hint so the agent deterministically calls the file tools.
        if files:
            query = f"{query}\n\n[Attached files: {', '.join(files)}]"

        if sid:
            ctx_id = _context_ids.get(sid)
            if ctx_id is None:
                ctx_id = str(uuid4())
                _context_ids[sid] = ctx_id
        else:
            ctx_id = None

        msg = create_text_message_object(content=query)
        if ctx_id:
            msg.context_id = ctx_id
        if tid and cid:
            msg.metadata = {
                "thread_id": tid,
                "conversation_id": cid,
                "attached_files": files,
            }

        # When Keycloak auth is enabled the local /a2a endpoint is also protected,
        # so attach a cached client-credentials service token to the loopback call.
        headers: dict[str, str] = {}
        if IS_KEYCLOAK_AUTHENABLED:
            svc_token = await _get_service_token()
            if not svc_token:
                raise RuntimeError(
                    "Keycloak auth is enabled but no service token could be obtained "
                    "for the internal MCP->A2A loopback call."
                )
            headers["Authorization"] = f"Bearer {svc_token}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0), headers=headers) as http:
            a2a_client = A2AClient(httpx_client=http, url=a2a_rpc_url)
            a2a_req = SendMessageRequest(
                id=str(uuid4()),
                params=MessageSendParams(message=msg),
            )
            response = await a2a_client.send_message(a2a_req)

        root = response.root
        if hasattr(root, "error"):
            raise RuntimeError(f"A2A error: {root.error.message}")
        return _a2a_result_text(root.result)

    return mcp


# ---------------------------------------------------------------------------
# Fail-closed placeholder
# ---------------------------------------------------------------------------

def _deny_all_app() -> Starlette:
    """503 for every request when required Keycloak auth could not be initialized."""
    async def _deny(_request: Request) -> JSONResponse:
        return JSONResponse({"error": "authentication_unavailable"}, status_code=503)

    return Starlette(routes=[
        Route("/{path:path}", _deny,
              methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    ])


# ---------------------------------------------------------------------------
# Combined ASGI app: /mcp (FastMCP) + /a2a (A2A) on one server
# ---------------------------------------------------------------------------

def create_mcp_a2a_app(
    agent: Agent,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    mcp_path: str = DEFAULT_MCP_PATH,
    a2a_path: str = DEFAULT_A2A_PATH,
):
    """Build the combined FastMCP + A2A ASGI app wrapping ``agent``.

    When Keycloak auth is enabled ([auth.keycloak] isAuthEnabled=true):
    - FastMCP's Keycloak OAuthProxy handles the browser OAuth code flow + JWT
      fallback for MCP clients on /mcp.
    - GatewayHeaderSwapMiddleware (outermost) restores the caller's Keycloak token
      on GCP API Gateway deployments; it is a no-op elsewhere.
    - Fail-closed: apply_mcp_keycloak_auth validates credentials and raises on any
      misconfiguration; the except block serves 503 on every route instead.
    """
    advertise_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    suffix = APPLICATION_SUFFIX.rstrip("/")
    mcp_full_path = f"{suffix}{mcp_path}" if suffix else mcp_path
    a2a_full_path = f"{suffix}{a2a_path}" if suffix else a2a_path
    a2a_public_url = f"http://{advertise_host}:{port}{a2a_full_path}/"

    a2a_app = _build_a2a_app(agent, a2a_public_url)
    mcp = _build_fastmcp_server(agent, a2a_rpc_url=a2a_public_url)

    # Apply Keycloak OAuthProxy + UserMiddleware to the FastMCP instance.
    # Must be called before mcp.http_app() so auth is baked into the built app.
    try:
        well_known_routes = apply_mcp_keycloak_auth(mcp, mcp_path)
    except Exception:
        logger.critical("Keycloak auth setup failed; serving deny-all 503.")
        return GatewayHeaderSwapMiddleware(_deny_all_app())

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "mcp_url": f"http://{advertise_host}:{port}{mcp_full_path}",
            "a2a_url": a2a_public_url,
        })

    if not APPLICATION_SUFFIX:
        # Cloud-Run-behind-gateway layout: serve FastMCP directly at mcp_path.
        # Prepend health + A2A routes so they're matched before the MCP mount.
        mcp_http_app = mcp.http_app(path=mcp_path, transport="streamable-http", stateless_http=True)
        mcp_http_app.router.routes[0:0] = [
            Route("/", health, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Mount(a2a_path, app=a2a_app),
        ]
        for route in well_known_routes:
            logger.debug("Adding well-known auth route: %s", getattr(route, "path", route))
            mcp_http_app.router.routes.append(route)
        return GatewayHeaderSwapMiddleware(mcp_http_app)

    # GKE / host-style layout: mount under APPLICATION_SUFFIX with MCP at mcp_path
    # and A2A at a2a_path, both nested under the application suffix.
    inner_mcp = mcp.http_app(path="/", transport="streamable-http", stateless_http=True)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with inner_mcp.lifespan(inner_mcp):
            logger.info("MCP endpoint ready at %s%s", APPLICATION_SUFFIX, mcp_path)
            yield

    mounted = Starlette(routes=[
        Route("/", health, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Mount(mcp_path, app=inner_mcp),
        Mount(a2a_path, app=a2a_app),
    ])
    http_app = Starlette(
        routes=[Mount(APPLICATION_SUFFIX, app=mounted)],
        lifespan=_lifespan,
        middleware=[Middleware(PathMiddleware)],
    )
    for route in well_known_routes:
        logger.debug("Adding well-known auth route: %s", getattr(route, "path", route))
        http_app.router.routes.append(route)

    return GatewayHeaderSwapMiddleware(http_app)


async def run_mcp_a2a_server(
    agent: Agent,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Serve the FastMCP -> A2A -> MAF pipeline with uvicorn (async, awaitable)."""
    host = host or os.getenv("HOST", DEFAULT_HOST)
    port = int(port or os.getenv("PORT", DEFAULT_PORT))
    mcp_path = os.getenv("MCP_PATH", DEFAULT_MCP_PATH)
    a2a_path = os.getenv("A2A_PATH", DEFAULT_A2A_PATH)

    app = create_mcp_a2a_app(agent, host=host, port=port, mcp_path=mcp_path, a2a_path=a2a_path)

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    suffix = APPLICATION_SUFFIX.rstrip("/")
    shown_mcp_path = f"{suffix}{mcp_path}" if suffix else mcp_path
    shown_a2a_path = f"{suffix}{a2a_path}" if suffix else a2a_path
    print(f"MCP URL:  http://{shown_host}:{port}{shown_mcp_path}")
    print(f"A2A URL:  http://{shown_host}:{port}{shown_a2a_path}/")

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()
