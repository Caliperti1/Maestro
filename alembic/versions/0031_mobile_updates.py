"""Add mobile update delivery and shared message receipts.

Revision ID: 0031_mobile_updates
Revises: 0030_retire_l3_domain
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_mobile_updates"
down_revision: str | None = "0030_retire_l3_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_via", sa.String(length=40), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_message_receipts_message_id"),
    )
    op.create_index(op.f("ix_message_receipts_message_id"), "message_receipts", ["message_id"])

    op.create_table(
        "mobile_device_endpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("device_token", sa.String(length=256), nullable=False),
        sa.Column("platform", sa.String(length=40), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("bundle_id", sa.String(length=240), nullable=False),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False),
        sa.Column("last_registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mobile_device_endpoints_device_token"),
        "mobile_device_endpoints",
        ["device_token"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mobile_device_endpoints_installation_id"),
        "mobile_device_endpoints",
        ["installation_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_mobile_device_endpoints_notifications_enabled"),
        "mobile_device_endpoints",
        ["notifications_enabled"],
    )

    op.create_table(
        "mobile_notification_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("device_endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("priority", sa.String(length=40), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apns_id", sa.String(length=120), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_endpoint_id"], ["mobile_device_endpoints.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "message_id",
            "device_endpoint_id",
            name="uq_mobile_notification_delivery_message_device",
        ),
    )
    op.create_index(
        op.f("ix_mobile_notification_deliveries_device_endpoint_id"),
        "mobile_notification_deliveries",
        ["device_endpoint_id"],
    )
    op.create_index(
        op.f("ix_mobile_notification_deliveries_message_id"),
        "mobile_notification_deliveries",
        ["message_id"],
    )
    op.create_index(
        op.f("ix_mobile_notification_deliveries_next_attempt_at"),
        "mobile_notification_deliveries",
        ["next_attempt_at"],
    )
    op.create_index(
        op.f("ix_mobile_notification_deliveries_status"),
        "mobile_notification_deliveries",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_mobile_notification_deliveries_status"),
        table_name="mobile_notification_deliveries",
    )
    op.drop_index(
        op.f("ix_mobile_notification_deliveries_next_attempt_at"),
        table_name="mobile_notification_deliveries",
    )
    op.drop_index(
        op.f("ix_mobile_notification_deliveries_message_id"),
        table_name="mobile_notification_deliveries",
    )
    op.drop_index(
        op.f("ix_mobile_notification_deliveries_device_endpoint_id"),
        table_name="mobile_notification_deliveries",
    )
    op.drop_table("mobile_notification_deliveries")
    op.drop_index(
        op.f("ix_mobile_device_endpoints_notifications_enabled"),
        table_name="mobile_device_endpoints",
    )
    op.drop_index(
        op.f("ix_mobile_device_endpoints_installation_id"),
        table_name="mobile_device_endpoints",
    )
    op.drop_index(
        op.f("ix_mobile_device_endpoints_device_token"),
        table_name="mobile_device_endpoints",
    )
    op.drop_table("mobile_device_endpoints")
    op.drop_index(op.f("ix_message_receipts_message_id"), table_name="message_receipts")
    op.drop_table("message_receipts")
