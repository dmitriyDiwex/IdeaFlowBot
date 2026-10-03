"""Persist moderation snapshots and punctuation-insensitive text identities.

Revision ID: 20261003_28
Revises: 20261003_27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261003_28"
down_revision = "20261003_27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_moderation_actions", sa.Column("rate_limit_at", sa.DateTime(timezone=True)))
    op.add_column("mcp_moderation_actions", sa.Column(
        "telegram_sync_state", sa.String(24), nullable=False, server_default="not_required"))
    op.execute("UPDATE mcp_moderation_actions SET rate_limit_at = completed_at WHERE outcome = 'applied' AND NOT dry_run")
    op.add_column("submissions", sa.Column("moderation_normalized_hash", sa.String(64)))
    # PostgreSQL core SHA-256; no extension needed.
    op.execute("""
        UPDATE submissions SET moderation_normalized_hash = CASE WHEN value = '' THEN NULL
        ELSE encode(sha256(convert_to(value, 'UTF8')), 'hex') END
        FROM (SELECT id, trim(regexp_replace(lower(normalize(
            coalesce(nullif(cleaned_text, ''), raw_text, ''), NFC)),
            '[^[:alnum:]]+', ' ', 'g')) AS value FROM submissions) AS fingerprints
        WHERE submissions.id = fingerprints.id
    """)
    op.create_index("ix_submissions_moderation_normalized_hash", "submissions", ["moderation_normalized_hash"])
    op.create_index("ix_submissions_mcp_order", "submissions", ["status", "created_at", "id"])
    op.create_index("ix_submissions_mcp_album", "submissions", ["channel_id", "source_chat_id", "media_group_id", "id"])
    op.create_index("ix_mcp_actions_channel_window", "mcp_moderation_actions", ["channel_id", "rate_limit_at"])
    op.create_table(
        "mcp_moderation_snapshots",
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("profile", sa.String(80), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        *[sa.Column(name, postgresql.JSONB(), nullable=False) for name in
          ("rows", "proposals", "user_changes", "decisions", "summary")],
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_mcp_moderation_snapshots_expires_at", "mcp_moderation_snapshots", ["expires_at"])


def downgrade() -> None:
    op.drop_table("mcp_moderation_snapshots")
    op.drop_index("ix_mcp_actions_channel_window", table_name="mcp_moderation_actions")
    op.drop_column("mcp_moderation_actions", "telegram_sync_state")
    op.drop_column("mcp_moderation_actions", "rate_limit_at")
    op.drop_index("ix_submissions_mcp_album", table_name="submissions")
    op.drop_index("ix_submissions_mcp_order", table_name="submissions")
    op.drop_index("ix_submissions_moderation_normalized_hash", table_name="submissions")
    op.drop_column("submissions", "moderation_normalized_hash")
