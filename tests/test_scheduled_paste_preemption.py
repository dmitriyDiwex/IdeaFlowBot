from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.editorial.models.channel import Channel
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import ContentFamily, ContentItemStatus, ContentSourceType, PublicationStatus
from src.editorial.models.publication import PublicationLog
from src.editorial.services.publisher import PublisherService
from src.editorial.services.scheduler import SchedulerService


def _scheduled_paste(now: datetime) -> tuple[PublicationLog, ContentItem, Channel]:
    channel = Channel(
        id=235,
        tg_channel_id=-100235,
        short_code="izhgmu_bot",
        title="izhgmu_bot",
        content_family="overheard",
        is_active=True,
    )
    item = ContentItem(
        id=100,
        channel_id=channel.id,
        source_type=ContentSourceType.PASTE,
        origin_paste_id=75,
        body_text="Fallback paste",
        status=ContentItemStatus.SCHEDULED,
        scheduled_for=now,
    )
    log_item = PublicationLog(
        id=200,
        content_item_id=item.id,
        channel_id=channel.id,
        scheduled_for=now,
        publish_status=PublicationStatus.SCHEDULED,
        attempt_count=0,
        created_at=now - timedelta(minutes=30),
    )
    return log_item, item, channel


def _approved_submission(channel_id: int) -> ContentItem:
    return ContentItem(
        id=101,
        channel_id=channel_id,
        source_type=ContentSourceType.SUBMISSION,
        origin_submission_id=300,
        body_text="Approved live submission",
        status=ContentItemStatus.APPROVED,
    )


@pytest.mark.asyncio
async def test_scheduled_paste_yields_its_slot_to_approved_live_content() -> None:
    now = datetime(2026, 8, 27, 13, 23, tzinfo=timezone.utc)
    log_item, paste_item, channel = _scheduled_paste(now)
    live_item = _approved_submission(channel.id)
    service = SchedulerService()
    service._pick_live_candidate = AsyncMock(return_value=live_item)

    replacement = await service.replace_scheduled_paste_with_live_candidate(
        SimpleNamespace(),
        log_item=log_item,
        scheduled_item=paste_item,
        channel=channel,
        eligible_at=now,
    )

    assert replacement is live_item
    assert log_item.content_item_id == live_item.id
    assert live_item.status == ContentItemStatus.SCHEDULED
    assert live_item.scheduled_for == log_item.scheduled_for
    assert paste_item.status == ContentItemStatus.APPROVED
    assert paste_item.scheduled_for is None


@pytest.mark.asyncio
async def test_confession_paste_yields_its_slot_to_approved_confession() -> None:
    now = datetime(2026, 8, 27, 13, 23, tzinfo=timezone.utc)
    log_item, paste_item, channel = _scheduled_paste(now)
    channel.content_family = ContentFamily.CONFESSION.value
    live_item = _approved_submission(channel.id)
    service = SchedulerService()
    service._pick_live_candidate = AsyncMock(return_value=live_item)

    replacement = await service.replace_scheduled_paste_with_live_candidate(
        SimpleNamespace(),
        log_item=log_item,
        scheduled_item=paste_item,
        channel=channel,
        eligible_at=now,
    )

    assert replacement is live_item
    assert log_item.content_item_id == live_item.id
    assert live_item.status == ContentItemStatus.SCHEDULED
    assert paste_item.status == ContentItemStatus.APPROVED


@pytest.mark.asyncio
async def test_publisher_rechecks_fallback_paste_before_sending() -> None:
    now = datetime(2026, 8, 27, 13, 23, tzinfo=timezone.utc)
    log_item, paste_item, channel = _scheduled_paste(now)
    live_item = _approved_submission(channel.id)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[log_item, None]),
        get=AsyncMock(side_effect=[paste_item, channel]),
        commit=AsyncMock(),
    )
    legacy_reader = SimpleNamespace(
        get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="1:token"))
    )
    status_sync = SimpleNamespace(
        mark_content_item_published=AsyncMock(return_value=1),
        reconcile_published_review_statuses=AsyncMock(return_value=0),
    )
    scheduler = SchedulerService()
    scheduler._pick_live_candidate = AsyncMock(return_value=live_item)
    service = PublisherService(
        telegram_adapter=SimpleNamespace(),
        legacy_reader=legacy_reader,
        legacy_publication_status=status_sync,
        scheduler=scheduler,
    )
    service._publish_submission_based_item = AsyncMock(return_value=777)

    result = await service.run(session, now=now, limit=1)

    assert result.sent == 1
    assert log_item.content_item_id == live_item.id
    assert live_item.status == ContentItemStatus.PUBLISHED
    assert paste_item.status == ContentItemStatus.APPROVED
    service._publish_submission_based_item.assert_awaited_once_with(
        session=session,
        content_item=live_item,
        channel=channel,
        bot_token="1:token",
    )
    status_sync.mark_content_item_published.assert_awaited_once_with(live_item.id)


@pytest.mark.asyncio
async def test_retrying_paste_is_not_replaced_after_an_uncertain_telegram_attempt() -> None:
    now = datetime(2026, 8, 27, 13, 23, tzinfo=timezone.utc)
    log_item, paste_item, channel = _scheduled_paste(now)
    log_item.attempt_count = 1
    log_item.retry_after = now
    service = SchedulerService()
    service._pick_live_candidate = AsyncMock(return_value=_approved_submission(channel.id))

    replacement = await service.replace_scheduled_paste_with_live_candidate(
        SimpleNamespace(),
        log_item=log_item,
        scheduled_item=paste_item,
        channel=channel,
        eligible_at=now,
    )

    assert replacement is paste_item
    assert log_item.content_item_id == paste_item.id
    service._pick_live_candidate.assert_not_awaited()
