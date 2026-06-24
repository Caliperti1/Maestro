from pathlib import Path
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agents.runtime import PromptAggregationService
from app.api.main import create_app
from app.core.config import get_settings
from app.db.models import Artifact, Report, Task
from app.db.session import get_db
from app.maestro.orchestrator import MaestroOrchestratorService


class FakeOrchestratorLLMClient:
    provider = "test"
    model = "test-orchestrator-agent"

    def structured_response(self, **kwargs):
        raise AssertionError("Orchestrated agent runs should use text_response.")

    def text_response(self, *, instructions: str, input_text: str) -> str:
        return (
            "## Summary\n"
            "Prepared the requested domain contribution.\n\n"
            "## Findings\n"
            "- Agent registry and scoped memory were available.\n\n"
            "## Next Steps\n"
            "- Return to Maestro for synthesis."
        )


def _client(session: Session, tmp_path: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)

    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_orchestrator_plan_is_registry_aware_and_plan_first(session: Session) -> None:
    plan = MaestroOrchestratorService(session).create_plan(
        "Prepare a Praxis partner call workflow and identify RFIs, tasks, contacts, and events."
    )

    assert plan.status == "proposed"
    assert plan.approval_required is True
    assert plan.execution_mode == "propose_first"
    assert any(intent.type == "workflow" for intent in plan.intents)
    assert any(intent.type == "rfi" for intent in plan.intents)
    assert any(subtask.agent_key == "praxis-planning-agent" for subtask in plan.subtasks)
    assert any(
        agent["key"] == "praxis-planning-agent" for agent in plan.registry_snapshot["agents"]
    )
    assert plan.scheduler["status"] == "queue_foundation"
    assert session.query(Task).filter(Task.status == "proposed").count() == 1
    assert session.query(Report).count() == 0


def test_orchestrator_run_dispatches_children_and_stages_one_artifact(
    session: Session,
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.memory_dropbox_root = str(tmp_path)
    runtime = PromptAggregationService(session, llm_client=FakeOrchestratorLLMClient())
    service = MaestroOrchestratorService(session, runtime=runtime)
    plan = service.create_plan(
        "Prepare a Praxis partner call workflow and ask Maestro Development to note system gaps."
    )

    run = service.run_plan(plan.parent_task_id, execute_llm=True)

    assert run.status == "completed"
    assert run.synthesis_report_id is not None
    assert run.staged_artifact_path is not None
    assert Path(run.staged_artifact_path).is_file()
    assert len(run.child_runs) >= 2
    parent_task_id = uuid.UUID(plan.parent_task_id)
    child_tasks = session.query(Task).filter(Task.parent_task_id == parent_task_id).all()
    assert len(child_tasks) == len(run.child_runs)
    assert all(task.source_type == "maestro_orchestrator" for task in child_tasks)
    artifacts = session.query(Artifact).all()
    assert len(artifacts) == 1
    assert artifacts[0].metadata_["canonical_workflow_artifact"] is True
    parent = session.get(Task, parent_task_id)
    assert parent is not None
    assert parent.status == "completed"
    assert parent.output_payload["synthesis_report_id"] == run.synthesis_report_id


def test_maestro_api_plan_and_stub_run(session: Session, tmp_path: Path) -> None:
    client = _client(session, tmp_path)

    plan_response = client.post(
        "/maestro/plan",
        json={
            "message": (
                "Coordinate Praxis and Maestro Development on a partner prep workflow, "
                "capture any RFIs, and produce a synthesized plan."
            )
        },
    )
    assert plan_response.status_code == 200
    plan = plan_response.json()["plan"]
    assert plan["status"] == "proposed"
    assert plan["subtasks"]

    run_response = client.post(
        f"/maestro/plans/{plan['parent_task_id']}/run",
        json={"execute_llm": False},
    )

    assert run_response.status_code == 200
    run = run_response.json()["run"]
    assert run["status"] == "completed"
    assert run["child_runs"]
    assert run["staged_artifact_path"]
    assert "Maestro Synthesis" in run["synthesis"]
