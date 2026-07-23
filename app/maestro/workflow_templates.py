from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runtime import AgentRegistryService
from app.db.models import Agent, Domain, ToolConnection, WorkflowDefinition
from app.maestro.scheduler import SchedulerService


PRAXIS_EMAIL_TRIAGE_KEY = "praxis-email-triage"
PRAXIS_EMAIL_AGENT_KEY = "praxis-email-agent"
PRAXIS_EMAIL_SKILLS = [
    "email_triage",
    "contact_manager",
    "to_do_manager",
    "calendar_manager",
    "organization_manager",
]
PRAXIS_EMAIL_TOOLS = [
    "memory.context_bundle",
    "reports.search",
    "reports.get",
    "gmail.message.get",
    "gmail.thread.get",
    "gmail.message.modify",
    "google.drive.file.get",
    "google.drive.folder.list",
    "google.drive.file.export",
    "google.docs.get",
    "google.slides.get",
    "google.sheets.get",
    "google.sheets.values.get",
    "routed.item.create",
    "workflow.notification.create",
]

_PRAXIS_EMAIL_TRIAGE_TEMPLATE: dict[str, Any] = {
    "key": PRAXIS_EMAIL_TRIAGE_KEY,
    "name": "Praxis Email Triage",
    "domain_key": "praxis",
    "description": (
        "Triage each newly received Praxis inbox message exactly once, inspect relevant linked "
        "Google Workspace content, route operational objects, and notify Chris only when action "
        "or a decision is required."
    ),
    "trigger_type": "event",
    "trigger_config": {
        "event_type": "gmail.message.received",
        "filters": {"domain_key": "praxis"},
    },
    "workflow_spec": {
        "model_profile": "openrouter:openai/gpt-5.6-luna",
        "queue_items": [
            {
                "id": "email-triage",
                "objective": (
                    "Triage the exact Praxis Gmail message identified by payload.message_id in "
                    "the immutable scheduler trigger event. Do not list or select the latest "
                    "email. Read that message and relevant thread context; inspect relevant "
                    "linked Google Docs, Drive folders, Slides, or Sheets when accessible; "
                    "classify it; create or update supported contacts, organizations, events, "
                    "and Chris-owned todos through the routed-item service; notify Chris only "
                    "when he must respond, decide, meet a material deadline, or address a "
                    "meaningful risk; then produce a concise report with source provenance."
                ),
                "domain_key": "praxis",
                "agent_key": PRAXIS_EMAIL_AGENT_KEY,
                "stage_index": 1,
                "position": 1,
                "priority": "normal",
                "required_skills": PRAXIS_EMAIL_SKILLS,
                "required_tools": PRAXIS_EMAIL_TOOLS,
                "model_tier": "luna",
                "model_profile": "openrouter:openai/gpt-5.6-luna",
                "model_rationale": (
                    "Routine email triage needs reliable multi-step tool use and extraction at "
                    "the lowest validated cloud tier."
                ),
                "max_attempts": 3,
            }
        ],
    },
    "priority": "normal",
    "fairness_group": "praxis",
}


class WorkflowTemplateService:
    def __init__(self, session: Session):
        self.session = session

    def list_templates(self) -> list[dict[str, Any]]:
        return [self.template_payload(PRAXIS_EMAIL_TRIAGE_KEY)]

    def template_payload(self, key: str) -> dict[str, Any]:
        template = self._template(key)
        definition = self.session.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.key == key)
        )
        readiness = self.readiness(key)
        return {
            **deepcopy(template),
            "installed": definition is not None,
            "definition_id": str(definition.id) if definition else None,
            "is_active": bool(definition and definition.is_active),
            "readiness": readiness,
        }

    def install(self, key: str, *, is_active: bool = False) -> WorkflowDefinition:
        template = self._template(key)
        AgentRegistryService(self.session).ensure_seed_agents()
        readiness = self.readiness(key)
        if is_active and not readiness["ready"]:
            raise ValueError(self._readiness_error(readiness))
        domain = self.session.scalar(select(Domain).where(Domain.key == template["domain_key"]))
        if domain is None:
            raise ValueError(f"Unknown domain: {template['domain_key']}")
        return SchedulerService(self.session).upsert_definition(
            key=template["key"],
            name=template["name"],
            domain_id=domain.id,
            description=template["description"],
            trigger_type=template["trigger_type"],
            trigger_config=deepcopy(template["trigger_config"]),
            workflow_spec=deepcopy(template["workflow_spec"]),
            priority=template["priority"],
            fairness_group=template["fairness_group"],
            is_active=is_active,
        )

    def set_active(self, definition: WorkflowDefinition, *, is_active: bool) -> WorkflowDefinition:
        if is_active and definition.key == PRAXIS_EMAIL_TRIAGE_KEY:
            readiness = self.readiness(definition.key)
            if not readiness["ready"]:
                raise ValueError(self._readiness_error(readiness))
        definition.is_active = is_active
        self.session.commit()
        self.session.refresh(definition)
        return definition

    def readiness(self, key: str) -> dict[str, Any]:
        template = self._template(key)
        domain = self.session.scalar(select(Domain).where(Domain.key == template["domain_key"]))
        agent = self.session.scalar(select(Agent).where(Agent.key == PRAXIS_EMAIL_AGENT_KEY))
        missing: list[str] = []
        if domain is None or not domain.is_active:
            missing.append("active Praxis domain")
        if agent is None or not agent.is_active:
            missing.append("active Praxis Email Agent")

        tool_permissions = set((agent.tool_permissions or {}).keys()) if agent else set()
        skill_permissions = set((agent.skill_permissions or {}).keys()) if agent else set()
        missing_tools = sorted(set(PRAXIS_EMAIL_TOOLS) - tool_permissions)
        missing_skills = sorted(set(PRAXIS_EMAIL_SKILLS) - skill_permissions)
        if missing_tools:
            missing.append("agent tools: " + ", ".join(missing_tools))
        if missing_skills:
            missing.append("agent skills: " + ", ".join(missing_skills))

        connection = None
        if domain is not None:
            connection = self.session.scalar(
                select(ToolConnection).where(
                    ToolConnection.domain_id == domain.id,
                    ToolConnection.tool_key.in_(["google", "gmail"]),
                    ToolConnection.is_active.is_(True),
                )
            )
        if connection is None:
            missing.append("active Praxis Google connection")
        return {
            "ready": not missing,
            "missing": missing,
            "domain_ready": bool(domain and domain.is_active),
            "agent_ready": bool(agent and agent.is_active),
            "connection_ready": connection is not None,
            "missing_tools": missing_tools,
            "missing_skills": missing_skills,
        }

    @staticmethod
    def _readiness_error(readiness: dict[str, Any]) -> str:
        return "Workflow prerequisites are incomplete: " + "; ".join(readiness["missing"])

    @staticmethod
    def _template(key: str) -> dict[str, Any]:
        if key != PRAXIS_EMAIL_TRIAGE_KEY:
            raise ValueError(f"Unknown workflow template: {key}")
        return _PRAXIS_EMAIL_TRIAGE_TEMPLATE
