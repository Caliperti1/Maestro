import json
from typing import Any, Protocol
from urllib import error, request

from app.core.config import get_settings


class LLMClientError(RuntimeError):
    pass


class LLMClient(Protocol):
    model: str
    provider: str

    def structured_response(
        self,
        *,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        pass

    def text_response(
        self,
        *,
        instructions: str,
        input_text: str,
    ) -> str:
        pass


class OpenAILLMClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        base_url: str | None = None,
    ):
        settings = get_settings()
        self.provider = provider or settings.llm_provider
        self.model = model or settings.llm_model
        self.max_output_tokens = settings.llm_max_output_tokens
        self.base_url = base_url
        self.default_headers: dict[str, str] = {}

        if self.provider == "openrouter":
            self.api_key = api_key or settings.openrouter_api_key
            self.base_url = base_url or settings.openrouter_base_url
            self.default_headers = {
                "HTTP-Referer": settings.openrouter_http_referer,
                "X-OpenRouter-Title": settings.openrouter_app_title,
            }
        elif self.provider == "openai":
            self.api_key = api_key or settings.openai_api_key
        else:
            raise LLMClientError(f"Unsupported LLM_PROVIDER: {self.provider}")

        if not self.api_key:
            key_name = "OPENROUTER_API_KEY" if self.provider == "openrouter" else "OPENAI_API_KEY"
            raise LLMClientError(f"{key_name} is required for live LLM calls.")

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

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=self.default_headers or None,
        )
        if self.provider == "openrouter":
            return self._openrouter_structured_response(
                client=client,
                instructions=instructions,
                input_text=input_text,
                schema_name=schema_name,
                schema=schema,
            )

        response = client.responses.create(
            model=self.model,
            instructions=instructions,
            input=input_text,
            max_output_tokens=self.max_output_tokens,
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

    def text_response(
        self,
        *,
        instructions: str,
        input_text: str,
    ) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMClientError("Install the `openai` package to use live LLM calls.") from exc

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=self.default_headers or None,
        )
        if self.provider == "openrouter":
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": input_text},
                ],
                max_tokens=self.max_output_tokens,
            )
            content = response.choices[0].message.content
            if not content:
                raise LLMClientError("LLM returned an empty response.")
            return content

        response = client.responses.create(
            model=self.model,
            instructions=instructions,
            input=input_text,
            max_output_tokens=self.max_output_tokens,
        )
        if not response.output_text:
            raise LLMClientError("LLM returned an empty response.")
        return response.output_text

    def web_search_response(
        self,
        *,
        instructions: str,
        input_text: str,
        search_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.provider != "openrouter":
            raise LLMClientError("web.search currently requires the OpenRouter LLM provider.")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMClientError("Install the `openai` package to use live LLM calls.") from exc

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=self.default_headers or None,
        )
        tool: dict[str, Any] = {"type": "openrouter:web_search"}
        if search_parameters:
            tool["parameters"] = search_parameters
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": input_text},
            ],
            max_tokens=self.max_output_tokens,
            tools=[tool],
        )
        message = response.choices[0].message
        content = message.content or ""
        if not content:
            raise LLMClientError("Web search returned an empty response.")
        annotations = [
            _annotation_to_dict(annotation)
            for annotation in (getattr(message, "annotations", None) or [])
        ]
        usage = getattr(response, "usage", None)
        return {
            "output_text": content,
            "annotations": annotations,
            "usage": _usage_to_dict(usage),
        }

    def _openrouter_structured_response(
        self,
        *,
        client,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": input_text},
            ],
            max_tokens=self.max_output_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise LLMClientError("LLM returned an empty response.")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMClientError("LLM returned non-JSON output.") from exc


class OllamaLLMClient:
    """Small local-chat LLM client for low-cost background Maestro work."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ):
        settings = get_settings()
        self.provider = "ollama"
        self.model = model
        self.base_url = (base_url or settings.embedding_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.ollama_llm_timeout_seconds

    def structured_response(
        self,
        *,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        content = self._chat(
            instructions=(
                f"{instructions}\n\nReturn only JSON for schema `{schema_name}`. "
                "Do not wrap it in markdown."
            ),
            input_text=f"{input_text}\n\nJSON schema:\n{json.dumps(schema)}",
            json_mode=True,
        )
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMClientError("Ollama returned non-JSON output.") from exc

    def text_response(
        self,
        *,
        instructions: str,
        input_text: str,
    ) -> str:
        return self._chat(instructions=instructions, input_text=input_text, json_mode=False)

    def _chat(self, *, instructions: str, input_text: str, json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": input_text},
            ],
        }
        if json_mode:
            payload["format"] = "json"
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, json.JSONDecodeError, error.URLError) as exc:
            raise LLMClientError(f"Ollama chat call failed for model {self.model}.") from exc
        content = raw.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMClientError("Ollama returned an empty response.")
        return content.strip()


def _annotation_to_dict(annotation: Any) -> dict[str, Any]:
    if hasattr(annotation, "model_dump"):
        return annotation.model_dump()
    if isinstance(annotation, dict):
        return annotation
    return {
        key: getattr(annotation, key)
        for key in ("type", "url_citation")
        if hasattr(annotation, key)
    }


def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if isinstance(usage, dict):
        return usage
    return {
        key: getattr(usage, key)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "server_tool_use")
        if hasattr(usage, key)
    }
