"""add suggestion advertising settings, exclusions and persistent counters

Revision ID: 20261003_27
Revises: 20260921_26
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa


revision = "20261003_27"
down_revision = "20260921_26"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "suggestion_ad_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("text_html", sa.Text(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suggestion_ad_settings")),
    )
    op.create_table(
        "suggestion_ad_exclusions",
        sa.Column("channel_tag", sa.String(32), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("channel_tag", name=op.f("pk_suggestion_ad_exclusions")),
    )
    op.create_table(
        "suggestion_ad_counters",
        sa.Column("channel_tg_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("submission_count", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("channel_tg_id", "user_id", name=op.f("pk_suggestion_ad_counters")),
    )


def downgrade() -> None:
    op.drop_table("suggestion_ad_counters")
    op.drop_table("suggestion_ad_exclusions")
    op.drop_table("suggestion_ad_settings")
