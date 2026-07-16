"""Bounded context assembly for Maestro-level chat and planning.

Maestro needs a broader view than any one agent: durable memory, routed objects, recent workflow
reports, run history, and artifact metadata. This service builds one compact bundle that can be
passed to direct chat or the planner without forcing those callers to know each retrieval store.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Artifact, Domain, Report, WorkflowRunLogEntry
from app.memory.retrieval import MemoryContextBundleRequest, MemoryRetrievalService
from app.memory.routed_retrieval import RoutedRetrievalService


@dataclass(frozen=True)
class MaestroContextBundle:
    query_text: str | None
    domain_key: str | None
    sections: dict[str, Any]
    rendered_text: str
    used_chars: int
    max_chars: int


class MaestroContextAssembler:
    def __init__(self, session: Session):
        self.session = session

    def build_bundle(
        self,
        *,
        query_text: str | None = None,
        domain_key: str | None = None,
        max_chars: int = 6500,
        memory_chars: int = 2200,
        routed_chars: int = 1800,
        report_limit: int = 6,
        run_log_limit: int = 6,
        artifact_limit: int = 6,
    ) -> MaestroContextBundle:
        domain = self._domain(domain_key)
        sections = {
            "memory": self._memory_section(
                query_text=query_text,
                domain=domain,
                max_chars=memory_chars,
            ),
            "routed_objects": self._routed_section(
                query_text=query_text,
                domain=domain,
                max_chars=routed_chars,
            ),
            "reports": self._reports_section(
                query_text=query_text,
                domain=domain,
                limit=report_limit,
            ),
            "run_log": self._run_log_section(
                query_text=query_text,
                domain=domain,
                limit=run_log_limit,
            ),
            "artifacts": self._artifacts_section(
                query_text=query_text,
                limit=artifact_limit,
            ),
            "web_search": {
                "status": "available_as_tool",
                "tool_key": "web.search",
                "note": "Use web.search when current external information is required.",
            },
        }
        rendered = self._render(sections, max_chars=max_chars)
        return MaestroContextBundle(
            query_text=query_text,
            domain_key=domain.key if domain else domain_key,
            sections=sections,
            rendered_text=rendered,
            used_chars=len(rendered),
            max_chars=max_chars,
        )

    def _domain(self, domain_key: str | None) -> Domain | None:
        if not domain_key:
            return None
        return self.session.scalar(select(Domain).where(Domain.key == domain_key))

    def _memory_section(
        self,
        *,
        query_text: str | None,
        domain: Domain | None,
        max_chars: int,
    ) -> dict[str, Any]:
        try:
            bundle = MemoryRetrievalService(self.session).build_context_bundle(
                MemoryContextBundleRequest(
                    profile="agent_prompt",
                    audience="maestro",
                    domain_id=domain.id if domain else None,
                    query_text=query_text,
                    use_semantic=True,
                    max_items=8,
                    max_chars=max_chars,
                )
            )
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc), "rendered_text": ""}
        return {
            "status": bundle.semantic_status,
            "included_count": bundle.included_count,
            "dropped_count": bundle.dropped_count,
            "used_chars": bundle.used_chars,
            "rendered_text": bundle.rendered_text,
        }

    def _routed_section(
        self,
        *,
        query_text: str | None,
        domain: Domain | None,
        max_chars: int,
    ) -> dict[str, Any]:
        try:
            service = RoutedRetrievalService(self.session)
            bundle = service.build_context_bundle(
                domain_id=domain.id if domain else None,
                query_text=query_text,
                limit=10,
                max_chars=max_chars,
            )
            fallback_used = False
            if query_text and not bundle.rendered_text.strip():
                bundle = service.build_context_bundle(
                    domain_id=domain.id if domain else None,
                    query_text=None,
                    limit=10,
                    max_chars=max_chars,
                )
                fallback_used = True
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc), "rendered_text": "", "stores": {}}
        return {
            "status": "ok",
            "fallback_used": fallback_used,
            "stores": bundle.stores,
            "rendered_text": bundle.rendered_text,
        }

    def _reports_section(
        self,
        *,
        query_text: str | None,
        domain: Domain | None,
        limit: int,
    ) -> dict[str, Any]:
        reports = self.session.scalars(
            self._text_filtered(
                select(Report).order_by(Report.created_at.desc()).limit(limit * 3),
                query_text=query_text,
                columns=(Report.title, Report.summary, Report.body_markdown),
            ).where(or_(Report.domain_id == domain.id, Report.domain_id.is_(None)) if domain else True)
        ).all()
        reports = [report for report in reports if not _report_is_archived(report)][:limit]
        return {
            "status": "ok",
            "items": [
                {
                    "id": str(report.id),
                    "title": report.title,
                    "summary": report.summary,
                    "report_type": report.report_type,
                    "domain_id": str(report.domain_id) if report.domain_id else None,
                    "created_at": report.created_at.isoformat(),
                }
                for report in reports
            ],
        }

    def _run_log_section(
        self,
        *,
        query_text: str | None,
        domain: Domain | None,
        limit: int,
    ) -> dict[str, Any]:
        entries = self.session.scalars(
            self._text_filtered(
                select(WorkflowRunLogEntry)
                .where(WorkflowRunLogEntry.status != "archived")
                .order_by(WorkflowRunLogEntry.run_completed_at.desc(), WorkflowRunLogEntry.created_at.desc())
                .limit(limit),
                query_text=query_text,
                columns=(WorkflowRunLogEntry.title, WorkflowRunLogEntry.summary),
            ).where(
                or_(WorkflowRunLogEntry.domain_id == domain.id, WorkflowRunLogEntry.domain_id.is_(None))
                if domain
                else True
            )
        ).all()
        return {
            "status": "ok",
            "items": [
                {
                    "id": str(entry.id),
                    "workflow_run_id": str(entry.workflow_run_id),
                    "title": entry.title,
                    "status": entry.status,
                    "summary": entry.summary,
                    "report_ids": entry.report_ids,
                    "artifact_ids": entry.artifact_ids,
                    "run_completed_at": entry.run_completed_at.isoformat()
                    if entry.run_completed_at
                    else None,
                }
                for entry in entries
            ],
        }

    def _artifacts_section(
        self,
        *,
        query_text: str | None,
        limit: int,
    ) -> dict[str, Any]:
        artifacts = self.session.scalars(
            self._text_filtered(
                select(Artifact).order_by(Artifact.created_at.desc()).limit(limit),
                query_text=query_text,
                columns=(Artifact.name, Artifact.uri, Artifact.artifact_type),
            )
        ).all()
        return {
            "status": "ok",
            "items": [
                {
                    "id": str(artifact.id),
                    "task_id": str(artifact.task_id) if artifact.task_id else None,
                    "report_id": str(artifact.report_id) if artifact.report_id else None,
                    "name": artifact.name,
                    "artifact_type": artifact.artifact_type,
                    "uri": artifact.uri,
                    "mime_type": artifact.mime_type,
                    "created_at": artifact.created_at.isoformat(),
                }
                for artifact in artifacts
            ],
        }

    def _text_filtered(self, statement, *, query_text: str | None, columns: tuple[Any, ...]):
        query = " ".join(str(query_text or "").split())
        if not query:
            return statement
        like = f"%{query}%"
        return statement.where(or_(*[column.ilike(like) for column in columns]))

    def _render(self, sections: dict[str, Any], *, max_chars: int) -> str:
        blocks: list[str] = []
        memory_text = str(sections.get("memory", {}).get("rendered_text") or "").strip()
        if memory_text:
            blocks.append(f"## Durable Memory\n{memory_text}")
        routed_text = str(sections.get("routed_objects", {}).get("rendered_text") or "").strip()
        if routed_text:
            blocks.append(f"## Routed Objects\n{routed_text}")
        report_lines = [
            f"- {item['title']}: {item.get('summary') or item.get('report_type')}"
            for item in sections.get("reports", {}).get("items", [])
        ]
        if report_lines:
            blocks.append("## Recent Reports\n" + "\n".join(report_lines))
        run_lines = [
            f"- {item['title']} [{item['status']}]: {item.get('summary') or ''}"
            for item in sections.get("run_log", {}).get("items", [])
        ]
        if run_lines:
            blocks.append("## Recent Run Log\n" + "\n".join(run_lines))
        artifact_lines = [
            f"- {item['name']} ({item['artifact_type']}): {item['uri']}"
            for item in sections.get("artifacts", {}).get("items", [])
        ]
        if artifact_lines:
            blocks.append("## Artifacts\n" + "\n".join(artifact_lines))
        blocks.append("## Web Search\nUse `web.search` if the answer needs current external information.")
        rendered = "\n\n".join(blocks).strip()
        return rendered[:max_chars]


def maestro_context_payload(bundle: MaestroContextBundle) -> dict[str, Any]:
    return {
        "query_text": bundle.query_text,
        "domain_key": bundle.domain_key,
        "sections": bundle.sections,
        "rendered_text": bundle.rendered_text,
        "used_chars": bundle.used_chars,
        "max_chars": bundle.max_chars,
    }


def _report_is_archived(report: Report) -> bool:
    return bool((report.structured_data or {}).get("archived"))
