"""
auth_handler.py
---------------
Authentication handling using the Strategy pattern.

Two concrete strategies:
  - QppTokenAuthStrategy  →  POST {username, password} → JWT (`apiToken` header)
  - ApiKeyAuthStrategy    →  static API key (e.g. `x-api-key` for Vertex)

Credential source is controlled by ``auth_profile["is_secret_store_enabled"]``:
  True  (default) → fetch from the secret store, slot matched by service endpoint
  False           → fetch from auth_profile["auth_config"] (stored in DB)

Secret store layout (multiple indexed slots per service type):
    quasar-1-endpoint     →  https://qpp-host-1/...
    quasar-1-tokenurl     →  https://qpp-host-1/api/auth/token
    quasar-1-username     →  <username>
    quasar-1-password     →  <password>
    gcpv-1-endpoint       →  https://vertex-host-1/...
    gcpv-1-key2           →  <api-key>

Prefixes (``quasar`` / ``gcpv``) are fixed by secret-store convention.
The active secret-store provider is selected by which section of
``llmConfig.ini`` is populated (AzureSecretStore / AWSSecretStore /
GCPSecretStore) and is loaded via ``src.secret_providers``.
"""

import logging
import httpx
from abc import ABC, abstractmethod
from configparser import ConfigParser
from pathlib import Path

logger = logging.getLogger("tool_gateway.auth_handler")


# Hardcoded prefixes — match the secret-store convention.
_QPP_PREFIX      = "quasar"
_VERTEX_PREFIX   = "gcpv"
_BACKSTAGE_PREFIX = "backstage"


# ─────────────────────────────────────────────
# Lazy, cached secret loader
# ─────────────────────────────────────────────

_LLM_CFG_PATH = Path(__file__).resolve().parents[2] / "llmConfig.ini"
_secrets_cache: dict | None = None


def _build_provider():
    """Build the right SecretProvider from llmConfig.ini sections."""
    if not _LLM_CFG_PATH.exists():
        return None
    cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read(_LLM_CFG_PATH)

    if cfg.has_section("AzureSecretStore") and cfg.get("AzureSecretStore", "AzureKeyVaultUri", fallback="").strip():
        from src.secret_providers import AzureKeyVaultProvider
        return AzureKeyVaultProvider(
            vault_url        = cfg.get("AzureSecretStore", "AzureKeyVaultUri").strip(),
            tenant_id        = (cfg.get("AzureSecretStore", "AzureTenantId", fallback="") or "").strip() or None,
            client_id        = (cfg.get("AzureSecretStore", "AzureClientId", fallback="") or "").strip() or None,
            client_secret    = (cfg.get("AzureSecretStore", "AzureClientSecret", fallback="") or "").strip() or None,
            user_assigned_mi = (cfg.get("AzureSecretStore", "AzureUserAssignedManagedIdentity", fallback="") or "").strip() or None,
        )

    if cfg.has_section("AWSSecretStore") and cfg.get("AWSSecretStore", "AWSSecretName", fallback="").strip():
        from src.secret_providers import AWSSecretsManagerProvider
        return AWSSecretsManagerProvider(
            region_name   = cfg.get("AWSSecretStore", "AWSRegion", fallback="us-east-1").strip(),
            secret_name   = cfg.get("AWSSecretStore", "AWSSecretName").strip(),
            version_stage = cfg.get("AWSSecretStore", "AWSVersion", fallback="AWSCURRENT").strip(),
        )

    if cfg.has_section("GCPSecretStore") and cfg.get("GCPSecretStore", "GCPSecretName", fallback="").strip():
        from src.secret_providers import GCPSecretManagerJsonBlobProvider
        return GCPSecretManagerJsonBlobProvider(
            project_id  = cfg.get("GCPSecretStore", "ProjectId").strip(),
            secret_name = cfg.get("GCPSecretStore", "GCPSecretName").strip(),
            version_id  = cfg.get("GCPSecretStore", "VersionId", fallback="latest").strip(),
        )

    return None


def _get_secrets() -> dict:
    """Return all secrets as a flat dict, cached across calls."""
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache

    provider = _build_provider()
    if provider is None:
        logger.warning("No secret-store provider configured in llmConfig.ini")
        _secrets_cache = {}
        return _secrets_cache

    try:
        _secrets_cache = provider.fetch_all() or {}
    except Exception as e:
        logger.exception("Failed to fetch secrets from provider: %s", e)
        _secrets_cache = {}

    return _secrets_cache


def _find_slot_for_endpoint(prefix: str, endpoint: str) -> str | None:
    """Find the secret-store slot whose `{prefix}-N-endpoint` matches `endpoint`."""
    if not prefix or not endpoint:
        return None
    target = endpoint.rstrip("/")
    secrets = _get_secrets()
    for attr, value in secrets.items():
        if attr.startswith(f"{prefix}-") and attr.endswith("-endpoint"):
            if isinstance(value, str) and value.rstrip("/") == target:
                return attr[: -len("-endpoint")]   # e.g. "quasar-1"
    return None


# ─────────────────────────────────────────────
# Credential builders
# ─────────────────────────────────────────────

def _get_qpp_credentials(endpoint: str, auth_profile: dict | None = None) -> dict:
    """
    Returns QPP credentials.

    Flow:
      use_secret_store=False, database_enabled=True  → DB auth_config
      use_secret_store=False, database_enabled=False → llmConfig.ini [Quasar]; endpoint taken from config
      use_secret_store=True                          → secret store; fallback to llmConfig.ini [Quasar] on any failure
    """
    use_secret_store = (auth_profile or {}).get("is_secret_store_enabled", True)

    # Read database_enabled flag from llmConfig.ini
    _cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
    _cfg.read(_LLM_CFG_PATH)
    database_enabled = _cfg.getboolean("appsettings", "database_enabled", fallback=False)
    logger.info(
        "QPP credentials: use_secret_store=%s, database_enabled=%s, endpoint=%s",
        use_secret_store, database_enabled, endpoint,
    )

    if not use_secret_store:
        if database_enabled:
            logger.info("QPP credentials source: DB auth_config (use_secret_store=False, database_enabled=True)")
            db_creds = (auth_profile or {}).get("auth_config", {})
            if not (db_creds.get("token_url") and db_creds.get("username") and db_creds.get("password")):
                raise ValueError(
                    "QPP auth: is_secret_store_enabled=False but 'auth_config' "
                    "(token_url / username / password) are missing in DB auth_profile."
                )
            logger.info("QPP credentials successfully retrieved from DB auth_config")
            return {
                "token_url": db_creds["token_url"],
                "username":  db_creds["username"],
                "password":  db_creds["password"],
            }
        else:
            logger.info("QPP credentials source: llmConfig.ini [Quasar] (use_secret_store=False, database_enabled=False)")
            endpoint = _cfg.get("Quasar", "endpoint",   fallback="").strip()
            token_url    = _cfg.get("Quasar", "token_url",  fallback="").strip()
            username     = _cfg.get("Quasar", "username",   fallback="").strip()
            password     = _cfg.get("Quasar", "password",   fallback="").strip()
            if not (token_url and username and password):
                raise ValueError(
                    "QPP auth: database_enabled=False but [Quasar] section in llmConfig.ini "
                    "is missing token_url / username / password."
                )
            logger.info("QPP credentials successfully retrieved from llmConfig.ini [Quasar]")
            return {
                "token_url": token_url,
                "username":  username,
                "password":  password,
            }

    # use_secret_store=True: try secret store first, fall back to llmConfig.ini [Quasar] on any failure
    logger.info("QPP credentials source: secret store (use_secret_store=True)")
    try:
        slot = _find_slot_for_endpoint(_QPP_PREFIX, endpoint)
        if not slot:
            raise ValueError(f"No QPP slot matched endpoint '{endpoint}' in secret store")
        secrets = _get_secrets()
        token_url = secrets.get(f"{slot}-tokenurl", "")
        username  = secrets.get(f"{slot}-username", "")
        password  = secrets.get(f"{slot}-password", "")
        if not (token_url and username and password):
            raise ValueError(
                f"QPP slot '{slot}' in secret store is missing one or more of: tokenurl, username, password"
            )
        logger.info("QPP credentials successfully retrieved from secret store (slot=%s)", slot)
        return {
            "token_url": token_url,
            "username":  username,
            "password":  password,
        }
    except Exception as exc:
        logger.warning(
            "QPP secret store fetch failed — %s. Falling back to llmConfig.ini [Quasar].", exc
        )
        endpoint = _cfg.get("Quasar", "endpoint",   fallback="").strip()
        token_url    = _cfg.get("Quasar", "token_url",  fallback="").strip()
        username     = _cfg.get("Quasar", "username",   fallback="").strip()
        password     = _cfg.get("Quasar", "password",   fallback="").strip()
        if not (token_url and username and password):
            raise ValueError(
                "QPP auth: secret store fetch failed and [Quasar] fallback in llmConfig.ini "
                "is also missing token_url / username / password."
            )
        logger.info("QPP credentials successfully retrieved from llmConfig.ini [Quasar] (secret store fallback)")
        return {
            "token_url": token_url,
            "username":  username,
            "password":  password,
        }


def _get_vertex_credentials(endpoint: str, auth_profile: dict | None = None) -> dict:
    """
    Returns Vertex API-key credential.

    Flow:
      use_secret_store=False, database_enabled=True  → DB auth_config
      use_secret_store=False, database_enabled=False → llmConfig.ini [VertexAI]; endpoint taken from config
      use_secret_store=True                          → secret store; fallback to llmConfig.ini [VertexAI] on any failure
    """
    use_secret_store = (auth_profile or {}).get("is_secret_store_enabled", True)

    # Read database_enabled flag from llmConfig.ini
    _cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
    _cfg.read(_LLM_CFG_PATH)
    database_enabled = _cfg.getboolean("appsettings", "database_enabled", fallback=False)
    logger.info(
        "Vertex credentials: use_secret_store=%s, database_enabled=%s, endpoint=%s",
        use_secret_store, database_enabled, endpoint,
    )

    if not use_secret_store:
        if database_enabled:
            logger.info("Vertex credentials source: DB auth_config (use_secret_store=False, database_enabled=True)")
            db_creds = (auth_profile or {}).get("auth_config", {})
            if not db_creds.get("api_key"):
                raise ValueError(
                    "Vertex auth: is_secret_store_enabled=False but 'auth_config.api_key' "
                    "is missing in DB auth_profile."
                )
            logger.info("Vertex credentials successfully retrieved from DB auth_config")
            return {"api_key": db_creds["api_key"]}
        else:
            logger.info("Vertex credentials source: llmConfig.ini [VertexAI] (use_secret_store=False, database_enabled=False)")
            endpoint = _cfg.get("VertexAI", "endpoint", fallback="").strip()
            api_key      = _cfg.get("VertexAI", "api_key",  fallback="").strip()
            if not api_key:
                raise ValueError(
                    "Vertex auth: database_enabled=False but [VertexAI] section in llmConfig.ini "
                    "is missing api_key."
                )
            logger.info("Vertex credentials successfully retrieved from llmConfig.ini [VertexAI]")
            return {
                "api_key":  api_key,
            }

    # use_secret_store=True: try secret store first, fall back to llmConfig.ini [VertexAI] on any failure
    logger.info("Vertex credentials source: secret store (use_secret_store=True)")
    try:
        slot = _find_slot_for_endpoint(_VERTEX_PREFIX, endpoint)
        if not slot:
            raise ValueError(f"No Vertex slot matched endpoint '{endpoint}' in secret store")
        secrets = _get_secrets()
        api_key = secrets.get(f"{slot}-key2", "")
        if not api_key:
            raise ValueError(f"Vertex slot '{slot}' in secret store is missing key2")
        logger.info("Vertex credentials successfully retrieved from secret store (slot=%s)", slot)
        return {"api_key": api_key}
    except Exception as exc:
        logger.warning(
            "Vertex secret store fetch failed — %s. Falling back to llmConfig.ini [VertexAI].", exc
        )
        endpoint = _cfg.get("VertexAI", "endpoint", fallback="").strip()
        api_key      = _cfg.get("VertexAI", "api_key",  fallback="").strip()
        if not api_key:
            raise ValueError(
                "Vertex auth: secret store fetch failed and [VertexAI] fallback in llmConfig.ini "
                "is also missing api_key."
            )
        logger.info("Vertex credentials successfully retrieved from llmConfig.ini [VertexAI] (secret store fallback)")
        return {
            "api_key":  api_key,
        }


def _get_backstage_credentials(endpoint: str, auth_profile: dict | None = None) -> dict:
    """
    Returns Backstage MCP credentials.

    Flow:
      use_secret_store=False, database_enabled=True  → DB auth_config
      use_secret_store=False, database_enabled=False → llmConfig.ini [Backstage]; endpoint taken from config
      use_secret_store=True                          → secret store; fallback to llmConfig.ini [Backstage] on any failure
    """
    use_secret_store = (auth_profile or {}).get("is_secret_store_enabled", True)

    # Read database_enabled flag from llmConfig.ini
    _cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
    _cfg.read(_LLM_CFG_PATH)
    database_enabled = _cfg.getboolean("appsettings", "database_enabled", fallback=False)
    logger.info(
        "Backstage credentials: use_secret_store=%s, database_enabled=%s, endpoint=%s",
        use_secret_store, database_enabled, endpoint,
    )

    if not use_secret_store:
        if database_enabled:
            logger.info("Backstage credentials source: DB auth_config (use_secret_store=False, database_enabled=True)")
            db_creds = (auth_profile or {}).get("auth_config", {})
            required = ["mcp_url", "mcp_server_name", "mcp_api_key"]
            if not all(db_creds.get(k) for k in required):
                raise ValueError(
                    f"Backstage auth: is_secret_store_enabled=False but auth_config "
                    f"missing required fields: {required}"
                )
            logger.info("Backstage credentials successfully retrieved from DB auth_config")
            return {
                "mcp_url":         db_creds["mcp_url"],
                "mcp_server_name": db_creds["mcp_server_name"],
                "mcp_api_key":     db_creds["mcp_api_key"],
            }
        else:
            logger.info("Backstage credentials source: llmConfig.ini [Backstage] (use_secret_store=False, database_enabled=False)")
            endpoint    = _cfg.get("Backstage", "endpoint",       fallback="").strip()
            mcp_url         = _cfg.get("Backstage", "mcp_url",        fallback="").strip()
            mcp_server_name = _cfg.get("Backstage", "mcp_server_name", fallback="").strip()
            mcp_api_key     = _cfg.get("Backstage", "mcp_api_key",    fallback="").strip()
            if not (mcp_url and mcp_server_name and mcp_api_key):
                raise ValueError(
                    "Backstage auth: database_enabled=False but [Backstage] section in llmConfig.ini "
                    "is missing mcp_url / mcp_server_name / mcp_api_key."
                )
            logger.info("Backstage credentials successfully retrieved from llmConfig.ini [Backstage]")
            return {
                "mcp_url":         mcp_url,
                "mcp_server_name": mcp_server_name,
                "mcp_api_key":     mcp_api_key,
            }

    # use_secret_store=True: try secret store first, fall back to llmConfig.ini [Backstage] on any failure
    logger.info("Backstage credentials source: secret store (use_secret_store=True)")
    try:
        slot = _find_slot_for_endpoint(_BACKSTAGE_PREFIX, endpoint)
        if not slot:
            raise ValueError(f"No Backstage slot matched endpoint '{endpoint}' in secret store")
        secrets = _get_secrets()
        mcp_url         = secrets.get(f"{slot}-mcpurl", "")
        mcp_server_name = secrets.get(f"{slot}-servername", "")
        mcp_api_key     = secrets.get(f"{slot}-apikey", "")
        if not (mcp_url and mcp_server_name and mcp_api_key):
            raise ValueError(
                f"Backstage slot '{slot}' in secret store is missing one or more of: mcpurl, servername, apikey"
            )
        logger.info("Backstage credentials successfully retrieved from secret store (slot=%s)", slot)
        return {
            "mcp_url":         mcp_url,
            "mcp_server_name": mcp_server_name,
            "mcp_api_key":     mcp_api_key,
        }
    except Exception as exc:
        logger.warning(
            "Backstage secret store fetch failed — %s. Falling back to llmConfig.ini [Backstage].", exc
        )
        endpoint    = _cfg.get("Backstage", "endpoint",        fallback="").strip()
        mcp_url         = _cfg.get("Backstage", "mcp_url",         fallback="").strip()
        mcp_server_name = _cfg.get("Backstage", "mcp_server_name", fallback="").strip()
        mcp_api_key     = _cfg.get("Backstage", "mcp_api_key",     fallback="").strip()
        if not (mcp_url and mcp_server_name and mcp_api_key):
            raise ValueError(
                "Backstage auth: secret store fetch failed and [Backstage] fallback in llmConfig.ini "
                "is also missing mcp_url / mcp_server_name / mcp_api_key."
            )
        logger.info("Backstage credentials successfully retrieved from llmConfig.ini [Backstage] (secret store fallback)")
        return {
            "mcp_url":         mcp_url,
            "mcp_server_name": mcp_server_name,
            "mcp_api_key":     mcp_api_key,
        }


# ─────────────────────────────────────────────
# Strategy interface
# ─────────────────────────────────────────────

class AuthStrategy(ABC):
    """Resolves authentication credentials into a (token, cookies) pair."""

    @abstractmethod
    async def resolve(self, credentials: dict) -> tuple:
        """Returns (token, cookies). Either value may be None."""
        ...


# ─────────────────────────────────────────────
# Concrete strategies
# ─────────────────────────────────────────────

class QppTokenAuthStrategy(AuthStrategy):
    """
    QPP-style authentication.
    Posts {"username": ..., "password": ...} JSON to the token URL and
    extracts the "token" field from the JSON response.
    """

    async def resolve(self, credentials: dict):
        url = credentials.get("token_url", "")
        if not url:
            raise ValueError(
                "QppTokenAuthStrategy: 'token_url' not available. Ensure the "
                "secret store has '{slot}-tokenurl' set for this service endpoint."
            )

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json={
                    "username": credentials.get("username", ""),
                    "password": credentials.get("password", ""),
                },
            )
        resp.raise_for_status()

        token = resp.json().get("token")
        if not token:
            raise ValueError(f"QPP token endpoint did not return a 'token' field: {resp.text[:200]}")
        logger.debug("QppTokenAuthStrategy: token acquired")
        return token, None


class ApiKeyAuthStrategy(AuthStrategy):
    """
    API-key authentication (Vertex / x-api-key style).
    Returns the api_key value directly; the inject rule controls header placement.
    """

    async def resolve(self, credentials: dict):
        api_key = credentials.get("api_key")
        if not api_key:
            raise ValueError("ApiKeyAuthStrategy requires credentials.api_key")
        return api_key, None


class BackstageMcpAuthStrategy(AuthStrategy):
    """
    Backstage MCP authentication using bearer token with x-litellm-api-key header.
    Returns the api_key as token; inject rules handle header placement.
    Also returns mcp_server_name as metadata in cookies dict for x-mcp-servers header.
    """

    async def resolve(self, credentials: dict):
        mcp_api_key = credentials.get("mcp_api_key")
        mcp_server_name = credentials.get("mcp_server_name")
        
        if not mcp_api_key:
            raise ValueError("BackstageMcpAuthStrategy requires credentials.mcp_api_key")
        if not mcp_server_name:
            raise ValueError("BackstageMcpAuthStrategy requires credentials.mcp_server_name")
        
        logger.debug("BackstageMcpAuthStrategy: credentials resolved")
        # Return api_key as token (for x-litellm-api-key header injection)
        # Return mcp_server_name in cookies dict (for x-mcp-servers header)
        return mcp_api_key, {"x-mcp-servers": mcp_server_name}


# ─────────────────────────────────────────────
# Factory registry
# ─────────────────────────────────────────────

_STRATEGY_REGISTRY: dict = {
    "qpp-token":      QppTokenAuthStrategy(),
    "api-key":        ApiKeyAuthStrategy(),
    "backstage-mcp": BackstageMcpAuthStrategy(),
}


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

async def resolve_auth_token(
    auth_profile: dict,
    endpoint:     str = "",
) -> tuple:
    """
    Resolve the auth token for the given auth_profile dict.
    Delegates to the matching AuthStrategy based on auth_profile["type"].
    """
    auth_type = auth_profile.get("type")
    strategy = _STRATEGY_REGISTRY.get(auth_type)
    if strategy is None:
        raise ValueError(f"Unsupported auth type: {auth_type!r}")

    if auth_type == "qpp-token":
        credentials = _get_qpp_credentials(endpoint, auth_profile=auth_profile)
    elif auth_type == "api-key":
        credentials = _get_vertex_credentials(endpoint, auth_profile=auth_profile)
    elif auth_type == "backstage-mcp":
        credentials = _get_backstage_credentials(endpoint, auth_profile=auth_profile)
    else:
        credentials = {}

    return await strategy.resolve(credentials=credentials)


def inject_auth_into_headers(
    headers:      dict,
    auth_profile: dict,
    token:        str | None,
    cookies:      dict | None,
) -> dict:
    """
    Inject the resolved token into headers based on auth_profile["inject"] rules.
    For Backstage MCP, also inject x-mcp-servers from cookies dict.
    """
    for rule in auth_profile.get("inject", []):
        if rule.get("location") == "header" and token:
            prefix = rule.get("prefix", "")
            headers[rule["name"]] = f"{prefix}{token}"
    
    # Handle MCP-specific headers passed via cookies dict
    if cookies and isinstance(cookies, dict):
        if "x-mcp-servers" in cookies:
            headers["x-mcp-servers"] = cookies["x-mcp-servers"]
    
    return headers
