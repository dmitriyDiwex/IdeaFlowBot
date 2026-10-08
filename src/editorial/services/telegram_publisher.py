from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiohttp
from loguru import logger

from telebot.async_telebot import AsyncTeleBot, asyncio_helper

from config import settings as legacy_settings
from src.editorial.config import settings
from src.editorial.services.telegram_resilience import (
    TelegramAPIError, run_telegram_operation, validate_album_copy,
)


@dataclass(slots=True)
class TelegramChatInfo:
    title: str | None = None
    tag: str | None = None
    invite_link: str | None = None


@asynccontextmanager
async def telegram_bot_session(bot_token: str) -> AsyncIterator[AsyncTeleBot]:
    bot = AsyncTeleBot(bot_token)
    try:
        yield bot
    finally:
        try:
            await bot.close_session()
        except Exception as exc:
            logger.warning("Failed to close Telegram bot session: {}", exc)


class TelegramPublisherAdapter:
    def __init__(self) -> None:
        proxy = None
        try:
            proxy = legacy_settings.proxies.get("http")
        except Exception:
            proxy = None
        if proxy:
            asyncio_helper.proxy = proxy

    async def send_text(
        self,
        bot_token: str,
        channel_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> int:
        async with telegram_bot_session(bot_token) as bot:
            message = await run_telegram_operation(
                bot.send_message(
                    chat_id=channel_id,
                    text=text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                ),
                operation="sendMessage",
            )
            return int(message.message_id)

    async def get_chat_tag(self, bot_token: str, channel_id: int) -> str | None:
        return (await self.get_chat_info(bot_token=bot_token, channel_id=channel_id)).tag

    async def get_chat_info(self, bot_token: str, channel_id: int) -> TelegramChatInfo:
        async with telegram_bot_session(bot_token) as bot:
            chat = await run_telegram_operation(
                bot.get_chat(channel_id),
                operation="getChat",
            )
            title = getattr(chat, "title", None)
            username = getattr(chat, "username", None)
            invite_link = getattr(chat, "invite_link", None)
            return TelegramChatInfo(
                title=title,
                tag=f"@{username}" if username else None,
                invite_link=invite_link,
            )

    async def send_text_with_entities(
        self,
        bot_token: str,
        channel_id: int,
        text: str,
        entities: list[dict],
        disable_web_page_preview: bool | None = None,
    ) -> int:
        payload = {
            "chat_id": channel_id,
            "text": text,
            "entities": entities,
        }
        if disable_web_page_preview is not None:
            payload["disable_web_page_preview"] = disable_web_page_preview
        request_kwargs = {}
        proxy = None
        try:
            proxy = legacy_settings.proxies.get("http")
        except Exception:
            proxy = None
        if proxy:
            request_kwargs["proxy"] = proxy

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        timeout = aiohttp.ClientTimeout(total=settings.telegram_request_timeout_seconds)

        async def request() -> dict:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, **request_kwargs) as response:
                    return await response.json()

        result = await run_telegram_operation(
            request(),
            operation="sendMessageWithEntities",
        )
        if not result.get("ok"):
            raise TelegramAPIError(result)
        return int(result["result"]["message_id"])

    async def copy_message(
        self,
        bot_token: str,
        channel_id: int,
        from_chat_id: int,
        message_id: int,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> int:
        async with telegram_bot_session(bot_token) as bot:
            message = await run_telegram_operation(
                bot.copy_message(
                    chat_id=channel_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    caption=caption,
                    parse_mode=parse_mode,
                ),
                operation="copyMessage",
            )
            return int(message.message_id)

    async def copy_messages(
        self,
        bot_token: str,
        channel_id: int,
        from_chat_id: int,
        message_ids: list[int],
    ) -> list[int]:
        if len(message_ids) == 1:
            return [
                await self.copy_message(
                    bot_token=bot_token,
                    channel_id=channel_id,
                    from_chat_id=from_chat_id,
                    message_id=message_ids[0],
                )
            ]

        payload = {
            "chat_id": channel_id,
            "from_chat_id": from_chat_id,
            "message_ids": message_ids,
        }
        request_kwargs = {}
        proxy = None
        try:
            proxy = legacy_settings.proxies.get("http")
        except Exception:
            proxy = None
        if proxy:
            request_kwargs["proxy"] = proxy

        url = f"https://api.telegram.org/bot{bot_token}/copyMessages"
        timeout = aiohttp.ClientTimeout(total=settings.telegram_request_timeout_seconds)

        async def request() -> dict:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, **request_kwargs) as response:
                    return await response.json()

        result = await run_telegram_operation(
            request(),
            operation="copyMessages",
        )
        if not result.get("ok"):
            raise TelegramAPIError(result)

        copied_items = result.get("result") or []
        copied_message_ids = [int(item["message_id"]) for item in copied_items]
        validate_album_copy(len(message_ids), copied_message_ids)
        return copied_message_ids

    async def edit_message_caption(
        self,
        bot_token: str,
        channel_id: int,
        message_id: int,
        caption: str,
        parse_mode: str | None = None,
    ) -> None:
        async with telegram_bot_session(bot_token) as bot:
            await run_telegram_operation(
                bot.edit_message_caption(
                    chat_id=channel_id,
                    message_id=message_id,
                    caption=caption,
                    parse_mode=parse_mode,
                ),
                operation="editMessageCaption",
            )
