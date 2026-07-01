import json
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
from app.db.models import Artifact, Report, Task, ToolCall
from app.core.config import get_settings
from app.llm.client import LLMClient, LLMClientError, OpenAILLMClient
from app.maestro.planner import (
    LLMMaestroPlanner,
    MaestroPlannerResponse,
    PlannerWorkItem,
)
from app.memory.retrieval import MemoryContextBundleRequest, MemoryRetrievalService
from app.tools.runtime import ToolExecutionResult, ToolExecutionService, tool_result_payload

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

    def create_plan(self, user_input: str) -> MaestroPlan:
        cleaned_input = user_input.strip()
        if not cleaned_input:
            raise MaestroOrchestratorError("Maestro input cannot be blank.")

        agents = self.registry.list_specs()
        domains = self.registry.list_domain_contexts()
        tools = self.registry.list_tools()
        registry_snapshot = self._registry_snapshot(domains, agents, tools)
        decomposition, planner_mode = self._decompose_request(
            cleaned_input,
            registry_snapshot=registry_snapshot,
        )
        work_items = self._harden_work_items(
            [self._work_item_from_planner(item) for item in decomposition.work_items]
        )
        is_chat_only = self._is_chat_only(work_items, decomposition)
        selected_agents = self._select_agents_for_work_items(work_items, agents)
        intents = self._intents_from_work_items(work_items, selected_agents)
        subtasks = self._build_subtasks(cleaned_input, selected_agents, intents, work_items)
        execution_stages = self._execution_stage_keys(subtasks)
        workflow_graph = self._workflow_graph(work_items, subtasks)
        queue_items = self._queue_items(subtasks)
        summary = decomposition.plan_summary or self._plan_summary(cleaned_input, intents, subtasks)
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
                "planner_mode": planner_mode,
                "work_items": [work_item.__dict__ for work_item in work_items],
                "intents": [intent.__dict__ for intent in intents],
                "subtasks": [subtask.__dict__ for subtask in subtasks],
                "execution_stages": execution_stages,
                "workflow_graph": workflow_graph,
                "is_chat_only": is_chat_only,
                "selected_agents": [
                    self._selected_agent_payload(agent, user_input=cleaned_input)
                    for agent in selected_agents
                ],
                "registry_snapshot": registry_snapshot,
                "approval_required": True,
                "scheduler": self._scheduler_payload(queue_items=queue_items),
                "direct_response": decomposition.direct_response,
                "planner_notes": decomposition.planner_notes,
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

    def refine_plan(self, plan_id: uuid.UUID | str, refinement: str) -> MaestroPlan:
        cleaned_refinement = refinement.strip()
        if not cleaned_refinement:
            raise MaestroOrchestratorError("Maestro refinement cannot be blank.")
        previous_plan = self.get_plan(plan_id)
        refined_input = self._refined_plan_input(previous_plan, cleaned_refinement)
        plan = self.create_plan(refined_input)
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
        runnable_parent_statuses = {"proposed", "queued", "failed"}
        if resume:
            runnable_parent_statuses.add("blocked")
        if parent_task.status not in runnable_parent_statuses:
            raise MaestroOrchestratorError(
                f"Plan cannot be run from status {parent_task.status}."
            )
        if plan.is_chat_only:
            raise MaestroOrchestratorError("Direct chat responses do not have an executable plan.")
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
                    child_runs.extend(run["attempts"])
                    final_run = run["final"]
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
            raise

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
    ) -> tuple[MaestroPlannerResponse, str]:
        planning_context = {
            "global_context": self.registry.get_global_context().context,
            "registry": registry_snapshot,
            "retrieved_memory": self._planning_memory_context(user_input),
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
            return (
                LLMMaestroPlanner(llm_client).decompose(
                    user_input=user_input,
                    planning_context=planning_context,
                ),
                planner_mode,
            )
        except Exception:
            return self._deterministic_decomposition(user_input, registry_snapshot), "deterministic"

    def _planning_memory_context(self, user_input: str) -> dict[str, Any]:
        try:
            bundle = MemoryRetrievalService(self.session).build_context_bundle(
                MemoryContextBundleRequest(
                    profile="agent_prompt",
                    audience="maestro",
                    query_text=user_input,
                    use_semantic=True,
                    max_items=8,
                    max_chars=2500,
                )
            )
        except Exception:
            return {"status": "unavailable", "rendered_text": ""}
        return {
            "status": bundle.semantic_status,
            "included_count": bundle.included_count,
            "rendered_text": bundle.rendered_text,
        }

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
        if not planning_only and any(
            token in lowered
            for token in ("implement", "code", "coding", "fix", "action issue", "work issue")
        ):
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
                    rationale="The request asks Maestro to execute coding work.",
                )
            )
        if any(token in lowered for token in ("plan", "prepare", "coordinate", "workflow")):
            work_items.append(
                PlannerWorkItem(
                    id=f"wi_{len(work_items) + 1}",
                    type="workflow_task",
                    title="Coordinate requested workflow",
                    description=user_input,
                    domain_key=domain_key,
                    priority="high",
                    required_capabilities=self._capabilities_from_text(lowered),
                    required_tools=[],
                    dependencies=[],
                    needs_agent=True,
                    needs_user_input=False,
                    blocks_execution=False,
                    can_log_directly=False,
                    suggested_agent_keys=[],
                    expected_output="Role-scoped workflow contribution and recommended next steps.",
                    rationale="The request asks Maestro to prepare or coordinate work.",
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
                    rationale="No workflow or routed operational item was detected.",
                )
            )
        return MaestroPlannerResponse(
            plan_summary=f"Proposed decomposition with {len(work_items)} work item(s): {user_input[:180]}",
            direct_response=None if any(item.needs_agent for item in work_items) else user_input,
            work_items=work_items,
            planner_notes="Deterministic fallback planner used because the LLM planner was unavailable.",
        )

    def _domain_for_input(self, lowered_input: str, registry_snapshot: dict[str, Any]) -> str | None:
        for domain in registry_snapshot.get("domains", []):
            key = str(domain.get("key") or "")
            if key and (key in lowered_input or key.replace("-", " ") in lowered_input):
                return key
            if any(token in lowered_input for token in _DOMAIN_HINTS.get(key, [])):
                return key
        return None

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
        return MaestroWorkItem(**item.model_dump())

    def _harden_work_items(self, work_items: list[MaestroWorkItem]) -> list[MaestroWorkItem]:
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
            ]
        ).lower()
        domain_noise = _meaningful_tokens(
            " ".join([agent.domain_key, agent.domain_key.replace("-", " "), "agent"])
        )
        overlap = (_meaningful_tokens(item_text) - domain_noise) & (
            _meaningful_tokens(agent_text) - domain_noise
        )
        score += min(len(overlap), 8) * 0.75
        agent_tool_keys = {tool.key for tool in agent.allowed_tools}
        score += len(agent_tool_keys & set(item.required_tools)) * 1.5
        if any(token in agent.key for token in ("planning", "chief", "manager", "lead")):
            score += 0.5
        return score

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
        work_items: list[MaestroWorkItem],
    ) -> list[MaestroSubtask]:
        subtasks: list[MaestroSubtask] = []
        for agent in selected_agents:
            assigned_items = self._work_items_for_agent(agent, work_items)
            if not assigned_items:
                continue
            grouped_items: dict[tuple[str, ...], list[MaestroWorkItem]] = {}
            assigned_item_ids = {item.id for item in assigned_items}
            for item in assigned_items:
                dependencies = tuple(
                    sorted(
                        dependency
                        for dependency in item.dependencies
                        if dependency not in assigned_item_ids
                    )
                )
                grouped_items.setdefault(dependencies, []).append(item)
            for dependencies, group_items in grouped_items.items():
                priority = "high" if any(item.priority in {"high", "urgent"} for item in group_items) else "normal"
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
                        depends_on_work_item_ids=list(dependencies),
                    )
                )
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
            top_score = best_score
            if score > 0 and score >= max(2.0, top_score * 0.5) and (
                top_score <= 3.5 or score > 3.0
            ):
                assigned.append(item)
        return assigned

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

    def _intents_from_work_items(
        self,
        work_items: list[MaestroWorkItem],
        selected_agents,
    ) -> list[MaestroIntent]:
        default_domain = selected_agents[0].domain_key if selected_agents else None
        intent_by_type: dict[str, MaestroIntent] = {}
        for item in work_items:
            intent_type = _INTENT_TYPE_BY_WORK_ITEM.get(item.type, "direct_chat")
            existing = intent_by_type.get(intent_type)
            if existing is not None:
                continue
            intent_by_type[intent_type] = MaestroIntent(
                type=intent_type,  # type: ignore[arg-type]
                summary=f"{item.type}: {item.title}",
                target=item.description[:180],
                domain_key=item.domain_key or default_domain,
                priority=item.priority,
                action=_ACTION_BY_WORK_ITEM_TYPE.get(item.type, item.rationale),
            )
        if not intent_by_type:
            return self._classify_intents(" ".join(item.description for item in work_items), selected_agents)
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

    def _scheduler_payload(
        self,
        *,
        queue_items: list[dict[str, Any]] | None = None,
        status: str = "queue_foundation",
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
            "current_step": "Not started.",
            "queue_items": queue_items or [],
        }

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
        priority_statuses = ("running", "retrying", "ready", "blocked", "pending", "failed")
        for status in priority_statuses:
            item = next((item for item in queue_items if item.get("status") == status), None)
            if item:
                agent_name = item.get("agent_name") or item.get("agent_key") or "agent"
                if status == "blocked":
                    current_step = f"Waiting on {agent_name}: {item.get('error_message') or 'blocked'}"
                elif status == "pending":
                    current_step = f"Queued: {agent_name}"
                elif status == "failed":
                    current_step = f"Failed: {agent_name}"
                else:
                    current_step = f"{status.title()}: {agent_name}"
                return {
                    "active_stage_index": item.get("stage_index"),
                    "active_queue_item_id": item.get("id"),
                    "current_step": current_step,
                }
        return {
            "active_stage_index": None,
            "active_queue_item_id": None,
            "current_step": "Workflow complete.",
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
                "New user refinement:",
                refinement,
                "",
                "Return an updated plan that preserves still-valid work, removes obsolete work, "
                "and incorporates the refinement without treating this as an unrelated new session.",
            ]
        )

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
                MaestroWorkItem(**work_item)
                for work_item in payload.get("work_items", [])
            ],
            intents=[MaestroIntent(**intent) for intent in payload.get("intents", [])],
            subtasks=[MaestroSubtask(**subtask) for subtask in payload.get("subtasks", [])],
            execution_stages=list(payload.get("execution_stages", [])),
            workflow_graph=dict(payload.get("workflow_graph", {})),
            is_chat_only=bool(payload.get("is_chat_only", False)),
            selected_agents=list(payload.get("selected_agents", [])),
            registry_snapshot=dict(payload.get("registry_snapshot", {})),
            approval_required=bool(payload.get("approval_required", True)),
            scheduler=dict(payload.get("scheduler", self._scheduler_payload())),
            created_at=task.created_at.isoformat() if task.created_at else "",
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
                    }
                )
        return activity

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
            final_message = str(output_payload.get("final_message") or "").strip()
            details = "Ran a local Codex coding task"
            if isinstance(changed_files, list):
                details += f" with {len(changed_files)} changed file(s)"
            if session_id:
                details += f" in session {session_id}"
            if final_message:
                details += f": {final_message[:180]}"
            return details + "."
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
                f"I ran {len(child_runs)} delegated agent task(s); {completed} completed and the workflow status is `{status}`."
            )

        if conversation:
            pass
        elif isinstance(change_summary, str) and change_summary.strip():
            lines.append(change_summary.strip())
        else:
            agent_summaries = [
                (run.output_text or run.execution_note or "").strip()
                for run in child_runs
                if (run.output_text or run.execution_note)
                and not self._looks_like_json(run.output_text or "")
            ]
            if agent_summaries:
                lines.append(self._plain_text_preview(agent_summaries[0], max_chars=450))

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

        return "\n\n".join(lines)

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


_DOMAIN_HINTS = {
    "personal": ["personal", "calendar", "family", "life", "preference"],
    "maestro-development": ["maestro", "orchestrator", "agent", "memory", "system", "code"],
    "praxis": ["praxis", "partner", "tactical innovation", "transition", "training"],
    "ophi": ["ophi", "product", "market", "research"],
    "usma": ["usma", "cadet", "class", "teaching", "academic"],
    "personal-irad-projects": ["irad", "prototype", "project"],
    "l3": ["l3"],
}

_INTENT_TYPE_BY_WORK_ITEM = {
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

_ACTION_BY_WORK_ITEM_TYPE = {
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
