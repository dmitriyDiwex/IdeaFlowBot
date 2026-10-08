from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core_database.models.bots_data import BotsData
from src.core_database.models.db_helper import db_helper as legacy_db_helper
from src.core_database.models.sender_info import SenderData
from src.editorial.models.channel import Channel
from src.editorial.models.submission import Submission


@dataclass(slots=True)
class LegacyBotBinding:
    channel_id: int
    bot_api_token: str
    bot_username: str


@dataclass(slots=True)
class LegacySenderRow:
    id: int
    user_id: int | None
    channel_id: int
    bot_username: str
    username: str | None
    first_name: str | None
    message_id: int | None
    chat_id: int | None
    text_post: str | None
    content_type: str | None
    media_group_id: str | None
    preview_file_id: str | None
    preview_file_size: int | None
    entities_json: str | None
    review_chat_id: int | None
    review_message_id: int | None
    timestamp: int


class LegacyCollectorReader:
    """Read-only bridge to the current legacy collector database."""

    @staticmethod
    def _sender_select():
        return select(*(getattr(SenderData, name) for name in LegacySenderRow.__dataclass_fields__))

    async def fetch_sender_media_group_rows(
        self, *, channel_id: int, source_chat_id: int, media_group_id: str,
    ) -> list[LegacySenderRow]:
        async with legacy_db_helper.engine.connect() as conn:
            result = await conn.execute(
                self._sender_select().where(
                    SenderData.channel_id == channel_id,
                    SenderData.chat_id == source_chat_id,
                    SenderData.media_group_id == media_group_id,
                ).order_by(SenderData.message_id, SenderData.id)
            )
            return [LegacySenderRow(**row) for row in result.mappings()]

    async def fetch_unimported_sender_rows(
        self, session: AsyncSession, *, limit: int = 200,
    ) -> list[LegacySenderRow]:
        """Find gaps as well as new rows; moderation can import out of order."""
        if session.bind.url == legacy_db_helper.engine.url:
            imported = select(Submission.id).where(
                Submission.legacy_source == "sender_info",
                Submission.legacy_row_id == SenderData.id,
            ).exists()
            result = await session.execute(
                self._sender_select()
                .join(Channel, Channel.tg_channel_id == SenderData.channel_id)
                .where(
                    ~imported,
                    or_(
                        SenderData.chat_id.is_(None), SenderData.chat_id >= 0,
                        SenderData.review_chat_id.is_not(None),
                        SenderData.review_message_id.is_not(None),
                    ),
                ).order_by(SenderData.id).limit(limit)
            )
            return [LegacySenderRow(**row) for row in result.mappings()]

        # A separate SQLite collector cannot join the editorial database.
        # Compare bounded batches, including old IDs, instead of using MAX(id).
        channel_ids = set((await session.scalars(select(Channel.tg_channel_id))).all())
        missing: list[LegacySenderRow] = []
        after_id = 0
        while len(missing) < limit:
            rows = await self.fetch_sender_rows(after_id=after_id, limit=200)
            if not rows:
                break
            after_id = max(row.id for row in rows)
            imported_ids = set((await session.scalars(select(Submission.legacy_row_id).where(
                Submission.legacy_source == "sender_info",
                Submission.legacy_row_id.in_([row.id for row in rows]),
            ))).all())
            missing.extend(row for row in rows if (
                row.id not in imported_ids and row.channel_id in channel_ids
                and not (row.chat_id is not None and row.chat_id < 0
                         and row.review_chat_id is None and row.review_message_id is None)
            ))
        return missing[:limit]

    async def fetch_sender_rows(self, after_id: int = 0, limit: int = 200) -> list[LegacySenderRow]:
        async with legacy_db_helper.engine.connect() as conn:
            result = await conn.execute(
                select(
                    SenderData.id,
                    SenderData.user_id,
                    SenderData.channel_id,
                    SenderData.bot_username,
                    SenderData.username,
                    SenderData.first_name,
                    SenderData.message_id,
                    SenderData.chat_id,
                    SenderData.text_post,
                    SenderData.content_type,
                    SenderData.media_group_id,
                    SenderData.preview_file_id,
                    SenderData.preview_file_size,
                    SenderData.entities_json,
                    SenderData.review_chat_id,
                    SenderData.review_message_id,
                    SenderData.timestamp,
                )
                .where(SenderData.id > after_id)
                .order_by(SenderData.id.asc())
                .limit(limit)
            )
            rows = result.mappings().all()
            return [
                LegacySenderRow(
                    id=row["id"],
                    user_id=row["user_id"],
                    channel_id=int(row["channel_id"]),
                    bot_username=row["bot_username"],
                    username=row["username"],
                    first_name=row["first_name"],
                    message_id=row["message_id"],
                    chat_id=row["chat_id"],
                    text_post=row["text_post"],
                    content_type=row["content_type"],
                    media_group_id=row["media_group_id"],
                    preview_file_id=row["preview_file_id"],
                    preview_file_size=row["preview_file_size"],
                    entities_json=row["entities_json"],
                    review_chat_id=row["review_chat_id"],
                    review_message_id=row["review_message_id"],
                    timestamp=row["timestamp"],
                )
                for row in rows
            ]

    async def fetch_sender_rows_by_ids(self, row_ids: list[int]) -> list[LegacySenderRow]:
        if not row_ids:
            return []
        async with legacy_db_helper.engine.connect() as conn:
            result = await conn.execute(
                select(
                    SenderData.id,
                    SenderData.user_id,
                    SenderData.channel_id,
                    SenderData.bot_username,
                    SenderData.username,
                    SenderData.first_name,
                    SenderData.message_id,
                    SenderData.chat_id,
                    SenderData.text_post,
                    SenderData.content_type,
                    SenderData.media_group_id,
                    SenderData.preview_file_id,
                    SenderData.preview_file_size,
                    SenderData.entities_json,
                    SenderData.review_chat_id,
                    SenderData.review_message_id,
                    SenderData.timestamp,
                )
                .where(SenderData.id.in_(row_ids))
                .order_by(SenderData.message_id.asc(), SenderData.id.asc())
            )
            rows = result.mappings().all()
            return [
                LegacySenderRow(
                    id=row["id"],
                    user_id=row["user_id"],
                    channel_id=int(row["channel_id"]),
                    bot_username=row["bot_username"],
                    username=row["username"],
                    first_name=row["first_name"],
                    message_id=row["message_id"],
                    chat_id=row["chat_id"],
                    text_post=row["text_post"],
                    content_type=row["content_type"],
                    media_group_id=row["media_group_id"],
                    preview_file_id=row["preview_file_id"],
                    preview_file_size=row["preview_file_size"],
                    entities_json=row["entities_json"],
                    review_chat_id=row["review_chat_id"],
                    review_message_id=row["review_message_id"],
                    timestamp=row["timestamp"],
                )
                for row in rows
            ]

    async def find_sender_row_by_review_message(
        self,
        channel_id: int,
        review_chat_id: int,
        review_message_id: int,
    ) -> LegacySenderRow | None:
        async with legacy_db_helper.engine.connect() as conn:
            result = await conn.execute(
                select(
                    SenderData.id,
                    SenderData.user_id,
                    SenderData.channel_id,
                    SenderData.bot_username,
                    SenderData.username,
                    SenderData.first_name,
                    SenderData.message_id,
                    SenderData.chat_id,
                    SenderData.text_post,
                    SenderData.content_type,
                    SenderData.media_group_id,
                    SenderData.preview_file_id,
                    SenderData.preview_file_size,
                    SenderData.entities_json,
                    SenderData.review_chat_id,
                    SenderData.review_message_id,
                    SenderData.timestamp,
                )
                .where(
                    SenderData.channel_id == channel_id,
                    SenderData.review_chat_id == review_chat_id,
                    SenderData.review_message_id == review_message_id,
                )
                .order_by(SenderData.id.asc())
                .limit(1)
            )
            row = result.mappings().first()
            if row is None:
                return None
            return LegacySenderRow(
                id=row["id"],
                user_id=row["user_id"],
                channel_id=int(row["channel_id"]),
                bot_username=row["bot_username"],
                username=row["username"],
                first_name=row["first_name"],
                message_id=row["message_id"],
                chat_id=row["chat_id"],
                text_post=row["text_post"],
                content_type=row["content_type"],
                media_group_id=row["media_group_id"],
                preview_file_id=row["preview_file_id"],
                preview_file_size=row["preview_file_size"],
                entities_json=row["entities_json"],
                review_chat_id=row["review_chat_id"],
                review_message_id=row["review_message_id"],
                timestamp=row["timestamp"],
            )

    async def fetch_all_bot_bindings(self) -> list[LegacyBotBinding]:
        async with legacy_db_helper.engine.connect() as conn:
            result = await conn.execute(
                select(
                    BotsData.channel_id,
                    BotsData.bot_api_token,
                    BotsData.bot_username,
                )
            )
            rows = result.mappings().all()
            return [
                LegacyBotBinding(
                    channel_id=row["channel_id"],
                    bot_api_token=row["bot_api_token"],
                    bot_username=row["bot_username"],
                )
                for row in rows
            ]

    async def get_bot_binding(self, channel_id: int) -> LegacyBotBinding | None:
        async with legacy_db_helper.engine.connect() as conn:
            result = await conn.execute(
                select(
                    BotsData.channel_id,
                    BotsData.bot_api_token,
                    BotsData.bot_username,
                ).where(BotsData.channel_id == channel_id).limit(1)
            )
            row = result.mappings().first()
            if row is None:
                return None
            return LegacyBotBinding(
                channel_id=row["channel_id"],
                bot_api_token=row["bot_api_token"],
                bot_username=row["bot_username"],
            )
