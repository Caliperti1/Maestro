"""Small bridge from subsystem-specific classifiers to Maestro's hosted LLM client."""

import json
from typing import Any

from app.llm.client import OpenAILLMClient


def hosted_structured_response(
    *,
    provider: str,
    model: str,
    instructions: str,
    input_payload: dict[str, Any],
    schema_name: str,
    schema: dict[str, Any],
) -> dict[str, Any] | None:
    if provider not in {"openai", "openrouter"}:
        return None
    try:
        content = OpenAILLMClient(provider=provider, model=model).text_response(
            instructions=(
                f"{instructions}\n\n"
                f"Return only one JSON object matching this JSON Schema. "
                f"Do not wrap it in a {schema_name} property: "
                f"{json.dumps(schema, default=str)}"
            ),
            input_text=json.dumps(input_payload, default=str),
        )
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```")
            cleaned = cleaned.removesuffix("```").strip()
        payload = json.loads(cleaned)
        if (
            isinstance(payload, dict)
            and set(payload) == {schema_name}
            and isinstance(payload[schema_name], dict)
        ):
            payload = payload[schema_name]
        return payload if isinstance(payload, dict) else None
    except Exception:
        # These classifiers are advisory. Their callers retain deterministic fallbacks,
        # so a provider outage or malformed response must not fail the user workflow.
        return None
