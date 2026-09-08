from typing import Dict

from src.secret_manager.secret_factory import SecretFactory


def fetch_all_secrets(provider_type: str, secret_config_path: str = "llmConfig.ini") -> Dict[str, str]:
    factory = SecretFactory.from_secret_config_file(secret_config_path, cache_ttl_seconds=3600)
    all_secrets = factory.get_all_secrets(provider_type)
    return all_secrets
