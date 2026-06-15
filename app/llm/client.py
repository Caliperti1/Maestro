import json
from typing import Any, Protocol

from app.core.config import get_settings


class LLMClientError(RuntimeError):
    pass


class LLMClient(Protocol):
    def structured_response(
        self,
        *,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        pass


class OpenAILLMClient:
    def __init__(self, *, api_key: str | None = None, model: str | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.llm_model
        if not self.api_key:
            raise LLMClientError("OPENAI_API_KEY is required for live LLM memory extraction.")

    def structured_response(
        self,
        *,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMClientError("Install the `openai` package to use live LLM calls.") from exc

        client = OpenAI(api_key=self.api_key)
        response = client.responses.create(
            model=self.model,
            instructions=instructions,
            input=input_text,
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        )
        try:
            return json.loads(response.output_text)
        except json.JSONDecodeError as exc:
            raise LLMClientError("LLM returned non-JSON output.") from exc
