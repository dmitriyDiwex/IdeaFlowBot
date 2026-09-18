from datetime import datetime, time, timedelta, timezone
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
from src.editorial.services.moderation import ModerationService
from src.editorial.services.publisher import PublisherService
from src.editorial.services.scheduler import SchedulerService
from src.editorial.utils.media import build_media_fingerprint
from src.editorial.utils.text import compute_text_hash, normalize_text


NOW = datetime(2026, 9, 18, 6, tzinfo=timezone.utc)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Run the real ORM queries while adapting only PostgreSQL's JSON type.
    metadata = MetaData()
    for table in EditorialBase.metadata.tables.values():
        clone = table.to_metadata(metadata)
        for column in clone.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    names = {"channels", "channel_slots", "channel_ad_blackouts", "submissions", "content_items", "publication_log"}
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: metadata.create_all(conn, tables=[metadata.tables[name] for name in names])
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(Channel(
            id=78, tg_channel_id=-1003874277116, short_code="rggmuni_bot",
            title="RGGMUni_bot", is_active=True, timezone="Europe/Moscow",
            slot_jitter_minutes=0, min_gap_minutes=0, max_posts_per_day=20,
            same_tag_cooldown_hours=0, same_template_cooldown_hours=0,
        ))
        await db.flush()
        yield db
    await engine.dispose()


async def _submission(session, kind, message_id, *, chat_id=1578880247, caption=None, album=None):
    sub = Submission(
        channel_id=78, source_chat_id=chat_id, source_message_id=message_id,
        content_type=kind, media_group_id=album, cleaned_text=caption, raw_text=caption,
        status="content_created", is_anonymous=True, created_at=NOW-timedelta(days=1),
    )
    session.add(sub)
    await session.flush()
    return sub


async def _item(session, sub, *, published=False, body=None):
    if body is None:
        body = sub.cleaned_text or ModerationService._build_media_placeholder(sub, 1)
    # Seed the incorrect hashes that shipped before this fix.
    item = ContentItem(
        channel_id=78, source_type=ContentSourceType.SUBMISSION, origin_submission_id=sub.id,
        body_text=body, normalized_text=normalize_text(body), text_hash=compute_text_hash(body) or "",
        status=ContentItemStatus.PUBLISHED if published else ContentItemStatus.APPROVED,
        review_required=False, created_at=NOW-timedelta(days=1),
    )
    session.add(item)
    await session.flush()
    return item


async def _log(session, item, status=PublicationStatus.SENT):
    log = PublicationLog(
        channel_id=78, content_item_id=item.id, publish_status=status,
        scheduled_for=NOW-timedelta(days=1), created_at=NOW-timedelta(days=1),
        published_at=NOW-timedelta(days=1) if status==PublicationStatus.SENT else None,
    )
    session.add(log)
    await session.flush()
    return log


@pytest.mark.parametrize("kind", ["photo", "video", "animation"])
@pytest.mark.parametrize("caption", [None, "Одинаковая подпись"])
async def test_legacy_media_is_repaired_scheduled_before_pastes_and_published(session, kind, caption):
    old_sub = await _submission(session, kind, 281, caption=caption)
    old_item = await _item(session, old_sub, published=True)
    await _log(session, old_item)
    sub = await _submission(session, kind, 354, caption=caption)
    item = await _item(session, sub)
    assert old_item.text_hash == item.text_hash
    session.add(ChannelSlot(channel_id=78, weekday=4, slot_time=time(9), is_active=True))
    await session.commit()

    paste_service = SimpleNamespace(
        build_availability_context=AsyncMock(return_value=SimpleNamespace()),
        list_available_for_channel=AsyncMock(return_value=[]),
    )
    result = await SchedulerService(paste_service=paste_service).run(session, now=NOW)
    assert result.scheduled_items == 1
    assert item.status == ContentItemStatus.SCHEDULED
    assert (item.normalized_text, item.text_hash) == build_media_fingerprint(sub)
    assert item.text_hash != old_item.text_hash
    paste_service.list_available_for_channel.assert_not_awaited()
    await session.refresh(item)
    assert (item.normalized_text, item.text_hash) == build_media_fingerprint(sub)

    adapter = SimpleNamespace(copy_message=AsyncMock(return_value=777))
    status_sync = SimpleNamespace(
        mark_content_item_published=AsyncMock(), reconcile_published_review_statuses=AsyncMock(),
    )
    publisher = PublisherService(
        telegram_adapter=adapter,
        legacy_reader=SimpleNamespace(get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="1:test"))),
        legacy_publication_status=status_sync,
    )
    publisher.should_add_channel_signature = AsyncMock(return_value=False)
    publisher._get_related_legacy_rows = AsyncMock(return_value=[])
    published = await publisher.run(session, now=NOW, limit=1)
    assert published.sent == 1
    assert published.failed == 0
    assert item.status == ContentItemStatus.PUBLISHED
    adapter.copy_message.assert_awaited_once_with(
        bot_token="1:test", channel_id=-1003874277116,
        from_chat_id=sub.source_chat_id, message_id=354, caption=caption or "", parse_mode=None,
    )
    log = await session.scalar(select(PublicationLog).where(PublicationLog.content_item_id==item.id))
    assert log.publish_status == PublicationStatus.SENT
    assert log.telegram_message_id == 777


@pytest.mark.parametrize("kind", ["photo", "video"])
async def test_new_media_gets_distinct_fingerprints_with_the_same_caption(session, kind):
    service = ModerationService(tag_service=SimpleNamespace(
        apply_tags_to_content_cache=AsyncMock(return_value=([], None)),
    ))
    first_sub = await _submission(session, kind, 352)
    second_sub = await _submission(session, kind, 354)
    first = await service.create_content_from_submission(session, first_sub.id)
    second = await service.create_content_from_submission(session, second_sub.id)
    assert first.body_text == second.body_text
    assert first.text_hash != second.text_hash
    assert first.normalized_text != second.normalized_text


@pytest.mark.parametrize("status", [PublicationStatus.SENT, PublicationStatus.SCHEDULED])
@pytest.mark.parametrize("same_submission", [True, False])
async def test_same_media_source_is_blocked_even_with_old_hashes(session, status, same_submission):
    old_sub = await _submission(session, "video", 352)
    old_item = await _item(session, old_sub, published=True)
    await _log(session, old_item, status)
    sub = old_sub if same_submission else await _submission(session, "video", 352)
    item = await _item(session, sub, body="Редактированная подпись")
    assert await SchedulerService()._is_duplicate_for_channel(session, 78, item) is True


@pytest.mark.parametrize("status", [PublicationStatus.CANCELLED, PublicationStatus.FAILED])
async def test_unsent_media_can_be_scheduled_again(session, status):
    sub = await _submission(session, "photo", 354)
    old_item = await _item(session, sub)
    await _log(session, old_item, status)
    item = await _item(session, sub)
    assert await SchedulerService()._is_duplicate_for_channel(session, 78, item) is False


@pytest.mark.parametrize(("album", "chat_id", "duplicate"), [
    ("album-1", 1578880247, True),
    ("album-2", 1578880247, False),
    ("album-1", 1578880248, False),
])
async def test_album_identity_handles_mixed_media_and_distinct_sources(session, album, chat_id, duplicate):
    old_sub = await _submission(session, "photo", 11, album="album-1")
    old_item = await _item(session, old_sub, published=True)
    await _log(session, old_item)
    sub = await _submission(session, "video", 12, album=album, chat_id=chat_id)
    item = await _item(session, sub)
    assert await SchedulerService()._is_duplicate_for_channel(session, 78, item) is duplicate
    if duplicate:
        assert build_media_fingerprint(old_sub) == build_media_fingerprint(sub)


@pytest.mark.parametrize("body", ["<фото без подписи>", "Одинаковая подпись"])
async def test_media_caption_and_placeholder_do_not_block_text_posts(session, body):
    old_sub = await _submission(session, "photo", 281)
    old_item = await _item(session, old_sub, published=True, body=body)
    await _log(session, old_item)
    sub = await _submission(session, "text", 354, caption=body)
    item = await _item(session, sub)
    assert await SchedulerService()._is_duplicate_for_channel(session, 78, item) is False


@pytest.mark.parametrize(("body", "duplicate"), [
    ("Как получить зачёт по философии?", True),
    ("Как получить зачет по философии?", True),
    ("Есть ли на Металлистов медпункт?", False),
])
async def test_text_duplicate_checks_still_work(session, body, duplicate):
    old_sub = await _submission(session, "text", 281, caption="Как получить зачёт по философии?")
    old_item = await _item(session, old_sub, published=True)
    await _log(session, old_item)
    sub = await _submission(session, "text", 354, caption=body)
    item = await _item(session, sub)
    assert await SchedulerService()._is_duplicate_for_channel(session, 78, item) is duplicate


def test_media_fingerprint_is_stable_when_caption_or_album_size_changes():
    sub = SimpleNamespace(id=1, channel_id=78, source_chat_id=1001,
                          source_message_id=11, content_type="photo", media_group_id="album-1")
    first = ModerationService._build_submission_fingerprint(sub, 1, "<медиа-группа: 1 влож.>")
    sub.source_message_id = 12
    sub.content_type = "video"
    second = ModerationService._build_submission_fingerprint(sub, 2, "Новая подпись")
    assert first == second
