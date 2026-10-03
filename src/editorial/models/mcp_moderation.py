from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from src.editorial.db.base import BaseIdMixin, EditorialBase


class McpModerationAction(EditorialBase, BaseIdMixin):
    """One idempotent moderation decision requested through MCP."""

    __tablename__ = "mcp_moderation_actions"
    __table_args__ = (
        UniqueConstraint("request_id"),
        Index("ix_mcp_actions_channel_window", "channel_id", "rate_limit_at"),
    )

    request_id: Mapped[str] = mapped_column(String(160), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    requested_submission_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    submission_id: Mapped[int | None] = mapped_column(
        ForeignKey("submissions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expected_status: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(32))
    resulting_status: Mapped[str | None] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    content_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    legacy_sync_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_text: Mapped[str | None] = mapped_column(Text)
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    rate_limit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_sync_state: Mapped[str] = mapped_column(String(24), nullable=False, default="not_required")


class McpModerationSnapshot(EditorialBase):
    __tablename__ = "mcp_moderation_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="awaiting_approval")
    # Private expected revisions and a separate compact public projection.
    rows: Mapped[list] = mapped_column(JSONB, nullable=False)
    proposals: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    user_changes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    decisions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
