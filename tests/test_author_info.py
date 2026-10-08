from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telebot.async_telebot import AsyncTeleBot
from telebot.types import CallbackQuery

from src.markups import MarkupButton, build_slot_status_markup
from src.utils import Utils
from src.worker import SubBot


def callback(data="add_info;1001"):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=-100123),
            message_id=4321,
            content_type="text",
            text="Suggestion",
        ),
    )


def bot_with_profile(username=None, first_name="Author"):
    return SimpleNamespace(
        token="1:test",
        get_chat=AsyncMock(return_value=SimpleNamespace(
            id=1001, username=username, first_name=first_name,
        )),
        send_message=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        copy_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("username,expected", [
    (None, "@None"), ("", "@None"), ("None", "@None"),
    ("author_name", "@author_name"),
])
async def test_author_info_shows_tag_and_id_without_requiring_username(username, expected):
    bot = bot_with_profile(username, "Имя <&> [test] `name`")
    await MarkupButton(bot).add_info(callback())

    bot.get_chat.assert_awaited_once_with(1001)
    sent = bot.send_message.await_args.kwargs
    assert sent["chat_id"] == -100123
    assert sent["parse_mode"] == "HTML"
    assert "TG ID: <code>1001</code>" in sent["text"]
    assert f"username: {expected}" in sent["text"]
    assert "Имя &lt;&amp;&gt; [test] `name`" in sent["text"]


@pytest.mark.asyncio
async def test_author_info_keeps_saved_id_when_telegram_profile_is_unavailable():
    bot = bot_with_profile()
    bot.get_chat.side_effect = TimeoutError("timed out")
    await MarkupButton(bot).add_info(callback())

    sent = bot.send_message.await_args.kwargs
    assert "TG ID: <code>1001</code>" in sent["text"]
    assert "username: недоступен" in sent["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["add_info;0", "add_info;None", "add_info", "add_info;-1"])
async def test_author_info_explains_missing_id_in_old_cards(data):
    bot = bot_with_profile()
    await MarkupButton(bot).add_info(callback(data))
    bot.get_chat.assert_not_awaited()
    assert "не сохранён Telegram ID" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["incoming", "approved", "rejected", "delayed_published", "slot_approved", "slot_published"])
async def test_author_without_tag_remains_clickable_after_moderation(state, monkeypatch):
    bot = bot_with_profile()
    buttons = MarkupButton(bot)
    if state == "incoming":
        markup = await buttons.get_main_menu_markup(1001)
    elif state == "approved":
        monkeypatch.setattr("src.markups.should_add_publication_signature", AsyncMock(return_value=False))
        await buttons.send_suggest(callback("send_suggest;1001"), "@channel", -100987, False)
        markup = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    elif state == "rejected":
        await buttons.reject_post(callback("reject;1001"), moderator_id=42)
        markup = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    elif state == "delayed_published":
        await buttons.push_post_button(-100123, 4321, sender_id=1001)
        markup = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    else:
        markup = build_slot_status_markup(
            sender_id=1001, sender_username=None, sender_first_name="Author",
            moderator_id=42, moderator_username=None, moderator_first_name="Admin",
            state="approved" if state == "slot_approved" else "published",
        )

    author_button = next(
        button for row in markup.keyboard for button in row
        if button.callback_data == "add_info;1001"
    )
    await buttons.add_info(callback(author_button.callback_data))
    sent = bot.send_message.await_args.kwargs["text"]
    assert "username: @None" in sent
    assert "TG ID: <code>1001</code>" in sent


@pytest.mark.asyncio
async def test_suggestion_bot_dispatches_none_tag_button_and_answers_callback(monkeypatch):
    subbot = SubBot.__new__(SubBot)
    subbot.sup_bot = AsyncTeleBot("1:test")
    subbot.sup_bot.get_chat = AsyncMock(return_value=SimpleNamespace(
        id=1001, username=None, first_name="Author",
    ))
    subbot.sup_bot.send_message = AsyncMock()
    subbot.sup_bot.answer_callback_query = AsyncMock()
    monkeypatch.setattr(Utils, "save_admin_action", AsyncMock())
    await subbot._SubBot__setup_handlers()
    call = CallbackQuery.de_json({
        "id": "click-author", "chat_instance": "42", "data": "add_info;1001",
        "from": {"id": 42, "is_bot": False, "first_name": "Admin"},
        "message": {
            "message_id": 4321, "date": 0,
            "chat": {"id": -100123, "type": "supergroup"},
            "from": {"id": 99, "is_bot": True, "first_name": "Bot"},
            "text": "Suggestion",
        },
    })
    await subbot.sup_bot.process_new_callback_query([call])

    subbot.sup_bot.answer_callback_query.assert_awaited_once()
    subbot.sup_bot.send_message.assert_awaited_once()
    text = subbot.sup_bot.send_message.await_args.kwargs["text"]
    assert "username: @None" in text
    assert "TG ID: <code>1001</code>" in text
