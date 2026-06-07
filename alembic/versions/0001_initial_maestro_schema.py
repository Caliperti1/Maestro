"""initial maestro schema

Revision ID: 0001_initial_maestro_schema
Revises: 
Create Date: 2026-05-10

This is a draft Alembic migration for the Maestro MVP data model.
It assumes PostgreSQL from the beginning because memory, provenance,
and structured retrieval are core product concerns.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "0001_initial_maestro_schema"
down_revision = None
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("email", sa.String(length=320), nullable=False, unique=True),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("role", sa.String(length=50), nullable=False, server_default="owner"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
    )

    op.create_table(
        "domains",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("key", sa.String(length=80), nullable=False, unique=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
    )

    op.create_table(
        "agents",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False, unique=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("agent_type", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("capabilities", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("tool_permissions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
    )
    op.create_index("ix_agents_domain_id", "agents", ["domain_id"])

    op.create_table(
        "conversations",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="SET NULL"), nullable=True),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_domain_id", "conversations", ["domain_id"])
    op.create_index("ix_conversations_agent_id", "conversations", ["agent_id"])

    op.create_table(
        "messages",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("conversation_id", UUID, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_type", sa.String(length=40), nullable=False),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])

    op.create_table(
        "tasks",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("parent_task_id", UUID, sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("conversation_id", UUID, sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requested_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("assigned_agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="queued"),
        sa.Column("priority", sa.String(length=40), nullable=False, server_default="normal"),
        sa.Column("source_type", sa.String(length=40), nullable=False, server_default="chat"),
        sa.Column("workflow_key", sa.String(length=120), nullable=True),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("input_payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("output_payload", JSONB, nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_domain_id", "tasks", ["domain_id"])
    op.create_index("ix_tasks_assigned_agent_id", "tasks", ["assigned_agent_id"])

    op.create_table(
        "reports",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("task_id", UUID, sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="SET NULL"), nullable=True),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("report_type", sa.String(length=80), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("structured_data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_timestamps(),
    )
    op.create_index("ix_reports_task_id", "reports", ["task_id"])
    op.create_index("ix_reports_domain_id", "reports", ["domain_id"])
    op.create_index("ix_reports_agent_id", "reports", ["agent_id"])

    op.create_table(
        "memory_items",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="CASCADE"), nullable=True),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_from_proposal_id", UUID, nullable=True),
        sa.Column("scope", sa.String(length=40), nullable=False),
        sa.Column("memory_type", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("impact_level", sa.String(length=40), nullable=False, server_default="low"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_memory_items_scope", "memory_items", ["scope"])
    op.create_index("ix_memory_items_domain_id", "memory_items", ["domain_id"])
    op.create_index("ix_memory_items_agent_id", "memory_items", ["agent_id"])

    op.create_table(
        "memory_proposals",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="SET NULL"), nullable=True),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("task_id", UUID, sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("report_id", UUID, sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True),
        sa.Column("scope", sa.String(length=40), nullable=False),
        sa.Column("memory_type", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("impact_level", sa.String(length=40), nullable=False, server_default="low"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="proposed"),
        sa.Column("source_refs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_memory_proposals_status", "memory_proposals", ["status"])
    op.create_index("ix_memory_proposals_domain_id", "memory_proposals", ["domain_id"])
    op.create_index("ix_memory_proposals_agent_id", "memory_proposals", ["agent_id"])

    op.create_foreign_key(
        "fk_memory_items_created_from_proposal",
        "memory_items",
        "memory_proposals",
        ["created_from_proposal_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "memory_links",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("source_memory_id", UUID, sa.ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_memory_id", UUID, sa.ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relation_type", sa.String(length=80), nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_memory_links_source", "memory_links", ["source_memory_id"])
    op.create_index("ix_memory_links_target", "memory_links", ["target_memory_id"])

    op.create_table(
        "tool_connections",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_key", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("auth_type", sa.String(length=80), nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
    )
    op.create_index("ix_tool_connections_domain_id", "tool_connections", ["domain_id"])
    op.create_unique_constraint("uq_tool_connections_domain_tool", "tool_connections", ["domain_id", "tool_key"])

    op.create_table(
        "tool_calls",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("task_id", UUID, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tool_connection_id", UUID, sa.ForeignKey("tool_connections.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tool_name", sa.String(length=160), nullable=False),
        sa.Column("input_payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("output_payload", JSONB, nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="running"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tool_calls_task_id", "tool_calls", ["task_id"])
    op.create_index("ix_tool_calls_agent_id", "tool_calls", ["agent_id"])

    op.create_table(
        "artifacts",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("task_id", UUID, sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("report_id", UUID, sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True),
        sa.Column("seed_package_id", UUID, nullable=True),
        sa.Column("artifact_type", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(length=160), nullable=True),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_artifacts_task_id", "artifacts", ["task_id"])
    op.create_index("ix_artifacts_report_id", "artifacts", ["report_id"])

    op.create_table(
        "seed_packages",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="uploaded"),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_seed_packages_domain_id", "seed_packages", ["domain_id"])

    op.create_foreign_key(
        "fk_artifacts_seed_package",
        "artifacts",
        "seed_packages",
        ["seed_package_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "scheduled_runs",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("domain_id", UUID, sa.ForeignKey("domains.id", ondelete="SET NULL"), nullable=True),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("workflow_key", sa.String(length=120), nullable=True),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("cadence", sa.String(length=160), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_scheduled_runs_domain_id", "scheduled_runs", ["domain_id"])
    op.create_index("ix_scheduled_runs_agent_id", "scheduled_runs", ["agent_id"])

    # Seed MVP domains.
    op.execute(
        """
        INSERT INTO domains (key, name, description) VALUES
        ('personal', 'Personal', 'Personal life operations, email, calendar, reminders, and household context.'),
        ('maestro-development', 'Maestro Development', 'Maestro introspection, architecture, backlog, GitHub, Codex, and self-improvement work.'),
        ('praxis', 'Praxis', 'Praxis Defense business development, delivery, product, and technical operations.'),
        ('ophi', 'Ophi', 'Ophiuchus Labs product, research, market, and technical operations.'),
        ('usma', 'USMA', 'USMA teaching, admin, research, and academic operations.'),
        ('personal-irad-projects', 'Personal IRAD Projects', 'Personal independent R&D projects, scaffolding, build plans, and low-priority async development.'),
        ('l3', 'L3', 'L3 domain operations and memory.')
        ON CONFLICT (key) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.drop_table("scheduled_runs")
    op.drop_constraint("fk_artifacts_seed_package", "artifacts", type_="foreignkey")
    op.drop_table("seed_packages")
    op.drop_table("artifacts")
    op.drop_table("tool_calls")
    op.drop_constraint("uq_tool_connections_domain_tool", "tool_connections", type_="unique")
    op.drop_table("tool_connections")
    op.drop_table("memory_links")
    op.drop_constraint("fk_memory_items_created_from_proposal", "memory_items", type_="foreignkey")
    op.drop_table("memory_proposals")
    op.drop_table("memory_items")
    op.drop_table("reports")
    op.drop_table("tasks")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("agents")
    op.drop_table("domains")
    op.drop_table("users")
