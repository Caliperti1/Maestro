import pytest

from app.core.config import Settings
from app.llm.client import LLMClientError, OllamaLLMClient, OpenAILLMClient
from app.memory.routed_resolver import OllamaRoutedResolverLLM


def test_openrouter_client_uses_openrouter_config() -> None:
    client = OpenAILLMClient(
        provider="openrouter",
        api_key="test-openrouter-key",
        model="openai/gpt-5.5",
    )

    assert client.provider == "openrouter"
    assert client.api_key == "test-openrouter-key"
    assert client.model == "openai/gpt-5.5"
    assert client.base_url == "https://openrouter.ai/api/v1"
    assert client.default_headers["X-OpenRouter-Title"] == "Maestro"
    assert client.max_output_tokens == 8192
    assert callable(client.structured_response)
    assert callable(client.text_response)
    assert callable(client.web_search_response)


def test_openai_client_uses_direct_openai_config() -> None:
    client = OpenAILLMClient(
        provider="openai",
        api_key="test-openai-key",
        model="gpt-5.5",
    )

    assert client.provider == "openai"
    assert client.api_key == "test-openai-key"
    assert client.model == "gpt-5.5"
    assert client.base_url is None
    assert callable(client.structured_response)
    assert callable(client.text_response)


def test_unknown_llm_provider_is_rejected() -> None:
    with pytest.raises(LLMClientError, match="Unsupported LLM_PROVIDER"):
        OpenAILLMClient(provider="unknown", api_key="test-key")


def test_ollama_client_does_not_require_api_key() -> None:
    client = OllamaLLMClient(model="qwen3:8b", base_url="http://localhost:11434")

    assert client.provider == "ollama"
    assert client.model == "qwen3:8b"
    assert client.base_url == "http://localhost:11434"
    assert client.timeout_seconds == Settings().ollama_llm_timeout_seconds
    assert not hasattr(client, "api_key")
    assert callable(client.structured_response)
    assert callable(client.text_response)


def test_local_llm_defaults_allow_cold_ollama_starts() -> None:
    settings = Settings()

    assert settings.maestro_intent_classifier_timeout_seconds >= 5
    assert settings.maestro_topic_resolver_timeout_seconds >= 5
    assert settings.ollama_llm_timeout_seconds >= 300
    assert settings.routed_resolver_llm_timeout_seconds >= 20
    assert settings.routed_enricher_llm_timeout_seconds >= 20


def test_routed_resolver_uses_configured_ollama_timeout() -> None:
    client = OllamaRoutedResolverLLM(timeout_seconds=12.5)

    assert client.timeout_seconds == 12.5
