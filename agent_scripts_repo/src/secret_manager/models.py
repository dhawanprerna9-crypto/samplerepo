from __future__ import annotations

import json
from configparser import ConfigParser
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


def sanitize_json(obj: Any) -> Any:
    """Remove comment-like keys and keep structure intact."""
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl.startswith("comment") or kl.startswith("_"):
                continue
            out[k] = sanitize_json(v)
        return out
    if isinstance(obj, list):
        return [sanitize_json(x) for x in obj]
    return obj


def load_secret_config_ini(path: str) -> Dict[str, Dict[str, str]]:
    """Load the secret-store sections of llmConfig.ini into a nested dict
    compatible with :class:`SecretRootConfig`.

    Only AzureSecretStore, AWSSecretStore, GCPSecretStore sections are surfaced.
    Empty values are omitted so optional fields fall back to model defaults.
    """
    parser = ConfigParser(inline_comment_prefixes=("#", ";"))
    parser.optionxform = str  # type: ignore[assignment]
    read_files = parser.read(path, encoding="utf-8")
    if not read_files:
        raise FileNotFoundError(f"Secret config file not found: {path}")

    allowed_sections = {"AzureSecretStore", "AWSSecretStore", "GCPSecretStore"}
    result: Dict[str, Dict[str, str]] = {}
    for section in parser.sections():
        if section not in allowed_sections:
            continue
        section_values: Dict[str, str] = {}
        for key, value in parser.items(section):
            value = (value or "").strip()
            if value:
                section_values[key] = value
        if section_values:
            result[section] = section_values
    return result


# =========================
# Configs/AzureOpenAI_config.json models
# =========================

class DirectConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    HeaderKeyName: Optional[str] = "api-key"
    SecretKey: Optional[str] = None
    ClientId: Optional[str] = None
    ClientSecret: Optional[str] = None
    TenantId: Optional[str] = None
    TokenURL: Optional[str] = None
    Scope: Optional[str] = None


class DirectConfigurationBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")

    DirectConfig: DirectConfig
    EndpointSuffix: Optional[str] = None
    EndpointAPIVersion: Optional[str] = None


class AIModelDetails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    DeploymentModelId: str
    ModelName: str
    ModelVersion: Optional[str] = None
    InputToken: Optional[int] = None
    OutputToken: Optional[int] = None
    PromptCost: Optional[float] = None
    CompletionCost: Optional[float] = None


class LLMConfiguration(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    EndPoint: str
    DeploymentName: Optional[str] = None
    Provider: str
    AIModelType: Optional[str] = None
    ApiVersion: Optional[str] = None
    Active: bool = True
    AuthenticationType: str
    SecretStoreType: str

    AIModelDetails: AIModelDetails
    AdditionalHeaders: Optional[Dict[str, str]] = None
    AIModelContractDetails: Optional[Dict[str, Any]] = None

    direct_configuration: Optional[DirectConfigurationBlock] = Field(default=None, alias="DirectConfiguration")


class ConfigurationItem(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    comment: Optional[str] = None
    llm_configuration: Optional[LLMConfiguration] = Field(default=None, alias="LLMConfiguration")


class LLMRootConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    configurations: List[ConfigurationItem] = Field(default_factory=list)


# =========================
# Secret-store models (loaded from llmConfig.ini)
# =========================

class AzureSecretStoreConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    AzureAuthType: Optional[Literal["ServicePrincipal", "ManagedIdentity"]] = None
    AzureKeyVaultUri: Optional[str] = None
    AzureUserAssignedManagedIdentity: Optional[str] = None
    AzureClientId: Optional[str] = None
    AzureClientSecret: Optional[str] = None
    AzureTenantId: Optional[str] = None


class AWSSecretStoreConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    AWSRegion: Optional[str] = None
    AWSSecretName: Optional[str] = None
    AWSVersion: Optional[str] = "AWSCURRENT"


class GCPSecretStoreConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ProjectId: Optional[str] = None
    VersionId: Optional[str] = "latest"
    GCPSecretName: Optional[str] = None


class SecretRootConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    azure_secret_store: Optional[AzureSecretStoreConfig] = Field(default=None, alias="AzureSecretStore")
    aws_secret_store: Optional[AWSSecretStoreConfig] = Field(default=None, alias="AWSSecretStore")
    gcp_secret_store: Optional[GCPSecretStoreConfig] = Field(default=None, alias="GCPSecretStore")


ConfigurationItem.model_rebuild()
LLMRootConfig.model_rebuild()
SecretRootConfig.model_rebuild()
