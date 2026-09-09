"""recover unsafe orphaned auto-profile channels

Revision ID: 20260910_24
Revises: 20260827_23
Create Date: 2026-09-10 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260910_24"
down_revision = "20260827_23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE channels
            SET settings_profile_auto_enabled = true,
                auto_slots_last_planned_for = NULL,
                updated_at = now()
            WHERE content_family = 'overheard'
              AND is_active = true
              AND auto_slots_enabled = true
              AND settings_profile_id IS NULL
              AND settings_profile_auto_enabled = false
              AND auto_slots_plan_time >= auto_slots_window_end
            """
        )
    )


def downgrade() -> None:
    # The previous manual/automatic intent cannot be reconstructed safely.
    pass
