import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runtime import (
    AgentRegistryService,
    AgentRunResult,
    InteractionArtifactPackager,
    PromptAggregationService,
    PromptPackageRequest,
)
from app.db.models import Artifact, Report, Task

IntentType = Literal[
    "direct_chat",
    "workflow",
    "task",
    "contact",
    "event",
    "decision",
    "rfi",
    "memory_route",
    "schedule",
]


@dataclass(frozen=True)
class MaestroIntent:
    type: IntentType
    summary: str
    target: str
    domain_key: str | None = None
    priority: str = "normal"
    action: str | None = None


@dataclass(frozen=True)
class MaestroSubtask:
    agent_key: str
    agent_name: str
    domain_key: str
    objective: str
    expected_output: str
    priority: str = "normal"
    rationale: str | None = None


@dataclass(frozen=True)
class MaestroPlan:
    plan_id: str
    status: str
    user_input: str
    summary: str
    execution_mode: str
    intents: list[MaestroIntent]
    subtasks: list[MaestroSubtask]
    selected_agents: list[dict[str, Any]]
    registry_snapshot: dict[str, Any]
    approval_required: bool
    scheduler: dict[str, Any]
    created_at: str
    parent_task_id: str


@dataclass(frozen=True)
class MaestroRun:
    plan: MaestroPlan
    status: str
    parent_task_id: str
    child_runs: list[AgentRunResult]
    synthesis_report_id: str | None
    synthesis: str
    staged_artifact_path: str | None
    artifact_id: str | None
    scheduler: dict[str, Any]
    error_message: str | None = None


class MaestroOrchestratorError(ValueError):
    pass


class MaestroOrchestratorService:
    def __init__(self, session: Session, *, runtime: PromptAggregationService | None = None):
        self.session = session
        self.registry = AgentRegistryService(session)
        self.runtime = runtime or PromptAggregationService(session)

    def create_plan(self, user_input: str) -> MaestroPlan:
        cleaned_input = user_input.strip()
        if not cleaned_input:
            raise MaestroOrchestratorError("Maestro input cannot be blank.")

        agents = self.registry.list_specs()
        domains = self.registry.list_domain_contexts()
        tools = self.registry.list_tools()
        selected_agents = self._select_agents(cleaned_input, agents)
        intents = self._classify_intents(cleaned_input, selected_agents)
        subtasks = self._build_subtasks(cleaned_input, selected_agents, intents)
        summary = self._plan_summary(cleaned_input, intents, subtasks)
        plan_id = str(uuid.uuid4())
        parent_task = Task(
            status="proposed",
            priority="high" if any(subtask.priority == "high" for subtask in subtasks) else "normal",
            source_type="maestro_chat",
            workflow_key="maestro.generic",
            objective=summary,
            input_payload={
                "plan_id": plan_id,
                "user_input": cleaned_input,
                "execution_mode": "propose_first",
                "intents": [intent.__dict__ for intent in intents],
                "subtasks": [subtask.__dict__ for subtask in subtasks],
                "selected_agents": [
                    self._selected_agent_payload(agent, user_input=cleaned_input)
                    for agent in selected_agents
                ],
                "registry_snapshot": self._registry_snapshot(domains, agents, tools),
                "approval_required": True,
                "scheduler": self._scheduler_payload(),
            },
        )
        self.session.add(parent_task)
        self.session.commit()
        self.session.refresh(parent_task)
        return self._plan_from_task(parent_task)

    def get_plan(self, plan_id: uuid.UUID | str) -> MaestroPlan:
        plan_uuid = plan_id if isinstance(plan_id, uuid.UUID) else uuid.UUID(str(plan_id))
        task = self.session.scalar(
            select(Task).where(
                Task.id == plan_uuid,
                Task.workflow_key == "maestro.generic",
            )
        )
        if task is None:
            task = self.session.scalar(
                select(Task).where(
                    Task.input_payload["plan_id"].as_string() == str(plan_uuid),
                    Task.workflow_key == "maestro.generic",
                )
            )
        if task is None:
            raise MaestroOrchestratorError(f"Unknown Maestro plan: {plan_id}")
        return self._plan_from_task(task)

    def run_plan(self, plan_id: uuid.UUID | str, *, execute_llm: bool = True) -> MaestroRun:
        plan = self.get_plan(plan_id)
        parent_task = self.session.get(Task, uuid.UUID(plan.parent_task_id))
        if parent_task is None:
            raise MaestroOrchestratorError(f"Plan parent task was not found: {plan.parent_task_id}")
        if parent_task.status not in {"proposed", "queued", "failed"}:
            raise MaestroOrchestratorError(
                f"Plan cannot be run from status {parent_task.status}."
            )

        parent_task.status = "running"
        parent_task.started_at = datetime.now(UTC)
        self.session.commit()

        child_runs: list[AgentRunResult] = []
        status = "completed"
        error_message: str | None = None
        try:
            for subtask in plan.subtasks:
                child_runs.append(
                    self.runtime.run_agent_once(
                        PromptPackageRequest(
                            agent_key=subtask.agent_key,
                            task_instruction=subtask.objective,
                            caller="maestro",
                            user_context=plan.user_input,
                            query_text=plan.user_input,
                            use_semantic=True,
                        ),
                        stage_interaction=False,
                        execute_llm=execute_llm,
                        parent_task_id=parent_task.id,
                        source_type="maestro_orchestrator",
                        workflow_key="maestro.generic.child",
                        priority=subtask.priority,
                    )
                )
            if any(run.status == "failed" for run in child_runs):
                status = "failed"
                error_message = "One or more delegated agent tasks failed."
            synthesis = self._synthesize(plan, child_runs, status=status)
            report = self._write_synthesis_report(parent_task, plan, child_runs, synthesis, status)
            staged = self._stage_workflow_artifact(parent_task, plan, child_runs, synthesis, report)
            parent_task.status = status
            parent_task.output_payload = {
                "plan_id": plan.plan_id,
                "status": status,
                "child_task_ids": [run.task_id for run in child_runs],
                "child_report_ids": [run.report_id for run in child_runs if run.report_id],
                "synthesis_report_id": str(report.id),
                "staged_artifact_path": staged.path,
                "artifact_id": staged.artifact_id,
            }
            parent_task.error_message = error_message
            parent_task.completed_at = datetime.now(UTC)
            self.session.commit()
            self.session.refresh(report)
            self.session.refresh(parent_task)
            return MaestroRun(
                plan=self._plan_from_task(parent_task),
                status=status,
                parent_task_id=str(parent_task.id),
                child_runs=child_runs,
                synthesis_report_id=str(report.id),
                synthesis=synthesis,
                staged_artifact_path=staged.path,
                artifact_id=staged.artifact_id,
                scheduler=self._scheduler_payload(),
                error_message=error_message,
            )
        except Exception as exc:
            parent_task.status = "failed"
            parent_task.error_message = str(exc)
            parent_task.completed_at = datetime.now(UTC)
            self.session.commit()
            raise

    def _select_agents(self, user_input: str, agents) -> list:
        lowered = user_input.lower()
        scored: list[tuple[float, Any, str]] = []
        for agent in agents:
            score, rationale = self._agent_relevance_score(lowered, agent)
            if score > 0:
                scored.append((score, agent, rationale))
        if not scored:
            scored = [
                (1.0, agent, "default fallback agent")
                for agent in agents
                if agent.domain_key in {"praxis", "personal", "maestro-development"}
            ]
        if not scored and agents:
            scored = [(1.0, agents[0], "first available agent")]

        scored.sort(key=lambda item: (-item[0], item[1].domain_key, item[1].key))
        top_score = scored[0][0] if scored else 0
        if top_score <= 3.5:
            return [scored[0][1]] if scored else []
        threshold = max(3.5, top_score * 0.55)
        selected = [
            agent
            for score, agent, _ in scored
            if score >= threshold
        ]
        if not selected and scored:
            selected = [scored[0][1]]
        return selected[:4]

    def _agent_relevance_score(self, lowered_input: str, agent) -> tuple[float, str]:
        score = 0.0
        rationale: list[str] = []
        domain_tokens = [agent.domain_key, agent.domain_key.replace("-", " ")]
        domain_tokens.extend(_DOMAIN_HINTS.get(agent.domain_key, []))
        if any(token and token in lowered_input for token in domain_tokens):
            score += 3.0
            rationale.append(f"domain match: {agent.domain_key}")

        agent_text = " ".join(
            [
                agent.key,
                agent.name,
                agent.role_summary,
                agent.current_action or "",
                " ".join(tool.key for tool in agent.allowed_tools),
            ]
        )
        input_tokens = _meaningful_tokens(lowered_input)
        agent_tokens = _meaningful_tokens(agent_text.lower())
        domain_noise = _meaningful_tokens(
            " ".join([agent.domain_key, agent.domain_key.replace("-", " "), "agent"])
        )
        agent_tokens = agent_tokens - domain_noise
        input_tokens = input_tokens - domain_noise
        overlap = sorted(input_tokens & agent_tokens)
        if overlap:
            score += min(len(overlap), 6) * 0.75
            rationale.append(f"role overlap: {', '.join(overlap[:6])}")
        if any(token in agent.key for token in ("planning", "chief", "manager", "lead")):
            score += 0.5
            rationale.append("coordination-capable role")
        return score, "; ".join(rationale) or "low relevance"

    def _build_subtasks(
        self,
        user_input: str,
        selected_agents,
        intents: list[MaestroIntent],
    ) -> list[MaestroSubtask]:
        priority = "high" if any(intent.priority == "high" for intent in intents) else "normal"
        return [
            MaestroSubtask(
                agent_key=agent.key,
                agent_name=agent.name,
                domain_key=agent.domain_key,
                objective=self._subtask_objective(user_input, agent, intents),
                expected_output=self._expected_output_for_agent(agent, intents),
                priority=priority,
                rationale=self._subtask_rationale(user_input, agent),
            )
            for agent in selected_agents
        ]

    def _classify_intents(self, user_input: str, selected_agents) -> list[MaestroIntent]:
        lowered = user_input.lower()
        default_domain = selected_agents[0].domain_key if selected_agents else None
        intents: list[MaestroIntent] = []
        if any(token in lowered for token in ("plan", "prepare", "coordinate", "workflow")):
            intents.append(
                MaestroIntent(
                    type="workflow",
                    summary="Coordinate a multi-step plan.",
                    target=user_input[:180],
                    domain_key=default_domain,
                    priority="high",
                    action="Generate an executable workflow plan and delegate work by agent specialty.",
                )
            )
        if any(token in lowered for token in ("task", "todo", "due", "follow up", "follow-up")):
            intents.append(
                MaestroIntent(
                    type="task",
                    summary="Capture or delegate a concrete follow-up.",
                    target=user_input[:180],
                    domain_key=default_domain,
                    action="Identify concrete due-outs and owners rather than storing them as memory.",
                )
            )
        if any(token in lowered for token in ("contact", "lead", "partner", "crm")):
            intents.append(
                MaestroIntent(
                    type="contact",
                    summary="Extract or use relationship/contact context.",
                    target=user_input[:180],
                    domain_key=default_domain,
                    action="Identify contact facts and relationship context for CRM routing.",
                )
            )
        if any(token in lowered for token in ("event", "calendar", "meeting", "call", "sync")):
            intents.append(
                MaestroIntent(
                    type="event",
                    summary="Extract or reason over schedule context.",
                    target=user_input[:180],
                    domain_key=default_domain,
                    action="Identify time-bound events or calendar implications.",
                )
            )
        if any(token in lowered for token in ("decision", "decide", "tradeoff", "recommend")):
            intents.append(
                MaestroIntent(
                    type="decision",
                    summary="Surface a decision or recommendation.",
                    target=user_input[:180],
                    domain_key=default_domain,
                    action="Separate recommendations and decisions from factual memory.",
                )
            )
        if any(token in lowered for token in ("?", "confirm", "need from me", "rfi", "question")):
            intents.append(
                MaestroIntent(
                    type="rfi",
                    summary="Identify information needed from Chris.",
                    target=user_input[:180],
                    domain_key=default_domain,
                    action="Surface questions that block execution or improve output quality.",
                )
            )
        if any(token in lowered for token in ("remember", "memory", "standing instruction", "preference")):
            intents.append(
                MaestroIntent(
                    type="memory_route",
                    summary="Route durable context through memory curation.",
                    target=user_input[:180],
                    domain_key=default_domain,
                    action="Send durable context to the memory curation pipeline at session close.",
                )
            )
        if not intents:
            intents.append(
                MaestroIntent(
                    type="direct_chat",
                    summary="Respond directly unless the user approves further work.",
                    target=user_input[:180],
                    domain_key=default_domain,
                    action="Prepare a concise direct answer and avoid unnecessary delegation.",
                )
            )
        if selected_agents and not any(intent.type == "workflow" for intent in intents):
            intents.append(
                MaestroIntent(
                    type="workflow",
                    summary="Use available agents if Chris approves execution.",
                    target=user_input[:180],
                    domain_key=default_domain,
                    action="Create agent-specific subtasks before execution.",
                )
            )
        return intents

    def _subtask_objective(
        self,
        user_input: str,
        agent,
        intents: list[MaestroIntent],
    ) -> str:
        intent_list = ", ".join(intent.type for intent in intents)
        intent_actions = "\n".join(
            f"- {intent.type}: {intent.action or intent.summary}" for intent in intents
        )
        role_summary = agent.role_summary or "No role summary configured."
        tool_list = ", ".join(tool.key for tool in agent.allowed_tools) or "no tools configured"
        return (
            f"You are {agent.name}. Work only within the {agent.domain_key} domain and only on "
            "the portion of this Maestro request that fits your specialty.\n\n"
            f"Your specialty: {role_summary}\n"
            f"Authorized tools: {tool_list}\n"
            f"Detected planning lanes: {intent_list}\n"
            f"Lane actions:\n{intent_actions}\n\n"
            f"Original Maestro request:\n{user_input}\n\n"
            "Do not answer for sister agents. Produce your domain contribution, note assumptions, "
            "surface RFIs, and call out any tasks/events/contacts/decisions that Maestro should "
            "route separately."
        )

    def _expected_output_for_agent(self, agent, intents: list[MaestroIntent]) -> str:
        if any(intent.type in {"workflow", "decision"} for intent in intents):
            return (
                "Role-scoped report with summary, specialty-specific findings, recommended "
                "actions, RFIs, and dependencies on other agents."
            )
        if any(intent.type in {"task", "event", "contact"} for intent in intents):
            return (
                "Structured extraction notes for items that should become tasks, events, contacts, "
                "or RFIs, plus a short action summary."
            )
        return "Concise role-scoped response with next steps and provenance notes."

    def _subtask_rationale(self, user_input: str, agent) -> str:
        score, rationale = self._agent_relevance_score(user_input.lower(), agent)
        return f"Selected for {rationale}; relevance score {score:.2f}."

    def _plan_summary(
        self,
        user_input: str,
        intents: list[MaestroIntent],
        subtasks: list[MaestroSubtask],
    ) -> str:
        return (
            f"Proposed Maestro plan for {len(subtasks)} agent task(s) and "
            f"{len(intents)} detected intent(s): {user_input[:180]}"
        )

    def _registry_snapshot(self, domains, agents, tools) -> dict[str, Any]:
        return {
            "domains": [
                {"key": domain.key, "name": domain.name, "context": domain.context}
                for domain in domains
            ],
            "agents": [self._selected_agent_payload(agent) for agent in agents],
            "tools": [
                {
                    "key": tool.key,
                    "name": tool.name,
                    "exclusive": tool.exclusive,
                    "connected_domains": tool.connected_domains,
                    "authorized_agents": tool.authorized_agents,
                }
                for tool in tools
            ],
        }

    def _selected_agent_payload(self, agent, *, user_input: str | None = None) -> dict[str, Any]:
        payload = {
            "key": agent.key,
            "name": agent.name,
            "domain_key": agent.domain_key,
            "role_summary": agent.role_summary,
            "current_action": agent.current_action,
            "allowed_tools": [
                {
                    "key": tool.key,
                    "name": tool.name,
                    "permission": tool.permission,
                    "connection_id": tool.connection_id,
                }
                for tool in agent.allowed_tools
            ],
        }
        if user_input is not None:
            payload["selection_rationale"] = self._subtask_rationale(user_input, agent)
        return payload

    def _scheduler_payload(self) -> dict[str, Any]:
        return {
            "status": "queue_foundation",
            "policy": "Plan-first execution. Child tasks run sequentially in MVP.",
            "resource_locks": [],
            "recurring_scheduler": "planned",
        }

    def _plan_from_task(self, task: Task) -> MaestroPlan:
        payload = task.input_payload or {}
        return MaestroPlan(
            plan_id=str(payload.get("plan_id") or task.id),
            status=task.status,
            user_input=str(payload.get("user_input") or ""),
            summary=task.objective,
            execution_mode=str(payload.get("execution_mode") or "propose_first"),
            intents=[MaestroIntent(**intent) for intent in payload.get("intents", [])],
            subtasks=[MaestroSubtask(**subtask) for subtask in payload.get("subtasks", [])],
            selected_agents=list(payload.get("selected_agents", [])),
            registry_snapshot=dict(payload.get("registry_snapshot", {})),
            approval_required=bool(payload.get("approval_required", True)),
            scheduler=dict(payload.get("scheduler", self._scheduler_payload())),
            created_at=task.created_at.isoformat() if task.created_at else "",
            parent_task_id=str(task.id),
        )

    def _synthesize(
        self,
        plan: MaestroPlan,
        child_runs: list[AgentRunResult],
        *,
        status: str,
    ) -> str:
        lines = [
            f"# Maestro Synthesis: {plan.summary}",
            "",
            f"Status: {status}",
            "",
            "## Original Request",
            plan.user_input,
            "",
            "## Delegated Results",
        ]
        for run in child_runs:
            lines.extend(
                [
                    f"### {run.agent.name} ({run.agent.domain_key})",
                    f"Status: {run.status}",
                    run.output_text or run.execution_note,
                    "",
                ]
            )
        lines.extend(
            [
                "## Next Steps",
                "- Review the synthesized output.",
                "- Address any RFIs or routed work surfaced by the agents.",
                "- Let the memory pipeline curate the canonical workflow artifact.",
            ]
        )
        return "\n".join(lines)

    def _write_synthesis_report(
        self,
        parent_task: Task,
        plan: MaestroPlan,
        child_runs: list[AgentRunResult],
        synthesis: str,
        status: str,
    ) -> Report:
        report = Report(
            task_id=parent_task.id,
            title="Maestro workflow synthesis",
            report_type="maestro_workflow_synthesis",
            summary=synthesis[:500],
            body_markdown=synthesis,
            structured_data={
                "plan_id": plan.plan_id,
                "status": status,
                "child_runs": [
                    {
                        "agent_key": run.agent.key,
                        "domain_key": run.agent.domain_key,
                        "status": run.status,
                        "task_id": run.task_id,
                        "report_id": run.report_id,
                    }
                    for run in child_runs
                ],
            },
        )
        self.session.add(report)
        self.session.flush()
        return report

    def _stage_workflow_artifact(
        self,
        parent_task: Task,
        plan: MaestroPlan,
        child_runs: list[AgentRunResult],
        synthesis: str,
        report: Report,
    ):
        package = InteractionArtifactPackager(self.session).build_package(
            domain_key="maestro-development",
            agent_key=None,
            user_input=plan.user_input,
            maestro_tasking=plan.summary,
            agent_output=synthesis,
            tool_calls=[
                tool_call
                for run in child_runs
                for tool_call in run.tool_calls
            ],
            generated_artifacts=[
                {
                    "name": f"{run.agent.key}-report",
                    "type": "agent_report",
                    "task_id": run.task_id,
                    "report_id": run.report_id,
                }
                for run in child_runs
            ]
            + [
                {
                    "name": "maestro-workflow-synthesis",
                    "type": "maestro_synthesis_report",
                    "task_id": str(parent_task.id),
                    "report_id": str(report.id),
                }
            ],
            open_questions=[
                intent.target for intent in plan.intents if intent.type == "rfi"
            ],
            next_steps=["Review workflow synthesis.", "Run memory curation on this artifact."],
            task_id=str(parent_task.id),
            provenance={
                "plan_id": plan.plan_id,
                "workflow_key": parent_task.workflow_key,
                "child_task_ids": [run.task_id for run in child_runs],
                "canonical_workflow_artifact": True,
            },
        )
        staged = InteractionArtifactPackager(self.session).stage_package(package)
        artifact = self.session.get(Artifact, uuid.UUID(staged.artifact_id or ""))
        if artifact is not None:
            artifact.task_id = parent_task.id
            artifact.report_id = report.id
            artifact.metadata_ = {
                **(artifact.metadata_ or {}),
                "plan_id": plan.plan_id,
                "workflow_key": parent_task.workflow_key,
                "canonical_workflow_artifact": True,
            }
            self.session.commit()
        return staged


_DOMAIN_HINTS = {
    "personal": ["personal", "calendar", "family", "life", "preference"],
    "maestro-development": ["maestro", "orchestrator", "agent", "memory", "system", "code"],
    "praxis": ["praxis", "partner", "tactical innovation", "transition", "training"],
    "ophi": ["ophi", "product", "market", "research"],
    "usma": ["usma", "cadet", "class", "teaching", "academic"],
    "personal-irad-projects": ["irad", "prototype", "project"],
    "l3": ["l3"],
}

_STOPWORDS = {
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


def _meaningful_tokens(text: str) -> set[str]:
    normalized = "".join(character if character.isalnum() else " " for character in text.lower())
    return {
        token
        for token in normalized.split()
        if len(token) > 2 and token not in _STOPWORDS
    }
