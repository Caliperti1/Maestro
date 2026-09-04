from __future__ import annotations

import os
from copy import deepcopy
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
PERSONAL_EMAIL_TRIAGE_KEY = "personal-email-triage"
PERSONAL_OPERATIONS_AGENT_KEY = "personal-operations-agent"
PRAXIS_CALENDAR_MONITOR_KEY = "praxis-calendar-monitor"
PRAXIS_CALENDAR_AGENT_KEY = "praxis-calendar-agent"
PERTI_CALENDAR_MONITOR_KEY = "perti-calendar-monitor"
PERTI_CALENDAR_AGENT_KEY = "perti-calendar-agent"
PERSONAL_CALENDAR_MONITOR_KEY = "personal-calendar-monitor"
DAILY_STANDUP_KEY = "daily-standup"
MAESTRO_BRIEFING_AGENT_KEY = "maestro-briefing-agent"
MAESTRO_OPERATIONS_AGENT_KEY = "maestro-operations-agent"
USMA_OPERATIONS_AGENT_KEY = "usma-operations-agent"
L3_OPERATIONS_AGENT_KEY = "l3-operations-agent"

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
    *,
    key: str,
    name: str,
    domain_key: str,
    domain_name: str,
    agent_key: str,
    gmail_scope: str = "all_inbox",
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
            "filters": {
                "domain_key": domain_key,
                "gmail_scope": gmail_scope,
            },
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
        "credential_family": "google",
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
        "credential_family": "google",
    }


def _daily_standup_template() -> dict[str, Any]:
    domain_items = [
        ("personal-input", "personal", "personal-operations-agent", "Personal"),
        (
            "maestro-development-input",
            "maestro-development",
            MAESTRO_OPERATIONS_AGENT_KEY,
            "Maestro Development",
        ),
        ("usma-input", "usma", USMA_OPERATIONS_AGENT_KEY, "USMA"),
        ("l3-input", "l3", L3_OPERATIONS_AGENT_KEY, "L3"),
        ("perti-input", "perti-laboratories", "perti-operations-agent", "Perti Laboratories"),
        ("praxis-input", "praxis", "praxis-planning-agent", "Praxis"),
    ]
    queue_items = [
        {
            "id": item_id,
            "objective": (
                f"Prepare the {label} input for Chris's daily standup. Use the Daily Standup "
                "skill and current canonical domain context. Inspect the domain calendar, open "
                "todos, active Product Issues, recent reports, unresolved decisions, and blocked "
                "work. Report what is already scheduled, recommend what should be added to today's "
                "schedule, identify work suited for delegation to an available domain agent, and "
                "ask only the specific questions Chris must answer to keep this domain current. "
                "Produce a concise evidence-grounded report; do not create or edit canonical items "
                "during this review. Apply any invocation focus or date supplied in scheduler context."
            ),
            "domain_key": domain_key,
            "agent_key": agent_key,
            "stage_index": 1,
            "position": position,
            "priority": "normal",
            "required_skills": ["daily_standup"],
            "required_tools": ["memory.context_bundle"],
            "model_tier": "luna",
            "model_profile": "openrouter:openai/gpt-5.6-luna",
            "model_rationale": "Focused domain retrieval and concise status synthesis.",
            "max_attempts": 2,
        }
        for position, (item_id, domain_key, agent_key, label) in enumerate(domain_items, start=1)
    ]
    queue_items.append(
        {
            "id": "standup-synthesis",
            "objective": (
                "Synthesize the completed Personal, Maestro Development, USMA, L3, Perti "
                "Laboratories, and Praxis reports into Chris's daily standup. Use the Daily Standup "
                "skill. Walk Chris through each domain's commitments, recommendations, and requested "
                "input, then reconcile cross-domain timing conflicts and dependencies into one "
                "feasible plan for the day. Distinguish existing commitments from proposed calendar "
                "blocks and proposed agent handoffs. Produce one polished report that Maestro can "
                "continue discussing and revise through Knowledge-mode actions after Chris responds."
            ),
            "domain_key": "maestro-development",
            "agent_key": MAESTRO_BRIEFING_AGENT_KEY,
            "stage_index": 2,
            "position": 1,
            "depends_on": [item_id for item_id, *_ in domain_items],
            "priority": "normal",
            "required_skills": ["daily_standup"],
            "required_tools": [],
            "model_tier": "terra",
            "model_profile": "openrouter:openai/gpt-5.6-terra",
            "model_rationale": "Cross-domain reconciliation and decision-oriented synthesis.",
            "max_attempts": 2,
        }
    )
    return {
        "key": DAILY_STANDUP_KEY,
        "name": "Daily Standup",
        "domain_key": "maestro-development",
        "description": (
            "Collect parallel operational inputs from active domains and synthesize one "
            "cross-domain daily briefing for Chris."
        ),
        "trigger_type": "manual",
        "trigger_config": {
            "invocation_aliases": [
                "prepare my daily standup",
                "run my daily standup",
                "daily briefing",
                "prepare my morning briefing",
            ],
            "parameter_schema": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Optional ISO date to brief; defaults to today.",
                    },
                    "focus": {
                        "type": "string",
                        "description": "Optional emphasis for this run.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            "approval_policy": "definition_approved",
            "workflow_version": "2",
        },
        "workflow_spec": {
            "model_profile": "openrouter:openai/gpt-5.6-luna",
            "queue_items": queue_items,
        },
        "priority": "normal",
        "fairness_group": "maestro-development",
        "credential_family": None,
    }


_TEMPLATES: dict[str, dict[str, Any]] = {
    DAILY_STANDUP_KEY: _daily_standup_template(),
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
    PERSONAL_EMAIL_TRIAGE_KEY: _email_template(
        key=PERSONAL_EMAIL_TRIAGE_KEY,
        name="Personal Email Triage",
        domain_key="personal",
        domain_name="Personal",
        agent_key=PERSONAL_OPERATIONS_AGENT_KEY,
        gmail_scope="focused",
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
    PERSONAL_CALENDAR_MONITOR_KEY: _calendar_template(
        key=PERSONAL_CALENDAR_MONITOR_KEY,
        name="Personal Calendar Monitor",
        domain_key="personal",
        domain_name="Personal",
        agent_key=PERSONAL_OPERATIONS_AGENT_KEY,
    ),
}


class WorkflowTemplateService:
    def __init__(self, session: Session):
        self.session = session

    def list_templates(self) -> list[dict[str, Any]]:
        AgentRegistryService(self.session).ensure_seed_agents()
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
        missing: list[str] = []
        if domain is None or not domain.is_active:
            missing.append(f"active {template['domain_key']} domain")
        agents_ready = True
        missing_tools_set: set[str] = set()
        missing_skills_set: set[str] = set()
        for item in template["workflow_spec"]["queue_items"]:
            agent = self.session.scalar(select(Agent).where(Agent.key == item["agent_key"]))
            if agent is None or not agent.is_active:
                agents_ready = False
                missing.append(f"active {item['agent_key']} agent")
                continue
            tool_permissions = set((agent.tool_permissions or {}).keys())
            skill_permissions = set((agent.skill_permissions or {}).keys())
            missing_tools_set.update(set(item.get("required_tools") or []) - tool_permissions)
            missing_skills_set.update(set(item.get("required_skills") or []) - skill_permissions)
        missing_tools = sorted(missing_tools_set)
        missing_skills = sorted(missing_skills_set)
        if missing_tools:
            missing.append("agent tools: " + ", ".join(missing_tools))
        if missing_skills:
            missing.append("agent skills: " + ", ".join(missing_skills))

        connection = None
        credential_family = template.get("credential_family")
        if credential_family == "google" and domain is not None:
            connection = self.session.scalar(
                select(ToolConnection).where(
                    ToolConnection.domain_id == domain.id,
                    ToolConnection.tool_key.in_(["google", "gmail"]),
                    ToolConnection.is_active.is_(True),
                )
            )
        credentials_ready = credential_family is None or bool(
            connection and _google_credentials_ready(connection)
        )
        if credential_family == "google":
            if connection is None:
                missing.append(f"active {template['domain_key']} Google connection")
            elif not credentials_ready:
                missing.append(f"configured {template['domain_key']} Google OAuth credentials")
        return {
            "ready": not missing,
            "missing": missing,
            "domain_ready": bool(domain and domain.is_active),
            "agent_ready": agents_ready,
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
