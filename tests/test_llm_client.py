import pytest

from app.llm.client import LLMClientError, OpenAILLMClient


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


def test_unknown_llm_provider_is_rejected() -> None:
    with pytest.raises(LLMClientError, match="Unsupported LLM_PROVIDER"):
        OpenAILLMClient(provider="unknown", api_key="test-key")
