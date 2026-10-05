from __future__ import annotations

from html import escape

from loguru import logger
from telebot.async_telebot import AsyncTeleBot

from config import settings


def _normalize_channel_label(channel_label: str | None) -> str:
    value = (channel_label or "").strip()
    return value or "@unknown_channel"


def _manager_mention() -> str:
    username = (settings.advertising_manager_username or "").strip() or "ivanblk"
    if not username.startswith("@"):
        username = f"@{username}"
    return username


def build_advertising_reply_text() -> str:
    return (
        f"По рекламе напишите пожалуйста {_manager_mention()}, "
        "сразу укажите, что вы хотите рекламировать"
    )


def _sender_tag(username: str | None) -> str:
    return f"@{username}" if username else "@None"


def _sender_nick(first_name: str | None) -> str:
    value = (first_name or "").strip()
    return value or "None"


def build_advertising_alert_text(
    *,
    channel_label: str | None,
    source_text: str | None,
    sender_user_id: int,
    sender_username: str | None,
    sender_first_name: str | None,
) -> str:
    safe_channel = escape(_normalize_channel_label(channel_label))
    safe_text = escape((source_text or "").strip() or "текста нет")
    safe_sender_user_id = escape(str(sender_user_id))
    safe_sender_tag = escape(_sender_tag(sender_username))
    safe_sender_nick = escape(_sender_nick(sender_first_name))
    return (
        f"реклама: {safe_channel}\n"
        f"<blockquote>{safe_text}</blockquote>\n"
        f"отправитель: {safe_sender_tag}, "
        f'tg id: <a href="tg://user?id={safe_sender_user_id}">{safe_sender_user_id}</a>, '
        f"ник: {safe_sender_nick}"
    )


def resolve_advertising_targets() -> list[int]:
    # Personal usernames are mentions, not valid Bot API private-chat targets.
    targets: list[int] = []
    seen: set[str] = set()

    for advertiser_id in settings.advertiser:
        key = f"id:{advertiser_id}"
        if key in seen:
            continue
        seen.add(key)
        targets.append(int(advertiser_id))

    manager_chat_id = settings.advertising_manager_chat_id
    if manager_chat_id is not None:
        key = f"id:{manager_chat_id}"
        if key not in seen:
            seen.add(key)
            targets.append(int(manager_chat_id))

    return targets


def _build_advertising_alert_bot() -> AsyncTeleBot | None:
    token = (settings.advertising_bot_token or "").strip()
    if not token:
        return None
    return AsyncTeleBot(token)


async def send_advertising_flow(
    *,
    bot: AsyncTeleBot,
    recipient_user_id: int,
    channel_label: str | None,
    source_text: str | None,
    sender_username: str | None,
    sender_first_name: str | None,
    strict: bool = False,
) -> None:
    targets = resolve_advertising_targets()
    if not targets:
        message = (
            "Advertising manager notification is not configured: set "
            "ADVERTISING_MANAGER_CHAT_ID or ADVERTISER_IDS; "
            "ADVERTISING_MANAGER_USERNAME is only used in the reply text"
        )
        if strict:
            raise ValueError(message)
        logger.warning(message)

    await bot.send_message(chat_id=recipient_user_id, text=build_advertising_reply_text())

    advertiser_message = build_advertising_alert_text(
        channel_label=channel_label,
        source_text=source_text,
        sender_user_id=recipient_user_id,
        sender_username=sender_username,
        sender_first_name=sender_first_name,
    )

    alert_bot = _build_advertising_alert_bot() or bot

    errors = []
    try:
        for target in targets:
            try:
                await alert_bot.send_message(
                    chat_id=target,
                    text=advertiser_message,
                    parse_mode="HTML",
                )
            except Exception as ex:
                logger.error("Failed to send advertising alert to {}: {}", target, ex)
                errors.append(f"chat_id={target}: {ex}")
    finally:
        if strict and alert_bot is not bot:
            await alert_bot.close_session()
    if strict and errors:
        raise ValueError("Advertising reply sent, but manager notification failed: " + "; ".join(errors))
