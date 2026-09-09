from configparser import ConfigParser
from dataclasses import dataclass, field
import os
from pathlib import Path


@dataclass(frozen=True)
class RuntimeLimits:
    max_round_count: int = 16
    max_stall_count: int = 3
    max_reset_count: int = 2
    max_agent_tokens: int = 8000
    autonomous_turn_limit: int = 12


@dataclass(frozen=True)
class AppSettings:
    # Foundry (Azure AI) path — required only when use_litellm is False.
    project_endpoint: str | None = None
    model_deployment_name: str | None = None
    # LiteLLM proxy path — required only when use_litellm is True.
    use_litellm: bool = False
    litellm_proxy_url: str | None = None
    litellm_api_key: str | None = None
    litellm_model: str | None = None
    # Runtime hosting mode for main.py server path.
    target_deployment: str = "foundry"
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.ini"
# LiteLLM settings live in llmConfig.ini (copied next to config.ini at the project root),
# matching the convention used by the rest of the agent-builder toolchain.
DEFAULT_LLM_CONFIG_PATH = Path(__file__).resolve().parents[1] / "llmConfig.ini"


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _first_env(*keys: str) -> str | None:
    for key in keys:
        value = _first_non_empty(os.getenv(key))
        if value is not None:
            return value
    return None


def _first_config_value(
    config: ConfigParser,
    section: str,
    *keys: str,
) -> str | None:
    if not config.has_section(section):
        return None

    for key in keys:
        value = _first_non_empty(config.get(section, key, fallback=None))
        if value is not None:
            return value
    return None


def _load_config(config_path: Path) -> ConfigParser:
    # inline comments are used in llmConfig.ini (e.g. "azurekeyvault # ..."), so honor them.
    config = ConfigParser(inline_comment_prefixes=("#", ";"))
    if config_path.exists():
        config.read(config_path, encoding="utf-8")
    return config


def _as_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _looks_like_placeholder(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if not normalized:
        return False
    if "placeholder{" in normalized and normalized.endswith("}"):
        return True
    return (normalized.startswith("{") and normalized.endswith("}")) or (
        normalized.startswith("${") and normalized.endswith("}")
    )


def _normalize_target_deployment(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    aliases = {
        "foundry": "foundry",
        "azurefoundry": "foundry",
        "azure-foundry": "foundry",
        "azure_foundry": "foundry",
        "aca": "aca",
        "containerapps": "aca",
        "container-apps": "aca",
        "container_apps": "aca",
        "azurecontainerapps": "aca",
        "azure-container-apps": "aca",
        "azure_container_apps": "aca",
    }
    return aliases.get(normalized, "foundry")


def _read_target_deployment(config: ConfigParser) -> str:
    raw_value = _first_non_empty(
        _first_env("TARGET_DEPLOYMENT", "TARGET_DEPLOYMET"),
        _first_config_value(
            config,
            "deployment",
            "targetDeployment",
            "targetDeploymet",
            "targetdeployment",
        ),
        _first_config_value(
            config,
            "azure",
            "targetDeployment",
            "targetDeploymet",
            "targetdeployment",
        ),
    )
    if _looks_like_placeholder(raw_value):
        return "foundry"
    return _normalize_target_deployment(raw_value)


def load_settings(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    llm_config_path: str | Path = DEFAULT_LLM_CONFIG_PATH,
) -> AppSettings:
    config_file = Path(config_path)
    config = _load_config(config_file)
    llm_config = _load_config(Path(llm_config_path))
    target_deployment = _read_target_deployment(config)

    # Master toggle: env override first, then [appsettings].use_litellm in llmConfig.ini.
    use_litellm = _as_bool(
        _first_non_empty(
            os.getenv("USE_LITELLM"),
            llm_config.get("appsettings", "use_litellm", fallback=None)
            if llm_config.has_section("appsettings")
            else None,
        )
    )

    if use_litellm:
        return _load_litellm_settings(llm_config, target_deployment)

    # Keep deployment/runtime environment values authoritative over local config.ini defaults.
    endpoint = _first_env(
        "FOUNDRY_PROJECT_ENDPOINT",
        "AZURE_AI_PROJECT_ENDPOINT",
        "PROJECT_ENDPOINT",
    )
    if endpoint is None:
        endpoint = _first_config_value(
            config,
            "azure",
            "project_endpoint",
            "azure_ai_project_endpoint",
            "endpoint",
        )

    deployment = _first_env(
        "AZURE_AI_MODEL_DEPLOYMENT_NAME",
        "MODEL_DEPLOYMENT_NAME",
        "MODEL_NAME",
    )
    if deployment is None:
        deployment = _first_config_value(
            config,
            "azure",
            "model_deployment_name",
            "azure_ai_model_deployment_name",
            "deployment_name",
            "model",
        )

    missing: list[str] = []
    if not endpoint:
        missing.append(
            "FOUNDRY_PROJECT_ENDPOINT (or AZURE_AI_PROJECT_ENDPOINT, or PROJECT_ENDPOINT, or "
            "[azure].project_endpoint in config.ini)"
        )
    if not deployment:
        missing.append(
            "AZURE_AI_MODEL_DEPLOYMENT_NAME (or MODEL_DEPLOYMENT_NAME, or MODEL_NAME, or "
            "[azure].model_deployment_name in config.ini)"
        )

    if missing:
        raise RuntimeError(
            "Missing required settings: " + ", ".join(missing)
        )

    return AppSettings(
        project_endpoint=endpoint,
        model_deployment_name=deployment,
        use_litellm=False,
        target_deployment=target_deployment,
    )


def _load_litellm_settings(llm_config: ConfigParser, target_deployment: str) -> AppSettings:
    # LiteLLM proxy is OpenAI-compatible; agents route through it via OpenAIChatClient.
    # Env vars win over [litellm_gateway] in llmConfig.ini.
    proxy_url = _first_env("LITELLM_PROXY_URL", "LITELLM_BASE_URL")
    if proxy_url is None:
        proxy_url = _first_config_value(llm_config, "litellm_gateway", "proxy_url")

    api_key = _first_env("LITELLM_API_KEY", "LITELLM_PROXY_API_KEY")
    if api_key is None:
        api_key = _first_config_value(llm_config, "litellm_gateway", "key")

    model = _first_env("LITELLM_MODEL")
    if model is None:
        model = _first_config_value(llm_config, "litellm_gateway", "model")

    missing: list[str] = []
    if not proxy_url:
        missing.append("LITELLM_PROXY_URL (or [litellm_gateway].proxy_url in llmConfig.ini)")
    if not api_key:
        missing.append("LITELLM_API_KEY (or [litellm_gateway].key in llmConfig.ini)")
    if not model:
        missing.append("LITELLM_MODEL (or [litellm_gateway].model in llmConfig.ini)")

    if missing:
        raise RuntimeError(
            "Missing required LiteLLM settings: " + ", ".join(missing)
        )

    return AppSettings(
        use_litellm=True,
        litellm_proxy_url=proxy_url,
        litellm_api_key=api_key,
        litellm_model=model,
        target_deployment=target_deployment,
    )
