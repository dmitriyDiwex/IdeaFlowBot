"""enable shared settings profiles for confession channels

Revision ID: 20260921_26
Revises: 20260910_25
Create Date: 2026-09-21 12:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260921_26"
down_revision = "20260910_25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE channels
        SET settings_profile_auto_enabled = true,
            auto_slots_last_planned_for = NULL
        WHERE content_family = 'confession'
          AND settings_profile_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE channels
        SET settings_profile_auto_enabled = false
        WHERE content_family = 'confession'
          AND settings_profile_id IS NULL
        """
    )
