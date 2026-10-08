from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.editorial.config import settings
from src.editorial.models.channel import Channel
from src.editorial.models.content import ContentItem
from src.editorial.models.submission import Submission
from src.editorial.services.legacy_source import LegacyCollectorReader, LegacySenderRow
from src.editorial.services.tag_service import TagService
from src.editorial.utils.text import (
    clean_text,
    compute_raw_text_hash,
    compute_moderation_hash,
    compute_text_hash,
    detect_language_code,
    normalize_text,
)


SLUG_RE = re.compile(r"[^a-z0-9_]+")


@dataclass(slots=True)
class ImportLegacyResult:
    scanned: int = 0
    imported: int = 0
    skipped_duplicates: int = 0
    channels_created: int = 0
    last_legacy_id: int = 0


class LegacyImporter:
    def __init__(self, legacy_reader: LegacyCollectorReader | None = None):
        self.legacy_reader = legacy_reader or LegacyCollectorReader()
        self.tag_service = TagService()

    @staticmethod
    def _build_media_fingerprint(row: LegacySenderRow) -> str:
        message_ref = row.media_group_id or str(row.message_id or row.id)
        return f"telegram media {row.content_type or 'unknown'} {row.channel_id} {row.chat_id or 0} {message_ref}"

    @staticmethod
    def _is_service_moderation_copy(row: LegacySenderRow) -> bool:
        return bool(
            row.chat_id is not None
            and row.chat_id < 0
            and row.review_chat_id is None
            and row.review_message_id is None
        )

    async def repair_moderation_copy_links(self, session: AsyncSession) -> int:
        duplicate_submissions = list(
            (
                await session.execute(
                    select(Submission)
                    .where(
                        Submission.legacy_source == "sender_info",
                        Submission.source_chat_id.is_not(None),
                        Submission.source_chat_id < 0,
                    )
                    .order_by(Submission.id.asc())
                )
            ).scalars().all()
        )
        if not duplicate_submissions:
            return 0

        legacy_rows = await self.legacy_reader.fetch_sender_rows_by_ids(
            [submission.legacy_row_id for submission in duplicate_submissions if submission.legacy_row_id is not None]
        )
        legacy_row_map = {row.id: row for row in legacy_rows}
        repaired_count = 0

        for duplicate in duplicate_submissions:
            if duplicate.legacy_row_id is None:
                continue
            duplicate_row = legacy_row_map.get(duplicate.legacy_row_id)
            if duplicate_row is None or duplicate_row.chat_id is None or duplicate_row.message_id is None:
                continue

            original_row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=duplicate_row.channel_id,
                review_chat_id=int(duplicate_row.chat_id),
                review_message_id=int(duplicate_row.message_id),
            )
            if original_row is None or original_row.id == duplicate_row.id:
                continue

            original_submission = await session.scalar(
                select(Submission)
                .where(
                    Submission.legacy_source == "sender_info",
                    Submission.legacy_row_id == original_row.id,
                )
                .limit(1)
            )
            if original_submission is None:
                continue

            content_items = list(
                (
                    await session.execute(
                        select(ContentItem).where(ContentItem.origin_submission_id == duplicate.id)
                    )
                ).scalars().all()
            )
            if not content_items:
                continue

            for item in content_items:
                item.origin_submission_id = original_submission.id

            original_submission.is_anonymous = duplicate.is_anonymous
            original_submission.status = duplicate.status
            if duplicate.moderator_note and not original_submission.moderator_note:
                original_submission.moderator_note = duplicate.moderator_note
            if duplicate.reviewed_at and (
                original_submission.reviewed_at is None
                or duplicate.reviewed_at > original_submission.reviewed_at
            ):
                original_submission.reviewed_at = duplicate.reviewed_at

            repaired_count += len(content_items)
            logger.info(
                "Re-linked {} content items from service copy submission {} to original submission {}",
                len(content_items),
                duplicate.id,
                original_submission.id,
            )

        return repaired_count

    async def sync_channels(self, session: AsyncSession) -> int:
        created = 0
        bindings = await self.legacy_reader.fetch_all_bot_bindings()
        if not bindings:
            return created

        existing_map = {
            item.tg_channel_id: item
            for item in (
                await session.execute(select(Channel).where(Channel.tg_channel_id.in_([b.channel_id for b in bindings])))
            ).scalars()
        }

        for binding in bindings:
            if binding.channel_id in existing_map:
                channel = existing_map[binding.channel_id]
                if not channel.title:
                    channel.title = binding.bot_username
                continue

            short_code_base = binding.bot_username.replace("@", "").lower()
            short_code_base = SLUG_RE.sub("_", short_code_base).strip("_") or f"channel_{abs(binding.channel_id)}"
            short_code = short_code_base
            suffix = 1
            while await session.scalar(select(func.count()).select_from(Channel).where(Channel.short_code == short_code)):
                suffix += 1
                short_code = f"{short_code_base}_{suffix}"

            session.add(
                Channel(
                    tg_channel_id=binding.channel_id,
                    title=binding.bot_username,
                    short_code=short_code,
                    timezone=settings.default_timezone,
                    settings_profile_auto_enabled=True,
                )
            )
            created += 1

        await session.flush()
        return created

    async def _build_submission_payload(self, session: AsyncSession, row: LegacySenderRow, channel_id: int) -> dict:
        raw_text = row.text_post or ""
        cleaned_text = clean_text(raw_text)
        normalized_text = normalize_text(cleaned_text)
        text_hash = compute_text_hash(cleaned_text)
        if not normalized_text:
            if cleaned_text and (row.content_type or "text") == "text":
                normalized_text = cleaned_text
            else:
                normalized_text = self._build_media_fingerprint(row)
        if text_hash is None:
            if cleaned_text and (row.content_type or "text") == "text":
                text_hash = compute_raw_text_hash(cleaned_text) or ""
            else:
                text_hash = compute_text_hash(normalized_text) or ""
        created_at = datetime.fromtimestamp(row.timestamp, tz=timezone.utc)

        return {
            "legacy_source": "sender_info",
            "legacy_row_id": row.id,
            "channel_id": channel_id,
            "source_user_id": row.user_id,
            "source_message_id": row.message_id,
            "source_chat_id": row.chat_id,
            "content_type": (row.content_type or "text"),
            "media_group_id": row.media_group_id or None,
            "bot_username": row.bot_username,
            "username": row.username or None,
            "first_name": row.first_name or None,
            "raw_text": raw_text or None,
            "cleaned_text": cleaned_text or None,
            "normalized_text": normalized_text or None,
            "text_hash": text_hash,
            "moderation_normalized_hash": compute_moderation_hash(cleaned_text or raw_text),
            "detected_tags": await self.tag_service.detect_tags(session, cleaned_text),
            "language_code": detect_language_code(cleaned_text),
            "is_candidate_for_generation": len(cleaned_text) >= settings.minimum_submission_length,
            "is_candidate_for_paste": len(cleaned_text) >= settings.minimum_submission_length,
            "created_at": created_at,
        }

    async def ensure_media_group_submissions(
        self, session: AsyncSession, row: LegacySenderRow,
    ) -> list[Submission]:
        if not row.media_group_id or row.chat_id is None:
            raise ValueError("Legacy album has no source chat or media group ID")
        rows = await self.legacy_reader.fetch_sender_media_group_rows(
            channel_id=row.channel_id,
            source_chat_id=row.chat_id,
            media_group_id=row.media_group_id,
        )
        if len(rows) < 2 or row.id not in {item.id for item in rows}:
            raise ValueError("Legacy album is incomplete in sender_info")
        message_ids = [item.message_id for item in rows]
        if None in message_ids or len(set(message_ids)) != len(rows) or any(
            (item.channel_id, item.chat_id, item.media_group_id)
            != (row.channel_id, row.chat_id, row.media_group_id) for item in rows
        ):
            raise ValueError("Legacy album has invalid or duplicate source references")
        submissions = []
        for item in rows:
            submission = await self._ensure_single_submission_for_legacy_row(session, item)
            if submission is None:
                raise ValueError(f"Could not import legacy album row {item.id}")
            if (submission.source_chat_id, submission.source_message_id, submission.media_group_id) != (
                item.chat_id, item.message_id, item.media_group_id,
            ):
                raise ValueError(f"Legacy album row {item.id} has inconsistent submission references")
            submissions.append(submission)
        return submissions

    async def ensure_submission_for_legacy_row(
        self, session: AsyncSession, row: LegacySenderRow,
    ) -> Submission | None:
        if row.id is None or self._is_service_moderation_copy(row):
            return None
        if row.media_group_id:
            related = await self.ensure_media_group_submissions(session, row)
            return next(item for item in related if item.legacy_row_id == row.id)
        return await self._ensure_single_submission_for_legacy_row(session, row)

    async def _ensure_single_submission_for_legacy_row(
        self,
        session: AsyncSession,
        row: LegacySenderRow,
        channel_id: int | None = None,
    ) -> Submission | None:
        if row.id is None or self._is_service_moderation_copy(row):
            return None

        existing = await session.scalar(
            select(Submission)
            .where(
                Submission.legacy_source == "sender_info",
                Submission.legacy_row_id == row.id,
            )
            .limit(1)
        )
        if existing is not None:
            return existing

        if channel_id is None:
            await self.sync_channels(session)
            channel_id = await session.scalar(
                select(Channel.id).where(Channel.tg_channel_id == row.channel_id).limit(1)
            )
        if channel_id is None:
            logger.warning("Skipping legacy row {} because channel {} is unknown", row.id, row.channel_id)
            return None

        payload = await self._build_submission_payload(session, row, channel_id)
        if row.media_group_id:
            sibling = await session.scalar(select(Submission).where(
                Submission.channel_id == channel_id,
                Submission.source_chat_id == row.chat_id,
                Submission.media_group_id == row.media_group_id,
            ).order_by(Submission.reviewed_at.desc().nullslast(), Submission.id).limit(1))
            if sibling is not None:
                # Backfilled media belongs to the existing moderation decision.
                # It must not reappear as a new proposal or lose anonymity.
                payload.update(
                    status=sibling.status, is_anonymous=sibling.is_anonymous,
                    reviewed_at=sibling.reviewed_at, moderator_note=sibling.moderator_note,
                )
        insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
        # Collector callbacks and the importer can discover the same row together.
        await session.execute(insert(Submission).values(**payload).on_conflict_do_nothing(
            index_elements=[Submission.legacy_source, Submission.legacy_row_id],
        ))
        return await session.scalar(select(Submission).where(
            Submission.legacy_source == "sender_info", Submission.legacy_row_id == row.id,
        ))

    async def import_new(self, session: AsyncSession, limit: int | None = None) -> ImportLegacyResult:
        result = ImportLegacyResult()
        result.channels_created = await self.sync_channels(session)

        rows = await self.legacy_reader.fetch_unimported_sender_rows(
            session, limit=limit or settings.legacy_import_batch_size,
        )

        if not rows:
            repaired_count = await self.repair_moderation_copy_links(session)
            if repaired_count:
                logger.info("Repaired {} existing content-item links from moderation-copy submissions", repaired_count)
            await session.commit()
            return result

        channels = {
            item.tg_channel_id: item.id
            for item in (await session.execute(select(Channel))).scalars()
        }

        for row in rows:
            result.scanned += 1
            result.last_legacy_id = row.id

            if row.id is None or row.channel_id not in channels:
                logger.warning("Skipping legacy row {} because channel {} is unknown", row.id, row.channel_id)
                continue

            if self._is_service_moderation_copy(row):
                logger.info("Skipping service moderation copy row {}", row.id)
                continue

            exists = await session.scalar(
                select(func.count())
                .select_from(Submission)
                .where(
                    Submission.legacy_source == "sender_info",
                    Submission.legacy_row_id == row.id,
                )
            )
            if exists:
                result.skipped_duplicates += 1
                continue

            await self._ensure_single_submission_for_legacy_row(
                session, row, channel_id=channels[row.channel_id],
            )
            result.imported += 1

        repaired_count = await self.repair_moderation_copy_links(session)
        if repaired_count:
            logger.info("Repaired {} existing content-item links from moderation-copy submissions", repaired_count)

        await session.commit()
        return result
