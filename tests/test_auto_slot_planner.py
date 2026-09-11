from datetime import date, datetime, time, timezone

from src.editorial.models.channel import Channel
from src.editorial.services.auto_slot_planner import AutoSlotPlannerService


def _channel(**overrides) -> Channel:
    values = {
        "id": 26,
        "tg_channel_id": 1,
        "short_code": "test",
        "timezone": "Europe/Moscow",
        "min_slots_per_day": 0,
        "max_posts_per_day": 100,
        "max_paste_per_day": 3,
        "allow_pastes": True,
        "auto_slots_plan_time": time(5, 30),
        "auto_slots_window_start": time(8, 0),
        "auto_slots_window_end": time(23, 0),
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


def test_target_date_does_not_plan_after_publication_window() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(
        auto_slots_plan_time=time(23, 30),
        auto_slots_window_end=time(22, 0),
        auto_slots_last_planned_for=None,
    )
    now = datetime(2026, 7, 30, 20, 31, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, now) is None


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


def test_target_date_does_not_preplan_next_day_after_window() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(
        auto_slots_plan_time=time(5, 30),
        auto_slots_window_start=time(8, 0),
        auto_slots_window_end=time(22, 0),
        auto_slots_last_planned_for=None,
    )
    now = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, now) is None


def test_target_date_skips_current_day_when_it_is_already_planned() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(auto_slots_last_planned_for=date(2026, 7, 30))
    now = datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, now) is None


def test_target_date_waits_until_plan_time_for_current_day() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(auto_slots_last_planned_for=None)
    now = datetime(2026, 7, 30, 2, 29, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, now) is None


def test_target_date_starts_at_exact_plan_time_for_current_day() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(auto_slots_last_planned_for=None)
    now = datetime(2026, 7, 30, 2, 30, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, now) == date(2026, 7, 30)


def test_itmo_channel_is_planned_at_its_stable_offset() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(id=338, auto_slots_plan_time=time(5, 30))

    before_offset = datetime(2026, 7, 30, 2, 29, 59, tzinfo=timezone.utc)
    at_offset = datetime(2026, 7, 30, 2, 30, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, before_offset) is None
    assert service._target_date_for_channel(channel, at_offset) == date(2026, 7, 30)


def test_channels_with_same_plan_time_are_distributed_across_window() -> None:
    service = AutoSlotPlannerService()
    target_date = date(2026, 7, 30)
    scheduled_minutes: list[int] = []

    for channel_id in range(1, 101):
        planning_window = service._planning_window_for_channel(_channel(id=channel_id), target_date)

        assert planning_window is not None
        planned_at, deadline = planning_window
        assert planned_at <= deadline
        assert planned_at.hour == 5
        scheduled_minutes.append(planned_at.minute)

    assert min(scheduled_minutes) == 30
    assert max(scheduled_minutes) == 55
    assert len(set(scheduled_minutes)) == 26
    assert max(scheduled_minutes.count(minute) for minute in set(scheduled_minutes)) == 4


def test_target_date_does_not_run_after_thirty_minute_deadline() -> None:
    service = AutoSlotPlannerService()
    channel = _channel(id=26, auto_slots_last_planned_for=None)
    after_deadline = datetime(2026, 7, 30, 3, 0, 1, tzinfo=timezone.utc)

    assert service._target_date_for_channel(channel, after_deadline) is None
