from typing import Any

DOMAIN_HINTS: dict[str, list[str]] = {
    "personal": ["personal", "calendar", "family", "life", "preference"],
    "maestro-development": ["maestro", "orchestrator", "agent", "memory", "system", "code"],
    "praxis": ["praxis", "partner", "tactical innovation", "transition", "training"],
    "ophi": ["ophi", "product", "market", "research"],
    "usma": ["usma", "cadet", "class", "teaching", "academic"],
    "personal-irad-projects": ["irad", "prototype", "project"],
    "l3": ["l3"],
}

INTENT_TYPE_BY_WORK_ITEM: dict[str, str] = {
    "workflow_task": "workflow",
    "standalone_task": "task",
    "contact": "contact",
    "event": "event",
    "decision": "decision",
    "rfi": "rfi",
    "memory_candidate": "memory_route",
    "think_tank": "direct_chat",
    "direct_response": "direct_chat",
}

ROUTE_TYPE_BY_WORK_ITEM: dict[str, str] = {
    "standalone_task": "task",
    "contact": "contact",
    "event": "event",
    "decision": "decision_log",
    "rfi": "human_input",
    "think_tank": "think_tank",
}

ACTION_BY_WORK_ITEM_TYPE: dict[str, str] = {
    "workflow_task": "Generate agent-specific tasking and execute after approval.",
    "standalone_task": "Route as a task/due-out unless it becomes part of a workflow.",
    "contact": "Route as contact or CRM context.",
    "event": "Route as event/calendar context.",
    "decision": "Route as an auditable decision.",
    "rfi": "Ask you directly or surface as human input needed.",
    "memory_candidate": "Stage for memory curation at session close.",
    "think_tank": "Capture as a think tank note until it matures.",
    "direct_response": "Respond directly without workflow execution.",
}

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "have",
    "in",
    "is",
    "it",
    "me",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "this",
    "to",
    "we",
    "with",
    "you",
}


def meaningful_tokens(text: str, *, extra_stopwords: set[str] | None = None) -> set[str]:
    stopwords = STOPWORDS | (extra_stopwords or set())
    normalized = "".join(character if character.isalnum() else " " for character in text.lower())
    return {
        token
        for token in normalized.split()
        if len(token) > 2 and token not in stopwords
    }


def route_type_for_work_item(work_item_type: str) -> str | None:
    return ROUTE_TYPE_BY_WORK_ITEM.get(work_item_type)


def action_for_work_item(work_item_type: str, fallback: str | None = None) -> str | None:
    return ACTION_BY_WORK_ITEM_TYPE.get(work_item_type, fallback)


def intent_type_for_work_item(work_item_type: str) -> str:
    return INTENT_TYPE_BY_WORK_ITEM.get(work_item_type, "direct_chat")


def domain_matches(lowered_input: str, domain: dict[str, Any]) -> bool:
    key = str(domain.get("key") or "")
    if not key:
        return False
    if key in lowered_input or key.replace("-", " ") in lowered_input:
        return True
    return any(token in lowered_input for token in DOMAIN_HINTS.get(key, []))
