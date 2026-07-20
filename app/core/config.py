from functools import lru_cache
from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Maestro"
    user_display_name: str = "Chris"
    user_full_name: str = "Chris Aliperti"
    user_email: str = "chris.aliperti@praxis-defense.com"
    app_host: str = "0.0.0.0"
    app_port: Annotated[int, Field(ge=1, le=65535)] = 8000
    frontend_origin: str = "http://localhost:5174"
    cors_allow_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:5174,http://127.0.0.1:5174,http://0.0.0.0:5174"
    )
    tailscale_frontend_origin: str | None = None
    database_url: str = "postgresql+psycopg://maestro:maestro@localhost:55432/maestro"
    llm_provider: str = "openrouter"
    openai_api_key: str | None = None
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_http_referer: str = "http://localhost:5173"
    openrouter_app_title: str = "Maestro"
    llm_model: str = "openai/gpt-5.6-sol"
    llm_qwen_model_profile: str = "ollama:qwen3:8b"
    llm_luna_model_profile: str = "openrouter:openai/gpt-5.6-luna"
    llm_terra_model_profile: str = "openrouter:openai/gpt-5.6-terra"
    llm_sol_model_profile: str = "openrouter:openai/gpt-5.6-sol"
    llm_max_output_tokens: Annotated[int, Field(ge=256, le=32768)] = 8192
    ollama_llm_timeout_seconds: float = 300.0
    memory_dropbox_root: str = "maestro_dropbox"
    memory_dropbox_autorun: bool = True
    memory_dropbox_interval_seconds: Annotated[int, Field(ge=5, le=3600)] = 30
    home_timezone: str = "America/New_York"
    embedding_provider: str = "ollama"
    embedding_model: str = "nomic-embed-text"
    embedding_base_url: str = "http://localhost:11434"
    embedding_api_key: str | None = None
    embedding_dimensions: int | None = None
    memory_embedding_best_effort: bool = True
    routed_resolver_llm_provider: str = "ollama"
    routed_resolver_llm_model: str = "llama3.1:8b"
    routed_resolver_llm_base_url: str = "http://localhost:11434"
    routed_resolver_llm_timeout_seconds: float = 20.0
    routed_enricher_llm_provider: str = "none"
    routed_enricher_llm_model: str = "llama3.1:8b"
    routed_enricher_llm_base_url: str = "http://localhost:11434"
    routed_enricher_llm_timeout_seconds: float = 20.0
    maestro_intent_classifier_provider: str = "ollama"
    maestro_intent_classifier_model: str = "qwen3:8b"
    maestro_intent_classifier_base_url: str = "http://localhost:11434"
    maestro_intent_classifier_timeout_seconds: float = 10.0
    maestro_topic_resolver_provider: str = "ollama"
    maestro_topic_resolver_model: str = "qwen3:8b"
    maestro_topic_resolver_base_url: str = "http://localhost:11434"
    maestro_topic_resolver_timeout_seconds: float = 10.0
    maestro_topic_resolver_confidence_threshold: float = 0.72
    scheduler_worker_autorun: bool = False
    scheduler_worker_interval_seconds: int = 30
    scheduler_worker_claim_limit: int = 4
    scheduler_worker_execute_llm: bool = True
    scheduler_worker_auto_tool_loop: bool = True

    @property
    def cors_origins(self) -> list[str]:
        origins = [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]
        if self.tailscale_frontend_origin and self.tailscale_frontend_origin not in origins:
            origins.append(self.tailscale_frontend_origin)
        return origins


@lru_cache
def get_settings() -> Settings:
    return Settings()
