"""Immediate, non-delegated read tools available to Maestro Knowledge mode."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Domain, ProductProject, RepositoryProfile, WorkflowDefinition
from app.issues.service import ProductIssueService, issue_payload, issue_search_score
from app.llm.client import OpenAILLMClient
from app.maestro.scheduler import SchedulerService
from app.memory.event_work_links import EventWorkLinkService
from app.memory.federated_retrieval import (
    STORE_NAMES,
    FederatedRetrievalRequest,
    FederatedRetrievalService,
)
from app.memory.routed_retrieval import RoutedRetrievalService

READ_ACTIONS = {
    "context.search",
    "web.search",
    "issue.search",
    "issue.get",
    "workflow.search",
    "workflow.get",
}
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
        if action_type in {"workflow.search", "workflow.get"}:
            return self._search_workflows(action_type, arguments)
        raise ValueError(f"Unsupported Knowledge read action: {action_type}")

    def _search_workflows(
        self,
        action_type: str,
        arguments: dict[str, Any],
    ) -> KnowledgeActionResult:
        query = str(
            arguments.get("query")
            or arguments.get("query_text")
            or arguments.get("target")
            or arguments.get("id")
            or ""
        ).strip()
        if not query:
            raise ValueError("A workflow search needs a name, key, ID, or focused query.")
        include_inactive = bool(arguments.get("include_inactive", False))
        trigger_type = str(arguments.get("trigger_type") or "").strip() or None
        definitions = SchedulerService(self.session).list_definitions(
            active_only=not include_inactive
        )
        if trigger_type:
            definitions = [item for item in definitions if item.trigger_type == trigger_type]
        ranked = sorted(
            (
                (_workflow_search_score(item, query), item)
                for item in definitions
            ),
            key=lambda entry: (entry[0], entry[1].updated_at),
            reverse=True,
        )
        matches = [item for score, item in ranked if score > 0]
        if action_type == "workflow.get":
            exact = [
                item
                for item in matches
                if str(item.id) == query
                or item.key.lower() == query.lower()
                or item.name.lower() == query.lower()
            ]
            matches = exact or matches[:1]
        else:
            limit = _bounded_int(arguments.get("max_items"), default=6, minimum=1, maximum=10)
            matches = matches[:limit]
        service = SchedulerService(self.session)
        payloads = [service.workflow_definition_payload(item) for item in matches]
        return KnowledgeActionResult(
            action_type,
            "completed",
            f"Found {len(payloads)} matching workflow{'s' if len(payloads) != 1 else ''}.",
            "workflow",
            payloads[0]["id"] if len(payloads) == 1 else None,
            data={"query": query, "matches": payloads},
        )

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
        domain_keys = _string_list(arguments.get("domain_keys"))
        project_keys = _string_list(arguments.get("project_keys"))
        repository_keys = _string_list(arguments.get("repository_keys"))
        requested_limit = 1 if action_type == "issue.get" else _bounded_int(
            arguments.get("max_items"), default=8, minimum=1, maximum=10
        )
        issues = service.search(
            query=query,
            domain_key=str(arguments.get("domain_key") or "").strip() or None,
            domain_keys=domain_keys,
            project_key=str(arguments.get("project_key") or "").strip() or None,
            project_keys=project_keys,
            repository_key=str(arguments.get("repository_key") or "").strip() or None,
            repository_keys=repository_keys,
            status=str(arguments.get("status") or "").strip() or None,
            limit=250 if action_type == "issue.search" and len(project_keys) > 1 else requested_limit,
        )
        project_match_counts = self._project_match_counts(
            issues,
            project_keys=project_keys,
        )
        if action_type == "issue.search" and len(project_keys) > 1:
            issues = self._balanced_project_results(
                issues,
                project_keys=project_keys,
                limit=requested_limit,
            )
        if action_type == "issue.get":
            payloads = [
                {
                    **issue_payload(issue),
                    "event_links": EventWorkLinkService(self.session).for_issue(issue.id),
                }
                for issue in issues
            ]
        else:
            payloads = self._compact_issue_payloads(issues, query=query)
        return KnowledgeActionResult(
            action_type,
            "completed",
            f"Retrieved {len(issues)} product issue{'s' if len(issues) != 1 else ''}.",
            "product_issue",
            data={
                "query": query,
                "filters": {
                    "domain_keys": domain_keys,
                    "project_keys": project_keys,
                    "repository_keys": repository_keys,
                    "status": str(arguments.get("status") or "").strip() or None,
                },
                "match_count": len(issues),
                "project_match_counts": project_match_counts,
                "ordering": (
                    "project_balanced_then_relevance"
                    if len(project_keys) > 1
                    else "relevance"
                ),
                "issues": payloads,
            },
        )

    def _balanced_project_results(
        self,
        issues,
        *,
        project_keys: list[str],
        limit: int,
    ):
        if not issues or limit <= 0:
            return []
        projects = {
            item.id: item.key
            for item in self.session.scalars(
                select(ProductProject).where(ProductProject.key.in_(project_keys))
            ).all()
        }
        grouped: dict[str, list[Any]] = {key: [] for key in project_keys}
        for issue in issues:
            key = projects.get(issue.project_id)
            if key in grouped:
                grouped[key].append(issue)
        active_groups = [values for values in grouped.values() if values]
        if not active_groups:
            return issues[:limit]
        quota = max(1, limit // len(active_groups))
        selected = []
        for rank in range(quota):
            for values in active_groups:
                if rank < len(values) and len(selected) < limit:
                    selected.append(values[rank])
        selected_ids = {issue.id for issue in selected}
        for issue in issues:
            if len(selected) >= limit:
                break
            if issue.id not in selected_ids:
                selected.append(issue)
                selected_ids.add(issue.id)
        return selected

    def _compact_issue_payloads(self, issues, *, query: str) -> list[dict[str, Any]]:
        if not issues:
            return []
        domain_ids = {issue.domain_id for issue in issues}
        project_ids = {issue.project_id for issue in issues}
        repository_ids = {issue.repository_id for issue in issues if issue.repository_id}
        domains = {
            item.id: item
            for item in self.session.scalars(select(Domain).where(Domain.id.in_(domain_ids))).all()
        }
        projects = {
            item.id: item
            for item in self.session.scalars(
                select(ProductProject).where(ProductProject.id.in_(project_ids))
            ).all()
        }
        repositories = {
            item.id: item
            for item in self.session.scalars(
                select(RepositoryProfile).where(RepositoryProfile.id.in_(repository_ids))
            ).all()
        } if repository_ids else {}
        payloads: list[dict[str, Any]] = []
        for issue in issues:
            domain = domains.get(issue.domain_id)
            project = projects.get(issue.project_id)
            repository = repositories.get(issue.repository_id)
            payloads.append(
                {
                    "id": str(issue.id),
                    "title": issue.title,
                    "summary": _issue_summary(issue),
                    "status": issue.status,
                    "priority": issue.priority,
                    "issue_type": issue.issue_type,
                    "domain_key": domain.key if domain else None,
                    "domain_name": domain.name if domain else None,
                    "project_key": project.key if project else None,
                    "project_name": project.name if project else None,
                    "repository_key": repository.key if repository else None,
                    "external_number": issue.external_number,
                    "external_url": issue.external_url,
                    "relevance_score": issue_search_score(issue, query),
                    "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
                }
            )
        return payloads

    def _project_match_counts(self, issues, *, project_keys: list[str]) -> dict[str, int]:
        if not project_keys:
            return {}
        projects = {
            item.id: item.key
            for item in self.session.scalars(
                select(ProductProject).where(ProductProject.key.in_(project_keys))
            ).all()
        }
        counts = {key: 0 for key in project_keys}
        for issue in issues:
            key = projects.get(issue.project_id)
            if key in counts:
                counts[key] += 1
        return counts


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


def _string_list(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    values = value if isinstance(value, list) else str(value).split(",")
    return list(
        dict.fromkeys(str(item).strip().lower() for item in values if str(item).strip())
    )


def _issue_summary(issue) -> str:
    text = str(issue.problem or issue.desired_outcome or issue.notes or "").strip()
    if not text:
        criteria = [str(item).strip() for item in (issue.acceptance_criteria or []) if str(item).strip()]
        text = "; ".join(criteria[:3])
    compact = " ".join(text.split())
    return compact if len(compact) <= 280 else compact[:277].rstrip() + "..."


def _workflow_search_score(definition: WorkflowDefinition, query: str) -> float:
    needle = " ".join(query.lower().replace("_", " ").replace("-", " ").split())
    if not needle:
        return 0.0
    aliases = [
        str(value)
        for value in (definition.trigger_config or {}).get("invocation_aliases", [])
        if str(value).strip()
    ]
    fields = [str(definition.id), definition.key, definition.name, definition.description or "", *aliases]
    normalized = [" ".join(value.lower().replace("_", " ").replace("-", " ").split()) for value in fields]
    if needle in normalized:
        return 10.0
    if any(needle in value for value in normalized):
        return 6.0
    query_terms = set(needle.split())
    best_overlap = max(
        (len(query_terms & set(value.split())) / max(1, len(query_terms)) for value in normalized),
        default=0.0,
    )
    return best_overlap * 4.0


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
