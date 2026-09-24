from __future__ import annotations

import asyncio
import json
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from loguru import logger
from requests import HTTPError
from telebot.async_telebot import AsyncTeleBot, asyncio_helper
from telebot.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from config import settings
from src.core_database.database import CrudBotAdmins, CrudBotsData, CrudDelayedPosts
from src.editorial.models.enums import ChannelPasteTagRuleMode, ContentFamily, SubmissionStatus
from src.editorial.services.advertising import send_advertising_flow
from src.editorial.services.admin_statistics_export import parse_admin_statistics_month
from src.editorial.services.db_export import DatabaseExportService
from src.editorial.services.legacy_source import LegacyCollectorReader
from src.editorial.services.sql_export import SqlExportService
from src.editorial.services.statistics_export import (
    DEFAULT_STATISTICS_DELTA_DAYS,
    MAX_STATISTICS_DELTA_DAYS,
    validate_statistics_delta_days,
)
from src.editorial.services.telegram_actions import TelegramEditorialActions
from src.confession_publisher import ConfessionPublisherRuntime
from src.telegram_runtime import calculate_telegram_request_limit
from src.panel_markups import (
    build_ad_link_exclusions_panel,
    build_admin_menu,
    build_channel_actions,
    build_channel_history_import_progress_actions,
    build_channel_history_import_start_actions,
    build_channel_paste_tag_actions,
    build_channel_slots_actions,
    build_channels_actions,
    build_confession_channel_actions,
    build_confession_channels_actions,
    build_confession_paste_actions,
    build_confession_paste_delete_confirm,
    build_confessions_menu,
    build_content_actions,
    build_empty_paste_actions,
    build_empty_confession_paste_actions,
    build_extra_panel,
    build_global_paste_tag_rule_actions,
    build_main_panel,
    build_my_channels_actions,
    build_paste_actions,
    build_paste_tag_actions,
    build_profile_actions,
    build_profiles_panel,
    build_tag_actions,
    build_tag_keywords_actions,
    build_tags_actions,
    build_submission_actions,
    build_submission_history_actions,
    build_subbot_menu,
    build_subbot_remove_confirm,
)
from src.utils import filter_admin
from src.worker import SubBot


WEEKDAY_LABELS = {
    0: "Пн",
    1: "Вт",
    2: "Ср",
    3: "Чт",
    4: "Пт",
    5: "Сб",
    6: "Вс",
}


PANEL_PAGE_SIZE = 10


class MasterBot:
    def __init__(self, api_token_bot: str):
        self.delayed_task = None
        self.subscriber_snapshot_task = None
        self.main_polling_task = None
        self.confession_publisher_runtime: ConfessionPublisherRuntime | None = None
        self.bot_info = None
        self.flag_register_push_message = False
        self.user_states: dict[int, dict[str, Any]] = {}

        self.bots_database = CrudBotsData()
        self.delayed_database = CrudDelayedPosts()
        self.bot_admins_database = CrudBotAdmins()
        self.editorial_actions = TelegramEditorialActions()
        self.db_export_service = DatabaseExportService()
        self.sql_export_service = SqlExportService()
        self.legacy_reader = LegacyCollectorReader()

        self.commands = [
            BotCommand("panel", "открыть панель управления"),
            BotCommand("bots", "список подключенных сабботов"),
            BotCommand("add", "добавить саббота: /add <token> @channel"),
            BotCommand("delete", "удалить саббота: /delete @bot @channel"),
            BotCommand("push", "подготовить ручную рассылку"),
        ]

        self.api_token_bot = api_token_bot
        asyncio_helper.proxy = settings.proxies["http"]
        # Bootstrap performs Telegram API calls before run_bot(), so start with
        # a generous limit to avoid creating the shared aiohttp session with a
        # too-small connector.
        self._initial_request_limit = calculate_telegram_request_limit(
            current_subbot_count=0,
            max_subbot_count=settings.max_subbots,
            configured_limit=settings.sup_bot_limit,
            connections_per_bot=settings.telegram_connections_per_bot,
            overhead_connections=settings.telegram_connection_overhead,
        )
        asyncio_helper.REQUEST_LIMIT = self._initial_request_limit
        asyncio_helper.REQUEST_TIMEOUT = settings.telegram_request_timeout_seconds
        self.main_bot = AsyncTeleBot(self.api_token_bot)
        self.bots_work: list[SubBot] = []

        self.chats = settings.moderators
        self.chats.add(settings.general_admin)

        asyncio.create_task(self.__bootstrap())
        self.__setup_handlers()
        logger.info("init bot")

    async def __bootstrap(self) -> None:
        await self.__load_dynamic_admins()
        await self.__setup_bot_info()

    async def __load_dynamic_admins(self) -> None:
        rows = await self.bot_admins_database.get_admins()
        for row in rows:
            settings.moderators.add(row[0])
        self.chats = settings.moderators
        self.chats.add(settings.general_admin)

    async def __setup_bot_info(self) -> None:
        await self.main_bot.set_my_commands(commands=self.commands)
        self.bot_info = await self.main_bot.get_me()

    def _configure_request_limit(self, subbot_count: int) -> int:
        # Long polling for every bot occupies a request slot almost constantly.
        # Keep extra headroom for callback answers, send/edit message operations,
        # and legacy moderation actions so UI interactions do not sit in queue.
        desired_limit = calculate_telegram_request_limit(
            current_subbot_count=subbot_count,
            max_subbot_count=settings.max_subbots,
            configured_limit=settings.sup_bot_limit,
            connections_per_bot=settings.telegram_connections_per_bot,
            overhead_connections=settings.telegram_connection_overhead,
        )
        asyncio_helper.REQUEST_LIMIT = desired_limit
        asyncio_helper.REQUEST_TIMEOUT = settings.telegram_request_timeout_seconds
        return desired_limit

    async def _reset_telegram_session(self) -> None:
        session = getattr(asyncio_helper.session_manager, "session", None)
        if session is None:
            return
        try:
            if not session.closed:
                await session.close()
        except Exception as ex:
            logger.debug("Failed to close shared Telegram aiohttp session: {}", ex)
        finally:
            asyncio_helper.session_manager.session = None

    async def _start_confession_publisher_runtime(self, publisher) -> None:
        current = self.confession_publisher_runtime
        if (
            current is not None
            and current.publisher_id == publisher.id
            and current.bot_api_token == publisher.bot_api_token
            and current.polling_task is not None
            and not current.polling_task.done()
        ):
            return
        if current is not None:
            await current.stop()
        runtime = ConfessionPublisherRuntime(
            publisher_id=publisher.id,
            bot_api_token=publisher.bot_api_token,
            bot_user_id=publisher.bot_user_id,
        )
        await runtime.start()
        self.confession_publisher_runtime = runtime

    @staticmethod
    def _normalize_username(value: str | None) -> str:
        if not value:
            return ""
        return value.strip().lstrip("@").lower()

    def _filter_enabled_subbots(self, bots_lst: list) -> list:
        allowed = {
            self._normalize_username(username)
            for username in settings.enabled_subbot_usernames
            if self._normalize_username(username)
        }
        if not allowed:
            return bots_lst

        filtered = []
        for api_token, bot_username, channel_id, id_row in bots_lst:
            if self._normalize_username(bot_username) in allowed:
                filtered.append((api_token, bot_username, channel_id, id_row))

        logger.info(
            "Enabled subbot filter is active: {}. Selected {} of {} subbots.",
            ", ".join(sorted(f"@{item}" for item in allowed)),
            len(filtered),
            len(bots_lst),
        )
        return filtered

    def _is_general_admin(self, user_id: int) -> bool:
        return user_id == settings.general_admin

    def _is_admin(self, user_id: int) -> bool:
        return self._is_general_admin(user_id) or user_id in settings.moderators

    def _set_user_state(self, user_id: int, action: str, **payload) -> None:
        self.user_states[user_id] = {"action": action, **payload}

    def _clear_user_state(self, user_id: int) -> None:
        self.user_states.pop(user_id, None)

    async def _safe_answer_callback(
        self,
        bot: AsyncTeleBot,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        try:
            await asyncio.wait_for(
                bot.answer_callback_query(
                    callback_query_id=callback_query_id,
                    text=text,
                    show_alert=show_alert,
                ),
                timeout=settings.telegram_request_timeout_seconds,
            )
        except Exception as ex:
            logger.debug("Failed to answer callback {}: {}", callback_query_id, ex)

    async def _answer_panel(self, chat_id: int, text: str | None = None) -> None:
        panel_text = text or (
            "Панель управления.\n\n"
            "Поступившие сообщения: новые сырые сообщения пользователей, которые еще не превращены в пост.\n"
            "Все сообщения: журнал submissions из базы, включая уже обработанные записи.\n"
            "Черновики на review: уже собранные content items, которые ждут финального approve.\n"
            "Пасты: библиотека сохраненных текстов, которые можно переиспользовать."
        )
        await self.main_bot.send_message(
            chat_id=chat_id,
            text=panel_text,
            reply_markup=build_main_panel(self._is_general_admin(chat_id)),
        )

    async def _show_extra_panel(self, chat_id: int) -> None:
        await self.main_bot.send_message(
            chat_id=chat_id,
            text=(
                "Р”РѕРїРѕР»РЅРёС‚РµР»СЊРЅС‹Рµ С„СѓРЅРєС†РёРё.\n\n"
                "Р’С‹РіСЂСѓР·РєР° Р‘Р” СЃРѕР·РґР°С‘С‚ .db-СЃРЅРёРјРѕРє С‚РµРєСѓС‰РµР№ PostgreSQL-Р±Р°Р·С‹, "
                "С‡С‚РѕР±С‹ С„Р°Р№Р» РїРѕС‚РѕРј Р±С‹Р»Рѕ СѓРґРѕР±РЅРѕ РѕС‚РєСЂС‹С‚СЊ Рё СЃРјРѕС‚СЂРµС‚СЊ."
            ),
            reply_markup=build_extra_panel(),
        )
        return
        await self.main_bot.send_message(
            chat_id=chat_id,
            text=(
                "Дополнительные функции.\n\n"
                "Выгрузка БД создаёт SQLite-снимок со старой collector-базой и editorial-таблицами, "
                "чтобы файл потом было удобно открыть и смотреть."
            ),
            reply_markup=build_extra_panel(),
        )

    async def _send_database_export(self, chat_id: int) -> None:
        status_message = await self.main_bot.send_message(
            chat_id,
            "Р“РѕС‚РѕРІР»СЋ РІС‹РіСЂСѓР·РєСѓ Р±Р°Р·С‹ РґР°РЅРЅС‹С…. Р­С‚Рѕ РјРѕР¶РµС‚ Р·Р°РЅСЏС‚СЊ РЅРµСЃРєРѕР»СЊРєРѕ СЃРµРєСѓРЅРґ.",
        )
        export_path = None
        try:
            export_path = await self.db_export_service.export_snapshot()
            with export_path.open("rb") as snapshot_file:
                await self.main_bot.send_document(
                    chat_id=chat_id,
                    document=snapshot_file,
                    visible_file_name=export_path.name,
                    caption=(
                        "РЎРЅРёРјРѕРє Р±Р°Р·С‹ РіРѕС‚РѕРІ.\n"
                        "Р’РЅСѓС‚СЂРё: РІСЃРµ Р°РєС‚СѓР°Р»СЊРЅС‹Рµ С‚Р°Р±Р»РёС†С‹ РїСЂРѕРµРєС‚Р° РёР· С‚РµРєСѓС‰РµР№ PostgreSQL-Р±Р°Р·С‹."
                    ),
                )
        except Exception as ex:
            logger.exception("Failed to export database snapshot")
            await self.main_bot.send_message(chat_id, f"РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРґРіРѕС‚РѕРІРёС‚СЊ РІС‹РіСЂСѓР·РєСѓ Р‘Р”: {ex}")
        finally:
            try:
                await self.main_bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)
            except Exception:
                pass
            if export_path and export_path.exists():
                export_path.unlink(missing_ok=True)
        return
        status_message = await self.main_bot.send_message(
            chat_id,
            "Готовлю выгрузку базы данных. Это может занять несколько секунд.",
        )
        export_path = None
        try:
            export_path = await self.db_export_service.export_snapshot()
            with export_path.open("rb") as snapshot_file:
                await self.main_bot.send_document(
                    chat_id=chat_id,
                    document=snapshot_file,
                    visible_file_name=export_path.name,
                    caption=(
                        "SQLite-снимок готов.\n"
                        "Внутри: исходные legacy-таблицы и editorial-таблицы с префиксом editorial__."
                    ),
                )
        except Exception as ex:
            logger.exception("Failed to export database snapshot")
            await self.main_bot.send_message(chat_id, f"Не удалось подготовить выгрузку БД: {ex}")
        finally:
            try:
                await self.main_bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)
            except Exception:
                pass
            if export_path and export_path.exists():
                export_path.unlink(missing_ok=True)

    async def _show_extra_panel(self, chat_id: int) -> None:
        is_general_admin = self._is_general_admin(chat_id)
        await self.main_bot.send_message(
            chat_id=chat_id,
            text=(
                "\u0414\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0435 "
                "\u0444\u0443\u043d\u043a\u0446\u0438\u0438.\n\n"
                "\u0412\u044b\u0433\u0440\u0443\u0437\u043a\u0430 \u0411\u0414 \u0441\u043e\u0437\u0434\u0430\u0451\u0442 "
                ".db-\u0441\u043d\u0438\u043c\u043e\u043a \u0442\u0435\u043a\u0443\u0449\u0435\u0439 "
                "PostgreSQL-\u0431\u0430\u0437\u044b, \u0447\u0442\u043e\u0431\u044b \u0444\u0430\u0439\u043b "
                "\u043f\u043e\u0442\u043e\u043c \u0431\u044b\u043b\u043e \u0443\u0434\u043e\u0431\u043d\u043e "
                "\u043e\u0442\u043a\u0440\u044b\u0442\u044c \u0438 \u0441\u043c\u043e\u0442\u0440\u0435\u0442\u044c.\n\n"
                + (
                    "\u041a\u043d\u043e\u043f\u043a\u0430 SQL -> CSV \u043f\u043e\u0437\u0432\u043e\u043b\u044f\u0435\u0442 "
                    "\u0433\u0435\u043d\u0430\u0434\u043c\u0438\u043d\u0443 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0442\u044c "
                    "\u043b\u044e\u0431\u043e\u0439 \u043e\u0434\u0438\u043d\u043e\u0447\u043d\u044b\u0439 SQL-\u0437\u0430\u043f\u0440\u043e\u0441 "
                    "\u0438 \u043f\u043e\u043b\u0443\u0447\u0430\u0442\u044c \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 \u0432 CSV."
                    if is_general_admin
                    else
                    "\u041a\u043d\u043e\u043f\u043a\u0430 SQL -> CSV \u0434\u043b\u044f \u043c\u043e\u0434\u0435\u0440\u0430\u0442\u043e\u0440\u0430 "
                    "\u0440\u0430\u0437\u0440\u0435\u0448\u0430\u0435\u0442 \u0442\u043e\u043b\u044c\u043a\u043e SELECT-\u0437\u0430\u043f\u0440\u043e\u0441\u044b."
                )
            ),
            reply_markup=build_extra_panel(is_general_admin=is_general_admin),
        )

    async def _show_ad_link_exclusions_panel(self, chat_id: int) -> None:
        exclusions = await self.editorial_actions.list_ad_link_exclusions()
        header = (
            "Исключения для автоматических рекламных окон.\n\n"
            "Указанная ссылка действует как префикс: например, исключение https://ya.cc "
            "также исключает любые ссылки на страницах ya.cc. Если пост содержит такую "
            "ссылку, часовое рекламное окно создано не будет. Ссылки самих каналов, "
            "которые используются в подписях, "
            "исключаются автоматически и здесь не отображаются.\n\n"
            "Исключённые ссылки:"
        )
        lines = [f"{index}. {row.url}" for index, row in enumerate(exclusions, start=1)]
        if not lines:
            lines = ["Список пока пуст."]

        chunks: list[str] = []
        current_chunk = header
        for line in lines:
            candidate = f"{current_chunk}\n{line}"
            if len(candidate) <= 3900:
                current_chunk = candidate
                continue
            chunks.append(current_chunk)
            current_chunk = line
        chunks.append(current_chunk)

        for chunk in chunks[:-1]:
            await self.main_bot.send_message(chat_id=chat_id, text=chunk)
        await self.main_bot.send_message(
            chat_id=chat_id,
            text=chunks[-1],
            reply_markup=build_ad_link_exclusions_panel(),
        )

    async def _send_database_export(self, chat_id: int) -> None:
        status_message = await self.main_bot.send_message(
            chat_id,
            "\u0413\u043e\u0442\u043e\u0432\u043b\u044e \u0432\u044b\u0433\u0440\u0443\u0437\u043a\u0443 "
            "\u0431\u0430\u0437\u044b \u0434\u0430\u043d\u043d\u044b\u0445. \u042d\u0442\u043e "
            "\u043c\u043e\u0436\u0435\u0442 \u0437\u0430\u043d\u044f\u0442\u044c "
            "\u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u0441\u0435\u043a\u0443\u043d\u0434.",
        )
        export_path = None
        try:
            export_path = await self.db_export_service.export_snapshot()
            with export_path.open("rb") as snapshot_file:
                await self.main_bot.send_document(
                    chat_id=chat_id,
                    document=snapshot_file,
                    visible_file_name=export_path.name,
                    caption=(
                        "\u0421\u043d\u0438\u043c\u043e\u043a \u0431\u0430\u0437\u044b "
                        "\u0433\u043e\u0442\u043e\u0432.\n"
                        "\u0412\u043d\u0443\u0442\u0440\u0438: \u0432\u0441\u0435 "
                        "\u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0435 "
                        "\u0442\u0430\u0431\u043b\u0438\u0446\u044b \u043f\u0440\u043e\u0435\u043a\u0442\u0430 "
                        "\u0438\u0437 \u0442\u0435\u043a\u0443\u0449\u0435\u0439 PostgreSQL-\u0431\u0430\u0437\u044b."
                    ),
                )
        except Exception as ex:
            logger.exception("Failed to export database snapshot")
            await self.main_bot.send_message(
                chat_id,
                f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c "
                f"\u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u0438\u0442\u044c "
                f"\u0432\u044b\u0433\u0440\u0443\u0437\u043a\u0443 \u0411\u0414: {ex}",
            )
        finally:
            try:
                await self.main_bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)
            except Exception:
                pass
            if export_path and export_path.exists():
                export_path.unlink(missing_ok=True)

    async def _send_statistics_export(self, chat_id: int, delta_days: int = DEFAULT_STATISTICS_DELTA_DAYS) -> None:
        delta_days = validate_statistics_delta_days(delta_days)
        status_message = await self.main_bot.send_message(
            chat_id,
            f"Готовлю выгрузку статистики с дельтой за {delta_days} дн.",
        )
        export_path = None
        try:
            channels = await self.editorial_actions.list_channels()
            channel_titles = {
                channel.id: self._channel_title_from_runtime(channel.tg_channel_id) or channel.title
                for channel in channels
            }
            channel_tags = {
                channel.id: self._channel_label_from_runtime(channel.tg_channel_id) or channel.short_code
                for channel in channels
            }
            export_path = await self.editorial_actions.export_channel_statistics(
                channel_titles=channel_titles,
                channel_tags=channel_tags,
                delta_days=delta_days,
            )
            with export_path.open("rb") as export_file:
                await self.main_bot.send_document(
                    chat_id=chat_id,
                    document=export_file,
                    visible_file_name=export_path.name,
                    caption=(
                        "Статистика каналов готова.\n"
                        f"Дельта: за {delta_days} дн.\n"
                        "Подписчики берутся из ежедневных снимков; "
                        "сообщения — по времени поступления в предложку."
                    ),
                )
        except Exception as ex:
            logger.exception("Failed to export channel statistics")
            await self.main_bot.send_message(chat_id, f"Не удалось подготовить статистику: {ex}")
        finally:
            try:
                await self.main_bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)
            except Exception:
                pass
            if export_path and export_path.exists():
                export_path.unlink(missing_ok=True)

    async def _admin_statistics_labels(self) -> dict[int, str]:
        async def resolve(admin_id: int) -> tuple[int, str]:
            try:
                chat = await self.main_bot.get_chat(admin_id)
            except Exception:
                return admin_id, str(admin_id)
            if getattr(chat, "username", None):
                return admin_id, f"@{chat.username}"
            full_name = " ".join(
                part
                for part in (getattr(chat, "first_name", None), getattr(chat, "last_name", None))
                if part
            ).strip()
            return admin_id, full_name or str(admin_id)

        admin_ids = sorted({int(item) for item in self.chats if item})
        return dict(await asyncio.gather(*(resolve(admin_id) for admin_id in admin_ids)))

    async def _send_admin_statistics_export(self, chat_id: int, month) -> None:
        status_message = await self.main_bot.send_message(
            chat_id,
            f"Готовлю статистику по администраторам за {month:%m.%Y}.",
        )
        export_path = None
        try:
            export_path = await self.editorial_actions.export_admin_statistics(
                month=month,
                admin_labels=await self._admin_statistics_labels(),
            )
            with export_path.open("rb") as export_file:
                await self.main_bot.send_document(
                    chat_id=chat_id,
                    document=export_file,
                    visible_file_name=export_path.name,
                    caption=(
                        f"Статистика администраторов за {month:%m.%Y}.\n"
                        "Сводка содержит итоговые количества, а лист «Детализация» — "
                        "авторов и сохранённые тексты предложек. Повторные нажатия и отменённые решения не считаются."
                    ),
                )
        except Exception as ex:
            logger.exception("Failed to export admin statistics")
            await self.main_bot.send_message(chat_id, f"Не удалось подготовить статистику администраторов: {ex}")
        finally:
            try:
                await self.main_bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)
            except Exception:
                pass
            if export_path and export_path.exists():
                export_path.unlink(missing_ok=True)

    async def _send_sql_export_prompt(self, chat_id: int, allow_mutating: bool) -> None:
        self._set_user_state(chat_id, "await_sql_export", allow_mutating=allow_mutating)
        if allow_mutating:
            prompt_text = (
                "\u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u043e\u0434\u0438\u043d SQL-\u0437\u0430\u043f\u0440\u043e\u0441.\n"
                "\u0414\u043b\u044f \u0433\u0435\u043d\u0430\u0434\u043c\u0438\u043d\u0430 \u0440\u0430\u0437\u0440\u0435\u0448\u0451\u043d "
                "\u043b\u044e\u0431\u043e\u0439 \u043e\u0434\u0438\u043d\u043e\u0447\u043d\u044b\u0439 SQL.\n"
                "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 \u043f\u0440\u0438\u0434\u0451\u0442 CSV-\u0444\u0430\u0439\u043b\u043e\u043c."
            )
        else:
            prompt_text = (
                "\u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u043e\u0434\u0438\u043d SELECT-\u0437\u0430\u043f\u0440\u043e\u0441.\n"
                "\u0414\u043b\u044f \u043c\u043e\u0434\u0435\u0440\u0430\u0442\u043e\u0440\u0430 \u0440\u0430\u0437\u0440\u0435\u0448\u0451\u043d "
                "\u0442\u043e\u043b\u044c\u043a\u043e SELECT.\n"
                "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 \u043f\u0440\u0438\u0434\u0451\u0442 CSV-\u0444\u0430\u0439\u043b\u043e\u043c."
            )
        await self.main_bot.send_message(chat_id, prompt_text)

    async def _send_sql_export_result(self, chat_id: int, query: str, allow_mutating: bool) -> None:
        status_message = await self.main_bot.send_message(
            chat_id,
            "\u0412\u044b\u043f\u043e\u043b\u043d\u044f\u044e SQL-\u0437\u0430\u043f\u0440\u043e\u0441 "
            "\u0438 \u0433\u043e\u0442\u043e\u0432\u043b\u044e CSV. \u042d\u0442\u043e \u043c\u043e\u0436\u0435\u0442 "
            "\u0437\u0430\u043d\u044f\u0442\u044c \u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e "
            "\u0441\u0435\u043a\u0443\u043d\u0434.",
        )
        export_path = None
        try:
            result = await self.sql_export_service.export_query(query=query, allow_mutating=allow_mutating)
            export_path = result.path
            with export_path.open("rb") as export_file:
                await self.main_bot.send_document(
                    chat_id=chat_id,
                    document=export_file,
                    visible_file_name=export_path.name,
                    caption=(
                        f"SQL -> CSV готов.\n"
                        f"Тип запроса: {result.statement_type}\n"
                        f"Строк в файле: {result.rows_written}"
                    ),
                )
        except Exception as ex:
            logger.exception("Failed to export SQL query result")
            await self.main_bot.send_message(
                chat_id,
                f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c "
                f"\u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u044c SQL: {ex}",
            )
        finally:
            try:
                await self.main_bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)
            except Exception:
                pass
            if export_path and export_path.exists():
                export_path.unlink(missing_ok=True)

    async def _list_subbots_text(self) -> str:
        all_info = await self.bots_database.get_bots_info()
        return await self._format_subbots_text(all_info)

    async def _format_subbots_text(self, all_info: list, page: int | None = None) -> str:
        if not all_info:
            return "Сабботы пока не подключены."

        page_info = ""
        page_items = all_info
        if page is not None:
            page = self._normalize_panel_page(page, len(all_info))
            start = page * PANEL_PAGE_SIZE
            end = min(start + PANEL_PAGE_SIZE, len(all_info))
            page_items = all_info[start:end]
            page_info = f" ({start + 1}-{end} \u0438\u0437 {len(all_info)})"

        lines = [f"Подключенные сабботы:{page_info}"]
        async with aiohttp.ClientSession() as session:
            for api_token, bot_username, channel_id, _row_id in page_items:
                channel_username = None
                try:
                    channel_username = await self._fetch_channel_username(session, api_token, channel_id)
                except Exception as ex:
                    logger.error(ex)
                lines.append(
                    f"@{bot_username} -> {('@' + channel_username) if channel_username else channel_id}"
                )
        return "\n".join(lines)

    @staticmethod
    def _normalize_panel_page(page: int, total_items: int) -> int:
        if total_items <= 0:
            return 0
        max_page = (total_items - 1) // PANEL_PAGE_SIZE
        return min(max(page, 0), max_page)

    @staticmethod
    def _parse_panel_page(data: str) -> int:
        parts = data.split(":")
        if len(parts) < 3:
            return 0
        try:
            return max(int(parts[2]), 0)
        except ValueError:
            return 0

    @staticmethod
    def _page_bounds(page: int, total_items: int) -> tuple[int, int, bool, bool]:
        if total_items <= 0:
            return 0, 0, False, False
        page = MasterBot._normalize_panel_page(page, total_items)
        start = page * PANEL_PAGE_SIZE
        end = min(start + PANEL_PAGE_SIZE, total_items)
        return start, end, page > 0, end < total_items

    async def _fetch_channel_username(
        self,
        session: aiohttp.ClientSession,
        api_token: str,
        channel_id: int,
    ) -> str | None:
        url = f"https://api.telegram.org/bot{api_token}/getchat?chat_id={channel_id}"
        request_kwargs = {}
        if settings.proxies["http"]:
            request_kwargs["proxy"] = settings.proxies["http"]
        async with session.get(url, **request_kwargs) as response:
            result = await response.json()
            if not result["ok"]:
                raise HTTPError(result)
            return result["result"].get("username")

    async def _normalize_channel_username(self, channel_username: str) -> str:
        if "https" in channel_username:
            channel_username = "@" + channel_username.split("/")[-1]
        if "@" not in channel_username:
            channel_username = "@" + channel_username
        return channel_username

    async def _add_subbot_from_values(
        self,
        api_token: str,
        channel_username: str | None = None,
        *,
        channel_tg_id: int | None = None,
    ) -> tuple[str, int | None]:
        if channel_tg_id is None:
            if not channel_username:
                return "Не указан канал для саббота.", None
            channel_username = await self._normalize_channel_username(channel_username)
            try:
                channel_chat = await self.main_bot.get_chat(channel_username)
                channel_id = int(channel_chat.id)
            except Exception:
                logger.error("channel not found: {}", channel_username)
                return f"Канал {channel_username} не найден.", None
        else:
            channel_id = int(channel_tg_id)
            channel_username = str(channel_id)

        bot = await SubBot.create(
            main_bot_username=self.bot_info.username,
            api_token_bot=api_token,
            channel_username=channel_username,
            hello_msg=settings.hello_msg,
            ban_usr_msg=settings.ban_msg,
            send_post_msg=settings.send_post_msg,
            callback_adv_action=self.callback_adv_send_message_v2,
            callback_new_submission=self.callback_new_submission_notification,
        )
        if bot.bot_info is None:
            return "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u0438\u043d\u0444\u043e \u043e \u0441\u0430\u0431\u0431\u043e\u0442\u0435. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 token.", None

        channel_id = int(getattr(bot, "channel_id", channel_id))
        bot_username = bot.bot_info.username.replace("@", "")
        main_bot_username = (self.bot_info.username if self.bot_info else "").replace("@", "")
        if bot_username == main_bot_username:
            return (
                f"Token принадлежит general-боту @{bot_username}. "
                "Его нельзя привязывать как саббота: возьмите token именно отдельного бота-предложки из BotFather.",
                None,
            )

        confession_publisher = await self.editorial_actions.get_active_confession_publisher()
        if confession_publisher is not None and bot.bot_info.id == confession_publisher.bot_user_id:
            return (
                "Бот паст признавашек нельзя одновременно использовать как предложку. "
                "Создайте отдельного бота в BotFather.",
                None,
            )

        for item in await self.bots_database.get_bots_info():
            existing_username = str(item[1]).replace("@", "")
            existing_channel_id = int(item[2])
            if existing_channel_id == channel_id and existing_username != bot_username:
                return (
                    f"К этому каналу уже привязан саббот @{existing_username}. "
                    "Сначала удалите старую привязку в разделе «Сабботы».",
                    None,
                )
            if existing_username == bot_username and existing_channel_id != channel_id:
                return "\u042d\u0442\u043e\u0442 \u0441\u0430\u0431\u0431\u043e\u0442 \u0443\u0436\u0435 \u043f\u0440\u0438\u0432\u044f\u0437\u0430\u043d \u043a \u0434\u0440\u0443\u0433\u043e\u043c\u0443 \u043a\u0430\u043d\u0430\u043b\u0443.", None
            if existing_username != bot_username:
                continue
            editorial_channel = await self.editorial_actions.ensure_channel_for_tg_channel_id(channel_id)
            if not self._is_subbot_running(bot_username, channel_id):
                self._configure_request_limit(len(self.bots_work) + 1)
                await bot.run_bot()
                self.bots_work.append(bot)
            try:
                await self.editorial_actions.sync_channel_profiles(channel_id=editorial_channel.id)
            except Exception as exc:
                logger.warning(
                    "Existing subbot @{} was started, but profile sync for channel {} failed: {}",
                    bot_username,
                    editorial_channel.id,
                    exc,
                )
            return "\u041f\u0440\u0438\u0432\u044f\u0437\u043a\u0430 \u0441\u0430\u0431\u0431\u043e\u0442\u0430 \u0443\u0436\u0435 \u0431\u044b\u043b\u0430 \u0432 \u0431\u0430\u0437\u0435; \u043a\u0430\u043d\u0430\u043b \u0440\u0435\u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u043d.", editorial_channel.id
        is_admin = await bot.check_admin(channel_id)
        if not is_admin:
            return (
                f"Token принадлежит @{bot_username}, но Telegram не видит этого бота админом канала {channel_username}. "
                f"Добавьте именно @{bot_username} в администраторы канала и попробуйте ещё раз.",
                None,
            )

        await self.bots_database.add_bots_info(
            {
                "channel_id": channel_id,
                "bot_username": bot_username,
                "bot_api_token": api_token,
            }
        )
        self._configure_request_limit(len(self.bots_work) + 1)
        await bot.run_bot()
        self.bots_work.append(bot)
        editorial_channel = await self.editorial_actions.ensure_channel_for_tg_channel_id(channel_id)
        try:
            await self.editorial_actions.sync_channel_profiles(channel_id=editorial_channel.id)
        except Exception as exc:
            logger.warning(
                "Subbot @{} was connected, but profile sync for channel {} failed: {}",
                bot_username,
                editorial_channel.id,
                exc,
            )
        return f"Саббот {bot.bot_info.username} подключен к {channel_username}.", editorial_channel.id

    async def _remove_subbot_from_values(self, username_bot: str, channel_id: int) -> str:
        await self.bots_database.delete_bots_info(
            {
                "bot_username": username_bot.replace("@", ""),
                "channel_id": channel_id,
            }
        )
        index_bot = -1
        for idx in range(len(self.bots_work)):
            username = (await self.bots_work[idx].getter_name()).replace("@", "")
            if username == username_bot.replace("@", ""):
                index_bot = idx
                try:
                    await asyncio.wait_for(self.bots_work[idx].stop_bot(), timeout=5)
                except asyncio.TimeoutError:
                    logger.warning("Timed out while stopping subbot {}", username_bot)
                except Exception as ex:
                    logger.error("Failed to stop subbot {} cleanly: {}", username_bot, ex)
                break

        if index_bot != -1:
            del self.bots_work[index_bot]
        await self.editorial_actions.deactivate_channel_by_tg_channel_id(channel_id)
        return f"Саббот {username_bot} отвязан."

    async def _get_dynamic_moderators(self) -> list[int]:
        rows = await self.bot_admins_database.get_admins()
        return sorted(row[0] for row in rows)

    async def _show_admins_menu(self, chat_id: int) -> None:
        dynamic_admins = await self._get_dynamic_moderators()
        text = "Динамические модераторы:\n"
        text += "\n".join(map(str, dynamic_admins)) if dynamic_admins else "Пока никого нет."
        await self.main_bot.send_message(chat_id, text, reply_markup=build_admin_menu(dynamic_admins))

    async def _show_subbots_menu(self, chat_id: int, page: int = 0) -> None:
        bots = await self.bots_database.get_bots_info()
        page = self._normalize_panel_page(page, len(bots))
        start, end, has_previous, has_next = self._page_bounds(page, len(bots))
        page_bots = bots[start:end]
        buttons = [(item[1].replace("@", ""), item[2]) for item in page_bots]
        await self.main_bot.send_message(
            chat_id,
            await self._format_subbots_text(bots, page=page),
            reply_markup=build_subbot_menu(
                buttons,
                page=page,
                has_previous=has_previous,
                has_next=has_next,
            ),
        )

    def _is_subbot_running(self, username_bot: str, channel_id: int | None = None) -> bool:
        target_username = username_bot.replace("@", "")
        for bot in self.bots_work:
            bot_info = getattr(bot, "bot_info", None)
            running_username = (getattr(bot_info, "username", "") or "").replace("@", "")
            if running_username != target_username:
                continue
            if channel_id is None or getattr(bot, "channel_id", None) == channel_id:
                return True
        return False

    def _running_subbot_for_channel(self, tg_channel_id: int) -> SubBot | None:
        return next(
            (
                bot
                for bot in self.bots_work
                if int(getattr(bot, "channel_id", 0)) == int(tg_channel_id)
            ),
            None,
        )

    def _channel_label_from_runtime(self, tg_channel_id: int) -> str | None:
        for bot in self.bots_work:
            if getattr(bot, "channel_id", None) == tg_channel_id:
                return getattr(bot, "channel_username", None)
        return None

    def _channel_title_from_runtime(self, tg_channel_id: int) -> str | None:
        for bot in self.bots_work:
            if getattr(bot, "channel_id", None) == tg_channel_id:
                return getattr(bot, "channel_title", None)
        return None

    @staticmethod
    def _compose_channel_display_label(title: str | None, tag: str | None, short_code: str | None) -> str:
        clean_title = (title or "").strip()
        clean_tag = (tag or "").strip()
        clean_short_code = (short_code or "").strip()

        if clean_title and clean_tag:
            return f"{clean_title} {clean_tag}"
        if clean_title:
            return clean_title
        if clean_tag:
            return clean_tag
        if clean_short_code:
            return clean_short_code
        return "Канал"

    async def _get_channel_label(self, editorial_channel_id: int) -> str:
        channel = await self.editorial_actions.get_channel(editorial_channel_id)
        if channel is None:
            return f"Канал #{editorial_channel_id}"
        runtime_title = self._channel_title_from_runtime(channel.tg_channel_id)
        runtime_label = self._channel_label_from_runtime(channel.tg_channel_id)
        return self._compose_channel_display_label(runtime_title or channel.title, runtime_label, channel.short_code)

    def _weekday_label(self, weekday: int) -> str:
        return WEEKDAY_LABELS.get(weekday, str(weekday))

    @staticmethod
    def _is_time_token(value: str) -> bool:
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            return False
        return True

    def _parse_slot_input(self, raw_value: str) -> tuple[list[int], list[str]]:
        text = raw_value.strip()
        if not text:
            raise ValueError("Пустой ввод")

        parts = text.split()
        if self._is_time_token(parts[0]):
            days_token = "all"
            time_tokens = parts
        else:
            days_token = parts[0]
            time_tokens = parts[1:]

        if not time_tokens:
            raise ValueError("После дней недели укажите хотя бы одно время в формате HH:MM")

        slot_times: list[str] = []
        for time_token in time_tokens:
            try:
                datetime.strptime(time_token, "%H:%M")
            except ValueError as exc:
                raise ValueError(f"Время '{time_token}' должно быть в формате HH:MM") from exc
            slot_times.append(time_token)

        if days_token.lower() in {"all", "*"}:
            weekdays = [0, 1, 2, 3, 4, 5, 6]
        else:
            weekdays = []
            for item in days_token.split(","):
                item = item.strip()
                if not item:
                    continue
                weekday = int(item)
                if weekday < 0 or weekday > 6:
                    raise ValueError("Дни недели должны быть числами от 0 до 6")
                weekdays.append(weekday)
            if not weekdays:
                raise ValueError("Не удалось распознать дни недели")

        return sorted(set(weekdays)), slot_times

    def _parse_channel_setting_input(self, raw_value: str) -> tuple[str, str]:
        text = raw_value.strip()
        if not text:
            raise ValueError("Пустой ввод")

        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError("Нужен формат: <параметр> <значение>")
        return parts[0].strip(), parts[1].strip()

    def _parse_profile_setting_input(self, raw_value: str, profile_slug: str | None = None) -> tuple[str, str, str]:
        text = raw_value.strip()
        if not text:
            raise ValueError("Empty input")

        if profile_slug is not None:
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                raise ValueError("Expected format: <field> <value>")
            return profile_slug, parts[0].strip(), parts[1].strip()

        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            raise ValueError("Expected format: <profile_slug> <field> <value>")
        return parts[0].strip(), parts[1].strip(), parts[2].strip()

    @staticmethod
    def _parse_profile_create_input(raw_value: str) -> tuple[str, str, int, int | None]:
        parts = [part.strip() for part in raw_value.strip().split("::")]
        if len(parts) != 4:
            raise ValueError("Expected format: slug :: title :: min_subscribers :: max_subscribers")
        slug, title, min_raw, max_raw = parts
        if not slug or not title:
            raise ValueError("Profile slug and title are required")
        try:
            min_subscribers = int(min_raw)
        except ValueError as exc:
            raise ValueError("min_subscribers must be an integer") from exc
        max_clean = max_raw.lower()
        if max_clean in {"none", "null", "-", "no", "inf", "infinity"}:
            max_subscribers = None
        else:
            try:
                max_subscribers = int(max_raw)
            except ValueError as exc:
                raise ValueError("max_subscribers must be an integer or none") from exc
        return slug, title, min_subscribers, max_subscribers

    @staticmethod
    def _parse_channel_id_list(raw_value: str, available_channel_ids: set[int]) -> list[int]:
        text = raw_value.strip().lower()
        if not text:
            raise ValueError("Empty channel list")
        if text == "all":
            return sorted(available_channel_ids)

        channel_ids: list[int] = []
        normalized = text.replace(",", " ")
        for token in normalized.split():
            if "-" in token:
                left, right = token.split("-", 1)
                if not left.isdigit() or not right.isdigit():
                    raise ValueError(f"Bad range '{token}'")
                start, end = int(left), int(right)
                if start > end:
                    start, end = end, start
                channel_ids.extend(range(start, end + 1))
                continue
            if not token.isdigit():
                raise ValueError(f"Bad channel id '{token}'")
            channel_ids.append(int(token))

        unique_channel_ids = list(dict.fromkeys(channel_ids))
        missing = [channel_id for channel_id in unique_channel_ids if channel_id not in available_channel_ids]
        if missing:
            raise ValueError(f"Channels not found: {', '.join(map(str, missing[:20]))}")
        return unique_channel_ids

    def _parse_ad_blackout_input(self, raw_value: str) -> tuple[int, str, str]:
        text = raw_value.strip()
        if not text:
            raise ValueError("Пустой ввод.")

        parts = text.split()
        if len(parts) != 3:
            raise ValueError("Нужен формат: <день месяца> <с HH:MM> <до HH:MM>.")

        try:
            day_of_month = int(parts[0])
        except ValueError as exc:
            raise ValueError("День месяца должен быть числом.") from exc
        if day_of_month < 1 or day_of_month > 31:
            raise ValueError("День месяца должен быть от 1 до 31.")

        for time_value in parts[1:]:
            try:
                datetime.strptime(time_value, "%H:%M")
            except ValueError as exc:
                raise ValueError(f"Время '{time_value}' должно быть в формате HH:MM.") from exc

        return day_of_month, parts[1], parts[2]

    async def _send_channel_settings_prompt(self, chat_id: int, channel_id: int) -> None:
        await self.editorial_actions.sync_channel_activity_from_bindings()
        channel = await self.editorial_actions.get_channel(channel_id)
        if channel is None:
            await self.main_bot.send_message(chat_id, f"РљР°РЅР°Р» {channel_id} РЅРµ РЅР°Р№РґРµРЅ.")
            return
        if not channel.is_active:
            await self.main_bot.send_message(chat_id, "\u041a\u0430\u043d\u0430\u043b \u043e\u0442\u0432\u044f\u0437\u0430\u043d \u0438 \u0441\u043a\u0440\u044b\u0442 \u0438\u0437 \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0439 \u043f\u0430\u043d\u0435\u043b\u0438.")
            return

        label = await self._get_channel_label(channel_id)
        settings_snapshot = await self.editorial_actions.get_channel_settings_snapshot(channel_id)
        lines = [
            f"Изменение параметров для {label}.",
            "",
            "Отправьте одну строку в формате:",
            "same_tag_cooldown_hours 24",
            "",
            "Для булевых полей используйте только true/false, yes/no, on/off или да/нет.",
            "",
            "Текущие доступные параметры:",
        ]
        lines.extend(
            f"{field_name} = {self._format_channel_setting_value(value)}"
            for field_name, value in settings_snapshot
        )
        await self.main_bot.send_message(chat_id, "\n".join(lines))

    async def _show_profiles_menu(self, chat_id: int) -> None:
        profiles = await self.editorial_actions.list_channel_setting_profiles(include_inactive=True)
        lines = [
            "Профили каналов.",
            "",
            "Выберите профиль, чтобы настроить его или выставить каналам.",
        ]
        profile_buttons = []
        for profile in profiles:
            max_subs = profile.max_subscribers if profile.max_subscribers is not None else "none"
            lines.append(
                f"{profile.slug}: {profile.min_subscribers}-{max_subs}, "
                f"min_slots={profile.min_slots_per_day}, posts={profile.max_posts_per_day}, "
                f"pastes={profile.max_paste_per_day}"
            )
            profile_buttons.append((profile.slug, profile.title, profile.is_active))
        await self.main_bot.send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=build_profiles_panel(profile_buttons),
        )

    async def _show_profile_card(self, chat_id: int, profile_slug: str) -> None:
        profiles = await self.editorial_actions.list_channel_setting_profiles(include_inactive=True)
        profile = next((item for item in profiles if item.slug == profile_slug), None)
        if profile is None:
            await self.main_bot.send_message(chat_id, f"Profile '{profile_slug}' not found.")
            await self._show_profiles_menu(chat_id)
            return
        await self.main_bot.send_message(
            chat_id,
            "\n".join(self._format_profile_lines(profile)),
            reply_markup=build_profile_actions(profile.slug),
        )

    def _format_profile_lines(self, profile) -> list[str]:
        max_subs = profile.max_subscribers if profile.max_subscribers is not None else "none"
        state = "true" if profile.is_active else "false"
        return [
            f"Профиль: {profile.title} ({profile.slug})",
            f"is_active = {state}",
            f"subscribers = {profile.min_subscribers}-{max_subs}",
            f"priority = {profile.priority}",
            "",
            f"min_gap_minutes = {profile.min_gap_minutes}",
            f"slot_jitter_minutes = {profile.slot_jitter_minutes}",
            f"auto_slots_enabled = {self._format_channel_setting_value(profile.auto_slots_enabled)}",
            f"auto_slots_plan_time = {self._format_channel_setting_value(profile.auto_slots_plan_time)}",
            f"auto_slots_window_start = {self._format_channel_setting_value(profile.auto_slots_window_start)}",
            f"auto_slots_window_end = {self._format_channel_setting_value(profile.auto_slots_window_end)}",
            f"auto_slots_replace_manual = {self._format_channel_setting_value(profile.auto_slots_replace_manual)}",
            f"min_slots_per_day = {profile.min_slots_per_day}",
            f"max_posts_per_day = {profile.max_posts_per_day}",
            f"max_generated_per_day = {profile.max_generated_per_day}",
            f"max_paste_per_day = {profile.max_paste_per_day}",
            f"same_tag_cooldown_hours = {profile.same_tag_cooldown_hours}",
            f"same_template_cooldown_hours = {profile.same_template_cooldown_hours}",
            f"same_paste_cooldown_days = {profile.same_paste_cooldown_days}",
            f"min_ready_queue = {profile.min_ready_queue}",
            f"prefer_real_ratio = {profile.prefer_real_ratio}",
            f"allow_generated = {self._format_channel_setting_value(profile.allow_generated)}",
            f"allow_pastes = {self._format_channel_setting_value(profile.allow_pastes)}",
        ]

    async def _send_profile_settings_prompt(self, chat_id: int, profile_slug: str) -> None:
        profiles = await self.editorial_actions.list_channel_setting_profiles(include_inactive=True)
        profile = next((item for item in profiles if item.slug == profile_slug), None)
        if profile is None:
            await self.main_bot.send_message(chat_id, f"Profile '{profile_slug}' not found.")
            await self._show_profiles_menu(chat_id)
            return
        lines = [
            f"Настройка профиля {profile_slug}.",
            "",
            "Текущие параметры:",
            *self._format_profile_lines(profile),
            "",
            "Отправьте строку:",
            "max_posts_per_day 8",
            "auto_slots_window_start 10:00",
            "max_subscribers none",
            "",
            "Можно менять:",
            "title, min_subscribers, max_subscribers, priority, is_active",
            "min_gap_minutes, slot_jitter_minutes, auto_slots_enabled",
            "auto_slots_plan_time, auto_slots_window_start, auto_slots_window_end",
            "min_slots_per_day, max_posts_per_day, max_generated_per_day, max_paste_per_day",
            "same_tag_cooldown_hours, same_template_cooldown_hours, same_paste_cooldown_days",
            "min_ready_queue, prefer_real_ratio, allow_generated, allow_pastes",
        ]
        self._set_user_state(chat_id, "await_profile_setting_update", profile_slug=profile_slug)
        await self.main_bot.send_message(chat_id, "\n".join(lines))

    async def _send_profile_assign_channels_prompt(self, chat_id: int, profile_slug: str) -> None:
        channels = await self.editorial_actions.list_profile_channels()
        lines = [
            f"Выставление профиля {profile_slug}.",
            "",
            "Отправьте номера каналов:",
            "1,2,3",
            "1-20",
            "all",
            "",
            f"Активных каналов: {len(channels)}",
            "Используются id из разделов «Каналы и слоты» и «Признавашки → Паблики».",
            "",
            "Канал будет переведен в ручной режим: settings_profile_auto_enabled=false.",
        ]
        self._set_user_state(chat_id, "await_profile_assign_channels", profile_slug=profile_slug)
        await self.main_bot.send_message(chat_id, "\n".join(lines))

    async def _send_profile_create_prompt(self, chat_id: int) -> None:
        self._set_user_state(chat_id, "await_profile_create")
        await self.main_bot.send_message(
            chat_id,
            "Создание профиля.\n\n"
            "Отправьте строку:\n"
            "slug :: title :: min_subscribers :: max_subscribers\n\n"
            "Примеры:\n"
            "small :: Small channels :: 0 :: 49\n"
            "huge :: Huge channels :: 10000 :: none",
        )

    async def _send_channel_history_import_offer(self, chat_id: int, channel_id: int) -> None:
        label = await self._get_channel_label(channel_id)
        await self.main_bot.send_message(
            chat_id,
            (
                f"Для {label} можно импортировать недавнюю историю канала.\n\n"
                "Если в канале уже публиковались тексты, которые совпадают с пастами из базы, "
                "бот запомнит это и не будет быстро переиспользовать такие пасты заново."
            ),
            reply_markup=build_channel_history_import_start_actions(channel_id),
        )

    @staticmethod
    def _extract_forwarded_channel_payload(message: Message) -> tuple[int | None, int | None, datetime | None]:
        source_chat_id = None
        source_message_id = getattr(message, "forward_from_message_id", None)
        original_published_at = None

        forward_from_chat = getattr(message, "forward_from_chat", None)
        if forward_from_chat is not None:
            source_chat_id = getattr(forward_from_chat, "id", None)

        forward_date = getattr(message, "forward_date", None)
        if isinstance(forward_date, datetime):
            original_published_at = forward_date if forward_date.tzinfo else forward_date.replace(tzinfo=timezone.utc)
        elif isinstance(forward_date, int):
            original_published_at = datetime.fromtimestamp(forward_date, tz=timezone.utc)

        forward_origin = getattr(message, "forward_origin", None)
        if forward_origin is not None:
            origin_chat = getattr(forward_origin, "chat", None)
            if origin_chat is not None and source_chat_id is None:
                source_chat_id = getattr(origin_chat, "id", None)
            origin_message_id = getattr(forward_origin, "message_id", None)
            if source_message_id is None and origin_message_id is not None:
                source_message_id = origin_message_id
            origin_date = getattr(forward_origin, "date", None)
            if original_published_at is None:
                if isinstance(origin_date, datetime):
                    original_published_at = origin_date if origin_date.tzinfo else origin_date.replace(tzinfo=timezone.utc)
                elif isinstance(origin_date, int):
                    original_published_at = datetime.fromtimestamp(origin_date, tz=timezone.utc)

        return source_chat_id, source_message_id, original_published_at

    def _channel_panel_entry(self, channel) -> tuple[int, str, str]:
        runtime_title = self._channel_title_from_runtime(channel.tg_channel_id)
        runtime_label = self._channel_label_from_runtime(channel.tg_channel_id)
        label = self._compose_channel_display_label(
            runtime_title or channel.title,
            runtime_label,
            channel.short_code,
        )
        search_text = " ".join(
            str(value)
            for value in (
                channel.id,
                channel.tg_channel_id,
                channel.title,
                channel.short_code,
                runtime_title,
                runtime_label,
                label,
            )
            if value
        ).lower()
        return channel.id, label, search_text

    async def _show_channel_search_results(self, chat_id: int, query: str) -> None:
        channels = await self.editorial_actions.list_channels()
        needle = query.strip().lower().lstrip("@")
        if not needle:
            await self._show_channels_menu(chat_id)
            return

        matches = []
        for channel in channels:
            channel_id, label, search_text = self._channel_panel_entry(channel)
            if needle in search_text.lstrip("@"):
                matches.append((channel_id, label))

        if not matches:
            self._set_user_state(chat_id, "await_channel_jump")
            await self.main_bot.send_message(
                chat_id,
                f"Каналы по запросу '{query}' не найдены. Отправьте номер канала или часть тега/названия.",
                reply_markup=build_channels_actions([], page=0, has_previous=False, has_next=False),
            )
            return

        visible_matches = matches[:PANEL_PAGE_SIZE]
        lines = [f"Каналы по запросу '{query}' ({len(visible_matches)} из {len(matches)}):"]
        lines.extend(f"{channel_id}. {label}" for channel_id, label in visible_matches)
        if len(matches) > PANEL_PAGE_SIZE:
            lines.append("")
            lines.append("Найдено больше 10 каналов, уточните запрос текстом.")

        self._set_user_state(chat_id, "await_channel_jump")
        await self.main_bot.send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=build_channels_actions(
                visible_matches,
                page=0,
                has_previous=False,
                has_next=False,
            ),
        )

    async def _show_channels_menu_for_input(self, chat_id: int, query: str) -> None:
        text = query.strip()
        if text.isdigit():
            channels = await self.editorial_actions.list_channels()
            if not channels:
                await self._show_channels_menu(chat_id)
                return
            target_id = int(text)
            index = next((idx for idx, channel in enumerate(channels) if channel.id == target_id), None)
            if index is None:
                index = min(max(target_id - 1, 0), len(channels) - 1)
            await self._show_channels_menu(chat_id, page=index // PANEL_PAGE_SIZE)
            return

        await self._show_channel_search_results(chat_id, text)

    async def _show_channels_menu(self, chat_id: int, page: int = 0) -> None:
        channels = await self.editorial_actions.list_channels()
        if not channels:
            await self.main_bot.send_message(chat_id, "Каналов в editorial-слое пока нет. Сначала импортируйте legacy данные.")
            return

        page = self._normalize_panel_page(page, len(channels))
        start, end, has_previous, has_next = self._page_bounds(page, len(channels))
        page_channels = channels[start:end]
        buttons = []
        lines = [f"Каналы ({start + 1}-{end} \u0438\u0437 {len(channels)}):"]
        for item in page_channels:
            channel_id, label, _ = self._channel_panel_entry(item)
            buttons.append((channel_id, label))
            lines.append(f"{item.id}. {label}")
        lines.append("")
        lines.append("Можно отправить номер канала или часть тега/названия.")

        self._set_user_state(chat_id, "await_channel_jump")
        await self.main_bot.send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=build_channels_actions(
                buttons,
                page=page,
                has_previous=has_previous,
                has_next=has_next,
            ),
        )

    async def _show_my_channels_menu(self, chat_id: int, user_id: int) -> None:
        channels = await self.editorial_actions.list_user_moderation_feed_channels(user_id)
        if not channels:
            await self.main_bot.send_message(
                chat_id,
                (
                    "\u0423 \u0432\u0430\u0441 \u043f\u043e\u043a\u0430 \u043d\u0435 \u0432\u044b\u0431\u0440\u0430\u043d\u044b \u043a\u0430\u043d\u0430\u043b\u044b.\n\n"
                    "\u041e\u0442\u043a\u0440\u043e\u0439\u0442\u0435 '\u041a\u0430\u043d\u0430\u043b\u044b \u0438 \u0441\u043b\u043e\u0442\u044b', "
                    "\u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u0430\u043d\u0430\u043b \u0438 \u0432\u043a\u043b\u044e\u0447\u0438\u0442\u0435 "
                    "'\u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439'."
                ),
                reply_markup=build_my_channels_actions([]),
            )
            return

        buttons = []
        lines = [
            "\u041c\u043e\u0438 \u043a\u0430\u043d\u0430\u043b\u044b.",
            "\u042d\u0442\u043e \u043a\u0430\u043d\u0430\u043b\u044b, \u0434\u043b\u044f \u043a\u043e\u0442\u043e\u0440\u044b\u0445 \u0443 \u0432\u0430\u0441 \u0432\u043a\u043b\u044e\u0447\u0435\u043d\u043e \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439.",
            "",
            "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435, \u043a\u0443\u0434\u0430 \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0440\u0443\u0447\u043d\u043e\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435:",
        ]
        for channel in channels:
            label = self._compose_channel_display_label(
                self._channel_title_from_runtime(channel.tg_channel_id) or channel.title,
                self._channel_label_from_runtime(channel.tg_channel_id),
                channel.short_code,
            )
            buttons.append((channel.id, label))
            lines.append(f"{len(buttons)}. {label}")

        await self.main_bot.send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=build_my_channels_actions(buttons),
        )

    @staticmethod
    def _format_manual_channel_message_result(result) -> str:
        lines = [
            "\u0420\u0443\u0447\u043d\u043e\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u0430\u043d\u043e.",
            f"\u041a\u0430\u043d\u0430\u043b\u043e\u0432 \u0432\u044b\u0431\u0440\u0430\u043d\u043e: {result.requested}",
            f"\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e: {result.sent}",
        ]
        if result.blocked:
            lines.append(f"\u0417\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u043e \u0440\u0435\u043a\u043b\u0430\u043c\u043d\u044b\u043c \u043e\u043a\u043d\u043e\u043c: {result.blocked}")
        if result.deferred:
            lines.append(f"Отложено до восстановления Telegram: {result.deferred}")
        if result.failed:
            lines.append(f"\u041e\u0448\u0438\u0431\u043e\u043a: {result.failed}")
        if result.content_item_ids:
            ids = ", ".join(map(str, result.content_item_ids))
            lines.append(f"Content items: {ids}")
        if result.publication_log_ids:
            ids = ", ".join(map(str, result.publication_log_ids))
            lines.append(f"Publication logs: {ids}")
        if result.errors:
            lines.append("")
            lines.append("\u0414\u0435\u0442\u0430\u043b\u0438:")
            lines.extend(result.errors[:5])
        return "\n".join(lines)

    @staticmethod
    def _format_channel_setting_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, time):
            return value.strftime("%H:%M")
        return str(value)

    async def _format_ad_blackout(self, channel_id: int, blackout) -> str:
        channel = await self.editorial_actions.get_channel(channel_id)
        timezone_name = channel.timezone if channel is not None else "Europe/Moscow"
        tz = ZoneInfo(timezone_name)
        starts_at = blackout.starts_at.astimezone(tz)
        ends_at = blackout.ends_at.astimezone(tz)
        return f"{starts_at.strftime('%d.%m %H:%M')} - {ends_at.strftime('%d.%m %H:%M')}"

    async def _show_channel_menu(self, chat_id: int, channel_id: int, user_id: int | None = None) -> None:
        await self.editorial_actions.sync_channel_activity_from_bindings()
        channel = await self.editorial_actions.get_channel(channel_id)
        if channel is None:
            await self.main_bot.send_message(chat_id, f"Канал {channel_id} не найден.")
            return

        if not channel.is_active:
            await self.main_bot.send_message(chat_id, "\u041a\u0430\u043d\u0430\u043b \u043e\u0442\u0432\u044f\u0437\u0430\u043d \u0438 \u0441\u043a\u0440\u044b\u0442 \u0438\u0437 \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0439 \u043f\u0430\u043d\u0435\u043b\u0438.")
            return

        if channel.content_family == ContentFamily.CONFESSION.value:
            await self._show_confession_channel_slots(
                chat_id,
                channel_id,
                user_id=user_id,
            )
            return

        effective_user_id = user_id if user_id is not None else chat_id
        label = await self._get_channel_label(channel_id)
        notifications_enabled = await self.editorial_actions.is_channel_notifications_enabled(channel_id, effective_user_id)
        moderation_feed_enabled = await self.editorial_actions.is_channel_moderation_feed_enabled(channel_id, effective_user_id)
        settings_snapshot = await self.editorial_actions.get_channel_settings_snapshot(channel_id)
        ad_blackouts = await self.editorial_actions.list_channel_ad_blackouts(channel_id)
        summary_fields = {
            "min_gap_minutes",
            "slot_jitter_minutes",
            "auto_slots_enabled",
            "auto_slots_plan_time",
            "auto_slots_window_start",
            "auto_slots_window_end",
            "auto_slots_replace_manual",
            "settings_profile_auto_enabled",
            "min_slots_per_day",
            "max_posts_per_day",
            "max_paste_per_day",
            "same_tag_cooldown_hours",
            "same_template_cooldown_hours",
            "same_paste_cooldown_days",
            "allow_generated",
            "allow_pastes",
        }
        summary_lines = [
            f"{field_name} = {self._format_channel_setting_value(value)}"
            for field_name, value in settings_snapshot
            if field_name in summary_fields
        ]
        text_lines = [
            f"Канал: {label}",
            f"tg_channel_id: {channel.tg_channel_id}",
            f"subscribers: {channel.subscriber_count if channel.subscriber_count is not None else 'unknown'}",
            f"settings_profile_id: {channel.settings_profile_id or 'none'}",
            f"timezone: {channel.timezone}",
            "",
            "Ключевые параметры:",
            *summary_lines,
        ]
        if ad_blackouts:
            text_lines.extend(["", "Рекламные окна:"])
            for blackout in ad_blackouts:
                text_lines.append(await self._format_ad_blackout(channel_id, blackout))
        await self.main_bot.send_message(
            chat_id,
            "\n".join(text_lines),
            reply_markup=build_channel_actions(channel_id, notifications_enabled, moderation_feed_enabled),
        )

    @staticmethod
    def _build_submission_open_markup(submission_id: int) -> InlineKeyboardMarkup:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("Открыть в панели", callback_data=f"submission:view:{submission_id}"))
        return markup

    async def callback_new_submission_notification(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
    ) -> None:
        submission = await self.editorial_actions.ensure_submission_for_review_message(
            channel_tg_id=channel_tg_id,
            review_chat_id=review_chat_id,
            review_message_id=review_message_id,
        )
        if submission is None:
            return

        recipient_ids = await self.editorial_actions.list_channel_notification_user_ids(submission.channel_id)
        if not recipient_ids:
            return

        channel_label = await self._get_channel_label(submission.channel_id)
        excerpt = self._submission_notification_excerpt(submission)
        notification_text = (
            f"Новое сообщение в канале {channel_label}\n"
            f"Тип: {self._submission_type_label(submission)}\n\n"
            f"{excerpt}"
        )
        reply_markup = self._build_submission_open_markup(submission.id)

        for recipient_id in recipient_ids:
            if not self._is_admin(recipient_id):
                continue
            try:
                await self.main_bot.send_message(
                    recipient_id,
                    notification_text,
                    reply_markup=reply_markup,
                )
            except Exception as ex:
                logger.error("Failed to deliver submission notification to {}: {}", recipient_id, ex)

    async def _show_channel_slots_menu(self, chat_id: int, channel_id: int) -> None:
        await self.editorial_actions.sync_channel_activity_from_bindings()
        channel = await self.editorial_actions.get_channel(channel_id)
        if channel is None:
            await self.main_bot.send_message(chat_id, f"Канал {channel_id} не найден.")
            return

        if not channel.is_active:
            await self.main_bot.send_message(chat_id, "\u041a\u0430\u043d\u0430\u043b \u043e\u0442\u0432\u044f\u0437\u0430\u043d \u0438 \u0441\u043a\u0440\u044b\u0442 \u0438\u0437 \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0439 \u043f\u0430\u043d\u0435\u043b\u0438.")
            return

        label = await self._get_channel_label(channel_id)
        slots = await self.editorial_actions.list_channel_slots(channel_id)
        slot_lines = [f"Слоты для {label}:"]
        if not slots:
            slot_lines.append("Слотов пока нет.")
        else:
            for slot in slots:
                slot_label = f"{self._weekday_label(slot.weekday)} {slot.slot_time.strftime('%H:%M')}"
                if getattr(slot, "is_auto_managed", False):
                    slot_label = f"{slot_label} auto"
                slot_lines.append(f"#{slot.id} {slot_label}")

        await self.main_bot.send_message(
            chat_id,
            "\n".join(slot_lines),
            reply_markup=build_channel_slots_actions(channel_id),
        )

    def _submission_moderation_allowed(self, status: str) -> bool:
        return status in {SubmissionStatus.NEW.value, SubmissionStatus.HOLD.value}

    def _submission_status_label(self, status: str) -> str:
        labels = {
            SubmissionStatus.NEW.value: "новое",
            SubmissionStatus.APPROVED_AS_SOURCE.value: "одобрено как источник",
            SubmissionStatus.PASTE_CANDIDATE.value: "сохранено как паста",
            SubmissionStatus.CONTENT_CREATED.value: "черновик создан",
            SubmissionStatus.REJECTED.value: "отклонено",
            SubmissionStatus.HOLD.value: "hold",
            SubmissionStatus.ADVERTISING.value: "реклама",
        }
        return labels.get(status, status)

    def _content_status_label(self, status: str) -> str:
        labels = {
            "draft": "черновик",
            "pending_review": "на review",
            "approved": "одобрено",
            "scheduled": "запланировано",
            "published": "опубликовано",
            "rejected": "отклонено",
            "hold": "hold",
        }
        return labels.get(status, status)

    def _submission_type_label(self, submission) -> str:
        if getattr(submission, "media_group_id", None):
            return "медиа-группа"
        labels = {
            "text": "текст",
            "photo": "фото",
            "video": "видео",
            "animation": "gif",
        }
        return labels.get(getattr(submission, "content_type", "text"), getattr(submission, "content_type", "text"))

    def _submission_author_tag(self, submission) -> str:
        return f"@{submission.username}" if submission.username else "@None"

    def _submission_notification_excerpt(self, submission) -> str:
        body = (submission.cleaned_text or submission.raw_text or "").strip()
        if not body:
            if getattr(submission, "media_group_id", None):
                body = "<медиа-группа без подписи>"
            elif getattr(submission, "content_type", "text") != "text":
                body = f"<{self._submission_type_label(submission)} без подписи>"
            else:
                body = "<без текста>"

        lines = [line.strip() for line in body.splitlines() if line.strip()]
        excerpt = "\n".join(lines[:2]) if lines else body
        if len(excerpt) > 220:
            excerpt = excerpt[:217].rstrip() + "..."
        return excerpt

    async def _send_submission_card(
        self,
        chat_id: int,
        submission_id: int,
        *,
        history_mode: bool,
        has_next: bool = False,
    ) -> None:
        submission = await self.editorial_actions.get_submission(submission_id)
        if submission is None:
            await self.main_bot.send_message(chat_id, f"Сообщение {submission_id} не найдено.")
            return

        await self._send_submission_preview(chat_id, submission.id)
        markup = (
            build_submission_history_actions(
                submission.id,
                has_next,
                submission.is_anonymous,
                allow_moderation=self._submission_moderation_allowed(str(submission.status)),
            )
            if history_mode
            else build_submission_actions(
                submission.id,
                has_next,
                submission.is_anonymous,
                allow_moderation=True,
            )
        )
        await self.main_bot.send_message(
            chat_id,
            await self._format_submission(submission),
            reply_markup=markup,
        )

    async def _format_submission(self, submission) -> str:
        body = submission.cleaned_text or submission.raw_text
        if not body:
            if getattr(submission, "media_group_id", None):
                body = "<медиа-группа без подписи>"
            elif getattr(submission, "content_type", "text") != "text":
                body = f"<{self._submission_type_label(submission)} без подписи>"
            else:
                body = "<без текста>"
        if len(body) > 3000:
            body = body[:3000] + "..."
        tags = ", ".join(submission.detected_tags) if submission.detected_tags else "нет"
        channel_label = await self._get_channel_label(submission.channel_id)
        username = f"@{submission.username}" if submission.username else "-"
        first_name = submission.first_name or "-"
        primary_item = await self.editorial_actions.get_submission_primary_content_item(submission.id)
        if primary_item is None:
            status_label = self._submission_status_label(str(submission.status))
            pipeline_line = "Публикация: еще не создан content item"
        else:
            status_label = self._content_status_label(str(primary_item.status))
            pipeline_line = (
                f"Пайплайн: content #{primary_item.id}, "
                f"статус submission = {self._submission_status_label(str(submission.status))}"
            )
        return (
            f"Сообщение #{submission.id}\n"
            f"Канал: {channel_label}\n"
            f"Тип: {self._submission_type_label(submission)}\n"
            f"Пользователь: {username} / {first_name}\n"
            f"Режим автора: {'анон' if submission.is_anonymous else f'не анон ({self._submission_author_tag(submission)})'}\n"
            f"Статус: {status_label}\n"
            f"{pipeline_line}\n"
            f"Теги: {tags}\n\n"
            f"{body}"
        )

    async def _format_content_item(self, item) -> str:
        body = item.body_text
        if len(body) > 3000:
            body = body[:3000] + "..."
        tags = ", ".join(item.tags) if item.tags else "нет"
        channel_label = await self._get_channel_label(item.channel_id)
        return (
            f"Контент #{item.id}\n"
            f"Канал: {channel_label}\n"
            f"Источник: {item.source_type}\n"
            f"Статус: {item.status}\n"
            f"Теги: {tags}\n\n"
            f"{body}"
        )

    async def _format_paste(self, paste) -> str:
        body = paste.body_text
        if len(body) > 3000:
            body = body[:3000] + "..."
        tags = ", ".join(paste.tags) if paste.tags else "нет"
        return (
            f"Паста #{paste.id}\n"
            f"Заголовок: {paste.title}\n"
            f"Статус: {paste.status}\n"
            f"Теги: {tags}\n"
            f"Primary tag: {paste.primary_tag or '-'}\n\n"
            f"{body}"
        )

    async def _send_submission_preview(self, chat_id: int, submission_id: int) -> None:
        preview = await self.editorial_actions.get_submission_preview(submission_id)
        if preview is None or not preview.review_message_ids:
            return

        if (
            preview.media_group_id
            and len(preview.preview_file_ids) > 1
            and await self._send_submission_preview_fallback(chat_id, preview)
        ):
            return

        try:
            if len(preview.review_message_ids) == 1:
                await self.main_bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=preview.review_chat_id,
                    message_id=preview.review_message_ids[0],
                )
                return

            url = f"https://api.telegram.org/bot{self.api_token_bot}/copyMessages"
            payload = {
                "chat_id": chat_id,
                "from_chat_id": preview.review_chat_id,
                "message_ids": preview.review_message_ids,
            }
            request_kwargs = {}
            if settings.proxies["http"]:
                request_kwargs["proxy"] = settings.proxies["http"]
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, **request_kwargs) as response:
                    result = await response.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("description", str(result)))
        except Exception as ex:
            logger.error("Failed to send submission preview for {}: {}", submission_id, ex)
            await self._send_submission_preview_fallback(chat_id, preview)

    async def _send_submission_preview_fallback(self, chat_id: int, preview) -> bool:
        if not preview.preview_file_ids:
            return False

        max_preview_bytes = settings.media_preview_max_mb * 1024 * 1024
        if any(size and size > max_preview_bytes for size in preview.preview_file_sizes):
            await self.main_bot.send_message(
                chat_id,
                (
                    "Медиа слишком большое для быстрого предпросмотра в главном боте. "
                    "Его всё ещё можно approve и публиковать, но превью здесь пропущено."
                ),
            )
            return True

        binding = await self.legacy_reader.get_bot_binding(preview.channel_tg_id)
        if binding is None:
            logger.error("Failed to build preview fallback: no bot binding for channel {}", preview.channel_tg_id)
            return False

        try:
            if preview.media_group_id and len(preview.preview_file_ids) > 1:
                await self._send_binary_preview_media_group(
                    chat_id=chat_id,
                    bot_token=binding.bot_api_token,
                    file_ids=preview.preview_file_ids,
                    content_types=preview.preview_content_types,
                    default_content_type=preview.content_type,
                )
                return True

            first_type = preview.preview_content_types[0] if preview.preview_content_types else preview.content_type
            await self._send_binary_preview_item(
                chat_id=chat_id,
                bot_token=binding.bot_api_token,
                file_id=preview.preview_file_ids[0],
                content_type=first_type,
            )
            return True
        except Exception as ex:
            logger.error("Failed to send preview fallback for submission: {}", ex)
            return False

    async def _download_binary_preview_item(
        self,
        *,
        bot_token: str,
        file_id: str,
        content_type: str,
    ) -> tuple[bytes, str, str, str]:
        subbot = AsyncTeleBot(bot_token)
        try:
            file_info = await subbot.get_file(file_id)
        finally:
            await subbot.close_session()
        file_url = f"https://api.telegram.org/file/bot{bot_token}/{file_info.file_path}"

        request_kwargs = {}
        if settings.proxies["http"]:
            request_kwargs["proxy"] = settings.proxies["http"]

        async with aiohttp.ClientSession() as session:
            async with session.get(file_url, **request_kwargs) as response:
                response.raise_for_status()
                payload = await response.read()

        filename = file_info.file_path.split("/")[-1] or "preview.bin"
        if content_type == "photo":
            return payload, filename, "photo", "image/jpeg"
        if content_type == "video":
            return payload, filename, "video", "video/mp4"
        if content_type == "document":
            return payload, filename, "document", "application/octet-stream"
        if content_type == "audio":
            return payload, filename, "audio", "audio/mpeg"
        raise ValueError(f"Unsupported media group preview type: {content_type}")

    async def _send_binary_preview_media_group(
        self,
        *,
        chat_id: int,
        bot_token: str,
        file_ids: list[str],
        content_types: list[str],
        default_content_type: str,
    ) -> None:
        downloaded_items = []
        for index, file_id in enumerate(file_ids):
            content_type = (
                content_types[index]
                if index < len(content_types)
                else default_content_type
            )
            downloaded_items.append(
                await self._download_binary_preview_item(
                    bot_token=bot_token,
                    file_id=file_id,
                    content_type=content_type,
                )
            )

        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        media = []
        for index, (payload, filename, item_type, mime_type) in enumerate(downloaded_items):
            field_name = f"media_{index}"
            media.append({"type": item_type, "media": f"attach://{field_name}"})
            form.add_field(
                field_name,
                payload,
                filename=filename,
                content_type=mime_type,
            )
        form.add_field("media", json.dumps(media))

        request_kwargs = {}
        if settings.proxies["http"]:
            request_kwargs["proxy"] = settings.proxies["http"]
        api_url = f"https://api.telegram.org/bot{self.api_token_bot}/sendMediaGroup"
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, data=form, **request_kwargs) as response:
                result = await response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("description", str(result)))

    async def _send_binary_preview_item(
        self,
        chat_id: int,
        bot_token: str,
        file_id: str,
        content_type: str,
    ) -> None:
        subbot = AsyncTeleBot(bot_token)
        file_info = await subbot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{bot_token}/{file_info.file_path}"

        request_kwargs = {}
        if settings.proxies["http"]:
            request_kwargs["proxy"] = settings.proxies["http"]

        async with aiohttp.ClientSession() as session:
            async with session.get(file_url, **request_kwargs) as response:
                response.raise_for_status()
                payload = await response.read()

        filename = file_info.file_path.split("/")[-1] or "preview.bin"
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))

        send_method = "sendPhoto"
        field_name = "photo"
        mime_type = "image/jpeg"
        if content_type == "video":
            send_method = "sendVideo"
            field_name = "video"
            mime_type = "video/mp4"
        elif content_type == "animation":
            send_method = "sendAnimation"
            field_name = "animation"
            mime_type = "image/gif"

        form.add_field(
            field_name,
            payload,
            filename=filename,
            content_type=mime_type,
        )

        api_url = f"https://api.telegram.org/bot{self.api_token_bot}/{send_method}"
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, data=form, **request_kwargs) as response:
                result = await response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("description", str(result)))

    async def _show_first_pending_submission(
        self,
        chat_id: int,
        current_id: int | None = None,
        user_id: int | None = None,
    ) -> None:
        effective_user_id = user_id if user_id is not None else chat_id
        submissions = await self.editorial_actions.list_pending_submissions(user_id=effective_user_id)
        if not submissions:
            selected_channel_ids = await self.editorial_actions.list_user_moderation_feed_channel_ids(effective_user_id)
            if selected_channel_ids:
                await self.main_bot.send_message(chat_id, "Новых сообщений для выбранных каналов нет.")
            else:
                await self.main_bot.send_message(chat_id, "Новых сообщений для review нет.")
            return

        index = 0
        if current_id is not None:
            for idx, item in enumerate(submissions):
                if item.id == current_id:
                    index = min(idx + 1, len(submissions) - 1)
                    break

        submission = submissions[index]
        await self._send_submission_card(
            chat_id,
            submission.id,
            history_mode=False,
            has_next=len(submissions) > index + 1,
        )

    async def _show_submission_history(self, chat_id: int, current_id: int | None = None) -> None:
        submissions = await self.editorial_actions.list_recent_submissions(limit=None)
        if not submissions:
            await self.main_bot.send_message(chat_id, "История сообщений пока пуста.")
            return

        index = 0
        if current_id is not None:
            for idx, item in enumerate(submissions):
                if item.id == current_id:
                    index = min(idx + 1, len(submissions) - 1)
                    break

        submission = submissions[index]
        await self._send_submission_card(
            chat_id,
            submission.id,
            history_mode=True,
            has_next=len(submissions) > index + 1,
        )

    async def _show_first_pending_content(self, chat_id: int, current_id: int | None = None) -> None:
        items = await self.editorial_actions.list_pending_content_items()
        if not items:
            await self.main_bot.send_message(chat_id, "Контента в pending_review нет.")
            return

        index = 0
        if current_id is not None:
            for idx, item in enumerate(items):
                if item.id == current_id:
                    index = min(idx + 1, len(items) - 1)
                    break

        item = items[index]
        await self.main_bot.send_message(
            chat_id,
            await self._format_content_item(item),
            reply_markup=build_content_actions(item.id, len(items) > index + 1),
        )

    async def _show_paste_at_index(self, chat_id: int, pastes: list, index: int) -> None:
        index = min(max(index, 0), len(pastes) - 1)
        paste = pastes[index]
        self._set_user_state(chat_id, "await_paste_jump", current_paste_id=paste.id)
        await self.main_bot.send_message(
            chat_id,
            f"Паста {index + 1}/{len(pastes)}\n\n{await self._format_paste(paste)}",
            reply_markup=build_paste_actions(
                paste.id,
                has_previous=index > 0,
                has_next=len(pastes) > index + 1,
            ),
        )

    async def _show_paste_from_input(self, chat_id: int, value: str) -> None:
        text = value.strip()
        if not text.isdigit():
            self._set_user_state(chat_id, "await_paste_jump")
            await self.main_bot.send_message(chat_id, "Отправьте номер пасты числом.")
            return

        target_number = int(text)
        pastes = await self.editorial_actions.list_pastes(limit=None)
        if not pastes:
            await self.main_bot.send_message(
                chat_id,
                "В библиотеке паст пока ничего нет.",
                reply_markup=build_empty_paste_actions(),
            )
            return

        for index, paste in enumerate(pastes):
            if paste.id == target_number:
                await self._show_paste_at_index(chat_id, pastes, index)
                return

        if 1 <= target_number <= len(pastes):
            await self._show_paste_at_index(chat_id, pastes, target_number - 1)
            return

        self._set_user_state(chat_id, "await_paste_jump")
        await self.main_bot.send_message(chat_id, f"Паста #{target_number} не найдена.")

    async def _show_first_paste(self, chat_id: int, current_id: int | None = None, step: int = 1) -> None:
        pastes = await self.editorial_actions.list_pastes(limit=None)
        if not pastes:
            await self.main_bot.send_message(
                chat_id,
                "В библиотеке паст пока ничего нет.",
                reply_markup=build_empty_paste_actions(),
            )
            return

        index = 0
        if current_id is not None:
            for idx, item in enumerate(pastes):
                if item.id == current_id:
                    index = min(max(idx + step, 0), len(pastes) - 1)
                    break

        await self._show_paste_at_index(chat_id, pastes, index)

    async def _show_confessions_menu(self, chat_id: int) -> None:
        publisher = await self.editorial_actions.get_active_confession_publisher()
        if publisher is None:
            status = "Саббот для публикаций пока не подключён."
        else:
            bot_label = f"@{publisher.bot_username}" if publisher.bot_username else str(publisher.bot_user_id)
            storage_label = publisher.storage_chat_title or publisher.storage_chat_id or "не подключён"
            status = f"Саббот: {bot_label}\nЧат паст: {storage_label}"
        await self.main_bot.send_message(
            chat_id,
            "Признавашки.\n\n" + status,
            reply_markup=build_confessions_menu(self._is_general_admin(chat_id)),
        )

    @staticmethod
    def _format_confession_paste(paste) -> str:
        body = paste.body_text or f"<{paste.storage_content_type or 'message'}>"
        if len(body) > 3000:
            body = body[:3000] + "..."
        return (
            f"Паста признавашек #{paste.id}\n"
            f"Тип: {paste.storage_content_type or 'message'}\n"
            f"Сообщение в хранилище: {paste.storage_message_id or '-'}\n\n"
            f"{body}"
        )

    async def _show_confession_paste_at_index(self, chat_id: int, pastes: list, index: int) -> None:
        index = min(max(index, 0), len(pastes) - 1)
        paste = pastes[index]
        await self.main_bot.send_message(
            chat_id,
            f"Паста {index + 1}/{len(pastes)}\n\n{self._format_confession_paste(paste)}",
            reply_markup=build_confession_paste_actions(
                paste.id,
                has_previous=index > 0,
                has_next=len(pastes) > index + 1,
            ),
        )

    async def _show_first_confession_paste(
        self,
        chat_id: int,
        current_id: int | None = None,
        step: int = 1,
    ) -> None:
        pastes = await self.editorial_actions.list_confession_pastes(limit=None)
        if not pastes:
            await self.main_bot.send_message(
                chat_id,
                "Паст признавашек пока нет. Добавьте сообщения в подключённый чат паст.",
                reply_markup=build_empty_confession_paste_actions(),
            )
            return
        index = 0
        if current_id is not None:
            for idx, paste in enumerate(pastes):
                if paste.id == current_id:
                    index = min(max(idx + step, 0), len(pastes) - 1)
                    break
        await self._show_confession_paste_at_index(chat_id, pastes, index)

    @staticmethod
    def _confession_channel_label(channel) -> str:
        return channel.title or channel.short_code or str(channel.tg_channel_id)

    async def _show_confession_channels_menu(self, chat_id: int, page: int = 0) -> None:
        channels = await self.editorial_actions.list_confession_channels()
        page = self._normalize_panel_page(page, len(channels))
        start, end, has_previous, has_next = self._page_bounds(page, len(channels))
        visible = channels[start:end]
        buttons = [(channel.id, self._confession_channel_label(channel)) for channel in visible]
        lines = [f"Паблики признавашек ({len(channels)}):"]
        if visible:
            lines.extend(
                f"{channel.id}. {self._confession_channel_label(channel)}"
                for channel in visible
            )
        else:
            lines.append("Пока не подключено ни одного паблика.")
        await self.main_bot.send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=build_confession_channels_actions(
                buttons,
                page=page,
                has_previous=has_previous,
                has_next=has_next,
                is_general_admin=self._is_general_admin(chat_id),
            ),
        )

    async def _show_confession_channel_slots(
        self,
        chat_id: int,
        channel_id: int,
        user_id: int | None = None,
    ) -> None:
        channel = await self.editorial_actions.get_channel(channel_id)
        if channel is None or channel.content_family != ContentFamily.CONFESSION.value:
            await self.main_bot.send_message(chat_id, "Паблик признавашек не найден.")
            return
        slots = await self.editorial_actions.list_channel_slots(channel_id)
        ad_blackouts = await self.editorial_actions.list_channel_ad_blackouts(channel_id)
        settings_snapshot = await self.editorial_actions.get_channel_settings_snapshot(channel_id)
        effective_user_id = user_id if user_id is not None else chat_id
        notifications_enabled = await self.editorial_actions.is_channel_notifications_enabled(
            channel_id,
            effective_user_id,
        )
        moderation_feed_enabled = await self.editorial_actions.is_channel_moderation_feed_enabled(
            channel_id,
            effective_user_id,
        )
        binding = await self.legacy_reader.get_bot_binding(channel.tg_channel_id)
        running_subbot = self._running_subbot_for_channel(channel.tg_channel_id)
        if binding is None:
            suggestion_status = "не подключена"
        else:
            suggestion_status = f"@{binding.bot_username.lstrip('@')}"
            if running_subbot is None:
                suggestion_status += " (ожидает запуска)"
            elif running_subbot.chat_suggest is None:
                suggestion_status += "; модер-чат не подключён"
            else:
                suggestion_status += f"; модер-чат {running_subbot.chat_suggest}"

        summary_fields = {
            "min_gap_minutes",
            "slot_jitter_minutes",
            "auto_slots_enabled",
            "auto_slots_plan_time",
            "auto_slots_window_start",
            "auto_slots_window_end",
            "auto_slots_replace_manual",
            "settings_profile_auto_enabled",
            "min_slots_per_day",
            "max_posts_per_day",
            "max_paste_per_day",
            "same_paste_cooldown_days",
            "allow_generated",
            "allow_pastes",
        }
        summary_lines = [
            f"{field_name} = {self._format_channel_setting_value(value)}"
            for field_name, value in settings_snapshot
            if field_name in summary_fields
        ]
        lines = [
            f"Паблик признавашек: {self._confession_channel_label(channel)}",
            f"tg_channel_id: {channel.tg_channel_id}",
            f"Предложка: {suggestion_status}",
            f"subscribers: {channel.subscriber_count if channel.subscriber_count is not None else 'unknown'}",
            f"settings_profile_id: {channel.settings_profile_id or 'none'}",
            "",
            "Ключевые параметры:",
            *summary_lines,
            "",
            "Слоты:",
        ]
        if slots:
            lines.extend(
                (
                    f"#{slot.id} {self._weekday_label(slot.weekday)} "
                    f"{slot.slot_time.strftime('%H:%M')}"
                    f"{' auto' if getattr(slot, 'is_auto_managed', False) else ''}"
                )
                for slot in slots
            )
        else:
            lines.append("Слотов пока нет.")
        if ad_blackouts:
            lines.extend(["", "Рекламные окна:"])
            for blackout in ad_blackouts:
                lines.append(await self._format_ad_blackout(channel_id, blackout))
        await self.main_bot.send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=build_confession_channel_actions(
                channel_id,
                is_general_admin=self._is_general_admin(effective_user_id),
                has_suggestion_bot=binding is not None,
                notifications_enabled=notifications_enabled,
                moderation_feed_enabled=moderation_feed_enabled,
            ),
        )

    async def _show_tags_menu(self, chat_id: int) -> None:
        tags = await self.editorial_actions.list_tags(include_inactive=True)
        buttons = [(tag.slug, tag.title, tag.is_active) for tag in tags]
        await self.main_bot.send_message(
            chat_id,
            "Теги управляют авто-разметкой паст и сообщений, а также правилами публикации паст по каналам.",
            reply_markup=build_tags_actions(buttons),
        )

    async def _show_tag_card(self, chat_id: int, slug: str) -> None:
        tags = await self.editorial_actions.list_tags(include_inactive=True)
        tag = next((item for item in tags if item.slug == slug), None)
        if tag is None:
            await self.main_bot.send_message(chat_id, f"Тег {slug} не найден.")
            return
        keywords = await self.editorial_actions.list_tag_keywords(slug)
        active_keywords = [keyword.keyword for keyword in keywords if keyword.is_active]
        keyword_text = ", ".join(active_keywords) if active_keywords else "нет"
        await self.main_bot.send_message(
            chat_id,
            (
                f"Тег: {tag.title}\n"
                f"Slug: {tag.slug}\n"
                f"Статус: {'активен' if tag.is_active else 'выключен'}\n"
                f"Priority: {tag.priority}\n"
                f"Ключевые слова: {keyword_text}"
            ),
            reply_markup=build_tag_actions(tag.slug, tag.is_active),
        )

    async def _show_tag_keywords(self, chat_id: int, slug: str) -> None:
        keywords = await self.editorial_actions.list_tag_keywords(slug)
        buttons = [(keyword.id, keyword.keyword, keyword.is_active) for keyword in keywords]
        await self.main_bot.send_message(
            chat_id,
            f"Ключевые слова тега {slug}:",
            reply_markup=build_tag_keywords_actions(slug, buttons),
        )

    async def _show_paste_tags(self, chat_id: int, paste_id: int) -> None:
        paste = await self.editorial_actions.get_paste(paste_id)
        if paste is None:
            await self.main_bot.send_message(chat_id, f"Паста #{paste_id} не найдена.")
            return
        summary = await self.editorial_actions.get_paste_tag_summary(paste_id)
        await self.main_bot.send_message(
            chat_id,
            (
                f"Теги пасты #{paste_id}: {paste.title}\n"
                f"Авто: {', '.join(summary.auto_tags) if summary.auto_tags else 'нет'}\n"
                f"Вручную: {', '.join(summary.manual_tags) if summary.manual_tags else 'нет'}\n"
                f"Итог: {', '.join(summary.all_tags) if summary.all_tags else 'нет'}\n"
                f"Основной: {summary.primary_tag or '-'}"
            ),
            reply_markup=build_paste_tag_actions(paste_id),
        )

    async def _show_channel_paste_tags(self, chat_id: int, channel_id: int) -> None:
        channel = await self.editorial_actions.get_channel(channel_id)
        if channel is None:
            await self.main_bot.send_message(chat_id, f"Канал {channel_id} не найден.")
            return
        label = await self._get_channel_label(channel_id)
        rows = await self.editorial_actions.list_channel_paste_tag_rules(channel_id)
        include_tags = [tag.slug for rule, tag in rows if rule.mode == ChannelPasteTagRuleMode.INCLUDE and rule.is_active]
        exclude_tags = [tag.slug for rule, tag in rows if rule.mode == ChannelPasteTagRuleMode.EXCLUDE and rule.is_active]
        await self.main_bot.send_message(
            chat_id,
            (
                f"Правила паст по тегам для {label}\n"
                f"Только с тегами: {', '.join(include_tags) if include_tags else 'нет'}\n"
                f"Запрещённые теги: {', '.join(exclude_tags) if exclude_tags else 'нет'}\n\n"
                "Если список 'только с тегами' пуст, подходят любые пасты без запрещённых тегов."
            ),
            reply_markup=build_channel_paste_tag_actions(channel_id),
        )

    async def _show_channel_paste_diagnostics(self, chat_id: int, channel_id: int) -> None:
        try:
            diagnostics = await self.editorial_actions.get_channel_paste_diagnostics(channel_id)
        except ValueError:
            await self.main_bot.send_message(chat_id, f"Канал {channel_id} не найден.")
            return

        channel_label = await self._get_channel_label(channel_id)
        paste_today = diagnostics.scheduled_pastes_today + diagnostics.sent_pastes_today
        lines = [
            f"Почему не публикует пасты: {channel_label}",
            "",
            "Канал:",
            f"active: {'да' if diagnostics.is_active else 'нет'}",
            f"allow_pastes: {'да' if diagnostics.allow_pastes else 'нет'}",
            f"max_paste_per_day: {diagnostics.max_paste_per_day}",
            f"same_paste_cooldown_days: {diagnostics.same_paste_cooldown_days}",
            f"same_tag_cooldown_hours: {diagnostics.same_tag_cooldown_hours}",
            "",
            "Пасты:",
            f"всего в библиотеке: {diagnostics.total_pastes}",
            f"active: {diagnostics.active_pastes}",
            f"доступно прямо сейчас: {diagnostics.available_pastes}",
            f"готовых approved paste items для канала: {diagnostics.approved_ready_paste_items}",
            f"сегодня scheduled/sent: {diagnostics.scheduled_pastes_today}/{diagnostics.sent_pastes_today} (всего {paste_today})",
        ]

        if diagnostics.next_available_examples:
            lines.extend(["", "Примеры доступных:"])
            lines.extend(diagnostics.next_available_examples)

        if diagnostics.reasons:
            lines.extend(["", "Почему остальные недоступны:"])
            for reason in diagnostics.reasons:
                lines.append(f"{reason.title}: {reason.count}")
                if reason.examples:
                    lines.append(f"  например: {', '.join(reason.examples)}")

        if diagnostics.available_pastes == 0:
            lines.extend(["", "Что проверить первым:"])
            if not diagnostics.is_active:
                lines.append("канал выключен в editorial-слое")
            if not diagnostics.allow_pastes:
                lines.append("включить allow_pastes")
            if paste_today >= diagnostics.max_paste_per_day:
                lines.append("max_paste_per_day уже выбран сегодня")
            if any(reason.code == "tag_rule" for reason in diagnostics.reasons):
                lines.append("include/exclude правила тегов для паст")
            if any(reason.code in {"cooldown", "reserved"} for reason in diagnostics.reasons):
                lines.append("кулдауны или уже запланированные пасты")

        await self.main_bot.send_message(chat_id, "\n".join(lines))

    async def _show_global_paste_tags(self, chat_id: int) -> None:
        rows = await self.editorial_actions.list_global_paste_tag_rules()
        now = datetime.now(timezone.utc)
        include_tags = [
            self._format_global_paste_tag_rule(rule, tag.slug)
            for rule, tag in rows
            if rule.mode == ChannelPasteTagRuleMode.INCLUDE and self._is_tag_rule_active(rule, now)
        ]
        exclude_tags = [
            self._format_global_paste_tag_rule(rule, tag.slug)
            for rule, tag in rows
            if rule.mode == ChannelPasteTagRuleMode.EXCLUDE and self._is_tag_rule_active(rule, now)
        ]
        expired_count = sum(1 for rule, _tag in rows if self._is_tag_rule_expired(rule, now))
        await self.main_bot.send_message(
            chat_id,
            (
                "Массовые правила паст для всех каналов\n"
                f"Include для всех: {', '.join(include_tags) if include_tags else 'нет'}\n"
                f"Exclude для всех: {', '.join(exclude_tags) if exclude_tags else 'нет'}\n"
                f"Истекших правил: {expired_count}\n\n"
                "Exclude всегда сильнее include. Если глобальный include включён, паста должна иметь этот тег для любого канала."
            ),
            reply_markup=build_global_paste_tag_rule_actions(),
        )

    @staticmethod
    def _split_slug_list(text_value: str) -> list[str]:
        raw_items = text_value.replace("\n", ",").replace(";", ",").split(",")
        if len(raw_items) == 1:
            raw_items = text_value.split()
        return [item.strip() for item in raw_items if item.strip()]

    @staticmethod
    def _is_tag_rule_active(rule: object, now: datetime) -> bool:
        starts_at = MasterBot._normalize_rule_datetime(getattr(rule, "starts_at", None))
        ends_at = MasterBot._normalize_rule_datetime(getattr(rule, "ends_at", None))
        return bool(getattr(rule, "is_active", False)) and (starts_at is None or starts_at <= now) and (ends_at is None or ends_at > now)

    @staticmethod
    def _is_tag_rule_expired(rule: object, now: datetime) -> bool:
        ends_at = MasterBot._normalize_rule_datetime(getattr(rule, "ends_at", None))
        return bool(getattr(rule, "is_active", False)) and ends_at is not None and ends_at <= now

    @staticmethod
    def _normalize_rule_datetime(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _format_global_paste_tag_rule(rule: object, slug: str) -> str:
        ends_at = MasterBot._normalize_rule_datetime(getattr(rule, "ends_at", None))
        if ends_at is None:
            return slug
        local_until = ends_at.astimezone(ZoneInfo("Europe/Moscow"))
        return f"{slug} до {local_until:%d.%m.%Y %H:%M}"

    def _parse_global_paste_tag_rule_input(self, text_value: str) -> tuple[list[str], datetime | None]:
        cleaned = text_value.strip()
        if not cleaned:
            return [], None

        lowered = cleaned.lower()
        for marker in (" до ", " until "):
            marker_index = lowered.rfind(marker)
            if marker_index != -1:
                tag_text = cleaned[:marker_index].strip()
                date_text = cleaned[marker_index + len(marker):].strip()
                ends_at = self._parse_global_rule_until(date_text)
                return self._split_slug_list(tag_text), ends_at

        parts = cleaned.split()
        for date_part_count in (2, 1):
            if len(parts) <= date_part_count:
                continue
            date_text = " ".join(parts[-date_part_count:])
            try:
                ends_at = self._parse_global_rule_until(date_text)
            except ValueError:
                continue
            tag_text = " ".join(parts[:-date_part_count])
            return self._split_slug_list(tag_text), ends_at

        return self._split_slug_list(cleaned), None

    @staticmethod
    def _parse_global_rule_until(date_text: str) -> datetime:
        cleaned = date_text.strip()
        formats = (
            ("%Y-%m-%d %H:%M", False),
            ("%d.%m.%Y %H:%M", False),
            ("%Y-%m-%d", True),
            ("%d.%m.%Y", True),
        )
        for fmt, date_only in formats:
            try:
                parsed = datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
            if date_only:
                parsed = parsed.replace(hour=23, minute=59, second=59)
            local_dt = parsed.replace(tzinfo=ZoneInfo("Europe/Moscow"))
            utc_dt = local_dt.astimezone(timezone.utc)
            if utc_dt <= datetime.now(timezone.utc):
                raise ValueError("Срок действия правила уже прошёл.")
            return utc_dt
        raise ValueError("Не удалось разобрать дату. Используйте формат 2026-08-31 или 31.08.2026 23:59.")

    async def _handle_stateful_admin_text(self, message: Message) -> bool:
        state = self.user_states.get(message.chat.id)
        if not state:
            return False

        action = state["action"]
        if action != "await_import_channel_history":
            self._clear_user_state(message.chat.id)
        text_value = (message.text or message.caption or "").strip()

        if action == "await_confession_publisher_token":
            if not self._is_general_admin(message.from_user.id if message.from_user else message.chat.id):
                await self.main_bot.send_message(message.chat.id, "Только для генерального администратора.")
                return True
            if not text_value:
                self._set_user_state(message.chat.id, action)
                await self.main_bot.send_message(message.chat.id, "Отправьте токен отдельного бота.")
                return True
            publisher_bot = AsyncTeleBot(text_value)
            try:
                bot_info = await publisher_bot.get_me()
            except Exception as exc:
                self._set_user_state(message.chat.id, action)
                await self.main_bot.send_message(message.chat.id, f"Не удалось проверить токен: {exc}")
                return True
            finally:
                try:
                    await publisher_bot.close_session()
                except Exception:
                    pass
            if self.bot_info is not None and bot_info.id == self.bot_info.id:
                await self.main_bot.send_message(
                    message.chat.id,
                    "Нужен отдельный бот, токен главного бота панели использовать нельзя.",
                )
                return True
            publisher = await self.editorial_actions.configure_confession_publisher(
                bot_api_token=text_value,
                bot_user_id=bot_info.id,
                bot_username=bot_info.username,
                created_by=message.from_user.id if message.from_user else message.chat.id,
            )
            await self._start_confession_publisher_runtime(publisher)
            await self.main_bot.send_message(
                message.chat.id,
                (
                    f"Саббот @{bot_info.username} подключён.\n\n"
                    "1. Создайте закрытую группу для паст.\n"
                    f"2. Добавьте @{bot_info.username} в администраторы группы.\n"
                    "3. Отправьте в этой группе команду:\n"
                    f"/connect_confessions {publisher.bind_code}\n\n"
                    "Код действует 30 минут. После привязки все новые сообщения группы будут попадать в базу признавашек."
                ),
            )
            await self._show_confessions_menu(message.chat.id)
            return True

        if action == "await_confession_suggestion_token":
            channel_id = int(state["channel_id"])
            if not self._is_general_admin(message.from_user.id if message.from_user else message.chat.id):
                await self.main_bot.send_message(message.chat.id, "Только для генерального администратора.")
                return True
            channel = await self.editorial_actions.get_channel(channel_id)
            if channel is None or channel.content_family != ContentFamily.CONFESSION.value:
                await self.main_bot.send_message(message.chat.id, "Паблик признавашек не найден.")
                return True
            if not text_value:
                self._set_user_state(message.chat.id, action, channel_id=channel_id)
                await self.main_bot.send_message(message.chat.id, "Отправьте токен отдельного бота-предложки.")
                return True
            result_text, connected_channel_id = await self._add_subbot_from_values(
                text_value,
                channel_tg_id=channel.tg_channel_id,
            )
            await self.main_bot.send_message(message.chat.id, result_text)
            if connected_channel_id is None:
                self._set_user_state(message.chat.id, action, channel_id=channel_id)
                return True
            binding = await self.legacy_reader.get_bot_binding(channel.tg_channel_id)
            bot_label = f"@{binding.bot_username.lstrip('@')}" if binding else "саббота"
            await self.main_bot.send_message(
                message.chat.id,
                (
                    f"Теперь добавьте {bot_label} администратором в чат модерации. "
                    "В этот чат будут приходить сообщения из предложки; функциональность "
                    "модерации и публикации такая же, как у подслушек."
                ),
            )
            await self._show_confession_channel_slots(message.chat.id, channel_id)
            return True

        if action == "await_confession_add_channel":
            if not self._is_general_admin(message.from_user.id if message.from_user else message.chat.id):
                await self.main_bot.send_message(message.chat.id, "Только для генерального администратора.")
                return True
            publisher = await self.editorial_actions.get_active_confession_publisher()
            if publisher is None:
                await self.main_bot.send_message(message.chat.id, "Сначала подключите саббота признавашек.")
                return True
            try:
                chat_ref: int | str = int(text_value)
            except ValueError:
                chat_ref = text_value if text_value.startswith("@") else f"@{text_value}"
            publisher_bot = AsyncTeleBot(publisher.bot_api_token)
            try:
                telegram_chat = await publisher_bot.get_chat(chat_ref)
                if telegram_chat.type != "channel":
                    raise ValueError("Нужен именно Telegram-канал, а не группа или личный чат.")
                member = await publisher_bot.get_chat_member(telegram_chat.id, publisher.bot_user_id)
                raw_member_status = getattr(member, "status", "")
                member_status = str(getattr(raw_member_status, "value", raw_member_status))
                if member_status not in {"administrator", "creator"}:
                    raise ValueError("Саббот не является администратором этого канала.")
                if member_status == "administrator" and not getattr(member, "can_post_messages", False):
                    raise ValueError("У саббота нет права публиковать сообщения в этом канале.")
                channel = await self.editorial_actions.ensure_confession_channel(
                    tg_channel_id=telegram_chat.id,
                    title=getattr(telegram_chat, "title", None),
                    username=getattr(telegram_chat, "username", None),
                )
            except Exception as exc:
                self._set_user_state(message.chat.id, action)
                await self.main_bot.send_message(
                    message.chat.id,
                    f"Не удалось подключить паблик: {exc}\nПроверьте ссылку и права саббота, затем отправьте паблик ещё раз.",
                )
                return True
            finally:
                try:
                    await publisher_bot.close_session()
                except Exception:
                    pass
            await self.main_bot.send_message(
                message.chat.id,
                f"Паблик {self._confession_channel_label(channel)} подключён.",
            )
            await self._show_confession_channel_slots(message.chat.id, channel.id)
            return True

        if action in {"await_confession_add_slot", "await_confession_delete_slots"}:
            channel_id = int(state["channel_id"])
            try:
                weekdays, slot_times = self._parse_slot_input(text_value)
            except ValueError as exc:
                self._set_user_state(message.chat.id, action, channel_id=channel_id)
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\n\nПримеры:\n10:00 15:00\nall 10:00 15:00\n0,2,4 18:00 21:00",
                )
                return True
            if action == "await_confession_add_slot":
                changed_count = await self.editorial_actions.add_slots(channel_id, slot_times, weekdays)
                result_label = "Добавлено"
            else:
                changed_count = await self.editorial_actions.remove_slots(channel_id, slot_times, weekdays)
                result_label = "Удалено"
            await self.main_bot.send_message(message.chat.id, f"{result_label} слотов: {changed_count}.")
            await self._show_confession_channel_slots(message.chat.id, channel_id)
            return True

        if action == "await_paste_jump":
            await self._show_paste_from_input(message.chat.id, text_value)
            return True

        if action == "await_channel_jump":
            await self._show_channels_menu_for_input(message.chat.id, text_value)
            return True

        if action == "await_add_ad_link_exclusion":
            if not text_value:
                self._set_user_state(message.chat.id, action)
                await self.main_bot.send_message(message.chat.id, "Отправьте ссылку для исключения.")
                return True
            try:
                exclusion, created = await self.editorial_actions.add_ad_link_exclusion(
                    url=text_value,
                    created_by=message.from_user.id if message.from_user else message.chat.id,
                )
            except ValueError as exc:
                self._set_user_state(message.chat.id, action)
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\nОтправьте ссылку ещё раз.",
                )
                return True
            result_text = (
                f"Ссылка добавлена в исключения: {exclusion.url}"
                if created
                else f"Ссылка уже есть в исключениях: {exclusion.url}"
            )
            await self.main_bot.send_message(message.chat.id, result_text)
            await self._show_ad_link_exclusions_panel(message.chat.id)
            return True

        if action == "await_delete_ad_link_exclusion":
            if not text_value:
                self._set_user_state(message.chat.id, action)
                await self.main_bot.send_message(message.chat.id, "Отправьте ссылку, которую нужно удалить.")
                return True
            try:
                exclusion = await self.editorial_actions.delete_ad_link_exclusion(url=text_value)
            except ValueError as exc:
                self._set_user_state(message.chat.id, action)
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\nОтправьте ссылку ещё раз.",
                )
                return True
            await self.main_bot.send_message(
                message.chat.id,
                f"Ссылка удалена из исключений: {exclusion.url}",
            )
            await self._show_ad_link_exclusions_panel(message.chat.id)
            return True

        if action == "await_statistics_delta_days":
            try:
                delta_days = validate_statistics_delta_days(text_value)
            except ValueError:
                self._set_user_state(message.chat.id, "await_statistics_delta_days")
                await self.main_bot.send_message(
                    message.chat.id,
                    f"Введите число дней от 1 до {MAX_STATISTICS_DELTA_DAYS}. Например: {DEFAULT_STATISTICS_DELTA_DAYS}",
                )
                return True
            await self._send_statistics_export(message.chat.id, delta_days=delta_days)
            return True

        if action == "await_admin_statistics_month":
            if not self._is_general_admin(message.from_user.id if message.from_user else message.chat.id):
                await self.main_bot.send_message(message.chat.id, "Доступно только генеральному администратору.")
                return True
            try:
                month = parse_admin_statistics_month(text_value)
            except ValueError as exc:
                self._set_user_state(message.chat.id, "await_admin_statistics_month")
                await self.main_bot.send_message(message.chat.id, str(exc))
                return True
            await self._send_admin_statistics_export(message.chat.id, month)
            return True

        if action == "await_add_moderator":
            try:
                user_id = int(text_value)
            except ValueError:
                await self.main_bot.send_message(message.chat.id, "Нужен числовой Telegram user id.")
                return True
            await self.bot_admins_database.add_admin({"user_id": user_id})
            settings.moderators.add(user_id)
            self.chats.add(user_id)
            await self.main_bot.send_message(message.chat.id, f"Модератор {user_id} добавлен.")
            return True

        if action == "await_add_subbot":
            parts = text_value.split(maxsplit=1)
            if len(parts) != 2:
                await self.main_bot.send_message(message.chat.id, "Отправьте строку в формате: <api_token> @channel_username")
                return True
            api_token, channel_username = parts
            result_text, channel_id = await self._add_subbot_from_values(api_token, channel_username)
            await self.main_bot.send_message(message.chat.id, result_text)
            if channel_id is not None:
                await self._send_channel_history_import_offer(message.chat.id, channel_id)
            return True

        if action == "await_import_channel_history":
            channel_id = state["channel_id"]
            source_chat_id, source_message_id, original_published_at = self._extract_forwarded_channel_payload(message)
            channel = await self.editorial_actions.get_channel(channel_id)
            if channel is None:
                self._clear_user_state(message.chat.id)
                await self.main_bot.send_message(message.chat.id, f"Канал {channel_id} не найден.")
                return True
            if source_chat_id is None or source_message_id is None:
                await self.main_bot.send_message(
                    message.chat.id,
                    "Перешлите именно сообщение из нужного канала, а не скопированный текст.",
                    reply_markup=build_channel_history_import_progress_actions(channel_id),
                )
                return True
            if int(source_chat_id) != int(channel.tg_channel_id):
                await self.main_bot.send_message(
                    message.chat.id,
                    "Это сообщение переслано не из выбранного канала. Перешлите пост именно из нужного канала.",
                    reply_markup=build_channel_history_import_progress_actions(channel_id),
                )
                return True

            import_result = await self.editorial_actions.import_channel_history_message(
                channel_id=channel_id,
                source_chat_id=int(source_chat_id),
                source_message_id=int(source_message_id),
                content_type=message.content_type or "text",
                raw_text=(message.text or message.caption or "").strip() or None,
                original_published_at=original_published_at,
                imported_by=message.from_user.id if message.from_user else message.chat.id,
            )

            if import_result.duplicate:
                state["history_duplicates"] = int(state.get("history_duplicates", 0)) + 1
            elif import_result.saved:
                state["history_saved"] = int(state.get("history_saved", 0)) + 1

            if import_result.matched_paste_id is not None and not import_result.duplicate:
                state["history_matches"] = int(state.get("history_matches", 0)) + 1

            return True

        if action == "await_add_slot":
            channel_id = state["channel_id"]
            try:
                weekdays, slot_times = self._parse_slot_input(text_value)
            except ValueError as exc:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\n\nПримеры:\n10:00 15:00 16:00\nall 10:00 15:00\n0 09:30 18:00\n0,2,4 18:00 21:00",
                )
                return True
            created_count = await self.editorial_actions.add_slots(channel_id, slot_times, weekdays)
            label = await self._get_channel_label(channel_id)
            await self.main_bot.send_message(
                message.chat.id,
                f"Для {label} добавлено слотов: {created_count}.",
            )
            await self._show_channel_slots_menu(message.chat.id, channel_id)
            return True

        if action == "await_delete_slots":
            channel_id = state["channel_id"]
            try:
                weekdays, slot_times = self._parse_slot_input(text_value)
            except ValueError as exc:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\n\nПримеры:\n10:00 15:00 16:00\nall 10:00 15:00\n0 09:30 18:00\n0,2,4 18:00 21:00",
                )
                return True
            removed_count = await self.editorial_actions.remove_slots(channel_id, slot_times, weekdays)
            label = await self._get_channel_label(channel_id)
            await self.main_bot.send_message(
                message.chat.id,
                f"Для {label} удалено слотов: {removed_count}.",
            )
            await self._show_channel_slots_menu(message.chat.id, channel_id)
            return True

        if action in {"await_add_ad_blackout", "await_confession_add_ad_blackout"}:
            channel_id = state["channel_id"]
            is_confession = action == "await_confession_add_ad_blackout"
            try:
                day_of_month, start_time, end_time = self._parse_ad_blackout_input(text_value)
                blackout = await self.editorial_actions.create_channel_ad_blackout(
                    channel_id=channel_id,
                    day_of_month=day_of_month,
                    start_time=start_time,
                    end_time=end_time,
                    created_by=message.from_user.id if message.from_user else message.chat.id,
                )
            except ValueError as exc:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\n\nПример:\n21 15:00 18:00",
                )
                return True

            label = await self._get_channel_label(channel_id)
            await self.main_bot.send_message(
                message.chat.id,
                f"Для {label} добавлено рекламное окно:\n{await self._format_ad_blackout(channel_id, blackout)}",
            )
            if is_confession:
                await self._show_confession_channel_slots(message.chat.id, channel_id)
            else:
                await self._show_channel_menu(
                    message.chat.id,
                    channel_id,
                    user_id=message.from_user.id if message.from_user else message.chat.id,
                )
            return True

        if action in {"await_delete_ad_blackout", "await_confession_delete_ad_blackout"}:
            channel_id = state["channel_id"]
            is_confession = action == "await_confession_delete_ad_blackout"
            try:
                day_of_month, start_time, end_time = self._parse_ad_blackout_input(text_value)
                blackout = await self.editorial_actions.delete_channel_ad_blackout(
                    channel_id=channel_id,
                    day_of_month=day_of_month,
                    start_time=start_time,
                    end_time=end_time,
                )
            except ValueError as exc:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\n\n\u041f\u0440\u0438\u043c\u0435\u0440:\n21 15:00 18:00",
                )
                return True

            label = await self._get_channel_label(channel_id)
            await self.main_bot.send_message(
                message.chat.id,
                f"\u0414\u043b\u044f {label} \u0443\u0434\u0430\u043b\u0435\u043d\u043e \u0440\u0435\u043a\u043b\u0430\u043c\u043d\u043e\u0435 \u043e\u043a\u043d\u043e:\n{await self._format_ad_blackout(channel_id, blackout)}",
            )
            if is_confession:
                await self._show_confession_channel_slots(message.chat.id, channel_id)
            else:
                await self._show_channel_menu(
                    message.chat.id,
                    channel_id,
                    user_id=message.from_user.id if message.from_user else message.chat.id,
                )
            return True

        if action == "await_update_channel_setting":
            channel_id = state["channel_id"]
            try:
                field_name, raw_setting_value = self._parse_channel_setting_input(text_value)
                channel = await self.editorial_actions.update_channel_setting(
                    channel_id=channel_id,
                    field_name=field_name,
                    raw_value=raw_setting_value,
                )
            except ValueError as exc:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\n\nПример:\nsame_tag_cooldown_hours 24\nallow_pastes false",
                )
                await self._send_channel_settings_prompt(message.chat.id, channel_id)
                return True
            await self.main_bot.send_message(
                message.chat.id,
                f"Параметр {field_name} обновлён: {self._format_channel_setting_value(getattr(channel, field_name))}.",
            )
            await self._show_channel_menu(message.chat.id, channel_id, user_id=message.from_user.id if message.from_user else message.chat.id)
            return True

        if action == "await_profile_setting_update":
            meta_fields = {"title", "min_subscribers", "max_subscribers", "priority", "is_active"}
            profile_slug_from_state = state.get("profile_slug")
            try:
                profile_slug, field_name, raw_profile_value = self._parse_profile_setting_input(
                    text_value,
                    profile_slug=profile_slug_from_state,
                )
                if field_name in meta_fields:
                    profile = await self.editorial_actions.update_channel_setting_profile_meta(
                        slug=profile_slug,
                        field_name=field_name,
                        raw_value=raw_profile_value,
                    )
                else:
                    profile = await self.editorial_actions.update_channel_setting_profile_field(
                        slug=profile_slug,
                        field_name=field_name,
                        raw_value=raw_profile_value,
                    )
            except ValueError as exc:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\n\nExample:\nmax_posts_per_day 8\nauto_slots_window_start 10:00\nmax_subscribers none",
                )
                if profile_slug_from_state:
                    await self._send_profile_settings_prompt(message.chat.id, profile_slug_from_state)
                return True

            self._clear_user_state(message.chat.id)
            await self.main_bot.send_message(
                message.chat.id,
                f"Profile {profile.slug} updated: {field_name} = {raw_profile_value}",
            )
            await self._show_profile_card(message.chat.id, profile.slug)
            return True

        if action == "await_profile_create":
            try:
                profile_slug, title, min_subscribers, max_subscribers = self._parse_profile_create_input(text_value)
                profile = await self.editorial_actions.upsert_channel_setting_profile(
                    slug=profile_slug,
                    title=title,
                    min_subscribers=min_subscribers,
                    max_subscribers=max_subscribers,
                )
            except ValueError as exc:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\n\nExample:\nsmall :: Small channels :: 0 :: 49\nhuge :: Huge channels :: 10000 :: none",
                )
                await self._send_profile_create_prompt(message.chat.id)
                return True

            self._clear_user_state(message.chat.id)
            await self.main_bot.send_message(message.chat.id, f"Profile {profile.slug} created.")
            await self._show_profile_card(message.chat.id, profile.slug)
            return True

        if action == "await_profile_assign_channels":
            channels = await self.editorial_actions.list_profile_channels()
            profile_slug = state.get("profile_slug")
            if not profile_slug:
                self._clear_user_state(message.chat.id)
                await self.main_bot.send_message(message.chat.id, "Profile was not selected. Open Profiles again.")
                await self._show_profiles_menu(message.chat.id)
                return True
            try:
                channel_ids = self._parse_channel_id_list(text_value, {channel.id for channel in channels})
                updated_channels = await self.editorial_actions.apply_profile_to_channels(
                    channel_ids=channel_ids,
                    profile_slug=profile_slug,
                    auto_enabled=False,
                )
            except ValueError as exc:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"{exc}\n\nExample:\n1,2,3\n1-20\nall",
                )
                await self._send_profile_assign_channels_prompt(message.chat.id, profile_slug)
                return True

            self._clear_user_state(message.chat.id)
            await self.main_bot.send_message(
                message.chat.id,
                f"Profile {profile_slug} applied to {len(updated_channels)} channels. Mode: manual.",
            )
            await self._show_profile_card(message.chat.id, profile_slug)
            return True

        if action == "await_generate_posts":
            channel_id = state["channel_id"]
            try:
                variant_count = int(text_value)
            except ValueError:
                await self.main_bot.send_message(
                    message.chat.id,
                    "\u041d\u0443\u0436\u043d\u043e \u0447\u0438\u0441\u043b\u043e \u0432\u0430\u0440\u0438\u0430\u043d\u0442\u043e\u0432. \u041d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: 3",
                )
                return True
            if variant_count < 1 or variant_count > 10:
                await self.main_bot.send_message(
                    message.chat.id,
                    "\u041c\u043e\u0436\u043d\u043e \u0441\u0433\u0435\u043d\u0435\u0440\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u043e\u0442 1 \u0434\u043e 10 \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a\u043e\u0432 \u0437\u0430 \u043e\u0434\u0438\u043d \u0437\u0430\u043f\u0443\u0441\u043a.",
                )
                return True

            label = await self._get_channel_label(channel_id)
            status_message = await self.main_bot.send_message(
                message.chat.id,
                f"\u0413\u0435\u043d\u0435\u0440\u0438\u0440\u0443\u044e {variant_count} \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a\u043e\u0432 \u0434\u043b\u044f {label}. \u042d\u0442\u043e \u043c\u043e\u0436\u0435\u0442 \u0437\u0430\u043d\u044f\u0442\u044c \u0434\u043e \u043c\u0438\u043d\u0443\u0442\u044b.",
            )
            try:
                run = await self.editorial_actions.run_generation(
                    channel_id=channel_id,
                    variant_count=variant_count,
                    source_count=max(8, variant_count * 3),
                )
            except Exception as ex:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u044c \u0433\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u044e: {ex}",
                )
                return True
            finally:
                try:
                    await self.main_bot.delete_message(message.chat.id, status_message.message_id)
                except Exception:
                    pass

            if run.generated_count:
                await self.main_bot.send_message(
                    message.chat.id,
                    (
                        f"\u0413\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u044f \u0433\u043e\u0442\u043e\u0432\u0430.\n"
                        f"Generation run #{run.id}\n"
                        f"\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u043e\u0432: {run.source_count}\n"
                        f"\u0421\u043e\u0437\u0434\u0430\u043d\u043e \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a\u043e\u0432: {run.generated_count}\n\n"
                        "\u0421\u0435\u0439\u0447\u0430\u0441 \u043e\u043d\u0438 \u043b\u0435\u0436\u0430\u0442 \u0432 '\u0427\u0435\u0440\u043d\u043e\u0432\u0438\u043a\u0438 \u043d\u0430 review'."
                    ),
                )
                await self._show_first_pending_content(message.chat.id)
                return True

            error_text = f"\n\u041e\u0448\u0438\u0431\u043a\u0430: {run.error_text}" if run.error_text else ""
            await self.main_bot.send_message(
                message.chat.id,
                (
                    f"\u0413\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u044f \u043d\u0435 \u0441\u043e\u0437\u0434\u0430\u043b\u0430 \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a\u0438.\n"
                    f"Generation run #{run.id}\n"
                    f"\u0421\u0442\u0430\u0442\u0443\u0441: {run.status}\n"
                    f"\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u043e\u0432: {run.source_count}"
                    f"{error_text}"
                ),
            )
            await self._show_channel_menu(
                message.chat.id,
                channel_id,
                user_id=message.from_user.id if message.from_user else message.chat.id,
            )
            return True

        if action == "await_edit_content_item":
            content_item_id = int(state["content_item_id"])
            if not text_value:
                self._set_user_state(message.chat.id, "await_edit_content_item", content_item_id=content_item_id)
                await self.main_bot.send_message(
                    message.chat.id,
                    "\u041d\u0443\u0436\u0435\u043d \u043d\u043e\u0432\u044b\u0439 \u0442\u0435\u043a\u0441\u0442 \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a\u0430.",
                )
                return True
            try:
                item = await self.editorial_actions.update_content_item_text(content_item_id, text_value)
            except Exception as ex:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0438\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a: {ex}",
                )
                return True
            await self.main_bot.send_message(
                message.chat.id,
                f"\u0427\u0435\u0440\u043d\u043e\u0432\u0438\u043a #{content_item_id} \u043e\u0431\u043d\u043e\u0432\u043b\u0451\u043d.",
            )
            await self.main_bot.send_message(
                message.chat.id,
                await self._format_content_item(item),
                reply_markup=build_content_actions(item.id, has_next=False),
            )
            return True

        if action == "await_add_paste":
            body_text = text_value
            if not body_text:
                await self.main_bot.send_message(message.chat.id, "Нужен текст пасты.")
                return True
            title = None
            if "::" in body_text:
                raw_title, raw_body = body_text.split("::", maxsplit=1)
                if raw_title.strip() and raw_body.strip():
                    title = raw_title.strip()
                    body_text = raw_body.strip()
            paste = await self.editorial_actions.create_manual_paste(
                body_text=body_text,
                reviewer_id=message.from_user.id if message.from_user else message.chat.id,
                title=title,
            )
            await self.main_bot.send_message(message.chat.id, f"Паста #{paste.id} добавлена: {paste.title}")
            await self._show_first_paste(message.chat.id, current_id=paste.id)
            return True

        if action == "await_add_tag":
            if not text_value:
                await self.main_bot.send_message(message.chat.id, "Нужен slug тега или строка 'slug :: Название'.")
                return True
            title = None
            slug = text_value
            if "::" in text_value:
                slug, title = [part.strip() for part in text_value.split("::", maxsplit=1)]
            try:
                tag = await self.editorial_actions.create_tag(
                    slug=slug,
                    title=title,
                    created_by=message.from_user.id if message.from_user else message.chat.id,
                )
            except ValueError as exc:
                await self.main_bot.send_message(message.chat.id, str(exc))
                return True
            await self.main_bot.send_message(message.chat.id, f"Тег {tag.slug} добавлен.")
            await self._show_tag_card(message.chat.id, tag.slug)
            return True

        if action == "await_add_tag_keywords":
            slug = state["slug"]
            keywords = self._split_slug_list(text_value)
            if not keywords:
                await self.main_bot.send_message(message.chat.id, "Нужны ключевые слова через запятую или пробел.")
                return True
            try:
                created = await self.editorial_actions.add_tag_keywords(tag_slug=slug, keywords=keywords)
            except ValueError as exc:
                await self.main_bot.send_message(message.chat.id, str(exc))
                return True
            await self.main_bot.send_message(message.chat.id, f"Добавлено/включено ключевых слов: {len(created)}.")
            await self._show_tag_card(message.chat.id, slug)
            return True

        if action == "await_add_paste_manual_tag":
            paste_id = int(state["paste_id"])
            try:
                summary = await self.editorial_actions.add_paste_manual_tag(
                    paste_id=paste_id,
                    tag_slug=text_value,
                    created_by=message.from_user.id if message.from_user else message.chat.id,
                )
            except ValueError as exc:
                await self.main_bot.send_message(message.chat.id, str(exc))
                return True
            await self.main_bot.send_message(message.chat.id, f"Теги пасты обновлены: {', '.join(summary.all_tags) or 'нет'}.")
            await self._show_paste_tags(message.chat.id, paste_id)
            return True

        if action == "await_remove_paste_manual_tag":
            paste_id = int(state["paste_id"])
            try:
                summary = await self.editorial_actions.remove_paste_manual_tag(paste_id=paste_id, tag_slug=text_value)
            except ValueError as exc:
                await self.main_bot.send_message(message.chat.id, str(exc))
                return True
            await self.main_bot.send_message(message.chat.id, f"Теги пасты обновлены: {', '.join(summary.all_tags) or 'нет'}.")
            await self._show_paste_tags(message.chat.id, paste_id)
            return True

        if action == "await_set_paste_primary_tag":
            paste_id = int(state["paste_id"])
            try:
                summary = await self.editorial_actions.set_paste_primary_tag(paste_id=paste_id, tag_slug=text_value)
            except ValueError as exc:
                await self.main_bot.send_message(message.chat.id, str(exc))
                return True
            await self.main_bot.send_message(message.chat.id, f"Основной тег пасты: {summary.primary_tag or '-'}.")
            await self._show_paste_tags(message.chat.id, paste_id)
            return True

        if action == "await_channel_paste_tag_rule":
            channel_id = int(state["channel_id"])
            mode = ChannelPasteTagRuleMode(state["mode"])
            operation = state["operation"]
            tag_slugs = self._split_slug_list(text_value)
            if not tag_slugs:
                await self.main_bot.send_message(message.chat.id, "Нужен slug тега.")
                return True
            changed = 0
            try:
                for tag_slug in tag_slugs:
                    if operation == "add":
                        await self.editorial_actions.add_channel_paste_tag_rule(
                            channel_id=channel_id,
                            tag_slug=tag_slug,
                            mode=mode,
                            created_by=message.from_user.id if message.from_user else message.chat.id,
                        )
                        changed += 1
                    else:
                        changed += await self.editorial_actions.remove_channel_paste_tag_rule(
                            channel_id=channel_id,
                            tag_slug=tag_slug,
                            mode=mode,
                        )
            except ValueError as exc:
                await self.main_bot.send_message(message.chat.id, str(exc))
                return True
            await self.main_bot.send_message(message.chat.id, f"Правила обновлены: {changed}.")
            await self._show_channel_paste_tags(message.chat.id, channel_id)
            return True

        if action == "await_global_paste_tag_rule":
            mode = ChannelPasteTagRuleMode(state["mode"])
            operation = state["operation"]
            if operation == "add":
                try:
                    tag_slugs, ends_at = self._parse_global_paste_tag_rule_input(text_value)
                except ValueError as exc:
                    await self.main_bot.send_message(message.chat.id, str(exc))
                    return True
            else:
                tag_slugs = self._split_slug_list(text_value)
                ends_at = None
            if not tag_slugs:
                await self.main_bot.send_message(message.chat.id, "Нужен slug тега.")
                return True

            changed = 0
            try:
                for tag_slug in tag_slugs:
                    if operation == "add":
                        await self.editorial_actions.add_global_paste_tag_rule(
                            tag_slug=tag_slug,
                            mode=mode,
                            ends_at=ends_at,
                            created_by=message.from_user.id if message.from_user else message.chat.id,
                        )
                        changed += 1
                    else:
                        changed += await self.editorial_actions.remove_global_paste_tag_rule(
                            tag_slug=tag_slug,
                            mode=mode,
                        )
            except ValueError as exc:
                await self.main_bot.send_message(message.chat.id, str(exc))
                return True

            mode_label = "Include" if mode == ChannelPasteTagRuleMode.INCLUDE else "Exclude"
            await self.main_bot.send_message(message.chat.id, f"Массовые правила {mode_label} обновлены: {changed}.")
            await self._show_global_paste_tags(message.chat.id)
            return True

        if action == "await_manual_channel_message":
            channel_ids = [int(channel_id) for channel_id in state.get("channel_ids", [])]
            if not text_value:
                self._set_user_state(message.chat.id, "await_manual_channel_message", channel_ids=channel_ids)
                await self.main_bot.send_message(
                    message.chat.id,
                    "\u041d\u0443\u0436\u0435\u043d \u0442\u0435\u043a\u0441\u0442, \u043a\u043e\u0442\u043e\u0440\u044b\u0439 \u043d\u0443\u0436\u043d\u043e \u043e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u0442\u044c.",
                )
                return True
            try:
                result = await self.editorial_actions.publish_manual_message_to_channels(
                    channel_ids=channel_ids,
                    moderator_id=message.from_user.id if message.from_user else message.chat.id,
                    body_text=text_value,
                )
            except Exception as ex:
                await self.main_bot.send_message(
                    message.chat.id,
                    f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0440\u0443\u0447\u043d\u043e\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435: {ex}",
                )
                return True
            await self.main_bot.send_message(
                message.chat.id,
                self._format_manual_channel_message_result(result),
            )
            await self._show_my_channels_menu(
                message.chat.id,
                user_id=message.from_user.id if message.from_user else message.chat.id,
            )
            return True

        if action == "await_reply_submission":
            submission_id = state["submission_id"]
            if not text_value:
                await self.main_bot.send_message(message.chat.id, "Нужен текст сообщения для пользователя.")
                return True
            try:
                await self.editorial_actions.reply_to_submission_author(submission_id, text_value)
            except Exception as ex:
                await self.main_bot.send_message(message.chat.id, f"Не удалось отправить сообщение: {ex}")
                return True
            await self.main_bot.send_message(message.chat.id, f"Ответ пользователю по сообщению {submission_id} отправлен.")
            return True

        if action == "await_sql_export":
            if not text_value:
                await self.main_bot.send_message(message.chat.id, "Нужен SQL-запрос.")
                return True
            await self._send_sql_export_result(
                chat_id=message.chat.id,
                query=text_value,
                allow_mutating=bool(state.get("allow_mutating")),
            )
            return True

        return False

    @logger.catch
    async def __send_post(self, delayed_post) -> None:
        tz = timezone(timedelta(hours=3))
        now = datetime.now(tz)
        bots_data = {bot.bot_info.id: bot for bot in self.bots_work}

        public_data = []
        for bot, info_post in delayed_post.items():
            for message_id, info in info_post:
                time_post, sender_id = info
                if now.timestamp() >= time_post:
                    public_data.append((message_id, sender_id, bot, time_post))

        for message_id, sender_id, bot, time_post in public_data:
            try:
                if not await bots_data[bot].is_delayed_publication_current(message_id, sender_id, time_post):
                    continue
                if await bots_data[bot].reschedule_delayed_if_publication_blocked(message_id, sender_id, time_post):
                    continue
                sent = await asyncio.wait_for(
                    bots_data[bot].send_delayed_message(message_id, sender_id, time_post),
                    timeout=settings.telegram_request_timeout_seconds,
                )
                if not sent:
                    continue
            except Exception as ex:
                logger.error(
                    "Failed to publish legacy delayed message {} for bot {}: {}",
                    message_id,
                    bot,
                    ex,
                )

    @logger.catch
    async def __delayed_posts_checker(self) -> None:
        poll_interval = min(settings.const_time_sleep, 5)
        while True:
            delayed_posts = {}
            for bot in self.bots_work:
                delayed_message = await bot.getter_delayed_info()
                info_lst = sorted(delayed_message.items(), key=lambda item: (item[1], item[0]))
                delayed_posts[bot.bot_info.id] = info_lst
            await self.__send_post(delayed_posts)
            await asyncio.sleep(poll_interval)

    @staticmethod
    def _next_subscriber_snapshot_run_at(now_msk: datetime) -> datetime:
        next_run = now_msk.replace(hour=2, minute=0, second=0, microsecond=0)
        if now_msk >= next_run:
            next_run += timedelta(days=1)
        return next_run

    @logger.catch
    async def __subscriber_snapshot_scheduler(self) -> None:
        while True:
            now_msk = datetime.now(ZoneInfo("Europe/Moscow"))
            next_run = self._next_subscriber_snapshot_run_at(now_msk)
            await asyncio.sleep(max((next_run - now_msk).total_seconds(), 1))

            try:
                result = await self.editorial_actions.record_daily_subscriber_snapshots()
                logger.info(
                    "Daily subscriber snapshots finished: checked={}, updated={}, recorded={}, deleted={}, failed={}",
                    result.channels_checked,
                    result.subscriber_counts_updated,
                    result.snapshots_recorded,
                    result.snapshots_deleted,
                    result.failed,
                )
            except Exception as ex:
                logger.exception("Failed to record daily subscriber snapshots: {}", ex)

    @logger.catch
    async def callback_adv_send_message(self, call: CallbackQuery, channel_username: str, info_sender: User) -> None:
        text_adv = call.message.text if call.message.text is not None else call.message.caption
        message = (
            f"<b>реклама:</b> {channel_username}\n"
            f"<blockquote expandable>{text_adv if text_adv is not None else 'текста нет'}</blockquote>\n"
            f"отправитель: {info_sender.id if info_sender.username is None else ('@' + info_sender.username)}, "
            f"ник: <b>{info_sender.first_name}</b>\n"
        )
        for adv_admin in settings.advertiser:
            try:
                await self.main_bot.send_message(
                    text=message,
                    chat_id=adv_admin,
                    parse_mode="HTML",
                )
            except Exception as ex:
                logger.error(ex)

    @logger.catch
    async def callback_adv_send_message_v2(
        self,
        call: CallbackQuery,
        source_bot: AsyncTeleBot,
        channel_label: str,
        info_sender: User,
        source_text: str | None = None,
    ) -> None:
        await send_advertising_flow(
            bot=source_bot,
            recipient_user_id=info_sender.id,
            channel_label=channel_label,
            source_text=source_text or call.message.text or call.message.caption,
            sender_username=info_sender.username,
            sender_first_name=info_sender.first_name,
        )

    def __setup_handlers(self) -> None:
        @self.main_bot.message_handler(commands=["start", "panel"])
        async def start(message: Message) -> None:
            if not self._is_admin(message.chat.id):
                await self.main_bot.send_message(
                    message.chat.id,
                    "Это служебный бот сети. Панель доступна только администраторам.",
                )
                return
            await self._answer_panel(message.chat.id)

        @self.main_bot.message_handler(commands=["push"])
        @filter_admin
        async def register_push_message(message: Message) -> None:
            await self.main_bot.send_message(settings.general_admin, "Отправьте пост, который нужно разослать.")
            self.flag_register_push_message = True

        @self.main_bot.message_handler(commands=["bots"])
        @filter_admin
        async def get_all_subbot(message: Message) -> None:
            await self.main_bot.send_message(message.chat.id, await self._list_subbots_text())

        @self.main_bot.message_handler(commands=["add"])
        @filter_admin
        async def add_bot(message: Message) -> None:
            parts = message.text.split(maxsplit=2)
            if len(parts) != 3:
                await self.main_bot.send_message(message.chat.id, "Формат команды: /add <token> @channel")
                return
            _command, api_token, channel_username = parts
            result_text, channel_id = await self._add_subbot_from_values(api_token, channel_username)
            await self.main_bot.send_message(message.chat.id, result_text)
            if channel_id is not None:
                await self._send_channel_history_import_offer(message.chat.id, channel_id)

        @self.main_bot.message_handler(commands=["delete"])
        @filter_admin
        async def remove_bot(message: Message) -> None:
            parts = message.text.split(maxsplit=2)
            if len(parts) != 3:
                await self.main_bot.send_message(message.chat.id, "Формат команды: /delete @bot @channel")
                return
            _command, username_bot, channel_username = parts
            channel_username = await self._normalize_channel_username(channel_username)
            try:
                channel_id = (await self.main_bot.get_chat(channel_username)).id
            except Exception:
                await self.main_bot.send_message(message.chat.id, "Канал не найден.")
                return
            result_text = await self._remove_subbot_from_values(username_bot, channel_id)
            await self.main_bot.send_message(message.chat.id, result_text)

        @self.main_bot.message_handler(content_types=["text", "video", "photo"])
        @filter_admin
        async def push_msg(message: Message) -> None:
            if await self._handle_stateful_admin_text(message):
                return

            if self.flag_register_push_message:
                markup = InlineKeyboardMarkup()
                self.flag_register_push_message = False
                message_id_push = message.id
                markup.add(
                    InlineKeyboardButton(text="Опубликовать", callback_data=f"push;{message_id_push}"),
                    InlineKeyboardButton(text="Удалить", callback_data=f"reject_push;{message_id_push}"),
                )
                await self.main_bot.copy_message(
                    from_chat_id=settings.general_admin,
                    chat_id=settings.general_admin,
                    message_id=message_id_push,
                    reply_markup=markup,
                )

        @logger.catch
        @self.main_bot.callback_query_handler(func=lambda call: True)
        async def callback(call: CallbackQuery) -> None:
            if not self._is_admin(call.from_user.id):
                await self.main_bot.answer_callback_query(call.id, "Доступно только администраторам.", show_alert=True)
                return

            data = call.data
            if data.startswith("panel:"):
                action = data.split(":")[1]
                page = self._parse_panel_page(data)
                answered_early = False
                match action:
                    case "main":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._answer_panel(call.message.chat.id)
                    case "import":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        result = await self.editorial_actions.import_new()
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            f"Импорт завершен.\nПросмотрено: {result.scanned}\nИмпортировано: {result.imported}\n"
                            f"Пропущено дублей: {result.skipped_duplicates}\nСоздано каналов: {result.channels_created}",
                        )
                    case "submissions":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self.editorial_actions.import_new()
                        await self._show_first_pending_submission(call.message.chat.id, user_id=call.from_user.id)
                    case "all_submissions":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_submission_history(call.message.chat.id)
                    case "content":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_first_pending_content(call.message.chat.id)
                    case "pastes":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_first_paste(call.message.chat.id)
                    case "confessions":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_confessions_menu(call.message.chat.id)
                    case "tags":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_tags_menu(call.message.chat.id)
                    case "channels":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_channels_menu(call.message.chat.id, page=page)
                    case "my_channels":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_my_channels_menu(call.message.chat.id, user_id=call.from_user.id)
                    case "profiles":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_profiles_menu(call.message.chat.id)
                    case "scheduler":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        result = await self.editorial_actions.run_scheduler()
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            f"Scheduler отработал.\nКаналов: {result.channels_checked}\n"
                            f"Слотов: {result.slots_checked}\nЗапланировано: {result.scheduled_items}",
                        )
                    case "auto_slots":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        result = await self.editorial_actions.run_auto_slot_planner()
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            "Auto slots finished.\n"
                            f"Channels checked: {result.channels_checked}\n"
                            f"Channels planned: {result.channels_planned}\n"
                            f"Slots deleted: {result.slots_deleted}\n"
                            f"Slots created: {result.slots_created}",
                        )
                    case "profile_sync":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        result = await self.editorial_actions.sync_channel_profiles()
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            "Channel profiles synced.\n"
                            f"Channels checked: {result.channels_checked}\n"
                            f"Subscriber counts updated: {result.subscriber_counts_updated}\n"
                            f"Profiles changed: {result.profiles_changed}\n"
                            f"Skipped manual: {result.skipped_manual}\n"
                            f"Failed: {result.failed}",
                        )
                    case "publisher":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        result = await self.editorial_actions.run_publisher()
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            f"Publisher отработал.\nПопыток: {result.attempted}\n"
                            f"Успешно: {result.sent}\n"
                            f"Отложено до восстановления Telegram: {result.deferred}\n"
                            f"Ошибок: {result.failed}",
                        )
                    case "ad_link_exclusions":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_ad_link_exclusions_panel(call.message.chat.id)
                    case "add_ad_link_exclusion":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        self._set_user_state(call.message.chat.id, "await_add_ad_link_exclusion")
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            (
                                "Отправьте одну ссылку. Она и все вложенные адреса будут "
                                "игнорироваться при автоматическом создании часовых рекламных окон."
                            ),
                        )
                    case "delete_ad_link_exclusion":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        self._set_user_state(call.message.chat.id, "await_delete_ad_link_exclusion")
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            "Отправьте ссылку, которую нужно удалить из списка исключений.",
                        )
                    case "extra":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_extra_panel(call.message.chat.id)
                    case "db_export":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        await self._send_database_export(call.message.chat.id)
                        return
                    case "stats_export":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        self._set_user_state(call.message.chat.id, "await_statistics_delta_days")
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            (
                                "Введите количество дней для расчёта дельты "
                                f"от 1 до {MAX_STATISTICS_DELTA_DAYS}.\n"
                                f"Например: {DEFAULT_STATISTICS_DELTA_DAYS}"
                            ),
                        )
                        return
                    case "admin_stats_export":
                        if not self._is_general_admin(call.from_user.id):
                            await self.main_bot.answer_callback_query(
                                call.id,
                                "Доступно только генеральному администратору.",
                                show_alert=True,
                            )
                            return
                        await self._safe_answer_callback(self.main_bot, call.id)
                        self._set_user_state(call.message.chat.id, "await_admin_statistics_month")
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            (
                                "Введите месяц в формате ГГГГ-ММ или ММ.ГГГГ.\n"
                                "Например: 2026-08\n"
                                "Для текущего месяца можно написать: текущий"
                            ),
                        )
                        return
                    case "sql_export":
                        await self._safe_answer_callback(self.main_bot, call.id)
                        await self._send_sql_export_prompt(
                            chat_id=call.message.chat.id,
                            allow_mutating=self._is_general_admin(call.from_user.id),
                        )
                        return
                    case "admins":
                        if not self._is_general_admin(call.from_user.id):
                            await self.main_bot.answer_callback_query(call.id, "Только для генерального админа.", show_alert=True)
                            return
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_admins_menu(call.message.chat.id)
                    case "subbots":
                        if not self._is_general_admin(call.from_user.id):
                            await self.main_bot.answer_callback_query(call.id, "Только для генерального админа.", show_alert=True)
                            return
                        await self._safe_answer_callback(self.main_bot, call.id)
                        answered_early = True
                        await self._show_subbots_menu(call.message.chat.id, page=page)
                if not answered_early:
                    await self._safe_answer_callback(self.main_bot, call.id)
                return

            if data.startswith("confessions:"):
                parts = data.split(":")
                action = parts[1]
                await self._safe_answer_callback(self.main_bot, call.id)
                if action == "connect_publisher":
                    if not self._is_general_admin(call.from_user.id):
                        await self.main_bot.send_message(call.message.chat.id, "Только для генерального администратора.")
                        return
                    self._set_user_state(call.message.chat.id, "await_confession_publisher_token")
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        "Отправьте токен отдельного бота, который будет хранить и копировать пасты признавашек.",
                    )
                    return
                if action == "connect_storage":
                    if not self._is_general_admin(call.from_user.id):
                        await self.main_bot.send_message(call.message.chat.id, "Только для генерального администратора.")
                        return
                    publisher = await self.editorial_actions.get_active_confession_publisher()
                    if publisher is None:
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            "Сначала нажмите «Подключить саббота» и отправьте токен отдельного бота.",
                        )
                        return
                    publisher = await self.editorial_actions.refresh_confession_bind_code(publisher.id)
                    await self._start_confession_publisher_runtime(publisher)
                    bot_label = f"@{publisher.bot_username}" if publisher.bot_username else str(publisher.bot_user_id)
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        (
                            "Подключение чата для паст.\n\n"
                            "1. Создайте закрытую группу или откройте уже существующую.\n"
                            f"2. Добавьте {bot_label} в администраторы группы.\n"
                            "3. Отправьте в этой группе команду:\n"
                            f"/connect_confessions {publisher.bind_code}\n\n"
                            "Код действует 30 минут. Чат, в котором будет отправлена команда, станет хранилищем паст признавашек."
                        ),
                    )
                    return
                if action == "pastes":
                    await self._show_first_confession_paste(call.message.chat.id)
                    return
                if action == "add_channel":
                    if not self._is_general_admin(call.from_user.id):
                        await self.main_bot.send_message(call.message.chat.id, "Только для генерального администратора.")
                        return
                    self._set_user_state(call.message.chat.id, "await_confession_add_channel")
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        (
                            "Добавьте саббота признавашек в администраторы паблика с правом публикации, "
                            "затем отправьте @username паблика или его numeric id."
                        ),
                    )
                    return
                if action == "channels":
                    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                    await self._show_confession_channels_menu(call.message.chat.id, page=page)
                    return
                await self._show_confessions_menu(call.message.chat.id)
                return

            if data.startswith("confession_paste:"):
                _prefix, action, value = data.split(":")
                paste_id = int(value)
                await self._safe_answer_callback(self.main_bot, call.id)
                if action == "delete":
                    paste = await self.editorial_actions.get_paste(paste_id)
                    if paste is None or paste.content_family != ContentFamily.CONFESSION.value:
                        await self.main_bot.send_message(call.message.chat.id, "Паста признавашек не найдена.")
                        return
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        (
                            f"Удалить пасту признавашек #{paste.id}: {paste.title}?\n\n"
                            "Она исчезнет из базы и больше не будет публиковаться. "
                            "Исходное сообщение в чате паст останется."
                        ),
                        reply_markup=build_confession_paste_delete_confirm(paste.id),
                    )
                    return
                if action == "delete_cancel":
                    await self._show_first_confession_paste(
                        call.message.chat.id,
                        current_id=paste_id,
                        step=0,
                    )
                    return
                if action == "delete_confirm":
                    pastes_before = await self.editorial_actions.list_confession_pastes(limit=None)
                    deleted_index = next(
                        (index for index, paste in enumerate(pastes_before) if paste.id == paste_id),
                        0,
                    )
                    try:
                        deleted_id, deleted_title = await self.editorial_actions.delete_confession_paste(paste_id)
                    except ValueError as exc:
                        await self.main_bot.send_message(call.message.chat.id, str(exc))
                        return
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        f"Паста признавашек #{deleted_id} удалена: {deleted_title}",
                    )
                    remaining = await self.editorial_actions.list_confession_pastes(limit=None)
                    if remaining:
                        await self._show_confession_paste_at_index(
                            call.message.chat.id,
                            remaining,
                            min(deleted_index, len(remaining) - 1),
                        )
                    else:
                        await self._show_first_confession_paste(call.message.chat.id)
                    return
                await self._show_first_confession_paste(
                    call.message.chat.id,
                    current_id=paste_id,
                    step=-1 if action == "prev" else 1,
                )
                return

            if data.startswith("confession_channel:"):
                _prefix, action, value = data.split(":")
                channel_id = int(value)
                await self._safe_answer_callback(self.main_bot, call.id)
                if action == "view":
                    await self._show_confession_channel_slots(
                        call.message.chat.id,
                        channel_id,
                        user_id=call.from_user.id,
                    )
                    return
                if action == "connect_suggestion":
                    if not self._is_general_admin(call.from_user.id):
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            "Только для генерального администратора.",
                        )
                        return
                    channel = await self.editorial_actions.get_channel(channel_id)
                    if channel is None or channel.content_family != ContentFamily.CONFESSION.value:
                        await self.main_bot.send_message(call.message.chat.id, "Паблик признавашек не найден.")
                        return
                    binding = await self.legacy_reader.get_bot_binding(channel.tg_channel_id)
                    if binding is not None:
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            f"Предложка уже подключена через @{binding.bot_username.lstrip('@')}.",
                        )
                        return
                    self._set_user_state(
                        call.message.chat.id,
                        "await_confession_suggestion_token",
                        channel_id=channel_id,
                    )
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        (
                            "1. Создайте отдельного бота-предложку в BotFather.\n"
                            "2. Добавьте его администратором этого паблика.\n"
                            "3. Отправьте сюда token бота.\n\n"
                            "После подключения добавьте того же бота администратором в нужный чат модерации — "
                            "туда будут пересылаться сообщения пользователей."
                        ),
                    )
                    return
                if action == "profile_sync":
                    result = await self.editorial_actions.sync_channel_profiles(channel_id=channel_id)
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        (
                            "Профиль обновлён по общей системе. "
                            f"Обновлено подписчиков: {result.subscriber_counts_updated}; "
                            f"изменено профилей: {result.profiles_changed}; ошибок: {result.failed}."
                        ),
                    )
                    await self._show_confession_channel_slots(
                        call.message.chat.id,
                        channel_id,
                        user_id=call.from_user.id,
                    )
                    return
                if action == "add_slot":
                    self._set_user_state(
                        call.message.chat.id,
                        "await_confession_add_slot",
                        channel_id=channel_id,
                    )
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        (
                            "Отправьте время для всех дней или '<дни> <времена...>'.\n"
                            "Примеры:\n12:00 15:00\nall 10:00 15:00\n0,2,4 18:00 21:00\n"
                            "Где 0 = понедельник, 6 = воскресенье."
                        ),
                    )
                    return
                if action == "delete_slots":
                    self._set_user_state(
                        call.message.chat.id,
                        "await_confession_delete_slots",
                        channel_id=channel_id,
                    )
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        "Отправьте удаляемые времена в том же формате. Например: all 10:00 15:00",
                    )
                    return
                if action == "add_ad_blackout":
                    self._set_user_state(
                        call.message.chat.id,
                        "await_confession_add_ad_blackout",
                        channel_id=channel_id,
                    )
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        "Отправьте рекламное окно в формате:\n"
                        "21 15:00 18:00\n\n"
                        "Это значит: 21 числа с 15:00 до 18:00 слоты паблика будут заблокированы. "
                        "Уже занятые сообщения дождутся окончания окна.",
                    )
                    return
                if action == "delete_ad_blackout":
                    self._set_user_state(
                        call.message.chat.id,
                        "await_confession_delete_ad_blackout",
                        channel_id=channel_id,
                    )
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        "Отправьте рекламное окно, которое нужно удалить, в формате:\n"
                        "21 15:00 18:00\n\n"
                        "Ввод должен совпадать с ранее добавленным окном.",
                    )
                    return
                return

            if data.startswith("profiles:"):
                parts = data.split(":")
                action = parts[1]
                profile_slug = parts[2] if len(parts) > 2 else None
                await self._safe_answer_callback(self.main_bot, call.id)
                if action == "add":
                    await self._send_profile_create_prompt(call.message.chat.id)
                    return
                if action == "view" and profile_slug:
                    await self._show_profile_card(call.message.chat.id, profile_slug)
                    return
                if action == "settings" and profile_slug:
                    await self._send_profile_settings_prompt(call.message.chat.id, profile_slug)
                    return
                if action == "assign" and profile_slug:
                    await self._send_profile_assign_channels_prompt(call.message.chat.id, profile_slug)
                    return
                await self._show_profiles_menu(call.message.chat.id)
                return

            if data.startswith("submission:") and not data.startswith("submission:view:"):
                _prefix, action, value = data.split(":")
                submission_id = int(value)
                reviewer_id = call.from_user.id
                await self._safe_answer_callback(self.main_bot, call.id)
                match action:
                    case "approve":
                        item = await self.editorial_actions.approve_submission(submission_id, reviewer_id)
                        await self.editorial_actions.sync_panel_submission_approved(submission_id)
                        await self.main_bot.send_message(call.message.chat.id, f"Сообщение {submission_id} одобрено. Content item #{item.id}.")
                        await self._show_first_pending_submission(call.message.chat.id, current_id=submission_id, user_id=call.from_user.id)
                    case "publish":
                        try:
                            log_item = await self.editorial_actions.publish_submission_now(submission_id, reviewer_id)
                        except ValueError as exc:
                            await self.main_bot.send_message(call.message.chat.id, str(exc))
                            return
                        await self.editorial_actions.sync_panel_submission_approved(submission_id)
                        await self.main_bot.send_message(call.message.chat.id, f"Сообщение {submission_id} отправлено в publish pipeline. Log #{log_item.id}.")
                        await self._show_first_pending_submission(call.message.chat.id, current_id=submission_id, user_id=call.from_user.id)
                    case "paste":
                        paste = await self.editorial_actions.paste_submission(submission_id, reviewer_id)
                        await self.main_bot.send_message(call.message.chat.id, f"Создана паста #{paste.id}: {paste.title}")
                        await self._show_first_pending_submission(call.message.chat.id, current_id=submission_id, user_id=call.from_user.id)
                    case "hold":
                        await self.editorial_actions.hold_submission(submission_id, reviewer_id)
                        await self.main_bot.send_message(call.message.chat.id, f"Сообщение {submission_id} отправлено в hold.")
                        await self._show_first_pending_submission(call.message.chat.id, current_id=submission_id, user_id=call.from_user.id)
                    case "reject":
                        await self.editorial_actions.reject_submission(submission_id, reviewer_id)
                        await self.editorial_actions.sync_panel_submission_rejected(submission_id)
                        await self.main_bot.send_message(call.message.chat.id, f"Сообщение {submission_id} отклонено.")
                        await self._show_first_pending_submission(call.message.chat.id, current_id=submission_id, user_id=call.from_user.id)
                    case "ban":
                        result = await self.editorial_actions.ban_submission_author(submission_id, reviewer_id)
                        await self.editorial_actions.sync_panel_submission_banned(submission_id)
                        author_label = f"@{result.username}" if result.username else str(result.user_id)
                        if result.already_banned:
                            await self.main_bot.send_message(
                                call.message.chat.id,
                                f"РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ {author_label} СѓР¶Рµ Р±С‹Р» РІ Р±Р°РЅРµ. РЎРѕРѕР±С‰РµРЅРёРµ {submission_id} РѕС‚РєР»РѕРЅРµРЅРѕ.",
                            )
                        else:
                            await self.main_bot.send_message(
                                call.message.chat.id,
                                f"РџРѕР»СЊР·РѕРІР°С‚РµР»СЊ {author_label} Р·Р°Р±Р°РЅРµРЅ. РЎРѕРѕР±С‰РµРЅРёРµ {submission_id} РѕС‚РєР»РѕРЅРµРЅРѕ.",
                            )
                        await self._show_first_pending_submission(call.message.chat.id, current_id=submission_id, user_id=call.from_user.id)
                    case "toggle_anon":
                        submission = await self.editorial_actions.get_submission(submission_id)
                        if submission is None:
                            await self.main_bot.send_message(call.message.chat.id, f"Сообщение {submission_id} не найдено.")
                        else:
                            updated = await self.editorial_actions.set_submission_anonymous(
                                submission_id=submission_id,
                                is_anonymous=not submission.is_anonymous,
                            )
                            mode_text = "анон" if updated.is_anonymous else f"не анон ({self._submission_author_tag(updated)})"
                            await self.main_bot.send_message(
                                call.message.chat.id,
                                f"Для сообщения {submission_id} установлен режим: {mode_text}.",
                            )
                            await self.main_bot.send_message(
                                call.message.chat.id,
                                await self._format_submission(updated),
                                reply_markup=build_submission_actions(
                                    updated.id,
                                    has_next=False,
                                    is_anonymous=updated.is_anonymous,
                                    allow_moderation=self._submission_moderation_allowed(str(updated.status)),
                                ),
                            )
                    case "advertise":
                        try:
                            await self.editorial_actions.advertise_submission(submission_id, reviewer_id)
                            await self.editorial_actions.sync_panel_submission_advertising(submission_id)
                            await self.main_bot.send_message(
                                call.message.chat.id,
                                f"Пользователю по сообщению {submission_id} отправлена инструкция по рекламе.",
                            )
                            await self._show_first_pending_submission(call.message.chat.id, current_id=submission_id, user_id=call.from_user.id)
                        except Exception as ex:
                            await self.main_bot.send_message(
                                call.message.chat.id,
                                f"Не удалось отправить рекламный ответ: {ex}",
                            )
                    case "reply":
                        self._set_user_state(call.message.chat.id, "await_reply_submission", submission_id=submission_id)
                        await self.main_bot.send_message(
                            call.message.chat.id,
                            "Отправьте одним сообщением текст, который нужно переслать автору предложки.",
                        )
                    case "next":
                        await self._show_first_pending_submission(call.message.chat.id, current_id=submission_id, user_id=call.from_user.id)
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("submission_all:next:"):
                submission_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                await self._show_submission_history(call.message.chat.id, current_id=submission_id)
                return

            if data.startswith("submission_all:delete:"):
                submission_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                deleted_count = await self.editorial_actions.delete_submission(submission_id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    f"Удалено сообщений: {deleted_count}. Submission #{submission_id} убран из базы.",
                )
                await self._show_submission_history(call.message.chat.id, current_id=submission_id)
                return

            if data.startswith("submission:view:"):
                submission_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                await self._send_submission_card(
                    call.message.chat.id,
                    submission_id,
                    history_mode=True,
                    has_next=False,
                )
                return

            if data.startswith("content:"):
                _prefix, action, value = data.split(":")
                content_item_id = int(value)
                reviewer_id = call.from_user.id
                await self._safe_answer_callback(self.main_bot, call.id)
                match action:
                    case "approve":
                        await self.editorial_actions.approve_content_item(content_item_id, reviewer_id)
                        await self.main_bot.send_message(call.message.chat.id, f"Контент {content_item_id} одобрен.")
                        await self._show_first_pending_content(call.message.chat.id, current_id=content_item_id)
                    case "publish":
                        try:
                            log_item = await self.editorial_actions.publish_content_item_now(content_item_id, reviewer_id)
                        except ValueError as exc:
                            await self.main_bot.send_message(call.message.chat.id, str(exc))
                            return
                        await self.main_bot.send_message(call.message.chat.id, f"Контент {content_item_id} поставлен в публикацию. Log #{log_item.id}.")
                        await self._show_first_pending_content(call.message.chat.id, current_id=content_item_id)
                    case "edit":
                        item = await self.editorial_actions.get_content_item(content_item_id)
                        if item is None:
                            await self.main_bot.send_message(
                                call.message.chat.id,
                                f"\u0427\u0435\u0440\u043d\u043e\u0432\u0438\u043a #{content_item_id} \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d.",
                            )
                        else:
                            self._set_user_state(
                                call.message.chat.id,
                                "await_edit_content_item",
                                content_item_id=content_item_id,
                            )
                            await self.main_bot.send_message(
                                call.message.chat.id,
                                (
                                    f"\u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u043d\u043e\u0432\u044b\u0439 \u0442\u0435\u043a\u0441\u0442 \u0434\u043b\u044f \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a\u0430 #{content_item_id}.\n"
                                    "\u041e\u043d \u043f\u0440\u043e\u0441\u0442\u043e \u0437\u0430\u043c\u0435\u043d\u0438\u0442 \u0442\u0435\u043a\u0443\u0449\u0438\u0439 \u0442\u0435\u043a\u0441\u0442, \u0442\u0438\u043f \u043a\u043e\u043d\u0442\u0435\u043d\u0442\u0430 \u043d\u0435 \u0438\u0437\u043c\u0435\u043d\u0438\u0442\u0441\u044f."
                                ),
                            )
                    case "hold":
                        await self.editorial_actions.hold_content_item(content_item_id, reviewer_id)
                        await self.main_bot.send_message(call.message.chat.id, f"Контент {content_item_id} отправлен в hold.")
                        await self._show_first_pending_content(call.message.chat.id, current_id=content_item_id)
                    case "reject":
                        await self.editorial_actions.reject_content_item(content_item_id, reviewer_id)
                        await self.main_bot.send_message(call.message.chat.id, f"Контент {content_item_id} отклонен.")
                        await self._show_first_pending_content(call.message.chat.id, current_id=content_item_id)
                    case "next":
                        await self._show_first_pending_content(call.message.chat.id, current_id=content_item_id)
                return

            if data == "tag:add":
                await self._safe_answer_callback(self.main_bot, call.id)
                self._set_user_state(call.message.chat.id, "await_add_tag")
                await self.main_bot.send_message(
                    call.message.chat.id,
                    "Отправьте slug тега или строку:\nslug :: Название\n\nНапример:\nsummer :: Лето",
                )
                return

            if data == "tag:global_rules":
                await self._safe_answer_callback(self.main_bot, call.id)
                await self._show_global_paste_tags(call.message.chat.id)
                return

            if data.startswith("tag:view:"):
                slug = data.split(":", maxsplit=2)[2]
                await self._safe_answer_callback(self.main_bot, call.id)
                await self._show_tag_card(call.message.chat.id, slug)
                return

            if data.startswith("tag:add_keywords:"):
                slug = data.split(":", maxsplit=2)[2]
                await self._safe_answer_callback(self.main_bot, call.id)
                self._set_user_state(call.message.chat.id, "await_add_tag_keywords", slug=slug)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    f"Отправьте ключевые слова для тега {slug} через запятую.\nНапример:\nлето, каникулы, июль",
                )
                return

            if data.startswith("tag:keywords:"):
                slug = data.split(":", maxsplit=2)[2]
                await self._safe_answer_callback(self.main_bot, call.id)
                await self._show_tag_keywords(call.message.chat.id, slug)
                return

            if data.startswith("tag:toggle:"):
                slug = data.split(":", maxsplit=2)[2]
                await self._safe_answer_callback(self.main_bot, call.id)
                tags = await self.editorial_actions.list_tags(include_inactive=True)
                tag = next((item for item in tags if item.slug == slug), None)
                if tag is None:
                    await self.main_bot.send_message(call.message.chat.id, f"Тег {slug} не найден.")
                    return
                updated = await self.editorial_actions.set_tag_active(slug=slug, is_active=not tag.is_active)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    f"Тег {updated.slug} {'включен' if updated.is_active else 'выключен'}.",
                )
                await self._show_tag_card(call.message.chat.id, slug)
                return

            if data.startswith("tag:remove_keyword:"):
                _prefix, _action, slug, keyword_id = data.split(":")
                await self._safe_answer_callback(self.main_bot, call.id)
                keyword = await self.editorial_actions.remove_tag_keyword(int(keyword_id))
                await self.main_bot.send_message(call.message.chat.id, f"Ключевое слово выключено: {keyword.keyword}")
                await self._show_tag_keywords(call.message.chat.id, slug)
                return

            if data.startswith("global_paste_tags:"):
                action = data.split(":", maxsplit=1)[1]
                await self._safe_answer_callback(self.main_bot, call.id)
                if action == "add_include":
                    operation = "add"
                    mode = ChannelPasteTagRuleMode.INCLUDE
                    prompt = (
                        "Отправьте slug тега, который нужно поставить как Include для всех каналов.\n"
                        "Можно указать срок действия:\n"
                        "summer до 2026-08-31\n"
                        "summer 31.08.2026 23:59"
                    )
                elif action == "add_exclude":
                    operation = "add"
                    mode = ChannelPasteTagRuleMode.EXCLUDE
                    prompt = (
                        "Отправьте slug тега, который нужно поставить как Exclude для всех каналов.\n"
                        "Можно указать срок действия:\n"
                        "tech до 2026-08-31"
                    )
                elif action == "remove_include":
                    operation = "remove"
                    mode = ChannelPasteTagRuleMode.INCLUDE
                    prompt = "Отправьте slug include-тега, который нужно убрать для всех каналов. Можно несколько через запятую."
                elif action == "remove_exclude":
                    operation = "remove"
                    mode = ChannelPasteTagRuleMode.EXCLUDE
                    prompt = "Отправьте slug exclude-тега, который нужно убрать для всех каналов. Можно несколько через запятую."
                else:
                    await self.main_bot.send_message(call.message.chat.id, "Неизвестное действие с массовыми правилами паст.")
                    return
                self._set_user_state(
                    call.message.chat.id,
                    "await_global_paste_tag_rule",
                    operation=operation,
                    mode=mode.value,
                )
                await self.main_bot.send_message(call.message.chat.id, prompt)
                return

            if data == "my_channels:all":
                await self._safe_answer_callback(self.main_bot, call.id)
                channels = await self.editorial_actions.list_user_moderation_feed_channels(call.from_user.id)
                channel_ids = [int(channel.id) for channel in channels]
                if not channel_ids:
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        "\u0423 \u0432\u0430\u0441 \u043f\u043e\u043a\u0430 \u043d\u0435 \u0432\u044b\u0431\u0440\u0430\u043d\u044b \u043a\u0430\u043d\u0430\u043b\u044b \u0434\u043b\u044f \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u044f \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439.",
                    )
                    return
                self._set_user_state(
                    call.message.chat.id,
                    "await_manual_channel_message",
                    channel_ids=channel_ids,
                )
                await self.main_bot.send_message(
                    call.message.chat.id,
                    (
                        "\u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u043e\u0434\u043d\u0438\u043c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435\u043c \u0442\u0435\u043a\u0441\u0442, "
                        f"\u043a\u043e\u0442\u043e\u0440\u044b\u0439 \u043d\u0443\u0436\u043d\u043e \u043e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u0442\u044c \u0432\u043e \u0432\u0441\u0435 \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0435 \u043a\u0430\u043d\u0430\u043b\u044b ({len(channel_ids)})."
                    ),
                )
                return

            if data.startswith("my_channels:channel:"):
                channel_id = int(data.split(":")[-1])
                selected_channel_ids = set(await self.editorial_actions.list_user_moderation_feed_channel_ids(call.from_user.id))
                if channel_id not in selected_channel_ids:
                    await self.main_bot.answer_callback_query(
                        call.id,
                        "\u042d\u0442\u043e\u0442 \u043a\u0430\u043d\u0430\u043b \u043d\u0435 \u0432\u043a\u043b\u044e\u0447\u0451\u043d \u0432 \u0432\u0430\u0448\u0438 '\u043f\u043e\u0441\u0442\u0443\u043f\u0438\u0432\u0448\u0438\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f'.",
                        show_alert=True,
                    )
                    return
                await self._safe_answer_callback(self.main_bot, call.id)
                label = await self._get_channel_label(channel_id)
                self._set_user_state(
                    call.message.chat.id,
                    "await_manual_channel_message",
                    channel_ids=[channel_id],
                )
                await self.main_bot.send_message(
                    call.message.chat.id,
                    (
                        f"\u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0442\u0435\u043a\u0441\u0442, "
                        f"\u043a\u043e\u0442\u043e\u0440\u044b\u0439 \u043d\u0443\u0436\u043d\u043e \u043e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u0442\u044c \u0432 {label}."
                    ),
                )
                return

            if data == "channel:jump":
                await self._safe_answer_callback(self.main_bot, call.id)
                self._set_user_state(call.message.chat.id, "await_channel_jump")
                await self.main_bot.send_message(
                    call.message.chat.id,
                    "Отправьте номер канала или часть тега/названия.",
                )
                return

            if data.startswith("channel:view:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                await self._show_channel_menu(call.message.chat.id, channel_id, user_id=call.from_user.id)
                return

            if data.startswith("channel:notify_toggle:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                enabled = await self.editorial_actions.toggle_channel_notifications(channel_id, call.from_user.id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    "Уведомления включены." if enabled else "Уведомления выключены.",
                )
                await self._show_channel_menu(call.message.chat.id, channel_id, user_id=call.from_user.id)
                return

            if data.startswith("channel:feed_toggle:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                enabled = await self.editorial_actions.toggle_channel_moderation_feed(channel_id, call.from_user.id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    "Получение сообщений из канала включено." if enabled else "Получение сообщений из канала выключено.",
                )
                await self._show_channel_menu(call.message.chat.id, channel_id, user_id=call.from_user.id)
                return

            if data.startswith("channel:ad_blackout:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                self._set_user_state(call.message.chat.id, "await_add_ad_blackout", channel_id=channel_id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    "Отправьте рекламное окно в формате:\n"
                    "21 15:00 18:00\n\n"
                    "Это значит: 21 числа с 15:00 до 18:00 канал будет защищён от публикаций. "
                    "Если такая дата в текущем месяце уже прошла, будет выбран следующий месяц.",
                )
                return

            if data.startswith("channel:delete_ad_blackout:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                self._set_user_state(call.message.chat.id, "await_delete_ad_blackout", channel_id=channel_id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    "\u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0440\u0435\u043a\u043b\u0430\u043c\u043d\u043e\u0435 \u043e\u043a\u043d\u043e, \u043a\u043e\u0442\u043e\u0440\u043e\u0435 \u043d\u0443\u0436\u043d\u043e \u0443\u0434\u0430\u043b\u0438\u0442\u044c, \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0435:\n"
                    "21 15:00 18:00\n\n"
                    "\u0412\u0432\u043e\u0434 \u0434\u043e\u043b\u0436\u0435\u043d \u0441\u043e\u0432\u043f\u0430\u0434\u0430\u0442\u044c \u0441 \u0442\u0435\u043c \u043e\u043a\u043d\u043e\u043c, \u043a\u043e\u0442\u043e\u0440\u043e\u0435 \u0431\u044b\u043b\u043e \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u043e.",
                )
                return

            if data.startswith("channel:generate:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                label = await self._get_channel_label(channel_id)
                self._set_user_state(call.message.chat.id, "await_generate_posts", channel_id=channel_id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    (
                        f"\u0421\u043a\u043e\u043b\u044c\u043a\u043e \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a\u043e\u0432 \u0441\u0433\u0435\u043d\u0435\u0440\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0434\u043b\u044f {label}?\n\n"
                        "\u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0447\u0438\u0441\u043b\u043e \u043e\u0442 1 \u0434\u043e 10. \u041e\u0431\u044b\u0447\u043d\u043e \u0445\u0432\u0430\u0442\u0430\u0435\u0442 3."
                    ),
                )
                return

            if data.startswith("channel:slots:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                await self._show_channel_slots_menu(call.message.chat.id, channel_id)
                return

            if data.startswith("channel:params:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                self._set_user_state(call.message.chat.id, "await_update_channel_setting", channel_id=channel_id)
                await self._send_channel_settings_prompt(call.message.chat.id, channel_id)
                return

            if data.startswith("channel:paste_diagnostics:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                await self._show_channel_paste_diagnostics(call.message.chat.id, channel_id)
                return

            if data.startswith("channel:paste_tags:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                await self._show_channel_paste_tags(call.message.chat.id, channel_id)
                return

            if data.startswith("channel:seed:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                created_count = await self.editorial_actions.seed_default_slots(channel_id)
                label = await self._get_channel_label(channel_id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    f"Для {label} создано стандартных слотов: {created_count}.",
                )
                await self._show_channel_slots_menu(call.message.chat.id, channel_id)
                return

            if data.startswith("channel:add_slot:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                self._set_user_state(call.message.chat.id, "await_add_slot", channel_id=channel_id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    "Отправьте одно или несколько времен через пробел.\n"
                    "Можно указать только время для всех дней или '<дни> <времена...>'.\n"
                    "Примеры:\n12:00 15:00 16:00\nall 10:00 15:00\n0 09:30 18:00\n0,2,4 18:00 21:00\n"
                    "Где 0 = понедельник, 6 = воскресенье.",
                )
                return

            if data.startswith("channel:delete_slots:"):
                channel_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                self._set_user_state(call.message.chat.id, "await_delete_slots", channel_id=channel_id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    "Отправьте времена слотов, которые нужно удалить.\n"
                    "Можно указать только время для всех дней или '<дни> <времена...>'.\n"
                    "Примеры:\n12:00 15:00 16:00\nall 10:00 15:00\n0 09:30 18:00\n0,2,4 18:00 21:00\n"
                    "Где 0 = понедельник, 6 = воскресенье.",
                )
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("channel_paste_tags:"):
                _prefix, action, value = data.split(":")
                channel_id = int(value)
                await self._safe_answer_callback(self.main_bot, call.id)
                if action == "add_include":
                    operation = "add"
                    mode = ChannelPasteTagRuleMode.INCLUDE
                    prompt = "Отправьте slug обязательного тега для паст. Можно несколько через запятую."
                elif action == "add_exclude":
                    operation = "add"
                    mode = ChannelPasteTagRuleMode.EXCLUDE
                    prompt = "Отправьте slug запрещённого тега для паст. Можно несколько через запятую."
                elif action == "remove_include":
                    operation = "remove"
                    mode = ChannelPasteTagRuleMode.INCLUDE
                    prompt = "Отправьте slug обязательного тега, который нужно убрать."
                elif action == "remove_exclude":
                    operation = "remove"
                    mode = ChannelPasteTagRuleMode.EXCLUDE
                    prompt = "Отправьте slug запрещённого тега, который нужно убрать."
                else:
                    await self.main_bot.send_message(call.message.chat.id, "Неизвестное действие с тегами паст канала.")
                    return
                self._set_user_state(
                    call.message.chat.id,
                    "await_channel_paste_tag_rule",
                    channel_id=channel_id,
                    operation=operation,
                    mode=mode.value,
                )
                await self.main_bot.send_message(call.message.chat.id, prompt)
                return

            if data == "paste:add":
                await self._safe_answer_callback(self.main_bot, call.id)
                self._set_user_state(call.message.chat.id, "await_add_paste")
                await self.main_bot.send_message(
                    call.message.chat.id,
                    "Отправьте текст пасты одним сообщением.\n"
                    "Если хотите задать заголовок вручную, используйте формат:\n"
                    "Заголовок :: Текст пасты",
                )
                return

            if data.startswith("paste:tags:"):
                paste_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                await self._show_paste_tags(call.message.chat.id, paste_id)
                return

            if data == "paste:jump":
                await self._safe_answer_callback(self.main_bot, call.id)
                self._set_user_state(call.message.chat.id, "await_paste_jump")
                await self.main_bot.send_message(call.message.chat.id, "Отправьте номер пасты.")
                return

            if data.startswith("paste_tags:"):
                _prefix, action, value = data.split(":")
                paste_id = int(value)
                await self._safe_answer_callback(self.main_bot, call.id)
                if action == "refresh":
                    summary = await self.editorial_actions.refresh_paste_auto_tags(paste_id)
                    await self.main_bot.send_message(
                        call.message.chat.id,
                        f"Авто-теги пересчитаны. Итог: {', '.join(summary.all_tags) or 'нет'}.",
                    )
                    await self._show_paste_tags(call.message.chat.id, paste_id)
                    return
                state_action = {
                    "add": "await_add_paste_manual_tag",
                    "remove": "await_remove_paste_manual_tag",
                    "primary": "await_set_paste_primary_tag",
                }.get(action)
                if state_action is None:
                    await self.main_bot.send_message(call.message.chat.id, "Неизвестное действие с тегами пасты.")
                    return
                self._set_user_state(call.message.chat.id, state_action, paste_id=paste_id)
                prompts = {
                    "add": "Отправьте slug тега, который нужно вручную добавить пасте.",
                    "remove": "Отправьте slug ручного тега, который нужно убрать с пасты.",
                    "primary": "Отправьте slug тега, который нужно сделать основным.",
                }
                await self.main_bot.send_message(call.message.chat.id, prompts[action])
                return

            if data.startswith("paste:delete:"):
                paste_id = int(data.split(":")[-1])
                await self._safe_answer_callback(self.main_bot, call.id)
                deleted_paste_id, deleted_paste_title = await self.editorial_actions.delete_paste(paste_id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    f"Паста #{deleted_paste_id} удалена: {deleted_paste_title}",
                )
                await self._show_first_paste(call.message.chat.id, current_id=paste_id)
                return

            if data.startswith("paste:delete:legacy_disabled:"):
                paste_id = int(data.split(":")[-1])
                deleted_paste_id, deleted_paste_title = await self.editorial_actions.delete_paste(paste_id)
                await self.main_bot.send_message(call.message.chat.id, f"Паста #{paste.id} переведена в archived.")
                await self._show_first_paste(call.message.chat.id, current_id=paste_id)
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("paste:next:"):
                paste_id = int(data.split(":")[-1])
                await self._show_first_paste(call.message.chat.id, current_id=paste_id)
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("paste:prev:"):
                paste_id = int(data.split(":")[-1])
                await self._show_first_paste(call.message.chat.id, current_id=paste_id, step=-1)
                await self.main_bot.answer_callback_query(call.id)
                return

            if data == "admin:add":
                if not self._is_general_admin(call.message.chat.id):
                    await self.main_bot.answer_callback_query(call.id, "Только для генерального админа.", show_alert=True)
                    return
                self._set_user_state(call.message.chat.id, "await_add_moderator")
                await self.main_bot.send_message(call.message.chat.id, "Отправьте numeric Telegram user id нового модератора.")
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("admin:remove:"):
                if not self._is_general_admin(call.message.chat.id):
                    await self.main_bot.answer_callback_query(call.id, "Только для генерального админа.", show_alert=True)
                    return
                user_id = int(data.split(":")[-1])
                await self.bot_admins_database.delete_admin(user_id)
                settings.moderators.discard(user_id)
                self.chats.discard(user_id)
                await self.main_bot.send_message(call.message.chat.id, f"Модератор {user_id} удален.")
                await self.main_bot.answer_callback_query(call.id)
                return

            if data == "subbot:add":
                if not self._is_general_admin(call.message.chat.id):
                    await self.main_bot.answer_callback_query(call.id, "Только для генерального админа.", show_alert=True)
                    return
                self._set_user_state(call.message.chat.id, "await_add_subbot")
                await self.main_bot.send_message(call.message.chat.id, "Отправьте строку: <api_token> @channel_username")
                await self.main_bot.answer_callback_query(call.id)
                return

            if data == "subbot:remove_cancel":
                if not self._is_general_admin(call.message.chat.id):
                    await self.main_bot.answer_callback_query(call.id, "\u0422\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u0433\u0435\u043d\u0435\u0440\u0430\u043b\u044c\u043d\u043e\u0433\u043e \u0430\u0434\u043c\u0438\u043d\u0430.", show_alert=True)
                    return
                await self.main_bot.send_message(call.message.chat.id, "\u0423\u0434\u0430\u043b\u0435\u043d\u0438\u0435 \u0441\u0430\u0431\u0431\u043e\u0442\u0430 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u043e.")
                await self._show_subbots_menu(call.message.chat.id)
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("subbot:remove_confirm:"):
                if not self._is_general_admin(call.message.chat.id):
                    await self.main_bot.answer_callback_query(call.id, "\u0422\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u0433\u0435\u043d\u0435\u0440\u0430\u043b\u044c\u043d\u043e\u0433\u043e \u0430\u0434\u043c\u0438\u043d\u0430.", show_alert=True)
                    return
                _, _, username_bot, channel_id = data.split(":")
                try:
                    result_text = await self._remove_subbot_from_values(username_bot, int(channel_id))
                except Exception as ex:
                    logger.exception("Failed to confirm subbot removal")
                    await self.main_bot.send_message(call.message.chat.id, f"Не удалось удалить саббота: {ex}")
                    await self.main_bot.answer_callback_query(call.id, "Ошибка удаления", show_alert=True)
                    return
                await self.main_bot.send_message(call.message.chat.id, result_text)
                await self._show_subbots_menu(call.message.chat.id)
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("subbot:remove:"):
                if not self._is_general_admin(call.message.chat.id):
                    await self.main_bot.answer_callback_query(call.id, "\u0422\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u0433\u0435\u043d\u0435\u0440\u0430\u043b\u044c\u043d\u043e\u0433\u043e \u0430\u0434\u043c\u0438\u043d\u0430.", show_alert=True)
                    return
                _, _, username_bot, channel_id = data.split(":")
                await self.main_bot.send_message(
                    call.message.chat.id,
                    f"\u0412\u044b \u0442\u043e\u0447\u043d\u043e \u0445\u043e\u0442\u0438\u0442\u0435 \u0443\u0434\u0430\u043b\u0438\u0442\u044c \u0441\u0430\u0431\u0431\u043e\u0442\u0430 @{username_bot}?",
                    reply_markup=build_subbot_remove_confirm(username_bot, int(channel_id)),
                )
                await self.main_bot.answer_callback_query(call.id)
                return

            if data == "subbot:remove_cancel":
                if not self._is_general_admin(call.message.chat.id):
                    await self.main_bot.answer_callback_query(call.id, "РўРѕР»СЊРєРѕ РґР»СЏ РіРµРЅРµСЂР°Р»СЊРЅРѕРіРѕ Р°РґРјРёРЅР°.", show_alert=True)
                    return
                await self.main_bot.send_message(call.message.chat.id, "РЈРґР°Р»РµРЅРёРµ СЃР°Р±Р±РѕС‚Р° РѕС‚РјРµРЅРµРЅРѕ.")
                await self._show_subbots_menu(call.message.chat.id)
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("subbot:remove_confirm:"):
                if not self._is_general_admin(call.message.chat.id):
                    await self.main_bot.answer_callback_query(call.id, "РўРѕР»СЊРєРѕ РґР»СЏ РіРµРЅРµСЂР°Р»СЊРЅРѕРіРѕ Р°РґРјРёРЅР°.", show_alert=True)
                    return
                _, _, username_bot, channel_id = data.split(":")
                result_text = await self._remove_subbot_from_values(username_bot, int(channel_id))
                await self.main_bot.send_message(call.message.chat.id, result_text)
                await self._show_subbots_menu(call.message.chat.id)
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("subbot:remove:"):
                if not self._is_general_admin(call.message.chat.id):
                    await self.main_bot.answer_callback_query(call.id, "Только для генерального админа.", show_alert=True)
                    return
                _, _, username_bot, channel_id = data.split(":")
                result_text = await self._remove_subbot_from_values(username_bot, int(channel_id))
                await self.main_bot.send_message(call.message.chat.id, result_text)
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("channel_history:start:"):
                channel_id = int(data.split(":")[-1])
                self._set_user_state(
                    call.message.chat.id,
                    "await_import_channel_history",
                    channel_id=channel_id,
                    history_saved=0,
                    history_matches=0,
                    history_duplicates=0,
                )
                label = await self._get_channel_label(channel_id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    (
                        f"Импорт истории для {label} запущен.\n\n"
                        "Теперь пересылайте сюда сообщения из этого канала. "
                        "Когда закончите, нажмите 'Завершить импорт'."
                    ),
                    reply_markup=build_channel_history_import_progress_actions(channel_id),
                )
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("channel_history:finish:"):
                channel_id = int(data.split(":")[-1])
                state = self.user_states.get(call.message.chat.id, {})
                saved = int(state.get("history_saved", 0))
                matches = int(state.get("history_matches", 0))
                duplicates = int(state.get("history_duplicates", 0))
                if state.get("action") == "await_import_channel_history" and state.get("channel_id") == channel_id:
                    self._clear_user_state(call.message.chat.id)
                label = await self._get_channel_label(channel_id)
                await self.main_bot.send_message(
                    call.message.chat.id,
                    (
                        f"Импорт истории для {label} завершён.\n"
                        f"Сохранено сообщений: {saved}\n"
                        f"Найдено совпадений с пастами: {matches}\n"
                        f"Пропущено дублей: {duplicates}"
                    ),
                )
                await self.main_bot.answer_callback_query(call.id)
                return

            if data.startswith("channel_history:cancel:"):
                channel_id = int(data.split(":")[-1])
                state = self.user_states.get(call.message.chat.id, {})
                if state.get("action") == "await_import_channel_history" and state.get("channel_id") == channel_id:
                    self._clear_user_state(call.message.chat.id)
                await self.main_bot.send_message(call.message.chat.id, "Импорт истории отменён.")
                await self.main_bot.answer_callback_query(call.id)
                return

            match data.split(";")[0]:
                case "push":
                    await self.main_bot.answer_callback_query(call.id, "Ручная push-рассылка пока оставлена как заглушка.")
                case "reject_push":
                    await self.main_bot.delete_message(
                        chat_id=settings.general_admin,
                        message_id=int(data.split(";")[1]),
                    )
                    await self.main_bot.answer_callback_query(call.id)

    @logger.catch
    async def run_bot(self) -> None:
        logger.info("run_bot")
        await self.__load_dynamic_admins()
        bots_lst = self._filter_enabled_subbots(await self.bots_database.get_bots_info())
        confession_publisher = await self.editorial_actions.get_active_confession_publisher()
        request_limit = self._configure_request_limit(
            len(bots_lst) + (1 if confession_publisher is not None else 0)
        )
        await self._reset_telegram_session()
        self.bot_info = await self.main_bot.get_me()

        try:
            logger.info("main bot @{} working", self.bot_info.username)
            logger.info(
                "Configured Telegram request limit {} for {} subbots (+ main bot)",
                request_limit,
                len(bots_lst),
            )
            # Start the admin bot before the potentially long initialization of
            # hundreds of subbots. Panel callbacks remain available during a
            # collector restart instead of waiting for the whole network.
            self.main_polling_task = asyncio.create_task(
                self.main_bot.infinity_polling(
                    timeout=10,
                    request_timeout=settings.telegram_request_timeout_seconds,
                )
            )
            if confession_publisher is not None:
                try:
                    await self._start_confession_publisher_runtime(confession_publisher)
                except Exception:
                    logger.exception("Failed to start confession publisher bot. Continuing startup.")
            async with aiohttp.ClientSession() as session:
                for api_token, bot_username, channel_id, _id_row in bots_lst:
                    try:
                        channel_username = await self._fetch_channel_username(session, api_token, channel_id)
                        channel_ref = f"@{channel_username}" if channel_username else str(channel_id)
                        bot = await SubBot.create(
                            main_bot_username=self.bot_info.username,
                            api_token_bot=api_token,
                            channel_username=channel_ref,
                            hello_msg=settings.hello_msg,
                            ban_usr_msg=settings.ban_msg,
                            send_post_msg=settings.send_post_msg,
                            callback_adv_action=self.callback_adv_send_message_v2,
                            callback_new_submission=self.callback_new_submission_notification,
                        )
                        await bot.run_bot()
                        self.bots_work.append(bot)
                    except Exception:
                        logger.exception(
                            "Failed to start saved subbot @{} for channel {}. Continuing startup.",
                            bot_username,
                            channel_id,
                        )
            self.delayed_task = asyncio.create_task(self.__delayed_posts_checker())
            if self.subscriber_snapshot_task is None or self.subscriber_snapshot_task.done():
                self.subscriber_snapshot_task = asyncio.create_task(self.__subscriber_snapshot_scheduler())
            await self.main_polling_task
        except Exception as ex:
            logger.error("bot: @{}, mistake: {}", self.bot_info.username if self.bot_info else "unknown", ex)
