from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import delete as sql_delete, func, or_, select, update as sql_update
from telebot.async_telebot import AsyncTeleBot

from src.core_database.database import CrudBannedUser
from src.core_database.models.db_helper import db_helper as legacy_db_helper
from src.core_database.models.sender_info import SenderData
from src.editorial.db.session import session_factory
from src.editorial.models.channel import Channel
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import (
    ChannelPasteTagRuleMode,
    ContentFamily,
    ContentItemStatus,
    ContentSourceType,
    PasteStatus,
    PublicationStatus,
    ReviewDecision,
    SubmissionStatus,
)
from src.editorial.models.moderation_subscription import ModerationChannelSubscription
from src.editorial.models.notification import NotificationSubscription
from src.editorial.models.paste import PasteLibrary
from src.editorial.models.publication import PublicationLog
from src.editorial.models.submission import Submission
from src.editorial.models.tag import ChannelPasteTagRule, GlobalPasteTagRule, TagDefinition, TagKeyword
from src.editorial.services.ad_link_exclusion_service import AdLinkExclusionService
from src.editorial.services.advertising import send_advertising_flow
from src.editorial.services.admin_statistics_export import AdminStatisticsExportService
from src.editorial.services.auto_slot_planner import AutoSlotPlannerService
from src.editorial.services.channel_profile_service import ChannelProfileService
from src.editorial.services.channel_history_service import ChannelHistoryImportResult, ChannelHistoryService
from src.editorial.services.channel_service import ChannelService
from src.editorial.services.confession_service import ConfessionService
from src.editorial.services.generation.service import GenerationService
from src.editorial.services.import_legacy import LegacyImporter
from src.editorial.services.legacy_moderation_sync import LegacyModerationSyncService
from src.editorial.services.legacy_source import LegacyCollectorReader
from src.editorial.services.moderation import ModerationService
from src.editorial.services.moderation_case_service import (
    MODERATION_REJECTED,
    ModerationCaseService,
)
from src.editorial.services.paste_service import PasteService
from src.editorial.services.publisher import PublisherService
from src.editorial.services.scheduler import SchedulerService
from src.editorial.services.statistics_export import StatisticsExportService
from src.editorial.services.tag_service import PasteTagSummary, TagService
from src.editorial.services.telegram_resilience import is_transient_telegram_error
from src.editorial.utils.text import clean_text, compute_raw_text_hash, compute_text_hash, normalize_text


@dataclass(slots=True)
class SubmissionPreview:
    channel_tg_id: int
    review_chat_id: int
    review_message_ids: list[int]
    preview_file_ids: list[str]
    preview_file_sizes: list[int]
    preview_content_types: list[str]
    content_type: str
    media_group_id: str | None


@dataclass(slots=True)
class SubmissionBanResult:
    submission_id: int
    user_id: int
    username: str | None
    channel_tg_id: int
    already_banned: bool


@dataclass(slots=True)
class ManualChannelMessageResult:
    requested: int = 0
    sent: int = 0
    deferred: int = 0
    blocked: int = 0
    failed: int = 0
    content_item_ids: list[int] = field(default_factory=list)
    publication_log_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PasteAvailabilityReason:
    code: str
    title: str
    count: int = 0
    examples: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ChannelPasteDiagnostics:
    channel_id: int
    channel_title: str
    is_active: bool
    allow_pastes: bool
    max_paste_per_day: int
    same_paste_cooldown_days: int
    same_tag_cooldown_hours: int
    total_pastes: int
    active_pastes: int
    available_pastes: int
    approved_ready_paste_items: int
    scheduled_pastes_today: int
    sent_pastes_today: int
    next_available_examples: list[str]
    reasons: list[PasteAvailabilityReason]


class TelegramEditorialActions:
    def __init__(self) -> None:
        self.importer = LegacyImporter()
        self.legacy_reader = LegacyCollectorReader()
        self.legacy_moderation_sync = LegacyModerationSyncService(
            legacy_reader=self.legacy_reader,
            importer=self.importer,
        )
        self.moderation = ModerationService()
        self.moderation_cases = ModerationCaseService()
        self.paste_service = PasteService()
        self.channel_history_service = ChannelHistoryService()
        self.channel_service = ChannelService()
        self.confession_service = ConfessionService()
        self.auto_slot_planner = AutoSlotPlannerService()
        self.channel_profile_service = ChannelProfileService(legacy_reader=self.legacy_reader)
        self.ad_link_exclusion_service = AdLinkExclusionService()
        self.tag_service = TagService()
        self.scheduler = SchedulerService()
        self.publisher = PublisherService()
        self.statistics_export_service = StatisticsExportService()
        self.admin_statistics_export_service = AdminStatisticsExportService()
        self.banned_users = CrudBannedUser()
        self._legacy_bot_id_cache: dict[str, int] = {}

    async def import_new(self):
        async with session_factory() as session:
            return await self.importer.import_new(session)

    async def sync_channel_activity_from_bindings(self) -> None:
        bindings = await self.legacy_reader.fetch_all_bot_bindings()
        active_tg_channel_ids = [int(binding.channel_id) for binding in bindings]

        async with session_factory() as session:
            await self.importer.sync_channels(session)
            if active_tg_channel_ids:
                await session.execute(
                    sql_update(Channel)
                    .where(
                        Channel.content_family == ContentFamily.OVERHEARD.value,
                        Channel.tg_channel_id.in_(active_tg_channel_ids),
                    )
                    .values(is_active=True)
                )
                await session.execute(
                    sql_update(Channel)
                    .where(
                        Channel.content_family == ContentFamily.OVERHEARD.value,
                        ~Channel.tg_channel_id.in_(active_tg_channel_ids),
                    )
                    .values(is_active=False)
                )
            else:
                await session.execute(
                    sql_update(Channel)
                    .where(Channel.content_family == ContentFamily.OVERHEARD.value)
                    .values(is_active=False)
                )
            await session.commit()

    async def list_channels(self) -> list[Channel]:
        await self.sync_channel_activity_from_bindings()
        async with session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(Channel)
                        .where(
                            Channel.is_active.is_(True),
                            Channel.content_family == ContentFamily.OVERHEARD.value,
                        )
                        .order_by(Channel.id.asc())
                    )
                ).scalars().all()
            )

    async def list_profile_channels(self) -> list[Channel]:
        """Return every active channel that can share a settings profile."""
        await self.sync_channel_activity_from_bindings()
        async with session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(Channel)
                        .where(Channel.is_active.is_(True))
                        .order_by(Channel.id.asc())
                    )
                ).scalars().all()
            )

    async def get_channel(self, channel_id: int) -> Channel | None:
        async with session_factory() as session:
            return await self.channel_service.get_channel(session, channel_id)

    async def is_channel_notifications_enabled(self, channel_id: int, user_id: int) -> bool:
        async with session_factory() as session:
            subscription = await session.scalar(
                select(NotificationSubscription)
                .where(
                    NotificationSubscription.channel_id == channel_id,
                    NotificationSubscription.user_id == user_id,
                )
                .limit(1)
            )
            return subscription is not None

    async def toggle_channel_notifications(self, channel_id: int, user_id: int) -> bool:
        async with session_factory() as session:
            subscription = await session.scalar(
                select(NotificationSubscription)
                .where(
                    NotificationSubscription.channel_id == channel_id,
                    NotificationSubscription.user_id == user_id,
                )
                .limit(1)
            )
            if subscription is None:
                session.add(
                    NotificationSubscription(
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                )
                await session.commit()
                return True

            await session.delete(subscription)
            await session.commit()
            return False

    async def is_channel_moderation_feed_enabled(self, channel_id: int, user_id: int) -> bool:
        async with session_factory() as session:
            subscription = await session.scalar(
                select(ModerationChannelSubscription)
                .where(
                    ModerationChannelSubscription.channel_id == channel_id,
                    ModerationChannelSubscription.user_id == user_id,
                )
                .limit(1)
            )
            return subscription is not None

    async def toggle_channel_moderation_feed(self, channel_id: int, user_id: int) -> bool:
        async with session_factory() as session:
            subscription = await session.scalar(
                select(ModerationChannelSubscription)
                .where(
                    ModerationChannelSubscription.channel_id == channel_id,
                    ModerationChannelSubscription.user_id == user_id,
                )
                .limit(1)
            )
            if subscription is None:
                session.add(
                    ModerationChannelSubscription(
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                )
                await session.commit()
                return True

            await session.delete(subscription)
            await session.commit()
            return False

    async def list_user_moderation_feed_channel_ids(self, user_id: int) -> list[int]:
        await self.sync_channel_activity_from_bindings()
        async with session_factory() as session:
            return [
                int(channel_id)
                for channel_id in (
                    await session.execute(
                        select(ModerationChannelSubscription.channel_id)
                        .join(Channel, Channel.id == ModerationChannelSubscription.channel_id)
                        .where(ModerationChannelSubscription.user_id == user_id)
                        .where(Channel.is_active.is_(True))
                        .order_by(ModerationChannelSubscription.channel_id.asc())
                    )
                ).scalars().all()
            ]

    async def list_user_moderation_feed_channels(self, user_id: int) -> list[Channel]:
        await self.sync_channel_activity_from_bindings()
        async with session_factory() as session:
            stmt = (
                select(Channel)
                .join(ModerationChannelSubscription, ModerationChannelSubscription.channel_id == Channel.id)
                .where(ModerationChannelSubscription.user_id == user_id)
                .where(Channel.is_active.is_(True))
                .order_by(Channel.id.asc())
            )
            return list((await session.execute(stmt)).scalars().all())

    async def list_channel_notification_user_ids(self, channel_id: int) -> list[int]:
        async with session_factory() as session:
            return [
                int(user_id)
                for user_id in (
                    await session.execute(
                        select(NotificationSubscription.user_id)
                        .where(NotificationSubscription.channel_id == channel_id)
                        .order_by(NotificationSubscription.user_id.asc())
                    )
                ).scalars().all()
            ]

    async def ensure_channel_for_tg_channel_id(self, tg_channel_id: int) -> Channel:
        async with session_factory() as session:
            await self.importer.sync_channels(session)
            channel = await session.scalar(
                select(Channel).where(Channel.tg_channel_id == tg_channel_id).limit(1)
            )
            if channel is None:
                raise ValueError(f"Channel with tg id {tg_channel_id} not found")
            channel.is_active = True
            await session.commit()
            await session.refresh(channel)
            return channel

    async def deactivate_channel_by_tg_channel_id(self, tg_channel_id: int) -> bool:
        async with session_factory() as session:
            channel = await session.scalar(
                select(Channel).where(Channel.tg_channel_id == tg_channel_id).limit(1)
            )
            if channel is None:
                return False

            # A confession channel is still connected through the shared
            # confession publisher when its suggestion bot is removed.
            if channel.content_family == ContentFamily.CONFESSION.value:
                await session.commit()
                return True

            channel.is_active = False

            await session.execute(
                sql_update(PublicationLog)
                .where(
                    PublicationLog.channel_id == channel.id,
                    PublicationLog.publish_status == PublicationStatus.SCHEDULED,
                )
                .values(
                    publish_status=PublicationStatus.CANCELLED,
                    error_text="Channel was unlinked from the panel",
                )
            )
            await session.execute(
                sql_update(ContentItem)
                .where(
                    ContentItem.channel_id == channel.id,
                    ContentItem.status == ContentItemStatus.SCHEDULED,
                )
                .values(
                    status=ContentItemStatus.APPROVED,
                    scheduled_for=None,
                )
            )
            await session.commit()
            return True

    async def ensure_submission_for_review_message(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
    ) -> Submission | None:
        async with session_factory() as session:
            row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=channel_tg_id,
                review_chat_id=review_chat_id,
                review_message_id=review_message_id,
            )
            if row is None:
                return None

            submission = await self.importer.ensure_submission_for_legacy_row(session, row)
            if submission is None:
                return None

            await session.commit()
            await session.refresh(submission)
            return submission

    async def list_channel_slots(self, channel_id: int):
        async with session_factory() as session:
            return await self.channel_service.list_channel_slots(session, channel_id)

    async def seed_default_slots(self, channel_id: int) -> int:
        async with session_factory() as session:
            created = await self.channel_service.seed_daily_slots(
                session=session,
                channel_id=channel_id,
                slot_times=["10:00", "15:00", "20:00"],
                weekdays=[0, 1, 2, 3, 4, 5, 6],
            )
            return len(created)

    async def add_slots(self, channel_id: int, slot_times: Iterable[str], weekdays: Iterable[int]) -> int:
        async with session_factory() as session:
            created = await self.channel_service.seed_daily_slots(
                session=session,
                channel_id=channel_id,
                slot_times=list(slot_times),
                weekdays=list(weekdays),
            )
            return len(created)

    async def remove_slot(self, slot_id: int):
        async with session_factory() as session:
            return await self.channel_service.delete_slot(session, slot_id)

    async def remove_slots(self, channel_id: int, slot_times: Iterable[str], weekdays: Iterable[int]) -> int:
        async with session_factory() as session:
            removed = await self.channel_service.delete_slots(
                session=session,
                channel_id=channel_id,
                slot_times=list(slot_times),
                weekdays=list(weekdays),
            )
            return len(removed)

    async def create_channel_ad_blackout(
        self,
        *,
        channel_id: int,
        day_of_month: int,
        start_time: str,
        end_time: str,
        created_by: int | None = None,
    ):
        async with session_factory() as session:
            return await self.channel_service.create_ad_blackout(
                session=session,
                channel_id=channel_id,
                day_of_month=day_of_month,
                start_time=start_time,
                end_time=end_time,
                created_by=created_by,
            )

    async def list_channel_ad_blackouts(self, channel_id: int, limit: int = 5):
        async with session_factory() as session:
            return await self.channel_service.list_upcoming_ad_blackouts(
                session=session,
                channel_id=channel_id,
                limit=limit,
            )

    async def delete_channel_ad_blackout(
        self,
        *,
        channel_id: int,
        day_of_month: int,
        start_time: str,
        end_time: str,
    ):
        async with session_factory() as session:
            return await self.channel_service.delete_ad_blackout(
                session=session,
                channel_id=channel_id,
                day_of_month=day_of_month,
                start_time=start_time,
                end_time=end_time,
            )

    async def list_ad_link_exclusions(self):
        async with session_factory() as session:
            return await self.ad_link_exclusion_service.list_exclusions(session)

    async def list_normalized_ad_link_exclusions(self) -> set[str]:
        async with session_factory() as session:
            return await self.ad_link_exclusion_service.list_normalized_links(session)

    async def add_ad_link_exclusion(
        self,
        *,
        url: str,
        created_by: int | None,
    ):
        async with session_factory() as session:
            return await self.ad_link_exclusion_service.add_exclusion(
                session,
                url=url,
                created_by=created_by,
            )

    async def delete_ad_link_exclusion(self, *, url: str):
        async with session_factory() as session:
            return await self.ad_link_exclusion_service.delete_exclusion(session, url=url)

    async def update_channel_setting(self, channel_id: int, field_name: str, raw_value: str) -> Channel:
        async with session_factory() as session:
            return await self.channel_service.update_channel_setting(
                session=session,
                channel_id=channel_id,
                field_name=field_name,
                raw_value=raw_value,
            )

    async def get_channel_settings_snapshot(self, channel_id: int) -> list[tuple[str, object]]:
        async with session_factory() as session:
            channel = await self.channel_service.get_channel(session, channel_id)
            if channel is None:
                raise ValueError(f"Channel {channel_id} not found")
            return self.channel_service.editable_settings_snapshot(channel)

    def get_editable_channel_setting_names(self) -> list[str]:
        return self.channel_service.editable_settings_names()

    async def import_channel_history_message(
        self,
        *,
        channel_id: int,
        source_chat_id: int,
        source_message_id: int,
        content_type: str,
        raw_text: str | None,
        original_published_at: datetime | None,
        imported_by: int | None,
    ) -> ChannelHistoryImportResult:
        async with session_factory() as session:
            return await self.channel_history_service.import_message(
                session=session,
                channel_id=channel_id,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                content_type=content_type,
                raw_text=raw_text,
                original_published_at=original_published_at,
                imported_by=imported_by,
            )

    async def list_pending_submissions(self, user_id: int | None = None) -> list[Submission]:
        async with session_factory() as session:
            stmt = (
                select(Submission)
                .where(
                    Submission.status.in_([SubmissionStatus.NEW, SubmissionStatus.HOLD]),
                    or_(Submission.source_chat_id.is_(None), Submission.source_chat_id >= 0),
                )
                .order_by(Submission.created_at.asc())
                .limit(50)
            )
            if user_id is not None:
                selected_channel_ids = [
                    int(channel_id)
                    for channel_id in (
                        await session.execute(
                            select(ModerationChannelSubscription.channel_id)
                            .where(ModerationChannelSubscription.user_id == user_id)
                            .order_by(ModerationChannelSubscription.channel_id.asc())
                        )
                    ).scalars().all()
                ]
                if selected_channel_ids:
                    stmt = stmt.where(Submission.channel_id.in_(selected_channel_ids))
            items = list((await session.execute(stmt)).scalars().all())
            return self.moderation.collapse_media_groups(items)

    async def list_recent_submissions(self, limit: int | None = None) -> list[Submission]:
        async with session_factory() as session:
            items = await self.moderation.list_submissions(session=session, status=None, limit=limit)
            return self.moderation.collapse_media_groups(items)

    async def get_submission(self, submission_id: int) -> Submission | None:
        async with session_factory() as session:
            return await session.get(Submission, submission_id)

    async def delete_submission(self, submission_id: int) -> int:
        async with session_factory() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                raise ValueError(f"Submission {submission_id} not found")

            related_submissions = await self.moderation.get_related_submissions(session, submission)
            legacy_row_ids = [item.legacy_row_id for item in related_submissions if item.legacy_row_id is not None]
            deleted_count = len(related_submissions)

            for item in related_submissions:
                await session.delete(item)
            await session.commit()

        if legacy_row_ids:
            async with legacy_db_helper.engine.begin() as conn:
                await conn.execute(
                    sql_delete(SenderData).where(SenderData.id.in_(legacy_row_ids))
                )

        return deleted_count

    async def set_submission_anonymous(self, submission_id: int, is_anonymous: bool) -> Submission:
        async with session_factory() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                raise ValueError(f"Submission {submission_id} not found")
            related = await self.moderation.get_related_submissions(session, submission)
            for item in related:
                item.is_anonymous = is_anonymous
            await session.commit()
            await session.refresh(submission)
            return submission

    async def get_submission_preview(self, submission_id: int) -> SubmissionPreview | None:
        async with session_factory() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                return None
            related_submissions = await self.moderation.get_related_submissions(session, submission)
            legacy_row_ids = [item.legacy_row_id for item in related_submissions if item.legacy_row_id is not None]
            legacy_rows = await self.legacy_reader.fetch_sender_rows_by_ids(legacy_row_ids)
            preview_rows = [
                row for row in legacy_rows
                if row.review_chat_id is not None and row.review_message_id is not None
            ]
            channel = await session.get(Channel, submission.channel_id)
            if not preview_rows or channel is None:
                return None
            review_chat_id = int(preview_rows[0].review_chat_id)
            review_message_ids = [int(row.review_message_id) for row in preview_rows]
            preview_file_ids = [row.preview_file_id for row in preview_rows if row.preview_file_id]
            preview_file_sizes = [int(row.preview_file_size or 0) for row in preview_rows if row.preview_file_id]
            preview_content_types = [row.content_type or "photo" for row in preview_rows if row.preview_file_id]
            return SubmissionPreview(
                channel_tg_id=int(channel.tg_channel_id),
                review_chat_id=review_chat_id,
                review_message_ids=sorted(set(review_message_ids)),
                preview_file_ids=preview_file_ids,
                preview_file_sizes=preview_file_sizes,
                preview_content_types=preview_content_types,
                content_type=submission.content_type,
                media_group_id=submission.media_group_id,
            )

    async def get_submission_primary_content_item(self, submission_id: int) -> ContentItem | None:
        priority_order = {
            ContentItemStatus.PUBLISHED: 0,
            ContentItemStatus.SCHEDULED: 1,
            ContentItemStatus.APPROVED: 2,
            ContentItemStatus.PENDING_REVIEW: 3,
            ContentItemStatus.HOLD: 4,
            ContentItemStatus.REJECTED: 5,
            ContentItemStatus.DRAFT: 6,
        }

        async with session_factory() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                return None
            related_submissions = await self.moderation.get_related_submissions(session, submission)
            submission_ids = [item.id for item in related_submissions]
            items = list(
                (
                    await session.execute(
                        select(ContentItem)
                        .where(ContentItem.origin_submission_id.in_(submission_ids))
                        .order_by(ContentItem.created_at.desc())
                    )
                ).scalars().all()
            )
            if not items:
                return None
            return min(
                items,
                key=lambda item: (
                    priority_order.get(item.status, 99),
                    -(item.id or 0),
                ),
            )

    async def approve_submission(self, submission_id: int, reviewer_id: int) -> ContentItem:
        async with session_factory() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                raise ValueError(f"Submission {submission_id} not found")
            channel = await session.get(Channel, submission.channel_id)
            if channel is None or not channel.is_active:
                raise ValueError("Channel is inactive or unlinked")
            item = await self._get_or_create_content_item(session, submission_id)
            item = await self.moderation.review_content_item(
                session=session,
                content_item_id=item.id,
                reviewer_id=reviewer_id,
                decision=ReviewDecision.APPROVE,
                review_note="Approved in Telegram panel",
                moderation_source="panel",
                moderation_action="approve_submission",
            )
            return item

    async def publish_submission_now(self, submission_id: int, reviewer_id: int) -> PublicationLog:
        async with session_factory() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                raise ValueError(f"Submission {submission_id} not found")
            channel = await session.get(Channel, submission.channel_id)
            if channel is None or not channel.is_active:
                raise ValueError("Channel is inactive or unlinked")
            now = datetime.now(timezone.utc)
            if await self.channel_service.is_channel_in_ad_blackout(session, submission.channel_id, now):
                raise ValueError(
                    "Сейчас для этого канала активно рекламное окно. Publish now временно заблокирован, чтобы не перебить рекламу."
                )
            item = await self._get_or_create_content_item(session, submission_id)
            item = await self.moderation.review_content_item(
                session=session,
                content_item_id=item.id,
                reviewer_id=reviewer_id,
                decision=ReviewDecision.APPROVE,
                review_note="Approved and published in Telegram panel",
                moderation_source="panel",
                moderation_action="publish_submission",
            )
            log_item = await self._schedule_now(session, item)
            await self.publisher.run(session, now=datetime.now(timezone.utc), limit=20)
            return log_item

    async def reject_submission(
        self,
        submission_id: int,
        reviewer_id: int,
        note: str = "Rejected in Telegram panel",
    ) -> Submission:
        async with session_factory() as session:
            submission = await self.moderation.set_submission_status(
                session=session,
                submission_id=submission_id,
                status=SubmissionStatus.REJECTED,
                moderator_note=note,
            )
            await self.moderation_cases.record_submission_decision(
                session,
                submission_id=submission_id,
                moderator_id=reviewer_id,
                decision=MODERATION_REJECTED,
                source="panel",
                action="reject_submission",
            )
            await session.commit()
            return submission

    async def hold_submission(
        self,
        submission_id: int,
        reviewer_id: int,
        note: str = "Hold in Telegram panel",
    ) -> Submission:
        async with session_factory() as session:
            submission = await self.moderation.set_submission_status(
                session=session,
                submission_id=submission_id,
                status=SubmissionStatus.HOLD,
                moderator_note=note,
            )
            await self.moderation_cases.void_submission_case(
                session,
                submission_id=submission_id,
                moderator_id=reviewer_id,
                source="panel",
                action="hold_submission",
            )
            await session.commit()
            return submission

    async def advertise_submission(
        self,
        submission_id: int,
        reviewer_id: int,
        note: str = "Advertising reply sent in Telegram panel",
    ) -> Submission:
        await self.send_submission_advertising_reply_v2(submission_id)
        async with session_factory() as session:
            submission = await self.moderation.set_submission_status(
                session=session,
                submission_id=submission_id,
                status=SubmissionStatus.ADVERTISING,
                moderator_note=note,
                commit=False,
            )
            await self.moderation_cases.void_submission_case(
                session,
                submission_id=submission_id,
                moderator_id=reviewer_id,
                source="panel",
                action="advertise_submission",
            )
            await session.commit()
            return submission

    async def paste_submission(self, submission_id: int, reviewer_id: int):
        async with session_factory() as session:
            return await self.paste_service.create_paste_from_submission(
                session=session,
                submission_id=submission_id,
                created_by=reviewer_id,
            )

    async def reply_to_submission_author(self, submission_id: int, text: str) -> None:
        async with session_factory() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                raise ValueError(f"Submission {submission_id} not found")
            if submission.source_user_id is None:
                raise ValueError("Submission has no source user id")
            channel = await session.get(Channel, submission.channel_id)
            if channel is None:
                raise ValueError(f"Channel {submission.channel_id} not found")

        binding = await self.legacy_reader.get_bot_binding(channel.tg_channel_id)
        if binding is None:
            raise ValueError(f"Legacy bot binding for channel {channel.tg_channel_id} not found")

        bot = AsyncTeleBot(binding.bot_api_token)
        await bot.send_message(chat_id=submission.source_user_id, text=text)

    async def send_submission_advertising_reply(self, submission_id: int) -> None:
        await self.reply_to_submission_author(
            submission_id,
            "По рекламе напишите пожалуйста @ivanblk, сразу укажите, что вы хотите рекламировать",
        )

    async def send_submission_advertising_reply_v2(self, submission_id: int) -> None:
        async with session_factory() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                raise ValueError(f"Submission {submission_id} not found")
            if submission.source_user_id is None:
                raise ValueError("Submission has no source user id")
            channel = await session.get(Channel, submission.channel_id)
            if channel is None:
                raise ValueError(f"Channel {submission.channel_id} not found")

        binding = await self.legacy_reader.get_bot_binding(channel.tg_channel_id)
        if binding is None:
            raise ValueError(f"Legacy bot binding for channel {channel.tg_channel_id} not found")

        bot = AsyncTeleBot(binding.bot_api_token)
        channel_label = channel.title or str(channel.tg_channel_id)
        try:
            telegram_channel = await bot.get_chat(channel.tg_channel_id)
            channel_username = getattr(telegram_channel, "username", None)
            if channel_username:
                channel_label = f"@{channel_username}"
            else:
                channel_label = getattr(telegram_channel, "title", None) or channel_label
        except Exception:
            pass

        await send_advertising_flow(
            bot=bot,
            recipient_user_id=int(submission.source_user_id),
            channel_label=channel_label,
            source_text=submission.raw_text or submission.cleaned_text,
            sender_username=submission.username,
            sender_first_name=submission.first_name,
        )

    async def ban_submission_author(
        self,
        submission_id: int,
        reviewer_id: int | None = None,
    ) -> SubmissionBanResult:
        async with session_factory() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                raise ValueError(f"Submission {submission_id} not found")
            if submission.source_user_id is None:
                raise ValueError("Submission has no source user id")

            channel = await session.get(Channel, submission.channel_id)
            if channel is None:
                raise ValueError(f"Channel {submission.channel_id} not found")

            await self.moderation.set_submission_status(
                session=session,
                submission_id=submission_id,
                status=SubmissionStatus.REJECTED,
                moderator_note=(
                    f"Banned in Telegram panel by {reviewer_id}"
                    if reviewer_id is not None
                    else "Banned in Telegram panel"
                ),
            )
            if reviewer_id is not None:
                await self.moderation_cases.record_submission_decision(
                    session,
                    submission_id=submission_id,
                    moderator_id=reviewer_id,
                    decision=MODERATION_REJECTED,
                    source="panel",
                    action="ban_submission_author",
                )
                await session.commit()
            user_id = int(submission.source_user_id)
            username = submission.username
            tg_channel_id = int(channel.tg_channel_id)

        binding = await self.legacy_reader.get_bot_binding(tg_channel_id)
        if binding is None:
            raise ValueError(f"Legacy bot binding for channel {tg_channel_id} not found")

        already_banned = bool(
            await self.banned_users.get_banned_users(id_user=user_id, id_channel=tg_channel_id)
        )
        if not already_banned:
            bot_id = await self._get_legacy_bot_id(binding.bot_api_token)
            await self.banned_users.add_banned_user(
                {
                    "id_user": user_id,
                    "id_channel": tg_channel_id,
                    "bot_id": bot_id,
                }
            )

        return SubmissionBanResult(
            submission_id=submission_id,
            user_id=user_id,
            username=username,
            channel_tg_id=tg_channel_id,
            already_banned=already_banned,
        )

    async def list_pending_content_items(self) -> list[ContentItem]:
        await self.sync_channel_activity_from_bindings()
        async with session_factory() as session:
            stmt = (
                select(ContentItem)
                .join(Channel, Channel.id == ContentItem.channel_id)
                .where(
                    ContentItem.status == ContentItemStatus.PENDING_REVIEW,
                    Channel.is_active.is_(True),
                )
                .order_by(ContentItem.created_at.asc())
                .limit(50)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def get_content_item(self, content_item_id: int) -> ContentItem | None:
        async with session_factory() as session:
            return await session.get(ContentItem, content_item_id)

    async def update_content_item_text(self, content_item_id: int, body_text: str) -> ContentItem:
        cleaned_body = clean_text(body_text)
        if not cleaned_body:
            raise ValueError("Content item text is empty")

        async with session_factory() as session:
            item = await session.get(ContentItem, content_item_id)
            if item is None:
                raise ValueError(f"Content item {content_item_id} not found")
            if item.status not in {ContentItemStatus.PENDING_REVIEW, ContentItemStatus.HOLD}:
                raise ValueError("Only pending or held content items can be edited")

            tags, primary_tag = await self.tag_service.apply_tags_to_content_cache(session, cleaned_body)
            item.body_text = cleaned_body
            item.normalized_text = normalize_text(cleaned_body)
            item.text_hash = compute_text_hash(cleaned_body) or compute_raw_text_hash(cleaned_body) or ""
            item.tags = tags
            item.primary_tag = primary_tag
            await session.commit()
            await session.refresh(item)
            return item

    async def list_pastes(self, limit: int | None = None) -> list[PasteLibrary]:
        async with session_factory() as session:
            return await self.paste_service.list_pastes(
                session=session,
                status=None,
                limit=limit,
                content_family=ContentFamily.OVERHEARD.value,
            )

    async def get_active_confession_publisher(self):
        async with session_factory() as session:
            return await self.confession_service.get_active_publisher(session)

    async def configure_confession_publisher(
        self,
        *,
        bot_api_token: str,
        bot_user_id: int,
        bot_username: str | None,
        created_by: int | None,
    ):
        async with session_factory() as session:
            return await self.confession_service.configure_publisher(
                session,
                bot_api_token=bot_api_token,
                bot_user_id=bot_user_id,
                bot_username=bot_username,
                created_by=created_by,
            )

    async def refresh_confession_bind_code(self, publisher_id: int):
        async with session_factory() as session:
            return await self.confession_service.refresh_bind_code(session, publisher_id)

    async def list_confession_pastes(self, limit: int | None = None) -> list[PasteLibrary]:
        async with session_factory() as session:
            return await self.confession_service.list_pastes(session, limit=limit)

    async def list_confession_channels(self) -> list[Channel]:
        async with session_factory() as session:
            return await self.confession_service.list_channels(session)

    async def ensure_confession_channel(
        self,
        *,
        tg_channel_id: int,
        title: str | None,
        username: str | None,
    ) -> Channel:
        async with session_factory() as session:
            return await self.confession_service.ensure_channel(
                session,
                tg_channel_id=tg_channel_id,
                title=title,
                username=username,
            )

    async def get_paste(self, paste_id: int) -> PasteLibrary | None:
        async with session_factory() as session:
            return await session.get(PasteLibrary, paste_id)

    async def get_channel_paste_diagnostics(self, channel_id: int) -> ChannelPasteDiagnostics:
        async with session_factory() as session:
            channel = await session.get(Channel, channel_id)
            if channel is None:
                raise ValueError(f"Channel {channel_id} not found")

            now = datetime.now(timezone.utc)
            day_start_utc, day_end_utc = self.scheduler._channel_day_bounds(channel.timezone, now)

            pastes = list(
                (
                    await session.execute(
                        select(PasteLibrary)
                        .where(PasteLibrary.content_family == channel.content_family)
                        .order_by(PasteLibrary.updated_at.desc())
                    )
                ).scalars().all()
            )
            active_pastes = [paste for paste in pastes if paste.status == PasteStatus.ACTIVE]

            scheduled_pastes_today = await session.scalar(
                select(func.count())
                .select_from(PublicationLog)
                .join(ContentItem, ContentItem.id == PublicationLog.content_item_id)
                .where(
                    PublicationLog.channel_id == channel.id,
                    PublicationLog.scheduled_for >= day_start_utc,
                    PublicationLog.scheduled_for < day_end_utc,
                    PublicationLog.publish_status == PublicationStatus.SCHEDULED,
                    ContentItem.source_type == ContentSourceType.PASTE,
                )
            )
            sent_pastes_today = await session.scalar(
                select(func.count())
                .select_from(PublicationLog)
                .join(ContentItem, ContentItem.id == PublicationLog.content_item_id)
                .where(
                    PublicationLog.channel_id == channel.id,
                    PublicationLog.scheduled_for >= day_start_utc,
                    PublicationLog.scheduled_for < day_end_utc,
                    PublicationLog.publish_status == PublicationStatus.SENT,
                    ContentItem.source_type == ContentSourceType.PASTE,
                )
            )
            approved_ready_paste_items = await session.scalar(
                select(func.count())
                .select_from(ContentItem)
                .where(
                    ContentItem.channel_id == channel.id,
                    ContentItem.source_type == ContentSourceType.PASTE,
                    ContentItem.status == ContentItemStatus.APPROVED,
                    (ContentItem.publish_after.is_(None) | (ContentItem.publish_after <= now)),
                    (ContentItem.expires_at.is_(None) | (ContentItem.expires_at > now)),
                )
            )

            reasons_by_code = {
                "inactive": PasteAvailabilityReason("inactive", "не active"),
                "channel_disabled": PasteAvailabilityReason("channel_disabled", "канал выключен"),
                "pastes_disabled": PasteAvailabilityReason("pastes_disabled", "allow_pastes выключен"),
                "channel_rule": PasteAvailabilityReason("channel_rule", "не разрешены для этого канала"),
                "tag_rule": PasteAvailabilityReason("tag_rule", "не проходят include/exclude теги"),
                "cooldown": PasteAvailabilityReason("cooldown", "в кулдауне"),
                "reserved": PasteAvailabilityReason("reserved", "уже запланированы/зарезервированы"),
                "daily_limit": PasteAvailabilityReason("daily_limit", "дневной лимит паст уже выбран"),
                "same_paste": PasteAvailabilityReason("same_paste", "same_paste_cooldown_days канала"),
                "same_tag": PasteAvailabilityReason("same_tag", "same_tag_cooldown_hours канала"),
                "duplicate": PasteAvailabilityReason("duplicate", "дубликат/похожий текст уже был в канале"),
            }
            available_examples: list[str] = []
            available_count = 0
            paste_today_total = (scheduled_pastes_today or 0) + (sent_pastes_today or 0)
            daily_limit_reached = paste_today_total >= channel.max_paste_per_day

            for paste in pastes:
                title = paste.title if len(paste.title) <= 80 else f"{paste.title[:77]}..."
                example = f"#{paste.id} {title}"
                reason: PasteAvailabilityReason | None = None

                if paste.status != PasteStatus.ACTIVE:
                    reason = reasons_by_code["inactive"]
                elif not channel.is_active:
                    reason = reasons_by_code["channel_disabled"]
                elif not channel.allow_pastes:
                    reason = reasons_by_code["pastes_disabled"]
                elif not await self.paste_service._is_paste_allowed_for_channel(session, paste, channel.id):
                    reason = reasons_by_code["channel_rule"]
                elif not await self.tag_service.is_paste_allowed_for_channel_tags(
                    session,
                    paste=paste,
                    channel_id=channel.id,
                ):
                    reason = reasons_by_code["tag_rule"]
                elif await self.paste_service._is_paste_in_cooldown(session, paste, channel.id):
                    reason = reasons_by_code["cooldown"]
                elif await self.paste_service._is_paste_recently_reserved(session, paste, channel.id):
                    reason = reasons_by_code["reserved"]
                elif daily_limit_reached:
                    reason = reasons_by_code["daily_limit"]
                elif await self._is_channel_same_paste_cooldown_active(session, channel, paste, now):
                    reason = reasons_by_code["same_paste"]
                elif await self._is_channel_same_tag_cooldown_active(session, channel, paste, now):
                    reason = reasons_by_code["same_tag"]
                elif await self._is_duplicate_paste_for_channel(session, channel, paste):
                    reason = reasons_by_code["duplicate"]

                if reason is None:
                    available_count += 1
                    if len(available_examples) < 5:
                        available_examples.append(example)
                    continue

                reason.count += 1
                if len(reason.examples) < 3:
                    reason.examples.append(example)

            return ChannelPasteDiagnostics(
                channel_id=channel.id,
                channel_title=channel.title or channel.short_code,
                is_active=channel.is_active,
                allow_pastes=channel.allow_pastes,
                max_paste_per_day=channel.max_paste_per_day,
                same_paste_cooldown_days=channel.same_paste_cooldown_days,
                same_tag_cooldown_hours=channel.same_tag_cooldown_hours,
                total_pastes=len(pastes),
                active_pastes=len(active_pastes),
                available_pastes=available_count,
                approved_ready_paste_items=approved_ready_paste_items or 0,
                scheduled_pastes_today=scheduled_pastes_today or 0,
                sent_pastes_today=sent_pastes_today or 0,
                next_available_examples=available_examples,
                reasons=[reason for reason in reasons_by_code.values() if reason.count > 0],
            )

    async def _is_channel_same_paste_cooldown_active(
        self,
        session,
        channel: Channel,
        paste: PasteLibrary,
        now: datetime,
    ) -> bool:
        if channel.same_paste_cooldown_days <= 0:
            return False
        latest_same_paste = await session.scalar(
            select(PublicationLog)
            .join(ContentItem, ContentItem.id == PublicationLog.content_item_id)
            .where(
                PublicationLog.channel_id == channel.id,
                PublicationLog.publish_status.in_([PublicationStatus.SCHEDULED, PublicationStatus.SENT]),
                ContentItem.origin_paste_id == paste.id,
            )
            .order_by(PublicationLog.scheduled_for.desc())
            .limit(1)
        )
        return bool(
            latest_same_paste
            and latest_same_paste.scheduled_for
            and latest_same_paste.scheduled_for >= now - timedelta(days=channel.same_paste_cooldown_days)
        )

    async def _is_channel_same_tag_cooldown_active(
        self,
        session,
        channel: Channel,
        paste: PasteLibrary,
        now: datetime,
    ) -> bool:
        if not paste.primary_tag or channel.same_tag_cooldown_hours <= 0:
            return False
        latest_same_tag = await session.scalar(
            select(PublicationLog)
            .join(ContentItem, ContentItem.id == PublicationLog.content_item_id)
            .where(
                PublicationLog.channel_id == channel.id,
                PublicationLog.publish_status == PublicationStatus.SENT,
                ContentItem.primary_tag == paste.primary_tag,
            )
            .order_by(PublicationLog.published_at.desc())
            .limit(1)
        )
        return bool(
            latest_same_tag
            and latest_same_tag.published_at
            and latest_same_tag.published_at >= now - timedelta(hours=channel.same_tag_cooldown_hours)
        )

    async def _is_duplicate_paste_for_channel(
        self,
        session,
        channel: Channel,
        paste: PasteLibrary,
    ) -> bool:
        draft_candidate = ContentItem(
            channel_id=channel.id,
            source_type=ContentSourceType.PASTE,
            origin_paste_id=paste.id,
            body_text=paste.body_text,
            normalized_text=paste.normalized_text,
            text_hash=paste.text_hash,
            primary_tag=paste.primary_tag,
            tags=paste.tags,
            tone_key="community",
            review_required=False,
            status=ContentItemStatus.APPROVED,
        )
        return await self.scheduler._is_duplicate_for_channel(session, channel.id, draft_candidate)

    async def delete_paste(self, paste_id: int) -> tuple[int, str]:
        async with session_factory() as session:
            paste = await session.get(PasteLibrary, paste_id)
            if paste is None:
                raise ValueError(f"Paste {paste_id} not found")
            paste_title = paste.title
            paste_pk = paste.id
            await session.delete(paste)
            await session.commit()
            return paste_pk, paste_title

    async def delete_confession_paste(self, paste_id: int) -> tuple[int, str]:
        async with session_factory() as session:
            paste = await session.get(PasteLibrary, paste_id)
            if paste is None or paste.content_family != ContentFamily.CONFESSION.value:
                raise ValueError(f"Паста признавашек #{paste_id} не найдена.")

            unpublished_items = list(
                (
                    await session.execute(
                        select(ContentItem).where(
                            ContentItem.origin_paste_id == paste.id,
                            ContentItem.status != ContentItemStatus.PUBLISHED,
                        )
                    )
                ).scalars().all()
            )
            for item in unpublished_items:
                await session.delete(item)

            paste_title = paste.title
            paste_pk = paste.id
            await session.delete(paste)
            await session.commit()
            return paste_pk, paste_title

    async def create_manual_paste(self, body_text: str, reviewer_id: int, title: str | None = None) -> PasteLibrary:
        clean_title = (title or body_text.strip().splitlines()[0][:60] or "Manual paste").strip()
        async with session_factory() as session:
            return await self.paste_service.create_manual_paste(
                session=session,
                title=clean_title,
                body_text=body_text.strip(),
                created_by=reviewer_id,
                status=PasteStatus.ACTIVE,
            )

    async def list_tags(self, include_inactive: bool = True) -> list[TagDefinition]:
        async with session_factory() as session:
            return await self.tag_service.list_tags(session, include_inactive=include_inactive)

    async def create_tag(self, *, slug: str, title: str | None, created_by: int | None) -> TagDefinition:
        async with session_factory() as session:
            return await self.tag_service.create_tag(
                session,
                slug=slug,
                title=title,
                created_by=created_by,
            )

    async def set_tag_active(self, *, slug: str, is_active: bool) -> TagDefinition:
        async with session_factory() as session:
            return await self.tag_service.set_tag_active(session, slug=slug, is_active=is_active)

    async def list_tag_keywords(self, tag_slug: str) -> list[TagKeyword]:
        async with session_factory() as session:
            return await self.tag_service.list_keywords(session, tag_slug)

    async def add_tag_keywords(self, *, tag_slug: str, keywords: Iterable[str]) -> list[TagKeyword]:
        created: list[TagKeyword] = []
        async with session_factory() as session:
            for keyword in keywords:
                if not keyword.strip():
                    continue
                created.append(
                    await self.tag_service.add_keyword(
                        session,
                        tag_slug=tag_slug,
                        keyword=keyword.strip(),
                    )
                )
            return created

    async def remove_tag_keyword(self, keyword_id: int) -> TagKeyword:
        async with session_factory() as session:
            return await self.tag_service.remove_keyword(session, keyword_id)

    async def get_paste_tag_summary(self, paste_id: int) -> PasteTagSummary:
        async with session_factory() as session:
            paste = await session.get(PasteLibrary, paste_id)
            if paste is None:
                raise ValueError(f"Paste {paste_id} not found")
            return await self.tag_service.get_paste_tag_summary(session, paste)

    async def add_paste_manual_tag(self, *, paste_id: int, tag_slug: str, created_by: int | None) -> PasteTagSummary:
        async with session_factory() as session:
            paste = await session.get(PasteLibrary, paste_id)
            if paste is None:
                raise ValueError(f"Paste {paste_id} not found")
            summary = await self.tag_service.add_paste_manual_tag(
                session,
                paste=paste,
                tag_slug=tag_slug,
                created_by=created_by,
            )
            await session.commit()
            return summary

    async def remove_paste_manual_tag(self, *, paste_id: int, tag_slug: str) -> PasteTagSummary:
        async with session_factory() as session:
            paste = await session.get(PasteLibrary, paste_id)
            if paste is None:
                raise ValueError(f"Paste {paste_id} not found")
            summary = await self.tag_service.remove_paste_manual_tag(session, paste=paste, tag_slug=tag_slug)
            await session.commit()
            return summary

    async def refresh_paste_auto_tags(self, paste_id: int) -> PasteTagSummary:
        async with session_factory() as session:
            paste = await session.get(PasteLibrary, paste_id)
            if paste is None:
                raise ValueError(f"Paste {paste_id} not found")
            summary = await self.tag_service.sync_paste_auto_tags(session, paste)
            await session.commit()
            return summary

    async def set_paste_primary_tag(self, *, paste_id: int, tag_slug: str) -> PasteTagSummary:
        async with session_factory() as session:
            paste = await session.get(PasteLibrary, paste_id)
            if paste is None:
                raise ValueError(f"Paste {paste_id} not found")
            summary = await self.tag_service.set_paste_primary_tag(session, paste=paste, tag_slug=tag_slug)
            await session.commit()
            return summary

    async def list_channel_paste_tag_rules(self, channel_id: int) -> list[tuple[ChannelPasteTagRule, TagDefinition]]:
        async with session_factory() as session:
            return await self.tag_service.list_channel_paste_tag_rules(session, channel_id)

    async def add_channel_paste_tag_rule(
        self,
        *,
        channel_id: int,
        tag_slug: str,
        mode: ChannelPasteTagRuleMode,
        created_by: int | None,
    ) -> ChannelPasteTagRule:
        async with session_factory() as session:
            return await self.tag_service.add_channel_paste_tag_rule(
                session,
                channel_id=channel_id,
                tag_slug=tag_slug,
                mode=mode,
                created_by=created_by,
            )

    async def remove_channel_paste_tag_rule(
        self,
        *,
        channel_id: int,
        tag_slug: str,
        mode: ChannelPasteTagRuleMode,
    ) -> int:
        async with session_factory() as session:
            return await self.tag_service.remove_channel_paste_tag_rule(
                session,
                channel_id=channel_id,
                tag_slug=tag_slug,
                mode=mode,
            )

    async def list_global_paste_tag_rules(self) -> list[tuple[GlobalPasteTagRule, TagDefinition]]:
        async with session_factory() as session:
            return await self.tag_service.list_global_paste_tag_rules(session)

    async def add_global_paste_tag_rule(
        self,
        *,
        tag_slug: str,
        mode: ChannelPasteTagRuleMode,
        ends_at: datetime | None,
        created_by: int | None,
    ) -> GlobalPasteTagRule:
        async with session_factory() as session:
            return await self.tag_service.add_global_paste_tag_rule(
                session,
                tag_slug=tag_slug,
                mode=mode,
                ends_at=ends_at,
                created_by=created_by,
            )

    async def remove_global_paste_tag_rule(
        self,
        *,
        tag_slug: str,
        mode: ChannelPasteTagRuleMode,
    ) -> int:
        async with session_factory() as session:
            return await self.tag_service.remove_global_paste_tag_rule(
                session,
                tag_slug=tag_slug,
                mode=mode,
            )

    async def archive_paste(self, paste_id: int) -> PasteLibrary:
        async with session_factory() as session:
            return await self.paste_service.archive_paste(session=session, paste_id=paste_id)

    async def approve_content_item(self, content_item_id: int, reviewer_id: int) -> ContentItem:
        async with session_factory() as session:
            item = await session.get(ContentItem, content_item_id)
            if item is None:
                raise ValueError(f"Content item {content_item_id} not found")
            channel = await session.get(Channel, item.channel_id)
            if channel is None or not channel.is_active:
                raise ValueError("Channel is inactive or unlinked")
            return await self.moderation.review_content_item(
                session=session,
                content_item_id=content_item_id,
                reviewer_id=reviewer_id,
                decision=ReviewDecision.APPROVE,
                review_note="Approved in Telegram panel",
                moderation_source="panel",
                moderation_action="approve_content_item",
            )

    async def sync_panel_submission_approved(self, submission_id: int) -> int:
        return await self.legacy_moderation_sync.mark_panel_submission_approved(submission_id)

    async def sync_panel_submission_rejected(self, submission_id: int) -> int:
        return await self.legacy_moderation_sync.mark_panel_submission_rejected(submission_id)

    async def sync_panel_submission_agent_approved(self, submission_id: int) -> int:
        return await self.legacy_moderation_sync.mark_panel_submission_agent_approved(submission_id)

    async def sync_panel_submission_agent_rejected(self, submission_id: int) -> int:
        return await self.legacy_moderation_sync.mark_panel_submission_agent_rejected(submission_id)

    async def sync_panel_submission_advertising(self, submission_id: int) -> int:
        return await self.legacy_moderation_sync.mark_panel_submission_advertising(submission_id)

    async def sync_panel_submission_agent_advertising(self, submission_id: int) -> int:
        return await self.legacy_moderation_sync.mark_panel_submission_agent_advertising(submission_id)

    async def sync_panel_submission_banned(self, submission_id: int) -> int:
        return await self.legacy_moderation_sync.mark_panel_submission_banned(submission_id)

    async def publish_content_item_now(self, content_item_id: int, reviewer_id: int) -> PublicationLog:
        async with session_factory() as session:
            item = await session.get(ContentItem, content_item_id)
            if item is None:
                raise ValueError(f"Content item {content_item_id} not found")
            channel = await session.get(Channel, item.channel_id)
            if channel is None or not channel.is_active:
                raise ValueError("Channel is inactive or unlinked")
            now = datetime.now(timezone.utc)
            if await self.channel_service.is_channel_in_ad_blackout(session, item.channel_id, now):
                raise ValueError(
                    "Сейчас для этого канала активно рекламное окно. Publish now временно заблокирован, чтобы не перебить рекламу."
                )
            item = await self.moderation.review_content_item(
                session=session,
                content_item_id=item.id,
                reviewer_id=reviewer_id,
                decision=ReviewDecision.APPROVE,
                review_note="Approved and published in Telegram panel",
                moderation_source="panel",
                moderation_action="publish_content_item",
            )
            log_item = await self._schedule_now(session, item)
            await self.publisher.run(session, now=datetime.now(timezone.utc), limit=20)
            return log_item

    async def reject_content_item(self, content_item_id: int, reviewer_id: int) -> ContentItem:
        async with session_factory() as session:
            return await self.moderation.review_content_item(
                session=session,
                content_item_id=content_item_id,
                reviewer_id=reviewer_id,
                decision=ReviewDecision.REJECT,
                review_note="Rejected in Telegram panel",
                moderation_source="panel",
                moderation_action="reject_content_item",
            )

    async def hold_content_item(self, content_item_id: int, reviewer_id: int) -> ContentItem:
        async with session_factory() as session:
            return await self.moderation.review_content_item(
                session=session,
                content_item_id=content_item_id,
                reviewer_id=reviewer_id,
                decision=ReviewDecision.HOLD,
                review_note="Hold in Telegram panel",
                moderation_source="panel",
                moderation_action="hold_content_item",
            )

    async def publish_manual_message_to_channels(
        self,
        *,
        channel_ids: Iterable[int],
        moderator_id: int,
        body_text: str,
    ) -> ManualChannelMessageResult:
        cleaned_body = clean_text(body_text)
        if not cleaned_body:
            raise ValueError("Manual channel message text is empty")

        unique_channel_ids = list(dict.fromkeys(int(channel_id) for channel_id in channel_ids))
        result = ManualChannelMessageResult(requested=len(unique_channel_ids))
        if not unique_channel_ids:
            return result

        normalized_text = normalize_text(cleaned_body) or cleaned_body
        text_hash = compute_text_hash(cleaned_body) or compute_raw_text_hash(cleaned_body) or ""
        now = datetime.now(timezone.utc)
        circuit_retry_after: datetime | None = None

        async with session_factory() as session:
            tags, primary_tag = await self.tag_service.apply_tags_to_content_cache(session, cleaned_body)
            for channel_id in unique_channel_ids:
                channel = await session.get(Channel, channel_id)
                if channel is None:
                    result.failed += 1
                    result.errors.append(f"Channel {channel_id} not found")
                    continue
                if not channel.is_active:
                    result.failed += 1
                    result.errors.append(f"Channel {channel_id} is inactive")
                    continue
                if await self.channel_service.is_channel_in_ad_blackout(session, channel.id, now):
                    result.blocked += 1
                    result.errors.append(f"Channel {channel_id} is in ad blackout")
                    continue

                item = ContentItem(
                    channel_id=channel.id,
                    source_type=ContentSourceType.EDITORIAL,
                    body_text=cleaned_body,
                    normalized_text=normalized_text,
                    text_hash=text_hash,
                    primary_tag=primary_tag,
                    tags=tags,
                    template_key="manual_panel_message",
                    tone_key=f"manual:{moderator_id}",
                    review_required=False,
                    status=ContentItemStatus.SCHEDULED,
                    scheduled_for=now,
                )
                session.add(item)
                await session.flush()

                log_item = PublicationLog(
                    content_item_id=item.id,
                    channel_id=channel.id,
                    scheduled_for=now,
                    publish_status=PublicationStatus.SCHEDULED,
                    created_at=now,
                )
                session.add(log_item)
                await session.flush()

                result.content_item_ids.append(int(item.id))
                result.publication_log_ids.append(int(log_item.id))

                if circuit_retry_after is not None:
                    log_item.retry_after = circuit_retry_after
                    log_item.error_text = "Deferred because Telegram is temporarily unavailable"
                    result.deferred += 1
                    continue

                binding = await self.legacy_reader.get_bot_binding(channel.tg_channel_id)
                if binding is None:
                    item.status = ContentItemStatus.HOLD
                    log_item.publish_status = PublicationStatus.FAILED
                    log_item.error_text = f"Legacy bot binding for channel {channel.tg_channel_id} not found"
                    result.failed += 1
                    result.errors.append(log_item.error_text)
                    continue

                log_item.attempt_count = int(log_item.attempt_count or 0) + 1
                log_item.last_attempt_at = now
                try:
                    add_channel_signature = await self.publisher.should_add_channel_signature(
                        binding.bot_api_token,
                        channel,
                    )
                    channel_publication_signature = (
                        await self.publisher.resolve_channel_publication_signature(
                            binding.bot_api_token,
                            channel,
                        )
                        if add_channel_signature
                        else None
                    )
                    telegram_message_id = await self.publisher.telegram_adapter.send_text(
                        bot_token=binding.bot_api_token,
                        channel_id=channel.tg_channel_id,
                        text=self.publisher.format_publication_text(
                            cleaned_body,
                            channel,
                            channel_signature=(
                                channel_publication_signature.ref
                                if channel_publication_signature
                                else None
                            ),
                            channel_title=(
                                channel_publication_signature.title
                                if channel_publication_signature
                                else None
                            ),
                            add_channel_signature=add_channel_signature,
                        ),
                        parse_mode=self.publisher.channel_signature_parse_mode(add_channel_signature),
                        disable_web_page_preview=add_channel_signature,
                    )
                except Exception as ex:
                    if is_transient_telegram_error(ex):
                        self.publisher.defer_transient_publication(
                            log_item=log_item,
                            content_item=item,
                            exc=ex,
                            attempted_at=now,
                        )
                        circuit_retry_after = log_item.retry_after
                        result.deferred += 1
                        result.errors.append(
                            f"Channel {channel_id}: Telegram временно недоступен, публикация будет повторена позже"
                        )
                        continue
                    item.status = ContentItemStatus.HOLD
                    log_item.publish_status = PublicationStatus.FAILED
                    log_item.error_text = str(ex)
                    result.failed += 1
                    result.errors.append(f"Channel {channel_id}: {ex}")
                    continue

                item.status = ContentItemStatus.PUBLISHED
                log_item.publish_status = PublicationStatus.SENT
                log_item.telegram_message_id = telegram_message_id
                log_item.published_at = now
                log_item.retry_after = None
                log_item.error_text = None
                result.sent += 1

            await session.commit()

        return result

    async def run_scheduler(self):
        async with session_factory() as session:
            return await self.scheduler.run(session)

    async def run_auto_slot_planner(self, channel_id: int | None = None):
        async with session_factory() as session:
            return await self.auto_slot_planner.run(session, channel_id=channel_id)

    async def sync_channel_profiles(self, channel_id: int | None = None):
        async with session_factory() as session:
            return await self.channel_profile_service.sync_profiles_by_subscribers(session, channel_id=channel_id)

    async def record_daily_subscriber_snapshots(self):
        async with session_factory() as session:
            return await self.channel_profile_service.record_daily_subscriber_snapshots(session)

    async def list_channel_setting_profiles(self, include_inactive: bool = False):
        async with session_factory() as session:
            return await self.channel_profile_service.list_profiles(session, include_inactive=include_inactive)

    async def update_channel_setting_profile_field(
        self,
        *,
        slug: str,
        field_name: str,
        raw_value: str,
    ):
        async with session_factory() as session:
            profile = await self.channel_profile_service.upsert_profile(
                session,
                slug=slug,
                raw_settings={field_name: raw_value},
            )
            return profile

    async def upsert_channel_setting_profile(
        self,
        *,
        slug: str,
        title: str,
        min_subscribers: int,
        max_subscribers: int | None,
    ):
        async with session_factory() as session:
            return await self.channel_profile_service.upsert_profile(
                session,
                slug=slug,
                title=title,
                min_subscribers=min_subscribers,
                max_subscribers=max_subscribers,
                clear_max_subscribers=max_subscribers is None,
            )

    async def update_channel_setting_profile_meta(
        self,
        *,
        slug: str,
        field_name: str,
        raw_value: str,
    ):
        async with session_factory() as session:
            kwargs: dict[str, object] = {"slug": slug}
            if field_name == "title":
                kwargs["title"] = raw_value
            elif field_name == "min_subscribers":
                kwargs["min_subscribers"] = int(raw_value)
            elif field_name == "max_subscribers":
                clean_value = raw_value.strip().lower()
                if clean_value in {"none", "null", "-", "no"}:
                    kwargs["clear_max_subscribers"] = True
                else:
                    kwargs["max_subscribers"] = int(raw_value)
            elif field_name == "priority":
                kwargs["priority"] = int(raw_value)
            elif field_name == "is_active":
                kwargs["is_active"] = self.channel_service._parse_setting_value(
                    field_name=field_name,
                    raw_value=raw_value,
                    expected_type="bool",
                )
            else:
                raise ValueError(f"Unknown profile meta field '{field_name}'")
            return await self.channel_profile_service.upsert_profile(session, **kwargs)

    async def apply_profile_to_channels(
        self,
        *,
        channel_ids: list[int],
        profile_slug: str,
        auto_enabled: bool = False,
    ):
        async with session_factory() as session:
            return await self.channel_profile_service.apply_profile_to_channels(
                session,
                channel_ids=channel_ids,
                profile_slug=profile_slug,
                auto_enabled=auto_enabled,
            )

    async def export_channel_statistics(
        self,
        *,
        channel_titles: dict[int, str | None] | None = None,
        channel_tags: dict[int, str | None] | None = None,
        delta_days: int = 7,
    ):
        async with session_factory() as session:
            return await self.statistics_export_service.export_channel_statistics(
                session,
                channel_titles=channel_titles,
                channel_tags=channel_tags,
                delta_days=delta_days,
            )

    async def export_admin_statistics(
        self,
        *,
        month: date,
        admin_labels: dict[int, str] | None = None,
    ):
        async with session_factory() as session:
            return await self.admin_statistics_export_service.export_admin_statistics(
                session,
                month=month,
                admin_labels=admin_labels,
            )

    async def run_publisher(self):
        async with session_factory() as session:
            return await self.publisher.run(session)

    async def run_generation(
        self,
        *,
        channel_id: int,
        variant_count: int = 3,
        source_count: int = 5,
    ):
        async with session_factory() as session:
            channel = await session.get(Channel, channel_id)
            if channel is None:
                raise ValueError(f"Channel {channel_id} not found")
            return await GenerationService().generate_for_channel(
                session=session,
                channel_id=channel_id,
                variant_count=variant_count,
                source_count=source_count,
            )

    async def _get_or_create_content_item(self, session, submission_id: int) -> ContentItem:
        submission = await session.get(Submission, submission_id)
        if submission is None:
            raise ValueError(f"Submission {submission_id} not found")

        related_submissions = await self.moderation.get_related_submissions(session, submission)
        submission_ids = [item.id for item in related_submissions]
        existing = await session.scalar(
            select(ContentItem)
            .where(ContentItem.origin_submission_id.in_(submission_ids))
            .order_by(ContentItem.created_at.desc())
            .limit(1)
        )
        if existing is not None:
            return existing

        return await self.moderation.create_content_from_submission(
            session=session,
            submission_id=submission_id,
            channel_id=submission.channel_id,
            status=ContentItemStatus.PENDING_REVIEW,
        )

    async def _schedule_now(self, session, item: ContentItem) -> PublicationLog:
        now = datetime.now(timezone.utc)
        if await self.channel_service.is_channel_in_ad_blackout(session, item.channel_id, now):
            raise ValueError(
                "Сейчас для этого канала активно рекламное окно. Publish now временно заблокирован, чтобы не перебить рекламу."
            )
        existing = await session.scalar(
            select(PublicationLog)
            .where(
                PublicationLog.content_item_id == item.id,
                PublicationLog.publish_status.in_({
                    PublicationStatus.SCHEDULED,
                    PublicationStatus.SENT,
                }),
            )
            .order_by(PublicationLog.created_at.desc())
            .limit(1)
        )
        if existing is not None:
            return existing

        item.status = ContentItemStatus.SCHEDULED
        item.scheduled_for = now
        log_item = PublicationLog(
            content_item_id=item.id,
            channel_id=item.channel_id,
            scheduled_for=now,
            publish_status=PublicationStatus.SCHEDULED,
            created_at=now,
        )
        session.add(log_item)
        await session.commit()
        await session.refresh(log_item)
        return log_item

    async def _get_legacy_bot_id(self, bot_api_token: str) -> int:
        cached_id = self._legacy_bot_id_cache.get(bot_api_token)
        if cached_id is not None:
            return cached_id

        bot = AsyncTeleBot(bot_api_token)
        try:
            bot_info = await bot.get_me()
            self._legacy_bot_id_cache[bot_api_token] = bot_info.id
            return bot_info.id
        finally:
            close_session = getattr(bot, "close_session", None)
            if close_session is not None:
                await close_session()
