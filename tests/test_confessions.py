from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.editorial.models.enums import ContentFamily, PasteDeliveryMode, PasteStatus
from src.editorial.models.confession import ConfessionPasteCandidate
from src.editorial.models.paste import PasteLibrary
from src.editorial.services.confession_service import ConfessionService
from src.editorial.services.paste_service import PasteAvailabilityContext
from src.editorial.services.publisher import PublisherService
from src.confession_publisher import ConfessionPublisherRuntime
from src.panel_markups import build_confession_channel_actions


def _paste(paste_id: int, family: str) -> PasteLibrary:
    paste = PasteLibrary(
        title=f"paste-{paste_id}",
        body_text=f"body-{paste_id}",
        normalized_text=f"body-{paste_id}",
        text_hash=f"hash-{paste_id}",
        content_family=family,
        delivery_mode=(
            PasteDeliveryMode.TELEGRAM_COPY.value
            if family == ContentFamily.CONFESSION.value
            else PasteDeliveryMode.TEXT.value
        ),
        tags=[],
        status=PasteStatus.ACTIVE,
        global_cooldown_days=0,
        per_channel_cooldown_days=0,
        allow_all_channels=True,
        min_channel_activity_score=0,
    )
    paste.id = paste_id
    return paste


def test_confession_channel_only_receives_confession_pastes() -> None:
    ordinary = _paste(1, ContentFamily.OVERHEARD.value)
    confession = _paste(2, ContentFamily.CONFESSION.value)
    context = PasteAvailabilityContext(
        reference_now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        channel_ids=frozenset({10}),
        pastes=[ordinary, confession],
        channel_families={10: ContentFamily.CONFESSION.value},
        global_included={"ordinary-only-rule"},
    )

    assert context.available_for_channel(10) == [confession]


@pytest.mark.parametrize(
    ("text", "caption", "expected"),
    [
        ("/ служебное сообщение", None, True),
        ("   /не сохранять", None, True),
        (None, "/ подпись к фото", True),
        ("обычная паста / со слешем внутри", None, False),
        (None, "обычная подпись", False),
    ],
)
def test_slash_prefix_marks_storage_message_as_service(
    text: str | None,
    caption: str | None,
    expected: bool,
) -> None:
    message = SimpleNamespace(text=text, caption=caption)

    assert ConfessionPublisherRuntime._is_service_message(message) is expected


def test_confession_candidate_confirmation_buttons() -> None:
    markup = ConfessionPublisherRuntime._candidate_review_markup(15)

    assert [button.text for button in markup.keyboard[0]] == ["Да", "Нет"]
    assert [button.callback_data for button in markup.keyboard[0]] == [
        "confession_candidate:yes:15",
        "confession_candidate:no:15",
    ]


def test_confession_channel_actions_include_ad_blackouts() -> None:
    markup = build_confession_channel_actions(15)
    callback_data = [button.callback_data for row in markup.keyboard for button in row]

    assert "confession_channel:add_ad_blackout:15" in callback_data
    assert "confession_channel:delete_ad_blackout:15" in callback_data


def _channel_post(text: str, *, username: str | None = "confessions") -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=-1001234567890, type="channel", username=username),
        message_id=77,
        date=1_787_760_000,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        reply_markup=None,
    )


@pytest.mark.asyncio
async def test_confession_channel_own_link_does_not_create_blackout() -> None:
    runtime = object.__new__(ConfessionPublisherRuntime)
    runtime.publication_guard = SimpleNamespace(
        ensure_automatic_ad_blackout_for_channel_post=AsyncMock()
    )

    result = await runtime._ensure_external_link_blackout(
        _channel_post("Подпись: https://t.me/confessions")
    )

    assert result is None
    runtime.publication_guard.ensure_automatic_ad_blackout_for_channel_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_confession_channel_manual_exclusion_does_not_create_blackout(monkeypatch) -> None:
    runtime = object.__new__(ConfessionPublisherRuntime)
    runtime.publication_guard = SimpleNamespace(
        ensure_automatic_ad_blackout_for_channel_post=AsyncMock()
    )
    runtime.ad_link_exclusion_service = SimpleNamespace(
        list_normalized_links=AsyncMock(return_value={"advertiser.example"})
    )

    @asynccontextmanager
    async def fake_session_factory():
        yield SimpleNamespace()

    monkeypatch.setattr("src.confession_publisher.session_factory", fake_session_factory)

    result = await runtime._ensure_external_link_blackout(
        _channel_post("https://advertiser.example")
    )

    assert result is None
    runtime.ad_link_exclusion_service.list_normalized_links.assert_awaited_once()
    runtime.publication_guard.ensure_automatic_ad_blackout_for_channel_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_confession_channel_external_link_creates_one_hour_blackout() -> None:
    runtime = object.__new__(ConfessionPublisherRuntime)
    blackout = SimpleNamespace(id=5)
    runtime.publication_guard = SimpleNamespace(
        ensure_automatic_ad_blackout_for_channel_post=AsyncMock(return_value=blackout)
    )

    result = await runtime._ensure_external_link_blackout(
        _channel_post("https://t.me/confessions https://advertiser.example")
    )

    assert result is blackout
    runtime.publication_guard.ensure_automatic_ad_blackout_for_channel_post.assert_awaited_once_with(
        tg_channel_id=-1001234567890,
        telegram_message_id=77,
        published_at=datetime.fromtimestamp(1_787_760_000, tz=timezone.utc),
    )


@pytest.mark.asyncio
async def test_confession_paste_is_published_with_copy_message() -> None:
    paste = _paste(2, ContentFamily.CONFESSION.value)
    paste.storage_chat_id = -100500
    paste.storage_message_id = 77
    session = SimpleNamespace(get=AsyncMock(return_value=paste))
    adapter = SimpleNamespace(copy_message=AsyncMock(return_value=901))
    service = PublisherService(telegram_adapter=adapter)
    item = SimpleNamespace(origin_paste_id=paste.id)
    channel = SimpleNamespace(tg_channel_id=-100700)

    message_id = await service._publish_submission_based_item(
        session,
        item,
        channel,
        "123:publisher",
    )

    assert message_id == 901
    adapter.copy_message.assert_awaited_once_with(
        bot_token="123:publisher",
        channel_id=-100700,
        from_chat_id=-100500,
        message_id=77,
    )


@pytest.mark.asyncio
async def test_storage_message_becomes_telegram_copy_paste() -> None:
    publisher = SimpleNamespace(id=3, is_active=True, storage_chat_id=-100500)
    session = SimpleNamespace(
        get=AsyncMock(return_value=publisher),
        scalar=AsyncMock(return_value=None),
        add=Mock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    paste, created = await ConfessionService().create_storage_paste(
        session,
        publisher_id=3,
        storage_chat_id=-100500,
        storage_message_id=77,
        content_type="sticker",
        body_text="💘",
        created_by=42,
    )

    assert created is True
    assert paste.content_family == ContentFamily.CONFESSION.value
    assert paste.delivery_mode == PasteDeliveryMode.TELEGRAM_COPY.value
    assert paste.storage_chat_id == -100500
    assert paste.storage_message_id == 77
    session.add.assert_called_once_with(paste)


@pytest.mark.asyncio
async def test_storage_message_stays_candidate_until_confirmation() -> None:
    publisher = SimpleNamespace(id=3, is_active=True, storage_chat_id=-100500)
    session = SimpleNamespace(
        get=AsyncMock(return_value=publisher),
        scalar=AsyncMock(side_effect=[None, None]),
        add=Mock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    candidate, created = await ConfessionService().create_candidate(
        session,
        publisher_id=3,
        storage_chat_id=-100500,
        storage_message_id=78,
        content_type="text",
        body_text="Признание",
        submitted_by=42,
    )

    assert created is True
    assert isinstance(candidate, ConfessionPasteCandidate)
    assert candidate.status == ConfessionService.CANDIDATE_PENDING
    assert candidate.storage_message_id == 78
    added_object = session.add.call_args.args[0]
    assert isinstance(added_object, ConfessionPasteCandidate)
    assert not isinstance(added_object, PasteLibrary)


@pytest.mark.asyncio
async def test_approving_candidate_creates_active_paste() -> None:
    candidate = ConfessionPasteCandidate(
        publisher_id=3,
        storage_chat_id=-100500,
        storage_message_id=79,
        content_type="sticker",
        body_text="💘",
        submitted_by=42,
        status=ConfessionService.CANDIDATE_PENDING,
    )
    candidate.id = 11
    publisher = SimpleNamespace(id=3, is_active=True, storage_chat_id=-100500)
    session = SimpleNamespace(
        get=AsyncMock(return_value=publisher),
        scalar=AsyncMock(side_effect=[candidate, None]),
        add=Mock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    async def assign_paste_id() -> None:
        session.add.call_args.args[0].id = 501

    session.flush.side_effect = assign_paste_id

    paste, created = await ConfessionService().approve_candidate(
        session,
        candidate_id=11,
        reviewed_by=99,
    )

    assert created is True
    assert paste.id == 501
    assert paste.status == PasteStatus.ACTIVE
    assert paste.delivery_mode == PasteDeliveryMode.TELEGRAM_COPY.value
    assert candidate.status == ConfessionService.CANDIDATE_APPROVED
    assert candidate.paste_id == 501
    assert candidate.reviewed_by == 99


@pytest.mark.asyncio
async def test_rejecting_candidate_does_not_create_paste() -> None:
    candidate = ConfessionPasteCandidate(
        publisher_id=3,
        storage_chat_id=-100500,
        storage_message_id=80,
        content_type="text",
        body_text="Не добавлять",
        submitted_by=42,
        status=ConfessionService.CANDIDATE_PENDING,
    )
    candidate.id = 12
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=candidate),
        add=Mock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    result, changed = await ConfessionService().reject_candidate(
        session,
        candidate_id=12,
        reviewed_by=99,
    )

    assert changed is True
    assert result.status == ConfessionService.CANDIDATE_REJECTED
    assert result.paste_id is None
    session.add.assert_not_called()
