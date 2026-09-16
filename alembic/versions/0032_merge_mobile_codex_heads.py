"""Merge mobile update and persistent Codex thread schema heads.

Revision ID: 0032_merge_mobile_codex_heads
Revises: 0031_mobile_updates, 0031_project_codex_threads
"""

from collections.abc import Sequence

revision: str = "0032_merge_mobile_codex_heads"
down_revision: tuple[str, str] = (
    "0031_mobile_updates",
    "0031_project_codex_threads",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
