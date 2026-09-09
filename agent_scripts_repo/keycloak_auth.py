"""
Keycloak OAuth front-door auth for a generated agent's MCP server.

Lifted from the ADLC new_host_agent reinvgw_mcpserver/main.py auth block.
Packaged as apply_mcp_keycloak_auth(mcp, suffix): builds the OAuthProxy
(browser OAuth code flow) + JWTVerifier fallback, attaches it to the FastMCP
instance, adds UserMiddleware (extracts the authenticated user's email), and
returns the Keycloak well-known routes to append to the HTTP app.

Integration adaptations (only what lift-and-shift requires):
- config loaded with ConfigParser from the generated agent's root config.ini;
- OAuthProxy base_url derived from [auth.keycloak] domain + the MCP suffix;
- token-store imports made resilient (fall back to in-memory / no store).

Requires: fastmcp, httpx, PyJWT[crypto]
"""

import os
import time
import logging
import httpx
from pathlib import Path
from configparser import ConfigParser

from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProxy, AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.middleware import Middleware
from starlette.authentication import AuthenticationError
from starlette.types import ASGIApp, Scope, Receive, Send

from user_middleware import UserMiddleware

logger = logging.getLogger("generated_agent")

_config = ConfigParser()
_config.read(Path(__file__).resolve().parent / "config.ini")

def _kc(key: str, env: str, fallback: str = "") -> str:
    """Prefer config.ini [auth.keycloak]; fall back to an env var. Unresolved
    deploy placeholders like {MyWizard...} are ignored so the env value wins.
    This lets `domain` be injected as the MCP_BASE_URL env var AFTER the API
    Gateway is created (`gcloud run services update --update-env-vars ...`),
    without editing config.ini or rebuilding the image."""
    val = _config.get("auth.keycloak", key, fallback="").strip()
    if val and "{" not in val:
        return val
    return os.getenv(env, fallback)


IS_KEYCLOAK_AUTHENABLED = _kc("isAuthEnabled", "KEYCLOAK_AUTH_ENABLED", "true").strip().lower() == "true"

KEYCLOAK_CLIENT_ID = _kc("client_id", "KEYCLOAK_CLIENT_ID")
KEYCLOAK_CLIENT_SECRET = _kc("client_secret", "KEYCLOAK_CLIENT_SECRET")
KEYCLOAK_URL = _kc("url", "KEYCLOAK_URL")
KEYCLOAK_REALM = _kc("realm", "KEYCLOAK_REALM")
KEYCLOAK_DOMAIN = _kc("domain", "MCP_BASE_URL")
# ADLC gateway prefix (matches new_host_agent [newhost] application_suffix).
APPLICATION_SUFFIX = _config.get("mcp", "application_suffix", fallback="")

try:
    from key_value.aio.stores.redis import RedisStore as _RedisStore
    _REDIS_STORE_AVAILABLE = True
except ImportError:
    _REDIS_STORE_AVAILABLE = False

try:
    from key_value.aio.stores.memory import MemoryStore as _MemoryStore
    _MEMORY_STORE_AVAILABLE = True
except ImportError:
    _MEMORY_STORE_AVAILABLE = False


def _has_unresolved_placeholders(*values: str) -> bool:
    return any("{" in v and "}" in v for v in values if v)


def _build_redis_storage():
    """Redis-backed AsyncKeyValue store for OAuthProxy client_storage, shared across pods.

    Falls back to MemoryStore (single-instance) when Redis is not configured/unreachable,
    and to None (OAuthProxy default) when the key_value package is unavailable.
    """
    _fallback = _MemoryStore() if _MEMORY_STORE_AVAILABLE else None
    if not _REDIS_STORE_AVAILABLE:
        logger.warning("key_value redis extra not installed; OAuthProxy using in-memory token store (single-instance only).")
        return _fallback
    try:
        redis_endpoint = _config.get("Redis", "redisEndpoint", fallback="")
        redis_port_str = _config.get("Redis", "port", fallback="6379")
        redis_password = _config.get("Redis", "authToken", fallback="")
        ssl_str = _config.get("Redis", "isSSLEnabled", fallback="false")

        if not redis_endpoint or _has_unresolved_placeholders(redis_endpoint):
            logger.warning("Redis not configured; OAuthProxy using in-memory token store (single-instance only).")
            return _fallback

        ssl_enabled = ssl_str.strip().lower() in ("true", "1", "yes")
        password = redis_password if redis_password and not _has_unresolved_placeholders(redis_password) else None

        import redis as _redis_test
        _test_client = _redis_test.Redis(
            host=redis_endpoint,
            port=int(redis_port_str),
            password=password,
            ssl=ssl_enabled,
            socket_connect_timeout=3,
        )
        _test_client.ping()
        _test_client.close()

        logger.info("OAuthProxy: using shared Redis token store at %s:%s", redis_endpoint, redis_port_str)
        return _RedisStore(host=redis_endpoint, port=int(redis_port_str), password=password, ssl=ssl_enabled)
    except Exception as exc:
        logger.warning("Failed to create Redis token store (%s); OAuthProxy using in-memory token store.", exc)
        return _fallback


def validate_keycloak_credentials() -> bool:
    """Validate Keycloak credentials by attempting a client_credentials token fetch."""
    try:
        if not all([KEYCLOAK_CLIENT_ID, KEYCLOAK_CLIENT_SECRET, KEYCLOAK_URL, KEYCLOAK_REALM]):
            logger.error("Missing required Keycloak configuration parameters")
            return False
        if _has_unresolved_placeholders(KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_CLIENT_ID, KEYCLOAK_CLIENT_SECRET):
            logger.error("Keycloak config contains unresolved placeholders. Auth DISABLED until replaced.")
            return False

        token_url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": KEYCLOAK_CLIENT_ID,
            "client_secret": KEYCLOAK_CLIENT_SECRET,
        }
        logger.info(f"Validating Keycloak credentials at: {token_url}")
        with httpx.Client(timeout=10.0) as client:
            response = client.post(token_url, data=data)
        if response.status_code == 200:
            logger.info("Keycloak credentials validated successfully")
            return True
        logger.error(f"Keycloak credential validation failed with status {response.status_code}: {response.text[:200]}")
        return False
    except httpx.TimeoutException:
        logger.error(f"Keycloak validation timeout - unable to reach {KEYCLOAK_URL}")
        return False
    except httpx.RequestError as e:
        logger.error(f"Keycloak validation request error: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error during Keycloak credential validation: {str(e)}")
        return False


def get_keycloak_auth(suffix: str) -> OAuthProxy:
    token_verifier = JWTVerifier(
        jwks_uri=f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs",
        issuer=f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}",
        required_scopes=["email", "openid", "profile", "microprofile-jwt"],
    )

    # For flat-mount (Cloud Run/Gateway, APPLICATION_SUFFIX="") the OAuth routes
    # are registered at the app root, so base_url must be the domain root.
    # For nested-mount (GKE, APPLICATION_SUFFIX set) they sit under the full path.
    if APPLICATION_SUFFIX:
        oauth_base = f"{KEYCLOAK_DOMAIN}{APPLICATION_SUFFIX}{suffix}/"
    else:
        oauth_base = f"{KEYCLOAK_DOMAIN}/"

    auth = OAuthProxy(
        upstream_authorization_endpoint=f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/auth",
        upstream_token_endpoint=f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token",
        upstream_client_id=KEYCLOAK_CLIENT_ID,
        upstream_client_secret=KEYCLOAK_CLIENT_SECRET,
        token_verifier=token_verifier,
        base_url=oauth_base,
        redirect_path="/callback",
        require_authorization_consent=False,
        client_storage=_build_redis_storage(),
    )

    _original_verify = auth.verify_token

    async def _verify_with_fallback(token: str) -> AccessToken | None:
        result = await _original_verify(token)
        if result is not None:
            return result
        return await token_verifier.verify_token(token)

    auth.verify_token = _verify_with_fallback
    return auth


def apply_mcp_keycloak_auth(mcp: FastMCP, suffix: str) -> list:
    """Attach Keycloak OAuth + UserMiddleware to the MCP instance.

    Returns the Keycloak well-known routes to append to the HTTP app (empty if
    auth is disabled or credentials are not configured). Must be called BEFORE
    mcp.http_app(...) so the auth provider is baked into the built app.
    """
    well_known_routes: list = []
    try:
        if not IS_KEYCLOAK_AUTHENABLED:
            logger.warning("Keycloak auth is disabled. NOT recommended for production.")
            return well_known_routes

        if not validate_keycloak_credentials():
            raise AuthenticationError("Keycloak credentials are not properly configured. Failed to initialize Keycloak")

        auth = get_keycloak_auth(suffix)
        if auth is not None:
            # Pass the MCP mount path so the protected-resource metadata advertises
            # resource=<domain><suffix> (RFC 8707/9728), matching what OAuthProxy
            # validates and what the client sends. Without the path the metadata
            # advertises the bare domain and the server rejects it as invalid_target.
            routes = auth.get_well_known_routes(suffix)
            logger.debug(f"Registering auth routes for {suffix}: {[route.path for route in routes]}")
            well_known_routes.extend(routes)
            mcp.auth = auth
            mcp.add_middleware(UserMiddleware())
        logger.info("Keycloak authentication applied to MCP server")
        return well_known_routes
    except Exception:
        # FAIL CLOSED: auth is required (isAuthEnabled=true) but setup failed;
        # re-raise so build_mcp_http_app serves a deny-all app instead of the
        # unauthenticated MCP app.
        import traceback
        logger.critical(f"Keycloak auth setup FAILED; refusing to serve unauthenticated: {traceback.format_exc()}")
        raise


class PathMiddleware:
    """Normalise the oauth-authorization-server discovery path (strip trailing
    slash) so MCP clients (e.g. VS Code Copilot) resolve the metadata route.
    Lifted from new_host_agent reinvgw_mcpserver/main.py.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if "oauth-authorization-server" in path:
            scope["path"] = path.rstrip("/")
        await self.app(scope, receive, send)


class GatewayHeaderSwapMiddleware:
    """ASGI middleware: copy X-Forwarded-Authorization -> Authorization.

    On Cloud Run behind a GCP API Gateway, the gateway replaces the client's
    Authorization with its own run.invoker service-account JWT (to satisfy Cloud
    Run "require-authentication" ingress) and forwards the original Keycloak
    token in X-Forwarded-Authorization. This middleware (outermost, before
    FastMCP's auth) swaps it back so the Keycloak OAuthProxy/JWTVerifier sees the
    user's Keycloak token. No-op on GKE (header absent).
    """

    def __init__(self, app):
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
                has_auth = any(name == b"authorization" for name, _ in raw_headers)
                if has_auth:
                    new_headers = [
                        (b"authorization", forwarded) if name == b"authorization" else (name, value)
                        for name, value in raw_headers
                    ]
                else:
                    new_headers = raw_headers + [(b"authorization", forwarded)]
                scope = dict(scope, headers=new_headers)
        await self.app(scope, receive, send)


def _auth_unavailable_app() -> Starlette:
    """Fail-closed placeholder: 503 for every request when required Keycloak
    auth could not be initialized, so the MCP app is never served open."""
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def _deny(request):
        return JSONResponse({"error": "authentication_unavailable"}, status_code=503)

    return Starlette(routes=[Route("/{path:path}", _deny,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])])


def build_mcp_http_app(mcp: FastMCP, mcp_path: str) -> Starlette:
    """Build the MCP server's HTTP app exactly like the ADLC new_host_agent
    reinvgw_mcpserver: apply Keycloak OAuth + UserMiddleware, mount the FastMCP
    app at {APPLICATION_SUFFIX}{mcp_path}/ (path="/", stateless), attach
    PathMiddleware, and append the Keycloak well-known routes at the app root.

    Returns the outer Starlette app to run with uvicorn.
    """
    try:
        well_known_routes = apply_mcp_keycloak_auth(mcp, mcp_path)
    except Exception:
        # Fail CLOSED: auth required but unavailable -> deny-all (503), never the
        # unauthenticated MCP app. Port still binds so the startup probe passes.
        logger.critical("Keycloak auth unavailable; mounting deny-all 503 app.")
        return _auth_unavailable_app()

    if not APPLICATION_SUFFIX:
        # Cloud-Run-behind-gateway layout: serve the MCP app DIRECTLY at mcp_path
        # (no Starlette sub-mount). A sub-mount forces a trailing slash and
        # redirects the bare path, which the API Gateway path template can't
        # express. Serving directly keeps the endpoint at exactly {mcp_path},
        # matching the gateway route (same as the EnvisionWorkflow layout).
        http_app = mcp.http_app(path=mcp_path, transport="streamable-http", stateless_http=True)
        for route in well_known_routes:
            logger.debug("Adding well-known auth route: %s", getattr(route, "path", route))
            http_app.router.routes.append(route)
        return http_app

    # Nested (GKE / host-style) layout: mount under {APPLICATION_SUFFIX}{mcp_path}/.
    inner_app = mcp.http_app(path="/", transport="streamable-http", stateless_http=True)

    @asynccontextmanager
    async def _lifespan(app):
        async with inner_app.lifespan(inner_app):
            yield

    mounted = Starlette(routes=[Mount(mcp_path, app=inner_app)])
    http_app = Starlette(
        routes=[Mount(APPLICATION_SUFFIX, app=mounted)],
        lifespan=_lifespan,
        middleware=[Middleware(PathMiddleware)],
    )

    for route in well_known_routes:
        logger.debug("Adding well-known auth route: %s", getattr(route, "path", route))
        http_app.router.routes.append(route)

    return http_app
