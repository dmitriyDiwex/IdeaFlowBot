from __future__ import annotations

from sqlalchemy import BigInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.editorial.db.base import BaseIdMixin, EditorialBase, TimestampMixin


class AdLinkExclusion(EditorialBase, BaseIdMixin, TimestampMixin):
    __tablename__ = "ad_link_exclusions"
    __table_args__ = (
        UniqueConstraint("normalized_url", name="uq_ad_link_exclusions_normalized_url"),
    )

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    normalized_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, index=True)
