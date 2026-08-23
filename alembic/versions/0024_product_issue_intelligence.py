"""Add canonical product projects, repositories, issues, relations, and executions.

Revision ID: 0024_product_issue_intelligence
Revises: 0023_routed_tasks
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_product_issue_intelligence"
down_revision: str | None = "0023_routed_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("name", sa.String(240), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("vision", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(40), nullable=False, server_default="active"),
        sa.Column("source_refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain_id", "key", name="uq_product_projects_domain_key"),
    )
    for column in ("domain_id", "key", "name", "status"):
        op.create_index(f"ix_product_projects_{column}", "product_projects", [column])

    op.create_table(
        "repository_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_registration_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(240), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False, server_default="github"),
        sa.Column("external_repo", sa.String(320), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("default_branch", sa.String(160), nullable=False, server_default="main"),
        sa.Column("current_commit", sa.String(80), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("codex_steward_session_id", sa.String(240), nullable=True),
        sa.Column("status", sa.String(40), nullable=False, server_default="active"),
        sa.Column("sync_config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["product_projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_registration_id"], ["source_registrations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "external_repo", name="uq_repository_profiles_provider_repo"),
        sa.UniqueConstraint("key"),
    )
    for column in ("domain_id", "project_id", "source_registration_id", "key", "provider", "external_repo", "current_commit", "last_observed_at", "last_synced_at", "codex_steward_session_id", "status"):
        op.create_index(f"ix_repository_profiles_{column}", "repository_profiles", [column])

    op.create_table(
        "product_issues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=True),
        sa.Column("issue_type", sa.String(80), nullable=False, server_default="feature"),
        sa.Column("title", sa.String(320), nullable=False),
        sa.Column("normalized_title", sa.String(320), nullable=False),
        sa.Column("problem", sa.Text(), nullable=False, server_default=""),
        sa.Column("desired_outcome", sa.Text(), nullable=False, server_default=""),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("priority", sa.String(40), nullable=False, server_default="normal"),
        sa.Column("estimated_minutes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(40), nullable=False, server_default="ready"),
        sa.Column("assignee_type", sa.String(40), nullable=False, server_default="unassigned"),
        sa.Column("assignee_ref", sa.String(240), nullable=True),
        sa.Column("agent_task", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("agent_task_status", sa.String(40), nullable=False, server_default="not_agent"),
        sa.Column("workflow_task_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("external_provider", sa.String(80), nullable=True),
        sa.Column("external_repo", sa.String(320), nullable=True),
        sa.Column("external_number", sa.Integer(), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("external_state", sa.String(40), nullable=True),
        sa.Column("external_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_status", sa.String(40), nullable=False, server_default="local"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("source_refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["product_projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repository_id"], ["repository_profiles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_provider", "external_repo", "external_number", name="uq_product_issues_external_identity"),
    )
    for column in ("domain_id", "project_id", "repository_id", "issue_type", "title", "normalized_title", "priority", "status", "agent_task", "agent_task_status", "workflow_task_id", "workflow_run_id", "external_provider", "external_repo", "external_number", "external_state", "sync_status", "last_synced_at"):
        op.create_index(f"ix_product_issues_{column}", "product_issues", [column])

    op.create_table(
        "product_issue_relations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_issue_id", sa.Uuid(), nullable=False),
        sa.Column("target_issue_id", sa.Uuid(), nullable=False),
        sa.Column("relation_type", sa.String(80), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_issue_id"], ["product_issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_issue_id"], ["product_issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_issue_id", "target_issue_id", "relation_type", name="uq_product_issue_relations_edge"),
    )
    for column in ("source_issue_id", "target_issue_id", "relation_type"):
        op.create_index(f"ix_product_issue_relations_{column}", "product_issue_relations", [column])

    op.create_table(
        "product_issue_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issue_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_task_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(40), nullable=False, server_default="queued"),
        sa.Column("branch_name", sa.String(320), nullable=True),
        sa.Column("pull_request_number", sa.Integer(), nullable=True),
        sa.Column("pull_request_url", sa.Text(), nullable=True),
        sa.Column("codex_session_id", sa.String(240), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("report_id", sa.Uuid(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["issue_id"], ["product_issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("issue_id", "workflow_task_id", "workflow_run_id", "status", "codex_session_id", "report_id"):
        op.create_index(f"ix_product_issue_executions_{column}", "product_issue_executions", [column])


def downgrade() -> None:
    op.drop_table("product_issue_executions")
    op.drop_table("product_issue_relations")
    op.drop_table("product_issues")
    op.drop_table("repository_profiles")
    op.drop_table("product_projects")
