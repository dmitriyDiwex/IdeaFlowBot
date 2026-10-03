from __future__ import annotations

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.editorial.db.base import EditorialBase, TimestampMixin


class SuggestionAdSettings(EditorialBase, TimestampMixin):
    __tablename__ = "suggestion_ad_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    text_html: Mapped[str] = mapped_column(Text, nullable=False)


class SuggestionAdExclusion(EditorialBase, TimestampMixin):
    __tablename__ = "suggestion_ad_exclusions"

    channel_tag: Mapped[str] = mapped_column(String(32), primary_key=True)


class SuggestionAdCounter(EditorialBase):
    __tablename__ = "suggestion_ad_counters"

    channel_tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    submission_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
