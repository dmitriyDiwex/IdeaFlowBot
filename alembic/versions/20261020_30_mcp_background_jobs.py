"""Durable moderation jobs and separate advertising-delivery audit.

Revision ID: 20261020_30
Revises: 20261017_29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261020_30"
down_revision = "20261017_29"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mcp_moderation_actions", sa.Column("advertising_reply_state", sa.String(24), nullable=False, server_default="not_required"))
    op.add_column("mcp_moderation_actions", sa.Column("advertising_warning_text", sa.Text()))
    op.add_column("mcp_moderation_actions", sa.Column("telegram_error_transient", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("mcp_moderation_actions", sa.Column("telegram_retry_after_seconds", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("mcp_moderation_actions", sa.Column("dispatch_deadline_at", sa.DateTime(timezone=True)))
    # A repaired card does not prove delivery of a historical advertising reply.
    # Do not enqueue or re-send any historical operation during migration.
    op.execute("UPDATE mcp_moderation_actions SET advertising_reply_state = 'unknown', "
               "advertising_warning_text = warning_text WHERE decision = 'advertising'")
    op.create_table(
        "mcp_moderation_jobs",
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("mcp_moderation_snapshots.snapshot_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("authorized_rows", postgresql.JSONB(), nullable=False),
        sa.Column("card_retries", postgresql.JSONB(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_text", sa.Text()),
    )
    op.create_index("ix_mcp_jobs_due", "mcp_moderation_jobs", ["state", "next_run_at"])


def downgrade():
    op.drop_table("mcp_moderation_jobs")
    for name in ("dispatch_deadline_at", "telegram_retry_after_seconds", "telegram_error_transient",
                 "advertising_warning_text", "advertising_reply_state"):
        op.drop_column("mcp_moderation_actions", name)
