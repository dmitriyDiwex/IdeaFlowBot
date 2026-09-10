from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger
from telebot.async_telebot import AsyncTeleBot
from telebot.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import settings
from src.editorial.db.session import session_factory
from src.editorial.models.ad_blackout import ChannelAdBlackout
from src.editorial.services.ad_link_exclusion_service import AdLinkExclusionService
from src.editorial.services.confession_service import ConfessionService
from src.editorial.services.legacy_publication_guard import LegacyPublicationGuard
from src.utils import Utils


CONFESSION_CONTENT_TYPES = [
    "text",
    "photo",
    "video",
    "animation",
    "sticker",
    "document",
    "audio",
    "voice",
    "video_note",
]


class ConfessionPublisherRuntime:
    """One bot that owns the confession paste storage and publishes to all targets."""

    def __init__(self, *, publisher_id: int, bot_api_token: str, bot_user_id: int) -> None:
        self.publisher_id = int(publisher_id)
        self.bot_api_token = bot_api_token
        self.bot_user_id = int(bot_user_id)
        self.bot = AsyncTeleBot(bot_api_token)
        self.polling_task: asyncio.Task | None = None
        self.service = ConfessionService()
        self.publication_guard = LegacyPublicationGuard()
        self.ad_link_exclusion_service = AdLinkExclusionService()
        self._setup_handlers()

    @staticmethod
    def _message_body(message: Message) -> str | None:
        value = (message.text or message.caption or "").strip()
        if value:
            return value
        sticker = getattr(message, "sticker", None)
        if sticker is not None and getattr(sticker, "emoji", None):
            return str(sticker.emoji)
        return None

    @staticmethod
    def _is_service_message(message: Message) -> bool:
        value = message.text if message.text is not None else message.caption
        return bool(value and value.lstrip().startswith("/"))

    @staticmethod
    def _candidate_review_markup(candidate_id: int) -> InlineKeyboardMarkup:
        markup = InlineKeyboardMarkup(row_width=2)
        markup.row(
            InlineKeyboardButton(
                "Да",
                callback_data=f"confession_candidate:yes:{int(candidate_id)}",
            ),
            InlineKeyboardButton(
                "Нет",
                callback_data=f"confession_candidate:no:{int(candidate_id)}",
            ),
        )
        return markup

    async def _ensure_external_link_blackout(
        self,
        message: Message,
    ) -> ChannelAdBlackout | None:
        if getattr(message.chat, "type", None) != "channel":
            return None

        channel_username = getattr(message.chat, "username", None)
        own_channel_ref: str | int = (
            f"@{channel_username}" if channel_username else int(message.chat.id)
        )
        ignored_links: set[str] = set()
        exclusion_service = getattr(self, "ad_link_exclusion_service", None)
        if exclusion_service is not None:
            try:
                async with session_factory() as session:
                    ignored_links = await exclusion_service.list_normalized_links(session)
            except Exception as exc:
                logger.error(
                    "Failed to load automatic ad link exclusions for confession channel {}: {}",
                    message.chat.id,
                    exc,
                )
        if not await Utils.check_link(
            message,
            ignored_channel_ref=own_channel_ref,
            ignored_links=ignored_links,
        ):
            return None

        published_at = message.date
        if not isinstance(published_at, datetime):
            published_at = datetime.fromtimestamp(published_at, tz=timezone.utc)
        elif published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)

        blackout = await self.publication_guard.ensure_automatic_ad_blackout_for_channel_post(
            tg_channel_id=message.chat.id,
            telegram_message_id=message.message_id,
            published_at=published_at,
        )
        if blackout is None:
            logger.warning(
                "Could not create automatic ad blackout: confession channel {} is not configured",
                message.chat.id,
            )
        else:
            logger.info(
                "Created or found automatic ad blackout for confession channel post {} in {}",
                message.message_id,
                message.chat.id,
            )
        return blackout

    def _setup_handlers(self) -> None:
        @self.bot.message_handler(commands=["connect_confessions"])
        async def connect_storage(message: Message) -> None:
            if message.chat.type not in {"group", "supergroup"}:
                await self.bot.reply_to(
                    message,
                    "Эту команду нужно отправить в закрытой группе, которая будет хранилищем паст.",
                )
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) != 2:
                await self.bot.reply_to(message, "Нужен код: /connect_confessions CODE")
                return
            try:
                member = await self.bot.get_chat_member(message.chat.id, self.bot_user_id)
                raw_status = getattr(member, "status", "")
                member_status = str(getattr(raw_status, "value", raw_status))
                if member_status not in {"administrator", "creator"}:
                    raise ValueError(
                        "Сначала добавьте этого бота в администраторы группы, иначе он не увидит все новые пасты."
                    )
                async with session_factory() as session:
                    await self.service.bind_storage_chat(
                        session,
                        publisher_id=self.publisher_id,
                        bind_code=parts[1],
                        storage_chat_id=message.chat.id,
                        storage_chat_title=getattr(message.chat, "title", None),
                    )
            except ValueError as exc:
                await self.bot.reply_to(message, str(exc))
                return
            except Exception as exc:
                logger.exception("Failed to bind confession storage chat {}", message.chat.id)
                await self.bot.reply_to(message, f"Не удалось подключить чат: {exc}")
                return
            await self.bot.reply_to(
                message,
                "Чат подключён. После каждого нового сообщения бот предложит подтвердить его добавление в пасты признавашек.",
            )

        async def store_message(message: Message) -> None:
            try:
                await self._ensure_external_link_blackout(message)
            except Exception:
                logger.exception(
                    "Failed to create automatic ad blackout for confession channel post {} in {}",
                    message.message_id,
                    message.chat.id,
                )
            if self._is_service_message(message):
                return
            try:
                async with session_factory() as session:
                    candidate, created = await self.service.create_candidate(
                        session,
                        publisher_id=self.publisher_id,
                        storage_chat_id=message.chat.id,
                        storage_message_id=message.message_id,
                        content_type=message.content_type or "text",
                        body_text=self._message_body(message),
                        submitted_by=message.from_user.id if message.from_user else None,
                    )
                if not created or candidate is None:
                    return

                prompt = await self.bot.reply_to(
                    message,
                    "Добавить эту пасту в базу?",
                    reply_markup=self._candidate_review_markup(candidate.id),
                )
                try:
                    async with session_factory() as session:
                        await self.service.set_candidate_prompt_message(
                            session,
                            candidate_id=candidate.id,
                            prompt_message_id=prompt.message_id,
                        )
                except Exception:
                    logger.exception(
                        "Failed to save confirmation message for confession candidate {}",
                        candidate.id,
                    )
                logger.info(
                    "Created confession paste candidate from chat {} message {}",
                    message.chat.id,
                    message.message_id,
                )
            except ValueError as exc:
                if "не из подключённого" not in str(exc):
                    logger.warning("Confession paste candidate was not saved: {}", exc)
            except Exception:
                logger.exception(
                    "Failed to create confession paste candidate from chat {} message {}",
                    message.chat.id,
                    message.message_id,
                )

        @self.bot.callback_query_handler(
            func=lambda call: bool(
                call.data and call.data.startswith("confession_candidate:")
            )
        )
        async def review_candidate(call: CallbackQuery) -> None:
            try:
                _prefix, action, raw_candidate_id = (call.data or "").split(":", maxsplit=2)
                candidate_id = int(raw_candidate_id)
                reviewer_id = call.from_user.id if call.from_user else None
                if action not in {"yes", "no"}:
                    raise ValueError("Неизвестное действие.")

                async with session_factory() as session:
                    if action == "yes":
                        await self.service.approve_candidate(
                            session,
                            candidate_id=candidate_id,
                            reviewed_by=reviewer_id,
                        )
                        status_text = "✅ Паста добавлена в базу."
                        callback_text = "Паста добавлена."
                    else:
                        await self.service.reject_candidate(
                            session,
                            candidate_id=candidate_id,
                            reviewed_by=reviewer_id,
                        )
                        status_text = "❌ Паста не добавлена."
                        callback_text = "Паста отклонена."

                if call.message is not None:
                    try:
                        await self.bot.edit_message_text(
                            status_text,
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id,
                            reply_markup=None,
                        )
                    except Exception as exc:
                        logger.debug(
                            "Could not update confession candidate prompt {}: {}",
                            candidate_id,
                            exc,
                        )
                await self.bot.answer_callback_query(call.id, callback_text)
            except ValueError as exc:
                await self.bot.answer_callback_query(call.id, str(exc), show_alert=True)
            except Exception:
                logger.exception("Failed to review confession paste candidate")
                await self.bot.answer_callback_query(
                    call.id,
                    "Не удалось изменить статус пасты.",
                    show_alert=True,
                )

        self.bot.register_message_handler(store_message, content_types=CONFESSION_CONTENT_TYPES)
        self.bot.register_channel_post_handler(store_message, content_types=CONFESSION_CONTENT_TYPES)
        self.bot.register_edited_channel_post_handler(
            store_message,
            content_types=CONFESSION_CONTENT_TYPES,
        )

    async def start(self) -> None:
        bot_info = await self.bot.get_me()
        logger.info("[OK] confession publisher @{} working", bot_info.username)
        self.polling_task = asyncio.create_task(
            self.bot.infinity_polling(
                timeout=10,
                request_timeout=settings.telegram_request_timeout_seconds,
            )
        )

    async def stop(self) -> None:
        if self.polling_task is not None and not self.polling_task.done():
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                pass
        try:
            await self.bot.close_session()
        except Exception:
            pass
