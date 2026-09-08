import logging
from configparser import ConfigParser
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT   = Path(__file__).resolve().parents[1]
_LLMCONFIG_PATH = _PROJECT_ROOT / "llmConfig.ini"

_llm_cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
if _LLMCONFIG_PATH.exists():
    _llm_cfg.read(_LLMCONFIG_PATH, encoding="utf-8")

_CONFIG_PATH    = _PROJECT_ROOT / "config.ini"

_app_cfg = ConfigParser(inline_comment_prefixes=("#", ";"))
if _CONFIG_PATH.exists():
    _app_cfg.read(_CONFIG_PATH, encoding="utf-8")

_DEFAULT_SECRET_PROVIDER = _llm_cfg.get("appsettings", "default_secret_provider", fallback="azurekeyvault").strip().lower()


def _is_sensitive_setting_key(key: str) -> bool:
    sensitive_tokens = ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL")
    return any(token in key.upper() for token in sensitive_tokens)


def _extract_required_openai_fields(llm_result: Any) -> dict:
    """
    Extract only the required LLM configuration fields from resolve_model() result.

    Output keys: Endpoint, DeploymentName, apiVersion, ModelName, ModelVersion, Provider, apikey.
    """
    if not isinstance(llm_result, dict):
        return {}

    config_block = llm_result.get("configurations", llm_result)
    if not isinstance(config_block, dict):
        return {}

    mapped_secrets = llm_result.get("mappedSecrets") or config_block.get("mappedSecrets") or {}
    if not isinstance(mapped_secrets, dict):
        mapped_secrets = {}

    prefix = config_block.get("mappingPrefix") or config_block.get("slot")
    endpoint_from_secrets = mapped_secrets.get(f"{prefix}-endpoint") if prefix else None
    apikey_from_secrets   = mapped_secrets.get(f"{prefix}-key1") if prefix else None

    # For directconfig, apikey lives in directConfiguration block
    if not apikey_from_secrets:
        direct = llm_result.get("directConfiguration", {}) or {}
        direct_cfg = direct.get("DirectConfig", {}) or {}
        apikey_from_secrets = direct_cfg.get("SecretKey")

    return {
        "Endpoint":       endpoint_from_secrets or config_block.get("endpoint"),
        "DeploymentName": config_block.get("deploymentName"),
        "apiVersion":     config_block.get("apiVersion"),
        "ModelName":      config_block.get("modelName") or config_block.get("deploymentModelId"),
        "ModelVersion":   config_block.get("modelVersion"),
        "Provider":       config_block.get("provider"),
        "apikey":         apikey_from_secrets,
    }


def _apply_langfuse_fallback(secrets: dict) -> dict:
    """Fill langfuse-publickey / langfuse-secretkey from config.ini [observability.langfuse]."""
    if not _app_cfg.has_section("observability.langfuse"):
        return secrets
    mapping = {
        "langfuse-publickey": _app_cfg.get("observability.langfuse", "public_key", fallback="").strip(),
        "langfuse-secretkey": _app_cfg.get("observability.langfuse", "secret_key", fallback="").strip(),
    }
    applied = []
    for secret_key, fallback_val in mapping.items():
        if fallback_val and not secrets.get(secret_key):
            secrets[secret_key] = fallback_val
            applied.append(secret_key)
    if applied:
        logger.info("[CONFIG] Applied config.ini [observability.langfuse] fallback for: %s", ", ".join(applied))
    return secrets


def _apply_secrets_fallback(secrets: dict) -> dict:
    """Fill any missing secret keys from config.ini [secrets_fallback] section."""
    if not _app_cfg.has_section("secrets_fallback"):
        return secrets
    applied = []
    for key, value in _app_cfg.items("secrets_fallback"):
        value = value.strip()
        if value and key not in secrets:
            secrets[key] = value
            applied.append(key)
    if applied:
        logger.info("[CONFIG] Applied config.ini [secrets_fallback] for: %s", ", ".join(applied))
    return secrets


def _fetch_secrets_from_source() -> dict:
    """Fetch all secrets from the configured secret store via SecretFactory,
    then fill any missing keys from config.ini [secrets_fallback]."""
    logger.info("[CONFIG] Fetching secrets from the source...")
    try:
        from src.secret_manager.secret_retriever import fetch_all_secrets
        raw = fetch_all_secrets(_DEFAULT_SECRET_PROVIDER, str(_LLMCONFIG_PATH))
        secrets = raw if isinstance(raw, dict) else {}
    except Exception:
        logger.exception("Failed to fetch secrets.")
        secrets = {}
    secrets = _apply_langfuse_fallback(secrets)
    return _apply_secrets_fallback(secrets)


def _apply_config_ini_fallback(result: dict) -> dict:
    """Fill any empty LLM config fields from config.ini [azure] section."""
    fb = {
        "Endpoint":       _app_cfg.get("azure", "endpoint",       fallback="").strip(),
        "apikey":         _app_cfg.get("azure", "key",            fallback="").strip(),
        "apiVersion":     _app_cfg.get("azure", "apiversion",     fallback="").strip(),
        "DeploymentName": _app_cfg.get("azure", "deploymentname", fallback="").strip(),
    }
    applied = []
    for field, fallback_val in fb.items():
        if not result.get(field) and fallback_val:
            result[field] = fallback_val
            applied.append(field)
    if applied:
        logger.info("[CONFIG] Applied config.ini [azure] fallback for: %s", ", ".join(applied))
    return result


def _fetch_openai_configurations() -> dict:
    """
    Mirrors ADLC _fetch_openai_configurations():
      Configurator.from_files() -> resolve_model(model_name) -> _extract_required_openai_fields()
    Uses default_model_name from llmConfig.ini [appsettings]; if blank, resolves first active config.
    """
    logger.info("[CONFIG] Fetching LLM configurations from the source...")
    try:
        from src.secret_manager.config_loader import Configurator
        configurator = Configurator.from_files(secret_config_path=str(_LLMCONFIG_PATH))

        configured_model_name = _llm_cfg.get("appsettings", "default_model_name", fallback="").strip()
        if not configured_model_name:
            active = [c for c in configurator._llm_list if c.Active]
            if not active:
                logger.warning("[CONFIG] No active LLM configurations found.")
                return {}
            configured_model_name = active[0].DeploymentName or active[0].AIModelDetails.ModelName

        llm_result = configurator.resolve_model(configured_model_name)

        if not isinstance(llm_result, dict):
            logger.error("[CONFIG] resolve_model returned unexpected type '%s'.", type(llm_result).__name__)
            return {}

        if not llm_result.get("success"):
            logger.error("[CONFIG] resolve_model failed: %s", llm_result.get("message"))
            return {}

        result = _extract_required_openai_fields(llm_result)
        return _apply_config_ini_fallback(result)

    except Exception:
        logger.exception("Failed to fetch LLM configurations.")
        return _apply_config_ini_fallback({})


class LLMConfigSettings:
    def __init__(self):
        _openai_configurations = _fetch_openai_configurations()

        for key, value in _openai_configurations.items():
            if not isinstance(key, str):
                logger.warning("Skipping non-string settings key of type '%s'.", type(key).__name__)
                continue
            if hasattr(self, key):
                logger.warning("Skipping settings key '%s' because it conflicts with an LLMConfigSettings attribute.", key)
                continue
            setattr(self, key, value)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


llmConfigs = LLMConfigSettings()
logger.info(
    "[startup] LLM config loaded — provider=%r  endpoint=%r  deployment=%r  api_version=%r  key_present=%s",
    llmConfigs.get("Provider"), llmConfigs.get("Endpoint"),
    llmConfigs.get("DeploymentName"), llmConfigs.get("apiVersion"),
    bool(llmConfigs.get("apikey")),
)


class AppSettings:
    def __init__(self):
        _secrets = _fetch_secrets_from_source()

        for key, value in _secrets.items():
            if not isinstance(key, str):
                logger.warning("Skipping non-string settings key of type '%s'.", type(key).__name__)
                continue
            if hasattr(self, key):
                logger.warning("Skipping settings key '%s' because it conflicts with an AppSettings attribute.", key)
                continue
            setattr(self, key, value)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self, redact_sensitive: bool = True) -> dict:
        output: dict = {}
        for key, value in self.__dict__.items():
            if redact_sensitive and _is_sensitive_setting_key(key):
                output[key] = "***REDACTED***"
                continue
            output[key] = value
        return output


settings = AppSettings()
logger.info(
    "[startup] AppSettings loaded — %d secret keys from provider=%r",
    len(settings.__dict__), _DEFAULT_SECRET_PROVIDER,
)
