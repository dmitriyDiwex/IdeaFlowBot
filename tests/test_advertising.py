from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.editorial.services.advertising import (
    build_advertising_alert_text,
    resolve_advertising_targets,
    send_advertising_flow,
)


def test_advertising_alert_contains_clickable_submission_author_id() -> None:
    text = build_advertising_alert_text(
        channel_label="@channel",
        source_text="Хочу рекламу",
        sender_user_id=123456789,
        sender_username="submission_author",
        sender_first_name="Author",
    )

    assert "отправитель: @submission_author" in text
    assert 'tg id: <a href="tg://user?id=123456789">123456789</a>' in text


@pytest.mark.asyncio
async def test_advertising_flow_uses_recipient_as_linked_submission_author(monkeypatch) -> None:
    source_bot = SimpleNamespace(send_message=AsyncMock())
    alert_bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(
        "src.editorial.services.advertising._build_advertising_alert_bot",
        lambda: alert_bot,
    )
    monkeypatch.setattr(
        "src.editorial.services.advertising.resolve_advertising_targets",
        lambda: [9001],
    )

    await send_advertising_flow(
        bot=source_bot,
        recipient_user_id=123456789,
        channel_label="@channel",
        source_text="Хочу рекламу",
        sender_username="submission_author",
        sender_first_name="Author",
    )

    source_bot.send_message.assert_awaited_once()
    assert source_bot.send_message.await_args.kwargs["chat_id"] == 123456789
    alert_bot.send_message.assert_awaited_once()
    alert_kwargs = alert_bot.send_message.await_args.kwargs
    assert alert_kwargs["chat_id"] == 9001
    assert alert_kwargs["parse_mode"] == "HTML"
    assert 'href="tg://user?id=123456789"' in alert_kwargs["text"]


@pytest.mark.parametrize("manager_chat_id", [9001, None])
def test_advertising_targets_use_unique_ids_and_never_private_username(
    monkeypatch, manager_chat_id,
) -> None:
    monkeypatch.setattr("src.editorial.services.advertising.settings", SimpleNamespace(
        advertiser=[8001, 8001, 8002],
        advertising_manager_chat_id=manager_chat_id,
        advertising_manager_username="@new_manager",
    ))

    expected = [8001, 8002, 9001] if manager_chat_id is not None else [8001, 8002]
    assert resolve_advertising_targets() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_advertising_manager_change_sends_by_id_without_username_error(
    monkeypatch, strict,
) -> None:
    source_bot = SimpleNamespace(send_message=AsyncMock())

    async def send_manager_message(*, chat_id, **kwargs):
        if isinstance(chat_id, str):
            raise RuntimeError("400 Bad Request: chat not found")

    alert_bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=send_manager_message),
        close_session=AsyncMock(),
    )
    monkeypatch.setattr("src.editorial.services.advertising.settings", SimpleNamespace(
        advertiser=[9001],
        advertising_manager_chat_id=9001,
        advertising_manager_username=" @new_manager ",
    ))
    monkeypatch.setattr(
        "src.editorial.services.advertising._build_advertising_alert_bot", lambda: alert_bot,
    )

    await send_advertising_flow(
        bot=source_bot,
        recipient_user_id=123456789,
        channel_label="@channel",
        source_text="Хочу рекламу",
        sender_username=None,
        sender_first_name=None,
        strict=strict,
    )

    source_bot.send_message.assert_awaited_once_with(
        chat_id=123456789,
        text="По рекламе напишите пожалуйста @new_manager, сразу укажите, что вы хотите рекламировать",
    )
    alert_bot.send_message.assert_awaited_once()
    assert alert_bot.send_message.await_args.kwargs["chat_id"] == 9001
    if strict:
        alert_bot.close_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_strict_advertising_requires_manager_id_before_sending_reply(monkeypatch) -> None:
    source_bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr("src.editorial.services.advertising.settings", SimpleNamespace(
        advertiser=[],
        advertising_manager_chat_id=None,
        advertising_manager_username="@new_manager",
    ))

    with pytest.raises(ValueError, match="ADVERTISING_MANAGER_CHAT_ID or ADVERTISER_IDS"):
        await send_advertising_flow(
            bot=source_bot,
            recipient_user_id=123456789,
            channel_label="@channel",
            source_text="Advertising request",
            sender_username=None,
            sender_first_name=None,
            strict=True,
        )

    source_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_advertising_reports_real_manager_failure_with_chat_id(monkeypatch) -> None:
    source_bot = SimpleNamespace(send_message=AsyncMock())
    alert_bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=RuntimeError("400 Bad Request: chat not found")),
        close_session=AsyncMock(),
    )
    monkeypatch.setattr("src.editorial.services.advertising.settings", SimpleNamespace(
        advertiser=[],
        advertising_manager_chat_id=9001,
        advertising_manager_username="@new_manager",
    ))
    monkeypatch.setattr(
        "src.editorial.services.advertising._build_advertising_alert_bot", lambda: alert_bot,
    )

    with pytest.raises(ValueError, match="manager notification failed: chat_id=9001:.*chat not found"):
        await send_advertising_flow(
            bot=source_bot,
            recipient_user_id=123456789,
            channel_label="@channel",
            source_text="Advertising request",
            sender_username=None,
            sender_first_name=None,
            strict=True,
        )

    source_bot.send_message.assert_awaited_once()
    alert_bot.send_message.assert_awaited_once()
    alert_bot.close_session.assert_awaited_once()
