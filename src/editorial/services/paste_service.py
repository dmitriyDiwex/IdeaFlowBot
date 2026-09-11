from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from random import SystemRandom

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.editorial.models.channel import Channel
from src.editorial.models.channel_history import ChannelHistoryMessage
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import (
    ChannelPasteTagRuleMode,
    ContentItemStatus,
    ContentSourceType,
    ContentFamily,
    PasteStatus,
    PublicationStatus,
    SubmissionStatus,
)
from src.editorial.models.paste import PasteChannelRule, PasteLibrary, PasteUsage
from src.editorial.models.publication import PublicationLog
from src.editorial.models.submission import Submission
from src.editorial.models.tag import ChannelPasteTagRule, GlobalPasteTagRule, TagDefinition
from src.editorial.services.tag_service import TagService
from src.editorial.utils.text import compute_text_hash, normalize_text


def _remember_latest(
    target: dict[int | tuple[int, int], datetime],
    key: int | tuple[int, int],
    value: datetime | None,
) -> None:
    if value is None:
        return
    current = target.get(key)
    if current is None or value > current:
        target[key] = value


def _combine_last_use(
    usage_at: datetime | None,
    published_at: datetime | None,
    history_at: datetime | None,
) -> datetime | None:
    # Keep get_last_used_at() semantics exactly: channel history only
    # participates after at least one PasteUsage row exists.
    if usage_at is None:
        return published_at
    combined = usage_at if published_at is None else max(usage_at, published_at)
    return combined if history_at is None else max(combined, history_at)


@dataclass(slots=True)
class PasteAvailabilityContext:
    """Point-in-time paste eligibility data shared by one scheduler run."""

    reference_now: datetime
    channel_ids: frozenset[int]
    pastes: list[PasteLibrary]
    channel_families: dict[int, str] = field(default_factory=dict)
    explicitly_allowed_pairs: set[tuple[int, int]] = field(default_factory=set)
    global_included: set[str] = field(default_factory=set)
    global_excluded: set[str] = field(default_factory=set)
    channel_included: dict[int, set[str]] = field(default_factory=dict)
    channel_excluded: dict[int, set[str]] = field(default_factory=dict)
    last_used_global: dict[int, datetime] = field(default_factory=dict)
    last_used_by_channel: dict[tuple[int, int], datetime] = field(default_factory=dict)
    last_reserved_global: dict[int, datetime] = field(default_factory=dict)
    last_reserved_by_channel: dict[tuple[int, int], datetime] = field(default_factory=dict)

    def covers_channel(self, channel_id: int) -> bool:
        return channel_id in self.channel_ids

    def available_for_channel(self, channel_id: int) -> list[PasteLibrary]:
        if not self.covers_channel(channel_id):
            raise ValueError(f"Paste availability context does not cover channel {channel_id}")

        available: list[PasteLibrary] = []
        channel_included = self.channel_included.get(channel_id, set())
        channel_excluded = self.channel_excluded.get(channel_id, set())
        channel_family = self.channel_families.get(channel_id, ContentFamily.OVERHEARD.value)

        for paste in self.pastes:
            paste_family = getattr(paste, "content_family", None) or ContentFamily.OVERHEARD.value
            if paste_family != channel_family:
                continue
            if not paste.allow_all_channels and (paste.id, channel_id) not in self.explicitly_allowed_pairs:
                continue

            if channel_family == ContentFamily.CONFESSION.value:
                paste_channel_key = (paste.id, channel_id)
                # A confession paste is a finite Telegram source message, not
                # reusable filler. Once it has been used or reserved for a
                # channel, it must never be selected for that channel again.
                if (
                    paste_channel_key in self.last_used_by_channel
                    or paste_channel_key in self.last_reserved_by_channel
                ):
                    continue

            if channel_family != ContentFamily.CONFESSION.value:
                paste_tags = set(paste.tags or [])
                if paste.primary_tag:
                    paste_tags.add(paste.primary_tag)
                if not TagService._is_allowed_by_tag_sets(
                    paste_tags,
                    global_included=self.global_included,
                    channel_included=channel_included,
                    excluded=self.global_excluded | channel_excluded,
                ):
                    continue

            global_cooldown_days = max(
                0,
                min(paste.global_cooldown_days, PasteService.GLOBAL_CROSS_CHANNEL_COOLDOWN_DAYS),
            )
            if self._is_within_cooldown(
                self.last_used_global.get(paste.id),
                global_cooldown_days,
                require_positive=True,
            ):
                continue
            if self._is_within_cooldown(
                self.last_used_by_channel.get((paste.id, channel_id)),
                paste.per_channel_cooldown_days,
            ):
                continue
            if self._is_within_cooldown(
                self.last_reserved_global.get(paste.id),
                global_cooldown_days,
                require_positive=True,
            ):
                continue
            if self._is_within_cooldown(
                self.last_reserved_by_channel.get((paste.id, channel_id)),
                paste.per_channel_cooldown_days,
            ):
                continue
            available.append(paste)

        return available

    def record_reservation(self, paste_id: int, channel_id: int, scheduled_for: datetime) -> None:
        _remember_latest(self.last_reserved_global, paste_id, scheduled_for)
        _remember_latest(self.last_reserved_by_channel, (paste_id, channel_id), scheduled_for)

    def _is_within_cooldown(
        self,
        value: datetime | None,
        cooldown_days: int,
        *,
        require_positive: bool = False,
    ) -> bool:
        return bool(
            value is not None
            and (not require_positive or cooldown_days > 0)
            and value >= self.reference_now - timedelta(days=cooldown_days)
        )


class PasteService:
    GLOBAL_CROSS_CHANNEL_COOLDOWN_DAYS = 3

    def __init__(self, tag_service: TagService | None = None) -> None:
        self._random = SystemRandom()
        self.tag_service = tag_service or TagService()

    async def list_pastes(
        self,
        session: AsyncSession,
        status: PasteStatus | None = None,
        limit: int | None = 50,
        content_family: str | None = None,
    ) -> list[PasteLibrary]:
        stmt = select(PasteLibrary).order_by(PasteLibrary.updated_at.desc())
        if status is not None:
            stmt = stmt.where(PasteLibrary.status == status)
        if content_family is not None:
            stmt = stmt.where(PasteLibrary.content_family == content_family)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def create_manual_paste(
        self,
        session: AsyncSession,
        title: str,
        body_text: str,
        created_by: int | None = None,
        status: PasteStatus = PasteStatus.ACTIVE,
    ) -> PasteLibrary:
        tags, primary_tag = await self.tag_service.apply_tags_to_content_cache(session, body_text)
        paste = PasteLibrary(
            title=title,
            body_text=body_text,
            normalized_text=normalize_text(body_text),
            text_hash=compute_text_hash(body_text) or "",
            tags=tags,
            primary_tag=primary_tag,
            status=status,
            created_by=created_by,
        )
        session.add(paste)
        await session.flush()
        await self.tag_service.sync_paste_auto_tags(session, paste)
        await session.commit()
        await session.refresh(paste)
        return paste

    async def create_paste_from_submission(
        self,
        session: AsyncSession,
        submission_id: int,
        created_by: int | None = None,
    ) -> PasteLibrary:
        submission = await session.get(Submission, submission_id)
        if submission is None:
            raise ValueError(f"Submission {submission_id} not found")
        body_text = (submission.cleaned_text or submission.raw_text or "").strip()
        if not body_text:
            raise ValueError("Submission has no text content for paste creation")

        paste = await self.create_manual_paste(
            session=session,
            title=f"Paste from submission {submission.id}",
            body_text=body_text,
            created_by=created_by,
        )
        paste.source_submission_id = submission.id
        paste.source_channel_id = submission.channel_id
        submission.status = SubmissionStatus.PASTE_CANDIDATE
        submission.is_candidate_for_paste = True
        submission.reviewed_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(paste)
        return paste

    async def create_paste_from_content_item(
        self,
        session: AsyncSession,
        content_item_id: int,
        created_by: int | None = None,
    ) -> PasteLibrary:
        item = await session.get(ContentItem, content_item_id)
        if item is None:
            raise ValueError(f"Content item {content_item_id} not found")

        paste = await self.create_manual_paste(
            session=session,
            title=f"Paste from content item {item.id}",
            body_text=item.body_text,
            created_by=created_by,
        )
        paste.source_content_item_id = item.id
        paste.source_channel_id = item.channel_id
        await session.commit()
        await session.refresh(paste)
        return paste

    async def create_content_item_from_paste(
        self,
        session: AsyncSession,
        paste_id: int,
        channel_id: int,
        status: ContentItemStatus = ContentItemStatus.PENDING_REVIEW,
        review_required: bool = True,
        *,
        commit: bool = True,
    ) -> ContentItem:
        paste = await session.get(PasteLibrary, paste_id)
        if paste is None:
            raise ValueError(f"Paste {paste_id} not found")

        if paste.content_family == ContentFamily.CONFESSION.value:
            tags = []
            primary_tag = None
        else:
            await self.tag_service.refresh_paste_tag_cache(session, paste)
            tags = list(paste.tags or [])
            primary_tag = paste.primary_tag or await self.tag_service.pick_primary_tag(session, tags)
        item = ContentItem(
            channel_id=channel_id,
            source_type=ContentSourceType.PASTE,
            origin_paste_id=paste.id,
            body_text=paste.body_text,
            normalized_text=normalize_text(paste.body_text),
            text_hash=compute_text_hash(paste.body_text) or "",
            primary_tag=primary_tag,
            tags=tags,
            tone_key="community",
            review_required=review_required,
            status=status,
        )
        session.add(item)
        if commit:
            await session.commit()
            await session.refresh(item)
        else:
            await session.flush()
        return item

    async def archive_paste(
        self,
        session: AsyncSession,
        paste_id: int,
    ) -> PasteLibrary:
        paste = await session.get(PasteLibrary, paste_id)
        if paste is None:
            raise ValueError(f"Paste {paste_id} not found")
        paste.status = PasteStatus.ARCHIVED
        await session.commit()
        await session.refresh(paste)
        return paste

    async def list_available_for_channel(
        self,
        session: AsyncSession,
        channel_id: int,
        limit: int = 20,
        *,
        availability_context: PasteAvailabilityContext | None = None,
    ) -> list[PasteLibrary]:
        if availability_context is None or not availability_context.covers_channel(channel_id):
            channel = await session.get(Channel, channel_id)
            channel_family = (
                getattr(channel, "content_family", None) or ContentFamily.OVERHEARD.value
            )
            availability_context = await self.build_availability_context(
                session,
                channel_ids=[channel_id],
                channel_families={channel_id: channel_family},
            )
        available = availability_context.available_for_channel(channel_id)

        # Choose randomly among all pastes that already passed cooldown and
        # channel restrictions so scheduler does not always reuse the same item.
        self._random.shuffle(available)
        return available[:limit]

    async def build_availability_context(
        self,
        session: AsyncSession,
        *,
        channel_ids: list[int],
        now: datetime | None = None,
        channel_families: dict[int, str] | None = None,
    ) -> PasteAvailabilityContext:
        reference_now = now or datetime.now(timezone.utc)
        unique_channel_ids = frozenset(int(channel_id) for channel_id in channel_ids)
        pastes = list(
            (
                await session.execute(
                    select(PasteLibrary)
                    .where(PasteLibrary.status == PasteStatus.ACTIVE)
                    .order_by(PasteLibrary.updated_at.desc())
                )
            ).scalars().all()
        )
        context = PasteAvailabilityContext(
            reference_now=reference_now,
            channel_ids=unique_channel_ids,
            pastes=pastes,
            channel_families=channel_families or {},
        )
        if not pastes or not unique_channel_ids:
            return context

        paste_ids = [paste.id for paste in pastes]
        explicit_rows = (
            await session.execute(
                select(PasteChannelRule.paste_id, PasteChannelRule.channel_id).where(
                    PasteChannelRule.paste_id.in_(paste_ids),
                    PasteChannelRule.channel_id.in_(unique_channel_ids),
                    PasteChannelRule.is_allowed.is_(True),
                )
            )
        ).all()
        context.explicitly_allowed_pairs = {
            (int(paste_id), int(channel_id)) for paste_id, channel_id in explicit_rows
        }

        global_rule_rows = (
            await session.execute(
                select(GlobalPasteTagRule, TagDefinition)
                .join(TagDefinition, TagDefinition.id == GlobalPasteTagRule.tag_id)
                .where(
                    GlobalPasteTagRule.is_active.is_(True),
                    TagDefinition.is_active.is_(True),
                    (GlobalPasteTagRule.starts_at.is_(None) | (GlobalPasteTagRule.starts_at <= reference_now)),
                    (GlobalPasteTagRule.ends_at.is_(None) | (GlobalPasteTagRule.ends_at > reference_now)),
                )
            )
        ).all()
        context.global_included = {
            tag.slug for rule, tag in global_rule_rows if rule.mode == ChannelPasteTagRuleMode.INCLUDE
        }
        context.global_excluded = {
            tag.slug for rule, tag in global_rule_rows if rule.mode == ChannelPasteTagRuleMode.EXCLUDE
        }

        channel_rule_rows = (
            await session.execute(
                select(ChannelPasteTagRule, TagDefinition)
                .join(TagDefinition, TagDefinition.id == ChannelPasteTagRule.tag_id)
                .where(
                    ChannelPasteTagRule.channel_id.in_(unique_channel_ids),
                    ChannelPasteTagRule.is_active.is_(True),
                    TagDefinition.is_active.is_(True),
                    (ChannelPasteTagRule.starts_at.is_(None) | (ChannelPasteTagRule.starts_at <= reference_now)),
                    (ChannelPasteTagRule.ends_at.is_(None) | (ChannelPasteTagRule.ends_at > reference_now)),
                )
            )
        ).all()
        for rule, tag in channel_rule_rows:
            target = context.channel_included if rule.mode == ChannelPasteTagRuleMode.INCLUDE else context.channel_excluded
            target.setdefault(int(rule.channel_id), set()).add(tag.slug)

        usage_global: dict[int, datetime] = {}
        usage_by_channel: dict[tuple[int, int], datetime] = {}
        usage_rows = (
            await session.execute(
                select(
                    PasteUsage.paste_id,
                    PasteUsage.channel_id,
                    func.max(PasteUsage.used_at),
                )
                .where(PasteUsage.paste_id.in_(paste_ids))
                .group_by(PasteUsage.paste_id, PasteUsage.channel_id)
            )
        ).all()
        for paste_id, channel_id, used_at in usage_rows:
            paste_id = int(paste_id)
            channel_id = int(channel_id)
            _remember_latest(usage_global, paste_id, used_at)
            _remember_latest(usage_by_channel, (paste_id, channel_id), used_at)

        published_global: dict[int, datetime] = {}
        published_by_channel: dict[tuple[int, int], datetime] = {}
        published_rows = (
            await session.execute(
                select(
                    ContentItem.origin_paste_id,
                    PublicationLog.channel_id,
                    func.max(PublicationLog.published_at),
                )
                .join(ContentItem, ContentItem.id == PublicationLog.content_item_id)
                .where(
                    ContentItem.origin_paste_id.in_(paste_ids),
                    PublicationLog.published_at.is_not(None),
                )
                .group_by(ContentItem.origin_paste_id, PublicationLog.channel_id)
            )
        ).all()
        for paste_id, channel_id, published_at in published_rows:
            paste_id = int(paste_id)
            channel_id = int(channel_id)
            _remember_latest(published_global, paste_id, published_at)
            _remember_latest(published_by_channel, (paste_id, channel_id), published_at)

        history_global: dict[int, datetime] = {}
        history_by_channel: dict[tuple[int, int], datetime] = {}
        history_timestamp = func.coalesce(
            ChannelHistoryMessage.original_published_at,
            ChannelHistoryMessage.created_at,
        )
        history_rows = (
            await session.execute(
                select(
                    ChannelHistoryMessage.matched_paste_id,
                    ChannelHistoryMessage.channel_id,
                    func.max(history_timestamp),
                )
                .where(ChannelHistoryMessage.matched_paste_id.in_(paste_ids))
                .group_by(ChannelHistoryMessage.matched_paste_id, ChannelHistoryMessage.channel_id)
            )
        ).all()
        for paste_id, channel_id, history_at in history_rows:
            paste_id = int(paste_id)
            channel_id = int(channel_id)
            _remember_latest(history_global, paste_id, history_at)
            _remember_latest(history_by_channel, (paste_id, channel_id), history_at)

        for paste_id in set(usage_global) | set(published_global) | set(history_global):
            combined = _combine_last_use(
                usage_global.get(paste_id),
                published_global.get(paste_id),
                history_global.get(paste_id),
            )
            if combined is not None:
                context.last_used_global[paste_id] = combined
        for key in set(usage_by_channel) | set(published_by_channel) | set(history_by_channel):
            combined = _combine_last_use(
                usage_by_channel.get(key),
                published_by_channel.get(key),
                history_by_channel.get(key),
            )
            if combined is not None:
                context.last_used_by_channel[key] = combined

        reservation_rows = (
            await session.execute(
                select(
                    ContentItem.origin_paste_id,
                    PublicationLog.channel_id,
                    func.max(PublicationLog.scheduled_for),
                )
                .join(ContentItem, ContentItem.id == PublicationLog.content_item_id)
                .where(
                    ContentItem.origin_paste_id.in_(paste_ids),
                    PublicationLog.publish_status.in_([PublicationStatus.SCHEDULED, PublicationStatus.SENT]),
                )
                .group_by(ContentItem.origin_paste_id, PublicationLog.channel_id)
            )
        ).all()
        for paste_id, channel_id, scheduled_for in reservation_rows:
            _remember_latest(context.last_reserved_global, int(paste_id), scheduled_for)
            _remember_latest(
                context.last_reserved_by_channel,
                (int(paste_id), int(channel_id)),
                scheduled_for,
            )

        return context

    async def register_usage(
        self,
        session: AsyncSession,
        paste_id: int,
        channel_id: int,
        content_item_id: int,
    ) -> PasteUsage:
        usage = PasteUsage(
            paste_id=paste_id,
            channel_id=channel_id,
            content_item_id=content_item_id,
            used_at=datetime.now(timezone.utc),
        )
        session.add(usage)
        await session.commit()
        await session.refresh(usage)
        return usage

    async def get_last_used_at(
        self,
        session: AsyncSession,
        paste_id: int,
        channel_id: int | None = None,
    ) -> datetime | None:
        usage_stmt = select(PasteUsage.used_at).where(PasteUsage.paste_id == paste_id)
        if channel_id is not None:
            usage_stmt = usage_stmt.where(PasteUsage.channel_id == channel_id)
        last_usage = await session.scalar(usage_stmt.order_by(desc(PasteUsage.used_at)).limit(1))

        publish_stmt = (
            select(PublicationLog.published_at)
            .join(ContentItem, ContentItem.id == PublicationLog.content_item_id)
            .where(
                ContentItem.origin_paste_id == paste_id,
                PublicationLog.published_at.is_not(None),
            )
        )
        if channel_id is not None:
            publish_stmt = publish_stmt.where(PublicationLog.channel_id == channel_id)
        last_publish = await session.scalar(publish_stmt.order_by(desc(PublicationLog.published_at)).limit(1))

        if last_usage is None:
            return last_publish
        if last_publish is None:
            last_combined = last_usage
        elif last_usage is None:
            last_combined = last_publish
        else:
            last_combined = max(last_usage, last_publish)

        history_timestamp_expr = func.coalesce(
            ChannelHistoryMessage.original_published_at,
            ChannelHistoryMessage.created_at,
        )
        history_stmt = select(history_timestamp_expr).where(ChannelHistoryMessage.matched_paste_id == paste_id)
        if channel_id is not None:
            history_stmt = history_stmt.where(ChannelHistoryMessage.channel_id == channel_id)
        last_history_use = await session.scalar(history_stmt.order_by(desc(history_timestamp_expr)).limit(1))

        if last_combined is None:
            return last_history_use
        if last_history_use is None:
            return last_combined
        return max(last_combined, last_history_use)

    async def _is_paste_allowed_for_channel(
        self,
        session: AsyncSession,
        paste: PasteLibrary,
        channel_id: int,
    ) -> bool:
        if paste.allow_all_channels:
            return True
        rule = await session.scalar(
            select(PasteChannelRule)
            .where(PasteChannelRule.paste_id == paste.id, PasteChannelRule.channel_id == channel_id)
        )
        return bool(rule and rule.is_allowed)

    async def _is_paste_in_cooldown(
        self,
        session: AsyncSession,
        paste: PasteLibrary,
        channel_id: int,
    ) -> bool:
        now = datetime.now(timezone.utc)
        global_cooldown_days = self._global_cooldown_days(paste)

        if global_cooldown_days > 0:
            last_global_use = await self.get_last_used_at(session, paste.id)
            if last_global_use and last_global_use >= now - timedelta(days=global_cooldown_days):
                return True

        last_channel_use = await self.get_last_used_at(session, paste.id, channel_id)
        if last_channel_use and last_channel_use >= now - timedelta(days=paste.per_channel_cooldown_days):
            return True
        return False

    async def _is_paste_recently_reserved(
        self,
        session: AsyncSession,
        paste: PasteLibrary,
        channel_id: int,
    ) -> bool:
        now = datetime.now(timezone.utc)
        global_cooldown_days = self._global_cooldown_days(paste)

        if global_cooldown_days > 0:
            latest_global_log = await session.scalar(
                select(PublicationLog)
                .join(ContentItem, ContentItem.id == PublicationLog.content_item_id)
                .where(
                    ContentItem.origin_paste_id == paste.id,
                    PublicationLog.publish_status.in_([PublicationStatus.SCHEDULED, PublicationStatus.SENT]),
                )
                .order_by(desc(PublicationLog.scheduled_for))
                .limit(1)
            )
            if (
                latest_global_log
                and latest_global_log.scheduled_for
                and latest_global_log.scheduled_for >= now - timedelta(days=global_cooldown_days)
            ):
                return True

        latest_channel_log = await session.scalar(
            select(PublicationLog)
            .join(ContentItem, ContentItem.id == PublicationLog.content_item_id)
            .where(
                ContentItem.origin_paste_id == paste.id,
                PublicationLog.channel_id == channel_id,
                PublicationLog.publish_status.in_([PublicationStatus.SCHEDULED, PublicationStatus.SENT]),
            )
            .order_by(desc(PublicationLog.scheduled_for))
            .limit(1)
        )
        if (
            latest_channel_log
            and latest_channel_log.scheduled_for
            and latest_channel_log.scheduled_for >= now - timedelta(days=paste.per_channel_cooldown_days)
        ):
            return True

        return False

    def _global_cooldown_days(self, paste: PasteLibrary) -> int:
        return max(0, min(paste.global_cooldown_days, self.GLOBAL_CROSS_CHANNEL_COOLDOWN_DAYS))
