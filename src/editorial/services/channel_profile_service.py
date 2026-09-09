from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete as sql_delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from telebot.async_telebot import AsyncTeleBot

from src.editorial.models.channel import Channel, ChannelSettingProfile, ChannelSubscriberSnapshot
from src.editorial.services.channel_service import ChannelService
from src.editorial.services.legacy_source import LegacyBotBinding, LegacyCollectorReader


PROFILE_SETTING_FIELDS = [
    "min_gap_minutes",
    "slot_jitter_minutes",
    "auto_slots_enabled",
    "auto_slots_plan_time",
    "auto_slots_window_start",
    "auto_slots_window_end",
    "auto_slots_replace_manual",
    "min_slots_per_day",
    "max_posts_per_day",
    "max_generated_per_day",
    "max_paste_per_day",
    "same_tag_cooldown_hours",
    "same_template_cooldown_hours",
    "same_paste_cooldown_days",
    "min_ready_queue",
    "prefer_real_ratio",
    "allow_generated",
    "allow_pastes",
]

AUTO_SLOT_REPLAN_FIELDS = {
    "min_gap_minutes",
    "auto_slots_enabled",
    "auto_slots_plan_time",
    "auto_slots_window_start",
    "auto_slots_window_end",
    "auto_slots_replace_manual",
    "min_slots_per_day",
    "max_posts_per_day",
    "max_paste_per_day",
    "allow_pastes",
}

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
SUBSCRIBER_SNAPSHOT_RETENTION_DAYS = 14


@dataclass(slots=True)
class ChannelProfileSyncItem:
    channel_id: int
    tg_channel_id: int
    subscriber_count: int | None
    old_profile_slug: str | None
    new_profile_slug: str | None
    changed: bool
    error: str | None = None


@dataclass(slots=True)
class ChannelProfileSyncResult:
    channels_checked: int = 0
    subscriber_counts_updated: int = 0
    profiles_changed: int = 0
    skipped_manual: int = 0
    failed: int = 0
    items: list[ChannelProfileSyncItem] = field(default_factory=list)


@dataclass(slots=True)
class ChannelSubscriberSnapshotResult:
    channels_checked: int = 0
    subscriber_counts_updated: int = 0
    snapshots_recorded: int = 0
    snapshots_deleted: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class ChannelProfileService:
    def __init__(self, legacy_reader: LegacyCollectorReader | None = None) -> None:
        self.legacy_reader = legacy_reader or LegacyCollectorReader()
        self.channel_service = ChannelService()

    async def list_profiles(self, session: AsyncSession, include_inactive: bool = False) -> list[ChannelSettingProfile]:
        stmt = select(ChannelSettingProfile).order_by(
            ChannelSettingProfile.min_subscribers.asc(),
            ChannelSettingProfile.priority.asc(),
            ChannelSettingProfile.id.asc(),
        )
        if not include_inactive:
            stmt = stmt.where(ChannelSettingProfile.is_active.is_(True))
        return list((await session.execute(stmt)).scalars().all())

    async def upsert_profile(
        self,
        session: AsyncSession,
        *,
        slug: str,
        title: str | None = None,
        min_subscribers: int | None = None,
        max_subscribers: int | None = None,
        clear_max_subscribers: bool = False,
        priority: int | None = None,
        is_active: bool | None = None,
        raw_settings: dict[str, str] | None = None,
    ) -> ChannelSettingProfile:
        clean_slug = slug.strip().lower()
        if not clean_slug:
            raise ValueError("Profile slug is empty")

        profile = await session.scalar(
            select(ChannelSettingProfile).where(ChannelSettingProfile.slug == clean_slug).limit(1)
        )
        if profile is None:
            profile = ChannelSettingProfile(
                slug=clean_slug,
                title=title or clean_slug,
                min_subscribers=min_subscribers or 0,
                max_subscribers=max_subscribers,
            )
            session.add(profile)

        if title is not None:
            profile.title = title
        if min_subscribers is not None:
            profile.min_subscribers = min_subscribers
        if clear_max_subscribers:
            profile.max_subscribers = None
        elif max_subscribers is not None:
            profile.max_subscribers = max_subscribers
        if priority is not None:
            profile.priority = priority
        if is_active is not None:
            profile.is_active = is_active

        for field_name, raw_value in (raw_settings or {}).items():
            self._set_profile_setting(profile, field_name, raw_value)

        await session.commit()
        await session.refresh(profile)
        return profile

    async def apply_profile_to_channel(
        self,
        session: AsyncSession,
        *,
        channel_id: int,
        profile_slug: str,
        auto_enabled: bool = False,
    ) -> Channel:
        channel = await session.get(Channel, channel_id)
        if channel is None:
            raise ValueError(f"Channel {channel_id} not found")
        profile = await self._get_profile_by_slug(session, profile_slug)
        if profile is None:
            raise ValueError(f"Profile '{profile_slug}' not found")

        self._apply_profile(channel, profile)
        channel.settings_profile_auto_enabled = auto_enabled
        await session.commit()
        await session.refresh(channel)
        return channel

    async def apply_profile_to_channels(
        self,
        session: AsyncSession,
        *,
        channel_ids: list[int],
        profile_slug: str,
        auto_enabled: bool = False,
    ) -> list[Channel]:
        profile = await self._get_profile_by_slug(session, profile_slug)
        if profile is None:
            raise ValueError(f"Profile '{profile_slug}' not found")

        unique_channel_ids = list(dict.fromkeys(int(channel_id) for channel_id in channel_ids))
        channels = list(
            (
                await session.execute(
                    select(Channel)
                    .where(Channel.id.in_(unique_channel_ids))
                    .order_by(Channel.id.asc())
                )
            ).scalars().all()
        )
        found_ids = {channel.id for channel in channels}
        missing_ids = [channel_id for channel_id in unique_channel_ids if channel_id not in found_ids]
        if missing_ids:
            raise ValueError(f"Channels not found: {', '.join(map(str, missing_ids))}")

        for channel in channels:
            self._apply_profile(channel, profile)
            channel.settings_profile_auto_enabled = auto_enabled
        await session.commit()
        return channels

    async def sync_profiles_by_subscribers(
        self,
        session: AsyncSession,
        *,
        channel_id: int | None = None,
        update_subscriber_counts: bool = True,
        record_subscriber_snapshots: bool = False,
    ) -> ChannelProfileSyncResult:
        result = ChannelProfileSyncResult()
        profiles = await self.list_profiles(session)
        bindings = await self.legacy_reader.fetch_all_bot_bindings()
        bindings_by_tg_id = {int(binding.channel_id): binding for binding in bindings}

        stmt = select(Channel).where(Channel.is_active.is_(True))
        if channel_id is not None:
            stmt = stmt.where(Channel.id == channel_id)
        channels = list((await session.execute(stmt.order_by(Channel.id.asc()))).scalars().all())

        now = datetime.now(timezone.utc)
        for channel in channels:
            result.channels_checked += 1

            error: str | None = None
            binding = bindings_by_tg_id.get(channel.tg_channel_id)
            if update_subscriber_counts and binding is not None:
                try:
                    channel.subscriber_count = await self._fetch_subscriber_count(binding)
                    channel.subscriber_count_checked_at = now
                    if record_subscriber_snapshots:
                        self._record_subscriber_snapshot(session, channel, now)
                    result.subscriber_counts_updated += 1
                except Exception as exc:
                    error = str(exc)
                    result.failed += 1

            old_profile = await session.get(ChannelSettingProfile, channel.settings_profile_id) if channel.settings_profile_id else None
            if not channel.settings_profile_auto_enabled:
                result.skipped_manual += 1
                result.items.append(
                    ChannelProfileSyncItem(
                        channel_id=channel.id,
                        tg_channel_id=channel.tg_channel_id,
                        subscriber_count=channel.subscriber_count,
                        old_profile_slug=old_profile.slug if old_profile is not None else None,
                        new_profile_slug=old_profile.slug if old_profile is not None else None,
                        changed=False,
                        error=error,
                    )
                )
                continue

            new_profile = self._select_profile(profiles, channel.subscriber_count)
            changed = False
            if new_profile is not None:
                old_profile_id = old_profile.id if old_profile is not None else None
                changed = old_profile_id != new_profile.id or not self._profile_settings_match(channel, new_profile)
                if changed:
                    self._apply_profile(channel, new_profile, applied_at=now)
                    result.profiles_changed += 1

            result.items.append(
                ChannelProfileSyncItem(
                    channel_id=channel.id,
                    tg_channel_id=channel.tg_channel_id,
                    subscriber_count=channel.subscriber_count,
                    old_profile_slug=old_profile.slug if old_profile is not None else None,
                    new_profile_slug=new_profile.slug if new_profile is not None else None,
                    changed=changed,
                    error=error,
                )
            )

        await session.commit()
        return result

    async def record_daily_subscriber_snapshots(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
        retention_days: int = SUBSCRIBER_SNAPSHOT_RETENTION_DAYS,
    ) -> ChannelSubscriberSnapshotResult:
        checked_at = self._ensure_aware_datetime(now or datetime.now(timezone.utc))
        day_start_utc, day_end_utc = self._moscow_day_bounds_utc(checked_at)
        result = ChannelSubscriberSnapshotResult()

        bindings = await self.legacy_reader.fetch_all_bot_bindings()
        bindings_by_tg_id = {int(binding.channel_id): binding for binding in bindings}
        channels = list(
            (
                await session.execute(
                    select(Channel)
                    .where(Channel.is_active.is_(True))
                    .order_by(Channel.id.asc())
                )
            ).scalars().all()
        )

        for channel in channels:
            result.channels_checked += 1
            binding = bindings_by_tg_id.get(channel.tg_channel_id)
            if binding is None:
                continue

            try:
                channel.subscriber_count = await self._fetch_subscriber_count(binding)
                channel.subscriber_count_checked_at = checked_at
                result.subscriber_counts_updated += 1
            except Exception as exc:
                result.failed += 1
                result.errors.append(f"Channel {channel.id}: {exc}")
                continue

            await session.execute(
                sql_delete(ChannelSubscriberSnapshot)
                .where(ChannelSubscriberSnapshot.channel_id == channel.id)
                .where(ChannelSubscriberSnapshot.checked_at >= day_start_utc)
                .where(ChannelSubscriberSnapshot.checked_at < day_end_utc)
            )
            self._record_subscriber_snapshot(session, channel, checked_at)
            result.snapshots_recorded += 1

        deleted = await session.execute(
            sql_delete(ChannelSubscriberSnapshot).where(
                ChannelSubscriberSnapshot.checked_at < self._retention_cutoff_utc(checked_at, retention_days)
            )
        )
        result.snapshots_deleted = deleted.rowcount or 0
        await session.commit()
        return result

    @staticmethod
    def _record_subscriber_snapshot(session: AsyncSession, channel: Channel, checked_at: datetime) -> None:
        if channel.subscriber_count is None:
            return
        session.add(
            ChannelSubscriberSnapshot(
                channel_id=channel.id,
                subscriber_count=channel.subscriber_count,
                checked_at=checked_at,
            )
        )

    @staticmethod
    def _ensure_aware_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @classmethod
    def _moscow_day_bounds_utc(cls, value: datetime) -> tuple[datetime, datetime]:
        local_value = cls._ensure_aware_datetime(value).astimezone(MOSCOW_TZ)
        day_start = datetime.combine(local_value.date(), time.min, tzinfo=MOSCOW_TZ)
        day_end = day_start + timedelta(days=1)
        return day_start.astimezone(timezone.utc), day_end.astimezone(timezone.utc)

    @classmethod
    def _retention_cutoff_utc(cls, value: datetime, retention_days: int) -> datetime:
        local_value = cls._ensure_aware_datetime(value).astimezone(MOSCOW_TZ)
        cutoff_date = local_value.date() - timedelta(days=retention_days)
        cutoff_start = datetime.combine(cutoff_date, time.min, tzinfo=MOSCOW_TZ)
        return cutoff_start.astimezone(timezone.utc)

    async def _get_profile_by_slug(self, session: AsyncSession, slug: str) -> ChannelSettingProfile | None:
        return await session.scalar(
            select(ChannelSettingProfile).where(ChannelSettingProfile.slug == slug.strip().lower()).limit(1)
        )

    def _set_profile_setting(self, profile: ChannelSettingProfile, field_name: str, raw_value: str) -> None:
        if field_name not in PROFILE_SETTING_FIELDS:
            allowed = ", ".join(PROFILE_SETTING_FIELDS)
            raise ValueError(f"Unknown profile setting '{field_name}'. Available: {allowed}")
        expected_type = self.channel_service.EDITABLE_SETTINGS_TYPES[field_name]
        parsed_value = self.channel_service._parse_setting_value(
            field_name=field_name,
            raw_value=raw_value,
            expected_type=expected_type,
        )
        setattr(profile, field_name, parsed_value)

    @staticmethod
    def _select_profile(
        profiles: list[ChannelSettingProfile],
        subscriber_count: int | None,
    ) -> ChannelSettingProfile | None:
        if subscriber_count is None:
            return None

        matching = [
            profile
            for profile in profiles
            if subscriber_count >= profile.min_subscribers
            and (profile.max_subscribers is None or subscriber_count <= profile.max_subscribers)
        ]
        if not matching:
            return None
        return sorted(matching, key=lambda profile: (-profile.min_subscribers, profile.priority, profile.id or 0))[0]

    @staticmethod
    def _apply_profile(
        channel: Channel,
        profile: ChannelSettingProfile,
        applied_at: datetime | None = None,
    ) -> None:
        requires_slot_replan = any(
            field_name in AUTO_SLOT_REPLAN_FIELDS
            and (profile_value := getattr(profile, field_name)) is not None
            and getattr(channel, field_name) != profile_value
            for field_name in PROFILE_SETTING_FIELDS
        )
        for field_name in PROFILE_SETTING_FIELDS:
            profile_value: Any = getattr(profile, field_name)
            if profile_value is not None:
                setattr(channel, field_name, profile_value)
        if requires_slot_replan:
            channel.auto_slots_last_planned_for = None
        channel.settings_profile_id = profile.id
        channel.settings_profile_applied_at = applied_at or datetime.now(timezone.utc)

    @staticmethod
    def _profile_settings_match(channel: Channel, profile: ChannelSettingProfile) -> bool:
        for field_name in PROFILE_SETTING_FIELDS:
            profile_value: Any = getattr(profile, field_name)
            if profile_value is not None and getattr(channel, field_name) != profile_value:
                return False
        return channel.settings_profile_id == profile.id

    @staticmethod
    async def _fetch_subscriber_count(binding: LegacyBotBinding) -> int:
        bot = AsyncTeleBot(binding.bot_api_token)
        try:
            return int(await bot.get_chat_member_count(binding.channel_id))
        finally:
            await bot.close_session()
