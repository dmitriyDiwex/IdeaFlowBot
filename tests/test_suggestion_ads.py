import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException
from telebot.types import CallbackQuery, Message, MessageEntity, User

from src.editorial.db.base import EditorialBase
from src.editorial.models.suggestion_ad import SuggestionAdCounter, SuggestionAdExclusion, SuggestionAdSettings
from src.editorial.services.suggestion_ad_service import (
    SuggestionAdService,
    normalize_suggestion_channel_tag,
    suggestion_ad_html,
)
from src.editorial.services.telegram_actions import TelegramEditorialActions
from src.master import MasterBot
from src.panel_markups import (
    build_extra_panel,
    build_suggestion_ad_exclusions_panel,
    build_suggestion_ads_panel,
)
from src.worker import SubBot


AD = '<b>Реклама</b> <tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>'


@pytest.fixture
async def ad_sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ads.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda connection: EditorialBase.metadata.create_all(
            connection,
            tables=[SuggestionAdSettings.__table__, SuggestionAdExclusion.__table__, SuggestionAdCounter.__table__],
        ))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _record(factory, *, user=123, channel=-1001, tag="@Overheard"):
    # New session/service on every submission simulates bot restarts.
    async with factory() as session:
        return await SuggestionAdService().record_submission(
            session, channel_tg_id=channel, channel_tag=tag, user_id=user,
        )


@pytest.mark.asyncio
async def test_ads_at_first_sixth_eleventh_with_persistent_independent_counters(ad_sessions):
    async with ad_sessions() as session:
        await SuggestionAdService().set_text(session, text_html=AD)
    results = [await _record(ad_sessions) for _ in range(12)]
    assert [index for index, ad in enumerate(results, start=1) if ad] == [1, 6, 11]
    assert all(ad == AD for ad in results if ad)
    assert await _record(ad_sessions, user=456) == AD
    assert await _record(ad_sessions, channel=-1002, tag="@Confessions") == AD
    async with ad_sessions() as session:
        assert await session.scalar(select(SuggestionAdCounter.submission_count).where(
            SuggestionAdCounter.channel_tg_id == -1001, SuggestionAdCounter.user_id == 123,
        )) == 12


@pytest.mark.asyncio
async def test_concurrent_submissions_do_not_lose_counter_updates(ad_sessions):
    async with ad_sessions() as session:
        await SuggestionAdService().set_text(session, text_html=AD)
    results = await asyncio.gather(*[_record(ad_sessions) for _ in range(11)])
    assert results.count(AD) == 3
    async with ad_sessions() as session:
        assert await session.scalar(select(SuggestionAdCounter.submission_count)) == 11


@pytest.mark.asyncio
async def test_ad_text_change_preserves_cadence(ad_sessions):
    service = SuggestionAdService()
    async with ad_sessions() as session:
        await service.set_text(session, text_html=AD)
    assert await _record(ad_sessions) == AD
    async with ad_sessions() as session:
        await service.set_text(session, text_html="<i>Новая реклама</i>")
    assert [await _record(ad_sessions) for _ in range(4)] == [None] * 4
    assert await _record(ad_sessions) == "<i>Новая реклама</i>"


@pytest.mark.asyncio
async def test_no_text_means_no_ad_and_no_counter(ad_sessions):
    assert await _record(ad_sessions) is None
    async with ad_sessions() as session:
        assert await session.scalar(select(SuggestionAdCounter.user_id)) is None
        await SuggestionAdService().set_text(session, text_html=AD)
    assert await _record(ad_sessions) == AD


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["@Overheard", "@Confessions"])
async def test_exclusions_add_list_delete_and_suppress_all_ads(ad_sessions, tag):
    service = SuggestionAdService()
    async with ad_sessions() as session:
        await service.set_text(session, text_html=AD)
        assert await service.add_exclusion(session, channel_tag=tag) == (tag[1:].lower(), True)
        assert await service.add_exclusion(session, channel_tag=tag.upper()) == (tag[1:].lower(), False)
        assert await service.list_exclusions(session) == [tag[1:].lower()]
    assert [await _record(ad_sessions, tag=tag) for _ in range(11)] == [None] * 11
    async with ad_sessions() as session:
        assert await session.scalar(select(SuggestionAdCounter.user_id)) is None
        assert await service.delete_exclusion(session, channel_tag=tag.upper()) == tag[1:].lower()
        assert await service.list_exclusions(session) == []
        with pytest.raises(ValueError, match="нет в списке"):
            await service.delete_exclusion(session, channel_tag=tag)
    assert await _record(ad_sessions, tag=tag) == AD


@pytest.mark.parametrize("tag", ["", "@bad tag", "@bad-tag", "https://t.me/channel", "@12345", "@" + "x" * 33])
def test_exclusion_rejects_invalid_tags(tag):
    with pytest.raises(ValueError):
        normalize_suggestion_channel_tag(tag)


def test_html_input_preserves_literal_tg_emoji_even_with_url_entities():
    message = SimpleNamespace(
        text=AD + " https://t.me/channel",
        entities=[MessageEntity(type="url", offset=len(AD) + 1, length=20)],
    )
    assert suggestion_ad_html(message) == message.text


def test_formatted_input_preserves_utf16_custom_emoji_and_nested_formatting():
    message = SimpleNamespace(
        text="  🔥Текст & ссылка",
        entities=[
            MessageEntity(type="custom_emoji", offset=2, length=2, custom_emoji_id="12345"),
            MessageEntity(type="bold", offset=2, length=7),
        ],
    )
    assert suggestion_ad_html(message) == '<b><tg-emoji emoji-id="12345">🔥</tg-emoji>Текст</b> &amp; ссылка'


def test_plain_text_is_escaped_and_media_caption_is_not_accepted():
    assert suggestion_ad_html(SimpleNamespace(text="A < B & C")) == "A &lt; B &amp; C"
    assert suggestion_ad_html(SimpleNamespace(text="A &lt; B &amp; C")) == "A &lt; B &amp; C"
    with pytest.raises(ValueError):
        suggestion_ad_html(SimpleNamespace(text=None, caption="Photo caption"))


def test_panel_navigation_contains_all_requested_buttons():
    extra = [button for row in build_extra_panel().keyboard for button in row]
    assert any(button.text == "Настройка рекламы предложек" and button.callback_data == "panel:suggestion_ads"
               for button in extra)
    assert [button.text for row in build_suggestion_ads_panel().keyboard for button in row] == [
        "Установить текст рекламы", "Настройка исключений", "Назад",
    ]
    assert [button.text for row in build_suggestion_ad_exclusions_panel().keyboard for button in row] == [
        "Добавить паблик в исключения", "Удалить из исключённых", "Назад",
    ]


def _master():
    master = MasterBot.__new__(MasterBot)
    master.user_states = {}
    master.main_bot = SimpleNamespace(send_message=AsyncMock())
    master.editorial_actions = SimpleNamespace(
        get_suggestion_ad_text=AsyncMock(return_value=AD),
        set_suggestion_ad_text=AsyncMock(),
        list_suggestion_ad_exclusions=AsyncMock(return_value=["overheard", "confessions"]),
        add_suggestion_ad_exclusion=AsyncMock(return_value=("overheard", True)),
        delete_suggestion_ad_exclusion=AsyncMock(return_value="overheard"),
    )
    return master


@pytest.mark.asyncio
async def test_settings_screens_display_html_and_excluded_public_tags():
    master = _master()
    await master._show_suggestion_ads_panel(123)
    assert AD in master.main_bot.send_message.await_args.kwargs["text"]
    await master._show_suggestion_ad_exclusions_panel(123)
    text = master.main_bot.send_message.await_args.kwargs["text"]
    assert "@overheard" in text and "@confessions" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(("callback", "state"), [
    ("suggestion_ad_text", "await_suggestion_ad_text"),
    ("add_suggestion_ad_exclusion", "await_add_suggestion_ad_exclusion"),
    ("delete_suggestion_ad_exclusion", "await_delete_suggestion_ad_exclusion"),
])
async def test_callbacks_prompt_for_text_or_tag_and_back_cancels_input(callback, state):
    master = _master()
    await master._handle_suggestion_ad_callback(callback, 123)
    assert master.user_states[123]["action"] == state
    await master._handle_suggestion_ad_callback("suggestion_ads", 123)
    assert 123 not in master.user_states


@pytest.mark.asyncio
async def test_text_is_previewed_as_html_before_saving():
    master = _master()
    master._set_user_state(123, "await_suggestion_ad_text")
    message = SimpleNamespace(text=AD, caption=None, entities=None, chat=SimpleNamespace(id=123))
    assert await master._handle_stateful_admin_text(message)
    master.editorial_actions.set_suggestion_ad_text.assert_awaited_once_with(text_html=AD)
    assert master.main_bot.send_message.await_args_list[0].kwargs == {
        "chat_id": 123, "text": AD, "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    assert 123 not in master.user_states


@pytest.mark.asyncio
async def test_invalid_html_keeps_previous_text_and_input_state():
    master = _master()
    master._set_user_state(123, "await_suggestion_ad_text")
    master.main_bot.send_message.side_effect = [
        ApiTelegramException("sendMessage", None, {"error_code": 400, "description": "Bad Request: can't parse entities"}),
        None,
    ]
    message = SimpleNamespace(text="<b>broken", caption=None, entities=None, chat=SimpleNamespace(id=123))
    assert await master._handle_stateful_admin_text(message)
    master.editorial_actions.set_suggestion_ad_text.assert_not_awaited()
    assert master.user_states[123]["action"] == "await_suggestion_ad_text"


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "method"), [
    ("await_add_suggestion_ad_exclusion", "add_suggestion_ad_exclusion"),
    ("await_delete_suggestion_ad_exclusion", "delete_suggestion_ad_exclusion"),
])
async def test_admin_entered_tag_updates_exclusions(state, method):
    master = _master()
    master._set_user_state(123, state)
    message = SimpleNamespace(text="@Overheard", caption=None, chat=SimpleNamespace(id=123))
    assert await master._handle_stateful_admin_text(message)
    getattr(master.editorial_actions, method).assert_awaited_once_with(channel_tag="@Overheard")
    assert 123 not in master.user_states


@pytest.mark.asyncio
async def test_single_submission_sends_ad_only_after_successful_review():
    subbot = SubBot.__new__(SubBot)
    message = SimpleNamespace(chat=SimpleNamespace(id=123))
    review = SimpleNamespace(message_id=11)
    subbot._send_review_message_to_legacy_chat = AsyncMock(return_value=review)
    subbot._save_incoming_message = AsyncMock()
    subbot._notify_new_submission = AsyncMock()
    subbot._send_suggestion_ad = AsyncMock()
    await subbot._process_single_submission(message)
    subbot._save_incoming_message.assert_awaited_once_with(message, review)
    subbot._send_suggestion_ad.assert_awaited_once_with(message)
    subbot._send_suggestion_ad.reset_mock()
    subbot._send_review_message_to_legacy_chat.return_value = None
    await subbot._process_single_submission(message)
    subbot._send_suggestion_ad.assert_not_awaited()


@pytest.mark.asyncio
async def test_subbot_sends_custom_emoji_html_and_handles_delivery_failure(ad_sessions, monkeypatch):
    monkeypatch.setattr("src.worker.session_factory", ad_sessions)
    async with ad_sessions() as session:
        await SuggestionAdService().set_text(session, text_html=AD)
    subbot = SubBot.__new__(SubBot)
    subbot.channel_id = -1001
    subbot.channel_username = "@Overheard"
    subbot.suggestion_ad_service = SuggestionAdService()
    subbot.sup_bot = SimpleNamespace(send_message=AsyncMock())
    message = SimpleNamespace(chat=SimpleNamespace(id=123))
    await subbot._send_suggestion_ad(message)
    subbot.sup_bot.send_message.assert_awaited_once_with(
        chat_id=123, text=AD, parse_mode="HTML", disable_web_page_preview=True,
    )
    subbot.sup_bot.send_message.reset_mock()
    await subbot._send_suggestion_ad(message)
    subbot.sup_bot.send_message.assert_not_awaited()
    subbot.sup_bot.send_message.side_effect = RuntimeError("Telegram unavailable")
    # A different user is due their first ad; a failed ad does not escape into collection.
    await subbot._send_suggestion_ad(SimpleNamespace(chat=SimpleNamespace(id=456)))


@pytest.mark.parametrize(("text", "entities", "expected"), [
    (
        "🔥Текст & ссылка",
        [MessageEntity(type="url", offset=10, length=6),
         MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="12345"),
         MessageEntity(type="bold", offset=0, length=16),
         MessageEntity(type="italic", offset=2, length=5)],
        '<b><tg-emoji emoji-id="12345">🔥</tg-emoji><i>Текст</i> &amp; ссылка</b>',
    ),
    (
        "🔥 & текст",
        [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="12345"),
         MessageEntity(type="bold", offset=0, length=2)],
        '<b><tg-emoji emoji-id="12345">🔥</tg-emoji></b> &amp; текст',
    ),
    (
        "Ссылка и текст",
        [MessageEntity(type="text_link", offset=0, length=6, url='https://example.com/?a=1&b="2"'),
         MessageEntity(type="bold", offset=0, length=6),
         MessageEntity(type="spoiler", offset=9, length=5)],
        '<a href="https://example.com/?a=1&amp;b=&quot;2&quot;"><b>Ссылка</b></a> и <span class="tg-spoiler">текст</span>',
    ),
    (
        "A < B & C",
        [MessageEntity(type="pre", offset=0, length=9, language="python")],
        '<pre><code class="language-python">A &lt; B &amp; C</code></pre>',
    ),
    (
        "Иван: цитата",
        [MessageEntity(type="text_mention", offset=0, length=4,
                       user=User(id=123, is_bot=False, first_name="Иван")),
         MessageEntity(type="expandable_blockquote", offset=6, length=6)],
        '<a href="tg://user?id=123">Иван</a>: <blockquote expandable>цитата</blockquote>',
    ),
])
def test_formatted_ad_preserves_nested_and_adjacent_entities(text, entities, expected):
    assert suggestion_ad_html(SimpleNamespace(text=text, entities=entities)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "entities", "expected"), [
    ("Новая реклама & текст", [], "Новая реклама &amp; текст"),
    ("<i>Новая реклама</i>", [], "<i>Новая реклама</i>"),
    (
        "🔥Новая реклама https://t.me/channel",
        [MessageEntity(type="bold", offset=0, length=15),
         MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="12345"),
         MessageEntity(type="url", offset=16, length=20)],
        '<b><tg-emoji emoji-id="12345">🔥</tg-emoji>Новая реклама</b> https://t.me/channel',
    ),
])
async def test_panel_handlers_replace_persisted_ad_text(ad_sessions, monkeypatch, text, entities, expected):
    monkeypatch.setattr("src.master.settings.general_admin", 123)
    monkeypatch.setattr("src.editorial.services.telegram_actions.session_factory", ad_sessions)
    service = SuggestionAdService()
    async with ad_sessions() as session:
        await service.set_text(session, text_html=AD)
    assert await _record(ad_sessions) == AD

    master = _master()
    master.editorial_actions = TelegramEditorialActions.__new__(TelegramEditorialActions)
    master.editorial_actions.suggestion_ad_service = service
    master.main_bot = AsyncTeleBot("1:test")
    master.main_bot.send_message = AsyncMock()
    master.main_bot.answer_callback_query = AsyncMock()
    master._MasterBot__setup_handlers()
    payload = {
        "message_id": 1, "date": 0, "chat": {"id": 123, "type": "private"},
        "from": {"id": 123, "is_bot": False, "first_name": "Admin"},
        "text": "Настройка рекламы",
    }
    callback = CallbackQuery.de_json({
        "id": "ad-text", "from": payload["from"], "chat_instance": "123",
        "message": payload, "data": "panel:suggestion_ad_text",
    })
    await master.main_bot.process_new_callback_query([callback])
    assert master.user_states[123]["action"] == "await_suggestion_ad_text"
    master.main_bot.send_message.reset_mock()

    payload.update(text=text, entities=[entity.to_dict() for entity in entities])
    await master.main_bot.process_new_messages([Message.de_json(payload)])
    assert master.main_bot.send_message.await_args_list[0].kwargs == {
        "chat_id": 123, "text": expected, "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    async with ad_sessions() as session:
        assert await service.get_text(session) == expected
    assert 123 not in master.user_states
    assert [await _record(ad_sessions) for _ in range(4)] == [None] * 4
    assert await _record(ad_sessions) == expected
