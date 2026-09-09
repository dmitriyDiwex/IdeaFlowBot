from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.editorial.models.ad_blackout import ChannelAdBlackout
from src.editorial.models.channel import Channel
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import ContentItemStatus, ContentSourceType, PublicationStatus
from src.editorial.models.publication import PublicationLog
from src.editorial.services.publisher import PublisherService
from src.editorial.services.telegram_resilience import TelegramOperationTimeout


def _publication_objects(now: datetime) -> tuple[PublicationLog, ContentItem, Channel]:
    channel = Channel(
        id=7,
        tg_channel_id=-1007,
        short_code="test",
        title="Test",
        is_active=True,
    )
    item = ContentItem(
        id=11,
        channel_id=channel.id,
        source_type=ContentSourceType.EDITORIAL,
        body_text="Message",
        status=ContentItemStatus.SCHEDULED,
        scheduled_for=now,
    )
    log_item = PublicationLog(
        id=13,
        content_item_id=item.id,
        channel_id=channel.id,
        scheduled_for=now,
        publish_status=PublicationStatus.SCHEDULED,
        attempt_count=0,
        created_at=now,
    )
    return log_item, item, channel


@pytest.mark.asyncio
async def test_ad_blackout_keeps_occupied_slot_scheduled_until_window_ends() -> None:
    now = datetime(2026, 8, 20, 20, 20, tzinfo=timezone.utc)
    log_item, item, channel = _publication_objects(now)
    blackout = ChannelAdBlackout(
        channel_id=channel.id,
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(minutes=55),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[log_item, blackout]),
        get=AsyncMock(side_effect=[item, channel]),
        commit=AsyncMock(),
    )
    status_sync = SimpleNamespace(
        mark_content_item_published=AsyncMock(return_value=0),
        reconcile_published_review_statuses=AsyncMock(return_value=0),
    )
    service = PublisherService(
        telegram_adapter=SimpleNamespace(),
        legacy_publication_status=status_sync,
    )
    service._publish_submission_based_item = AsyncMock()

    result = await service.run(session, now=now, limit=1)

    assert result.attempted == 0
    assert result.deferred == 1
    assert result.sent == 0
    assert log_item.publish_status == PublicationStatus.SCHEDULED
    assert item.status == ContentItemStatus.SCHEDULED
    assert log_item.retry_after == blackout.ends_at
    assert "Deferred by ad blackout" in log_item.error_text
    service._publish_submission_based_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_error_keeps_message_scheduled_for_later_retry(monkeypatch) -> None:
    now = datetime(2026, 8, 20, 20, 20, tzinfo=timezone.utc)
    log_item, item, channel = _publication_objects(now)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[log_item, None]),
        get=AsyncMock(side_effect=[item, channel]),
        commit=AsyncMock(),
    )
    legacy_reader = SimpleNamespace(
        get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="1:token"))
    )
    status_sync = SimpleNamespace(
        mark_content_item_published=AsyncMock(return_value=0),
        reconcile_published_review_statuses=AsyncMock(return_value=0),
    )
    service = PublisherService(
        telegram_adapter=SimpleNamespace(),
        legacy_reader=legacy_reader,
        legacy_publication_status=status_sync,
    )
    service._publish_submission_based_item = AsyncMock(
        side_effect=TelegramOperationTimeout("Telegram sendMessage exceeded 15 seconds")
    )

    result = await service.run(session, now=now, limit=20)

    assert result.attempted == 1
    assert result.sent == 0
    assert result.deferred == 1
    assert result.failed == 0
    assert log_item.publish_status == PublicationStatus.SCHEDULED
    assert item.status == ContentItemStatus.SCHEDULED
    assert log_item.attempt_count == 1
    assert log_item.last_attempt_at == now
    assert log_item.retry_after == now + timedelta(seconds=60)
    assert "retry scheduled" in log_item.error_text
    session.commit.assert_awaited_once()
    status_sync.reconcile_published_review_statuses.assert_awaited_once_with(limit=20)


@pytest.mark.asyncio
async def test_successful_retry_marks_message_sent(monkeypatch) -> None:
    now = datetime(2026, 8, 20, 20, 22, tzinfo=timezone.utc)
    log_item, item, channel = _publication_objects(now - timedelta(minutes=2))
    log_item.attempt_count = 1
    log_item.retry_after = now - timedelta(seconds=1)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[log_item, None, None]),
        get=AsyncMock(side_effect=[item, channel]),
        commit=AsyncMock(),
    )
    legacy_reader = SimpleNamespace(
        get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="1:token"))
    )
    status_sync = SimpleNamespace(
        mark_content_item_published=AsyncMock(return_value=1),
        reconcile_published_review_statuses=AsyncMock(return_value=0),
    )
    service = PublisherService(
        telegram_adapter=SimpleNamespace(),
        legacy_reader=legacy_reader,
        legacy_publication_status=status_sync,
    )
    service._publish_submission_based_item = AsyncMock(return_value=777)

    result = await service.run(session, now=now, limit=1)

    assert result.sent == 1
    assert result.deferred == 0
    assert log_item.publish_status == PublicationStatus.SENT
    assert log_item.telegram_message_id == 777
    assert log_item.published_at == now
    assert log_item.retry_after is None
    assert log_item.error_text is None
    assert item.status == ContentItemStatus.PUBLISHED
    status_sync.mark_content_item_published.assert_awaited_once_with(item.id)
    status_sync.reconcile_published_review_statuses.assert_awaited_once_with(limit=20)


@pytest.mark.asyncio
async def test_permanent_error_moves_message_to_hold() -> None:
    now = datetime(2026, 9, 9, 16, 55, tzinfo=timezone.utc)
    log_item, item, channel = _publication_objects(now)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[log_item, None]),
        get=AsyncMock(side_effect=[item, channel]),
        commit=AsyncMock(),
    )
    legacy_reader = SimpleNamespace(
        get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="1:token"))
    )
    status_sync = SimpleNamespace(
        mark_content_item_published=AsyncMock(return_value=0),
        reconcile_published_review_statuses=AsyncMock(return_value=0),
    )
    service = PublisherService(
        telegram_adapter=SimpleNamespace(),
        legacy_reader=legacy_reader,
        legacy_publication_status=status_sync,
    )
    service._publish_submission_based_item = AsyncMock(
        side_effect=RuntimeError("Bad Request: MEDIA_CAPTION_TOO_LONG")
    )

    result = await service.run(session, now=now, limit=1)

    assert result.attempted == 1
    assert result.sent == 0
    assert result.failed == 1
    assert log_item.publish_status == PublicationStatus.FAILED
    assert log_item.retry_after is None
    assert "MEDIA_CAPTION_TOO_LONG" in log_item.error_text
    assert item.status == ContentItemStatus.HOLD
    assert item.scheduled_for is None
    status_sync.mark_content_item_published.assert_not_awaited()
