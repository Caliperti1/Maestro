from __future__ import annotations

from copy import deepcopy
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runtime import AgentRegistryService
from app.db.models import Agent, Domain, ToolConnection, WorkflowDefinition
from app.maestro.scheduler import SchedulerService
from app.tools.runtime import _dotenv_value


PRAXIS_EMAIL_TRIAGE_KEY = "praxis-email-triage"
PRAXIS_EMAIL_AGENT_KEY = "praxis-email-agent"
PERTI_EMAIL_TRIAGE_KEY = "perti-email-triage"
PERTI_EMAIL_AGENT_KEY = "perti-email-agent"
PRAXIS_CALENDAR_MONITOR_KEY = "praxis-calendar-monitor"
PRAXIS_CALENDAR_AGENT_KEY = "praxis-calendar-agent"
PERTI_CALENDAR_MONITOR_KEY = "perti-calendar-monitor"
PERTI_CALENDAR_AGENT_KEY = "perti-calendar-agent"

EMAIL_TRIAGE_SKILLS = [
    "email_triage",
    "contact_manager",
    "to_do_manager",
    "calendar_manager",
    "organization_manager",
]
EMAIL_TRIAGE_TOOLS = [
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
CALENDAR_MONITOR_SKILLS = ["calendar_manager", "contact_manager", "organization_manager"]
CALENDAR_MONITOR_TOOLS = [
    "memory.context_bundle",
    "google.calendar.event.get",
    "routed.item.create",
]

# Backwards-compatible exports used by existing tests and integrations.
PRAXIS_EMAIL_SKILLS = EMAIL_TRIAGE_SKILLS
PRAXIS_EMAIL_TOOLS = EMAIL_TRIAGE_TOOLS


def _email_template(
    *, key: str, name: str, domain_key: str, domain_name: str, agent_key: str
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "domain_key": domain_key,
        "description": (
            f"Triage each newly received {domain_name} inbox message exactly once, inspect "
            "relevant linked Google Workspace content, route operational objects, and notify "
            "Chris only when action or a decision is required."
        ),
        "trigger_type": "event",
        "trigger_config": {
            "event_type": "gmail.message.received",
            "filters": {"domain_key": domain_key},
            "gmail_watch_enabled": False,
        },
        "workflow_spec": {
            "shadow_mode": True,
            "model_profile": "openrouter:openai/gpt-5.6-luna",
            "queue_items": [
                {
                    "id": "email-triage",
                    "objective": (
                        f"Triage the exact {domain_name} Gmail message identified by "
                        "payload.message_id in the immutable scheduler trigger event. Do not "
                        "list or select the latest email. Read that message and relevant thread "
                        "context; inspect relevant linked Google Docs, Drive folders, Slides, or "
                        "Sheets when accessible; classify it; create or update supported "
                        "contacts, organizations, events, and Chris-owned todos through the "
                        "routed-item service; notify Chris only when he must respond, decide, "
                        "meet a material deadline, or address a meaningful risk; then produce a "
                        "concise report with source provenance."
                    ),
                    "domain_key": domain_key,
                    "agent_key": agent_key,
                    "stage_index": 1,
                    "position": 1,
                    "priority": "normal",
                    "required_skills": EMAIL_TRIAGE_SKILLS,
                    "required_tools": EMAIL_TRIAGE_TOOLS,
                    "model_tier": "luna",
                    "model_profile": "openrouter:openai/gpt-5.6-luna",
                    "model_rationale": (
                        "Routine email triage needs reliable multi-step tool use and extraction "
                        "at the lowest validated cloud tier."
                    ),
                    "max_attempts": 3,
                }
            ],
        },
        "priority": "normal",
        "fairness_group": domain_key,
    }


def _calendar_template(
    *, key: str, name: str, domain_key: str, domain_name: str, agent_key: str
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "domain_key": domain_key,
        "description": (
            f"Monitor {domain_name} Google Calendar changes independently from email and keep "
            "Maestro's canonical calendar, attendee links, and provenance current."
        ),
        "trigger_type": "event",
        "trigger_config": {
            "event_type": "google.calendar.event.changed",
            "filters": {"domain_key": domain_key},
            "calendar_watch_enabled": False,
        },
        "workflow_spec": {
            "shadow_mode": True,
            "model_profile": "openrouter:openai/gpt-5.6-luna",
            "queue_items": [
                {
                    "id": "calendar-sync",
                    "objective": (
                        f"Process the exact {domain_name} Google Calendar event identified by "
                        "payload.event_id in the immutable scheduler trigger. The deterministic "
                        "bootstrap has already synchronized the provider fields into Maestro. "
                        "Review the event from Chris Aliperti's perspective, verify attendee and "
                        "organization context, and produce a concise report. Do not create a "
                        "second event or notify Chris merely because a calendar item changed."
                    ),
                    "domain_key": domain_key,
                    "agent_key": agent_key,
                    "stage_index": 1,
                    "position": 1,
                    "priority": "normal",
                    "required_skills": CALENDAR_MONITOR_SKILLS,
                    "required_tools": CALENDAR_MONITOR_TOOLS,
                    "model_tier": "luna",
                    "model_profile": "openrouter:openai/gpt-5.6-luna",
                    "model_rationale": (
                        "Calendar synchronization is structured but still benefits from reliable "
                        "identity and relationship interpretation."
                    ),
                    "max_attempts": 3,
                }
            ],
        },
        "priority": "normal",
        "fairness_group": domain_key,
    }


_TEMPLATES: dict[str, dict[str, Any]] = {
    PRAXIS_EMAIL_TRIAGE_KEY: _email_template(
        key=PRAXIS_EMAIL_TRIAGE_KEY,
        name="Praxis Email Triage",
        domain_key="praxis",
        domain_name="Praxis",
        agent_key=PRAXIS_EMAIL_AGENT_KEY,
    ),
    PERTI_EMAIL_TRIAGE_KEY: _email_template(
        key=PERTI_EMAIL_TRIAGE_KEY,
        name="Perti Email Triage",
        domain_key="perti-laboratories",
        domain_name="Perti Laboratories",
        agent_key=PERTI_EMAIL_AGENT_KEY,
    ),
    PRAXIS_CALENDAR_MONITOR_KEY: _calendar_template(
        key=PRAXIS_CALENDAR_MONITOR_KEY,
        name="Praxis Calendar Monitor",
        domain_key="praxis",
        domain_name="Praxis",
        agent_key=PRAXIS_CALENDAR_AGENT_KEY,
    ),
    PERTI_CALENDAR_MONITOR_KEY: _calendar_template(
        key=PERTI_CALENDAR_MONITOR_KEY,
        name="Perti Calendar Monitor",
        domain_key="perti-laboratories",
        domain_name="Perti Laboratories",
        agent_key=PERTI_CALENDAR_AGENT_KEY,
    ),
}


class WorkflowTemplateService:
    def __init__(self, session: Session):
        self.session = session

    def list_templates(self) -> list[dict[str, Any]]:
        return [self.template_payload(key) for key in _TEMPLATES]

    def template_payload(self, key: str) -> dict[str, Any]:
        template = self._template(key)
        definition = self.session.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.key == key)
        )
        return {
            **deepcopy(template),
            "installed": definition is not None,
            "definition_id": str(definition.id) if definition else None,
            "is_active": bool(definition and definition.is_active),
            "readiness": self.readiness(key),
        }

    def install(self, key: str, *, is_active: bool = False) -> WorkflowDefinition:
        template = self._template(key)
        registry = AgentRegistryService(self.session)
        registry.ensure_seed_agents()
        registry.ensure_domain_provider_connections()
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
        if definition.key in _TEMPLATES and is_active:
            readiness = self.readiness(definition.key)
            if not readiness["ready"]:
                raise ValueError(self._readiness_error(readiness))
        definition.is_active = is_active
        template = _TEMPLATES.get(definition.key)
        if template:
            watch_key = self.watch_key(definition)
            if watch_key and watch_key not in (definition.trigger_config or {}):
                definition.trigger_config = {
                    **(definition.trigger_config or {}),
                    watch_key: False,
                }
        self.session.commit()
        self.session.refresh(definition)
        from app.maestro.calendar_trigger import sync_calendar_trigger_worker_settings
        from app.maestro.gmail_trigger import sync_gmail_trigger_worker_settings

        sync_gmail_trigger_worker_settings(self.session)
        sync_calendar_trigger_worker_settings(self.session)
        return definition

    def readiness(self, key: str) -> dict[str, Any]:
        template = self._template(key)
        domain = self.session.scalar(select(Domain).where(Domain.key == template["domain_key"]))
        item = template["workflow_spec"]["queue_items"][0]
        agent = self.session.scalar(select(Agent).where(Agent.key == item["agent_key"]))
        missing: list[str] = []
        if domain is None or not domain.is_active:
            missing.append(f"active {template['domain_key']} domain")
        if agent is None or not agent.is_active:
            missing.append(f"active {item['agent_key']} agent")

        tool_permissions = set((agent.tool_permissions or {}).keys()) if agent else set()
        skill_permissions = set((agent.skill_permissions or {}).keys()) if agent else set()
        missing_tools = sorted(set(item["required_tools"]) - tool_permissions)
        missing_skills = sorted(set(item["required_skills"]) - skill_permissions)
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
        credentials_ready = bool(connection and _google_credentials_ready(connection))
        if connection is None:
            missing.append(f"active {template['domain_key']} Google connection")
        elif not credentials_ready:
            missing.append(f"configured {template['domain_key']} Google OAuth credentials")
        return {
            "ready": not missing,
            "missing": missing,
            "domain_ready": bool(domain and domain.is_active),
            "agent_ready": bool(agent and agent.is_active),
            "connection_ready": credentials_ready,
            "missing_tools": missing_tools,
            "missing_skills": missing_skills,
        }

    @staticmethod
    def watch_key(definition: WorkflowDefinition) -> str | None:
        event_type = (definition.trigger_config or {}).get("event_type")
        if event_type == "gmail.message.received":
            return "gmail_watch_enabled"
        if event_type == "google.calendar.event.changed":
            return "calendar_watch_enabled"
        return None

    @staticmethod
    def is_template(key: str) -> bool:
        return key in _TEMPLATES

    @staticmethod
    def _readiness_error(readiness: dict[str, Any]) -> str:
        return "Workflow prerequisites are incomplete: " + "; ".join(readiness["missing"])

    @staticmethod
    def _template(key: str) -> dict[str, Any]:
        try:
            return _TEMPLATES[key]
        except KeyError as exc:
            raise ValueError(f"Unknown workflow template: {key}") from exc


def _google_credentials_ready(connection: ToolConnection) -> bool:
    config = connection.config or {}
    for value_key, env_key in (
        ("client_id", "client_id_env"),
        ("client_secret", "client_secret_env"),
        ("refresh_token", "refresh_token_env"),
    ):
        if str(config.get(value_key) or "").strip():
            continue
        env_name = str(config.get(env_key) or "").strip()
        if not env_name or not (os.environ.get(env_name) or _dotenv_value(env_name)):
            return False
    return True
