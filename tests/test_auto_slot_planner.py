from datetime import date, datetime, time, timezone

from src.editorial.models.channel import Channel
from src.editorial.services.auto_slot_planner import AutoSlotPlannerService


def _channel(**overrides) -> Channel:
    values = {
        "tg_channel_id": 1,
        "short_code": "test",
        "timezone": "Europe/Moscow",
        "min_slots_per_day": 0,
        "max_posts_per_day": 100,
        "max_paste_per_day": 3,
        "allow_pastes": True,
        "auto_slots_plan_time": time(23, 30),
        "auto_slots_window_start": time(10, 0),
        "auto_slots_window_end": time(22, 0),
        "auto_slots_last_planned_for": None,
    }
    values.update(overrides)
    return Channel(**values)


def test_target_slots_fall_back_to_paste_limit_when_no_approved_items() -> None:
    channel = _channel(max_paste_per_day=3, max_posts_per_day=10)

    target_slots, paste_slots = AutoSlotPlannerService._calculate_target_slots(channel, approved_ready_count=0)

    assert target_slots == 3
    assert paste_slots == 3


def test_target_slots_use_approved_count_when_it_exceeds_paste_limit() -> None:
    channel = _channel(max_paste_per_day=3, max_posts_per_day=10)

    target_slots, paste_slots = AutoSlotPlannerService._calculate_target_slots(channel, approved_ready_count=7)

    assert target_slots == 7
    assert paste_slots == 0


def test_target_slots_are_capped_by_max_posts_per_day() -> None:
    channel = _channel(max_paste_per_day=3, max_posts_per_day=5)

    target_slots, paste_slots = AutoSlotPlannerService._calculate_target_slots(channel, approved_ready_count=12)

    assert target_slots == 5
    assert paste_slots == 0


def test_target_slots_respect_min_slots_per_day_without_raising_paste_limit() -> None:
    channel = _channel(min_slots_per_day=5, max_paste_per_day=2, max_posts_per_day=10)

    target_slots, paste_slots = AutoSlotPlannerService._calculate_target_slots(channel, approved_ready_count=0)

    assert target_slots == 5
    assert paste_slots == 2


def test_spread_slot_times_evenly_across_window() -> None:
    slot_times = AutoSlotPlannerService._spread_slot_times(
        start=time(10, 0),
        end=time(22, 0),
        count=3,
    )

    assert slot_times == [time(10, 0), time(16, 0), time(22, 0)]


def test_spread_slot_times_caps_count_by_min_gap() -> None:
    slot_times = AutoSlotPlannerService._spread_slot_times(
        start=time(10, 0),
        end=time(12, 0),
        count=5,
        min_gap_minutes=60,
    )

    assert slot_times == [time(10, 0), time(11, 0), time(12, 0)]


def test_target_date_uses_next_day_when_plan_time_is_after_window() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(auto_slots_last_planned_for=None)
    now = datetime(2026, 7, 30, 20, 31, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, now) == date(2026, 7, 31)


def test_target_date_uses_current_day_when_run_is_before_window_end() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(
        auto_slots_plan_time=time(5, 30),
        auto_slots_window_start=time(8, 0),
        auto_slots_window_end=time(23, 0),
        auto_slots_last_planned_for=None,
    )
    now = datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, now) == date(2026, 7, 30)


def test_target_date_uses_next_day_when_a_normal_run_was_delayed_past_window() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(
        auto_slots_plan_time=time(5, 30),
        auto_slots_window_start=time(8, 0),
        auto_slots_window_end=time(22, 0),
        auto_slots_last_planned_for=None,
    )
    now = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, now) == date(2026, 7, 31)


def test_target_date_skips_next_day_when_it_is_already_planned() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(auto_slots_last_planned_for=date(2026, 7, 31))
    now = datetime(2026, 7, 30, 20, 31, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, now) is None


def test_target_date_waits_until_plan_time_for_current_day() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(auto_slots_last_planned_for=None)
    now = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, now) is None
