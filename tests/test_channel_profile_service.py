from datetime import date, datetime, time, timezone

from src.editorial.models.channel import Channel, ChannelSettingProfile
from src.editorial.services.channel_profile_service import AUTO_SLOT_REPLAN_FIELDS, ChannelProfileService


def _profile(slug: str, min_subscribers: int, max_subscribers: int | None, **overrides) -> ChannelSettingProfile:
    values = {
        "slug": slug,
        "title": slug,
        "min_subscribers": min_subscribers,
        "max_subscribers": max_subscribers,
        "priority": 100,
        "min_gap_minutes": 1,
        "slot_jitter_minutes": 30,
        "auto_slots_enabled": True,
        "auto_slots_plan_time": time(23, 30),
        "auto_slots_window_start": time(10, 0),
        "auto_slots_window_end": time(22, 0),
        "auto_slots_replace_manual": True,
        "min_slots_per_day": 0,
        "max_posts_per_day": 6,
        "max_generated_per_day": 1,
        "max_paste_per_day": 3,
        "same_tag_cooldown_hours": 0,
        "same_template_cooldown_hours": 0,
        "same_paste_cooldown_days": 120,
        "min_ready_queue": 3,
        "prefer_real_ratio": 70,
        "allow_generated": True,
        "allow_pastes": True,
    }
    values.update(overrides)
    return ChannelSettingProfile(**values)


def test_select_profile_uses_subscriber_thresholds() -> None:
    profiles = [
        _profile("starter", 0, 49),
        _profile("growing", 50, 999),
        _profile("large", 1000, None),
    ]

    assert ChannelProfileService._select_profile(profiles, 0).slug == "starter"
    assert ChannelProfileService._select_profile(profiles, 50).slug == "growing"
    assert ChannelProfileService._select_profile(profiles, 1000).slug == "large"


def test_select_profile_prefers_highest_matching_min_subscribers() -> None:
    profiles = [
        _profile("base", 0, None, priority=100),
        _profile("specific", 50, 100, priority=100),
    ]

    assert ChannelProfileService._select_profile(profiles, 75).slug == "specific"


def test_apply_profile_copies_publication_policy_to_channel() -> None:
    channel = Channel(tg_channel_id=1, short_code="test", timezone="Europe/Moscow")
    profile = _profile(
        "growing",
        50,
        999,
        id=42,
        min_slots_per_day=5,
        max_posts_per_day=8,
        max_paste_per_day=4,
        auto_slots_window_start=time(9, 0),
        auto_slots_window_end=time(23, 0),
    )

    ChannelProfileService._apply_profile(channel, profile)

    assert channel.settings_profile_id == 42
    assert channel.min_slots_per_day == 5
    assert channel.max_posts_per_day == 8
    assert channel.max_paste_per_day == 4
    assert channel.auto_slots_window_start == time(9, 0)
    assert channel.auto_slots_window_end == time(23, 0)


def test_apply_profile_keeps_channel_timezone() -> None:
    channel = Channel(tg_channel_id=1, short_code="test", timezone="Asia/Yekaterinburg")
    profile = _profile("growing", 50, 999, id=42, timezone="Europe/Moscow")

    ChannelProfileService._apply_profile(channel, profile)

    assert channel.timezone == "Asia/Yekaterinburg"


def test_apply_profile_resets_planned_date_when_slot_policy_changes() -> None:
    channel = Channel(
        tg_channel_id=1,
        short_code="test",
        timezone="Europe/Moscow",
        min_slots_per_day=3,
        auto_slots_last_planned_for=date(2026, 9, 10),
    )
    profile = _profile("growing", 50, 999, id=42, min_slots_per_day=5)

    ChannelProfileService._apply_profile(channel, profile)

    assert channel.auto_slots_last_planned_for is None


def test_apply_profile_preserves_planned_date_when_slot_policy_is_unchanged() -> None:
    profile = _profile("growing", 50, 999, id=42, min_slots_per_day=5)
    channel = Channel(
        tg_channel_id=1,
        short_code="test",
        timezone="Europe/Moscow",
        auto_slots_last_planned_for=date(2026, 9, 10),
        **{field_name: getattr(profile, field_name) for field_name in AUTO_SLOT_REPLAN_FIELDS},
    )

    ChannelProfileService._apply_profile(channel, profile)

    assert channel.auto_slots_last_planned_for == date(2026, 9, 10)


def test_profile_settings_match_detects_changed_profile_values() -> None:
    channel = Channel(
        tg_channel_id=1,
        short_code="test",
        timezone="Europe/Moscow",
        settings_profile_id=42,
        min_slots_per_day=0,
        max_posts_per_day=6,
    )
    profile = _profile("growing", 50, 999, id=42, min_slots_per_day=3, max_posts_per_day=6)

    assert not ChannelProfileService._profile_settings_match(channel, profile)


def test_subscriber_snapshot_moscow_day_bounds_and_retention_cutoff() -> None:
    checked_at = datetime(2026, 8, 6, 23, 30, tzinfo=timezone.utc)

    day_start, day_end = ChannelProfileService._moscow_day_bounds_utc(checked_at)
    cutoff = ChannelProfileService._retention_cutoff_utc(checked_at, 14)

    assert day_start == datetime(2026, 8, 6, 21, 0, tzinfo=timezone.utc)
    assert day_end == datetime(2026, 8, 7, 21, 0, tzinfo=timezone.utc)
    assert cutoff == datetime(2026, 7, 23, 21, 0, tzinfo=timezone.utc)
