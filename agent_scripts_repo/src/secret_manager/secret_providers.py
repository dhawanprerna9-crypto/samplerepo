"""
Secret providers for fetching credentials from external secret stores.
Supports AWS Secrets Manager, Azure Key Vault, and GCP Secret Manager.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =========================================================
# Retry helper (3 attempts with exponential backoff)
# =========================================================

def retry_with_backoff(
    operation: Callable[[], T],
    *,
    retries: int = 3,
    delay_seconds: float = 1.0,
    backoff_multiplier: float = 2.0,
    operation_name: str = "operation",
) -> T:
    last_exc: Optional[Exception] = None
    wait = delay_seconds
    for attempt in range(1, retries + 1):
        try:
            return operation()
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                break
            print(f"[WARN] {operation_name} failed (attempt {attempt}/{retries}): {exc}. Retrying in {wait:.1f}s")
            time.sleep(wait)
            wait *= backoff_multiplier
    raise last_exc  # type: ignore[misc]


# =========================================================
# TTL Cache
# =========================================================

@dataclass
class TTLCache:
    ttl_seconds: int = 3600
    _store: Dict[str, Tuple[float, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._store is None:
            self._store = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if not item:
            return None
        expires_at, value = item
        if time.time() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time() + self.ttl_seconds, value)


def _parse_json_object(payload: str) -> Optional[Dict[str, Any]]:
    """Return dict if payload is a JSON object; else None."""
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


# =========================================================
# Base Provider Interface
# =========================================================

class SecretProvider:
    def fetch_all(self) -> Dict[str, str]:
        raise NotImplementedError

    def fetch_one(self, key: str) -> str:
        raise NotImplementedError


# =========================================================
# Azure Key Vault Provider
# =========================================================

class AzureKeyVaultProvider(SecretProvider):
    """
    Azure Key Vault secret provider.

    auth_type:
      - "ServicePrincipal": needs tenant_id, client_id, client_secret
      - "ManagedIdentity": optional user_assigned_mi (client id)
    """

    def __init__(
        self,
        *,
        vault_url: str,
        auth_type: str = "ManagedIdentity",
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        user_assigned_mi: Optional[str] = None,
        cache: Optional[TTLCache] = None,
    ):
        self.vault_url = vault_url
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_assigned_mi = user_assigned_mi
        # Derive auth_type from presence of user_assigned_mi to avoid misconfiguration.
        self.auth_type = "ManagedIdentity" if self.user_assigned_mi else "ServicePrincipal"
        self.cache = cache or TTLCache(ttl_seconds=3600)

        from azure.keyvault.secrets import SecretClient  # type: ignore
        from azure.identity import (  # type: ignore
            ClientSecretCredential,
            ManagedIdentityCredential,
            DefaultAzureCredential,
        )

        if self.auth_type.lower() == "serviceprincipal":
            if not (self.tenant_id and self.client_id and self.client_secret):
                raise ValueError("Azure ServicePrincipal requires tenant_id, client_id, client_secret")
            credential = ClientSecretCredential(self.tenant_id, self.client_id, self.client_secret)
        elif self.auth_type.lower() == "managedidentity":
            if self.user_assigned_mi:
                credential = ManagedIdentityCredential(client_id=self.user_assigned_mi)
            else:
                credential = ManagedIdentityCredential()
        else:
            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)

        self._client = SecretClient(vault_url=self.vault_url, credential=credential)

    def fetch_one(self, key: str) -> str:
        cache_key = f"azkv::one::{self.vault_url}::{key}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        def _op() -> str:
            sec = self._client.get_secret(key)
            return sec.value or ""

        val = retry_with_backoff(_op, retries=3, operation_name=f"AzureKeyVault.get_secret({key})")
        self.cache.set(cache_key, val)
        return val

    def fetch_all(self) -> Dict[str, str]:
        """Lists all secrets and fetches each value."""
        cache_key = f"azkv::all::{self.vault_url}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        def _list_names() -> list:
            props = self._client.list_properties_of_secrets()
            return [p.name for p in props]

        names = retry_with_backoff(_list_names, retries=3, operation_name="AzureKeyVault.list_properties_of_secrets")

        out: Dict[str, str] = {}
        for name in names:
            try:
                out[name] = self.fetch_one(name)
            except Exception as exc:
                logger.warning("[AzureKeyVault] Failed to fetch secret %r: %s", name, exc)

        self.cache.set(cache_key, out)
        return out


# =========================================================
# AWS Secrets Manager Provider (JSON blob secret)
# =========================================================

class AWSSecretsManagerProvider(SecretProvider):
    """
    AWS Secrets Manager provider.
    Assumes the secret value is a single JSON object string like:
      {"oai-1-endpoint":"...", "oai-1-key1":"..."}
    """

    def __init__(
        self,
        *,
        region_name: str,
        secret_name: str,
        version_stage: str = "AWSCURRENT",
        cache: Optional[TTLCache] = None,
    ):
        self.region_name = region_name
        self.secret_name = secret_name
        self.version_stage = version_stage or "AWSCURRENT"
        self.cache = cache or TTLCache(ttl_seconds=3600)

        import boto3  # type: ignore
        self._client = boto3.client("secretsmanager", region_name=self.region_name)

    def _get_secret_string(self) -> str:
        def _op() -> str:
            resp = self._client.get_secret_value(SecretId=self.secret_name, VersionStage=self.version_stage)
            if "SecretString" in resp and resp["SecretString"] is not None:
                return resp["SecretString"]
            if "SecretBinary" in resp and resp["SecretBinary"] is not None:
                data = resp["SecretBinary"]
                if isinstance(data, (bytes, bytearray)):
                    return data.decode("utf-8", errors="replace")
                return str(data)
            return ""

        return retry_with_backoff(_op, retries=3, operation_name=f"AWSSecretsManager.get_secret_value({self.secret_name})")

    def fetch_all(self) -> Dict[str, str]:
        cache_key = f"aws::all::{self.region_name}::{self.secret_name}::{self.version_stage}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        raw = self._get_secret_string()
        obj = _parse_json_object(raw)
        if obj is None:
            out = {"_raw": raw}
            self.cache.set(cache_key, out)
            return out

        out = {str(k): str(v) for k, v in obj.items()}
        self.cache.set(cache_key, out)
        return out

    def fetch_one(self, key: str) -> str:
        allv = self.fetch_all()
        if key not in allv:
            raise KeyError(f"AWS key '{key}' not found in secret '{self.secret_name}'")
        return allv[key]


# =========================================================
# GCP Secret Manager Provider (single JSON blob secret)
# =========================================================

class GCPSecretManagerJsonBlobProvider(SecretProvider):
    """GCP Secret Manager provider."""

    def __init__(
        self,
        *,
        project_id: str,
        secret_name: str,
        version_id: str = "latest",
        cache: Optional[TTLCache] = None,
    ):
        self.project_id = project_id
        self.secret_name = secret_name
        self.version_id = version_id or "latest"
        self.cache = cache or TTLCache(ttl_seconds=3600)

        from google.cloud import secretmanager  # type: ignore
        self._client = secretmanager.SecretManagerServiceClient()

    def _access_payload(self) -> str:
        name = f"projects/{self.project_id}/secrets/{self.secret_name}/versions/{self.version_id}"

        def _op() -> str:
            resp = self._client.access_secret_version(request={"name": name})
            return resp.payload.data.decode("utf-8")

        return retry_with_backoff(_op, retries=3, operation_name=f"GCPSecretManager.access_secret_version({self.secret_name})")

    def fetch_all(self) -> Dict[str, str]:
        cache_key = f"gcp::json::{self.project_id}::{self.secret_name}::{self.version_id}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        raw = self._access_payload()
        obj = _parse_json_object(raw)
        if obj is None:
            raise ValueError(
                f"GCP secret '{self.secret_name}' payload is not a JSON object. "
                "Expected JSON like {\"oai-1-endpoint\":\"...\", \"oai-1-key1\":\"...\"}"
            )

        out = {str(k): str(v) for k, v in obj.items()}
        self.cache.set(cache_key, out)
        return out

    def fetch_one(self, key: str) -> str:
        allv = self.fetch_all()
        if key not in allv:
            raise KeyError(f"GCP key not found in JSON blob secret")
        return allv[key]
