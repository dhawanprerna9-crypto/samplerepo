from __future__ import annotations

import json
import logging
from configparser import ConfigParser
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.secret_manager.models import (
    LLMRootConfig,
    LLMConfiguration,
    SecretRootConfig,
    load_secret_config_ini,
    sanitize_json,
)
from src.secret_manager.secret_providers import (
    TTLCache,
    AzureKeyVaultProvider,
    AWSSecretsManagerProvider,
    GCPSecretManagerJsonBlobProvider,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # src/secret_manager -> src -> project_root
_CONFIGS_DIR  = Path(__file__).resolve().parent / "Configs"  # src/secret_manager/Configs/


def canon(v: Any) -> str:
    return (str(v) if v is not None else "").strip().lower()


def canon_endpoint(url: str) -> str:
    s = (url or "").strip().lower()
    while s.endswith("/"):
        s = s[:-1]
    return s


REQUIRED_SECRET_PROVIDER_PREFIX_LIST: Dict[str, str] = {
    "azureopenai":      "oai",
    "enterpriseopenai": "eoai",
    "awsbedrock":       "awsb",
    "gcpvertexai":      "gcpv",
    "rag":              "rag",
    "rest":             "apim",
}

_PROVIDER_TO_CONFIG_FILE: Dict[str, str] = {
    "azureopenai":      "AzureOpenAI_config.json",
    "enterpriseopenai": "EnterpriseOpenAI_config.json",
    "awsbedrock":       "AWSBedrock_config.json",
    "gcpvertexai":      "GCPVertexAI_config.json",
}


def provider_to_prefix(provider: str) -> str:
    p = canon(provider)
    if p in ("azureopenai", "azure openai"):
        return REQUIRED_SECRET_PROVIDER_PREFIX_LIST.get("azureopenai", "oai")
    if p in ("enterpriseopenai", "enterprise openai"):
        return REQUIRED_SECRET_PROVIDER_PREFIX_LIST.get("enterpriseopenai", "oai")
    if p in ("awsbedrock", "amazonbedrock"):
        return REQUIRED_SECRET_PROVIDER_PREFIX_LIST.get("awsbedrock", "oai")
    if p in ("gcpvertexai", "vertexai", "googlevertex"):
        return REQUIRED_SECRET_PROVIDER_PREFIX_LIST.get("gcpvertexai", "oai")
    if p in ("restapi", "rest", "custom", "customllm"):
        return REQUIRED_SECRET_PROVIDER_PREFIX_LIST.get("rest", "oai")
    if p in ("rag", "customrag"):
        return REQUIRED_SECRET_PROVIDER_PREFIX_LIST.get("rag", "oai")
    return REQUIRED_SECRET_PROVIDER_PREFIX_LIST.get(p, "oai")


class Configurator:
    def __init__(self, llm_root: LLMRootConfig, secret_root: SecretRootConfig, default_secret_provider: str = ""):
        self.llm_root = llm_root
        self.secret_root = secret_root
        # When set, overrides per-config SecretStoreType so all models use the same store.
        self._default_sst = canon(default_secret_provider)

        self.cache = TTLCache(ttl_seconds=3600)
        self._rr_state: Dict[str, int] = {}

        self._llm_list: List[LLMConfiguration] = []
        for item in llm_root.configurations:
            if item.llm_configuration is not None:
                self._llm_list.append(item.llm_configuration)

    @classmethod
    def from_files(
        cls,
        llm_config_path: Optional[str] = None,
        secret_config_path: Optional[str] = None,
    ) -> "Configurator":
        if secret_config_path is None:
            secret_config_path = str(_PROJECT_ROOT / "llmConfig.ini")

        # Read default_llm_provider to pick the right JSON config file
        ini = ConfigParser(inline_comment_prefixes=("#", ";"))
        ini.read(secret_config_path, encoding="utf-8")
        default_llm_provider = (
            ini.get("appsettings", "default_llm_provider", fallback="azureopenai")
            .strip().lower().replace("_", "").replace("-", "")
        )

        if llm_config_path is None:
            config_filename = _PROVIDER_TO_CONFIG_FILE.get(default_llm_provider, "AzureOpenAI_config.json")
            llm_config_path = str(_CONFIGS_DIR / config_filename)

        logger.info(
            "[Configurator] Loading LLM config from %r, secret config from %r",
            llm_config_path, secret_config_path,
        )

        with open(llm_config_path, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.strip():
                raise ValueError(f"LLM config file is empty: {llm_config_path}")
            raw_llm = sanitize_json(json.loads(content))

        raw_sec = load_secret_config_ini(secret_config_path)

        # Validate required secret store fields for configured provider
        default_secret_provider = ini.get("appsettings", "default_secret_provider", fallback="").strip().lower()
        if default_secret_provider:
            missing = []
            if default_secret_provider.startswith("azure"):
                sec = SecretRootConfig.model_validate(raw_sec)
                if not sec.azure_secret_store or not sec.azure_secret_store.AzureKeyVaultUri:
                    missing.append("AzureSecretStore.AzureKeyVaultUri")
            elif default_secret_provider.startswith("aws"):
                sec = SecretRootConfig.model_validate(raw_sec)
                if not sec.aws_secret_store or not sec.aws_secret_store.AWSRegion:
                    missing.append("AWSSecretStore.AWSRegion")
                if not sec.aws_secret_store or not sec.aws_secret_store.AWSSecretName:
                    missing.append("AWSSecretStore.AWSSecretName")
            elif default_secret_provider.startswith("gcp"):
                sec = SecretRootConfig.model_validate(raw_sec)
                if not sec.gcp_secret_store or not sec.gcp_secret_store.ProjectId:
                    missing.append("GCPSecretStore.ProjectId")
                if not sec.gcp_secret_store or not sec.gcp_secret_store.GCPSecretName:
                    missing.append("GCPSecretStore.GCPSecretName")
            if missing:
                logger.warning(
                    "[Configurator] llmConfig.ini: Required fields missing for default_secret_provider "
                    "'%s': %s — secret store calls will fail if used.",
                    default_secret_provider, ", ".join(missing),
                )

        llm_root = LLMRootConfig.model_validate(raw_llm)
        sec_root = SecretRootConfig.model_validate(raw_sec)
        return cls(llm_root, sec_root, default_secret_provider=default_secret_provider)

    # -----------------------------
    # Matching / RR selection
    # -----------------------------
    def _matches_model(self, cfg: LLMConfiguration, modelname: str) -> bool:
        mk = canon(modelname)
        dn = canon(cfg.DeploymentName)
        dm = canon(cfg.AIModelDetails.DeploymentModelId)
        mn = canon(cfg.AIModelDetails.ModelName)
        return mk in (dn, dm, mn)

    def _active_matches(self, modelname: str) -> List[LLMConfiguration]:
        return [c for c in self._llm_list if bool(c.Active) and self._matches_model(c, modelname)]

    def _pick_round_robin(self, modelname: str, configs: List[LLMConfiguration]) -> LLMConfiguration:
        key = canon(modelname)
        idx = self._rr_state.get(key, 0)
        chosen = configs[idx % len(configs)]
        self._rr_state[key] = (idx + 1) % len(configs)
        return chosen

    # -----------------------------
    # Secret store fetch
    # -----------------------------
    def _secret_store_type(self, cfg: LLMConfiguration) -> str:
        # default_secret_provider from llmConfig.ini takes precedence over the
        # per-config SecretStoreType in the JSON file, so operators only need to
        # configure one place.
        return self._default_sst or canon(cfg.SecretStoreType)

    def _processed_cache_key(self, sst: str) -> str:
        if sst in ("azurekeyvault", "azurekeyvaultstore", "azurekeyvaulturi"):
            az = self.secret_root.azure_secret_store
            return f"processed::azkv::{az.AzureKeyVaultUri if az else 'missing'}"
        if sst in ("awssecretsmanager", "awssecretmgr", "awssecretstore"):
            aws = self.secret_root.aws_secret_store
            if not aws:
                return "processed::aws::missing"
            return f"processed::aws::{aws.AWSRegion}::{aws.AWSSecretName}::{aws.AWSVersion or 'AWSCURRENT'}"
        if sst in ("gcpsecretmanager", "gcpsecretmgr", "gcpsecretstore"):
            gcp = self.secret_root.gcp_secret_store
            if not gcp:
                return "processed::gcp::missing"
            return f"processed::gcp::{gcp.ProjectId}::{gcp.GCPSecretName}::{gcp.VersionId or 'latest'}"
        return f"processed::other::{sst}"

    def _fetch_and_process_secrets(self, cfg: LLMConfiguration) -> Dict[str, str]:
        """Fetch raw secrets from secret store and filter by allowed prefixes."""
        sst = self._secret_store_type(cfg)
        cache_key = self._processed_cache_key(sst)

        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        if sst in ("azurekeyvault", "azurekeyvaultstore", "azurekeyvaulturi"):
            az = self.secret_root.azure_secret_store
            if not az:
                raise ValueError("llmConfig.ini missing AzureSecretStore section.")
            provider = AzureKeyVaultProvider(
                vault_url=az.AzureKeyVaultUri,
                auth_type=az.AzureAuthType or "ManagedIdentity",
                tenant_id=az.AzureTenantId,
                client_id=az.AzureClientId,
                client_secret=az.AzureClientSecret,
                user_assigned_mi=az.AzureUserAssignedManagedIdentity,
                cache=self.cache,
            )
            raw = provider.fetch_all()

        elif sst in ("awssecretsmanager", "awssecretmgr", "awssecretstore"):
            aws = self.secret_root.aws_secret_store
            if not aws:
                raise ValueError("llmConfig.ini missing AWSSecretStore section.")
            provider = AWSSecretsManagerProvider(
                region_name=aws.AWSRegion,
                secret_name=aws.AWSSecretName,
                version_stage=aws.AWSVersion or "AWSCURRENT",
                cache=self.cache,
            )
            raw = provider.fetch_all()

        elif sst in ("gcpsecretmanager", "gcpsecretmgr", "gcpsecretstore"):
            gcp = self.secret_root.gcp_secret_store
            if not gcp:
                raise ValueError("llmConfig.ini missing GCPSecretStore section.")
            provider = GCPSecretManagerJsonBlobProvider(
                project_id=gcp.ProjectId,
                secret_name=gcp.GCPSecretName,
                version_id=gcp.VersionId or "latest",
                cache=self.cache,
            )
            raw = provider.fetch_all()

        else:
            raise ValueError(f"Unsupported SecretStoreType: {cfg.SecretStoreType}")

        # Filter by allowed prefixes (oai-/awsb-/gcpv-/eoai-/rag-/apim-)
        allowed_prefixes = set(REQUIRED_SECRET_PROVIDER_PREFIX_LIST.values())
        processed: Dict[str, str] = {}
        for k, v in raw.items():
            kl = canon(k)
            for pref in allowed_prefixes:
                if kl.startswith(pref + "-"):
                    processed[k] = str(v)
                    break

        self.cache.set(cache_key, processed)
        return processed

    # -----------------------------
    # Slot selection by endpoint
    # -----------------------------
    def _find_slot_by_endpoint(self, cfg: LLMConfiguration, processed: Dict[str, str]) -> Optional[str]:
        prefix = provider_to_prefix(cfg.Provider)
        target = canon_endpoint(cfg.EndPoint)

        for i in range(1, 51):
            k = f"{prefix}-{i}-endpoint"
            if k in processed and canon_endpoint(processed[k]) == target:
                return f"{prefix}-{i}"
        return None

    def _slot_group(self, slot: str, processed: Dict[str, str]) -> Dict[str, str]:
        pref = slot + "-"
        return {k: v for k, v in processed.items() if canon(k).startswith(pref)}

    # -----------------------------
    # Public API
    # -----------------------------
    def resolve_model(self, modelname: str) -> Dict[str, Any]:
        matches = self._active_matches(modelname)
        if not matches:
            return {"success": False, "message": f"No ACTIVE configurations available for model '{modelname}'"}

        cfg = self._pick_round_robin(modelname, matches)
        sst = self._secret_store_type(cfg)

        if sst == "directconfig":
            dc = cfg.direct_configuration.model_dump(by_alias=True) if cfg.direct_configuration else {}
            return {
                "success": True,
                "configurations": {
                    "provider": cfg.Provider,
                    "secretStoreType": cfg.SecretStoreType,
                    "authenticationType": cfg.AuthenticationType,
                    "endpoint": cfg.EndPoint,
                    "deploymentName": cfg.DeploymentName,
                    "deploymentModelId": cfg.AIModelDetails.DeploymentModelId,
                    "apiVersion": cfg.ApiVersion,
                    "modelName": cfg.AIModelDetails.ModelName,
                    "modelVersion": cfg.AIModelDetails.ModelVersion,
                    "active": cfg.Active,
                },
                "directConfiguration": dc,
            }

        processed = self._fetch_and_process_secrets(cfg)
        slot = self._find_slot_by_endpoint(cfg, processed)
        if not slot:
            return {
                "success": False,
                "message": f"No secrets found for endpoint '{cfg.EndPoint}' in secret store '{cfg.SecretStoreType}'.",
                "hint": f"Ensure '{provider_to_prefix(cfg.Provider)}-<n>-endpoint' exists in secret store and matches endpoint exactly.",
            }

        group = self._slot_group(slot, processed)
        return {
            "success": True,
            "configurations": {
                "provider": cfg.Provider,
                "secretStoreType": cfg.SecretStoreType,
                "authenticationType": cfg.AuthenticationType,
                "endpoint": cfg.EndPoint,
                "deploymentName": cfg.DeploymentName,
                "deploymentModelId": cfg.AIModelDetails.DeploymentModelId,
                "apiVersion": cfg.ApiVersion,
                "modelName": cfg.AIModelDetails.ModelName,
                "modelVersion": cfg.AIModelDetails.ModelVersion,
                "active": cfg.Active,
                "mappingPrefix": slot,
            },
            "mappedSecrets": group,
        }
