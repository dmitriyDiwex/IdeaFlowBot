import pytz
import json
import re
from collections.abc import Iterable
from telebot.types import Message, CallbackQuery, InlineKeyboardButton
from datetime import datetime, timedelta, timezone
from loguru import logger

from src.core_database.database import CrudBannedUser, CrudPostData
from src.core_database.models.sender_info import SenderData
from src.core_database.models.admin_actions import AdminActionData
from src.editorial.services.ad_link_exclusion_service import normalized_ad_link_is_excluded
from config import settings


class Utils:
    def __init__(self):
        self.db_banned = CrudBannedUser()
        self.sender_data = CrudPostData(SenderData)
        self.action_admin = CrudPostData(AdminActionData)

    @staticmethod
    def _extract_preview_file_id(message: Message) -> str | None:
        if message.content_type == "photo" and message.photo:
            return message.photo[-1].file_id
        if message.content_type == "video" and message.video is not None:
            return message.video.file_id
        if message.content_type == "animation" and message.animation is not None:
            return message.animation.file_id
        return None

    @staticmethod
    def _extract_preview_file_size(message: Message) -> int | None:
        if message.content_type == "photo" and message.photo:
            return message.photo[-1].file_size
        if message.content_type == "video" and message.video is not None:
            return message.video.file_size
        if message.content_type == "animation" and message.animation is not None:
            return message.animation.file_size
        return None

    @staticmethod
    def _serialize_entity_value(value):
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [Utils._serialize_entity_value(item) for item in value if item is not None]
        if isinstance(value, dict):
            return {
                key: Utils._serialize_entity_value(item)
                for key, item in value.items()
                if item is not None
            }
        if hasattr(value, "to_dict"):
            return Utils._serialize_entity_value(value.to_dict())
        if hasattr(value, "__dict__"):
            return {
                key: Utils._serialize_entity_value(item)
                for key, item in value.__dict__.items()
                if not key.startswith("_") and item is not None
            }
        return str(value)

    @staticmethod
    def _extract_entities_json(message: Message) -> str | None:
        entities = message.entities if message.content_type == "text" else message.caption_entities
        if not entities:
            raw_payload = None
            if message.content_type == "text":
                raw_payload = message.json.get("entities")
            elif message.caption is not None:
                raw_payload = message.json.get("caption_entities")
            if not raw_payload:
                return None
            return json.dumps(raw_payload, ensure_ascii=False)

        payload = [Utils._serialize_entity_value(entity) for entity in entities]
        payload = [item for item in payload if item]
        if not payload:
            return None
        return json.dumps(payload, ensure_ascii=False)

    async def check_banned_user(self, id_user: int, id_channel: int) -> bool:
        all_info = await self.db_banned.get_banned_users(id_user=id_user, id_channel=id_channel)
        return bool(all_info)

    @staticmethod
    async def get_timestamp_public(time) -> float:
        tz = timezone(timedelta(hours=3))
        hour, minute = map(int, time.split(':'))
        now = datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If the moderator taps the current visible minute, publish immediately
        # instead of silently moving the post to the next day.
        if target <= now and now.hour == hour and now.minute == minute:
            target = now.replace(microsecond=0) + timedelta(seconds=1)
        elif now >= target:
            target += timedelta(days=1)

        time_public = target.timestamp()
        return time_public

    @staticmethod
    async def get_timestamp_to_time(timestamp) -> str:
        tz = timezone(timedelta(hours=3))
        return datetime.fromtimestamp(timestamp, tz=tz).strftime('%H:%M')

    @staticmethod
    async def conversion_to_moscow_time(timestamp):
        utc_date = datetime.fromtimestamp(timestamp, tz=pytz.utc)
        local_tz = pytz.timezone('Europe/Moscow')
        local_date = utc_date.astimezone(local_tz)
        return local_date.timestamp()

    @logger.catch
    async def save_post(self, call: CallbackQuery, channel_id, info_sender, bot_info) -> None:
        timestamp = datetime.now(pytz.timezone('Europe/Moscow')).timestamp()
        logger.debug(timestamp)
        data = {
            "user_id":  info_sender.id,
            "channel_id": channel_id,
            "bot_username": bot_info,
            "username": info_sender.username if info_sender.username is not None else "",
            "first_name": info_sender.first_name,
            "message_id": call.message.id,
            "chat_id": call.message.chat.id,
            "text_post": "",
            "content_type": call.message.content_type,
            "media_group_id": getattr(call.message, "media_group_id", None),
            "preview_file_id": self._extract_preview_file_id(call.message),
            "preview_file_size": self._extract_preview_file_size(call.message),
            "entities_json": self._extract_entities_json(call.message),
            "timestamp": int(timestamp),
            }
        if call.message.content_type == "text":
            data["text_post"] = call.message.text
        elif call.message.caption is not None:
            data["text_post"] = call.message.caption

        await self._save_sender_record(data)

    @logger.catch
    async def save_incoming_message(self, message: Message, channel_id: int, bot_info: str) -> None:
        await self.save_incoming_message_with_review(
            message=message,
            channel_id=channel_id,
            bot_info=bot_info,
            review_chat_id=None,
            review_message_id=None,
        )

    @logger.catch
    async def save_incoming_message_with_review(
            self,
            message: Message,
            channel_id: int,
            bot_info: str,
            review_chat_id: int | None,
            review_message_id: int | None,
    ) -> None:
        timestamp = datetime.now(pytz.timezone('Europe/Moscow')).timestamp()
        data = {
            "user_id": message.from_user.id,
            "channel_id": channel_id,
            "bot_username": bot_info,
            "username": message.from_user.username if message.from_user.username is not None else "",
            "first_name": message.from_user.first_name,
            "message_id": message.id,
            "chat_id": message.chat.id,
            "text_post": "",
            "content_type": message.content_type,
            "media_group_id": getattr(message, "media_group_id", None),
            "preview_file_id": self._extract_preview_file_id(message),
            "preview_file_size": self._extract_preview_file_size(message),
            "entities_json": self._extract_entities_json(message),
            "review_chat_id": review_chat_id,
            "review_message_id": review_message_id,
            "timestamp": int(timestamp),
        }
        if message.content_type == "text":
            data["text_post"] = message.text
        elif message.caption is not None:
            data["text_post"] = message.caption

        await self._save_sender_record(data)

    async def _save_sender_record(self, data: dict) -> None:
        existing = await self.sender_data.get_first_by_filters(
            channel_id=data["channel_id"],
            bot_username=data["bot_username"],
            message_id=data["message_id"],
            chat_id=data["chat_id"],
        )
        if existing:
            return
        await self.sender_data.add_public_posts(data)

    @logger.catch
    async def save_admin_action(self, call):
        timestamp = datetime.now(pytz.timezone('Europe/Moscow')).timestamp()
        data = {
            "message_id": call.message.id,
            "chat_id": call.message.chat.id,
            # CallbackQuery.from_user is the administrator who pressed the
            # button. Message.from_user is the bot that sent the moderation
            # card, so storing it made every legacy action look bot-owned.
            "admin_id": call.from_user.id,
            "timestamp": int(timestamp),
            "button": call.data.split(";")[0],
        }
        await self.action_admin.add_public_posts(data)

    @staticmethod
    @logger.catch
    async def get_anon(message: Message):
        reply_markup = message.reply_markup.keyboard
        logger.debug(reply_markup)
        anon_markup: InlineKeyboardButton = reply_markup[2][1]
        is_anon = anon_markup.callback_data.split(";")[-1]
        return True if is_anon == "True" else False

    @staticmethod
    def _tme_slug(value: str | int | None) -> str | None:
        text = str(value or "").strip().lower()
        if not text:
            return None
        if text.startswith("@"):
            return text[1:].strip("/") or None
        text = text.removeprefix("https://").removeprefix("http://").removeprefix("www.")
        for prefix in ("t.me/", "telegram.me/"):
            if text.startswith(prefix):
                path = text.removeprefix(prefix).split("?", 1)[0].strip("/")
                if path.startswith("s/"):
                    path = path[2:]
                slug = path.split("/", 1)[0]
                return slug or None
        if "/" not in text and not text.startswith("-"):
            return text
        return None

    @classmethod
    def _is_ignored_channel_link(
        cls,
        url: str | None,
        ignored_channel_ref: str | int | None,
    ) -> bool:
        ignored_slug = cls._tme_slug(ignored_channel_ref)
        url_slug = cls._tme_slug(url)
        if ignored_slug and url_slug and ignored_slug == url_slug:
            return True

        ignored_value = str(ignored_channel_ref or "").strip()
        if ignored_value.startswith("-100") and ignored_value[4:].isdigit():
            ignored_private_id = ignored_value[4:]
            normalized_url = str(url or "").strip().lower()
            normalized_url = normalized_url.removeprefix("https://").removeprefix("http://").removeprefix("www.")
            for prefix in ("t.me/c/", "telegram.me/c/"):
                if normalized_url.startswith(prefix):
                    linked_private_id = normalized_url.removeprefix(prefix).split("/", 1)[0].split("?", 1)[0]
                    return linked_private_id == ignored_private_id
        return False

    @staticmethod
    def _message_text_for_links(message: Message) -> str:
        return message.text or message.caption or ""

    @classmethod
    def _extract_message_links(cls, message: Message) -> list[str]:
        text = cls._message_text_for_links(message)
        links = re.findall(r"(?:https?://|www\.|t\.me/|telegram\.me/)[^\s<>)]+", text, flags=re.IGNORECASE)
        entities = message.entities or message.caption_entities or []
        for entity in entities:
            entity_type = getattr(entity, "type", None)
            if entity_type == "text_link":
                url = getattr(entity, "url", None)
                if url:
                    links.append(url)
            elif entity_type == "url":
                offset = getattr(entity, "offset", 0)
                length = getattr(entity, "length", 0)
                if length:
                    links.append(text[offset:offset + length])

        reply_markup = getattr(message, "reply_markup", None)
        keyboard = (
            getattr(reply_markup, "keyboard", None)
            or getattr(reply_markup, "inline_keyboard", None)
            or []
        )
        for row in keyboard:
            for button in row:
                button_url = button.get("url") if isinstance(button, dict) else getattr(button, "url", None)
                if button_url:
                    links.append(button_url)
                for nested_name in ("login_url", "web_app"):
                    nested = button.get(nested_name) if isinstance(button, dict) else getattr(button, nested_name, None)
                    nested_url = nested.get("url") if isinstance(nested, dict) else getattr(nested, "url", None)
                    if nested_url:
                        links.append(nested_url)
        return links

    @classmethod
    @logger.catch
    async def check_link(
        cls,
        message: Message,
        ignored_channel_ref: str | int | None = None,
        ignored_links: Iterable[str] = (),
    ) -> bool:
        links = cls._extract_message_links(message)
        ignored_links = tuple(ignored_links)
        if any(normalized_ad_link_is_excluded(link, ignored_links) for link in links):
            return False

        return any(
            not cls._is_ignored_channel_link(link, ignored_channel_ref)
            for link in links
        )


def filter_chats(func):
    def wrapper(message: Message):
        if (message.chat.id < 0 or
                message.chat.id == settings.general_admin or
                message.chat.id in settings.moderators):
            return func(message)
    return wrapper


def filter_admin(func):
    def wrapper(message: Message):
        if message.chat.id == settings.general_admin or message.chat.id in settings.moderators:
            return func(message)
    return wrapper
