"""Keep standalone human-input todos out of workflow attention.

Revision ID: 0028_attention_blockers_only
Revises: 0027_maestro_voice_turns
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_attention_blockers_only"
down_revision: str | None = "0027_maestro_voice_turns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE todos
        SET status = 'open', updated_at = now()
        WHERE status = 'needs_input'
          AND agent_task = false
          AND workflow_task_id IS NULL
          AND workflow_run_id IS NULL
          AND metadata ->> 'route_type' = 'human_input'
          AND COALESCE(metadata ->> 'blocks_execution', 'false') <> 'true'
        """
    )
    op.execute(
        """
        UPDATE routed_items
        SET status = 'open', updated_at = now()
        WHERE status = 'needs_input'
          AND route_type = 'human_input'
          AND task_id IS NULL
          AND COALESCE(metadata ->> 'blocks_execution', 'false') <> 'true'
        """
    )


def downgrade() -> None:
    pass
