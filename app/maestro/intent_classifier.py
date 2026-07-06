import json
from typing import Any, Literal
from urllib import error, request

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import get_settings

MaestroMessageIntent = Literal[
    "new_workflow",
    "delete_workflow",
    "refined",
    "rfi_answered",
    "routed",
    "side_chat",
]


class MaestroIntentClassifierResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: MaestroMessageIntent
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class OllamaMaestroIntentClassifier:
    def __init__(self, *, model: str | None = None, base_url: str | None = None, timeout_seconds: float | None = None):
        settings = get_settings()
        self.model = model or settings.maestro_intent_classifier_model
        self.base_url = (base_url or settings.maestro_intent_classifier_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.maestro_intent_classifier_timeout_seconds

    def classify(
        self,
        *,
        message: str,
        active_plan: dict[str, Any],
        has_blocking_rfi: bool,
    ) -> MaestroIntentClassifierResponse | None:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Classify Chris's latest Maestro chat message. Return JSON only with "
                        "intent, confidence, and reason. Allowed intents: new_workflow, "
                        "delete_workflow, refined, rfi_answered, routed, side_chat. "
                        "Use delete_workflow for commands like clear/cancel/delete/archive the "
                        "current workflow or plan. Use side_chat for questions/explanations that "
                        "do not change work. Use routed for notes/tasks/contacts/events to store. "
                        "Use refined only when the message changes the active plan. Use "
                        "new_workflow only when Chris is starting separate agent work."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": message,
                            "has_blocking_rfi": has_blocking_rfi,
                            "active_plan": active_plan,
                        },
                        default=str,
                    ),
                },
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, TimeoutError, json.JSONDecodeError):
            return None
        message_body = body.get("message") if isinstance(body, dict) else None
        content = message_body.get("content") if isinstance(message_body, dict) else None
        if not content:
            return None
        try:
            return MaestroIntentClassifierResponse.model_validate_json(content)
        except (ValidationError, ValueError):
            return None


def classify_active_message_with_local_llm(
    *,
    message: str,
    active_plan: dict[str, Any],
    has_blocking_rfi: bool,
) -> str | None:
    settings = get_settings()
    if settings.maestro_intent_classifier_provider != "ollama":
        return None
    response = OllamaMaestroIntentClassifier().classify(
        message=message,
        active_plan=active_plan,
        has_blocking_rfi=has_blocking_rfi,
    )
    if response is None or response.confidence < 0.68:
        return None
    return response.intent
