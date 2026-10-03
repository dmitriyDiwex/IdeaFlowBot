from __future__ import annotations

from html import escape
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from telebot.formatting import apply_html_entities
from telebot.types import Message

from src.editorial.models.suggestion_ad import (
    SuggestionAdCounter,
    SuggestionAdExclusion,
    SuggestionAdSettings,
)


def normalize_suggestion_channel_tag(value: str | None) -> str:
    tag = str(value or "").strip().removeprefix("@").lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{3,31}", tag):
        raise ValueError("Введите тег паблика в формате @channel_name.")
    return tag


def suggestion_ad_html(message: Message) -> str:
    """Accept literal Telegram HTML or preserve formatting/custom emoji entities."""
    raw = (message.text or "").strip()
    if not raw:
        raise ValueError("Отправьте рекламный текст одним текстовым сообщением.")
    if re.search(r"</?(?:b|strong|i|em|u|ins|s|strike|del|span|tg-spoiler|a|tg-emoji|tg-time|code|pre|blockquote)(?:\s|>)|&(?:lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);", raw):
        return raw
    entities = getattr(message, "entities", None)
    if entities:
        # Entity offsets refer to the untrimmed text and use UTF-16 code units.
        return apply_html_entities(message.text, entities).strip()
    return escape(raw, quote=False)


class SuggestionAdService:
    async def get_text(self, session: AsyncSession) -> str | None:
        return await session.scalar(
            select(SuggestionAdSettings.text_html).where(SuggestionAdSettings.id == 1)
        )

    async def set_text(self, session: AsyncSession, *, text_html: str) -> None:
        if not text_html.strip():
            raise ValueError("Рекламный текст не должен быть пустым.")
        statement = insert(SuggestionAdSettings).values(id=1, text_html=text_html)
        await session.execute(statement.on_conflict_do_update(
            index_elements=[SuggestionAdSettings.id],
            set_={"text_html": text_html, "updated_at": statement.excluded.updated_at},
        ))
        await session.commit()

    async def list_exclusions(self, session: AsyncSession) -> list[str]:
        result = await session.scalars(
            select(SuggestionAdExclusion.channel_tag).order_by(SuggestionAdExclusion.channel_tag)
        )
        return list(result.all())

    async def add_exclusion(self, session: AsyncSession, *, channel_tag: str) -> tuple[str, bool]:
        tag = normalize_suggestion_channel_tag(channel_tag)
        statement = insert(SuggestionAdExclusion).values(channel_tag=tag)
        added = await session.scalar(statement.on_conflict_do_nothing(
            index_elements=[SuggestionAdExclusion.channel_tag],
        ).returning(SuggestionAdExclusion.channel_tag))
        await session.commit()
        return tag, added is not None

    async def delete_exclusion(self, session: AsyncSession, *, channel_tag: str) -> str:
        tag = normalize_suggestion_channel_tag(channel_tag)
        row = await session.get(SuggestionAdExclusion, tag)
        if row is None:
            raise ValueError("Этого паблика нет в списке исключений.")
        await session.delete(row)
        await session.commit()
        return tag

    async def record_submission(
        self,
        session: AsyncSession,
        *,
        channel_tg_id: int,
        channel_tag: str | None,
        user_id: int,
    ) -> str | None:
        """Atomically count eligible submissions per user/channel: ads at 1, 6, 11…"""
        text_html = await self.get_text(session)
        if not text_html:
            return None
        try:
            tag = normalize_suggestion_channel_tag(channel_tag)
        except ValueError:
            tag = None  # Private channels can be identified only by their Telegram ID.
        if tag and await session.get(SuggestionAdExclusion, tag) is not None:
            return None

        statement = insert(SuggestionAdCounter).values(
            channel_tg_id=channel_tg_id, user_id=user_id, submission_count=1,
        )
        count = await session.scalar(statement.on_conflict_do_update(
            index_elements=[SuggestionAdCounter.channel_tg_id, SuggestionAdCounter.user_id],
            set_={"submission_count": SuggestionAdCounter.submission_count + 1},
        ).returning(SuggestionAdCounter.submission_count))
        await session.commit()
        return text_html if (count - 1) % 5 == 0 else None
