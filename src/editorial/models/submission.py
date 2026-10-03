from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, event, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.editorial.db.base import BaseIdMixin, EditorialBase
from src.editorial.models.enums import SubmissionStatus, enum_column


class Submission(EditorialBase, BaseIdMixin):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_row_id"),
        Index("ix_submissions_mcp_order", "status", "created_at", "id"),
        Index("ix_submissions_mcp_album", "channel_id", "source_chat_id", "media_group_id", "id"),
    )

    legacy_source: Mapped[str] = mapped_column(String(64), default="sender_info", nullable=False)
    legacy_row_id: Mapped[int | None] = mapped_column(index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True)
    source_user_id: Mapped[int | None] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(32), default="text", nullable=False, index=True)
    media_group_id: Mapped[str | None] = mapped_column(String(255), index=True)
    bot_username: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    raw_text: Mapped[str | None] = mapped_column(Text)
    cleaned_text: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text, index=True)
    text_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    moderation_normalized_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    detected_tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_candidate_for_generation: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_candidate_for_paste: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[SubmissionStatus] = mapped_column(
        enum_column(SubmissionStatus, "submission_status"),
        default=SubmissionStatus.NEW,
        nullable=False,
        index=True,
    )
    moderator_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@event.listens_for(Submission, "before_insert")
@event.listens_for(Submission, "before_update")
def _moderation_fingerprint(mapper, connection, submission) -> None:
    from src.editorial.utils.text import compute_moderation_hash

    submission.moderation_normalized_hash = compute_moderation_hash(
        submission.cleaned_text or submission.raw_text
    )
