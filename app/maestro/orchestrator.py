"""Top-level Maestro planning, delegation, execution, and synthesis service.

This module is intentionally the current orchestration center: it turns a user message into a plan,
routes direct operational items, delegates agent work, resumes blocked tool approvals, synthesizes
results, and stages one canonical workflow artifact for memory curation. The cleanup roadmap in
`docs/CODEBASE_CLEANUP.md` tracks how to split these responsibilities once behavior stabilizes.
"""

import json
import re
import uuid
from dataclasses import dataclass, replace
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
from app.core.config import get_settings
from app.core.time import home_isoformat
from app.db.models import Artifact, Report, RoutedItem, Task, ToolCall
from app.db.repositories import DomainRepository
from app.llm.client import LLMClient, LLMClientError, OpenAILLMClient
from app.maestro.context_assembler import MaestroContextAssembler, maestro_context_payload
from app.maestro.planner import (
    LLMMaestroPlanner,
    MaestroPlannerResponse,
    PlannerWorkItem,
)
from app.maestro.planner_rules import (
    DOMAIN_HINTS,
    action_for_work_item,
    domain_matches,
    intent_type_for_work_item,
    meaningful_tokens,
    route_type_for_work_item,
)
from app.maestro.scheduler import SchedulerService
from app.memory.routed_service import RoutedMemoryService
from app.tools.runtime import (
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolExecutionService,
    tool_result_payload,
)


def _strip_hidden_context(value: str) -> str:
    stripped = re.sub(
        r"<maestro_hidden_context\b[^>]*>.*?</maestro_hidden_context>",
        "",
        value,
        flags=re.DOTALL | re.IGNORECASE,
    )
    stripped = re.sub(
        r"<latest_chris_message>\s*(.*?)\s*</latest_chris_message>",
        r"\1",
        stripped,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return " ".join(stripped.split())


def _coding_pr_number(tool_calls: list[dict[str, Any]]) -> int | None:
    for call in reversed(tool_calls):
        if call.get("tool_name") != "codex.task.run":
            continue
        output = call.get("output_payload")
        if not isinstance(output, dict):
            continue
        number = output.get("pr_number")
        if number is None and isinstance(output.get("pr"), dict):
            number = output["pr"].get("number") or output["pr"].get("pr_number")
        try:
            return int(number)
        except (TypeError, ValueError):
            continue
    return None


def _truncate_registry_text(value: str | None, max_chars: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(0, max_chars - 3)].rstrip() + "..."


def _hydrate_work_item_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "required_skills": payload.get("required_skills") or [],
        "model_profile": payload.get("model_profile"),
        "model_tier": payload.get("model_tier") or "auto",
        "model_rationale": payload.get("model_rationale") or "",
    }


def _hydrate_subtask_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "required_skills": payload.get("required_skills") or [],
        "model_profile": payload.get("model_profile"),
        "model_tier": payload.get("model_tier") or "auto",
        "model_rationale": payload.get("model_rationale"),
    }


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
    work_item_ids: list[str] | None = None
    depends_on_work_item_ids: list[str] | None = None
    required_skills: list[str] | None = None
    model_profile: str | None = None
    model_tier: str = "auto"
    model_rationale: str | None = None


@dataclass(frozen=True)
class MaestroWorkItem:
    id: str
    type: str
    title: str
    description: str
    domain_key: str | None
    priority: str
    required_capabilities: list[str]
    required_tools: list[str]
    required_skills: list[str]
    model_profile: str | None
    model_tier: str
    model_rationale: str
    dependencies: list[str]
    needs_agent: bool
    needs_user_input: bool
    blocks_execution: bool
    can_log_directly: bool
    suggested_agent_keys: list[str]
    expected_output: str
    rationale: str


@dataclass(frozen=True)
class MaestroPlan:
    plan_id: str
    status: str
    user_input: str
    summary: str
    execution_mode: str
    planner_mode: str
    work_items: list[MaestroWorkItem]
    intents: list[MaestroIntent]
    subtasks: list[MaestroSubtask]
    execution_stages: list[list[str]]
    workflow_graph: dict[str, Any]
    is_chat_only: bool
    is_routing_only: bool
    selected_agents: list[dict[str, Any]]
    registry_snapshot: dict[str, Any]
    approval_required: bool
    scheduler: dict[str, Any]
    created_at: str
    parent_task_id: str
    direct_response: str | None = None
    planner_notes: str | None = None


@dataclass(frozen=True)
class MaestroRun:
    plan: MaestroPlan
    status: str
    parent_task_id: str
    child_runs: list[AgentRunResult]
    synthesis_report_id: str | None
    synthesis: str
    chat_summary: str
    staged_artifact_path: str | None
    artifact_id: str | None
    scheduler: dict[str, Any]
    execution_stages: list[list[str]]
    tool_activity: list[dict[str, Any]]
    error_message: str | None = None


class MaestroOrchestratorError(ValueError):
    pass


class MaestroOrchestratorService:
    def __init__(
        self,
        session: Session,
        *,
        runtime: PromptAggregationService | None = None,
        planner_llm_client: LLMClient | None = None,
    ):
        self.session = session
        self.registry = AgentRegistryService(session)
        self.runtime = runtime or PromptAggregationService(session)
        self.planner_llm_client = planner_llm_client

    def create_plan(
        self,
        user_input: str,
        *,
        conversation_id: uuid.UUID | None = None,
        topic_id: str | None = None,
    ) -> MaestroPlan:
        cleaned_input = user_input.strip()
        if not cleaned_input:
            raise MaestroOrchestratorError("Maestro input cannot be blank.")
        visible_input = _strip_hidden_context(cleaned_input)

        agents = self.registry.list_specs()
        domains = self.registry.list_domain_contexts()
        tools = self.registry.list_tools()
        skills = self.registry.list_skills()
        registry_snapshot = self._registry_snapshot(domains, agents, tools, skills)
        maestro_context = self._planning_context_bundle(visible_input)
        decomposition, planner_mode = self._decompose_request(
            cleaned_input,
            registry_snapshot=registry_snapshot,
            maestro_context=maestro_context,
        )
        decomposition = MaestroPlannerResponse(
            plan_summary=_strip_hidden_context(decomposition.plan_summary),
            direct_response=_strip_hidden_context(decomposition.direct_response)
            if decomposition.direct_response
            else None,
            planner_notes=_strip_hidden_context(decomposition.planner_notes),
            work_items=decomposition.work_items,
        )
        work_items = self._harden_work_items(
            [self._work_item_from_planner(item) for item in decomposition.work_items],
            user_input=visible_input,
        )
        is_routing_only = self._is_routing_only(work_items)
        is_chat_only = self._is_chat_only(work_items, decomposition) or is_routing_only
        selected_agents = self._select_agents_for_work_items(work_items, agents)
        intents = self._intents_from_work_items(work_items, selected_agents)
        subtasks = self._build_subtasks(visible_input, selected_agents, intents, work_items)
        execution_stages = self._execution_stage_keys(subtasks)
        workflow_graph = self._workflow_graph(work_items, subtasks)
        queue_items = [] if is_chat_only else self._queue_items(subtasks)
        schedule_guidance = self._schedule_guidance_from_classifier(cleaned_input)
        schedule_candidate = self._schedule_candidate_from_input(
            visible_input,
            work_items,
            subtasks,
            schedule_guidance=schedule_guidance,
        )
        summary = decomposition.plan_summary or self._plan_summary(cleaned_input, intents, subtasks)
        direct_response = decomposition.direct_response
        if is_routing_only:
            direct_response = self._routed_direct_response(work_items)
        plan_id = str(uuid.uuid4())
        parent_task = Task(
            conversation_id=conversation_id,
            status="completed" if is_chat_only else "proposed",
            priority="high" if any(subtask.priority == "high" for subtask in subtasks) else "normal",
            source_type="maestro_chat",
            workflow_key="maestro.generic",
            objective=summary,
            input_payload={
                "plan_id": plan_id,
                "topic_id": topic_id,
                "user_input": visible_input,
                "execution_mode": "propose_first",
                "planner_mode": planner_mode,
                "planner_prompt_metrics": getattr(self, "_last_planner_prompt_metrics", {}),
                "maestro_context_metrics": {
                    "used_chars": maestro_context.get("used_chars"),
                    "max_chars": maestro_context.get("max_chars"),
                    "memory_count": maestro_context.get("sections", {})
                    .get("memory", {})
                    .get("included_count"),
                    "report_count": len(maestro_context.get("sections", {}).get("reports", {}).get("items", [])),
                    "run_log_count": len(maestro_context.get("sections", {}).get("run_log", {}).get("items", [])),
                },
                "work_items": [work_item.__dict__ for work_item in work_items],
                "intents": [intent.__dict__ for intent in intents],
                "subtasks": [subtask.__dict__ for subtask in subtasks],
                "execution_stages": execution_stages,
                "workflow_graph": workflow_graph,
                "is_chat_only": is_chat_only,
                "is_routing_only": is_routing_only,
                "selected_agents": [
                    self._selected_agent_payload(agent, user_input=cleaned_input)
                    for agent in selected_agents
                ],
                "registry_snapshot": registry_snapshot,
                "approval_required": not is_chat_only,
                "scheduler": self._scheduler_payload(
                    queue_items=queue_items,
                    status="direct_chat" if is_chat_only else "queue_foundation",
                    schedule_candidate=schedule_candidate,
                ),
                "direct_response": direct_response,
                "planner_notes": decomposition.planner_notes,
            },
            completed_at=datetime.now(UTC) if is_chat_only else None,
        )
        self.session.add(parent_task)
        self.session.commit()
        self.session.refresh(parent_task)
        self._route_direct_work_items(parent_task, work_items)
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

    def refine_plan(self, plan_id: uuid.UUID | str, refinement: str) -> MaestroPlan:
        cleaned_refinement = refinement.strip()
        if not cleaned_refinement:
            raise MaestroOrchestratorError("Maestro refinement cannot be blank.")
        previous_plan = self.get_plan(plan_id)
        previous_task = self.session.get(Task, uuid.UUID(previous_plan.parent_task_id))
        refined_input = self._refined_plan_input(previous_plan, cleaned_refinement)
        plan = self.create_plan(
            refined_input,
            conversation_id=previous_task.conversation_id if previous_task else None,
            topic_id=(previous_task.input_payload or {}).get("topic_id") if previous_task else None,
        )
        if previous_task is not None and previous_task.status in {
            "proposed",
            "queued",
            "ready",
            "blocked",
            "failed",
        }:
            self._archive_parent_task(
                previous_task,
                reason=f"Workflow superseded by refined plan {plan.plan_id}.",
                commit=False,
            )
        task = self.session.get(Task, uuid.UUID(plan.parent_task_id))
        if task is not None:
            task.input_payload = {
                **(task.input_payload or {}),
                "refined_from_plan_id": previous_plan.plan_id,
                "refinement": cleaned_refinement,
            }
            self.session.commit()
            self.session.refresh(task)
            return self._plan_from_task(task)
        return plan

    def archive_plan(
        self,
        plan_id: uuid.UUID | str,
        *,
        reason: str = "Workflow archived at Chris's request.",
    ) -> MaestroPlan:
        plan = self.get_plan(plan_id)
        task = self.session.get(Task, uuid.UUID(plan.parent_task_id))
        if task is None:
            raise MaestroOrchestratorError(f"Plan parent task was not found: {plan.parent_task_id}")
        self._archive_parent_task(task, reason=reason, commit=True)
        return self._plan_from_task(task)

    def archive_open_plans_for_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        reason: str = "Open workflows archived at Chris's request.",
    ) -> int:
        open_statuses = {"proposed", "queued", "ready", "running", "blocked", "failed", "scheduled"}
        tasks = self.session.scalars(
            select(Task)
            .where(
                Task.conversation_id == conversation_id,
                Task.workflow_key == "maestro.generic",
                Task.status.in_(open_statuses),
            )
            .order_by(Task.created_at.desc())
        ).all()
        for task in tasks:
            self._archive_parent_task(task, reason=reason, commit=False)
        self.session.commit()
        return len(tasks)

    def _archive_parent_task(self, task: Task, *, reason: str, commit: bool) -> None:
        raw_scheduler = task.input_payload.get("scheduler") if isinstance(task.input_payload, dict) else {}
        scheduler = raw_scheduler if isinstance(raw_scheduler, dict) else {}
        queue_items = scheduler.get("queue_items") if isinstance(scheduler.get("queue_items"), list) else []
        archived_queue_items = [
            {
                **item,
                "status": "archived",
                "error_message": item.get("error_message") or reason,
            }
            for item in queue_items
            if isinstance(item, dict)
        ]
        if isinstance(task.input_payload, dict):
            task.input_payload = {
                **task.input_payload,
                "scheduler": {
                    **scheduler,
                    "status": "archived",
                    "current_step": "Workflow archived.",
                    "queue_items": archived_queue_items,
                },
            }
        task.status = "archived"
        task.error_message = reason
        task.completed_at = task.completed_at or datetime.now(UTC)
        scheduler_service = SchedulerService(self.session)
        definition_id = self._scheduled_definition_id(task)
        if definition_id is not None:
            scheduler_service.archive_definition(
                definition_id,
                reason=reason,
                commit=False,
            )
        scheduler_service.archive_run_for_parent_task(
            task.id,
            reason=reason,
            commit=False,
        )
        if commit:
            self.session.commit()
            self.session.refresh(task)

    def _scheduled_definition_id(self, task: Task) -> uuid.UUID | None:
        payload = task.input_payload or {}
        scheduler = payload.get("scheduler") if isinstance(payload.get("scheduler"), dict) else {}
        candidates = [
            scheduler.get("scheduled_definition_id"),
            (task.output_payload or {}).get("scheduled_definition_id") if task.output_payload else None,
        ]
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return uuid.UUID(str(candidate))
            except (TypeError, ValueError):
                continue
        return None

    def _route_direct_work_items(
        self,
        parent_task: Task,
        work_items: list[MaestroWorkItem],
    ) -> list[RoutedItem]:
        routed_items: list[RoutedItem] = []
        for item in work_items:
            route_type = self._route_type_for_work_item(item)
            if route_type is None:
                continue
            routed_items.append(
                RoutedItem(
                    domain_id=self._domain_id_for_key(item.domain_key) or parent_task.domain_id,
                    task_id=parent_task.id,
                    route_type=route_type,
                    title=item.title,
                    content=item.description,
                    priority=item.priority,
                    status="needs_input" if route_type == "human_input" else "open",
                    source_refs=[
                        {
                            "type": "maestro_chat",
                            "task_id": str(parent_task.id),
                            "plan_id": str((parent_task.input_payload or {}).get("plan_id") or parent_task.id),
                        }
                    ],
                    metadata_={
                        "curator": "maestro_orchestrator",
                        "work_item_id": item.id,
                        "work_item_type": item.type,
                        "rationale": item.rationale,
                        "expected_output": item.expected_output,
                        "required_capabilities": item.required_capabilities,
                        "required_tools": item.required_tools,
                        "dependencies": item.dependencies,
                        "needs_agent": item.needs_agent,
                        "needs_user_input": item.needs_user_input,
                        "blocks_execution": item.blocks_execution,
                        "can_log_directly": item.can_log_directly,
                    },
                )
            )
        for routed_item in routed_items:
            self.session.add(routed_item)
        if routed_items:
            self.session.commit()
            RoutedMemoryService(self.session).promote_items(routed_items)
        return routed_items

    def _route_type_for_work_item(self, item: MaestroWorkItem) -> str | None:
        route_type = route_type_for_work_item(item.type)
        if item.type == "standalone_task":
            text = f"{item.title}\n{item.description}\n{item.expected_output}".lower()
            if any(token in text for token in ("contact", "crm", "relationship context", "partner lead")):
                return "contact"
            if any(token in text for token in ("event", "calendar", "meeting", "standup", "sync")):
                return "event"
            if any(token in text for token in ("decision:", "decision -", "decided ")):
                return "decision_log"
        return route_type

    def _domain_id_for_key(self, domain_key: str | None) -> uuid.UUID | None:
        if not domain_key:
            return None
        domain = DomainRepository(self.session).get_by_key(domain_key)
        return domain.id if domain else None

    def close_session(
        self,
        *,
        messages: list[dict[str, str]],
        plan_id: uuid.UUID | str | None = None,
    ) -> str | None:
        cleaned_messages = [
            {
                "sender": str(message.get("sender") or "").strip(),
                "content": str(message.get("content") or "").strip(),
            }
            for message in messages
            if str(message.get("content") or "").strip()
        ]
        if not cleaned_messages:
            return None
        plan = self.get_plan(plan_id) if plan_id else None
        transcript = "\n".join(
            f"{message['sender']}: {message['content']}" for message in cleaned_messages
        )
        package = InteractionArtifactPackager(self.session).build_package(
            domain_key="maestro-development",
            agent_key=None,
            user_input=transcript,
            maestro_tasking=plan.summary if plan else "Maestro chat session",
            agent_output=plan.direct_response if plan else None,
            generated_artifacts=[
                {
                    "name": "maestro-chat-transcript",
                    "type": "maestro_session_transcript",
                    "message_count": len(cleaned_messages),
                }
            ],
            open_questions=[
                item.description
                for item in (plan.work_items if plan else [])
                if item.type == "rfi" or item.needs_user_input
            ],
            next_steps=["Curate durable context from this Maestro session artifact."],
            task_id=plan.parent_task_id if plan else None,
            provenance={
                "session_boundary": "manual_new_session",
                "plan_id": plan.plan_id if plan else None,
                "artifact_role": "maestro_session_close",
            },
        )
        staged = InteractionArtifactPackager(self.session).stage_package(package)
        artifact = self.session.get(Artifact, uuid.UUID(staged.artifact_id or ""))
        if artifact is not None:
            artifact.metadata_ = {
                **(artifact.metadata_ or {}),
                "session_boundary": "manual_new_session",
                "plan_id": plan.plan_id if plan else None,
                "canonical_session_artifact": True,
            }
            self.session.commit()
        return staged.path

    def run_plan(
        self,
        plan_id: uuid.UUID | str,
        *,
        execute_llm: bool = True,
        auto_tool_loop: bool = False,
        max_tool_iterations: int = 2,
        resume: bool = False,
        approved_tool_results_by_child_task: dict[str, list[dict[str, Any]]] | None = None,
    ) -> MaestroRun:
        approved_tool_results_by_child_task = approved_tool_results_by_child_task or {}
        plan = self.get_plan(plan_id)
        parent_task = self.session.get(Task, uuid.UUID(plan.parent_task_id))
        if parent_task is None:
            raise MaestroOrchestratorError(f"Plan parent task was not found: {plan.parent_task_id}")
        if plan.is_chat_only:
            raise MaestroOrchestratorError("Direct chat responses do not have an executable plan.")
        runnable_parent_statuses = {"proposed", "queued", "failed"}
        if resume:
            runnable_parent_statuses.add("blocked")
        if parent_task.status not in runnable_parent_statuses:
            raise MaestroOrchestratorError(
                f"Plan cannot be run from status {parent_task.status}."
            )
        if self._is_schedule_definition_request(plan):
            return self._save_scheduled_workflow(parent_task, plan)
        self._upsert_schedule_definition_if_requested(parent_task, plan)
        blocking_dependency_ids = self._blocked_work_item_ids(plan.work_items)
        has_attached_blocking_dependency = any(
            set(item.dependencies) & {
                blocking_item.id
                for blocking_item in plan.work_items
                if blocking_item.needs_user_input and blocking_item.blocks_execution
            }
            for item in plan.work_items
        )
        executable_subtasks = [
            subtask
            for subtask in plan.subtasks
            if not (
                set(subtask.depends_on_work_item_ids or []) & blocking_dependency_ids
                or set(subtask.work_item_ids or []) & blocking_dependency_ids
            )
        ]
        if resume:
            self._mark_approved_queue_items_ready(
                parent_task,
                approved_tool_results_by_child_task,
            )
            executable_subtasks = [
                subtask
                for subtask in executable_subtasks
                if self._queue_item_status(parent_task, subtask) != "completed"
                and self._queue_item_status(parent_task, subtask) != "failed"
                and not (
                    self._queue_item_status(parent_task, subtask) == "blocked"
                    and not self._queue_item_has_approved_tool_result(
                        parent_task,
                        subtask,
                        approved_tool_results_by_child_task,
                    )
                )
            ]
        if blocking_dependency_ids and (not executable_subtasks or not has_attached_blocking_dependency):
            titles = ", ".join(
                item.title
                for item in plan.work_items
                if item.needs_user_input and item.blocks_execution
            )
            raise MaestroOrchestratorError(
                f"Plan needs Chris before execution can start: {titles}"
            )

        if not resume:
            self._replace_scheduler(
                parent_task,
                queue_items=self._queue_with_status(plan.scheduler, "pending"),
                scheduler_status="pending",
            )
        for subtask in plan.subtasks:
            if (
                set(subtask.depends_on_work_item_ids or []) & blocking_dependency_ids
                or set(subtask.work_item_ids or []) & blocking_dependency_ids
            ):
                self._update_queue_item(
                    parent_task,
                    subtask,
                    status="blocked",
                    error_message="Waiting for blocking user input.",
                )
        parent_task.status = "running"
        parent_task.started_at = datetime.now(UTC)
        self._set_scheduler_status(parent_task, "running")
        self.session.commit()
        scheduler_service = SchedulerService(self.session)
        scheduler_service.enqueue_maestro_plan(parent_task)
        scheduler_service.sync_run_status_from_task(parent_task)
        self.session.refresh(parent_task)

        child_runs: list[AgentRunResult] = []
        completed_outputs_by_work_item: dict[str, str] = {}
        status = "completed"
        error_message: str | None = None
        phase_syntheses: list[dict[str, Any]] = []
        failed_work_item_ids: set[str] = set()
        pending_approval_work_item_ids: set[str] = set()
        completed_outputs_by_work_item.update(self._completed_outputs_from_scheduler(parent_task))
        try:
            for stage_index, stage in enumerate(self._execution_stages(executable_subtasks), start=1):
                runnable_stage = [
                    subtask
                    for subtask in stage
                    if not (set(subtask.depends_on_work_item_ids or []) & failed_work_item_ids)
                ]
                blocked_stage = [subtask for subtask in stage if subtask not in runnable_stage]
                for blocked_subtask in blocked_stage:
                    self._update_queue_item(
                        parent_task,
                        blocked_subtask,
                        status="blocked",
                        completed_at=datetime.now(UTC).isoformat(),
                        error_message="Blocked because an upstream agent task failed.",
                    )
                if not runnable_stage:
                    continue
                for stage_subtask in runnable_stage:
                    self._update_queue_item(
                        parent_task,
                        stage_subtask,
                        status="ready",
                    )
                for stage_subtask in runnable_stage:
                    self._update_queue_item(
                        parent_task,
                        stage_subtask,
                        status="running",
                        started_at=datetime.now(UTC).isoformat(),
                    )
                stage_runs: list[AgentRunResult] = []
                for subtask in runnable_stage:
                    run = self._run_subtask_with_retries(
                        parent_task,
                        plan,
                        subtask,
                        completed_outputs_by_work_item,
                        execute_llm=execute_llm,
                        auto_tool_loop=auto_tool_loop,
                        max_tool_iterations=max_tool_iterations,
                        initial_tool_results=self._approved_tool_results_for_subtask(
                            parent_task,
                            subtask,
                            approved_tool_results_by_child_task,
                        ),
                    )
                    final_run = run["final"]
                    if not resume:
                        final_run = self._queue_coding_delivery_review(
                            parent_task,
                            subtask,
                            final_run,
                        )
                        run["attempts"][-1] = final_run
                    child_runs.extend(run["attempts"])
                    stage_runs.append(final_run)
                    if final_run.status == "failed":
                        failed_work_item_ids.update(subtask.work_item_ids or [])
                    elif self._run_has_pending_approval(final_run):
                        pending_approval_work_item_ids.update(subtask.work_item_ids or [])
                    else:
                        for work_item_id in subtask.work_item_ids or []:
                            completed_outputs_by_work_item[work_item_id] = (
                                final_run.output_text or final_run.execution_note
                            )
                phase_syntheses.append(
                    self._synthesize_phase(
                        stage_index=stage_index,
                        stage=runnable_stage,
                        child_runs=stage_runs,
                    )
                )
            if failed_work_item_ids:
                status = "failed"
                error_message = "One or more delegated agent tasks failed."
            elif pending_approval_work_item_ids:
                status = "blocked"
                error_message = "Some queue items are waiting for Chris to approve tool use."
            elif not resume and len(executable_subtasks) < len(plan.subtasks):
                status = "blocked"
                error_message = "Some queue items are waiting for blocking user input."
            elif self._scheduler_has_incomplete_queue(parent_task):
                status = "blocked"
                error_message = "Some queue items are still waiting to run."
            tool_activity = self._tool_activity(child_runs)
            chat_summary = self._chat_summary(
                plan,
                child_runs,
                status=status,
                tool_activity=tool_activity,
            )
            synthesis = self._synthesize(
                plan,
                child_runs,
                status=status,
                phase_syntheses=phase_syntheses,
                tool_activity=tool_activity,
            )
            report = self._write_synthesis_report(
                parent_task,
                plan,
                child_runs,
                synthesis,
                status,
                phase_syntheses=phase_syntheses,
                tool_activity=tool_activity,
            )
            staged = self._stage_workflow_artifact(
                parent_task,
                plan,
                child_runs,
                synthesis,
                report,
                phase_syntheses=phase_syntheses,
            )
            execution_stages = self._execution_stage_keys(executable_subtasks)
            parent_task.status = status
            self._set_scheduler_status(parent_task, status)
            parent_task.output_payload = {
                "plan_id": plan.plan_id,
                "status": status,
                "child_task_ids": [run.task_id for run in child_runs],
                "child_report_ids": [run.report_id for run in child_runs if run.report_id],
                "execution_stages": execution_stages,
                "scheduler": dict((parent_task.input_payload or {}).get("scheduler", {})),
                "phase_syntheses": phase_syntheses,
                "tool_activity": tool_activity,
                "chat_summary": chat_summary,
                "synthesis_report_id": str(report.id),
                "staged_artifact_path": staged.path,
                "artifact_id": staged.artifact_id,
            }
            parent_task.error_message = error_message
            parent_task.completed_at = datetime.now(UTC)
            self.session.commit()
            SchedulerService(self.session).sync_run_status_from_task(parent_task)
            self.session.refresh(report)
            self.session.refresh(parent_task)
            return MaestroRun(
                plan=self._plan_from_task(parent_task),
                status=status,
                parent_task_id=str(parent_task.id),
                child_runs=child_runs,
                synthesis_report_id=str(report.id),
                synthesis=synthesis,
                chat_summary=chat_summary,
                staged_artifact_path=staged.path,
                artifact_id=staged.artifact_id,
                scheduler=dict((parent_task.input_payload or {}).get("scheduler", {})),
                execution_stages=execution_stages,
                tool_activity=tool_activity,
                error_message=error_message,
            )
        except Exception as exc:
            for subtask in plan.subtasks:
                self._update_queue_item(
                    parent_task,
                    subtask,
                    status="failed",
                    completed_at=datetime.now(UTC).isoformat(),
                    error_message=str(exc),
                    only_statuses={"ready", "running"},
                )
            parent_task.status = "failed"
            parent_task.error_message = str(exc)
            parent_task.completed_at = datetime.now(UTC)
            self._set_scheduler_status(parent_task, "failed")
            self.session.commit()
            SchedulerService(self.session).sync_run_status_from_task(parent_task)
            raise

    def enqueue_plan(self, plan_id: uuid.UUID | str) -> MaestroRun:
        plan = self.get_plan(plan_id)
        parent_task = self.session.get(Task, uuid.UUID(plan.parent_task_id))
        if parent_task is None:
            raise MaestroOrchestratorError(f"Plan parent task was not found: {plan.parent_task_id}")
        if plan.is_chat_only:
            raise MaestroOrchestratorError("Direct chat responses do not have an executable plan.")
        if parent_task.status not in {"proposed", "queued", "failed"}:
            raise MaestroOrchestratorError(
                f"Plan cannot be queued from status {parent_task.status}."
            )
        if self._is_schedule_definition_request(plan):
            return self._save_scheduled_workflow(parent_task, plan)
        self._upsert_schedule_definition_if_requested(parent_task, plan)
        blocking_dependency_ids = self._blocked_work_item_ids(plan.work_items)
        executable_subtasks = [
            subtask
            for subtask in plan.subtasks
            if not (
                set(subtask.depends_on_work_item_ids or []) & blocking_dependency_ids
                or set(subtask.work_item_ids or []) & blocking_dependency_ids
            )
        ]
        has_attached_blocking_dependency = any(
            set(item.dependencies) & {
                blocking_item.id
                for blocking_item in plan.work_items
                if blocking_item.needs_user_input and blocking_item.blocks_execution
            }
            for item in plan.work_items
        )
        if blocking_dependency_ids and (not executable_subtasks or not has_attached_blocking_dependency):
            titles = ", ".join(
                item.title
                for item in plan.work_items
                if item.needs_user_input and item.blocks_execution
            )
            raise MaestroOrchestratorError(
                f"Plan needs Chris before execution can start: {titles}"
            )

        queue_items = self._queue_with_status(plan.scheduler, "queued")
        for item in queue_items:
            work_item_ids = set(item.get("work_item_ids") or [])
            dependency_ids = set(item.get("depends_on_work_item_ids") or [])
            if work_item_ids & blocking_dependency_ids or dependency_ids & blocking_dependency_ids:
                item["status"] = "blocked"
                item["error_message"] = "Waiting for blocking user input."
        self._replace_scheduler(
            parent_task,
            queue_items=queue_items,
            scheduler_status="queued",
        )
        parent_task.status = "queued"
        parent_task.started_at = None
        parent_task.completed_at = None
        summary = (
            f"I queued `{plan.summary}` and moved it to Active Workflows. "
            "I’ll keep working in the background and report back here if it finishes or needs you."
        )
        parent_task.output_payload = {
            "plan_id": plan.plan_id,
            "status": "queued",
            "chat_summary": summary,
            "scheduler": dict((parent_task.input_payload or {}).get("scheduler", {})),
        }
        parent_task.error_message = None
        self.session.commit()
        scheduler_service = SchedulerService(self.session)
        scheduler_service.enqueue_maestro_plan(parent_task)
        scheduler_service.sync_run_status_from_task(parent_task)
        self.session.refresh(parent_task)
        return MaestroRun(
            plan=self._plan_from_task(parent_task),
            status="queued",
            parent_task_id=str(parent_task.id),
            child_runs=[],
            synthesis_report_id=None,
            synthesis=summary,
            chat_summary=summary,
            staged_artifact_path=None,
            artifact_id=None,
            scheduler=dict((parent_task.input_payload or {}).get("scheduler", {})),
            execution_stages=plan.execution_stages,
            tool_activity=[],
            error_message=None,
        )

    def approve_tool_call_and_resume(
        self,
        tool_call_id: uuid.UUID | str,
        *,
        execute_llm: bool = True,
        auto_tool_loop: bool = True,
        max_tool_iterations: int = 2,
    ) -> tuple[ToolExecutionResult, MaestroRun | None]:
        result = ToolExecutionService(
            self.session,
            adapters=self.runtime.tool_adapters,
        ).approve_tool_call(tool_call_id)
        tool_call = self.session.get(ToolCall, uuid.UUID(str(tool_call_id)))
        if tool_call is None or tool_call.task_id is None:
            return result, None
        child_task = self.session.get(Task, tool_call.task_id)
        if child_task is None or child_task.parent_task_id is None:
            return result, None
        parent_task = self.session.get(Task, child_task.parent_task_id)
        if parent_task is None or parent_task.workflow_key != "maestro.generic":
            return result, None
        if result.status != "complete":
            return result, None
        if child_task.workflow_key == "scheduler.workflow_item" and result.tool_key == "local.app.deploy_pr":
            from app.maestro.scheduler_worker import SchedulerWorkerService

            SchedulerWorkerService(self.session).complete_approved_delivery(
                task_id=child_task.id,
                delivery_result=tool_result_payload(result),
            )
            return result, None
        run = self.run_plan(
            parent_task.id,
            execute_llm=execute_llm,
            auto_tool_loop=False,
            max_tool_iterations=max_tool_iterations,
            resume=True,
            approved_tool_results_by_child_task={
                str(child_task.id): [tool_result_payload(result)],
            },
        )
        return result, run

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

    def _decompose_request(
        self,
        user_input: str,
        *,
        registry_snapshot: dict[str, Any],
        maestro_context: dict[str, Any],
    ) -> tuple[MaestroPlannerResponse, str]:
        planning_context = {
            "global_context": self.registry.get_global_context().context,
            "registry": registry_snapshot,
            "maestro_context": maestro_context,
            "retrieved_memory": maestro_context.get("sections", {}).get("memory", {}),
            "scheduler": self._scheduler_payload(),
        }
        llm_client = self.planner_llm_client
        planner_mode = "llm"
        try:
            if llm_client is None:
                settings = get_settings()
                if settings.llm_provider == "openrouter" and not settings.openrouter_api_key:
                    raise LLMClientError("OPENROUTER_API_KEY is not configured.")
                if settings.llm_provider == "openai" and not settings.openai_api_key:
                    raise LLMClientError("OPENAI_API_KEY is not configured.")
                llm_client = OpenAILLMClient()
            planner = LLMMaestroPlanner(llm_client)
            response = planner.decompose(
                user_input=user_input,
                planning_context=planning_context,
            )
            self._last_planner_prompt_metrics = planner.last_prompt_metrics
            return (
                response,
                planner_mode,
            )
        except Exception:
            self._last_planner_prompt_metrics = {
                "system_prompt_chars": 0,
                "input_chars": 0,
                "schema_chars": 0,
                "planning_context_chars": len(str(planning_context)),
                "registry_chars": len(str(registry_snapshot)),
                "memory_chars": len(str(planning_context.get("retrieved_memory", {}).get("rendered_text", ""))),
                "maestro_context_chars": len(str(maestro_context.get("rendered_text", ""))),
                "fallback": 1,
            }
            return self._deterministic_decomposition(user_input, registry_snapshot), "deterministic"

    def _planning_context_bundle(self, user_input: str) -> dict[str, Any]:
        bundle = MaestroContextAssembler(self.session).build_bundle(
            query_text=user_input,
            max_chars=6500,
            memory_chars=2500,
            routed_chars=1800,
            report_limit=6,
            run_log_limit=6,
            artifact_limit=6,
        )
        return maestro_context_payload(bundle)

    def _deterministic_decomposition(
        self,
        user_input: str,
        registry_snapshot: dict[str, Any],
    ) -> MaestroPlannerResponse:
        lowered = user_input.lower()
        domain_key = self._domain_for_input(lowered, registry_snapshot)
        work_items: list[PlannerWorkItem] = []
        planning_only = any(
            token in lowered
            for token in ("plan only", "minimal plan", "no code changes", "do not make code")
        )
        is_maestro_ui_change = (
            any(token in lowered for token in ("maestro app", "maestro ui", "frontend", "ui", "button", "css"))
            and any(token in lowered for token in ("change", "update", "edit", "color", "style"))
        )
        coding_request = any(
            token in lowered
            for token in ("implement", "code", "coding", "fix", "action issue", "work issue")
        ) or is_maestro_ui_change
        feature_planning = any(
            token in lowered
            for token in (
                "new feature",
                "feature for maestro",
                "plan a new",
                "design a new",
                "brainstorm a new",
                "interact with",
                "integration",
            )
        ) and not coding_request
        if not planning_only and coding_request:
            work_items.append(
                PlannerWorkItem(
                    id="wi_1",
                    type="workflow_task",
                    title="Execute scoped Maestro coding work",
                    description=user_input,
                    domain_key="maestro-development",
                    priority="high",
                    required_capabilities=["software implementation", "repository editing", "test execution"],
                    required_tools=["codex.task.run"],
                    dependencies=[],
                    needs_agent=True,
                    needs_user_input=False,
                    blocks_execution=False,
                    can_log_directly=False,
                    suggested_agent_keys=["maestro-coding-agent"],
                    expected_output="Codex task result with changed files, validation run, and follow-up risks.",
                    model_tier="auto",
                    model_rationale="Runtime routing will select the coding-appropriate tier.",
                    rationale="The request asks Maestro to execute coding work.",
                )
            )
        if feature_planning:
            work_items.append(
                PlannerWorkItem(
                    id=f"wi_{len(work_items) + 1}",
                    type="workflow_task",
                    title="Draft Maestro feature architecture",
                    description=(
                        "Turn Chris's feature idea into a scoped Maestro architecture concept, "
                        "including user-facing behavior, agent/tool boundaries, permissions, "
                        f"memory/artifact handling, and rollout risks. Feature idea: {user_input}"
                    ),
                    domain_key=domain_key or "maestro-development",
                    priority="high",
                    required_capabilities=[
                        "Maestro architecture",
                        "tool-boundary design",
                        "agent workflow design",
                        "approval and permission design",
                    ],
                    required_tools=["memory.context_bundle"],
                    dependencies=[],
                    needs_agent=True,
                    needs_user_input=False,
                    blocks_execution=False,
                    can_log_directly=False,
                    suggested_agent_keys=["maestro-chief-engineer"],
                    expected_output=(
                        "Conversational feature architecture sketch with scope, user flow, "
                        "agent/tool boundaries, permissions, risks, and recommended next steps."
                    ),
                    model_tier="auto",
                    model_rationale="Runtime routing will select the architecture-appropriate tier.",
                    rationale="Fallback decomposition for a Maestro feature-design request.",
                )
            )
            if any(token in lowered for token in ("google", "drive", "docs", "sheets", "api", "integration")):
                work_items.append(
                    PlannerWorkItem(
                        id=f"wi_{len(work_items) + 1}",
                        type="workflow_task",
                        title="Research Google Workspace integration options",
                        description=(
                            "Research practical Google Drive, Docs, and Sheets integration options "
                            "for Maestro, including APIs, OAuth scopes, document-editing patterns, "
                            f"and safety constraints. Feature idea: {user_input}"
                        ),
                        domain_key=domain_key or "maestro-development",
                        priority="normal",
                        required_capabilities=[
                            "current-state research",
                            "integration research",
                            "API capability analysis",
                        ],
                        required_tools=["web.search"],
                        dependencies=[],
                        needs_agent=True,
                        needs_user_input=False,
                        blocks_execution=False,
                        can_log_directly=False,
                        suggested_agent_keys=["maestro-sota-researcher"],
                    expected_output=(
                        "Research report summarizing Google Workspace API options, auth/scopes, "
                        "document editing approaches, constraints, and links/citations."
                    ),
                    model_tier="auto",
                    model_rationale="Runtime routing will select the research-appropriate tier.",
                    rationale="Google Workspace integration depends on current external API/tool context.",
                    )
                )
        elif any(token in lowered for token in ("plan", "prepare", "coordinate", "workflow")):
            work_items.append(
                PlannerWorkItem(
                    id=f"wi_{len(work_items) + 1}",
                    type="workflow_task",
                    title="Coordinate requested workflow",
                    description=user_input,
                    domain_key=domain_key,
                    priority="high",
                    required_capabilities=self._capabilities_from_text(lowered),
                    required_tools=self._research_tools_from_text(lowered),
                    dependencies=[],
                    needs_agent=True,
                    needs_user_input=False,
                    blocks_execution=False,
                    can_log_directly=False,
                    suggested_agent_keys=[],
                    expected_output="Role-scoped workflow contribution and recommended next steps.",
                    model_tier="auto",
                    model_rationale="Runtime routing will select the appropriate execution tier.",
                    rationale="The request asks Maestro to prepare or coordinate work.",
                )
            )
            if any(token in lowered for token in ("email", "message", "follow-up", "follow up", "outreach")):
                work_items.append(
                    PlannerWorkItem(
                        id=f"wi_{len(work_items) + 1}",
                        type="workflow_task",
                        title="Draft role-scoped follow-up communication",
                        description=(
                            "Prepare the communication-specific portion of the request, focusing "
                            f"only on the email, partner message, or follow-up draft needed for: {user_input}"
                        ),
                        domain_key=domain_key,
                        priority="high",
                        required_capabilities=[
                            "email triage",
                            "partner communications",
                            "follow-up drafting",
                        ],
                        required_tools=[],
                        dependencies=[],
                        needs_agent=True,
                        needs_user_input=False,
                        blocks_execution=False,
                        can_log_directly=False,
                        suggested_agent_keys=self._agent_keys_matching(
                            registry_snapshot,
                            domain_key=domain_key,
                            include_any=("email", "message", "communication", "follow-up", "follow up", "outreach"),
                            exclude_any=("finance", "budget", "invoice"),
                        ),
                        expected_output=(
                            "Communication-focused contribution with draft language, assumptions, "
                            "and any missing recipient/context questions."
                        ),
                        model_tier="auto",
                        model_rationale="Runtime routing will select the drafting-appropriate tier.",
                        rationale="The request contains an email or follow-up drafting lane.",
                    )
                )
        if (
            any(token in lowered for token in ("email", "gmail", "inbox", "message"))
            and any(token in lowered for token in ("review", "triage", "latest", "new", "extract", "route", "classify"))
        ):
            work_items.append(
                PlannerWorkItem(
                    id=f"wi_{len(work_items) + 1}",
                    type="workflow_task",
                    title="Triage latest domain email",
                    description=user_input,
                    domain_key=domain_key,
                    priority="normal",
                    required_capabilities=[
                        "email triage",
                        "routed item extraction",
                        "contact extraction",
                        "calendar extraction",
                        "task extraction",
                    ],
                    required_tools=[
                        "gmail.message.list_recent",
                        "gmail.message.get",
                        "routed.item.create",
                    ],
                    dependencies=[],
                    needs_agent=True,
                    needs_user_input=False,
                    blocks_execution=False,
                    can_log_directly=False,
                    suggested_agent_keys=self._agent_keys_matching(
                        registry_snapshot,
                        domain_key=domain_key,
                        include_any=("email", "gmail", "inbox", "triage"),
                    ),
                    expected_output=(
                        "Email classification, notification recommendation, routed candidates "
                        "created with provenance, and concise run report."
                    ),
                    model_tier="auto",
                    model_rationale="Runtime routing will select the routine email-triage tier.",
                    rationale="The request requires fetching email before routed items can be created.",
                )
            )
        if (
            not work_items
            and self._schedule_trigger_type(lowered) is not None
            and any(token in lowered for token in ("review", "identify", "extract", "route", "notify", "propose"))
        ):
            work_items.append(
                PlannerWorkItem(
                    id="wi_1",
                    type="workflow_task",
                    title="Configure scheduled Maestro workflow",
                    description=user_input,
                    domain_key=domain_key,
                    priority="normal",
                    required_capabilities=self._capabilities_from_text(lowered),
                    required_tools=self._tools_from_scheduled_text(lowered),
                    dependencies=[],
                    needs_agent=True,
                    needs_user_input=False,
                    blocks_execution=False,
                    can_log_directly=False,
                    suggested_agent_keys=[],
                    expected_output="Recurring or event-triggered workflow ready for Maestro scheduling.",
                    model_tier="auto",
                    model_rationale="Runtime routing will select the appropriate scheduling tier.",
                    rationale="The request asks Maestro to create work that should run on a trigger.",
                )
            )
        if any(token in lowered for token in ("task", "todo", "due", "follow up", "follow-up")):
            work_items.append(
                PlannerWorkItem(
                    id=f"wi_{len(work_items) + 1}",
                    type="standalone_task",
                    title="Capture follow-up task candidate",
                    description=user_input,
                    domain_key=domain_key,
                    priority="normal",
                    required_capabilities=["task extraction", "follow-up planning"],
                    required_tools=[],
                    dependencies=[],
                    needs_agent=False,
                    needs_user_input=False,
                    blocks_execution=False,
                    can_log_directly=True,
                    suggested_agent_keys=[],
                    expected_output="Task candidate routed for review.",
                    model_tier="auto",
                    model_rationale="This routed item does not require agent execution.",
                    rationale="The request contains task or follow-up language.",
                )
            )
        if any(token in lowered for token in ("contact", "lead", "partner", "crm")):
            work_items.append(
                PlannerWorkItem(
                    id=f"wi_{len(work_items) + 1}",
                    type="contact",
                    title="Capture relationship/contact context",
                    description=user_input,
                    domain_key=domain_key,
                    priority="normal",
                    required_capabilities=["relationship management", "CRM context"],
                    required_tools=[],
                    dependencies=[],
                    needs_agent=False,
                    needs_user_input=False,
                    blocks_execution=False,
                    can_log_directly=True,
                    suggested_agent_keys=[],
                    expected_output="Contact or relationship candidate routed for review.",
                    model_tier="auto",
                    model_rationale="This routed item does not require agent execution.",
                    rationale="The request mentions partner/contact relationship context.",
                )
            )
        if any(token in lowered for token in ("event", "calendar", "meeting", "call", "sync")):
            work_items.append(
                PlannerWorkItem(
                    id=f"wi_{len(work_items) + 1}",
                    type="event",
                    title="Capture event/calendar context",
                    description=user_input,
                    domain_key=domain_key,
                    priority="normal",
                    required_capabilities=["calendar reasoning", "schedule extraction"],
                    required_tools=[],
                    dependencies=[],
                    needs_agent=False,
                    needs_user_input=False,
                    blocks_execution=False,
                    can_log_directly=True,
                    suggested_agent_keys=[],
                    expected_output="Event candidate routed for review.",
                    model_tier="auto",
                    model_rationale="This routed item does not require agent execution.",
                    rationale="The request mentions a time-bound meeting or call.",
                )
            )
        if any(token in lowered for token in ("?", "confirm", "rfi", "question")):
            work_items.append(
                PlannerWorkItem(
                    id=f"wi_{len(work_items) + 1}",
                    type="rfi",
                    title="Surface missing user input",
                    description=user_input,
                    domain_key=domain_key,
                    priority="normal",
                    required_capabilities=[],
                    required_tools=[],
                    dependencies=[],
                    needs_agent=False,
                    needs_user_input=True,
                    blocks_execution=True,
                    can_log_directly=True,
                    suggested_agent_keys=[],
                    expected_output="RFI routed for your answer.",
                    model_tier="auto",
                    model_rationale="This routed item does not require agent execution.",
                    rationale="The request asks or implies a question.",
                )
            )
        if not work_items:
            work_items.append(
                PlannerWorkItem(
                    id="wi_1",
                    type="direct_response",
                    title="Direct response",
                    description=user_input,
                    domain_key=domain_key,
                    priority="normal",
                    required_capabilities=[],
                    required_tools=[],
                    dependencies=[],
                    needs_agent=False,
                    needs_user_input=False,
                    blocks_execution=False,
                    can_log_directly=False,
                    suggested_agent_keys=[],
                    expected_output="Direct Maestro response.",
                    model_tier="auto",
                    model_rationale="Direct chat does not require agent execution.",
                    rationale="No workflow or routed operational item was detected.",
                )
            )
        return MaestroPlannerResponse(
            plan_summary=f"Proposed decomposition with {len(work_items)} work item(s): {user_input[:180]}",
            direct_response=(
                self._deterministic_direct_response(user_input, work_items)
            ),
            work_items=work_items,
            planner_notes="Deterministic fallback planner used because the LLM planner was unavailable.",
        )

    def _deterministic_direct_response(
        self,
        user_input: str,
        work_items: list[PlannerWorkItem],
    ) -> str:
        if any(item.needs_agent for item in work_items):
            workflow_items = [item for item in work_items if item.needs_agent]
            titles = ", ".join(item.title for item in workflow_items[:3])
            if any("feature" in f"{item.title} {item.description}".lower() for item in workflow_items):
                return (
                    "I can help you think this through here first. I’ll treat this as a "
                    "feature-design conversation, not an implementation request. I prepared a "
                    "focused plan that separates the architecture/design work from any supporting "
                    f"research: {titles}. Once you review it, we can either run the research/design "
                    "agents or keep brainstorming here before tasking anyone."
                )
            return (
                f"I prepared a focused plan for this with {len(workflow_items)} executable work "
                f"item{'' if len(workflow_items) == 1 else 's'}: {titles}. Review it here and I’ll "
                "run it only after you approve."
            )
        if any(item.type == "direct_response" for item in work_items):
            lowered = user_input.lower()
            if any(token in lowered for token in ("brainstorm", "think through", "design", "idea")):
                return (
                    "I can help you think this through here first. I will keep this as a "
                    "conversation until we decide there is concrete agent work, routing, or an "
                    "issue to create."
                )
            if "?" in user_input:
                return (
                    "I can answer this directly here. If we need codebase context, web research, "
                    "or another agent's help, I will turn that into a proposed workflow first."
                )
            return (
                "I can handle this directly here. If it turns into concrete work, I will propose "
                "a plan before tasking agents."
            )
        return ""

    def _domain_for_input(self, lowered_input: str, registry_snapshot: dict[str, Any]) -> str | None:
        for domain in registry_snapshot.get("domains", []):
            if domain_matches(lowered_input, domain):
                return str(domain.get("key") or "")
        return None

    def _agent_keys_matching(
        self,
        registry_snapshot: dict[str, Any],
        *,
        domain_key: str | None,
        include_any: tuple[str, ...],
        exclude_any: tuple[str, ...] = (),
        limit: int = 1,
    ) -> list[str]:
        matches: list[tuple[int, str]] = []
        for agent in registry_snapshot.get("agents", []):
            if domain_key and agent.get("domain_key") != domain_key:
                continue
            text = " ".join(
                str(value or "")
                for value in (
                    agent.get("key"),
                    agent.get("name"),
                    agent.get("role_summary"),
                )
            ).lower()
            if exclude_any and any(token in text for token in exclude_any):
                continue
            score = sum(1 for token in include_any if token in text)
            if score:
                matches.append((score, str(agent.get("key") or "")))
        matches.sort(key=lambda pair: (-pair[0], pair[1]))
        return [key for _, key in matches[:limit] if key]

    def _capabilities_from_text(self, lowered_input: str) -> list[str]:
        capabilities: list[str] = ["planning"]
        if any(token in lowered_input for token in ("partner", "crm", "contact", "lead")):
            capabilities.append("relationship management")
        if any(token in lowered_input for token in ("email", "message", "follow-up", "follow up")):
            capabilities.append("communications")
        if any(token in lowered_input for token in ("technical", "architecture", "build")):
            capabilities.append("technical planning")
        if any(token in lowered_input for token in ("research", "market", "competitor")):
            capabilities.append("research")
        return capabilities

    def _work_item_from_planner(self, item: PlannerWorkItem) -> MaestroWorkItem:
        payload = item.model_dump()
        for key in ("title", "description", "rationale", "expected_output"):
            if isinstance(payload.get(key), str):
                payload[key] = _strip_hidden_context(payload[key])
        payload["required_skills"] = self._skills_for_work_item_payload(payload)
        model_selection = self._model_selection_for_work_item_payload(payload)
        payload.update(model_selection)
        return MaestroWorkItem(**payload)

    def _harden_work_items(
        self,
        work_items: list[MaestroWorkItem],
        *,
        user_input: str = "",
    ) -> list[MaestroWorkItem]:
        work_items = [
            item
            for item in work_items
            if not self._is_deferred_tool_approval_rfi(item)
        ]
        work_items = [
            replace(
                item,
                type="think_tank",
                can_log_directly=True,
                rationale=(
                    item.rationale
                    + " Hardened by Maestro: this is an immature idea or feature concept, "
                    "so it belongs in Think Tank rather than durable RAG memory."
                ),
            )
            if self._is_think_tank_candidate(item)
            else replace(
                item,
                type="contact",
                can_log_directly=True,
                rationale=(
                    item.rationale
                    + " Hardened by Maestro: this is person-specific context or a preference, "
                    "so it belongs with the relevant contact record."
                ),
            )
            if self._is_contact_context_candidate(item)
            else item
            for item in work_items
        ]
        has_agent_work = any(item.needs_agent for item in work_items)
        if has_agent_work:
            work_items = [
                item
                for item in work_items
                if item.type != "standalone_task" or self._is_user_reminder_candidate(item)
            ]
        intake_rfis = [
            item
            for item in work_items
            if item.type == "rfi"
            and item.needs_user_input
            and any(
                token in f"{item.title} {item.description}".lower()
                for token in (
                    "attendee",
                    "attendees",
                    "contact",
                    "who",
                    "person",
                    "end user",
                    "calendar",
                    "meeting details",
                    "demo details",
                    "missing",
                )
            )
        ]
        if not intake_rfis:
            return work_items
        intake_ids = [item.id for item in intake_rfis]
        hardened: list[MaestroWorkItem] = []
        for item in work_items:
            if item.id in intake_ids:
                hardened.append(
                    replace(
                        item,
                        blocks_execution=True,
                        rationale=(
                            item.rationale
                            + " Hardened by Maestro: this missing user/context detail blocks "
                            "dependent CRM/calendar/person-specific work."
                        ),
                    )
                )
                continue
            text = " ".join(
                [
                    item.title,
                    item.description,
                    item.expected_output,
                    " ".join(item.required_capabilities),
                ]
            ).lower()
            needs_intake = any(
                token in text
                for token in (
                    "crm",
                    "contact",
                    "calendar",
                    "attendee",
                    "attendees",
                    "person",
                    "end user",
                    "organization",
                    "meeting invite",
                )
            )
            if item.needs_agent and needs_intake:
                dependencies = list(dict.fromkeys([*item.dependencies, *intake_ids]))
                hardened.append(replace(item, dependencies=dependencies))
            else:
                hardened.append(item)
        return hardened

    def _is_deferred_tool_approval_rfi(
        self,
        item: MaestroWorkItem,
    ) -> bool:
        if item.type != "rfi" or not item.needs_user_input:
            return False
        item_text = f"{item.title} {item.description} {item.expected_output}".lower()
        approval_context = any(
            token in item_text
            for token in ("approve", "approval", "review the pr", "review pull request")
        ) and any(token in item_text for token in ("pull request", " pr", "merge", "deploy"))
        future_checkpoint = any(
            token in item_text
            for token in (
                "after the pull request",
                "once the pull request",
                "when the pull request",
                "after the pr",
                "once the pr",
                "when the pr",
                "pr is ready",
                "pull request is ready",
                "unseen pr",
            )
        )
        return approval_context and future_checkpoint

    def _skills_for_work_item_payload(self, payload: dict[str, Any]) -> list[str]:
        text = " ".join(
            str(payload.get(key) or "")
            for key in ("type", "title", "description", "expected_output", "rationale")
        ).lower()
        text = " ".join(
            [
                text,
                " ".join(str(value).lower() for value in payload.get("required_capabilities") or []),
                " ".join(str(value).lower() for value in payload.get("required_tools") or []),
            ]
        )
        if (
            payload.get("type") == "workflow_task"
            and str(payload.get("title") or "").lower() == "coordinate requested workflow"
        ):
            return []
        skills: list[str] = []
        if any(token in text for token in ("email", "gmail", "inbox", "message", "triage")):
            skills.append("email_triage")
        if payload.get("type") == "contact" or any(token in text for token in ("contact", "crm", "person", "partner lead")):
            skills.append("contact_manager")
        if payload.get("type") in {"standalone_task", "workflow_task"} and any(
            token in text for token in ("todo", "to do", "task", "due-out", "due out", "follow-up", "follow up")
        ):
            skills.append("to_do_manager")
        if payload.get("type") == "event" or any(token in text for token in ("calendar", "event", "meeting", "call", "sync")):
            skills.append("calendar_manager")
        if any(token in text for token in ("organization", "company", "vendor", "agency", "unit", "partner organization")):
            skills.append("organization_manager")
        return list(dict.fromkeys(skills))

    def _model_selection_for_work_item_payload(self, payload: dict[str, Any]) -> dict[str, str]:
        requested_tier = str(payload.get("model_tier") or "auto").strip().lower()
        requested_tier = {
            "local_routine": "qwen",
            "cloud_standard": "terra",
            "cloud_advanced": "sol",
        }.get(requested_tier, requested_tier)
        text = " ".join(
            [
                str(payload.get("type") or ""),
                str(payload.get("title") or ""),
                str(payload.get("description") or ""),
                " ".join(str(value) for value in payload.get("required_tools") or []),
            ]
        ).lower()
        if requested_tier not in {"qwen", "luna", "terra", "sol"}:
            if any(
                token in text
                for token in (
                    "codex.task.run",
                    "web.search",
                    "sota",
                    "research",
                    "architecture",
                    "strategy",
                    "brainstorm",
                    "design",
                    "complex",
                )
            ):
                requested_tier = "sol"
            elif payload.get("can_log_directly") or any(
                token in text
                for token in ("email", "gmail", "inbox", "extract", "route", "contact", "calendar", "todo")
            ):
                requested_tier = "qwen"
            else:
                requested_tier = "terra"

        settings = get_settings()
        profile_by_tier = {
            "qwen": settings.llm_qwen_model_profile,
            "luna": settings.llm_luna_model_profile,
            "terra": settings.llm_terra_model_profile,
            "sol": settings.llm_sol_model_profile,
        }
        fallback_rationales = {
            "qwen": "Routine, bounded extraction or routing work is suitable for the local Qwen tier.",
            "luna": "This straightforward task benefits from the fast, cost-efficient cloud tier.",
            "terra": "This task benefits from balanced cloud reasoning and drafting without needing the flagship tier.",
            "sol": "This task needs the strongest reasoning tier for ambiguity, synthesis, research, design, or strategy.",
        }
        planner_rationale = str(payload.get("model_rationale") or "").strip()
        return {
            "model_tier": requested_tier,
            "model_profile": profile_by_tier[requested_tier].strip() or "default",
            "model_rationale": planner_rationale or fallback_rationales[requested_tier],
        }

    def _model_profile_for_work_item_payload(self, payload: dict[str, Any]) -> str | None:
        """Compatibility shim for callers that only need the resolved runtime profile."""
        return self._model_selection_for_work_item_payload(payload)["model_profile"]

    def _is_think_tank_candidate(self, item: MaestroWorkItem) -> bool:
        if item.type != "memory_candidate":
            return False
        text = " ".join(
            [
                item.title,
                item.description,
                item.expected_output,
                item.rationale,
            ]
        ).lower()
        idea_markers = (
            "idea",
            "concept",
            "brainstorm",
            "feature",
            "possible",
            "proposal",
            "prototype",
            "explore",
            "future",
            "think tank",
        )
        return any(marker in text for marker in idea_markers)

    def _is_contact_context_candidate(self, item: MaestroWorkItem) -> bool:
        if item.type != "memory_candidate":
            return False
        text = " ".join([item.title, item.description, item.expected_output, item.rationale])
        lowered = text.lower()
        contact_markers = (
            "prefers",
            "preference",
            "likes",
            "dislikes",
            "partner lead",
            "contact",
            "email",
            "phone",
            "works at",
            "works with",
        )
        if not any(marker in lowered for marker in contact_markers):
            return False
        return bool(re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", text))

    def _is_user_reminder_candidate(self, item: MaestroWorkItem) -> bool:
        if item.type != "standalone_task":
            return False
        text = f"{item.title} {item.description} {item.rationale}".lower()
        user_markers = (
            "remind me",
            "reminder for me",
            "i need to",
            "i should",
            "my todo",
            "my to do",
            "for chris to",
            "chris needs to",
            "due-out for me",
            "due out for me",
        )
        return any(marker in text for marker in user_markers)

    def _select_agents_for_work_items(
        self,
        work_items: list[MaestroWorkItem],
        agents,
    ) -> list:
        selected_by_key: dict[str, Any] = {}
        for item in work_items:
            if not item.needs_agent:
                continue
            candidates = [
                agent
                for agent in agents
                if item.domain_key is None or agent.domain_key == item.domain_key
            ]
            if not candidates:
                candidates = list(agents)
            scored = [
                (self._agent_work_item_score(item, agent), agent)
                for agent in candidates
            ]
            scored = [(score, agent) for score, agent in scored if score > 0]
            if not scored:
                continue
            scored.sort(key=lambda pair: (-pair[0], pair[1].domain_key, pair[1].key))
            top_score = scored[0][0]
            threshold = max(2.0, top_score * 0.5)
            for score, agent in scored:
                if score >= threshold and (top_score <= 3.5 or score > 3.0):
                    selected_by_key[agent.key] = agent
            if not selected_by_key.get(scored[0][1].key):
                selected_by_key[scored[0][1].key] = scored[0][1]
        if not selected_by_key:
            selected = self._select_agents(" ".join(item.description for item in work_items), agents)
            selected_by_key = {agent.key: agent for agent in selected}
        return sorted(selected_by_key.values(), key=lambda agent: (agent.domain_key, agent.key))[:6]

    def _agent_work_item_score(self, item: MaestroWorkItem, agent) -> float:
        score = 0.0
        if item.domain_key and agent.domain_key == item.domain_key:
            score += 3.0
        if agent.key in item.suggested_agent_keys:
            score += 5.0
        item_text = " ".join(
            [
                item.title,
                item.description,
                item.expected_output,
                " ".join(item.required_capabilities),
                " ".join(item.required_tools),
                " ".join(item.required_skills),
            ]
        ).lower()
        agent_text = " ".join(
            [
                agent.key,
                agent.name,
                agent.role_summary,
                agent.current_action or "",
                " ".join(tool.key for tool in agent.allowed_tools),
                " ".join(tool.name for tool in agent.allowed_tools),
                " ".join(skill.key for skill in agent.allowed_skills),
                " ".join(skill.name for skill in agent.allowed_skills),
            ]
        ).lower()
        if "coding" in agent.key and not (
            set(item.required_tools) & {"codex.task.run"}
            or any(
                token in item_text
                for token in (
                    "code",
                    "coding",
                    "implement",
                    "implementation",
                    "repository editing",
                    "test execution",
                    "codex",
                )
            )
        ):
            return 0.0
        domain_noise = meaningful_tokens(
            " ".join([agent.domain_key, agent.domain_key.replace("-", " "), "agent"])
        )
        overlap = (meaningful_tokens(item_text) - domain_noise) & (
            meaningful_tokens(agent_text) - domain_noise
        )
        score += min(len(overlap), 8) * 0.75
        agent_tool_keys = {tool.key for tool in agent.allowed_tools}
        score += len(agent_tool_keys & set(item.required_tools)) * 1.5
        agent_skill_keys = {skill.key for skill in agent.allowed_skills}
        score += len(agent_skill_keys & set(item.required_skills)) * 1.25
        if any(token in agent.key for token in ("planning", "chief", "manager", "lead")):
            score += 0.5
        return score

    def _agent_relevance_score(self, lowered_input: str, agent) -> tuple[float, str]:
        score = 0.0
        rationale: list[str] = []
        domain_tokens = [agent.domain_key, agent.domain_key.replace("-", " ")]
        domain_tokens.extend(DOMAIN_HINTS.get(agent.domain_key, []))
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
        input_tokens = meaningful_tokens(lowered_input)
        agent_tokens = meaningful_tokens(agent_text.lower())
        domain_noise = meaningful_tokens(
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
        work_items: list[MaestroWorkItem],
    ) -> list[MaestroSubtask]:
        subtasks: list[MaestroSubtask] = []
        for agent in selected_agents:
            assigned_items = self._work_items_for_agent(agent, work_items)
            if not assigned_items:
                continue
            assigned_item_ids = {item.id for item in assigned_items}
            prior_agent_work_item_ids: list[str] = []
            remaining_items = list(assigned_items)
            while remaining_items:
                ready_items = [
                    item
                    for item in remaining_items
                    if set(item.dependencies or []) & assigned_item_ids <= set(prior_agent_work_item_ids)
                ]
                if not ready_items:
                    ready_items = [remaining_items[0]]
                grouped_items: dict[tuple[tuple[str, ...], str], list[MaestroWorkItem]] = {}
                for item in ready_items:
                    dependencies = tuple(
                        sorted(
                            dependency
                            for dependency in item.dependencies
                            if dependency not in assigned_item_ids
                        )
                    )
                    model_profile = item.model_profile or "default"
                    grouped_items.setdefault((dependencies, model_profile), []).append(item)
                (dependencies, _model_profile), group_items = sorted(
                    grouped_items.items(),
                    key=lambda pair: (len(pair[0][0]), pair[0]),
                )[0]
                priority = "high" if any(item.priority in {"high", "urgent"} for item in group_items) else "normal"
                effective_dependencies = list(dict.fromkeys([*dependencies, *prior_agent_work_item_ids]))
                required_skills = list(
                    dict.fromkeys(
                        skill
                        for item in group_items
                        for skill in (item.required_skills or [])
                    )
                )
                model_profile = next((item.model_profile for item in group_items if item.model_profile), None)
                model_tier = next((item.model_tier for item in group_items if item.model_tier), "auto")
                model_rationale = next(
                    (item.model_rationale for item in group_items if item.model_rationale),
                    None,
                )
                subtasks.append(
                    MaestroSubtask(
                        agent_key=agent.key,
                        agent_name=agent.name,
                        domain_key=agent.domain_key,
                        objective=self._subtask_objective(user_input, agent, intents, group_items),
                        expected_output=self._expected_output_for_agent(agent, intents, group_items),
                        priority=priority,
                        rationale=self._subtask_rationale_for_items(agent, group_items),
                        work_item_ids=[item.id for item in group_items],
                        depends_on_work_item_ids=effective_dependencies,
                        required_skills=required_skills,
                        model_profile=model_profile,
                        model_tier=model_tier,
                        model_rationale=model_rationale,
                    )
                )
                prior_agent_work_item_ids.extend(item.id for item in group_items)
                for item in group_items:
                    if item in remaining_items:
                        remaining_items.remove(item)
        return subtasks

    def _work_items_for_agent(self, agent, work_items: list[MaestroWorkItem]) -> list[MaestroWorkItem]:
        assigned: list[MaestroWorkItem] = []
        specs = self.registry.list_specs()
        active_agent_keys = {spec.key for spec in specs}
        for item in work_items:
            if not item.needs_agent:
                continue
            if item.domain_key is not None and item.domain_key != agent.domain_key:
                continue
            valid_suggested_keys = [
                key for key in item.suggested_agent_keys if key in active_agent_keys
            ]
            if valid_suggested_keys:
                if agent.key == valid_suggested_keys[0]:
                    assigned.append(item)
                continue
            score = self._agent_work_item_score(item, agent)
            candidates = sorted(
                [
                    (self._agent_work_item_score(item, candidate), candidate.key)
                    for candidate in specs
                    if item.domain_key is None or candidate.domain_key == item.domain_key
                ],
                key=lambda pair: (-pair[0], pair[1]),
            )
            best_score, best_agent_key = candidates[0] if candidates else (score, agent.key)
            if item.required_tools:
                if score > 0 and agent.key == best_agent_key and best_score >= 2.0:
                    assigned.append(item)
                continue
            if not self._allows_multi_agent_assignment(item):
                if score > 0 and agent.key == best_agent_key and best_score >= 2.0:
                    assigned.append(item)
                continue
            top_score = best_score
            if score > 0 and score >= max(2.0, top_score * 0.5) and (
                top_score <= 3.5 or score > 3.0
            ):
                assigned.append(item)
        return assigned

    def _allows_multi_agent_assignment(self, item: MaestroWorkItem) -> bool:
        text = " ".join(
            [
                item.title,
                item.description,
                item.expected_output,
                item.rationale,
                " ".join(item.required_capabilities),
            ]
        ).lower()
        return any(
            token in text
            for token in (
                "cross-domain",
                "multi-agent",
                "parallel lanes",
                "synthesis from multiple",
                "all relevant agents",
            )
        )

    def _intents_from_work_items(
        self,
        work_items: list[MaestroWorkItem],
        selected_agents,
    ) -> list[MaestroIntent]:
        default_domain = selected_agents[0].domain_key if selected_agents else None
        intent_by_type: dict[str, MaestroIntent] = {}
        for item in work_items:
            intent_type = intent_type_for_work_item(item.type)
            existing = intent_by_type.get(intent_type)
            if existing is not None:
                continue
            intent_by_type[intent_type] = MaestroIntent(
                type=intent_type,  # type: ignore[arg-type]
                summary=f"{item.type}: {item.title}",
                target=item.description[:180],
                domain_key=item.domain_key or default_domain,
                priority=item.priority,
                action=action_for_work_item(item.type, item.rationale),
            )
        if not intent_by_type:
            return [
                MaestroIntent(
                    type="direct_chat",
                    summary="Respond directly unless the user approves further work.",
                    target="",
                    domain_key=default_domain,
                    action="Prepare a concise direct answer and avoid unnecessary delegation.",
                )
            ]
        return list(intent_by_type.values())

    def _subtask_objective(
        self,
        user_input: str,
        agent,
        intents: list[MaestroIntent],
        work_items: list[MaestroWorkItem],
    ) -> str:
        intent_list = ", ".join(intent.type for intent in intents)
        intent_actions = "\n".join(
            f"- {intent.type}: {intent.action or intent.summary}" for intent in intents
        )
        role_summary = agent.role_summary or "No role summary configured."
        tool_list = ", ".join(tool.key for tool in agent.allowed_tools) or "no tools configured"
        work_item_text = "\n\n".join(
            (
                f"Work item {item.id}: {item.title}\n"
                f"Type: {item.type}\n"
                f"Description: {item.description}\n"
                f"Required capabilities: {', '.join(item.required_capabilities) or 'none'}\n"
                f"Required tools: {', '.join(item.required_tools) or 'none'}\n"
                f"Expected output: {item.expected_output}\n"
                f"Dependencies: {', '.join(item.dependencies) or 'none'}"
            )
            for item in work_items
        )
        return (
            f"You are {agent.name}. Work only within the {agent.domain_key} domain and only on "
            "the portion of this Maestro request that fits your specialty.\n\n"
            f"Your specialty: {role_summary}\n"
            f"Authorized tools: {tool_list}\n"
            f"Detected planning lanes: {intent_list}\n"
            f"Lane actions:\n{intent_actions}\n\n"
            f"Assigned decomposed work items:\n{work_item_text}\n\n"
            "Do not answer for sister agents. Produce your domain contribution, note assumptions, "
            "surface RFIs, and call out any tasks/events/contacts/decisions that Maestro should "
            "route separately."
        )

    def _expected_output_for_agent(
        self,
        agent,
        intents: list[MaestroIntent],
        work_items: list[MaestroWorkItem],
    ) -> str:
        item_outputs = [item.expected_output for item in work_items if item.expected_output]
        if item_outputs:
            return " | ".join(item_outputs)
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

    def _subtask_rationale_for_items(
        self,
        agent,
        work_items: list[MaestroWorkItem],
    ) -> str:
        parts = [
            f"{item.id}: score {self._agent_work_item_score(item, agent):.2f} for {item.title}"
            for item in work_items
        ]
        return "Assigned decomposed work items based on role/tool fit. " + " | ".join(parts)

    def _execution_stages(self, subtasks: list[MaestroSubtask]) -> list[list[MaestroSubtask]]:
        remaining = list(subtasks)
        completed_work_items: set[str] = set()
        stages: list[list[MaestroSubtask]] = []
        while remaining:
            ready = [
                subtask
                for subtask in remaining
                if set(subtask.depends_on_work_item_ids or []).issubset(completed_work_items)
            ]
            if not ready:
                stages.extend([[subtask] for subtask in remaining])
                break
            stages.append(ready)
            for subtask in ready:
                remaining.remove(subtask)
                completed_work_items.update(subtask.work_item_ids or [])
        return stages

    def _execution_stage_keys(self, subtasks: list[MaestroSubtask]) -> list[list[str]]:
        return [[subtask.agent_key for subtask in stage] for stage in self._execution_stages(subtasks)]

    def _blocked_work_item_ids(self, work_items: list[MaestroWorkItem]) -> set[str]:
        blocked = {
            item.id for item in work_items if item.needs_user_input and item.blocks_execution
        }
        changed = True
        while changed:
            changed = False
            for item in work_items:
                if item.id not in blocked and set(item.dependencies) & blocked:
                    blocked.add(item.id)
                    changed = True
        return blocked

    def _workflow_graph(
        self,
        work_items: list[MaestroWorkItem],
        subtasks: list[MaestroSubtask],
    ) -> dict[str, Any]:
        agent_keys_by_work_item: dict[str, list[str]] = {}
        for subtask in subtasks:
            for work_item_id in subtask.work_item_ids or []:
                agent_keys_by_work_item.setdefault(work_item_id, []).append(subtask.agent_key)
        stages = []
        for index, stage in enumerate(self._execution_stages(subtasks), start=1):
            stages.append(
                {
                    "index": index,
                    "agent_keys": [subtask.agent_key for subtask in stage],
                    "work_item_ids": [
                        work_item_id
                        for subtask in stage
                        for work_item_id in (subtask.work_item_ids or [])
                    ],
                    "waits_for_work_item_ids": sorted(
                        {
                            dependency
                            for subtask in stage
                            for dependency in (subtask.depends_on_work_item_ids or [])
                        }
                    ),
                }
            )
        return {
            "nodes": [
                {
                    "id": item.id,
                    "type": item.type,
                    "title": item.title,
                    "domain_key": item.domain_key,
                    "priority": item.priority,
                    "needs_agent": item.needs_agent,
                    "can_log_directly": item.can_log_directly,
                    "agent_keys": sorted(agent_keys_by_work_item.get(item.id, [])),
                }
                for item in work_items
            ],
            "edges": [
                {
                    "from_work_item_id": dependency,
                    "to_work_item_id": item.id,
                    "relation": "must_complete_before",
                }
                for item in work_items
                for dependency in item.dependencies
            ],
            "stages": stages,
        }

    def _is_chat_only(
        self,
        work_items: list[MaestroWorkItem],
        decomposition: MaestroPlannerResponse,
    ) -> bool:
        return bool(decomposition.direct_response) and not any(
            item.needs_agent or item.can_log_directly or item.needs_user_input
            for item in work_items
        )

    def _is_routing_only(self, work_items: list[MaestroWorkItem]) -> bool:
        return bool(work_items) and not any(item.needs_agent for item in work_items) and any(
            route_type_for_work_item(item.type) is not None for item in work_items
        )

    def _routed_direct_response(self, work_items: list[MaestroWorkItem]) -> str:
        routed = [
            item
            for item in work_items
            if route_type_for_work_item(item.type) is not None
        ]
        if not routed:
            return "I captured that context."
        blocking_rfis = [
            item for item in routed if item.type == "rfi" and item.needs_user_input
        ]
        if blocking_rfis and len(routed) == 1:
            item = blocking_rfis[0]
            detail = item.description.strip() if item.description else item.title
            return f"I need one bit of context before I can answer that well: {detail}"
        think_tank_items = [item for item in routed if item.type == "think_tank"]
        if think_tank_items and len(routed) == len(think_tank_items):
            if len(think_tank_items) == 1:
                item = think_tank_items[0]
                return (
                    f'I saved this in Think Tank as "{item.title}". We can keep '
                    "brainstorming here, and when it matures I can turn it into a workflow, "
                    "GitHub issue, or durable memory."
                )
            return (
                f"I saved {len(think_tank_items)} ideas in Think Tank. We can keep "
                "working them here and promote the useful ones when they get sharper."
            )
        route_counts: dict[str, int] = {}
        for item in routed:
            route_type = route_type_for_work_item(item.type) or "item"
            route_counts[route_type] = route_counts.get(route_type, 0) + 1
        summary = ", ".join(
            f"{count} {route_type.replace('_', ' ')}{'' if count == 1 else 's'}"
            for route_type, count in sorted(route_counts.items())
        )
        examples = "; ".join(item.title for item in routed[:3])
        return (
            f"I routed {summary} into the right store with provenance. "
            f"Top item{'s' if len(routed[:3]) != 1 else ''}: {examples}."
        )

    def _dependency_context(
        self,
        completed_outputs_by_work_item: dict[str, str],
        subtask: MaestroSubtask,
    ) -> str | None:
        dependencies = set(subtask.depends_on_work_item_ids or [])
        if not dependencies:
            return None
        matching_outputs = [
            (work_item_id, completed_outputs_by_work_item[work_item_id])
            for work_item_id in sorted(dependencies)
            if work_item_id in completed_outputs_by_work_item
        ]
        if not matching_outputs:
            return None
        return "\n\n".join(
            f"Upstream output for {work_item_id}:\n{output}"
            for work_item_id, output in matching_outputs
        )

    def _child_user_context(self, plan: MaestroPlan, dependency_context: str | None) -> str:
        base = f"Original Maestro request:\n{plan.user_input}"
        if dependency_context:
            return f"{base}\n\nDependency context:\n{dependency_context}"
        return base

    def _run_subtask_with_retries(
        self,
        parent_task: Task,
        plan: MaestroPlan,
        subtask: MaestroSubtask,
        completed_outputs_by_work_item: dict[str, str],
        *,
        execute_llm: bool,
        auto_tool_loop: bool,
        max_tool_iterations: int,
        initial_tool_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        max_attempts = 2
        attempts: list[AgentRunResult] = []
        final_run: AgentRunResult | None = None
        for attempt in range(1, max_attempts + 1):
            dependency_context = self._dependency_context(
                completed_outputs_by_work_item,
                subtask,
            )
            run = self.runtime.run_agent_once(
                PromptPackageRequest(
                    agent_key=subtask.agent_key,
                    task_instruction=subtask.objective,
                    caller="maestro",
                    user_context=self._child_user_context(plan, dependency_context),
                    query_text=plan.user_input,
                    use_semantic=True,
                ),
                stage_interaction=False,
                execute_llm=execute_llm,
                initial_tool_results=initial_tool_results if attempt == 1 else None,
                auto_tool_loop=auto_tool_loop,
                max_tool_iterations=max_tool_iterations,
                parent_task_id=parent_task.id,
                source_type="maestro_orchestrator",
                workflow_key="maestro.generic.child",
                priority=subtask.priority,
            )
            attempts.append(run)
            final_run = run
            if run.status != "failed":
                if self._run_has_pending_approval(run):
                    self._update_queue_item(
                        parent_task,
                        subtask,
                        status="blocked",
                        child_task_id=run.task_id,
                        child_report_id=run.report_id,
                        error_message="Waiting for Chris to approve tool use.",
                        retry_count=attempt - 1,
                    )
                    break
                self._update_queue_item(
                    parent_task,
                    subtask,
                    status="completed",
                    child_task_id=run.task_id,
                    child_report_id=run.report_id,
                    completed_at=datetime.now(UTC).isoformat(),
                    error_message=None,
                    retry_count=attempt - 1,
                )
                break
            if attempt < max_attempts:
                self._update_queue_item(
                    parent_task,
                    subtask,
                    status="retrying",
                    child_task_id=run.task_id,
                    child_report_id=run.report_id,
                    error_message=run.error_message or "Agent task failed; retrying.",
                    retry_count=attempt,
                )
                continue
            self._update_queue_item(
                parent_task,
                subtask,
                status="failed",
                child_task_id=run.task_id,
                child_report_id=run.report_id,
                completed_at=datetime.now(UTC).isoformat(),
                error_message=run.error_message,
                retry_count=attempt - 1,
            )
        if final_run is None:
            raise MaestroOrchestratorError("Subtask did not produce a run result.")
        return {"attempts": attempts, "final": final_run}

    def _run_has_pending_approval(self, run: AgentRunResult) -> bool:
        return any(call.get("status") == "approval_required" for call in run.tool_calls)

    def _queue_coding_delivery_review(
        self,
        parent_task: Task,
        subtask: MaestroSubtask,
        run: AgentRunResult,
    ) -> AgentRunResult:
        """Hold a coding workflow at a deliberate PR review checkpoint."""
        if run.status != "completed" or not run.task_id:
            return run
        if any(call.get("tool_name") == "local.app.deploy_pr" for call in run.tool_calls):
            return run
        pr_number = _coding_pr_number(run.tool_calls)
        if pr_number is None:
            return run
        try:
            child_task = self.session.get(Task, uuid.UUID(run.task_id))
        except (TypeError, ValueError):
            return run
        if child_task is None:
            return run
        proposed = ToolExecutionService(
            self.session,
            adapters=self.runtime.tool_adapters,
        ).propose_for_task(
            ToolExecutionRequest(
                agent_key=run.agent.key,
                tool_key="local.app.deploy_pr",
                payload={"pr_number": pr_number, "method": "squash", "delete_branch": True},
            ),
            task=child_task,
            rationale=(
                f"Codex created PR #{pr_number}. Chris must review it before Maestro merges it "
                "and updates the dedicated runtime."
            ),
            safety_level="production_code_delivery",
            reason=(
                f"PR #{pr_number} is ready for Chris's review. Approval will merge it and pull "
                "the result into the dedicated runtime checkout."
            ),
        )
        approval_payload = tool_result_payload(proposed)
        child_task.status = "blocked"
        child_task.error_message = f"Waiting for Chris to review PR #{pr_number} and approve delivery."
        child_task.completed_at = None
        self.session.commit()
        self._update_queue_item(
            parent_task,
            subtask,
            status="approval_required",
            child_task_id=run.task_id,
            child_report_id=run.report_id,
            error_message=child_task.error_message,
        )
        return replace(
            run,
            status="blocked",
            execution_note=(
                f"Coding is complete and PR #{pr_number} is ready for your review. "
                "The workflow will resume after you approve delivery."
            ),
            tool_calls=[*run.tool_calls, approval_payload],
        )

    def _queue_item_for_subtask(
        self,
        task: Task,
        subtask: MaestroSubtask,
    ) -> dict[str, Any] | None:
        scheduler = dict((task.input_payload or {}).get("scheduler", {}))
        for item in scheduler.get("queue_items", []):
            if (
                item.get("agent_key") == subtask.agent_key
                and item.get("work_item_ids") == (subtask.work_item_ids or [])
            ):
                return item
        return None

    def _queue_item_status(self, task: Task, subtask: MaestroSubtask) -> str | None:
        item = self._queue_item_for_subtask(task, subtask)
        return str(item.get("status")) if item else None

    def _queue_item_has_approved_tool_result(
        self,
        task: Task,
        subtask: MaestroSubtask,
        approved_tool_results_by_child_task: dict[str, list[dict[str, Any]]],
    ) -> bool:
        item = self._queue_item_for_subtask(task, subtask)
        if not item:
            return False
        child_task_id = item.get("child_task_id")
        return bool(child_task_id and approved_tool_results_by_child_task.get(str(child_task_id)))

    def _approved_tool_results_for_subtask(
        self,
        task: Task,
        subtask: MaestroSubtask,
        approved_tool_results_by_child_task: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        item = self._queue_item_for_subtask(task, subtask)
        if not item:
            return []
        child_task_id = item.get("child_task_id")
        if not child_task_id:
            return []
        return list(approved_tool_results_by_child_task.get(str(child_task_id), []))

    def _mark_approved_queue_items_ready(
        self,
        task: Task,
        approved_tool_results_by_child_task: dict[str, list[dict[str, Any]]],
    ) -> None:
        if not approved_tool_results_by_child_task:
            return
        payload = dict(task.input_payload or {})
        scheduler = {
            **self._scheduler_payload(),
            **dict(payload.get("scheduler", {})),
        }
        approved_child_ids = set(approved_tool_results_by_child_task)
        queue_items = []
        changed = False
        for item in scheduler.get("queue_items", []):
            if str(item.get("child_task_id")) in approved_child_ids and item.get("status") == "blocked":
                item = {
                    **item,
                    "status": "pending",
                    "error_message": None,
                    "completed_at": None,
                }
                changed = True
            if str(item.get("child_task_id")) in approved_child_ids and item.get("status") == "approval_required":
                approved_results = approved_tool_results_by_child_task.get(str(item.get("child_task_id")), [])
                is_delivery = any(
                    result.get("tool_name") == "local.app.deploy_pr" and result.get("status") == "complete"
                    for result in approved_results
                )
                if is_delivery:
                    item = {
                        **item,
                        "status": "completed",
                        "error_message": None,
                        "completed_at": datetime.now(UTC).isoformat(),
                    }
                    changed = True
                else:
                    item = {
                        **item,
                        "status": "pending",
                        "error_message": None,
                        "completed_at": None,
                    }
                    changed = True
            queue_items.append(item)
        if changed:
            scheduler["queue_items"] = queue_items
            payload["scheduler"] = scheduler
            task.input_payload = payload
            self.session.commit()

    def _completed_outputs_from_scheduler(self, task: Task) -> dict[str, str]:
        scheduler = dict((task.input_payload or {}).get("scheduler", {}))
        outputs: dict[str, str] = {}
        for item in scheduler.get("queue_items", []):
            if item.get("status") != "completed":
                continue
            report_id = item.get("child_report_id")
            body = ""
            if report_id:
                report = self.session.get(Report, uuid.UUID(str(report_id)))
                if report is not None:
                    body = report.body_markdown or report.summary or ""
            for work_item_id in item.get("work_item_ids") or []:
                outputs[str(work_item_id)] = body or "Completed in an earlier workflow stage."
        return outputs

    def _scheduler_has_incomplete_queue(self, task: Task) -> bool:
        scheduler = dict((task.input_payload or {}).get("scheduler", {}))
        queue_items = scheduler.get("queue_items", [])
        return any(item.get("status") != "completed" for item in queue_items)

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

    def _registry_snapshot(self, domains, agents, tools, skills=None) -> dict[str, Any]:
        return {
            "domains": [
                {
                    "key": domain.key,
                    "name": domain.name,
                    "context": _truncate_registry_text(domain.context, 500),
                }
                for domain in domains
            ],
            "agents": [self._selected_agent_payload(agent) for agent in agents],
            "tools": [
                {
                    "key": tool.key,
                    "name": tool.name,
                    "exclusive": tool.exclusive,
                }
                for tool in tools
            ],
            "skills": [
                {
                    "key": skill.key,
                    "name": skill.name,
                    "category": skill.category,
                    "domain_key": skill.domain_key,
                    "description": _truncate_registry_text(skill.description or skill.instruction, 260),
                }
                for skill in (skills or [])
            ],
        }

    def _selected_agent_payload(self, agent, *, user_input: str | None = None) -> dict[str, Any]:
        payload = {
            "key": agent.key,
            "name": agent.name,
            "domain_key": agent.domain_key,
            "role_summary": _truncate_registry_text(agent.role_summary, 260),
            "allowed_tool_keys": [tool.key for tool in agent.allowed_tools],
            "allowed_skill_keys": [skill.key for skill in agent.allowed_skills],
            "model_profile": agent.model_profile,
        }
        if user_input is not None:
            payload["selection_rationale"] = self._subtask_rationale(user_input, agent)
        return payload

    def _scheduler_payload(
        self,
        *,
        queue_items: list[dict[str, Any]] | None = None,
        status: str = "queue_foundation",
        schedule_candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "policy": (
                "Plan-first execution. Queue items are grouped into dependency stages; items "
                "inside a stage are parallel-ready, retried once on failure, and downstream "
                "dependents are blocked if retries fail."
            ),
            "max_attempts": 2,
            "resource_locks": [],
            "recurring_scheduler": "planned",
            "active_stage_index": None,
            "active_queue_item_id": None,
            "current_step": "Proposed plan ready for review.",
            "queue_items": queue_items or [],
            "schedule_candidate": schedule_candidate,
            "scheduled_definition_id": None,
        }

    def _schedule_candidate_from_input(
        self,
        user_input: str,
        work_items: list[MaestroWorkItem],
        subtasks: list[MaestroSubtask],
        *,
        schedule_guidance: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        lowered = user_input.lower()
        trigger_type = self._trigger_type_from_schedule_guidance(schedule_guidance)
        if schedule_guidance and trigger_type is None:
            return None
        if trigger_type is None:
            trigger_type = self._schedule_trigger_type(lowered)
        if trigger_type is None:
            return None
        if not any(item.needs_agent for item in work_items):
            return None
        primary_domain = subtasks[0].domain_key if subtasks else (work_items[0].domain_key if work_items else None)
        name_seed = next((item.title for item in work_items if item.needs_agent), "Scheduled Maestro workflow")
        key_seed = self._slug(f"{primary_domain or 'maestro'}-{name_seed}")
        schedule_details = dict((schedule_guidance or {}).get("schedule_details") or {})
        if trigger_type == "event":
            trigger_config = {
                "event_type": schedule_details.get("event_type") or self._event_type_from_text(lowered),
                "filters": self._event_filters_from_text(lowered, primary_domain),
                "source": "maestro_plan",
            }
            if isinstance(schedule_details.get("filters"), dict):
                trigger_config["filters"] = schedule_details["filters"]
        else:
            trigger_config = {
                "time_of_day": schedule_details.get("time_of_day") or self._time_of_day_from_text(lowered),
                "interval_minutes": int(schedule_details.get("interval_minutes") or 1440),
                "source": "maestro_plan",
            }
        return {
            "key": key_seed,
            "name": name_seed,
            "description": user_input[:500],
            "domain_key": primary_domain,
            "trigger_type": trigger_type,
            "trigger_config": trigger_config,
            "priority": "high" if any(item.priority == "high" for item in work_items) else "normal",
            "fairness_group": primary_domain or "maestro",
        }

    def _schedule_guidance_from_classifier(self, user_input: str) -> dict[str, Any] | None:
        match = re.search(
            r"<maestro_hidden_context\b[^>]*purpose=\"message_intent\"[^>]*>(.*?)</maestro_hidden_context>",
            user_input,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return None
        body = match.group(1)
        json_start = body.find("{")
        json_end = body.rfind("}")
        if json_start < 0 or json_end <= json_start:
            return None
        try:
            payload = json.loads(body[json_start : json_end + 1])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        intents = payload.get("intents") if isinstance(payload, dict) else None
        if not isinstance(intents, list):
            return None
        timing_priority = {
            "delete_schedule": 100,
            "modify_schedule": 90,
            "one_time": 80,
            "triggered": 70,
            "recurring": 65,
            "scheduled": 60,
            "unspecified": 0,
        }
        best: dict[str, Any] | None = None
        best_score = 0
        for intent in intents:
            if not isinstance(intent, dict):
                continue
            if float(intent.get("confidence") or 0.0) < 0.55:
                continue
            if intent.get("type") not in {"workflow_request", "plan_refinement", "system_command"}:
                continue
            timing = str(intent.get("workflow_timing") or "unspecified")
            score = timing_priority.get(timing, 0)
            if score <= best_score:
                continue
            best_score = score
            best = {
                "workflow_timing": timing,
                "schedule_details": intent.get("schedule_details") if isinstance(intent.get("schedule_details"), dict) else {},
                "recommended_next_step": intent.get("recommended_next_step"),
                "span": intent.get("span"),
                "reason": intent.get("reason"),
            }
        return best

    def _trigger_type_from_schedule_guidance(self, guidance: dict[str, Any] | None) -> str | None:
        if not guidance:
            return None
        timing = str(guidance.get("workflow_timing") or "unspecified")
        if timing in {"one_time", "modify_schedule", "delete_schedule", "unspecified"}:
            return None
        details = guidance.get("schedule_details") if isinstance(guidance.get("schedule_details"), dict) else {}
        trigger_type = str(details.get("trigger_type") or "").lower()
        if trigger_type == "event" or timing == "triggered":
            return "event"
        if trigger_type in {"scheduled", "recurring"}:
            return "recurring"
        if timing in {"scheduled", "recurring"}:
            return "recurring"
        return None

    def _schedule_trigger_type(self, lowered_input: str) -> str | None:
        if self._negates_scheduling(lowered_input):
            return None
        event_tokens = (
            "each time",
            "every time",
            "whenever",
            "when a new",
            "when new",
            "new email arrives",
            "email arrives",
        )
        if any(token in lowered_input for token in event_tokens):
            return "event"
        recurring_tokens = (
            "every morning",
            "every day",
            "daily",
            "recurring",
            "schedule",
            "scheduled",
            "each morning",
            "each day",
        )
        if any(token in lowered_input for token in recurring_tokens):
            return "recurring"
        return None

    def _negates_scheduling(self, lowered_input: str) -> bool:
        negation_patterns = (
            r"\bdo\s+not\s+schedu\w*",
            r"\bdon't\s+schedu\w*",
            r"\bnot\s+(?:a\s+)?(?:recurring|scheduled|schedule)",
            r"\bno\s+(?:recurring|scheduled|schedule)",
            r"\bnot\s+recurring\b",
            r"\bone[-\s]?time\b",
            r"\bonly\s+needs?\s+to\s+happen\s+once\b",
            r"\brun\s+now\b",
            r"\bqueue\s+it\s+for\s+execution\b",
            r"\bnot\s+a\s+schedule\b",
        )
        return any(re.search(pattern, lowered_input) for pattern in negation_patterns)

    def _time_of_day_from_text(self, lowered_input: str) -> str:
        before_match = re.search(r"before\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lowered_input)
        if before_match:
            hour = self._hour_24(before_match.group(1), before_match.group(3))
            minute = int(before_match.group(2) or "0")
            if minute == 0:
                hour = max(0, hour - 1)
                minute = 55
            return f"{hour:02d}:{minute:02d}"
        at_match = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lowered_input)
        if at_match:
            hour = self._hour_24(at_match.group(1), at_match.group(3))
            minute = int(at_match.group(2) or "0")
            return f"{hour:02d}:{minute:02d}"
        return "08:00"

    def _hour_24(self, hour_text: str, suffix: str | None) -> int:
        hour = int(hour_text)
        if suffix == "pm" and hour < 12:
            hour += 12
        if suffix == "am" and hour == 12:
            hour = 0
        return hour

    def _event_type_from_text(self, lowered_input: str) -> str:
        if "email" in lowered_input or "gmail" in lowered_input:
            return "gmail.message.received"
        return "maestro.event.received"

    def _event_filters_from_text(
        self,
        lowered_input: str,
        primary_domain: str | None,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        if primary_domain:
            filters["domain_key"] = primary_domain
        if "gmail" in lowered_input:
            filters["provider"] = "gmail"
        return filters

    def _tools_from_scheduled_text(self, lowered_input: str) -> list[str]:
        return self._tools_from_request_text(lowered_input)

    def _tools_from_request_text(self, lowered_input: str) -> list[str]:
        tools: list[str] = []
        if "email" in lowered_input or "gmail" in lowered_input:
            tools.extend(["gmail.message.search", "gmail.message.get"])
        if "backlog" in lowered_input or "github" in lowered_input:
            tools.extend(["github.issue.search", "github.issue.get"])
        if any(token in lowered_input for token in ("sota", "state of the art", "web", "latest", "current", "research")):
            tools.append("web.search")
        return tools

    def _research_tools_from_text(self, lowered_input: str) -> list[str]:
        if any(token in lowered_input for token in ("sota", "state of the art", "web", "latest", "current", "research")):
            return ["web.search"]
        return []

    def _is_schedule_definition_request(self, plan: MaestroPlan) -> bool:
        candidate = plan.scheduler.get("schedule_candidate")
        if not isinstance(candidate, dict):
            return False
        lowered = plan.user_input.lower()
        immediate_tokens = ("run now", "also run now", "execute now", "start now", "do it now")
        return not any(token in lowered for token in immediate_tokens)

    def _upsert_schedule_definition_if_requested(self, task: Task, plan: MaestroPlan):
        scheduler = dict((task.input_payload or {}).get("scheduler", {}))
        candidate = scheduler.get("schedule_candidate")
        if not isinstance(candidate, dict):
            return None
        queue_items = scheduler.get("queue_items") if isinstance(scheduler.get("queue_items"), list) else []
        if not queue_items:
            payload_items = [
                MaestroWorkItem(**item)
                for item in (task.input_payload or {}).get("work_items", [])
                if isinstance(item, dict)
            ]
            queue_items = self._definition_queue_items_from_work_items(payload_items)
        if not queue_items:
            return None
        domain_id = None
        domain_key = candidate.get("domain_key")
        if domain_key:
            from app.db.repositories import DomainRepository

            domain = DomainRepository(self.session).get_by_key(str(domain_key))
            domain_id = domain.id if domain else None
        definition = SchedulerService(self.session).upsert_definition(
            key=str(candidate.get("key") or self._slug(plan.summary)),
            name=str(candidate.get("name") or plan.summary[:120]),
            domain_id=domain_id,
            description=str(candidate.get("description") or plan.user_input[:500]),
            trigger_type=str(candidate.get("trigger_type") or "recurring"),
            trigger_config=dict(candidate.get("trigger_config") or {}),
            workflow_spec={
                "source_plan_id": plan.plan_id,
                "source_parent_task_id": plan.parent_task_id,
                "queue_items": queue_items,
            },
            priority=str(candidate.get("priority") or plan.status or "normal"),
            fairness_group=str(candidate.get("fairness_group") or domain_key or "maestro"),
            is_active=True,
        )
        payload = dict(task.input_payload or {})
        scheduler = {
            **self._scheduler_payload(),
            **dict(payload.get("scheduler", {})),
            "scheduled_definition_id": str(definition.id),
            "status": "scheduled_definition_saved",
        }
        payload["scheduler"] = scheduler
        task.input_payload = payload
        self.session.commit()
        return definition

    def _save_scheduled_workflow(self, task: Task, plan: MaestroPlan) -> MaestroRun:
        definition = self._upsert_schedule_definition_if_requested(task, plan)
        if definition is None:
            raise MaestroOrchestratorError("Scheduled workflow could not be saved.")
        task.status = "scheduled"
        task.completed_at = datetime.now(UTC)
        payload = dict(task.input_payload or {})
        scheduler = {
            **self._scheduler_payload(),
            **dict(payload.get("scheduler", {})),
            "scheduled_definition_id": str(definition.id),
            "status": "scheduled",
            "current_step": "Scheduled workflow saved.",
        }
        scheduler["queue_items"] = [
            {**item, "status": "scheduled"} for item in scheduler.get("queue_items", [])
        ]
        payload["scheduler"] = scheduler
        task.input_payload = payload
        summary = (
            f"I scheduled `{definition.name}` as a {definition.trigger_type} workflow. "
            "You can inspect or edit it from Queue."
        )
        task.output_payload = {
            "plan_id": plan.plan_id,
            "status": "scheduled",
            "chat_summary": summary,
            "scheduled_definition_id": str(definition.id),
            "scheduler": scheduler,
        }
        self.session.commit()
        SchedulerService(self.session).enqueue_maestro_plan(task)
        SchedulerService(self.session).sync_run_status_from_task(task)
        self.session.refresh(task)
        return MaestroRun(
            plan=self._plan_from_task(task),
            status="scheduled",
            parent_task_id=str(task.id),
            child_runs=[],
            synthesis_report_id=None,
            synthesis=summary,
            chat_summary=summary,
            staged_artifact_path=None,
            artifact_id=None,
            scheduler=scheduler,
            execution_stages=plan.execution_stages,
            tool_activity=[],
            error_message=None,
        )

    def _slug(self, value: str) -> str:
        cleaned = "".join(character.lower() if character.isalnum() else "-" for character in value)
        while "--" in cleaned:
            cleaned = cleaned.replace("--", "-")
        return cleaned.strip("-")[:120] or "scheduled-maestro-workflow"

    def _definition_queue_items_from_work_items(
        self,
        work_items: list[MaestroWorkItem],
    ) -> list[dict[str, Any]]:
        queue_items: list[dict[str, Any]] = []
        for position, item in enumerate([item for item in work_items if item.needs_agent], start=1):
            queue_items.append(
                {
                    "id": item.id,
                    "stage_index": 1,
                    "position": position,
                    "status": "pending",
                    "domain_key": item.domain_key,
                    "objective": item.description or item.title,
                    "priority": item.priority,
                    "work_item_ids": [item.id],
                    "depends_on_work_item_ids": item.dependencies,
                    "required_tools": item.required_tools,
                    "required_skills": item.required_skills,
                    "model_profile": item.model_profile,
                    "model_tier": item.model_tier,
                    "model_rationale": item.model_rationale,
                }
            )
        return queue_items

    def _queue_items(self, subtasks: list[MaestroSubtask]) -> list[dict[str, Any]]:
        queue_items: list[dict[str, Any]] = []
        for stage_index, stage in enumerate(self._execution_stages(subtasks), start=1):
            for position, subtask in enumerate(stage, start=1):
                work_item_key = "-".join(subtask.work_item_ids or [subtask.agent_key])
                queue_items.append(
                    {
                        "id": f"q{stage_index}-{position}-{work_item_key}",
                        "stage_index": stage_index,
                        "position": position,
                        "status": "pending",
                        "agent_key": subtask.agent_key,
                        "agent_name": subtask.agent_name,
                        "domain_key": subtask.domain_key,
                        "objective": subtask.objective,
                        "priority": subtask.priority,
                        "work_item_ids": subtask.work_item_ids or [],
                        "depends_on_work_item_ids": subtask.depends_on_work_item_ids or [],
                        "required_skills": subtask.required_skills or [],
                        "model_profile": subtask.model_profile,
                        "model_tier": subtask.model_tier,
                        "model_rationale": subtask.model_rationale,
                        "child_task_id": None,
                        "child_report_id": None,
                        "retry_count": 0,
                        "started_at": None,
                        "completed_at": None,
                        "error_message": None,
                    }
                )
        return queue_items

    def _queue_with_status(self, scheduler: dict[str, Any], status: str) -> list[dict[str, Any]]:
        return [
            {
                **item,
                "status": status,
                "child_task_id": None,
                "child_report_id": None,
                "retry_count": 0,
                "started_at": None,
                "completed_at": None,
                "error_message": None,
            }
            for item in scheduler.get("queue_items", [])
        ]

    def _replace_scheduler(
        self,
        task: Task,
        *,
        queue_items: list[dict[str, Any]],
        scheduler_status: str | None = None,
    ) -> None:
        payload = dict(task.input_payload or {})
        scheduler = {
            **self._scheduler_payload(),
            **dict(payload.get("scheduler", {})),
            "queue_items": queue_items,
        }
        if scheduler_status is not None:
            scheduler["status"] = scheduler_status
        scheduler.update(self._current_scheduler_step(queue_items))
        payload["scheduler"] = scheduler
        task.input_payload = payload

    def _set_scheduler_status(self, task: Task, status: str) -> None:
        payload = dict(task.input_payload or {})
        existing_scheduler = dict(payload.get("scheduler", {}))
        scheduler = {
            **self._scheduler_payload(),
            **existing_scheduler,
            "status": status,
        }
        scheduler.update(self._current_scheduler_step(existing_scheduler.get("queue_items", [])))
        payload["scheduler"] = scheduler
        task.input_payload = payload

    def _update_queue_item(
        self,
        task: Task,
        subtask: MaestroSubtask,
        *,
        status: str,
        child_task_id: str | None = None,
        child_report_id: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        error_message: str | None = None,
        retry_count: int | None = None,
        only_statuses: set[str] | None = None,
    ) -> None:
        payload = dict(task.input_payload or {})
        scheduler = {
            **self._scheduler_payload(),
            **dict(payload.get("scheduler", {})),
        }
        queue_items = []
        for item in scheduler.get("queue_items", []):
            matches = (
                item.get("agent_key") == subtask.agent_key
                and item.get("work_item_ids") == (subtask.work_item_ids or [])
            )
            if matches and (only_statuses is None or item.get("status") in only_statuses):
                item = {
                    **item,
                    "status": status,
                    "child_task_id": child_task_id if child_task_id is not None else item.get("child_task_id"),
                    "child_report_id": child_report_id if child_report_id is not None else item.get("child_report_id"),
                    "retry_count": retry_count if retry_count is not None else item.get("retry_count", 0),
                    "started_at": started_at if started_at is not None else item.get("started_at"),
                    "completed_at": completed_at if completed_at is not None else item.get("completed_at"),
                    "error_message": error_message,
                }
            queue_items.append(item)
        scheduler["queue_items"] = queue_items
        scheduler.update(self._current_scheduler_step(queue_items))
        payload["scheduler"] = scheduler
        task.input_payload = payload
        self.session.commit()

    def _current_scheduler_step(self, queue_items: list[dict[str, Any]]) -> dict[str, Any]:
        if not queue_items:
            return {
                "active_stage_index": None,
                "active_queue_item_id": None,
                "current_step": "No executable agent work.",
            }
        priority_statuses = (
            "running",
            "retrying",
            "approval_required",
            "blocked",
            "failed",
            "ready",
            "queued",
            "pending",
            "proposed",
            "scheduled",
        )
        for status in priority_statuses:
            item = next((item for item in queue_items if item.get("status") == status), None)
            if item:
                agent_name = item.get("agent_name") or item.get("agent_key") or "agent"
                if status == "blocked":
                    current_step = f"Waiting on {agent_name}: {item.get('error_message') or 'blocked'}"
                elif status == "approval_required":
                    current_step = f"Waiting for approval: {agent_name}"
                elif status == "scheduled":
                    current_step = f"Scheduled: {agent_name}"
                elif status == "queued":
                    current_step = f"Queued: {agent_name}"
                elif status == "pending":
                    current_step = f"Ready to queue: {agent_name}"
                elif status == "failed":
                    current_step = f"Failed: {agent_name}"
                else:
                    current_step = f"{status.title()}: {agent_name}"
                return {
                    "active_stage_index": item.get("stage_index"),
                    "active_queue_item_id": item.get("id"),
                    "current_step": current_step,
                }
        if all(item.get("status") == "completed" for item in queue_items):
            return {
                "active_stage_index": None,
                "active_queue_item_id": None,
                "current_step": "Workflow complete.",
            }
        if all(item.get("status") == "archived" for item in queue_items):
            return {
                "active_stage_index": None,
                "active_queue_item_id": None,
                "current_step": "Workflow archived.",
            }
        return {
            "active_stage_index": None,
            "active_queue_item_id": None,
            "current_step": "Workflow state unknown.",
        }

    def _refined_plan_input(self, previous_plan: MaestroPlan, refinement: str) -> str:
        work_item_lines = [
            f"- {item.id}: {item.type} / {item.domain_key or 'global'} / {item.title}"
            for item in previous_plan.work_items
        ]
        subtask_lines = [
            f"- {subtask.agent_key}: {subtask.objective}"
            for subtask in previous_plan.subtasks
        ]
        previous_run_context_lines = self._previous_run_context_lines(previous_plan)
        return "\n".join(
            [
                "Refine the existing Maestro plan using the new user message.",
                "",
                "Original user input:",
                previous_plan.user_input,
                "",
                "Previous plan summary:",
                previous_plan.summary,
                "",
                "Previous work items:",
                *(work_item_lines or ["- none"]),
                "",
                "Previous subtasks:",
                *(subtask_lines or ["- none"]),
                "",
                "Previous run context:",
                *(previous_run_context_lines or ["- no completed run context available"]),
                "",
                "New user refinement:",
                refinement,
                "",
                "Return an updated plan that preserves still-valid work, removes obsolete work, "
                "and incorporates the refinement without treating this as an unrelated new session.",
            ]
        )

    def _previous_run_context_lines(self, previous_plan: MaestroPlan) -> list[str]:
        try:
            parent_task = self.session.get(Task, uuid.UUID(previous_plan.parent_task_id))
        except (TypeError, ValueError):
            return []
        if parent_task is None:
            return []
        payload = parent_task.output_payload or {}
        if not isinstance(payload, dict):
            return []

        lines: list[str] = []
        chat_summary = str(payload.get("chat_summary") or "").strip()
        if chat_summary:
            lines.append(f"- Last user-facing summary: {chat_summary}")

        synthesis_report_id = payload.get("synthesis_report_id")
        if synthesis_report_id:
            lines.append(f"- Last synthesis report id: {synthesis_report_id}")

        for item in payload.get("tool_activity") or []:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool_name") or "unknown tool")
            status = str(item.get("status") or "unknown")
            details = str(item.get("details") or "").strip()
            output_payload = item.get("output_payload") if isinstance(item.get("output_payload"), dict) else {}
            fragments = [f"- Tool {tool_name} finished with status {status}"]
            if details:
                fragments.append(f"details: {details}")
            pr_number = output_payload.get("pr_number") or output_payload.get("number")
            pr_url = output_payload.get("pr_url")
            branch = output_payload.get("branch")
            base_branch = output_payload.get("base_branch")
            changed_files = output_payload.get("changed_files")
            if pr_number:
                fragments.append(f"PR number: {pr_number}")
            if pr_url:
                fragments.append(f"PR URL: {pr_url}")
            if branch:
                fragments.append(f"branch: {branch}")
            if base_branch:
                fragments.append(f"base branch: {base_branch}")
            if changed_files:
                fragments.append(f"changed files: {changed_files}")
            lines.append("; ".join(fragments))

        return lines

    def _plan_from_task(self, task: Task) -> MaestroPlan:
        payload = task.input_payload or {}
        return MaestroPlan(
            plan_id=str(payload.get("plan_id") or task.id),
            status=task.status,
            user_input=str(payload.get("user_input") or ""),
            summary=task.objective,
            execution_mode=str(payload.get("execution_mode") or "propose_first"),
            planner_mode=str(payload.get("planner_mode") or "deterministic"),
            work_items=[
                MaestroWorkItem(**_hydrate_work_item_payload(work_item))
                for work_item in payload.get("work_items", [])
            ],
            intents=[MaestroIntent(**intent) for intent in payload.get("intents", [])],
            subtasks=[MaestroSubtask(**_hydrate_subtask_payload(subtask)) for subtask in payload.get("subtasks", [])],
            execution_stages=list(payload.get("execution_stages", [])),
            workflow_graph=dict(payload.get("workflow_graph", {})),
            is_chat_only=bool(payload.get("is_chat_only", False)),
            is_routing_only=bool(payload.get("is_routing_only", False)),
            selected_agents=list(payload.get("selected_agents", [])),
            registry_snapshot=dict(payload.get("registry_snapshot", {})),
            approval_required=bool(payload.get("approval_required", True)),
            scheduler=dict(payload.get("scheduler", self._scheduler_payload())),
            created_at=home_isoformat(task.created_at) or "",
            parent_task_id=str(task.id),
            direct_response=payload.get("direct_response"),
            planner_notes=payload.get("planner_notes"),
        )

    def _synthesize(
        self,
        plan: MaestroPlan,
        child_runs: list[AgentRunResult],
        *,
        status: str,
        phase_syntheses: list[dict[str, Any]] | None = None,
        tool_activity: list[dict[str, Any]] | None = None,
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
        if tool_activity:
            lines.extend(["## Tool Activity", ""])
            for item in tool_activity:
                details = item.get("details") or ""
                lines.extend(
                    [
                        (
                            f"- {item['agent_name']} used `{item['tool_name']}` "
                            f"with status `{item['status']}`.{(' ' + details) if details else ''}"
                        )
                    ]
                )
            lines.append("")
        if phase_syntheses:
            lines.extend(["## Phase Syntheses", ""])
            for phase in phase_syntheses:
                lines.extend(
                    [
                        f"### Stage {phase['stage_index']}",
                        phase["summary"],
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

    def _synthesize_phase(
        self,
        *,
        stage_index: int,
        stage: list[MaestroSubtask],
        child_runs: list[AgentRunResult],
    ) -> dict[str, Any]:
        completed = [run for run in child_runs if run.status != "failed"]
        failed = [run for run in child_runs if run.status == "failed"]
        agent_names = ", ".join(subtask.agent_name for subtask in stage)
        summary = (
            f"Stage {stage_index} ran {len(stage)} parallel-ready queue item(s): {agent_names}. "
            f"{len(completed)} completed and {len(failed)} failed."
        )
        if failed:
            summary += " Failed work will block dependent downstream queue items."
        return {
            "stage_index": stage_index,
            "agent_keys": [subtask.agent_key for subtask in stage],
            "work_item_ids": [
                work_item_id
                for subtask in stage
                for work_item_id in (subtask.work_item_ids or [])
            ],
            "completed_count": len(completed),
            "failed_count": len(failed),
            "summary": summary,
        }

    def _write_synthesis_report(
        self,
        parent_task: Task,
        plan: MaestroPlan,
        child_runs: list[AgentRunResult],
        synthesis: str,
        status: str,
        phase_syntheses: list[dict[str, Any]] | None = None,
        tool_activity: list[dict[str, Any]] | None = None,
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
                "phase_syntheses": phase_syntheses or [],
                "tool_activity": tool_activity or [],
            },
        )
        self.session.add(report)
        self.session.flush()
        return report

    def _tool_activity(self, child_runs: list[AgentRunResult]) -> list[dict[str, Any]]:
        activity: list[dict[str, Any]] = []
        for run in child_runs:
            for call in run.tool_calls:
                tool_name = call.get("tool_name")
                if tool_name in {None, "llm.gateway"}:
                    continue
                raw_output_payload = call.get("output_payload") or {}
                output_payload = raw_output_payload if isinstance(raw_output_payload, dict) else {}
                details = self._tool_activity_details(str(tool_name), output_payload)
                activity.append(
                    {
                        "tool_call_id": call.get("id"),
                        "agent_key": run.agent.key,
                        "agent_name": run.agent.name,
                        "domain_key": run.agent.domain_key,
                        "tool_name": tool_name,
                        "status": call.get("status"),
                        "error_message": call.get("error_message"),
                        "details": details,
                        "output_payload": self._tool_activity_output_payload(
                            str(tool_name),
                            output_payload,
                        ),
                    }
                )
        return activity

    def _tool_activity_output_payload(
        self,
        tool_name: str,
        output_payload: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name == "codex.task.run":
            return {
                key: output_payload.get(key)
                for key in (
                    "branch_workflow",
                    "branch",
                    "base_branch",
                    "commit_sha",
                    "changed_files",
                    "diff_summary",
                    "final_message",
                    "pr",
                    "pr_url",
                    "pr_number",
                    "review_status",
                )
                if key in output_payload
            }
        if tool_name.startswith("github.pr."):
            return {
                key: output_payload.get(key)
                for key in ("pr", "pr_url", "pr_number", "diff", "checks", "summary")
                if key in output_payload
            }
        return {}

    def _tool_activity_details(self, tool_name: str, output_payload: dict[str, Any]) -> str:
        if tool_name == "llm.tool_planner":
            summary = output_payload.get("plan_summary")
            count = output_payload.get("tool_call_count")
            if summary:
                return f"Planner: {summary}"
            if count is not None:
                return f"Planner requested {count} tool call(s)."
        if tool_name == "github.pr.search":
            prs = output_payload.get("prs")
            if isinstance(prs, list):
                return f"Found {len(prs)} PR(s)."
        if tool_name == "github.pr.get":
            pr = output_payload.get("pr")
            if isinstance(pr, dict):
                number = pr.get("number")
                title = pr.get("title")
                if number and title:
                    return f"Read PR #{number}: {title}."
                if number:
                    return f"Read PR #{number}."
        if tool_name == "github.pr.diff":
            files = output_payload.get("files")
            if isinstance(files, list):
                return f"Read diff for {len(files)} changed file(s)."
            diff = output_payload.get("diff")
            if isinstance(diff, str):
                return f"Read diff ({len(diff)} chars)."
        if tool_name == "github.pr.checks":
            checks = output_payload.get("checks")
            check_status = output_payload.get("check_status")
            if isinstance(checks, list):
                if check_status:
                    return f"Read {len(checks)} check(s): {check_status}."
                return f"Read {len(checks)} check(s)."
            conclusion = output_payload.get("conclusion") or output_payload.get("state")
            if conclusion:
                return f"Checks: {conclusion}."
        if tool_name == "github.pr.merge":
            number = output_payload.get("pr_number") or output_payload.get("number")
            method = output_payload.get("merge_method")
            if number:
                return f"Merged PR #{number}{f' with {method}' if method else ''}."
            return "Merged a GitHub pull request."
        if tool_name == "github.issue.search":
            issues = output_payload.get("issues")
            if isinstance(issues, list):
                return f"Found {len(issues)} issue(s)."
        if tool_name == "github.issue.get":
            issue = output_payload.get("issue")
            if isinstance(issue, dict):
                number = issue.get("number")
                title = issue.get("title")
                if number and title:
                    return f"Read issue #{number}: {title}."
        if tool_name == "github.issue.create":
            preview = output_payload.get("approval_preview")
            if isinstance(preview, dict):
                summary = str(preview.get("summary") or "").strip()
                if summary:
                    return summary.replace("\n", " ")
            url = output_payload.get("issue_url") or output_payload.get("url")
            skipped_labels = output_payload.get("labels_skipped") or output_payload.get(
                "skipped_labels"
            )
            if url:
                details = f"Created issue: {url}."
                if isinstance(skipped_labels, list) and skipped_labels:
                    skipped = ", ".join(str(label) for label in skipped_labels)
                    details += f" Skipped missing label(s): {skipped}."
                return details
        if output_payload.get("approval_required"):
            reason = output_payload.get("reason")
            return str(reason or "Requires Chris approval before execution.")
        if tool_name == "github.repo.get":
            repo = output_payload.get("name") or output_payload.get("repo")
            if repo:
                return f"Repository: {repo}."
        if tool_name == "github.repo.list":
            repos = output_payload.get("repos")
            owner = output_payload.get("owner")
            if isinstance(repos, list):
                return f"Listed {len(repos)} repo(s){f' for {owner}' if owner else ''}."
        if tool_name == "github.repo.create":
            repo = output_payload.get("repo")
            if repo:
                return f"Repository creation proposed for {repo}."
        if tool_name == "github.file.get":
            path = output_payload.get("path")
            file_type = output_payload.get("type")
            if path:
                return f"Read {file_type or 'file'} `{path}`."
        if tool_name == "github.file.search":
            files = output_payload.get("files")
            if isinstance(files, list):
                return f"Found {len(files)} matching file(s)."
        if tool_name == "codex.task.run":
            changed_files = output_payload.get("changed_files")
            session_id = output_payload.get("session_id")
            pr_url = output_payload.get("pr_url")
            final_message = str(output_payload.get("final_message") or "").strip()
            details = "Ran a local Codex coding task"
            if isinstance(changed_files, list):
                details += f" with {len(changed_files)} changed file(s)"
            if pr_url:
                details += f" and opened PR {pr_url}"
            if session_id:
                details += f" in session {session_id}"
            if final_message:
                details += f": {final_message[:180]}"
            return details + "."
        if tool_name == "local.app.reload":
            commands = output_payload.get("commands")
            target = output_payload.get("target_path")
            if isinstance(commands, list):
                return f"Reloaded local app at {target or 'configured target'} with {len(commands)} command(s)."
            return "Reloaded local app."
        return ""

    def _chat_summary(
        self,
        plan: MaestroPlan,
        child_runs: list[AgentRunResult],
        *,
        status: str,
        tool_activity: list[dict[str, Any]],
    ) -> str:
        if not child_runs:
            return f"I finished the workflow with status `{status}`, but no child agents produced reports."

        parsed_outputs = [
            parsed
            for parsed in (self._parse_agent_output(run.output_text) for run in child_runs)
            if parsed is not None
        ]
        primary = parsed_outputs[0] if parsed_outputs else {}
        conversation = self._conversation_from_output(primary)
        summary = primary.get("summary") if isinstance(primary.get("summary"), dict) else {}
        latest_pr = summary.get("latest_pr") if isinstance(summary.get("latest_pr"), dict) else {}
        change_summary = summary.get("change_summary")
        ci_status = summary.get("ci_status")
        manual_test = self._manual_test_summary(primary)
        failed_tools = [
            item
            for item in tool_activity
            if item.get("status") == "failed" and item.get("tool_name") != "llm.tool_planner"
        ]
        approval_tools = [
            item
            for item in tool_activity
            if item.get("status") == "approval_required"
        ]

        lines: list[str] = []
        if conversation:
            lines.append(conversation)
        elif latest_pr:
            number = latest_pr.get("number")
            title = latest_pr.get("title")
            status_text = latest_pr.get("status")
            if number and title:
                line = f"I checked PR #{number}, `{title}`"
                if status_text:
                    line += f" ({status_text})"
                line += "."
                lines.append(line)
        if not lines:
            completed = sum(1 for run in child_runs if run.status == "completed")
            lines.append(
                self._workflow_completion_lead(
                    child_runs=child_runs,
                    completed=completed,
                    status=status,
                )
            )

        if conversation:
            pass
        elif isinstance(change_summary, str) and change_summary.strip():
            lines.append(change_summary.strip())
        else:
            agent_summaries = self._agent_completion_summaries(child_runs)
            if agent_summaries:
                lines.append("What came back:\n" + "\n".join(agent_summaries[:4]))

        if isinstance(ci_status, str) and ci_status.strip():
            lines.append(f"Validation note: {ci_status.strip()}")
        elif failed_tools:
            failed = "; ".join(
                f"{item.get('tool_name')}: {item.get('error_message')}"
                for item in failed_tools[:3]
            )
            lines.append(f"Tool note: {failed}")

        if approval_tools:
            proposed = ", ".join(str(item.get("tool_name")) for item in approval_tools[:3])
            lines.append(f"I did not run {proposed}; those actions need your approval first.")

        if manual_test:
            lines.append(f"Manual test I recommend: {manual_test}")

        fallback = "\n\n".join(lines)
        return self._conversational_completion_summary(
            plan=plan,
            child_runs=child_runs,
            status=status,
            fallback=fallback,
            tool_activity=tool_activity,
        )

    def _conversational_completion_summary(
        self,
        *,
        plan: MaestroPlan,
        child_runs: list[AgentRunResult],
        status: str,
        fallback: str,
        tool_activity: list[dict[str, Any]],
    ) -> str:
        settings = get_settings()
        if settings.llm_provider == "openrouter" and not settings.openrouter_api_key:
            return fallback
        if settings.llm_provider == "openai" and not settings.openai_api_key:
            return fallback
        agent_evidence = []
        for run in child_runs[:4]:
            agent_evidence.append(
                {
                    "agent": run.agent.name,
                    "status": run.status,
                    "report_excerpt": self._plain_text_preview(
                        run.output_text or run.execution_note or "",
                        max_chars=1200,
                    ),
                    "error": run.error_message,
                }
            )
        instructions = (
            "You are Maestro speaking directly to Chris after a workflow run. Write like a capable "
            "human assistant: concise, conversational, and clear about what matters. Do not simply "
            "regurgitate the agent report. Mention the outcome, the most important finding, any "
            "required action from Chris, and whether a report/artifact is available. Use first person "
            "singular for Maestro. Keep it under 180 words. Use clean Markdown paragraphs or bullets."
        )
        input_text = json.dumps(
            {
                "workflow": plan.summary,
                "status": status,
                "fallback_summary": fallback,
                "agent_evidence": agent_evidence,
                "tool_activity": tool_activity[:8],
            },
            default=str,
        )
        try:
            response = OpenAILLMClient().text_response(
                instructions=instructions,
                input_text=input_text,
            )
        except (LLMClientError, OSError, ValueError):
            return fallback
        return _strip_hidden_context(response.strip()) or fallback

    def _workflow_completion_lead(
        self,
        *,
        child_runs: list[AgentRunResult],
        completed: int,
        status: str,
    ) -> str:
        if status == "completed":
            if len(child_runs) == 1:
                return "I finished the workflow and brought the agent's findings back here."
            return (
                f"I finished the workflow and brought back reports from {completed} "
                "delegated agent tasks."
            )
        if completed:
            return (
                f"The workflow is `{status}`. {completed} delegated agent task"
                f"{'' if completed == 1 else 's'} completed, and I brought back what is available."
            )
        return f"The workflow finished with status `{status}`."

    def _agent_completion_summaries(self, child_runs: list[AgentRunResult]) -> list[str]:
        summaries: list[str] = []
        for run in child_runs:
            text = (run.output_text or run.execution_note or "").strip()
            if not text or self._looks_like_json(text):
                continue
            summary = self._markdown_section(text, "Summary") or text
            findings = self._markdown_section(text, "Findings")
            next_steps = self._markdown_section(text, "Next Steps")
            parts = [self._plain_text_preview(summary, max_chars=260)]
            if findings:
                parts.append("Found: " + self._plain_text_preview(findings, max_chars=220))
            if next_steps:
                parts.append("Next: " + self._plain_text_preview(next_steps, max_chars=180))
            summaries.append(f"- {run.agent.name}: {' '.join(part for part in parts if part)}")
        return summaries

    def _markdown_section(self, text: str, heading: str) -> str | None:
        pattern = re.compile(
            rf"^##+\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##+\s+|\Z)",
            flags=re.IGNORECASE | re.MULTILINE,
        )
        match = pattern.search(text)
        if not match:
            return None
        content = match.group(1).strip()
        return content or None

    def _conversation_from_output(self, parsed_output: dict[str, Any]) -> str | None:
        for key in ("conversation", "conversational_summary", "chat_summary"):
            value = parsed_output.get(key)
            if isinstance(value, str) and value.strip():
                return self._plain_text_preview(value.strip(), max_chars=900)
        return None

    def _parse_agent_output(self, output_text: str | None) -> dict[str, Any] | None:
        if not output_text:
            return None
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _manual_test_summary(self, parsed_output: dict[str, Any]) -> str | None:
        next_steps = parsed_output.get("next_steps")
        if not isinstance(next_steps, list):
            return None
        for item in next_steps:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "")
            if "manual" not in title.lower() and "test" not in title.lower():
                continue
            steps = item.get("steps")
            expected = item.get("expected_outcome")
            step_text = ""
            if isinstance(steps, list) and steps:
                step_text = " ".join(str(step).strip() for step in steps[:3] if str(step).strip())
            expected_text = ""
            if isinstance(expected, list) and expected:
                expected_text = " Expect " + "; ".join(
                    str(outcome).strip() for outcome in expected[:2] if str(outcome).strip()
                )
            summary = f"{title}. {step_text}{expected_text}".strip()
            return self._plain_text_preview(summary, max_chars=600) if summary else None
        return None

    def _looks_like_json(self, value: str) -> bool:
        stripped = value.strip()
        return stripped.startswith("{") or stripped.startswith("[")

    def _plain_text_preview(self, value: str, *, max_chars: int) -> str:
        text = " ".join(value.replace("```", "").split())
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "…"

    def _stage_workflow_artifact(
        self,
        parent_task: Task,
        plan: MaestroPlan,
        child_runs: list[AgentRunResult],
        synthesis: str,
        report: Report,
        phase_syntheses: list[dict[str, Any]] | None = None,
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
                item.description
                for item in plan.work_items
                if item.type == "rfi" or item.needs_user_input
            ],
            next_steps=["Review workflow synthesis.", "Run memory curation on this artifact."],
            task_id=str(parent_task.id),
            provenance={
                "plan_id": plan.plan_id,
                "workflow_key": parent_task.workflow_key,
                "child_task_ids": [run.task_id for run in child_runs],
                "canonical_workflow_artifact": True,
                "planner_mode": plan.planner_mode,
                "work_items": [item.__dict__ for item in plan.work_items],
                "subtasks": [subtask.__dict__ for subtask in plan.subtasks],
                "phase_syntheses": phase_syntheses or [],
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
