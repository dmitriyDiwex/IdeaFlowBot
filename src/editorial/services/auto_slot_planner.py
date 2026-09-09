from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from math import floor
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.editorial.models.channel import Channel, ChannelSlot
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import ContentItemStatus, ContentSourceType, PublicationStatus
from src.editorial.models.publication import PublicationLog
from src.editorial.config import settings


@dataclass(slots=True)
class AutoSlotChannelPlan:
    channel_id: int
    target_date: date
    approved_ready_count: int
    target_slots: int
    paste_slots: int
    slot_times: list[time]
    deleted_slots: int = 0
    created_slots: int = 0


@dataclass(slots=True)
class AutoSlotPlannerResult:
    channels_checked: int = 0
    channels_planned: int = 0
    slots_deleted: int = 0
    slots_created: int = 0
    plans: list[AutoSlotChannelPlan] = field(default_factory=list)


class AutoSlotPlannerService:
    async def run(
        self,
        session: AsyncSession,
        now: datetime | None = None,
        channel_id: int | None = None,
    ) -> AutoSlotPlannerResult:
        now = now or datetime.now(timezone.utc)
        result = AutoSlotPlannerResult()

        stmt = select(Channel).where(Channel.is_active.is_(True), Channel.auto_slots_enabled.is_(True))
        if channel_id is not None:
            stmt = stmt.where(Channel.id == channel_id)
        channels = list((await session.execute(stmt.order_by(Channel.id.asc()))).scalars().all())
        pending_plans = 0

        for channel in channels:
            result.channels_checked += 1
            target_date = self._target_date_for_channel(channel, now)
            if target_date is None:
                continue
            if channel.auto_slots_last_planned_for == target_date:
                continue

            plan = await self._build_channel_plan(session, channel, target_date)
            await self._apply_channel_plan(session, channel, plan)
            channel.auto_slots_last_planned_for = target_date

            result.channels_planned += 1
            result.slots_deleted += plan.deleted_slots
            result.slots_created += plan.created_slots
            result.plans.append(plan)
            pending_plans += 1
            if pending_plans >= settings.scheduler_commit_batch_size:
                await session.commit()
                pending_plans = 0

        await session.commit()
        return result

    def _target_date_for_channel(self, channel: Channel, now: datetime) -> date | None:
        local_now = now.astimezone(ZoneInfo(channel.timezone))
        if local_now.time() < channel.auto_slots_plan_time:
            return None

        target_date = local_now.date()
        if local_now.time() >= channel.auto_slots_window_end:
            target_date += timedelta(days=1)

        if channel.auto_slots_last_planned_for != target_date:
            return target_date

        return None

    async def _build_channel_plan(
        self,
        session: AsyncSession,
        channel: Channel,
        target_date: date,
    ) -> AutoSlotChannelPlan:
        day_start_utc, day_end_utc = self._channel_day_bounds(channel.timezone, target_date)
        approved_ready_count = await self._count_approved_ready_items(
            session=session,
            channel_id=channel.id,
            day_start_utc=day_start_utc,
            day_end_utc=day_end_utc,
        )

        target_slots, paste_slots = self._calculate_target_slots(channel, approved_ready_count)
        slot_times = self._spread_slot_times(
            start=channel.auto_slots_window_start,
            end=channel.auto_slots_window_end,
            count=target_slots,
            min_gap_minutes=max(channel.min_gap_minutes, 0),
        )
        if len(slot_times) < target_slots:
            target_slots = len(slot_times)
            paste_slots = min(max(0, target_slots - approved_ready_count), channel.max_paste_per_day)

        return AutoSlotChannelPlan(
            channel_id=channel.id,
            target_date=target_date,
            approved_ready_count=approved_ready_count,
            target_slots=target_slots,
            paste_slots=paste_slots,
            slot_times=slot_times,
        )

    async def _apply_channel_plan(
        self,
        session: AsyncSession,
        channel: Channel,
        plan: AutoSlotChannelPlan,
    ) -> None:
        weekday = plan.target_date.weekday()
        used_slot_ids = set(
            (
                await session.execute(
                    select(PublicationLog.slot_id).where(
                        PublicationLog.channel_id == channel.id,
                        PublicationLog.slot_date == plan.target_date,
                        PublicationLog.slot_id.is_not(None),
                        PublicationLog.publish_status.in_(
                            [PublicationStatus.SCHEDULED, PublicationStatus.SENT]
                        ),
                    )
                )
            ).scalars().all()
        )

        slots = list(
            (
                await session.execute(
                    select(ChannelSlot).where(
                        ChannelSlot.channel_id == channel.id,
                        ChannelSlot.weekday == weekday,
                    )
                )
            ).scalars().all()
        )
        existing_by_time = {slot.slot_time: slot for slot in slots}
        target_times = set(plan.slot_times)

        for slot in slots:
            should_manage = channel.auto_slots_replace_manual or slot.is_auto_managed
            if not should_manage or slot.id in used_slot_ids:
                continue
            if slot.slot_time in target_times:
                slot.is_active = True
                slot.is_auto_managed = True
                continue
            await session.delete(slot)
            plan.deleted_slots += 1

        for slot_time in plan.slot_times:
            existing = existing_by_time.get(slot_time)
            if existing is not None:
                existing.is_active = True
                existing.is_auto_managed = True
                continue
            session.add(
                ChannelSlot(
                    channel_id=channel.id,
                    weekday=weekday,
                    slot_time=slot_time,
                    is_active=True,
                    is_auto_managed=True,
                )
            )
            plan.created_slots += 1

    async def _count_approved_ready_items(
        self,
        session: AsyncSession,
        channel_id: int,
        day_start_utc: datetime,
        day_end_utc: datetime,
    ) -> int:
        count = await session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.channel_id == channel_id,
                ContentItem.status == ContentItemStatus.APPROVED,
                ContentItem.scheduled_for.is_(None),
                ContentItem.source_type.in_([ContentSourceType.SUBMISSION, ContentSourceType.EDITORIAL]),
                (ContentItem.publish_after.is_(None) | (ContentItem.publish_after < day_end_utc)),
                (ContentItem.expires_at.is_(None) | (ContentItem.expires_at > day_start_utc)),
            )
        )
        return int(count or 0)

    @staticmethod
    def _calculate_target_slots(channel: Channel, approved_ready_count: int) -> tuple[int, int]:
        fallback_paste_slots = channel.max_paste_per_day if channel.allow_pastes else 0
        target_slots = max(approved_ready_count, fallback_paste_slots, channel.min_slots_per_day)
        target_slots = min(target_slots, max(channel.max_posts_per_day, 0))
        paste_slots = min(max(0, target_slots - approved_ready_count), fallback_paste_slots)
        return target_slots, paste_slots

    @staticmethod
    def _spread_slot_times(
        *,
        start: time,
        end: time,
        count: int,
        min_gap_minutes: int = 0,
    ) -> list[time]:
        if count <= 0:
            return []

        start_minutes = start.hour * 60 + start.minute
        end_minutes = end.hour * 60 + end.minute
        if end_minutes <= start_minutes:
            raise ValueError("auto_slots_window_end must be later than auto_slots_window_start")

        window_minutes = end_minutes - start_minutes
        count = min(count, window_minutes + 1)
        if min_gap_minutes > 0:
            count = min(count, floor(window_minutes / min_gap_minutes) + 1)
        if count <= 0:
            return []

        if count == 1:
            offsets = [round(window_minutes / 2)]
        else:
            step = window_minutes / (count - 1)
            offsets = [round(index * step) for index in range(count)]

        slot_times: list[time] = []
        for offset in offsets:
            total_minutes = start_minutes + offset
            slot_times.append(time(hour=total_minutes // 60, minute=total_minutes % 60))
        return slot_times

    @staticmethod
    def _channel_day_bounds(timezone_name: str, target_date: date) -> tuple[datetime, datetime]:
        tz = ZoneInfo(timezone_name)
        local_start = datetime.combine(target_date, time.min, tzinfo=tz)
        local_end = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=tz)
        return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)
