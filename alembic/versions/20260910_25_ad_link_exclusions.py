"""add automatic advertising link exclusions

Revision ID: 20260910_25
Revises: 20260910_24
Create Date: 2026-09-10 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260910_25"
down_revision = "20260910_24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ad_link_exclusions",
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("normalized_url", sa.String(length=2048), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ad_link_exclusions")),
        sa.UniqueConstraint("normalized_url", name="uq_ad_link_exclusions_normalized_url"),
    )
    op.create_index(
        op.f("ix_ad_link_exclusions_created_by"),
        "ad_link_exclusions",
        ["created_by"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ad_link_exclusions_created_by"), table_name="ad_link_exclusions")
    op.drop_table("ad_link_exclusions")
