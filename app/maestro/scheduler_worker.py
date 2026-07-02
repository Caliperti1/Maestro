import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.agents.runtime import (
    AgentRuntimeError,
    PromptAggregationService,
    PromptPackageRequest,
)
from app.db.models import Agent, Report, WorkflowQueueItem, WorkflowRun
from app.maestro.scheduler import SchedulerService


class SchedulerWorkerService:
    def __init__(
        self,
        session: Session,
        *,
        runtime: PromptAggregationService | None = None,
    ):
        self.session = session
        self.scheduler = SchedulerService(session)
        self.runtime = runtime or PromptAggregationService(session)

    def run_once(
        self,
        *,
        owner: str = "maestro-worker",
        claim_limit: int = 4,
        lease_seconds: int = 900,
        execute_llm: bool = True,
        auto_tool_loop: bool = True,
        max_tool_iterations: int = 2,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        enqueued = self.scheduler.enqueue_due_workflows(now=now)
        claimed = self.scheduler.claim_ready_items(
            owner=owner,
            limit=claim_limit,
            lease_seconds=lease_seconds,
        )
        executed = [
            self.execute_queue_item(
                item.id,
                execute_llm=execute_llm,
                auto_tool_loop=auto_tool_loop,
                max_tool_iterations=max_tool_iterations,
            )
            for item in claimed
        ]
        return {
            "enqueued": [self.scheduler.workflow_run_payload(run) for run in enqueued],
            "claimed": [self.scheduler.queue_item_payload(item) for item in claimed],
            "executed": executed,
            "runnable_batches": self.scheduler.runnable_batches(),
        }

    def execute_queue_item(
        self,
        queue_item_id: uuid.UUID,
        *,
        execute_llm: bool = True,
        auto_tool_loop: bool = True,
        max_tool_iterations: int = 2,
    ) -> dict[str, Any]:
        item = self.session.get(WorkflowQueueItem, queue_item_id)
        if item is None:
            raise ValueError("Unknown queue item.")
        run = self.session.get(WorkflowRun, item.workflow_run_id)
        if run is None:
            raise ValueError("Queue item has no workflow run.")
        agent = self.session.get(Agent, item.agent_id) if item.agent_id else None
        if agent is None:
            blocked = self.scheduler.block_queue_item(
                item.id,
                error_message="No agent is assigned to this scheduled queue item.",
            )
            return {
                "queue_item": self.scheduler.queue_item_payload(blocked),
                "agent_run": None,
                "status": "blocked",
            }

        try:
            agent_run = self.runtime.run_agent_once(
                PromptPackageRequest(
                    agent_key=agent.key,
                    task_instruction=item.objective,
                    caller="system",
                    user_context=self._worker_user_context(run, item),
                    query_text=item.objective,
                    use_semantic=True,
                ),
                stage_interaction=True,
                execute_llm=execute_llm,
                auto_tool_loop=auto_tool_loop,
                max_tool_iterations=max_tool_iterations,
                parent_task_id=item.parent_task_id,
                source_type="scheduler_worker",
                workflow_key="scheduler.workflow_item",
                priority=item.priority,
            )
        except AgentRuntimeError as exc:
            failed = self.scheduler.fail_queue_item(item.id, error_message=str(exc))
            return {
                "queue_item": self.scheduler.queue_item_payload(failed),
                "agent_run": None,
                "status": "failed",
            }

        if agent_run.status == "completed" or agent_run.status == "prepared":
            completed = self.scheduler.complete_queue_item(
                item.id,
                output_payload=self._agent_run_payload(agent_run),
            )
            self.scheduler.record_event(
                run,
                queue_item=completed,
                event_type="queue_item_agent_run_completed",
                message=f"Scheduled worker completed `{item.external_key}` through {agent.name}.",
                payload=self._agent_run_payload(agent_run),
            )
            return {
                "queue_item": self.scheduler.queue_item_payload(completed),
                "agent_run": self._agent_run_payload(agent_run),
                "status": "completed",
            }
        if agent_run.status == "blocked":
            blocked = self.scheduler.block_queue_item(
                item.id,
                error_message=agent_run.error_message or "Agent run is blocked.",
                output_payload=self._agent_run_payload(agent_run),
            )
            return {
                "queue_item": self.scheduler.queue_item_payload(blocked),
                "agent_run": self._agent_run_payload(agent_run),
                "status": "blocked",
            }
        failed = self.scheduler.fail_queue_item(
            item.id,
            error_message=agent_run.error_message or f"Agent run finished with status {agent_run.status}.",
        )
        return {
            "queue_item": self.scheduler.queue_item_payload(failed),
            "agent_run": self._agent_run_payload(agent_run),
            "status": "failed",
        }

    def _worker_user_context(self, run: WorkflowRun, item: WorkflowQueueItem) -> str:
        context = {
            "workflow_run_id": str(run.id),
            "workflow_definition_id": str(run.workflow_definition_id) if run.workflow_definition_id else None,
            "source_type": run.source_type,
            "summary": (run.input_payload or {}).get("summary"),
            "event": (run.input_payload or {}).get("event"),
            "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None,
            "queue_item": {
                "external_key": item.external_key,
                "stage_index": item.stage_index,
                "dependency_keys": item.dependency_keys,
                "resource_locks": item.resource_locks,
            },
            "completed_dependencies": self._completed_dependency_outputs(item),
        }
        return (
            "This task is being executed by Maestro's scheduler worker. Use the workflow "
            "context below as authoritative scheduling context.\n\n"
            f"{json.dumps(context, indent=2)}"
        )

    def _completed_dependency_outputs(self, item: WorkflowQueueItem) -> list[dict[str, Any]]:
        if not item.dependency_keys:
            return []
        siblings = {
            sibling.external_key: sibling
            for sibling in self.scheduler._queue_items_for_run(item.workflow_run_id)
        }
        outputs: list[dict[str, Any]] = []
        for key in item.dependency_keys:
            dependency = siblings.get(key)
            if dependency is None:
                continue
            output_payload = dependency.output_payload or {}
            report_body = None
            report_id = output_payload.get("report_id")
            if report_id:
                report = self.session.get(Report, uuid.UUID(str(report_id)))
                if report is not None:
                    report_body = report.body_markdown
            outputs.append(
                {
                    "external_key": dependency.external_key,
                    "status": dependency.status,
                    "output_payload": output_payload,
                    "report_body": report_body,
                }
            )
        return outputs

    def _agent_run_payload(self, agent_run) -> dict[str, Any]:
        return {
            "run_id": agent_run.run_id,
            "status": agent_run.status,
            "agent_key": agent_run.agent.key,
            "agent_name": agent_run.agent.name,
            "task_id": agent_run.task_id,
            "report_id": agent_run.report_id,
            "execution_note": agent_run.execution_note,
            "output_preview": (agent_run.output_text or "")[:500],
            "tool_calls": agent_run.tool_calls,
            "staged_artifact_path": agent_run.staged_artifact_path,
            "artifact_id": agent_run.artifact_id,
            "error_message": agent_run.error_message,
            "completed_at": datetime.now(UTC).isoformat(),
        }
