"""Durable scheduler worker for autonomous Maestro work.

The scheduler separates "work exists" from "work is executing". Definitions create workflow runs,
runs contain queue items, and this worker claims ready items while honoring scheduler state. It is
safe to call `run_once` from tests, API buttons, or the app heartbeat; each call performs one small
unit of queue adjudication and agent execution.
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runtime import (
    AgentRuntimeError,
    InteractionArtifactPackager,
    PromptAggregationService,
    PromptPackageRequest,
)
from app.core.config import get_settings
from app.db.models import Agent, Artifact, Domain, Report, RuntimeSetting, Task, WorkflowQueueItem, WorkflowRun
from app.maestro.channel import record_channel_message
from app.maestro.scheduler import SchedulerService
from app.maestro.workflow_outputs import WorkflowOutputService
from app.tools.runtime import ToolExecutionRequest, ToolExecutionService, tool_result_payload

SCHEDULER_WORKER_SETTING_KEY = "scheduler_worker"
logger = logging.getLogger(__name__)


def _queue_item_required_skills(item: WorkflowQueueItem) -> list[str]:
    payload = item.input_payload or {}
    skills = payload.get("required_skills")
    if not isinstance(skills, list):
        return []
    return [str(skill).strip() for skill in skills if str(skill).strip()]


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


def _queue_item_model_profile(
    run: WorkflowRun,
    item: WorkflowQueueItem,
    agent: Agent | None = None,
) -> str | None:
    item_payload = item.input_payload or {}
    item_profile = str(item_payload.get("model_profile") or "").strip()
    if item_profile:
        return item_profile
    run_payload = run.input_payload or {}
    workflow_spec = run_payload.get("workflow_spec") if isinstance(run_payload.get("workflow_spec"), dict) else {}
    spec_profile = str(workflow_spec.get("model_profile") or run_payload.get("model_profile") or "").strip()
    if spec_profile:
        return spec_profile
    if agent is not None:
        capabilities = agent.capabilities or {}
        agent_profile = str(capabilities.get("model_profile") or "").strip()
        if agent_profile and agent_profile != "default":
            return agent_profile
    return None


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
        max_tool_iterations: int = 4,
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
        max_tool_iterations: int = 4,
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
                    required_skills=_queue_item_required_skills(item),
                    model_profile=_queue_item_model_profile(run, item, agent),
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
            delivery_review = self._propose_coding_delivery_review(item, agent_run)
            if delivery_review is not None:
                blocked = self.scheduler.block_queue_item(
                    item.id,
                    error_message=delivery_review["message"],
                    output_payload={
                        "agent_run": self._agent_run_payload(agent_run),
                        "delivery_review": delivery_review["tool_call"],
                    },
                )
                self.scheduler.record_event(
                    run,
                    queue_item=blocked,
                    event_type="coding_pr_review_required",
                    message=delivery_review["message"],
                    payload=delivery_review["tool_call"],
                )
                self._post_channel_update(
                    run,
                    blocked,
                    status="blocked",
                    message=delivery_review["message"],
                )
                return {
                    "queue_item": self.scheduler.queue_item_payload(blocked),
                    "agent_run": self._agent_run_payload(agent_run),
                    "status": "blocked",
                }
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
            self.session.refresh(run)
            if run.status == "completed":
                self._finalize_completed_workflow_run(run)
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

    def _propose_coding_delivery_review(self, item: WorkflowQueueItem, agent_run) -> dict[str, Any] | None:
        pr_number = _coding_pr_number(agent_run.tool_calls)
        if pr_number is None or not agent_run.task_id:
            return None
        try:
            task_id = uuid.UUID(str(agent_run.task_id))
        except (TypeError, ValueError):
            return None
        task = self.session.get(Task, task_id)
        if task is None:
            return None
        proposed = ToolExecutionService(self.session).propose_for_task(
            ToolExecutionRequest(
                agent_key=agent_run.agent.key,
                tool_key="local.app.deploy_pr",
                payload={"pr_number": pr_number, "method": "squash", "delete_branch": True},
            ),
            task=task,
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
        task.status = "blocked"
        task.error_message = f"Waiting for Chris to review PR #{pr_number} and approve delivery."
        task.completed_at = None
        self.session.commit()
        return {
            "tool_call": tool_result_payload(proposed),
            "message": (
                f"PR #{pr_number} is ready for review. This workflow is paused until you approve "
                "merging it and updating the dedicated Maestro runtime."
            ),
        }

    def complete_approved_delivery(
        self,
        *,
        task_id: uuid.UUID,
        delivery_result: dict[str, Any],
    ) -> WorkflowRun | None:
        for item in self.session.scalars(select(WorkflowQueueItem)).all():
            agent_run = (item.output_payload or {}).get("agent_run")
            if not isinstance(agent_run, dict) or str(agent_run.get("task_id")) != str(task_id):
                continue
            task = self.session.get(Task, task_id)
            if task is not None:
                task.status = "completed"
                task.error_message = None
                task.completed_at = datetime.now(UTC)
            completed = self.scheduler.complete_queue_item(
                item.id,
                output_payload={"delivery": delivery_result},
            )
            run = self.session.get(WorkflowRun, completed.workflow_run_id)
            if run is not None and run.status == "completed":
                self._finalize_completed_workflow_run(run)
            return run
        return None

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
                "required_skills": _queue_item_required_skills(item),
                "model_profile": _queue_item_model_profile(run, item),
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
        queue_items = [
            item
            for item in self.scheduler._queue_items_for_run(run.id)
            if item.status != "archived"
        ]
        domain_key = "maestro-development"
        domain_id = run.domain_id
        if domain_id is None:
            queue_domain_ids = {item.domain_id for item in queue_items if item.domain_id is not None}
            if len(queue_domain_ids) == 1:
                domain_id = next(iter(queue_domain_ids))
        if domain_id is not None:
            domain = self.session.get(Domain, domain_id)
            if domain is not None:
                domain_key = domain.key
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

    def _finalize_completed_workflow_run(self, run: WorkflowRun) -> None:
        """Persist independent completion outputs without letting one suppress the others."""
        run_id = run.id
        try:
            self._stage_completed_workflow_run(run)
        except Exception:
            self.session.rollback()
            logger.exception("Could not stage completion artifact for workflow run %s", run_id)
            run = self.session.get(WorkflowRun, run_id)
            if run is None:
                return

        completion_message = ""
        try:
            completion_message = self._post_workflow_completion_update(run)
        except Exception:
            self.session.rollback()
            logger.exception("Could not post completion update for workflow run %s", run_id)
            run = self.session.get(WorkflowRun, run_id)
            if run is None:
                return

        try:
            WorkflowOutputService(self.session).record_run_log(run)
        except Exception:
            self.session.rollback()
            logger.exception("Could not record run log for workflow run %s", run_id)
            run = self.session.get(WorkflowRun, run_id)
            if run is None:
                return

        parent_task = self.session.get(Task, run.parent_task_id) if run.parent_task_id else None
        if parent_task is not None:
            parent_task.status = "completed"
            parent_task.completed_at = run.completed_at or datetime.now(UTC)
            parent_task.output_payload = {
                **(parent_task.output_payload or {}),
                "status": "completed",
                "chat_summary": completion_message or (parent_task.output_payload or {}).get("chat_summary"),
            }
            self.session.commit()

    def _post_workflow_completion_update(self, run: WorkflowRun) -> str:
        self.session.refresh(run)
        output_payload = run.output_payload or {}
        message = str(output_payload.get("completion_channel_message") or "")
        if not message:
            queue_items = [
                item
                for item in self.scheduler._queue_items_for_run(run.id)
                if item.status != "archived"
            ]
            message = self._delivery_completion_message(run, queue_items)
        if not message:
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
            run_kind = "scheduled workflow" if run.workflow_definition_id else "workflow"
            message = f"I finished the {run_kind} `{self._run_title(run)}`."
            if summaries:
                message += "\n\nWhat came back:\n" + "\n".join(summaries)
            else:
                message += " No agent report text was available, but the queue items completed."

        if not output_payload.get("completion_channel_message_posted"):
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
                "completion_channel_message": message,
            }
            self.session.commit()

        self.session.refresh(run)
        if not (run.output_payload or {}).get("completion_notification_posted"):
            WorkflowOutputService(self.session).create_notification(
                run,
                title=f"Workflow completed: {self._run_title(run)}",
                message=message,
                severity="info",
                notification_type="workflow_completed",
                status="delivered",
                delivered_at=datetime.now(UTC),
                metadata={
                    "source": "scheduler_worker",
                    "workflow_definition_id": str(run.workflow_definition_id) if run.workflow_definition_id else None,
                },
            )
            run.output_payload = {
                **(run.output_payload or {}),
                "completion_notification_posted": True,
            }
            self.session.commit()
        return message

    def _delivery_completion_message(
        self,
        run: WorkflowRun,
        queue_items: list[WorkflowQueueItem],
    ) -> str:
        for item in queue_items:
            delivery = (item.output_payload or {}).get("delivery")
            if not isinstance(delivery, dict):
                continue
            output = delivery.get("output_payload")
            if not isinstance(output, dict):
                output = delivery
            summary = output.get("summary")
            if not isinstance(summary, dict):
                summary = {}
            pr_number = summary.get("pr_number") or output.get("pr_number")
            merged = bool(summary.get("merged") or output.get("merged"))
            reloaded = bool(summary.get("reloaded") or output.get("reloaded"))
            if not (merged or reloaded):
                continue
            pr_text = f" PR #{pr_number}" if pr_number else " the approved pull request"
            if merged and reloaded:
                return (
                    f"Done. I merged{pr_text}, updated the dedicated Maestro runtime, and the "
                    f"running app reloaded successfully. The workflow `{self._run_title(run)}` is complete."
                )
            if merged:
                return f"Done. I merged{pr_text}. The workflow `{self._run_title(run)}` is complete."
            return (
                f"Done. I updated the dedicated Maestro runtime and reloaded the running app. "
                f"The workflow `{self._run_title(run)}` is complete."
            )
        return ""

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
