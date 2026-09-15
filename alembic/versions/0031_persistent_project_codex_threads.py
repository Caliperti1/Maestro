"""Add the persistent repository worker Codex thread.

Revision ID: 0031_persistent_project_codex_threads
Revises: 0030_retire_l3_domain
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_persistent_project_codex_threads"
down_revision: str | None = "0030_retire_l3_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repository_profiles",
        sa.Column("codex_worker_session_id", sa.String(240), nullable=True),
    )
    op.create_index(
        "ix_repository_profiles_codex_worker_session_id",
        "repository_profiles",
        ["codex_worker_session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_repository_profiles_codex_worker_session_id",
        table_name="repository_profiles",
    )
    op.drop_column("repository_profiles", "codex_worker_session_id")
