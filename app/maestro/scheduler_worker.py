"""Durable scheduler worker for autonomous Maestro work.

The scheduler separates "work exists" from "work is executing". Definitions create workflow runs,
runs contain queue items, and this worker claims ready items while honoring scheduler state. It is
safe to call `run_once` from tests, API buttons, or the app heartbeat; each call performs one small
unit of queue adjudication and agent execution.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.agents.runtime import (
    AgentRuntimeError,
    InteractionArtifactPackager,
    PromptAggregationService,
    PromptPackageRequest,
)
from app.core.config import get_settings
from app.db.models import Agent, Artifact, Domain, Report, RuntimeSetting, WorkflowQueueItem, WorkflowRun
from app.maestro.channel import record_channel_message
from app.maestro.scheduler import SchedulerService

SCHEDULER_WORKER_SETTING_KEY = "scheduler_worker"


def scheduler_worker_settings(session: Session) -> dict[str, Any]:
    settings = get_settings()
    defaults: dict[str, Any] = {
        "enabled": settings.scheduler_worker_autorun,
        "interval_seconds": settings.scheduler_worker_interval_seconds,
        "claim_limit": settings.scheduler_worker_claim_limit,
        "execute_llm": settings.scheduler_worker_execute_llm,
        "auto_tool_loop": settings.scheduler_worker_auto_tool_loop,
        "source": "env",
    }
    stored = session.get(RuntimeSetting, SCHEDULER_WORKER_SETTING_KEY)
    if stored is None:
        return defaults
    payload = stored.value or {}
    return {
        **defaults,
        **{
            key: payload[key]
            for key in (
                "enabled",
                "interval_seconds",
                "claim_limit",
                "execute_llm",
                "auto_tool_loop",
            )
            if key in payload
        },
        "source": "runtime",
    }


def update_scheduler_worker_settings(
    session: Session,
    *,
    enabled: bool | None = None,
    interval_seconds: int | None = None,
    claim_limit: int | None = None,
    execute_llm: bool | None = None,
    auto_tool_loop: bool | None = None,
) -> dict[str, Any]:
    current = scheduler_worker_settings(session)
    updates = {
        "enabled": enabled,
        "interval_seconds": interval_seconds,
        "claim_limit": claim_limit,
        "execute_llm": execute_llm,
        "auto_tool_loop": auto_tool_loop,
    }
    for key, value in updates.items():
        if value is not None:
            current[key] = value

    stored = session.get(RuntimeSetting, SCHEDULER_WORKER_SETTING_KEY)
    if stored is None:
        stored = RuntimeSetting(key=SCHEDULER_WORKER_SETTING_KEY, value={})
        session.add(stored)
    stored.value = {
        "enabled": bool(current["enabled"]),
        "interval_seconds": int(current["interval_seconds"]),
        "claim_limit": int(current["claim_limit"]),
        "execute_llm": bool(current["execute_llm"]),
        "auto_tool_loop": bool(current["auto_tool_loop"]),
    }
    session.commit()
    return scheduler_worker_settings(session)


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
            self._post_channel_update(
                run,
                item,
                status="blocked",
                message=(
                    f"Scheduled workflow `{self._run_title(run)}` is blocked because "
                    f"`{item.external_key}` has no assigned agent."
                ),
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
            self._post_channel_update(
                run,
                completed,
                status="completed",
                message=(
                    f"Scheduled workflow `{self._run_title(run)}` completed `{item.external_key}` "
                    f"through {agent.name}."
                ),
            )
            if run.status == "completed":
                self._stage_completed_workflow_run(run)
                self._post_workflow_completion_update(run)
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
            self._post_channel_update(
                run,
                blocked,
                status="blocked",
                message=(
                    f"Scheduled workflow `{self._run_title(run)}` is waiting on `{item.external_key}`: "
                    f"{blocked.error_message}"
                ),
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
        self._post_channel_update(
            run,
            failed,
            status="failed",
            message=(
                f"Scheduled workflow `{self._run_title(run)}` failed `{item.external_key}`: "
                f"{failed.error_message}"
            ),
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

    def _stage_completed_workflow_run(self, run: WorkflowRun) -> None:
        output_payload = run.output_payload or {}
        if output_payload.get("staged_artifact_path"):
            return
        domain_key = "maestro-development"
        if run.domain_id:
            domain = self.session.get(Domain, run.domain_id)
            if domain is not None:
                domain_key = domain.key
        queue_items = self.scheduler._queue_items_for_run(run.id)
        generated_artifacts: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        output_sections: list[str] = []
        for item in queue_items:
            item_output = item.output_payload or {}
            agent_run = item_output.get("agent_run") if isinstance(item_output.get("agent_run"), dict) else item_output
            generated_artifacts.append(
                {
                    "name": item.external_key,
                    "type": "scheduled_queue_item",
                    "queue_item_id": str(item.id),
                    "task_id": agent_run.get("task_id"),
                    "report_id": agent_run.get("report_id"),
                    "staged_artifact_path": agent_run.get("staged_artifact_path"),
                    "artifact_id": agent_run.get("artifact_id"),
                }
            )
            if isinstance(agent_run.get("tool_calls"), list):
                tool_calls.extend(agent_run["tool_calls"])
            preview = str(
                agent_run.get("output_preview")
                or agent_run.get("execution_note")
                or ""
            ).strip()
            if preview:
                output_sections.append(f"## {item.external_key}\n{preview}")
        package = InteractionArtifactPackager(self.session).build_package(
            domain_key=domain_key,
            agent_key=None,
            user_input=str((run.input_payload or {}).get("summary") or self._run_title(run)),
            maestro_tasking=str((run.input_payload or {}).get("summary") or "Scheduled Maestro workflow"),
            agent_output="\n\n".join(output_sections) or f"Scheduled workflow {run.id} completed.",
            tool_calls=tool_calls,
            generated_artifacts=generated_artifacts,
            next_steps=["Curate durable context from this scheduled workflow artifact."],
            provenance={
                "workflow_run_id": str(run.id),
                "workflow_definition_id": str(run.workflow_definition_id) if run.workflow_definition_id else None,
                "canonical_scheduled_workflow_artifact": True,
                "source_type": run.source_type,
            },
        )
        staged = InteractionArtifactPackager(self.session).stage_package(package)
        artifact = self.session.get(Artifact, uuid.UUID(staged.artifact_id or ""))
        if artifact is not None:
            artifact.metadata_ = {
                **(artifact.metadata_ or {}),
                "workflow_run_id": str(run.id),
                "workflow_definition_id": str(run.workflow_definition_id) if run.workflow_definition_id else None,
                "canonical_scheduled_workflow_artifact": True,
            }
        run.output_payload = {
            **output_payload,
            "staged_artifact_path": staged.path,
            "artifact_id": staged.artifact_id,
        }
        self.session.commit()

    def _post_workflow_completion_update(self, run: WorkflowRun) -> None:
        self.session.refresh(run)
        output_payload = run.output_payload or {}
        if output_payload.get("completion_channel_message_posted"):
            return
        queue_items = self.scheduler._queue_items_for_run(run.id)
        summaries: list[str] = []
        for item in queue_items[:4]:
            item_output = item.output_payload or {}
            agent_run = item_output.get("agent_run") if isinstance(item_output.get("agent_run"), dict) else item_output
            preview = str(
                agent_run.get("output_preview")
                or agent_run.get("execution_note")
                or f"Finished with status {agent_run.get('status') or item.status}."
            ).strip()
            agent_name = str(agent_run.get("agent_name") or item.external_key)
            summaries.append(f"- {agent_name}: {self._plain_text_preview(preview, max_chars=260)}")
        message = f"I finished scheduled workflow `{self._run_title(run)}`."
        if summaries:
            message += "\n\nWhat came back:\n" + "\n".join(summaries)
        else:
            message += " No agent report text was available, but the queue items completed."
        record_channel_message(
            self.session,
            sender="maestro",
            content=message,
            metadata={
                "source": "scheduler_worker",
                "status": "completed",
                "workflow_run_id": str(run.id),
                "workflow_definition_id": str(run.workflow_definition_id) if run.workflow_definition_id else None,
                "event_type": "workflow_completed",
            },
        )
        run.output_payload = {
            **(run.output_payload or {}),
            "completion_channel_message_posted": True,
        }
        self.session.commit()

    def _post_channel_update(
        self,
        run: WorkflowRun,
        item: WorkflowQueueItem,
        *,
        status: str,
        message: str,
    ) -> None:
        record_channel_message(
            self.session,
            sender="maestro",
            content=message,
            metadata={
                "source": "scheduler_worker",
                "status": status,
                "workflow_run_id": str(run.id),
                "workflow_definition_id": str(run.workflow_definition_id) if run.workflow_definition_id else None,
                "queue_item_id": str(item.id),
                "queue_item_key": item.external_key,
            },
        )

    def _run_title(self, run: WorkflowRun) -> str:
        return str((run.input_payload or {}).get("summary") or run.id)

    def _plain_text_preview(self, value: str, *, max_chars: int) -> str:
        text = " ".join(value.replace("```", "").split())
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "…"
