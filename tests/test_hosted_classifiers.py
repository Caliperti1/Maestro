from app.core.config import get_settings
from app.db.models import RoutedItem
from app.llm import structured
from app.maestro import intent_classifier
from app.memory import federated_retrieval, routed_resolver


def test_hosted_structured_response_accepts_wrapped_fenced_json(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def text_response(self, **_kwargs):
            return '```json\n{"example_schema":{"value":"ok"}}\n```'

    monkeypatch.setattr(structured, "OpenAILLMClient", FakeClient)

    result = structured.hosted_structured_response(
        provider="openrouter",
        model="example",
        instructions="Classify.",
        input_payload={"message": "test"},
        schema_name="example_schema",
        schema={"type": "object"},
    )

    assert result == {"value": "ok"}


def test_hosted_intent_and_topic_classifiers_use_structured_provider(monkeypatch):
    monkeypatch.setenv("MAESTRO_INTENT_CLASSIFIER_PROVIDER", "openrouter")
    monkeypatch.setenv("MAESTRO_TOPIC_RESOLVER_PROVIDER", "openrouter")
    get_settings.cache_clear()

    def fake_structured_response(**kwargs):
        if kwargs["schema_name"] == "maestro_message_understanding":
            return {
                "topic_scope": "new_topic",
                "relationship_to_active_plan": "none",
                "intents": [
                    {
                        "type": "workflow_request",
                        "span": "Plan the trip",
                        "confidence": 0.94,
                        "recommended_next_step": "plan",
                        "reason": "The user requested work.",
                    }
                ],
                "recommended_next_step": "plan",
                "confidence": 0.94,
                "reason": "The user requested work.",
            }
        return {
            "scope": "new_topic",
            "topic_id": None,
            "confidence": 0.91,
            "reason": "This starts a new subject.",
            "suggested_title": "Trip planning",
        }

    monkeypatch.setattr(intent_classifier, "hosted_structured_response", fake_structured_response)

    understood = intent_classifier.understand_message_with_local_llm(
        message="Plan the trip",
        active_plan={},
        has_blocking_rfi=False,
    )
    topic = intent_classifier.resolve_topic_with_local_llm(
        message="Plan the trip",
        active_topic=None,
        recent_topics=[],
    )

    assert understood is not None
    assert understood.legacy_intent() == "new_workflow"
    assert topic is not None
    assert topic.scope == "new_topic"


def test_hosted_retrieval_router_sanitizes_structured_plan(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_ROUTER_PROVIDER", "openrouter")
    get_settings.cache_clear()
    monkeypatch.setattr(
        federated_retrieval,
        "hosted_structured_response",
        lambda **_kwargs: {
            "domains": ["praxis", "not-a-domain"],
            "stores": ["memory", "not-a-store"],
            "query_variants": ["first", "second", "third"],
            "entities": [],
            "current_truth": True,
            "confidence": 0.9,
            "reason": "Hosted routing.",
        },
    )

    plan = federated_retrieval.RetrievalQueryRouter().route(
        query_text="Praxis context",
        available_domains=["praxis"],
        forced_domain=None,
    )

    assert plan.domains == ["praxis"]
    assert plan.stores == ["memory"]
    assert plan.query_variants == ["first", "second", "third"]


def test_hosted_routed_resolver_returns_structured_decision(monkeypatch):
    monkeypatch.setenv("ROUTED_RESOLVER_LLM_PROVIDER", "openrouter")
    get_settings.cache_clear()
    monkeypatch.setattr(
        routed_resolver,
        "hosted_structured_response",
        lambda **_kwargs: {
            "action": "create_new",
            "object_id": None,
            "confidence": 0.88,
            "reason": "No candidate is the same person.",
        },
    )
    item = RoutedItem(
        route_type="contact",
        title="Jane Doe",
        content="New contact",
        source_refs=[],
        metadata_={},
    )

    result = routed_resolver.HostedRoutedResolverLLM().choose_match(
        item=item,
        object_type="contact",
        candidates=[],
    )

    assert result is not None
    assert result["action"] == "create_new"
