"""
Google OAuth relay for the generated agent's MCP server.

Replaces mcp_keycloak_auth. The MCP server acts as the OAuth Authorization
Server and relays the full authorization_code flow to Google. Token validation
calls Google's userinfo endpoint — no local key material needed.

Wiring (called from the generated main.py):
    from google_oauth import build_mcp_http_app, GatewayHeaderSwapMiddleware
    http_app = build_mcp_http_app(mcp, MCP_SERVER_PATH)
    uvicorn.run(GatewayHeaderSwapMiddleware(http_app), ...)

Required env vars (injected by the deployer after gateway creation):
    MCP_BASE_URL           — public HTTPS root of this service (no trailing slash)
    OAUTH_CLIENT_ID        — Google OAuth 2.0 Web client ID
    OAUTH_CLIENT_SECRET    — Google OAuth 2.0 client secret
    ALLOWED_EMAIL_DOMAINS  — comma-separated (default: accenture.com)
    GOOGLE_AUTH_ENABLED    — "true" / "false" (default: true)
"""

import os
import secrets
import logging
import base64
import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from configparser import ConfigParser
from urllib.parse import urlencode, urlparse, parse_qs

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Scope, Receive, Send

from user_middleware import UserMiddleware

logger = logging.getLogger("generated_agent")

# ── Config ────────────────────────────────────────────────────────────────────

_config = ConfigParser()
_config.read(Path(__file__).resolve().parent / "config.ini")


def _g(key: str, env: str, fallback: str = "") -> str:
    """Read [auth.google] from config; fall back to env var if value is a placeholder."""
    val = _config.get("auth.google", key, fallback="").strip()
    if val and "{" not in val:
        return val
    return os.getenv(env, fallback)


IS_AUTH_ENABLED = _g("isAuthEnabled", "GOOGLE_AUTH_ENABLED", "true").lower() == "true"
MCP_BASE_URL    = _g("domain", "MCP_BASE_URL", "")
OAUTH_CLIENT_ID     = _g("client_id", "OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = _g("client_secret", "OAUTH_CLIENT_SECRET", "")
ALLOWED_EMAIL_DOMAINS = tuple(
    d.strip().lower()
    for d in _g("allowed_domains", "ALLOWED_EMAIL_DOMAINS", "accenture.com").split(",")
    if d.strip()
)
APPLICATION_SUFFIX = _config.get("mcp", "application_suffix", fallback="")

# In-memory state store for the OAuth authorization relay (proxy_state → VS Code state)
_oauth_state_store: dict[str, dict] = {}


# ── Token Verifier ────────────────────────────────────────────────────────────

class GoogleTokenVerifier(TokenVerifier):
    """Validates Google OAuth access tokens via Google's userinfo endpoint."""

    _USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

    def __init__(self):
        super().__init__(base_url=MCP_BASE_URL or None, required_scopes=["openid", "email"])

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    self._USERINFO_URL,
                    headers={"Authorization": f"Bearer {token}"},
                )

            if resp.status_code != 200:
                logger.warning("Google userinfo returned %d", resp.status_code)
                return None

            info = resp.json()
            email = info.get("email", "")
            sub   = info.get("sub", "")

            if not sub:
                logger.warning("Google userinfo missing 'sub' claim")
                return None

            if ALLOWED_EMAIL_DOMAINS:
                if not any(email.lower().endswith("@" + d) for d in ALLOWED_EMAIL_DOMAINS):
                    logger.warning("Rejecting token for %s: domain not in allow-list", email)
                    return None

            logger.info("Authenticated user: %s", email)
            return AccessToken(
                token=token,
                client_id=OAUTH_CLIENT_ID,
                scopes=["openid", "email", "profile"],
                claims={"email": email, "sub": sub},
            )

        except httpx.HTTPError as exc:
            logger.warning("Failed to verify Google token: %s", exc)
            return None


# ── OAuth Route Handlers ──────────────────────────────────────────────────────

def _is_safe_redirect(uri: str) -> bool:
    """Prevent open-redirect: allow VS Code scheme and known localhost/OAuth hosts."""
    parsed = urlparse(uri)
    if parsed.scheme == "vscode":
        return True
    if parsed.scheme in ("http", "https") and parsed.hostname in (
        "127.0.0.1", "localhost", "::1",
        "oauth.pstmn.io", "claude.ai", "claude.com",
        "vscode.dev",
    ):
        return True
    return False


async def _well_known_protected_resource(request: Request) -> JSONResponse:
    return JSONResponse({
        "resource": f"{MCP_BASE_URL}/mcp",
        "authorization_servers": [MCP_BASE_URL],
        "scopes_supported": ["openid", "email", "profile"],
        "bearer_methods_supported": ["header"],
    })


async def _well_known_authorization_server(request: Request) -> JSONResponse:
    return JSONResponse({
        "issuer": MCP_BASE_URL,
        "authorization_endpoint": f"{MCP_BASE_URL}/authorize",
        "token_endpoint": f"{MCP_BASE_URL}/token",
        "registration_endpoint": f"{MCP_BASE_URL}/register",
        "revocation_endpoint": "https://oauth2.googleapis.com/revoke",
        "scopes_supported": ["openid", "email", "profile"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post", "client_secret_basic", "none",
        ],
    })


async def _oauth_register(request: Request) -> JSONResponse:
    """Stub DCR (RFC 7591). Returns the pre-configured Google client credentials."""
    if request.method == "OPTIONS":
        return JSONResponse({}, status_code=204)
    try:
        body = await request.json()
    except Exception:
        body = {}
    redirect_uris = body.get("redirect_uris") or []
    # No redirect_uri validation here — DCR just registers the client.
    # The open-redirect check is enforced in /authorize where the redirect actually fires.
    return JSONResponse({
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
        "client_id_issued_at": 0,
        "client_secret_expires_at": 0,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }, status_code=201)


async def _oauth_authorize(request: Request):
    """Relay the authorization request to Google's consent screen."""
    params = dict(request.query_params)
    vscode_redirect = params.get("redirect_uri", "")
    vscode_state    = params.get("state", "")

    if not vscode_redirect or not _is_safe_redirect(vscode_redirect):
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Invalid redirect_uri"},
            status_code=400,
        )

    proxy_state = secrets.token_urlsafe(32)
    _oauth_state_store[proxy_state] = {"redirect_uri": vscode_redirect, "state": vscode_state}

    google_params = {
        "client_id":     params.get("client_id", OAUTH_CLIENT_ID),
        "redirect_uri":  f"{MCP_BASE_URL}/callback",
        "response_type": "code",
        "scope":         params.get("scope", "openid email profile"),
        "state":         proxy_state,
        "access_type":   "offline",
        "prompt":        "consent",
    }
    if "code_challenge" in params:
        google_params["code_challenge"] = params["code_challenge"]
    if "code_challenge_method" in params:
        google_params["code_challenge_method"] = params["code_challenge_method"]

    google_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(google_params)}"
    return RedirectResponse(url=google_url, status_code=302)


async def _oauth_callback(request: Request):
    """Receive Google's authorization code and relay it back to VS Code."""
    params      = dict(request.query_params)
    proxy_state = params.get("state", "")
    code        = params.get("code", "")
    error       = params.get("error", "")

    stored = _oauth_state_store.pop(proxy_state, None)
    if not stored:
        return JSONResponse(
            {"error": "invalid_state", "error_description": "Unknown or expired state"},
            status_code=400,
        )

    vscode_redirect = stored["redirect_uri"]
    vscode_state    = stored["state"]

    if error:
        return RedirectResponse(
            url=f"{vscode_redirect}?{urlencode({'error': error, 'state': vscode_state})}",
            status_code=302,
        )
    return RedirectResponse(
        url=f"{vscode_redirect}?{urlencode({'code': code, 'state': vscode_state})}",
        status_code=302,
    )


async def _oauth_token(request: Request) -> JSONResponse:
    """Proxy the token exchange to Google's token endpoint."""
    body = await request.body()
    token_params = {
        k: v[0]
        for k, v in parse_qs(body.decode(), keep_blank_values=True).items()
    }

    # RFC 6749 §2.3.1: credentials may arrive via HTTP Basic auth header
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            basic_id, _, basic_secret = decoded.partition(":")
            token_params.setdefault("client_id", basic_id)
            token_params.setdefault("client_secret", basic_secret)
        except Exception:
            pass

    if token_params.get("grant_type") == "authorization_code":
        token_params["redirect_uri"] = f"{MCP_BASE_URL}/callback"

    token_params.setdefault("client_id", OAUTH_CLIENT_ID)
    token_params.setdefault("client_secret", OAUTH_CLIENT_SECRET)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post("https://oauth2.googleapis.com/token", data=token_params)
    return JSONResponse(resp.json(), status_code=resp.status_code)


def _get_oauth_routes() -> list:
    return [
        Route("/.well-known/oauth-protected-resource",  _well_known_protected_resource,  methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", _well_known_authorization_server, methods=["GET"]),
        Route("/register",  _oauth_register,  methods=["POST", "OPTIONS"]),
        Route("/authorize", _oauth_authorize, methods=["GET"]),
        Route("/callback",  _oauth_callback,  methods=["GET"]),
        Route("/token",     _oauth_token,     methods=["POST"]),
    ]


# ── ASGI Middleware ───────────────────────────────────────────────────────────

class GatewayHeaderSwapMiddleware:
    """ASGI middleware: copy X-Forwarded-Authorization -> Authorization.

    GCP API Gateway replaces the client's Authorization with its own
    service-account JWT and copies the original to X-Forwarded-Authorization.
    Must wrap the OUTERMOST layer of the ASGI app so it runs before FastMCP auth.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            raw_headers = list(scope.get("headers", []))
            forwarded = None
            for name, value in raw_headers:
                if name == b"x-forwarded-authorization":
                    forwarded = value
                    break
            if forwarded:
                new_headers = [
                    (b"authorization", forwarded) if name == b"authorization" else (name, value)
                    for name, value in raw_headers
                ]
                scope = dict(scope, headers=new_headers)
        await self.app(scope, receive, send)


class _PathMiddleware:
    """Strip trailing slash from oauth-authorization-server discovery path."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and "oauth-authorization-server" in scope.get("path", ""):
            scope["path"] = scope["path"].rstrip("/")
        await self.app(scope, receive, send)


# ── App Builder ───────────────────────────────────────────────────────────────

def build_mcp_http_app(mcp: FastMCP, mcp_path: str) -> Starlette:
    """Wire Google OAuth into the FastMCP instance and return the ASGI app.

    Flat-mount (Cloud Run / API Gateway, APPLICATION_SUFFIX=""): the MCP app
    is served directly at mcp_path; OAuth routes sit at the root. Wrap the
    returned app in GatewayHeaderSwapMiddleware before passing to uvicorn.

    Nested-mount (GKE, APPLICATION_SUFFIX set): routes are mounted under the
    full APPLICATION_SUFFIX path.
    """
    if IS_AUTH_ENABLED and OAUTH_CLIENT_ID:
        mcp.auth = GoogleTokenVerifier()
        mcp.add_middleware(UserMiddleware())
        logger.info("Google OAuth authentication enabled (domain: %s)", MCP_BASE_URL)
    else:
        logger.warning("Google OAuth authentication DISABLED.")

    oauth_routes = _get_oauth_routes() if IS_AUTH_ENABLED else []

    if not APPLICATION_SUFFIX:
        http_app = mcp.http_app(path=mcp_path, transport="streamable-http", stateless_http=True)
        for route in oauth_routes:
            http_app.router.routes.append(route)
        return http_app

    # GKE nested-mount layout
    inner_app = mcp.http_app(path="/", transport="streamable-http", stateless_http=True)

    @asynccontextmanager
    async def _lifespan(app):
        async with inner_app.lifespan(inner_app):
            yield

    mounted = Starlette(routes=[Mount(mcp_path, app=inner_app)])
    http_app = Starlette(
        routes=[Mount(APPLICATION_SUFFIX, app=mounted)],
        lifespan=_lifespan,
        middleware=[Middleware(_PathMiddleware)],
    )
    for route in oauth_routes:
        http_app.router.routes.append(route)
    return http_app
