from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import JSON, MetaData, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.editorial.db.base import EditorialBase
from src.editorial.models.channel import Channel, ChannelSlot
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import ContentItemStatus, ContentSourceType, PublicationStatus
from src.editorial.models.publication import PublicationLog
from src.editorial.models.submission import Submission
from src.editorial.services.auto_slot_planner import AutoSlotPlannerService
from src.editorial.services.scheduler import SchedulerService
from src.editorial.utils.text import compute_text_hash, normalize_text


DAY = date(2026, 10, 8)
PLAN_AT = datetime(2026, 10, 8, 2, 30, tzinfo=timezone.utc)
LIVE_TEXTS = [
    "The observatory invites astronomers to photograph distant constellations.",
    "Our kitchen needs a replacement refrigerator and new wooden cupboards.",
    "Swimming instructors are organising a championship in the renovated pool.",
    "A jazz ensemble will perform improvised melodies on saxophones tonight.",
    "Gardeners planted colourful tulips beside the railway station yesterday.",
    "Lost a purple backpack containing chemistry notes and a laboratory coat.",
    "A documentary explores ancient civilisations and archaeological discoveries.",
]
OLD_TEXT = "Previously published announcement about scholarship applications."


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    for table in EditorialBase.metadata.tables.values():
        clone = table.to_metadata(metadata)
        for column in clone.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    names = {"channels", "channel_slots", "submissions", "content_items", "publication_log", "channel_ad_blackouts"}
    async with engine.begin() as connection:
        await connection.run_sync(lambda conn: metadata.create_all(
            conn, tables=[metadata.tables[name] for name in names],
        ))
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(Channel(
            id=26, tg_channel_id=-10026, short_code="duplicate_queue_test",
            timezone="Europe/Moscow", is_active=True, auto_slots_enabled=True,
            auto_slots_plan_time=time(5, 30), auto_slots_window_start=time(8),
            auto_slots_window_end=time(23), min_slots_per_day=7,
            max_posts_per_day=40, max_paste_per_day=5, allow_pastes=True,
            slot_jitter_minutes=0, min_gap_minutes=0,
            same_tag_cooldown_hours=0, same_template_cooldown_hours=0,
        ))
        await db.flush()
        yield db
    await engine.dispose()


async def text_item(session, text, *, source=ContentSourceType.SUBMISSION, **values):
    item = ContentItem(
        channel_id=26, source_type=source, body_text=text,
        normalized_text=normalize_text(text), text_hash=compute_text_hash(text) or "",
        status=ContentItemStatus.APPROVED, review_required=False,
        created_at=PLAN_AT-timedelta(days=10), **values,
    )
    session.add(item)
    await session.flush()
    return item


async def log_item(session, item, status=PublicationStatus.SENT):
    item.status = ContentItemStatus.PUBLISHED if status == PublicationStatus.SENT else ContentItemStatus.SCHEDULED
    item.scheduled_for = PLAN_AT-timedelta(days=1)
    log = PublicationLog(
        content_item_id=item.id, channel_id=26, publish_status=status,
        scheduled_for=item.scheduled_for, created_at=item.scheduled_for,
        published_at=item.scheduled_for if status == PublicationStatus.SENT else None,
    )
    session.add(log)
    await session.flush()
    return log


async def ready_count(session):
    start, end = AutoSlotPlannerService._channel_day_bounds("Europe/Moscow", DAY)
    return await AutoSlotPlannerService()._count_approved_ready_items(session, 26, start, end)


async def seed_old_duplicates(session, count):
    published = await text_item(session, OLD_TEXT)
    await log_item(session, published)
    return [await text_item(session, OLD_TEXT) for _ in range(count)]


async def media_item(session, kind, message, *, caption="", album=None, chat=1001):
    submission = Submission(
        channel_id=26, source_chat_id=chat, source_message_id=message,
        content_type=kind, media_group_id=album, raw_text=caption,
        cleaned_text=caption, created_at=PLAN_AT-timedelta(days=2),
    )
    session.add(submission)
    await session.flush()
    # Include legacy caption/placeholder fingerprints: the scheduler repairs them.
    return await text_item(session, caption or "<media without a caption>", origin_submission_id=submission.id)


async def test_real_queue_replaces_grid_inflated_by_47_old_duplicates(session):
    duplicates = await seed_old_duplicates(session, 47)
    live = [await text_item(session, body) for body in LIVE_TEXTS]
    for minute in range(40):
        session.add(ChannelSlot(channel_id=26, weekday=DAY.weekday(), slot_time=time(8, minute), is_auto_managed=True))
    await session.commit()

    result = await AutoSlotPlannerService().run(session, now=PLAN_AT, channel_id=26)
    plan = result.plans[0]
    assert (plan.approved_ready_count, plan.target_slots, plan.paste_slots) == (7, 7, 0)
    slots = list((await session.scalars(select(ChannelSlot).where(ChannelSlot.channel_id == 26))).all())
    assert len(slots) == 7
    assert {slot.slot_time for slot in slots} == set(plan.slot_times)
    assert all(item.status == ContentItemStatus.APPROVED for item in duplicates)

    pastes = SimpleNamespace(list_available_for_channel=AsyncMock(return_value=[]))
    scheduler = SchedulerService(paste_service=pastes)
    selected = []
    for slot_time in plan.slot_times:
        slot_at = datetime.combine(DAY, slot_time, tzinfo=timezone(timedelta(hours=3))).astimezone(timezone.utc)
        item = await scheduler._pick_candidate(session, await session.get(Channel, 26), slot_at)
        assert item is not None
        selected.append(item.id)
        await log_item(session, item)
    assert set(selected) == {item.id for item in live}
    pastes.list_available_for_channel.assert_not_awaited()


async def test_similar_already_published_text_does_not_add_a_slot(session):
    old = await text_item(session, OLD_TEXT)
    await log_item(session, old)
    await text_item(session, OLD_TEXT.replace("applications", "application"))
    assert await ready_count(session) == 0


async def test_queue_counts_repeated_text_and_editorial_copy_only_once(session):
    await text_item(session, LIVE_TEXTS[0])
    await text_item(session, LIVE_TEXTS[0], source=ContentSourceType.EDITORIAL)
    await text_item(session, LIVE_TEXTS[0].replace("constellations", "constellation"))
    assert await ready_count(session) == 1


@pytest.mark.parametrize("kind", ["photo", "video", "animation"])
@pytest.mark.parametrize("caption", ["", OLD_TEXT])
async def test_distinct_media_with_equal_captions_keep_their_slots(session, kind, caption):
    old = await media_item(session, kind, 10, caption=caption)
    await log_item(session, old)
    await media_item(session, kind, 11, caption=caption)
    await media_item(session, kind, 12, caption=caption)
    assert await ready_count(session) == 2


@pytest.mark.parametrize("status", [PublicationStatus.SENT, PublicationStatus.SCHEDULED])
@pytest.mark.parametrize("album", [None, "same-album"])
async def test_published_or_reserved_media_source_does_not_add_a_slot(session, status, album):
    old = await media_item(session, "photo", 10, album=album)
    await log_item(session, old, status)
    await media_item(session, "video", 11 if album else 10, album=album)
    assert await ready_count(session) == 0


async def test_pending_album_members_count_once_and_other_chats_remain_distinct(session):
    await media_item(session, "photo", 10, album="album")
    await media_item(session, "video", 11, album="album")
    await media_item(session, "photo", 10, album="album", chat=1002)
    assert await ready_count(session) == 2


async def test_equal_media_caption_does_not_block_live_text_slot(session):
    media = await media_item(session, "photo", 10, caption=LIVE_TEXTS[0])
    await log_item(session, media)
    await text_item(session, LIVE_TEXTS[0])
    assert await ready_count(session) == 1


async def test_scheduler_finds_live_item_after_more_than_50_old_duplicates(session):
    await seed_old_duplicates(session, 61)
    live = await text_item(session, LIVE_TEXTS[0])
    pastes = SimpleNamespace(list_available_for_channel=AsyncMock(return_value=[]))
    scheduler = SchedulerService(paste_service=pastes)
    candidate = await scheduler._pick_candidate(session, await session.get(Channel, 26), PLAN_AT)
    assert candidate is live
    assert await ready_count(session) == 1
    pastes.list_available_for_channel.assert_not_awaited()


async def test_date_and_status_filters_still_exclude_unavailable_content(session):
    await text_item(session, LIVE_TEXTS[0])
    rejected = await text_item(session, LIVE_TEXTS[1]); rejected.status = ContentItemStatus.REJECTED
    held = await text_item(session, LIVE_TEXTS[2]); held.status = ContentItemStatus.HOLD
    await text_item(session, LIVE_TEXTS[3], publish_after=PLAN_AT+timedelta(days=1))
    await text_item(session, LIVE_TEXTS[4], expires_at=PLAN_AT-timedelta(days=1))
    scheduled = await text_item(session, LIVE_TEXTS[5]); await log_item(session, scheduled, PublicationStatus.SCHEDULED)
    await text_item(session, LIVE_TEXTS[6], source=ContentSourceType.PASTE)
    await text_item(session, "Generated filler", source=ContentSourceType.GENERATED)
    assert await ready_count(session) == 1
