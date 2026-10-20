import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.editorial.models.channel import Channel
from src.editorial.models.submission import Submission
from src.editorial.services.legacy_moderation_sync import LegacyModerationSyncService
from src.telegram_runtime import telegram_process_session


@pytest.mark.asyncio
async def test_parallel_card_edits_do_not_close_each_others_shared_session(monkeypatch):
    closed = False
    first_finished = asyncio.Event()
    close = AsyncMock()

    async def close_session():
        nonlocal closed
        closed = True
        await close()

    class Bot:
        def __init__(self, _token):
            pass

        async def edit_message_reply_markup(self, *, message_id, reply_markup, **kwargs):
            if message_id == 1:
                first_finished.set()
            else:
                await first_finished.wait()
                await asyncio.sleep(0.01)
            if closed:
                raise TimeoutError("Shared session closed by another bot")
            return SimpleNamespace(reply_markup=reply_markup)

    Bot.close_session = staticmethod(close_session)
    channel = SimpleNamespace(id=1, tg_channel_id=-1001)
    submissions = {i: SimpleNamespace(id=i, channel_id=1, legacy_row_id=i,
                                      source_user_id=100+i, username=None, first_name="Sender")
                   for i in [1, 2]}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, key):
            return channel if model is Channel else submissions[key]

    async def fetch(ids):
        return [SimpleNamespace(id=i, review_chat_id=-1002, review_message_id=i,
                                user_id=100+i, username=None, first_name="Sender") for i in ids]

    async def related(session, submission):
        return [submission]

    monkeypatch.setattr("src.editorial.services.legacy_moderation_sync.session_factory", Session)
    monkeypatch.setattr("src.editorial.services.legacy_moderation_sync.AsyncTeleBot", Bot)
    service = LegacyModerationSyncService(
        legacy_reader=SimpleNamespace(fetch_sender_rows_by_ids=fetch,
                                      get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="fake"))),
        importer=SimpleNamespace(), moderation=SimpleNamespace(get_related_submissions=related),
    )
    assert await asyncio.gather(*[
        service.sync_panel_submission_agent_checked(i, decision="reject") for i in [1, 2]
    ]) == [1, 1]
    close.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_session_is_closed_once_on_process_shutdown(monkeypatch):
    from telebot import asyncio_helper
    fake = SimpleNamespace(closed=False, close=AsyncMock())
    manager = SimpleNamespace(session=fake)
    monkeypatch.setattr(asyncio_helper, "session_manager", manager)
    monkeypatch.setattr(asyncio_helper, "REQUEST_TIMEOUT", asyncio_helper.REQUEST_TIMEOUT)
    monkeypatch.setattr(asyncio_helper, "REQUEST_LIMIT", asyncio_helper.REQUEST_LIMIT)
    async with telegram_process_session():
        fake.close.assert_not_awaited()
        assert asyncio_helper.REQUEST_TIMEOUT < 300
    fake.close.assert_awaited_once()
    assert manager.session is None
