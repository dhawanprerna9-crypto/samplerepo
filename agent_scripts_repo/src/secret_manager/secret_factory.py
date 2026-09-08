from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from src.secret_manager.models import SecretRootConfig, load_secret_config_ini
from src.secret_manager.secret_providers import (
    TTLCache,
    AzureKeyVaultProvider,
    AWSSecretsManagerProvider,
    GCPSecretManagerJsonBlobProvider,
)


def _canon(s: str) -> str:
    return (s or "").strip().lower()


@dataclass
class SecretFactory:
    """
    Factory that fetches ALL secrets from a chosen provider type.
    - No AzureOpenAI_config.json required
    - Uses internal shared TTL cache
    - Reuses provider clients internally
    """
    secret_root: SecretRootConfig
    cache_ttl_seconds: int = 3600

    def __post_init__(self) -> None:
        self._cache = TTLCache(ttl_seconds=self.cache_ttl_seconds)
        self._providers: Dict[str, object] = {}

    @classmethod
    def from_secret_config_file(cls, path: str = "llmConfig.ini", cache_ttl_seconds: int = 3600) -> "SecretFactory":
        raw = load_secret_config_ini(path)
        secret_root = SecretRootConfig.model_validate(raw)
        return cls(secret_root=secret_root, cache_ttl_seconds=cache_ttl_seconds)

    def _get_or_create_provider(self, provider_type: str):
        p = _canon(provider_type)
        if p in self._providers:
            return self._providers[p]

        if p in ("azurekeyvault", "azure", "azurekeyvaultstore", "azurekeyvaulturi"):
            az = self.secret_root.azure_secret_store
            if az is None:
                raise ValueError("llmConfig.ini missing AzureSecretStore section")

            provider = AzureKeyVaultProvider(
                vault_url=az.AzureKeyVaultUri,
                auth_type=az.AzureAuthType or "ManagedIdentity",
                tenant_id=az.AzureTenantId,
                client_id=az.AzureClientId,
                client_secret=az.AzureClientSecret,
                user_assigned_mi=az.AzureUserAssignedManagedIdentity,
                cache=self._cache,
            )

        elif p in ("awssecretsmanager", "aws", "awssecretmgr", "awssecretstore"):
            aws = self.secret_root.aws_secret_store
            if aws is None:
                raise ValueError("llmConfig.ini missing AWSSecretStore section")

            provider = AWSSecretsManagerProvider(
                region_name=aws.AWSRegion,
                secret_name=aws.AWSSecretName,
                version_stage=aws.AWSVersion or "AWSCURRENT",
                cache=self._cache,
            )

        elif p in ("gcpsecretmanager", "gcp", "gcpsecretmgr", "gcpsecretstore"):
            gcp = self.secret_root.gcp_secret_store
            if gcp is None:
                raise ValueError("llmConfig.ini missing GCPSecretStore section")

            provider = GCPSecretManagerJsonBlobProvider(
                project_id=gcp.ProjectId,
                secret_name=gcp.GCPSecretName,
                version_id=gcp.VersionId or "latest",
                cache=self._cache,
            )

        else:
            raise ValueError(f"Unsupported providerType '{provider_type}'")

        self._providers[p] = provider
        return provider

    def get_all_secrets(self, provider_type: str) -> Dict[str, str]:
        provider = self._get_or_create_provider(provider_type)
        secrets = provider.fetch_all()  # type: ignore[attr-defined]
        return {str(k): str(v) for k, v in secrets.items()}
