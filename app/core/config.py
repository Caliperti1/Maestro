from functools import lru_cache
from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Maestro"
    app_host: str = "0.0.0.0"
    app_port: Annotated[int, Field(ge=1, le=65535)] = 8000
    frontend_origin: str = "http://localhost:5173"
    cors_allow_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    database_url: str = "postgresql+psycopg://maestro:maestro@localhost:55432/maestro"
    openai_api_key: str | None = None
    llm_model: str = "gpt-5.5"
    memory_dropbox_root: str = "maestro_dropbox"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
