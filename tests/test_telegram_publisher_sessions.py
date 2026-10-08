from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.editorial.services.telegram_publisher import TelegramPublisherAdapter


@pytest.mark.asyncio
async def test_send_text_closes_bot_session_after_success(monkeypatch) -> None:
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        close_session=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.editorial.services.telegram_publisher.AsyncTeleBot",
        lambda _token: bot,
    )

    message_id = await TelegramPublisherAdapter().send_text(
        bot_token="1:token",
        channel_id=-1001,
        text="test",
    )

    assert message_id == 42
    bot.close_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_text_closes_bot_session_after_failure(monkeypatch) -> None:
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=RuntimeError("Telegram failed")),
        close_session=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.editorial.services.telegram_publisher.AsyncTeleBot",
        lambda _token: bot,
    )

    with pytest.raises(RuntimeError, match="Telegram failed"):
        await TelegramPublisherAdapter().send_text(
            bot_token="1:token",
            channel_id=-1001,
            text="test",
        )

    bot.close_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_copy_messages_checks_partial_telegram_response(monkeypatch):
    from src.editorial.services.telegram_resilience import PartialTelegramCopyError

    class Response:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def json(self):
            return {"ok": True, "result": [{"message_id": 701}]}

    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def post(self, *args, **kwargs):
            assert kwargs["json"]["message_ids"] == [11, 12]
            return Response()

    monkeypatch.setattr("src.editorial.services.telegram_publisher.aiohttp.ClientSession", Client)
    with pytest.raises(PartialTelegramCopyError) as error:
        await TelegramPublisherAdapter().copy_messages("1:token", -1001, 1001, [11, 12])
    assert error.value.copied_message_ids == [701]
