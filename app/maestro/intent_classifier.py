import json
from typing import Any, Literal
from urllib import error, request

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import get_settings
from app.prompts import load_prompt

MaestroMessageIntent = Literal[
    "new_workflow",
    "delete_workflow",
    "refined",
    "rfi_answered",
    "routed",
    "side_chat",
]

MaestroIntentType = Literal[
    "chat_response",
    "workflow_request",
    "routed_item",
    "rfi_answer",
    "plan_refinement",
    "plan_question",
    "system_command",
]

MaestroIntentNextStep = Literal[
    "respond",
    "plan",
    "route",
    "refine_plan",
    "answer_plan_question",
    "execute_system_command",
    "ask_clarifying_question",
    "no_action",
]

MaestroWorkflowTiming = Literal[
    "unspecified",
    "one_time",
    "scheduled",
    "recurring",
    "triggered",
    "modify_schedule",
    "delete_schedule",
]

MaestroMessageNextStep = Literal[
    "respond",
    "plan",
    "route",
    "answer_and_refine_plan",
    "refine_plan",
    "execute_system_command",
    "ask_clarifying_question",
    "no_action",
]


class MaestroIntentClassifierResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: MaestroMessageIntent
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class MaestroClassifiedIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: MaestroIntentType
    span: str
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_next_step: MaestroIntentNextStep
    workflow_timing: MaestroWorkflowTiming = "unspecified"
    schedule_details: dict[str, Any] = Field(default_factory=dict)
    rfi_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class MaestroMessageUnderstandingResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic_scope: Literal["active_topic", "new_topic", "existing_topic", "global_system"]
    relationship_to_active_plan: Literal[
        "none",
        "answers_rfi",
        "refines_plan",
        "asks_about_plan",
        "unrelated",
    ]
    intents: list[MaestroClassifiedIntent]
    recommended_next_step: MaestroMessageNextStep
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str

    def legacy_intent(self) -> MaestroMessageIntent:
        high_confidence = [intent for intent in self.intents if intent.confidence >= 0.55]
        intent_types = {intent.type for intent in high_confidence}
        if "system_command" in intent_types and self.recommended_next_step == "execute_system_command":
            return "delete_workflow"
        if "rfi_answer" in intent_types or self.relationship_to_active_plan == "answers_rfi":
            return "rfi_answered"
        if "plan_refinement" in intent_types or self.relationship_to_active_plan == "refines_plan":
            return "refined"
        if "workflow_request" in intent_types or self.recommended_next_step == "plan":
            return "new_workflow"
        if "routed_item" in intent_types or self.recommended_next_step == "route":
            return "routed"
        if "plan_question" in intent_types or "chat_response" in intent_types:
            return "side_chat"
        return "side_chat"


class MaestroTopicResolverResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scope: Literal["active_topic", "new_topic", "existing_topic", "global_system"]
    topic_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    suggested_title: str | None = None


class OllamaMaestroIntentClassifier:
    def __init__(self, *, model: str | None = None, base_url: str | None = None, timeout_seconds: float | None = None):
        settings = get_settings()
        self.model = model or settings.maestro_intent_classifier_model
        self.base_url = (base_url or settings.maestro_intent_classifier_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.maestro_intent_classifier_timeout_seconds

    def understand(
        self,
        *,
        message: str,
        active_plan: dict[str, Any],
        has_blocking_rfi: bool,
    ) -> MaestroMessageUnderstandingResponse | None:
        payload = {
            "model": self.model,
            "stream": False,
            "format": MaestroMessageUnderstandingResponse.model_json_schema(),
            "messages": [
                {
                    "role": "system",
                    "content": load_prompt("maestro_message_understanding.md"),
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
            return MaestroMessageUnderstandingResponse.model_validate_json(content)
        except (ValidationError, ValueError):
            return None

    def classify(
        self,
        *,
        message: str,
        active_plan: dict[str, Any],
        has_blocking_rfi: bool,
    ) -> MaestroIntentClassifierResponse | None:
        understood = self.understand(
            message=message,
            active_plan=active_plan,
            has_blocking_rfi=has_blocking_rfi,
        )
        if understood is None:
            return None
        return MaestroIntentClassifierResponse(
            intent=understood.legacy_intent(),
            confidence=understood.confidence,
            reason=understood.reason,
        )


class OllamaMaestroTopicResolver:
    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ):
        settings = get_settings()
        self.model = model or settings.maestro_topic_resolver_model
        self.base_url = (base_url or settings.maestro_topic_resolver_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds or settings.maestro_topic_resolver_timeout_seconds

    def resolve(
        self,
        *,
        message: str,
        active_topic: dict[str, Any] | None,
        recent_topics: list[dict[str, Any]],
    ) -> MaestroTopicResolverResponse | None:
        topic_ids = {
            str(topic.get("id"))
            for topic in [active_topic or {}, *recent_topics]
            if topic.get("id")
        }
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {
                    "role": "system",
                    "content": load_prompt("maestro_topic_resolver.md"),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": message,
                            "active_topic": active_topic,
                            "recent_topics": recent_topics[:8],
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
            resolved = MaestroTopicResolverResponse.model_validate_json(content)
        except (ValidationError, ValueError):
            return None
        if resolved.scope == "existing_topic" and str(resolved.topic_id or "") not in topic_ids:
            return None
        return resolved


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


def understand_message_with_local_llm(
    *,
    message: str,
    active_plan: dict[str, Any],
    has_blocking_rfi: bool,
) -> MaestroMessageUnderstandingResponse | None:
    settings = get_settings()
    if settings.maestro_intent_classifier_provider != "ollama":
        return None
    response = OllamaMaestroIntentClassifier().understand(
        message=message,
        active_plan=active_plan,
        has_blocking_rfi=has_blocking_rfi,
    )
    if response is None or response.confidence < 0.60:
        return None
    return response


def resolve_topic_with_local_llm(
    *,
    message: str,
    active_topic: dict[str, Any] | None,
    recent_topics: list[dict[str, Any]],
) -> MaestroTopicResolverResponse | None:
    settings = get_settings()
    if settings.maestro_topic_resolver_provider != "ollama":
        return None
    response = OllamaMaestroTopicResolver().resolve(
        message=message,
        active_topic=active_topic,
        recent_topics=recent_topics,
    )
    if response is None or response.confidence < settings.maestro_topic_resolver_confidence_threshold:
        return None
    return response
