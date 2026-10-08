import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine
from telebot.async_telebot import AsyncTeleBot
from telebot.types import CallbackQuery, Message

from src.core_database.database import CrudBannedUser
from src.core_database.models.banned_user import BannedUser
from src.core_database.models.db_helper import db_helper
from src.core_database.models.sender_info import SenderData
from src.editorial.services.user_bans import UserBanService
from src.master import MasterBot
from src.panel_markups import build_bans_panel, build_extra_panel
from src.utils import Utils
from src.worker import SubBot


@pytest.fixture
async def ban_database(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'bans.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(BannedUser.__table__.create)
        await conn.run_sync(SenderData.__table__.create)
    monkeypatch.setattr(db_helper, "engine", engine)
    yield CrudBannedUser()
    await engine.dispose()


async def sender(user_id, username, timestamp=1):
    async with db_helper.engine.begin() as conn:
        await conn.execute(insert(SenderData).values(
            user_id=user_id, username=username, timestamp=timestamp,
            channel_id=-1001, bot_username="suggest_bot", first_name="Author",
            message_id=timestamp, chat_id=user_id, text_post="Message", content_type="text",
        ))


async def test_global_ban_is_idempotent_persistent_and_applies_to_future_channels(ban_database):
    results = await asyncio.gather(*(ban_database.add_global_ban(123) for _ in range(8)))
    assert sum(results) == 1
    assert len(await ban_database.get_banned_users(id_user=123)) == 1
    for channel in (-1001, -1002, -1009999):
        # New instance represents another bot or a restarted process.
        assert await Utils().check_banned_user(123, channel)
        assert not await Utils().check_banned_user(456, channel)


async def test_local_ban_stays_local_and_global_unban_removes_all_user_bans(ban_database):
    await ban_database.add_banned_user({"id_user": 123, "id_channel": -1001, "bot_id": 1})
    await ban_database.add_banned_user({"id_user": 456, "id_channel": -1001, "bot_id": 1})
    assert await Utils().check_banned_user(123, -1001)
    assert not await Utils().check_banned_user(123, -1002)
    await ban_database.add_global_ban(123)
    await ban_database.add_banned_user({"id_user": 123, "id_channel": -1002, "bot_id": 2})
    assert await UserBanService().unban(123) == 3
    assert await UserBanService().unban(123) == 0
    assert await ban_database.get_banned_users(id_user=123) == []
    assert not await Utils().check_banned_user(123, -1001)
    assert await Utils().check_banned_user(456, -1001)


@pytest.mark.parametrize("value", ["@TeSt_User", "test_user", "123"])
async def test_resolve_username_case_insensitively_or_by_id(ban_database, value):
    await sender(123, "Test_User")
    target = await UserBanService().resolve_user(value)
    assert (target.user_id, target.username) == (123, "Test_User")


async def test_unknown_numeric_id_can_be_banned_without_prior_contact(ban_database):
    target = await UserBanService().resolve_user("999")
    assert target.label == "@None | TG ID: 999"
    assert await UserBanService().ban(target.user_id)
    assert await Utils().check_banned_user(999, -1001)


async def test_username_resolution_uses_latest_record_and_handles_reassignment(ban_database):
    await sender(123, "old_name", 1)
    await sender(123, "new_name", 2)
    await sender(456, "old_name", 3)
    assert (await UserBanService().resolve_user("@old_name")).user_id == 456
    assert (await UserBanService().resolve_user("@new_name")).user_id == 123
    await sender(456, "None", 4)
    with pytest.raises(ValueError, match="не найден"):
        await UserBanService().resolve_user("@old_name")


async def test_ambiguous_or_unknown_username_does_not_ban_anyone(ban_database):
    await sender(123, "shared_name")
    await sender(456, "SHARED_NAME")
    with pytest.raises(ValueError, match="несколько"):
        await UserBanService().resolve_user("@shared_name")
    with pytest.raises(ValueError, match="не найден"):
        await UserBanService().resolve_user("@unknown_name")
    assert await ban_database.get_banned_users() == []


@pytest.mark.parametrize("value", ["", "@", "@None", "0", "-100123", "9223372036854775808", "123 name", "https://t.me/person"])
async def test_invalid_identity_is_rejected_without_mutation(ban_database, value):
    with pytest.raises(ValueError):
        await UserBanService().resolve_user(value)
    assert await ban_database.get_banned_users() == []


async def test_list_deduplicates_local_and_global_bans_and_paginates(ban_database):
    await sender(100, "previous", 1)
    await sender(100, "latest_tag", 2)
    await sender(101, "None", 1)
    for user in range(100, 125):
        await ban_database.add_global_ban(user)
    await ban_database.add_banned_user({"id_user": 100, "id_channel": -1001, "bot_id": 1})
    service = UserBanService()
    first, total, page = await service.list_users()
    assert (total, page, len(first)) == (25, 0, 20)
    assert first[0].label == "@latest_tag | TG ID: 100"
    assert first[1].label == "@None | TG ID: 101"
    second, total, page = await service.list_users(999)
    assert (total, page, len(second)) == (25, 1, 5)
    assert len({user.user_id for user in first+second}) == 25
    assert all(user.username is None for user in second)


def master():
    instance = MasterBot.__new__(MasterBot)
    instance.user_states = {}
    instance.user_ban_service = UserBanService()
    instance.main_bot = SimpleNamespace(send_message=AsyncMock())
    instance._is_admin = lambda user: user == 42
    instance._is_general_admin = lambda user: user == 42
    return instance


def admin_message(text, *, user=42, caption=None):
    return SimpleNamespace(text=text, caption=caption, chat=SimpleNamespace(id=user), from_user=SimpleNamespace(id=user))


def test_extra_panel_has_bans_and_menu_contains_three_actions_and_back():
    buttons = [button for row in build_extra_panel().keyboard for button in row]
    assert any(button.text == "Баны" and button.callback_data == "panel:bans" for button in buttons)
    assert [button.callback_data for row in build_bans_panel().keyboard for button in row] == [
        "panel:bans_list:0", "panel:ban_user", "panel:unban_user", "panel:extra",
    ]


async def test_panel_callbacks_and_text_ban_unban_list_through_real_handlers(ban_database, monkeypatch):
    monkeypatch.setattr("src.master.settings.general_admin", 42)
    await sender(123, "test_user")
    bot = master()
    bot.main_bot = AsyncTeleBot("1:test")
    bot.main_bot.send_message = AsyncMock()
    bot.main_bot.answer_callback_query = AsyncMock()
    bot._MasterBot__setup_handlers()
    payload = {"message_id": 1, "date": 0, "chat": {"id": 42, "type": "private"},
               "from": {"id": 42, "is_bot": False, "first_name": "Admin"}, "text": "Menu"}
    async def callback(data):
        call = CallbackQuery.de_json({"id": data, "from": payload["from"], "chat_instance": "42", "message": payload, "data": data})
        await bot.main_bot.process_new_callback_query([call])
    await callback("panel:bans")
    await callback("panel:ban_user")
    assert bot.user_states[42]["action"] == "await_ban_user"
    await bot.main_bot.process_new_messages([Message.de_json({**payload, "text": "@TEST_USER"})])
    assert await Utils().check_banned_user(123, -100999)
    assert 42 not in bot.user_states
    await callback("panel:bans_list:0")
    assert "@test_user | TG ID: 123" in bot.main_bot.send_message.await_args.args[1]
    await callback("panel:unban_user")
    await bot.main_bot.process_new_messages([Message.de_json({**payload, "text": "123"})])
    assert not await Utils().check_banned_user(123, -100999)
    assert 42 not in bot.user_states


async def test_panel_retry_cancel_and_back_clear_pending_ban(ban_database):
    bot = master()
    await bot._handle_bans_callback("ban_user", 42)
    assert await bot._handle_stateful_admin_text(admin_message("@unknown"))
    assert bot.user_states[42]["action"] == "await_ban_user"
    assert await ban_database.get_banned_users() == []
    assert await bot._handle_stateful_admin_text(admin_message("отмена"))
    assert 42 not in bot.user_states
    await bot._handle_bans_callback("unban_user", 42)
    await bot._show_extra_panel(42)
    assert 42 not in bot.user_states


async def test_non_admin_cannot_submit_pending_global_ban(ban_database):
    bot = master()
    bot._set_user_state(777, "await_ban_user")
    assert await bot._handle_stateful_admin_text(admin_message("123", user=777))
    assert await ban_database.get_banned_users() == []
    assert 777 not in bot.user_states


async def test_photo_caption_cannot_trigger_a_global_ban(ban_database):
    bot = master()
    bot._set_user_state(42, "await_ban_user")
    assert await bot._handle_stateful_admin_text(admin_message(None, caption="123"))
    assert await ban_database.get_banned_users() == []
    assert bot.user_states[42]["action"] == "await_ban_user"


@pytest.mark.parametrize("channel", [-1001, -1002])
async def test_global_ban_blocks_actual_suggestion_handlers_in_each_bot(ban_database, channel):
    await ban_database.add_global_ban(123)
    subbot = SubBot.__new__(SubBot)
    subbot.channel_id = channel
    subbot.channel_username = "@test_channel"
    subbot.bot_info = SimpleNamespace(username="suggest_bot", id=99)
    subbot.users_data = {123}
    subbot.chat_suggest = -10055
    subbot.ban_usr_msg = "Вы заблокированы"
    subbot.sup_bot = AsyncTeleBot("1:test")
    subbot.sup_bot.send_message = AsyncMock()
    subbot.sup_bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))
    subbot._process_single_submission = AsyncMock()
    subbot._queue_media_group = AsyncMock()
    await subbot._SubBot__setup_handlers()
    for album in (None, "album-1"):
        message = Message.de_json({"message_id": 1, "date": 0, "chat": {"id": 123, "type": "private"},
                                  "from": {"id": 123, "is_bot": False, "first_name": "Author"},
                                  "text": "Suggestion", **({"media_group_id": album} if album else {})})
        await subbot.sup_bot.process_new_messages([message])
    subbot._process_single_submission.assert_not_awaited()
    subbot._queue_media_group.assert_not_awaited()
    assert subbot.sup_bot.send_message.await_count == 2


async def test_non_admin_callback_cannot_open_or_change_bans(ban_database):
    bot = master()
    bot.main_bot = AsyncTeleBot("1:test")
    bot.main_bot.send_message = AsyncMock()
    bot.main_bot.answer_callback_query = AsyncMock()
    bot._MasterBot__setup_handlers()
    for data in ("panel:bans", "panel:bans_list:0", "panel:ban_user", "panel:unban_user"):
        await bot.main_bot.process_new_callback_query([CallbackQuery.de_json({
            "id": data, "from": {"id": 777, "is_bot": False, "first_name": "Visitor"},
            "chat_instance": "777", "data": data,
            "message": {"message_id": 1, "date": 0, "chat": {"id": 777, "type": "private"}, "text": "Menu"},
        })])
    assert bot.user_states == {}
    assert await ban_database.get_banned_users() == []
    bot.main_bot.send_message.assert_not_awaited()
    assert bot.main_bot.answer_callback_query.await_count == 4


async def test_empty_list_keeps_back_button(ban_database):
    bot = master()
    await bot._show_banned_users(42)
    assert bot.main_bot.send_message.await_args.args[1] == "Забаненных пользователей нет."
    callbacks = [button.callback_data for row in bot.main_bot.send_message.await_args.kwargs["reply_markup"].keyboard for button in row]
    assert callbacks == ["panel:bans"]


async def test_postgres_global_ban_targets_existing_unique_key(monkeypatch):
    from sqlalchemy.dialects import postgresql
    result = SimpleNamespace(scalar_one_or_none=lambda: 1)
    conn = SimpleNamespace(dialect=postgresql.dialect(), execute=AsyncMock(return_value=result), commit=AsyncMock())
    class Context:
        async def __aenter__(self):
            return conn
        async def __aexit__(self, *args):
            return None
    monkeypatch.setattr(db_helper, "engine", SimpleNamespace(connect=Context))
    assert await CrudBannedUser.add_global_ban(123)
    stmt = conn.execute.await_args.args[0]
    sql = str(stmt.compile(dialect=conn.dialect))
    assert "ON CONFLICT (id_user, id_channel, bot_id) DO NOTHING" in sql
    assert stmt.compile(dialect=conn.dialect).params == {"id_user": 123, "id_channel": 0, "bot_id": 0}
    conn.commit.assert_awaited_once()


async def test_banned_list_navigation_has_all_users_without_repeated_rows(ban_database):
    for user_id in range(100, 125):
        await ban_database.add_global_ban(user_id)
    bot = master()
    await bot._show_banned_users(42, 0)
    first = bot.main_bot.send_message.await_args
    callbacks = [button.callback_data for row in first.kwargs["reply_markup"].keyboard for button in row]
    assert callbacks == ["panel:bans_list:1", "panel:bans"]
    assert first.args[1].count("TG ID:") == 20
    await bot._show_banned_users(42, 1)
    second = bot.main_bot.send_message.await_args
    callbacks = [button.callback_data for row in second.kwargs["reply_markup"].keyboard for button in row]
    assert callbacks == ["panel:bans_list:0", "panel:bans"]
    assert second.args[1].count("TG ID:") == 5
    assert "Страница 2" in second.args[1]
