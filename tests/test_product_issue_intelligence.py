
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.main import create_app
from app.db.models import (
    Domain,
    ProductIssue,
    ProductIssueRelation,
    ProductProject,
    RepositoryProfile,
    WorkflowDefinition,
    WorkflowRun,
)
from app.db.seed import seed_default_domains
from app.db.session import get_db
from app.issues.github_sync import GitHubIssueSyncService
from app.issues.repositories import ensure_default_repository_portfolio, ensure_repository_workflows
from app.issues.service import ProductIssueService
from app.issues.worker import RepositoryIntelligenceWorker


def _domain(session, key="maestro-development"):
    domain = Domain(key=key, name="Maestro Development", description="", is_active=True)
    session.add(domain)
    session.commit()
    return domain


def _project(session, domain):
    project = ProductProject(domain_id=domain.id, key="maestro", name="Maestro", summary="", vision="", source_refs=[], provenance={})
    session.add(project)
    session.commit()
    return project


def _repository(session, domain, project):
    profile = RepositoryProfile(domain_id=domain.id, project_id=project.id, key="maestro", display_name="Maestro", external_repo="Caliperti1/Maestro", default_branch="main", sync_config={}, provenance={})
    session.add(profile)
    session.commit()
    return profile


def test_capture_asks_before_creating_an_underspecified_issue(session):
    _domain(session)
    result = ProductIssueService(session).capture(
        domain_key="maestro-development",
        project_key=None,
        title="Improve memory",
    )

    assert result.status == "needs_clarification"
    assert "which project" in result.clarification
    assert session.scalar(select(ProductIssue)) is None


def test_capture_creates_then_reconciles_duplicate_at_canonical_level(session):
    _domain(session)
    service = ProductIssueService(session)
    first = service.capture(
        domain_key="maestro-development",
        project_key="maestro",
        title="Add issue intelligence",
        problem="Product ideas are scattered across notes.",
        desired_outcome="Maintain one canonical issue store.",
        acceptance_criteria=["Issues retain provenance"],
        use_semantic_match=False,
    )
    second = service.capture(
        domain_key="maestro-development",
        project_key="maestro",
        title="Add issue intelligence",
        problem="GitHub and local work need reconciliation.",
        acceptance_criteria=["GitHub sync is idempotent"],
        source_refs=[{"source_system": "maestro_chat", "source_id": "message-2"}],
        use_semantic_match=False,
    )

    assert first.status == "created"
    assert second.status == "merged"
    issues = session.scalars(select(ProductIssue)).all()
    assert len(issues) == 1
    assert "GitHub and local" in issues[0].problem
    assert issues[0].acceptance_criteria == ["Issues retain provenance", "GitHub sync is idempotent"]
    assert issues[0].metadata_["merged_submissions"]


def test_distinct_overlapping_issue_is_related_not_deleted(session):
    domain = _domain(session)
    project = _project(session, domain)
    existing = ProductIssue(domain_id=domain.id, project_id=project.id, issue_type="feature", title="GitHub issue sync", normalized_title="github issue sync", problem="Sync issue state.", desired_outcome="", acceptance_criteria=[], notes="", source_refs=[], provenance={})
    session.add(existing)
    session.commit()

    class RelatedMatcher:
        def resolve(self, *, proposed, candidates):
            from app.issues.service import IssueMatchDecision
            return IssueMatchDecision("relate", str(candidates[0].id), "related_to", "Shared GitHub boundary but separate UI work.", 0.91)

    result = ProductIssueService(session, matcher=RelatedMatcher()).capture(
        domain_key=domain.key,
        project_key=project.key,
        title="GitHub issue viewer",
        problem="Inspect canonical issues in Maestro.",
        desired_outcome="Add a clean issue UI.",
    )

    assert result.status == "created"
    assert session.scalar(select(ProductIssueRelation)).relation_type == "related_to"
    assert len(session.scalars(select(ProductIssue)).all()) == 2


class FakeGitHub:
    def __init__(self):
        self.created = []

    def list_issues(self, _repo):
        return [{
            "number": 143, "title": "Repository state briefing", "body": "Observe the codebase.",
            "state": "open", "updated_at": "2026-08-23T10:00:00Z",
            "html_url": "https://github.test/repo/issues/143", "labels": [],
        }]

    def create_issue(self, repo, *, title, body, labels):
        self.created.append((repo, title, body, labels))
        return {"number": 144, "title": title, "body": body, "state": "open", "updated_at": "2026-08-23T11:00:00Z", "html_url": "https://github.test/repo/issues/144", "labels": []}

    def update_issue(self, repo, number, *, title, body, state):
        return {"number": number, "title": title, "body": body, "state": state, "updated_at": "2026-08-23T11:00:00Z", "html_url": f"https://github.test/repo/issues/{number}", "labels": []}


def test_github_sync_imports_remote_and_publishes_local(session):
    domain = _domain(session)
    project = _project(session, domain)
    repository = _repository(session, domain, project)
    local = ProductIssue(domain_id=domain.id, project_id=project.id, repository_id=repository.id, issue_type="feature", title="Local product idea", normalized_title="local product idea", problem="Needs implementation.", desired_outcome="Ship it.", acceptance_criteria=["A PR exists"], notes="", status="ready", sync_status="pending_push", source_refs=[], provenance={})
    session.add(local)
    session.commit()
    client = FakeGitHub()

    result = GitHubIssueSyncService(session, client=client).sync(repository)

    assert result.imported == 1
    assert result.published == 1
    session.refresh(local)
    assert local.external_number == 144
    assert local.sync_status == "synced"
    assert client.created[0][0] == "Caliperti1/Maestro"


def test_issue_api_registers_visible_repository_workflows(session, tmp_path):
    domain = _domain(session)
    project = _project(session, domain)
    repository_path = tmp_path / "repo"
    repository_path.mkdir()
    (repository_path / ".git").mkdir()
    app = create_app()
    def override_db():
        yield session
    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    response = client.post("/issues/repositories", json={
        "domain_key": domain.key, "project_key": project.key, "key": "maestro",
        "display_name": "Maestro", "external_repo": "Caliperti1/Maestro",
        "local_path": str(repository_path), "default_branch": "main",
    })

    assert response.status_code == 200
    definitions = session.scalars(select(WorkflowDefinition)).all()
    assert {item.key for item in definitions} == {"repository-intelligence:maestro", "issue-hygiene:maestro"}
    assert all(item.trigger_config["managed_by"] == "repository_intelligence_worker" for item in definitions)


def test_default_portfolio_registers_each_product_repository(session, monkeypatch):
    seed_default_domains(session)
    monkeypatch.setattr("app.issues.repositories._find_checkout", lambda _spec: None)

    profiles = ensure_default_repository_portfolio(session)

    assert {profile.external_repo for profile in profiles} == {
        "Praxis-Defense/GroundTruth",
        "Perti-Laboratories/Deeper-Learning",
        "Perti-Laboratories/AAce",
        "Perti-Laboratories/Ophi",
    }
    definitions = session.scalars(select(WorkflowDefinition)).all()
    assert len(definitions) == 8
    assert {definition.name for definition in definitions} >= {
        "Repository Intelligence - GroundTruth",
        "Repository Intelligence - Deeper Learning",
        "Repository Intelligence - AAce",
        "Repository Intelligence - Ophi",
    }


def test_repository_failure_is_throttled_per_workflow(session):
    domain = _domain(session)
    project = _project(session, domain)
    repository = _repository(session, domain, project)
    ensure_repository_workflows(session, repository, domain)
    session.commit()
    worker = RepositoryIntelligenceWorker(session)

    worker._record_failure(repository, "issue-hygiene", "first failure")
    worker._record_failure(repository, "issue-hygiene", "updated failure")

    failures = session.scalars(
        select(WorkflowRun).where(WorkflowRun.status == "failed")
    ).all()
    assert len(failures) == 1
    assert failures[0].error_message == "updated failure"
