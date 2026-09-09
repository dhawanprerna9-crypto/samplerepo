import logging
import os

from agent_framework import BaseChatClient
from agent_framework.foundry import FoundryChatClient
from azure.core.credentials import TokenCredential
from azure.identity import ClientSecretCredential, DefaultAzureCredential, ManagedIdentityCredential

from src.config import AppSettings


logger = logging.getLogger(__name__)


def _has_non_empty_env(name: str) -> bool:
    value = os.getenv(name)
    return bool(value and value.strip())


def _managed_identity_endpoint_detected() -> bool:
    return any(
        _has_non_empty_env(name)
        for name in (
            "IDENTITY_ENDPOINT",
            "MSI_ENDPOINT",
            "IMDS_ENDPOINT",
        )
    )


def _running_in_containerized_host() -> bool:
    return any(
        _has_non_empty_env(name)
        for name in (
            "CONTAINER_APP_NAME",
            "CONTAINER_APP_REVISION",
            "CONTAINER_APP_HOSTNAME",
            "KUBERNETES_SERVICE_HOST",
        )
    )


def _create_credential() -> TokenCredential:
    tenant_id = (os.getenv("AZURE_TENANT_ID") or "").strip()
    client_id = (os.getenv("AZURE_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("AZURE_CLIENT_SECRET") or "").strip()

    if tenant_id and client_id and client_secret:
        logger.info(
            "Using ClientSecretCredential from AZURE_TENANT_ID/AZURE_CLIENT_ID/AZURE_CLIENT_SECRET."
        )
        return ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        )

    managed_identity_client_id = (os.getenv("MANAGED_IDENTITY_CLIENT_ID") or client_id or "").strip() or None

    if _managed_identity_endpoint_detected():
        logger.info(
            "Using ManagedIdentityCredential%s.",
            " with explicit client id" if managed_identity_client_id else "",
        )
        return ManagedIdentityCredential(client_id=managed_identity_client_id)

    if _running_in_containerized_host():
        raise RuntimeError(
            "Container App is missing Azure credential configuration. "
            "Set AZURE_TENANT_ID/AZURE_CLIENT_ID/AZURE_CLIENT_SECRET for service principal "
            "or enable managed identity on the Container App."
        )

    logger.info(
        "Using DefaultAzureCredential%s.",
        " with managed_identity_client_id" if managed_identity_client_id else "",
    )
    return DefaultAzureCredential(
        managed_identity_client_id=managed_identity_client_id,
        exclude_interactive_browser_credential=True,
        exclude_visual_studio_code_credential=True,
    )


def create_responses_client(
    settings: AppSettings,
) -> BaseChatClient:
    # Route agent LLM calls through LiteLLM when enabled. LiteLLMChatClient wraps
    # src.litellm_factory.create_llm() and calls litellm.acompletion under the
    # hood (Chat Completions wire format), so it honours the same proxy/direct
    # routing, guardrails and fallbacks as the rest of the project — and does not
    # depend on the LiteLLM proxy exposing the OpenAI Responses API.
    if settings.use_litellm:
        from src.litellm_factory_maf import LiteLLMChatClient

        return LiteLLMChatClient()

    return FoundryChatClient(
        project_endpoint=settings.project_endpoint,
        model=settings.model_deployment_name,
        credential=_create_credential(),
    )
