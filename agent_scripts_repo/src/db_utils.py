"""
DB utility for fetching AI model routing config and details.
Used by litellm_factory.create_llm() to resolve primary model + fallbacks
from the AIModelConfig table, and endpoint details from AIModelDetails.
"""

import json
import logging
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_config = ConfigParser(inline_comment_prefixes=("#", ";"))
_config_path = Path(__file__).parent.parent / "llmConfig.ini"
if _config_path.exists():
    _config.read(_config_path)


def _get_db_connection():
    try:
        import psycopg

        section = "database"
        if not _config.has_section(section):
            logger.debug("No [database] section in llmConfig.ini — DB model lookup skipped.")
            return None

        host = _config.get(section, "host", fallback="").strip()
        port = _config.get(section, "port", fallback="5432").strip()
        name = _config.get(section, "name", fallback="").strip()
        user = _config.get(section, "user", fallback="").strip()
        password = _config.get(section, "password", fallback="").strip()

        if not all([host, name, user, password]):
            logger.debug("Incomplete [database] config in llmConfig.ini — DB model lookup skipped.")
            return None

        conninfo = f"host={host} port={port} dbname={name} user={user} password={password} connect_timeout=5"
        return psycopg.connect(conninfo)

    except Exception as e:
        logger.warning(f"DB connection unavailable for model config lookup: {e}")
        return None


def fetch_ai_model_config(
    agent_name: str,
    use_case: str = "default",
) -> tuple:
    """
    Fetch the primary model and fallback list for the given agent + usecase
    from the AIModelConfig table.

    Returns:
        (primary_model, fallbacks) e.g. ("gpt-4.1", ["gpt-5.4"])
        Returns (None, []) if DB is unreachable or no rows found.
    """
    conn = _get_db_connection()
    if conn is None:
        return None, []

    try:
        schema = _config.get("database", "schema", fallback="agentbuilder")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT "ModelName"
                FROM "{schema}"."AIModelConfig"
                WHERE "IsActive" = TRUE
                  AND (
                      ("AgentName" = %s AND "UseCase" = %s)
                      OR ("AgentName" = '*' AND "UseCase" = 'default')
                  )
                ORDER BY
                    CASE WHEN "AgentName" = %s AND "UseCase" = %s THEN 0 ELSE 1 END,
                    "Priority" ASC
                """,
                (agent_name, use_case, agent_name, use_case),
            )
            rows = [r[0] for r in cur.fetchall()]

        if not rows:
            logger.info(
                f"No AIModelConfig rows for agent='{agent_name}' usecase='{use_case}'"
            )
            return None, []

        primary = rows[0]
        fallbacks = rows[1:]
        logger.info(
            f"AIModelConfig resolved | agent='{agent_name}' usecase='{use_case}'"
            f" | primary='{primary}' fallbacks={fallbacks}"
        )
        return primary, fallbacks

    except Exception as e:
        logger.warning(f"Failed to fetch AIModelConfig from DB: {e}")
        return None, []
    finally:
        try:
            conn.close()
        except Exception:
            pass


@dataclass
class AIModelDetails:
    """Connection details for a single endpoint of a logical model."""
    model_name: str
    provider: str
    deployment_name: Optional[str]
    endpoint: Optional[str]
    api_version: Optional[str]
    region: Optional[str]
    api_key: Optional[str]
    secret_store_type: Optional[str] = None
    secret_store_config: Optional[str] = None


def fetch_ai_model_details(model_name: str) -> list:
    """Fetch all active endpoint records for the given model name."""
    conn = _get_db_connection()
    if conn is None:
        return []

    try:
        schema = _config.get("database", "schema", fallback="agentbuilder")
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT "ModelName", "Provider", "DeploymentName",
                       "Endpoint", "ApiVersion", "Region", "ApiKey",
                       "SecretStoreType", "SecretStoreConfig"
                FROM "{schema}"."AIModelDetails"
                WHERE "ModelName" = %s AND "IsActive" = TRUE
                """,
                (model_name,),
            )
            rows = cur.fetchall()

        if not rows:
            logger.info(f"No AIModelDetails rows found for model='{model_name}'")
            return []

        results = [
            AIModelDetails(
                model_name=row[0],
                provider=row[1],
                deployment_name=row[2],
                endpoint=row[3],
                api_version=row[4],
                region=row[5],
                api_key=row[6],
                secret_store_type=row[7],
                secret_store_config=row[8],
            )
            for row in rows
        ]
        logger.info(
            f"AIModelDetails resolved | model='{model_name}'"
            f" | {len(results)} endpoint(s)"
        )
        return results

    except Exception as e:
        logger.warning(f"Failed to fetch AIModelDetails for model='{model_name}': {e}")
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def resolve_secrets_for_model(details: "AIModelDetails") -> "AIModelDetails":
    """
    If the model uses an external secret store (AWS/Azure/GCP), resolve the
    API key and other secrets by matching the endpoint value in the secret blob.
    """
    store_type = (details.secret_store_type or "").strip().lower()

    # No external secret store — use DB values as-is
    if not store_type or store_type in ("directdbconfig", "direct", "db"):
        return details

    # Parse SecretStoreConfig — may already be a dict (JSONB auto-parsed by psycopg)
    store_config: dict = {}
    if details.secret_store_config:
        try:
            if isinstance(details.secret_store_config, dict):
                store_config = details.secret_store_config
            else:
                store_config = json.loads(details.secret_store_config)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(
                f"Invalid SecretStoreConfig JSON for model='{details.model_name}': {e}"
            )
            return details

    if not store_config:
        logger.warning(
            f"Empty SecretStoreConfig for model='{details.model_name}' "
            f"with SecretStoreType='{store_type}'"
        )
        return details

    # Fetch secrets from the appropriate provider
    try:
        secrets = _fetch_secrets_from_store(store_type, store_config)
    except Exception as e:
        logger.error(
            f"Failed to fetch secrets from '{store_type}' for model='{details.model_name}': {e}"
        )
        return details

    if not secrets:
        logger.warning(f"Empty secrets returned from '{store_type}' for model='{details.model_name}'")
        return details

    logger.info(
        f"Secrets fetched from '{store_type}' for model='{details.model_name}'"
        f" | {len(secrets)} key(s) retrieved"
    )

    # Derive the prefix by matching the endpoint value from DB against secret values
    prefix = _derive_secret_prefix(secrets, details.endpoint)
    if not prefix:
        logger.warning(
            f"Could not derive secret prefix for model='{details.model_name}'"
            f" — no secret value matches endpoint='{details.endpoint}'"
        )
        return details

    logger.info(f"Derived secret prefix='{prefix}' for model='{details.model_name}'")

    # Use the prefix to look up model secrets
    resolved_api_key = secrets.get(f"{prefix}-key1")
    resolved_api_version = secrets.get(f"{prefix}-endpointapiversion")
    resolved_endpoint = secrets.get(f"{prefix}-endpoint")

    return AIModelDetails(
        model_name=details.model_name,
        provider=details.provider,
        deployment_name=details.deployment_name,
        endpoint=resolved_endpoint or details.endpoint,
        api_version=resolved_api_version or details.api_version,
        region=details.region,
        api_key=resolved_api_key or details.api_key,
        secret_store_type=details.secret_store_type,
        secret_store_config=details.secret_store_config,
    )


def _derive_secret_prefix(secrets: dict, endpoint: Optional[str]) -> Optional[str]:
    """Match endpoint value in secret blob to find key prefix like 'oai-1'."""
    if not endpoint:
        return None

    endpoint_normalized = endpoint.strip().rstrip("/")

    for key, value in secrets.items():
        if not isinstance(value, str):
            continue
        value_normalized = value.strip().rstrip("/")
        if value_normalized == endpoint_normalized and key.endswith("-endpoint"):
            # Extract prefix: "oai-1-endpoint" -> "oai-1"
            prefix = key[: -len("-endpoint")]
            return prefix

    return None


def _fetch_secrets_from_store(store_type: str, store_config: dict) -> dict:
    """Connect to the configured secret store and fetch all secrets."""
    from .secret_providers import (
        TTLCache,
        AWSSecretsManagerProvider,
        AzureKeyVaultProvider,
        GCPSecretManagerJsonBlobProvider,
    )

    cache = TTLCache(ttl_seconds=3600)

    if store_type in ("awssecretsmanager", "aws"):
        aws_config = store_config.get("AWSSecretStore", {})
        region = aws_config.get("AWSRegion") or _config.get("AWSSecretStore", "AWSRegion", fallback="us-east-1")
        secret_name = aws_config.get("AWSSecretName") or _config.get("AWSSecretStore", "AWSSecretName", fallback="")
        version_stage = aws_config.get("AWSVersion") or _config.get("AWSSecretStore", "AWSVersion", fallback="AWSCURRENT")

        if not secret_name:
            raise ValueError("AWSSecretName not configured in SecretStoreConfig or llmConfig.ini")

        logger.info(f"Connecting to AWS Secrets Manager | region={region} | secret={secret_name}")
        provider = AWSSecretsManagerProvider(
            region_name=region,
            secret_name=secret_name,
            version_stage=version_stage,
            cache=cache,
        )
        return provider.fetch_all()

    elif store_type in ("azurekeyvault", "azure"):
        azure_config = store_config.get("AzureSecretStore", {})
        vault_url = azure_config.get("AzureKeyVaultUri") or _config.get("AzureSecretStore", "AzureKeyVaultUri", fallback="")
        tenant_id = azure_config.get("AzureTenantId") or _config.get("AzureSecretStore", "AzureTenantId", fallback=None)
        client_id = azure_config.get("AzureClientId") or _config.get("AzureSecretStore", "AzureClientId", fallback=None)
        client_secret = azure_config.get("AzureClientSecret") or _config.get("AzureSecretStore", "AzureClientSecret", fallback=None)
        user_assigned_mi = azure_config.get("AzureUserAssignedManagedIdentity") or _config.get("AzureSecretStore", "AzureUserAssignedManagedIdentity", fallback=None)

        if not vault_url:
            raise ValueError("AzureKeyVaultUri not configured in SecretStoreConfig or llmConfig.ini")

        logger.info(f"Connecting to Azure Key Vault | vault_url={vault_url}")
        provider = AzureKeyVaultProvider(
            vault_url=vault_url,
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            user_assigned_mi=user_assigned_mi,
            cache=cache,
        )
        return provider.fetch_all()

    elif store_type in ("gcpsecretmanager", "gcp"):
        gcp_config = store_config.get("GCPSecretStore", {})
        project_id = gcp_config.get("ProjectId") or _config.get("GCPSecretStore", "ProjectId", fallback="")
        secret_name = gcp_config.get("GCPSecretName") or _config.get("GCPSecretStore", "GCPSecretName", fallback="")
        version_id = gcp_config.get("VersionId") or _config.get("GCPSecretStore", "VersionId", fallback="latest")

        if not project_id or not secret_name:
            raise ValueError("ProjectId/GCPSecretName not configured in SecretStoreConfig or llmConfig.ini")

        logger.info(f"Connecting to GCP Secret Manager | project={project_id} | secret={secret_name}")
        provider = GCPSecretManagerJsonBlobProvider(
            project_id=project_id,
            secret_name=secret_name,
            version_id=version_id,
            cache=cache,
        )
        return provider.fetch_all()

    else:
        raise ValueError(f"Unsupported secret store type: '{store_type}'")
