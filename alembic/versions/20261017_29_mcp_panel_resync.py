"""Audit safe card-only MCP resynchronization.

Revision ID: 20261017_29
Revises: 20261003_28
"""

from alembic import op
import sqlalchemy as sa

revision = "20261017_29"
down_revision = "20261003_28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_panel_sync_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.String(160), nullable=False),
        sa.Column("batch_id", sa.String(120), nullable=False),
        sa.Column("operation_id", sa.Integer(), sa.ForeignKey("mcp_moderation_actions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("claim_token", sa.String(36), nullable=False),
        sa.Column("legacy_sync_count", sa.Integer(), nullable=False),
        sa.Column("error_text", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("rate_limit_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index("ix_mcp_panel_sync_attempts_batch_id", "mcp_panel_sync_attempts", ["batch_id"])
    op.create_index("ix_mcp_panel_sync_attempts_operation_id", "mcp_panel_sync_attempts", ["operation_id"])
    op.create_index("ix_mcp_panel_sync_channel_window", "mcp_panel_sync_attempts", ["channel_id", "rate_limit_at"])


def downgrade() -> None:
    op.drop_table("mcp_panel_sync_attempts")
