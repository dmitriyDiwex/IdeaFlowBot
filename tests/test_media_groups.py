import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.markups import MarkupButton
from src.master import MasterBot
from src.editorial.services.publisher import PublisherService
from src.legacy_media_groups import LegacyMediaGroupReference
from src.worker import SubBot


def _message(message_id: int, *, caption: str | None = None):
    return SimpleNamespace(
        message_id=message_id,
        id=message_id,
        caption=caption,
        media_group_id="album-1",
        chat=SimpleNamespace(id=1001),
    )


@pytest.mark.asyncio
async def test_legacy_review_sends_one_control_card_for_the_whole_album() -> None:
    copied_messages = [SimpleNamespace(message_id=501), SimpleNamespace(message_id=502)]
    control_message = SimpleNamespace(message_id=503)
    bot = SimpleNamespace(
        copy_messages=AsyncMock(return_value=copied_messages),
        send_message=AsyncMock(return_value=control_message),
        get_chat=AsyncMock(return_value=SimpleNamespace(username="author")),
    )
    subbot = SubBot.__new__(SubBot)
    subbot.sup_bot = bot
    subbot.chat_suggest = -10055
    subbot.channel_id = -10077
    subbot.channel_username = "@channel"
    subbot.channel_title = "Channel"
    subbot.bot_info = SimpleNamespace(username="suggest_bot")
    subbot.callback_new_submission = AsyncMock()
    subbot._save_incoming_message = AsyncMock()

    messages = [_message(12), _message(11, caption="Album caption")]
    await subbot._send_media_group_to_legacy_chat(messages)

    bot.copy_messages.assert_awaited_once_with(
        chat_id=-10055,
        from_chat_id=1001,
        message_ids=[11, 12],
    )
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == -10055
    assert "Действия ниже применяются ко всему альбому" in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["reply_markup"] is not None
    assert subbot._save_incoming_message.await_args_list == [
        ((messages[1], control_message), {}),
        ((messages[0], control_message), {}),
    ]
    subbot.callback_new_submission.assert_awaited_once_with(
        channel_tg_id=-10077,
        review_chat_id=-10055,
        review_message_id=503,
    )


@pytest.mark.asyncio
async def test_media_group_queue_debounces_until_all_items_arrive() -> None:
    subbot = SubBot.__new__(SubBot)
    subbot.MEDIA_GROUP_SETTLE_SECONDS = 0.01
    subbot._media_group_messages = {}
    subbot._media_group_flush_tasks = {}
    subbot._media_group_lock = asyncio.Lock()
    subbot._send_media_group_to_legacy_chat = AsyncMock()
    subbot.bot_info = SimpleNamespace(username="suggest_bot")
    subbot.chat_suggest = -10055

    first = _message(11)
    second = _message(12)
    await subbot._queue_media_group(first)
    await subbot._queue_media_group(second)
    await subbot._media_group_flush_tasks[(1001, "album-1")]

    subbot._send_media_group_to_legacy_chat.assert_awaited_once_with([first, second])
    assert subbot._media_group_messages == {}
    assert subbot._media_group_flush_tasks == {}


@pytest.mark.asyncio
async def test_legacy_approval_publishes_album_with_copy_messages(monkeypatch) -> None:
    bot = SimpleNamespace(
        token="1:test",
        get_chat=AsyncMock(return_value=SimpleNamespace(id=1001, username="author")),
        copy_messages=AsyncMock(
            return_value=[SimpleNamespace(message_id=701), SimpleNamespace(message_id=702)]
        ),
        copy_message=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        send_message=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.markups.should_add_publication_signature",
        AsyncMock(return_value=False),
    )
    call = SimpleNamespace(
        data="send_suggest;1001",
        message=SimpleNamespace(
            message_id=503,
            chat=SimpleNamespace(id=-10055),
            content_type="text",
            text="Media group controls",
            caption=None,
        ),
    )
    media_group = LegacyMediaGroupReference(
        source_chat_id=1001,
        source_message_ids=[11, 12],
        caption="Album caption",
        caption_index=0,
    )

    sent = await MarkupButton(bot).send_suggest(
        call,
        "@channel",
        -10077,
        False,
        "Channel",
        media_group=media_group,
    )

    assert sent == 701
    bot.copy_messages.assert_awaited_once_with(
        chat_id=-10077,
        from_chat_id=1001,
        message_ids=[11, 12],
    )
    bot.copy_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_album_is_not_retried_after_post_copy_caption_failure(monkeypatch) -> None:
    bot = SimpleNamespace(
        token="1:test",
        get_chat=AsyncMock(return_value=SimpleNamespace(id=1001, username="author")),
        copy_messages=AsyncMock(
            return_value=[SimpleNamespace(message_id=701), SimpleNamespace(message_id=702)]
        ),
        copy_message=AsyncMock(),
        edit_message_caption=AsyncMock(side_effect=TimeoutError("timed out")),
        edit_message_reply_markup=AsyncMock(),
        send_message=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.markups.should_add_publication_signature",
        AsyncMock(return_value=True),
    )
    call = SimpleNamespace(
        data="send_suggest;1001",
        message=SimpleNamespace(
            message_id=503,
            chat=SimpleNamespace(id=-10055),
            content_type="text",
            text="Media group controls",
            caption=None,
        ),
    )
    media_group = LegacyMediaGroupReference(
        source_chat_id=1001,
        source_message_ids=[11, 12],
        caption="Album caption",
        caption_index=0,
    )

    sent = await MarkupButton(bot).send_suggest(
        call,
        "@channel",
        -10077,
        False,
        "Channel",
        media_group=media_group,
    )

    assert sent == 701
    bot.copy_messages.assert_awaited_once()
    bot.edit_message_caption.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_delayed_publication_copies_album_from_original_messages(monkeypatch) -> None:
    media_group = LegacyMediaGroupReference(
        source_chat_id=1001,
        source_message_ids=[11, 12],
        caption="Album caption",
        caption_index=0,
    )
    copied_messages = [SimpleNamespace(message_id=701), SimpleNamespace(message_id=702)]
    bot = SimpleNamespace(
        token="1:test",
        copy_messages=AsyncMock(return_value=copied_messages),
        copy_message=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        get_chat=AsyncMock(return_value=SimpleNamespace(username="author")),
    )
    subbot = SubBot.__new__(SubBot)
    subbot.sup_bot = bot
    subbot.channel_id = -10077
    subbot.chat_suggest = -10055
    subbot.channel_username = "@channel"
    subbot.channel_title = "Channel"
    subbot.channel_signature_ref = "@channel"
    subbot.delayed_message = {503: [100, 1001]}
    subbot.anonym_send = set()
    subbot._get_review_media_group = AsyncMock(return_value=media_group)
    subbot.legacy_moderation_sync = SimpleNamespace(
        mark_legacy_delayed_published=AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "src.worker.should_add_publication_signature",
        AsyncMock(return_value=False),
    )

    sent = await subbot.send_delayed_message(503, 1001)

    assert sent is True
    bot.copy_messages.assert_awaited_once_with(
        from_chat_id=1001,
        chat_id=-10077,
        message_ids=[11, 12],
    )
    bot.copy_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_delayed_album_is_finalized_after_caption_update_failure(monkeypatch) -> None:
    media_group = LegacyMediaGroupReference(
        source_chat_id=1001,
        source_message_ids=[11, 12],
        caption="Album caption",
        caption_index=0,
    )
    copied_messages = [SimpleNamespace(message_id=701), SimpleNamespace(message_id=702)]
    bot = SimpleNamespace(
        token="1:test",
        copy_messages=AsyncMock(return_value=copied_messages),
        copy_message=AsyncMock(),
        edit_message_caption=AsyncMock(side_effect=TimeoutError("timed out")),
        edit_message_reply_markup=AsyncMock(),
        get_chat=AsyncMock(return_value=SimpleNamespace(username="author")),
    )
    subbot = SubBot.__new__(SubBot)
    subbot.sup_bot = bot
    subbot.channel_id = -10077
    subbot.chat_suggest = -10055
    subbot.channel_username = "@channel"
    subbot.channel_title = "Channel"
    subbot.channel_signature_ref = "@channel"
    subbot.delayed_message = {503: [100, 1001]}
    subbot.anonym_send = set()
    subbot._get_review_media_group = AsyncMock(return_value=media_group)
    subbot.legacy_moderation_sync = SimpleNamespace(
        mark_legacy_delayed_published=AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "src.worker.should_add_publication_signature",
        AsyncMock(return_value=True),
    )

    sent = await subbot.send_delayed_message(503, 1001)

    assert sent is True
    assert 503 not in subbot.delayed_message
    bot.copy_messages.assert_awaited_once()
    subbot.legacy_moderation_sync.mark_legacy_delayed_published.assert_awaited_once_with(
        channel_tg_id=-10077,
        review_chat_id=-10055,
        review_message_id=503,
        telegram_message_id=701,
    )


@pytest.mark.asyncio
async def test_slot_publisher_uses_original_album_when_review_rows_share_control_card() -> None:
    submission = SimpleNamespace(
        id=1,
        channel_id=7,
        media_group_id="album-1",
        source_chat_id=1001,
        source_message_id=11,
        cleaned_text=None,
        raw_text=None,
        is_anonymous=False,
        username="author",
    )
    caption_submission = SimpleNamespace(
        id=2,
        channel_id=7,
        media_group_id="album-1",
        source_chat_id=1001,
        source_message_id=12,
        cleaned_text="Album caption",
        raw_text="Album caption",
        is_anonymous=False,
        username="author",
    )
    related_rows = [submission, caption_submission]
    legacy_rows = [
        SimpleNamespace(review_chat_id=-10055, review_message_id=503),
        SimpleNamespace(review_chat_id=-10055, review_message_id=503),
    ]
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: related_rows))
    session = SimpleNamespace(
        get=AsyncMock(return_value=submission),
        execute=AsyncMock(return_value=result),
    )
    adapter = SimpleNamespace(
        copy_messages=AsyncMock(return_value=[701, 702]),
        edit_message_caption=AsyncMock(),
    )
    service = PublisherService(telegram_adapter=adapter)
    service.should_add_channel_signature = AsyncMock(return_value=False)
    service._get_related_legacy_rows = AsyncMock(return_value=legacy_rows)
    channel = SimpleNamespace(id=7, tg_channel_id=-10077, short_code="channel", title="Channel")
    content_item = SimpleNamespace(origin_submission_id=1)

    published_id = await service._publish_submission_based_item(
        session,
        content_item,
        channel,
        "1:test",
    )

    assert published_id == 701
    adapter.copy_messages.assert_awaited_once_with(
        bot_token="1:test",
        channel_id=-10077,
        from_chat_id=1001,
        message_ids=[11, 12],
    )
    adapter.edit_message_caption.assert_awaited_once()
    assert adapter.edit_message_caption.await_args.kwargs["message_id"] == 702


@pytest.mark.asyncio
async def test_slot_publisher_preserves_original_album_caption_when_formatted_caption_is_too_long() -> None:
    long_caption = "x" * 1100
    submission = SimpleNamespace(
        id=1,
        channel_id=7,
        media_group_id="album-1",
        source_chat_id=1001,
        source_message_id=11,
        cleaned_text=long_caption,
        raw_text=long_caption,
        is_anonymous=True,
        username=None,
    )
    related_rows = [submission]
    legacy_rows = [SimpleNamespace(review_chat_id=-10055, review_message_id=503)]
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: related_rows))
    session = SimpleNamespace(
        get=AsyncMock(return_value=submission),
        execute=AsyncMock(return_value=result),
    )
    adapter = SimpleNamespace(
        copy_messages=AsyncMock(return_value=[701]),
        edit_message_caption=AsyncMock(),
    )
    service = PublisherService(telegram_adapter=adapter)
    service.should_add_channel_signature = AsyncMock(return_value=True)
    service.resolve_channel_publication_signature = AsyncMock(
        return_value=SimpleNamespace(ref="@channel", title="Channel")
    )
    service._get_related_legacy_rows = AsyncMock(return_value=legacy_rows)
    channel = SimpleNamespace(id=7, tg_channel_id=-10077, short_code="channel", title="Channel")
    content_item = SimpleNamespace(id=42, origin_submission_id=1)

    published_id = await service._publish_submission_based_item(
        session,
        content_item,
        channel,
        "1:test",
    )

    assert published_id == 701
    adapter.copy_messages.assert_awaited_once()
    adapter.edit_message_caption.assert_not_awaited()


@pytest.mark.asyncio
async def test_slot_publisher_does_not_retry_album_after_caption_edit_failure() -> None:
    submission = SimpleNamespace(
        id=1,
        channel_id=7,
        media_group_id="album-1",
        source_chat_id=1001,
        source_message_id=11,
        cleaned_text="Album caption",
        raw_text="Album caption",
        is_anonymous=False,
        username="author",
    )
    related_rows = [submission]
    legacy_rows = [SimpleNamespace(review_chat_id=-10055, review_message_id=503)]
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: related_rows))
    session = SimpleNamespace(
        get=AsyncMock(return_value=submission),
        execute=AsyncMock(return_value=result),
    )
    adapter = SimpleNamespace(
        copy_messages=AsyncMock(return_value=[701]),
        edit_message_caption=AsyncMock(side_effect=RuntimeError("caption update failed")),
    )
    service = PublisherService(telegram_adapter=adapter)
    service.should_add_channel_signature = AsyncMock(return_value=False)
    service._get_related_legacy_rows = AsyncMock(return_value=legacy_rows)
    channel = SimpleNamespace(id=7, tg_channel_id=-10077, short_code="channel", title="Channel")
    content_item = SimpleNamespace(id=42, origin_submission_id=1)

    published_id = await service._publish_submission_based_item(
        session,
        content_item,
        channel,
        "1:test",
    )

    assert published_id == 701
    adapter.copy_messages.assert_awaited_once()
    adapter.edit_message_caption.assert_awaited_once()


@pytest.mark.asyncio
async def test_panel_album_preview_uses_one_media_group_fallback() -> None:
    preview = SimpleNamespace(
        media_group_id="album-1",
        preview_file_ids=["photo-file", "video-file"],
        preview_file_sizes=[100, 200],
        preview_content_types=["photo", "video"],
        content_type="photo",
        channel_tg_id=-10077,
    )
    master = MasterBot.__new__(MasterBot)
    master.legacy_reader = SimpleNamespace(
        get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="2:test"))
    )
    master.main_bot = SimpleNamespace(send_message=AsyncMock())
    master._send_binary_preview_media_group = AsyncMock()
    master._send_binary_preview_item = AsyncMock()

    sent = await master._send_submission_preview_fallback(1001, preview)

    assert sent is True
    master._send_binary_preview_media_group.assert_awaited_once_with(
        chat_id=1001,
        bot_token="2:test",
        file_ids=["photo-file", "video-file"],
        content_types=["photo", "video"],
        default_content_type="photo",
    )
    master._send_binary_preview_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_prefers_rebuilt_album_over_separate_legacy_copies() -> None:
    preview = SimpleNamespace(
        media_group_id="album-1",
        preview_file_ids=["one", "two"],
        review_message_ids=[501, 502],
        review_chat_id=-10055,
    )
    master = MasterBot.__new__(MasterBot)
    master.editorial_actions = SimpleNamespace(
        get_submission_preview=AsyncMock(return_value=preview)
    )
    master._send_submission_preview_fallback = AsyncMock(return_value=True)
    master.main_bot = SimpleNamespace(copy_message=AsyncMock())

    await master._send_submission_preview(1001, 42)

    master._send_submission_preview_fallback.assert_awaited_once_with(1001, preview)
    master.main_bot.copy_message.assert_not_awaited()
