"""Immediate, non-delegated read tools available to Maestro Knowledge mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db.models import Domain
from app.issues.service import ProductIssueService, issue_payload
from app.llm.client import OpenAILLMClient
from app.memory.federated_retrieval import (
    STORE_NAMES,
    FederatedRetrievalRequest,
    FederatedRetrievalService,
)
from app.memory.routed_retrieval import RoutedRetrievalService

READ_ACTIONS = {"context.search", "web.search", "issue.search", "issue.get"}
ROUTED_CONTEXT_STORES = {"contacts", "organizations", "events", "todos", "decisions"}


@dataclass(frozen=True)
class KnowledgeActionResult:
    action_type: str
    status: str
    message: str
    object_type: str | None = None
    object_id: str | None = None
    data: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "status": self.status,
            "message": self.message,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "data": self.data,
        }


class KnowledgeReadToolService:
    def __init__(
        self,
        session: Session,
        *,
        domain_resolver: Callable[..., Domain | None],
        web_client: OpenAILLMClient | None = None,
    ):
        self.session = session
        self.domain_resolver = domain_resolver
        self.web_client = web_client

    def execute(self, action_type: str, arguments: dict[str, Any]) -> KnowledgeActionResult:
        if action_type == "context.search":
            return self._search_context(arguments)
        if action_type == "web.search":
            return self._search_web(arguments)
        if action_type in {"issue.search", "issue.get"}:
            return self._search_issues(action_type, arguments)
        raise ValueError(f"Unsupported Knowledge read action: {action_type}")

    def _search_context(self, arguments: dict[str, Any]) -> KnowledgeActionResult:
        query_text = str(
            arguments.get("query_text") or arguments.get("query") or arguments.get("prompt") or ""
        ).strip()
        if not query_text:
            raise ValueError("A context search needs a focused query.")
        domain = self.domain_resolver(arguments.get("domain_key"), required=False)
        requested_stores = _context_stores(arguments.get("stores") or arguments.get("store"))
        limit = _bounded_int(arguments.get("max_items"), default=6, minimum=1, maximum=12)
        max_chars = _bounded_int(
            arguments.get("max_chars"), default=4500, minimum=500, maximum=7000
        )
        routed_requested = (
            ROUTED_CONTEXT_STORES
            if requested_stores is None
            else requested_stores & ROUTED_CONTEXT_STORES
        )
        federated_requested = (
            STORE_NAMES - ROUTED_CONTEXT_STORES
            if requested_stores is None
            else requested_stores - ROUTED_CONTEXT_STORES
        )
        routed_payload: dict[str, list[dict[str, Any]]] = {}
        routed_text = ""
        if routed_requested:
            routed_bundle = RoutedRetrievalService(self.session).build_context_bundle(
                domain_id=domain.id if domain else None,
                query_text=query_text,
                limit=limit,
                max_chars=max_chars,
                egress_target="external",
            )
            routed_payload = {
                _public_store_name(key): values
                for key, values in routed_bundle.stores.items()
                if _public_store_name(key) in routed_requested
            }
            routed_text = routed_bundle.rendered_text
        federated_results: list[dict[str, Any]] = []
        federated_text = ""
        semantic_status = "not_requested"
        if federated_requested:
            bundle = FederatedRetrievalService(self.session).retrieve(
                FederatedRetrievalRequest(
                    query_text=query_text,
                    audience="maestro",
                    domain_id=domain.id if domain else None,
                    egress_target="external",
                    stores=federated_requested,
                    max_items=limit,
                    max_chars=max_chars,
                    use_semantic=True,
                )
            )
            semantic_status = bundle.semantic_status
            federated_text = bundle.rendered_text
            federated_results = [
                {
                    "store": result.document.store,
                    "source_id": result.document.source_id,
                    "domain_key": result.domain_key,
                    "title": result.document.title,
                    "content": result.document.content[:1200],
                    "score": round(result.score, 4),
                    "source_timestamp": (
                        result.document.source_timestamp.isoformat()
                        if result.document.source_timestamp
                        else None
                    ),
                }
                for result in bundle.results
            ]
        rendered = "\n\n".join(part for part in (routed_text, federated_text) if part)[:max_chars]
        match_count = sum(len(values) for values in routed_payload.values()) + len(
            federated_results
        )
        return KnowledgeActionResult(
            "context.search",
            "completed",
            f"Retrieved {match_count} current context result{'s' if match_count != 1 else ''}.",
            "context",
            data={
                "query_text": query_text,
                "domain_key": domain.key if domain else None,
                "stores": sorted(requested_stores or STORE_NAMES),
                "match_count": match_count,
                "semantic_status": semantic_status,
                "rendered_text": rendered,
                "routed": routed_payload,
                "federated": federated_results,
            },
        )

    def _search_web(self, arguments: dict[str, Any]) -> KnowledgeActionResult:
        query = str(arguments.get("query") or arguments.get("query_text") or "").strip()
        if not query:
            raise ValueError("A web search needs a focused query.")
        parameters = _web_search_parameters(arguments)
        client = self.web_client or OpenAILLMClient()
        result = client.web_search_response(
            instructions=(
                "You are Maestro's immediate web research capability. Search only as needed, "
                "return concise factual findings, preserve source citations, and distinguish "
                "facts from inference."
            ),
            input_text=query,
            search_parameters=parameters,
        )
        annotations = result.get("annotations") or []
        return KnowledgeActionResult(
            "web.search",
            "completed",
            "Completed a current web search.",
            "web_result",
            data={
                "query": query,
                "output_text": str(result.get("output_text") or "")[:7000],
                "citations": _web_citations(annotations),
                "annotations": annotations[:12],
                "usage": result.get("usage"),
            },
        )

    def _search_issues(
        self,
        action_type: str,
        arguments: dict[str, Any],
    ) -> KnowledgeActionResult:
        service = ProductIssueService(self.session)
        query = str(
            arguments.get("query")
            or arguments.get("query_text")
            or arguments.get("title")
            or arguments.get("id")
            or ""
        ).strip()
        if not query:
            raise ValueError("An issue search needs an issue ID, title, or focused query.")
        issues = service.search(
            query=query,
            domain_key=str(arguments.get("domain_key") or "").strip() or None,
            project_key=str(arguments.get("project_key") or "").strip() or None,
            repository_key=str(arguments.get("repository_key") or "").strip() or None,
            status=str(arguments.get("status") or "").strip() or None,
            limit=1 if action_type == "issue.get" else _bounded_int(
                arguments.get("max_items"), default=8, minimum=1, maximum=20
            ),
        )
        return KnowledgeActionResult(
            action_type,
            "completed",
            f"Retrieved {len(issues)} product issue{'s' if len(issues) != 1 else ''}.",
            "product_issue",
            data={"query": query, "issues": [issue_payload(issue) for issue in issues]},
        )


def _context_stores(value: Any) -> set[str] | None:
    if value in (None, "", []):
        return None
    values = value if isinstance(value, list) else str(value).split(",")
    aliases = {
        "calendar": "events",
        "event": "events",
        "contact": "contacts",
        "organization": "organizations",
        "entity": "organizations",
        "entities": "organizations",
        "todo": "todos",
        "task": "todos",
        "idea": "issues",
        "brainstorm": "issues",
        "decision": "decisions",
        "report": "reports",
        "workflow_report": "reports",
        "run_logs": "run_log",
        "artifact": "artifacts",
        "memories": "memory",
        "issue": "issues",
        "story": "issues",
        "backlog": "issues",
    }
    normalized = {
        aliases.get(str(item).strip().lower(), str(item).strip().lower())
        for item in values
        if str(item).strip()
    }
    invalid = normalized - STORE_NAMES
    if invalid:
        raise ValueError(f"Unknown context stores: {', '.join(sorted(invalid))}.")
    return normalized


def _public_store_name(value: str) -> str:
    return "organizations" if value == "entities" else value


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _web_search_parameters(arguments: dict[str, Any]) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for key in ("max_results", "max_total_results", "max_characters"):
        if arguments.get(key) not in (None, ""):
            parameters[key] = _bounded_int(arguments[key], default=5, minimum=1, maximum=100_000)
    context_size = str(arguments.get("search_context_size") or "").strip().lower()
    if context_size in {"low", "medium", "high"}:
        parameters["search_context_size"] = context_size
    for key in ("allowed_domains", "excluded_domains"):
        raw = arguments.get(key)
        values = raw if isinstance(raw, list) else str(raw or "").split(",")
        cleaned = [str(item).strip() for item in values if str(item).strip()]
        if cleaned:
            parameters[key] = cleaned
    return parameters


def _web_citations(annotations: list[Any]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        citation = annotation.get("url_citation")
        if not isinstance(citation, dict):
            continue
        citations.append(
            {
                "url": citation.get("url"),
                "title": citation.get("title"),
                "content": citation.get("content"),
            }
        )
    return citations
