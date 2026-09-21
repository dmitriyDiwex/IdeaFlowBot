import asyncio
from datetime import datetime, timezone

from loguru import logger

from sqlalchemy.exc import IntegrityError
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException
from telebot.types import (Message, InlineKeyboardMarkup,
                           InlineKeyboardButton, CallbackQuery,
                           ChatMemberUpdated, BotCommand, BotCommandScopeAllGroupChats)

from src.core_database.database import (CrudChatAdmins, CrudBannedUser,
                                        CrudServiceMessage,
                                        CrudUserData, CrudDelayedPosts,
                                        CrudAnonymMessage, CrudAdvertising)
from src.editorial.db.session import session_factory
from src.editorial.models.enums import SubmissionStatus
from src.editorial.services.ad_link_exclusion_service import AdLinkExclusionService
from src.editorial.services.legacy_moderation_sync import LegacyModerationSyncService
from src.editorial.services.legacy_publication_guard import LegacyPublicationGuard
from src.editorial.services.publication_signature import (
    format_publication_html,
    publication_signature_html,
    should_add_publication_signature,
)
from src.editorial.services.telegram_resilience import is_transient_telegram_error
from src.legacy_delayed import delayed_publication_matches
from src.legacy_media_groups import (
    LegacyMediaGroupReference,
    build_media_group_reference,
    fetch_sender_media_group_rows,
)
from src.utils import Utils, filter_chats
from config import settings
from src.markups import MarkupButton


class SubBot:
    MEDIA_GROUP_SETTLE_SECONDS = 0.8

    def __init__(
            self,
            main_bot_username: str,
            api_token_bot: str,
            channel_username: str,
            hello_msg: str,
            ban_usr_msg: str,
            send_post_msg: str,
            callback_adv_action,
            callback_new_submission=None,
    ):
        self.polling_task = None
        self.bot_info = None

        self.hello_msg = hello_msg
        self.ban_usr_msg = ban_usr_msg
        self.send_post_msg = send_post_msg

        self.callback_adv_action = callback_adv_action
        self.callback_new_submission = callback_new_submission

        self.main_bot_username = main_bot_username

        self.commands = [
            BotCommand("ban_lst", "список забаненых пользователей"),
            BotCommand("unban", "разблокировка пользователя после команды нужно указать id"),
            BotCommand("update_hello", "изменение приветственного сообщения,"
                                       " после команды нужно текстом указать приветствие"),
            BotCommand("update_ban_user", "изменение сообщения при бане,"
                                          " после команды нужно текстом указать приветствие"),
            BotCommand("update_send_post", "изменение сообщения при отправке поста пользователем"),
            BotCommand("get_msg", "возвращает сервисные сообщения")
        ]

        self.admins_database = CrudChatAdmins()
        self.ban_database = CrudBannedUser()
        self.service_msg_database = CrudServiceMessage()
        self.user_database = CrudUserData()
        self.delayed_database = CrudDelayedPosts()
        self.anonym_message_database = CrudAnonymMessage()
        self.advertising_database = CrudAdvertising()
        self.legacy_moderation_sync = LegacyModerationSyncService()
        self.publication_guard = LegacyPublicationGuard()
        self.ad_link_exclusion_service = AdLinkExclusionService()

        self.token = api_token_bot
        self.channel_username = channel_username
        self.channel_signature_ref = channel_username
        self.sup_bot = AsyncTeleBot(self.token)
        self.users_data = set()

        self.advertising_data = set()
        self.delayed_message = {}
        self.anonym_send = set()

        self.chat_suggest = None
        self.chat_suggests = None
        self._media_group_messages: dict[tuple[int, str], list[Message]] = {}
        self._media_group_flush_tasks: dict[tuple[int, str], asyncio.Task] = {}
        self._media_group_lock = asyncio.Lock()

    async def _safe_answer_callback(
        self,
        call: CallbackQuery,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        try:
            await asyncio.wait_for(
                self.sup_bot.answer_callback_query(
                    callback_query_id=call.id,
                    text=text,
                    show_alert=show_alert,
                ),
                timeout=settings.telegram_request_timeout_seconds,
            )
        except Exception as ex:
            logger.debug("Failed to answer callback {}: {}", call.id, ex)

    @classmethod
    @logger.catch
    async def create(cls,
                     api_token_bot: str,
                     channel_username: str,
                     hello_msg: str,
                     ban_usr_msg: str,
                     send_post_msg: str,
                     main_bot_username: str,
                     callback_adv_action,
                     callback_new_submission=None,
                     ):
        self = cls(
            main_bot_username,
            api_token_bot,
            channel_username,
            hello_msg,
            ban_usr_msg,
            send_post_msg,
            callback_adv_action,
            callback_new_submission,
        )

        logger.info("init")
        await self.__setup_bot_info()
        logger.info("setup bot info")
        await self.__setup_service_msg()
        logger.info("setup service msg")
        await self.__setup_handlers()
        logger.info("setup handlers")

        logger.info("create")
        return self

    @logger.catch
    async def __setup_bot_info(self):
        await self.sup_bot.set_my_commands(
            commands=self.commands,
            scope=BotCommandScopeAllGroupChats(),
        )
        await self.sup_bot.set_my_commands(
            commands=self.commands,
            scope=BotCommandScopeAllGroupChats(),
        )
        self.bot_info = await self.sup_bot.get_me()
        channel_chat = await self.sup_bot.get_chat(self.channel_username)
        self.channel_id = channel_chat.id
        self.channel_title = getattr(channel_chat, "title", None)
        channel_username = getattr(channel_chat, "username", None)
        if channel_username:
            self.channel_username = f"@{channel_username}"
            self.channel_signature_ref = self.channel_username
        else:
            self.channel_signature_ref = getattr(channel_chat, "invite_link", None) or self.channel_username
        self.chat_suggest = await self._refresh_chat_suggest()
        logger.info(f"{self.chat_suggest}")
        users = await self.user_database.get_user_data(bot_username=self.bot_info.username)
        self.users_data = set(user_id for user_id, bot_username, id_el in users)
        delayed_message = await self.delayed_database.get_delayed_posts(bot_id=self.bot_info.id)
        self.delayed_message = {}
        for bot_id, time_seconds, message_id, sender_id, id_item in delayed_message:
            self.delayed_message[message_id] = [time_seconds, sender_id]

        if self.chat_suggest is not None:
            anonym_data = await self.anonym_message_database.get_posts(chat_id=self.chat_suggest)
            self.anonym_send = set(map(lambda el: el[0], anonym_data))
        else:
            self.anonym_send = set()

        advertising_data = await self.advertising_database.get_advertising(channel_id=self.channel_id)
        self.advertising_data = set(map(lambda el: (el[1], el[2]), advertising_data))

    @logger.catch
    async def __setup_service_msg(self) -> None:
        message_info = await self.service_msg_database.get_service_message(self.bot_info.id)
        if message_info:
            self.hello_msg = message_info[0][1]
            self.ban_usr_msg = message_info[0][2]
            self.send_post_msg = message_info[0][3]
        else:
            await self.service_msg_database.add_service_message({
                "bot_id": self.bot_info.id,
                "hello_message": self.hello_msg,
                "ban_user_message": self.ban_usr_msg,
                "send_post_message": self.send_post_msg,
            })

    @logger.catch
    async def __setup_handlers(self) -> None:
        @logger.catch
        async def __check_exist_user(message: Message) -> None:
            try:
                if message.chat.id not in self.users_data:
                    await self.user_database.insert_user(
                        {
                            "user_id": message.chat.id,
                            "bot_username": self.bot_info.username
                        }
                    )
                    self.users_data.add(message.chat.id)
            except IntegrityError:
                logger.error("IntegrityError")

        @logger.catch
        @self.sup_bot.message_handler(commands=['start'])
        async def start(message: Message, is_command=True) -> None:
            if message.chat.id < 0:
                return
            await __check_exist_user(message)
            message_text = self.hello_msg
            if not is_command:
                message_text = f"Чтобы продолжить, подпишитесь на канал: {self.channel_username}"

            markup = InlineKeyboardMarkup()
            btn_subscribe = InlineKeyboardButton(
                text=f"подпишитесь на канал -> {self.channel_username}",
                url=f"https://t.me/{self.channel_username[1:]}",
            )
            markup.add(btn_subscribe)
            info_subscribe = await self.sup_bot.get_chat_member(user_id=message.chat.id, chat_id=self.channel_id)
            if info_subscribe.status != "left":
                markup = None
                if not is_command:
                    message_text = self.send_post_msg

            await self.sup_bot.send_message(message.chat.id, message_text, reply_markup=markup)

        @logger.catch
        @self.sup_bot.message_handler(commands=["ban_lst"])
        @filter_chats
        async def ban_lst(message: Message) -> None:
            all_info = await self.ban_database.get_banned_users()
            if len(all_info) == 0:
                await self.sup_bot.send_message(message.chat.id, "нет забанeных пользователей")
                return
            answer = "Забаненные пользователи:\n"
            for id_user, id_channel, bot_id, id_db in all_info:
                if bot_id == self.bot_info.id:
                    user_info = await self.sup_bot.get_chat(id_user)
                    answer += f"`{user_info.id}` `{user_info.username}`\n"
            await self.sup_bot.send_message(chat_id=message.chat.id, text=answer, parse_mode='Markdown')

        @logger.catch
        @self.sup_bot.message_handler(commands=["unban"])
        @filter_chats
        async def unban_user(message: Message) -> None:
            user_id = message.text.split()[1]
            logger.info("unban user: %s", user_id)
            all_ban_user = await self.ban_database.get_banned_users(id_user=int(user_id), id_channel=self.channel_id)
            if all_ban_user:
                await self.ban_database.delete_banned_user({"id_user": user_id, "id_channel": self.channel_id})
                await self.sup_bot.send_message(message.chat.id, "пользователь разбанен")
                logger.info("unban success")
            else:
                logger.info("unban failed but user not banned")
                await self.sup_bot.send_message(message.chat.id, "пользователь не забанен")

        @logger.catch
        @self.sup_bot.message_handler(commands=["update_hello"])
        @filter_chats
        async def update_hello(message: Message) -> None:
            hello_msg = message.text[13:]
            self.hello_msg = hello_msg
            logger.info(f"update hello msg: {hello_msg}")
            await self.service_msg_database.update_service_message(**{
                "bot_id": self.bot_info.id,
                "hello_message": hello_msg.strip(),
                "ban_user_message": self.ban_usr_msg.strip(),
                "send_post_message": self.send_post_msg.strip(),
            })
            await self.sup_bot.send_message(message.chat.id, "👍")

        @logger.catch
        @self.sup_bot.message_handler(commands=["update_ban_user"])
        @filter_chats
        async def update_ban_user(message: Message) -> None:
            ban_usr_msg = message.text[16:]
            self.ban_usr_msg = ban_usr_msg
            logger.info(f"update ban msg: {ban_usr_msg}")
            await self.service_msg_database.update_service_message(**{
                "bot_id": self.bot_info.id,
                "hello_message": self.hello_msg.strip(),
                "ban_user_message": self.ban_usr_msg.strip(),
                "send_post_message": self.send_post_msg.strip(),
            })
            await self.sup_bot.send_message(message.chat.id, "👍")
            logger.info("set success")

        @logger.catch
        @self.sup_bot.message_handler(commands=["update_send_post"])
        async def update_send_post(message: Message) -> None:
            msg_send = message.text[17:]
            self.send_post_msg = msg_send
            logger.info(f"new send post msg: {msg_send}")
            await self.service_msg_database.update_service_message(**{
                "bot_id": self.bot_info.id,
                "hello_message": self.hello_msg.strip(),
                "ban_user_message": self.ban_usr_msg.strip(),
                "send_post_message": msg_send.strip(),
            })
            await self.sup_bot.send_message(message.chat.id, "👍")
            logger.info("set success")

        @logger.catch
        @self.sup_bot.message_handler(commands=["get_msg"])
        @filter_chats
        async def get_msg(message: Message) -> None:
            answer = (f"*Приветствие*:\n\n"
                      f"{self.hello_msg}\n\n"
                      f"*Сообщение при бане*\n\n"
                      f"{self.ban_usr_msg}\n\n"
                      f"*Сообщение при отправке сообщения*\n\n"
                      f"{self.send_post_msg}\n\n")
            await self.sup_bot.send_message(message.chat.id, answer, parse_mode="Markdown")

        @logger.catch
        @self.sup_bot.my_chat_member_handler()
        async def add_chat_member(chat_member_info: ChatMemberUpdated) -> None:
            if chat_member_info.chat.type == "channel":
                return

            info_chat = await self.sup_bot.get_chat_member(chat_member_info.chat.id, settings.general_admin)
            if info_chat.status not in ("administrator", "creator"):
                await self.sup_bot.leave_chat(chat_member_info.chat.id)
                return
            if chat_member_info.chat.id > 0 and chat_member_info.new_chat_member.status == "kicked":
                # noinspection PyBroadException
                try:
                    await self.user_database.delete_user_data(
                        user_id=chat_member_info.chat.id,
                        bot_username=self.bot_info.username
                    )
                except:
                    return
            if chat_member_info.new_chat_member.status == "administrator":
                try:
                    await self.admins_database.add_chat_admins({
                        "bot_id": self.bot_info.id,
                        "chat_id": chat_member_info.chat.id,
                    })
                except IntegrityError:
                    logger.info(
                        "Legacy moderation chat {} already linked for @{}",
                        chat_member_info.chat.id,
                        self.bot_info.username,
                    )
                self.chat_suggest = await self._refresh_chat_suggest()
            else:
                chats = await self.admins_database.get_chat_admins(bot=self.bot_info.id, chat=chat_member_info.chat.id)
                if chats:
                    await self.admins_database.delete_chat_admins({
                        "bot_id": self.bot_info.id,
                        "chat_id": chat_member_info.chat.id,
                    })
                self.chat_suggest = await self._refresh_chat_suggest()

        @logger.catch
        @self.sup_bot.message_handler(
            func=lambda message: message.reply_to_message is not None
        )
        async def reply_to_message(message: Message) -> None:
            info = message.json
            try:
                if not info["reply_to_message"]["from"]["is_bot"]:
                    return
                info_sender = message.json["reply_to_message"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                logger.info(f"sender post: {info_sender}")
                user_id = int(info_sender.split(";")[1])
                await self.sup_bot.send_message(
                    chat_id=user_id,
                    text=message.text
                )
                logger.info("answer success")
            except Exception as ex:
                logger.error(ex)

        @logger.catch
        @self.sup_bot.message_handler(content_types=["text", "photo", "video", "animation"])
        async def get_suggest(message: Message) -> None:
            logger.info(f"channel: {self.channel_username}, sender: {message.chat.id, message.chat.username}")
            info_subscribe = await self.sup_bot.get_chat_member(user_id=message.chat.id, chat_id=self.channel_id)
            await start(message, is_command=False)
            if info_subscribe.status == "left":
                logger.info("user not in channel")
                return

            if message.chat.id == self.chat_suggest or message.chat.id < 0:
                return

            if await Utils().check_banned_user(message.chat.id, self.channel_id):
                await self.sup_bot.send_message(chat_id=message.chat.id, text=self.ban_usr_msg)
                logger.info("user banned")
                return

            self.chat_suggest = await self._refresh_chat_suggest()
            if self.chat_suggest is None:
                await self.sup_bot.send_message(
                    chat_id=settings.general_admin,
                    text="Бот предлога не добавлен в чат"
                )
                logger.info("bot not chat")
                return

            if getattr(message, "media_group_id", None):
                await self._queue_media_group(message)
                return

            review_message = await self._send_review_message_to_legacy_chat(message)
            await self._save_incoming_message(message, review_message)
            await self._notify_new_submission(review_message)

        async def shift_timer():
            interval_lst = list(map(
                lambda x: (x[1], x[1] + settings.shift_time_seconds),
                self.advertising_data
            ))
            flag_interval = False
            for message_id in list(self.delayed_message.keys()):
                for interval_down, interval_up in interval_lst:
                    if interval_down <= self.delayed_message[message_id][0] <= interval_up:
                        flag_interval = True
                        break
                if flag_interval:
                    break
            if not flag_interval:
                return
            for message_id in self.delayed_message.keys():
                try:
                    old_time = await Utils.get_timestamp_to_time(self.delayed_message[message_id][0])
                    logger.debug(old_time)
                    self.delayed_message[message_id][0] += settings.shift_time_seconds
                    await self.delayed_database.upsert_delayed_post({
                        "bot_id": self.bot_info.id,
                        "time_seconds": int(self.delayed_message[message_id][0]),
                        "message_id": int(message_id),
                        "sender_id": int(self.delayed_message[message_id][1]),
                    })
                    await self._sync_editorial_submission_status(
                        review_message_id=message_id,
                        status=SubmissionStatus.CONTENT_CREATED,
                        moderator_note="Handled in legacy moderation: delayed shifted by ad",
                        legacy_scheduled_for=datetime.fromtimestamp(
                            float(self.delayed_message[message_id][0]),
                            tz=timezone.utc,
                        ),
                    )
                    new_time = await Utils.get_timestamp_to_time(self.delayed_message[message_id][0])
                    logger.debug(new_time)
                    logger.info(f"shift timer success")
                    markup = await MarkupButton(self.sup_bot).get_markup(*self.delayed_message[message_id])
                    await self.sup_bot.edit_message_reply_markup(
                        chat_id=self.chat_suggest,
                        message_id=message_id,
                        reply_markup=markup
                    )
                    logger.info(f"shift timer {message_id}, bot: {self.bot_info.username}")
                except Exception as ex:
                    logger.error(ex)

        async def save_advertising(message: Message) -> bool:
            time = await Utils.conversion_to_moscow_time(message.date)
            advertising_key = (message.message_id, time)
            is_new = advertising_key not in self.advertising_data
            self.advertising_data.add((message.message_id, time))
            existing = await self.advertising_database.get_advertising(
                channel_id=self.channel_id,
                post_id=message.message_id,
            )
            if not existing:
                await self.advertising_database.add_advertising(
                    channel_id=self.channel_id,
                    post_id=message.message_id,
                    time=time,
                )
            try:
                blackout = await self.publication_guard.ensure_automatic_ad_blackout_for_channel_post(
                    tg_channel_id=self.channel_id,
                    telegram_message_id=message.message_id,
                    published_at=datetime.fromtimestamp(message.date, tz=timezone.utc),
                )
                if blackout is None:
                    logger.warning(
                        "Could not create automatic ad blackout: editorial channel for Telegram channel {} not found",
                        self.channel_id,
                    )
            except Exception as ex:
                logger.error(
                    "Failed to create automatic ad blackout for channel post {} in {}: {}",
                    message.message_id,
                    self.channel_id,
                    ex,
                )
            logger.info(f"advertising data: {self.advertising_data}, time: {time}")
            return is_new

        @logger.catch
        @self.sup_bot.edited_channel_post_handler(
            content_types=['text', 'photo', 'video', 'animation', 'document', 'audio']
        )
        @self.sup_bot.channel_post_handler(
            content_types=['text', 'photo', 'video', 'animation', 'document', 'audio']
        )
        async def snipe_post(message: Message) -> None:
            ignored_links: set[str] = set()
            try:
                async with session_factory() as session:
                    ignored_links = await self.ad_link_exclusion_service.list_normalized_links(session)
            except Exception as exc:
                logger.error(
                    "Failed to load automatic ad link exclusions for channel {}: {}",
                    self.channel_id,
                    exc,
                )
            check_link = await Utils.check_link(
                message,
                ignored_channel_ref=self.channel_signature_ref,
                ignored_links=ignored_links,
            )
            if check_link:
                if await save_advertising(message):
                    await shift_timer()

        @logger.catch
        async def save_delayed_post(call: CallbackQuery) -> bool:
            command, day_div, time, message_id, sender_id = call.data.split(";")
            time_public = await Utils.get_timestamp_public(time)
            logger.info(f"command: {command}, time: {time}, sender_id: {sender_id}")

            message_id = int(message_id)
            blocked_until = await self._get_publication_blocked_until(float(time_public))
            if blocked_until is not None:
                await self._warn_publication_blocked(call, blocked_until)
                return False

            logger.info(self.delayed_message)
            logger.info(message_id)
            already_delayed = message_id in self.delayed_message
            self.delayed_message[message_id] = [int(time_public), int(sender_id)]
            await self.delayed_database.upsert_delayed_post({
                "bot_id": self.bot_info.id,
                "time_seconds": int(time_public),
                "message_id": message_id,
                "sender_id": int(sender_id)
            })
            old_time = await Utils.get_timestamp_to_time(self.delayed_message[message_id][0])

            logger.debug(old_time)
            logger.info("time post set" if already_delayed else "post delayed")
            return True

        @logger.catch
        @self.sup_bot.callback_query_handler(func=lambda call: True)
        async def callback(call: CallbackQuery) -> None:
            buttons_func = MarkupButton(self.sup_bot)
            utils_func = Utils()
            action = call.data.split(";")[0]
            if action != "agent_info":
                await utils_func.save_admin_action(call)
            if action not in {"send_suggest", "day_choice", "agent_info"}:
                await self._safe_answer_callback(call)

            match action:
                case "agent_info":
                    await self._safe_answer_callback(call, text="Обработано агентом")
                case "banned_user":
                    await buttons_func.add_ban_user(call, self.ban_database, self.channel_id, self.bot_info, self.chat_suggest)
                    await self._sync_editorial_submission_status(
                        review_message_id=call.message.message_id,
                        status=SubmissionStatus.REJECTED,
                        moderator_note="Handled in legacy moderation: banned",
                        reviewer_id=call.from_user.id,
                        moderation_decision="rejected",
                        moderation_action="ban_submission_author",
                    )
                case "add_info":
                    await utils_func.save_admin_action(call)
                    await buttons_func.add_info(call)
                case "send_suggest":
                    is_anon = call.message.message_id in self.anonym_send
                    sender_id = int(call.data.split(";")[1])
                    blocked_until = await self._get_publication_blocked_until(datetime.now(timezone.utc).timestamp())
                    if blocked_until is not None:
                        await self._warn_publication_blocked(call, blocked_until)
                        return
                    logger.debug(sender_id)
                    info_sender = await self.sup_bot.get_chat(sender_id)
                    logger.debug(info_sender)
                    if info_sender.username is None:
                        is_anon = False
                    logger.debug(self.bot_info)
                    media_group = await self._get_review_media_group(
                        review_chat_id=call.message.chat.id,
                        review_message_id=call.message.message_id,
                    )
                    try:
                        publication_claimed = await self.legacy_moderation_sync.claim_legacy_publication(
                            channel_tg_id=self.channel_id,
                            review_chat_id=call.message.chat.id,
                            review_message_id=call.message.message_id,
                        )
                    except Exception as ex:
                        logger.error(
                            "Refusing legacy publication because the editorial claim failed for message {}: {}",
                            call.message.message_id,
                            ex,
                        )
                        await self._safe_answer_callback(
                            call,
                            text="Не удалось зафиксировать публикацию. Сообщение не отправлено.",
                            show_alert=True,
                        )
                        return
                    if not publication_claimed:
                        logger.warning(
                            "Skipped duplicate legacy publication claim for review message {} in channel {}",
                            call.message.message_id,
                            self.channel_id,
                        )
                        await self._safe_answer_callback(
                            call,
                            text="Сообщение уже обработано или ожидает публикации.",
                            show_alert=True,
                        )
                        return

                    await self._safe_answer_callback(call)
                    try:
                        telegram_message_id = await asyncio.wait_for(
                            buttons_func.send_suggest(
                                call,
                                self.channel_signature_ref,
                                self.channel_id,
                                is_anon,
                                self.channel_title,
                                media_group=media_group,
                            ),
                            timeout=settings.telegram_request_timeout_seconds,
                        )
                    except Exception as ex:
                        if not is_transient_telegram_error(ex):
                            logger.error("Permanent legacy publication error: {}", ex)
                            try:
                                await self.legacy_moderation_sync.release_legacy_publication_claim(
                                    channel_tg_id=self.channel_id,
                                    review_chat_id=call.message.chat.id,
                                    review_message_id=call.message.message_id,
                                    error_text=str(ex),
                                )
                            except Exception as release_ex:
                                logger.error(
                                    "Failed to release legacy publication claim for message {}: {}",
                                    call.message.message_id,
                                    release_ex,
                                )
                            return

                        retry_at = int(datetime.now(timezone.utc).timestamp()) + settings.telegram_retry_delay_seconds
                        self.delayed_message[call.message.message_id] = [retry_at, sender_id]
                        await self.delayed_database.upsert_delayed_post({
                            "bot_id": self.bot_info.id,
                            "time_seconds": retry_at,
                            "message_id": call.message.message_id,
                            "sender_id": sender_id,
                        })
                        await self._sync_editorial_submission_status(
                            review_message_id=call.message.message_id,
                            status=SubmissionStatus.CONTENT_CREATED,
                            moderator_note="Handled in legacy moderation: publish deferred after Telegram outage",
                            legacy_scheduled_for=datetime.fromtimestamp(retry_at, tz=timezone.utc),
                            reviewer_id=call.from_user.id,
                            moderation_decision="approved",
                            moderation_action="publish_now_deferred",
                        )
                        logger.warning(
                            "Deferred legacy publication message {} for retry at {} after transient Telegram error: {}",
                            call.message.message_id,
                            retry_at,
                            ex,
                        )
                        return
                    if telegram_message_id is not None:
                        try:
                            finalized = await self.legacy_moderation_sync.mark_legacy_published(
                                channel_tg_id=self.channel_id,
                                review_chat_id=call.message.chat.id,
                                review_message_id=call.message.message_id,
                                telegram_message_id=int(telegram_message_id),
                                moderator_note="Handled in legacy moderation: published",
                                reviewer_id=call.from_user.id,
                                moderation_action="publish_now",
                            )
                            if not finalized:
                                raise RuntimeError("Legacy publication audit row was not found")
                        except Exception as finalize_ex:
                            # The pre-send claim remains committed, so neither MCP nor
                            # the slot publisher can publish the submission again.
                            logger.error(
                                "Legacy post {} was sent as Telegram message {}, but audit finalization failed: {}",
                                call.message.message_id,
                                telegram_message_id,
                                finalize_ex,
                            )
                            await self.sup_bot.send_message(
                                chat_id=call.message.chat.id,
                                text=(
                                    "Пост опубликован, но не удалось завершить запись аудита. "
                                    "Повторная публикация заблокирована; проверьте журнал ошибок."
                                ),
                            )
                        await utils_func.save_post(
                            call,
                            self.channel_id,
                            info_sender,
                            self.bot_info.username,
                        )
                        self.anonym_send.discard(call.message.message_id)
                        await self.anonym_message_database.delete_posts({
                            "message_id": call.message.message_id,
                            "chat_id": call.message.chat.id,
                        })
                case "approve_to_slot":
                    sender_id = int(call.data.split(";")[1])
                    try:
                        item = await self.legacy_moderation_sync.approve_review_message(
                            channel_tg_id=self.channel_id,
                            review_chat_id=call.message.chat.id,
                            review_message_id=call.message.message_id,
                            reviewer_id=call.from_user.id,
                        )
                    except Exception as ex:
                        logger.error("Failed to approve legacy submission into slot pipeline: {}", ex)
                        await self.sup_bot.send_message(
                            chat_id=call.message.chat.id,
                            text=f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0434\u043e\u0431\u0440\u0438\u0442\u044c \u0432 \u0441\u043b\u043e\u0442: {ex}",
                        )
                        return
                    if item is None:
                        await self.sup_bot.send_message(
                            chat_id=call.message.chat.id,
                            text="\u041d\u0435 \u043d\u0430\u0448\u0451\u043b \u044d\u0442\u0443 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u043a\u0443 \u0432 legacy-\u0431\u0430\u0437\u0435.",
                        )
                        return
                    self.delayed_message.pop(call.message.message_id, None)
                    self.anonym_send.discard(call.message.message_id)
                    await buttons_func.approve_to_slot_button(
                        call.message.chat.id,
                        call.message.message_id,
                        sender_id,
                        moderator_id=call.from_user.id,
                        moderator_username=getattr(call.from_user, "username", None),
                        moderator_first_name=getattr(call.from_user, "first_name", None),
                    )
                case "cancel_approve_to_slot":
                    sender_id = int(call.data.split(";")[1])
                    try:
                        item = await self.legacy_moderation_sync.cancel_review_message_approval(
                            channel_tg_id=self.channel_id,
                            review_chat_id=call.message.chat.id,
                            review_message_id=call.message.message_id,
                            reviewer_id=call.from_user.id,
                        )
                    except Exception as ex:
                        logger.error("Failed to cancel legacy slot approval: {}", ex)
                        await self.sup_bot.send_message(
                            chat_id=call.message.chat.id,
                            text=f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043c\u0435\u043d\u0438\u0442\u044c: {ex}",
                        )
                        return
                    await buttons_func.main_menu(
                        sender_id=call.message.chat.id,
                        chat_id=sender_id,
                        message_id=call.message.message_id,
                        chat_suggest=self.chat_suggest,
                        is_send=False,
                        is_anon=(call.message.message_id in self.anonym_send),
                    )
                    suffix = f" Content item #{item.id} переведён в hold." if item is not None else ""
                    await self.sup_bot.send_message(
                        chat_id=call.message.chat.id,
                        text=f"\u041e\u0434\u043e\u0431\u0440\u0435\u043d\u0438\u0435 \u0432 \u0441\u043b\u043e\u0442 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u043e.{suffix}",
                    )
                case "reject":
                    await buttons_func.reject_post(
                        call,
                        moderator_id=call.from_user.id,
                        moderator_username=getattr(call.from_user, "username", None),
                        moderator_first_name=getattr(call.from_user, "first_name", None),
                    )
                    await self._sync_editorial_submission_status(
                        review_message_id=call.message.message_id,
                        status=SubmissionStatus.REJECTED,
                        moderator_note="Handled in legacy moderation: rejected",
                        reviewer_id=call.from_user.id,
                        moderation_decision="rejected",
                        moderation_action="reject",
                    )
                case "cancel_reject":
                    sender_id = int(call.data.split(";")[1])
                    try:
                        restored = await self.legacy_moderation_sync.cancel_review_message_rejection(
                            channel_tg_id=self.channel_id,
                            review_chat_id=call.message.chat.id,
                            review_message_id=call.message.message_id,
                            reviewer_id=call.from_user.id,
                        )
                    except Exception as ex:
                        logger.error("Failed to cancel legacy rejection: {}", ex)
                        await self.sup_bot.send_message(
                            chat_id=call.message.chat.id,
                            text=f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043c\u0435\u043d\u0438\u0442\u044c \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0438\u0435: {ex}",
                        )
                        return
                    if not restored:
                        await self.sup_bot.send_message(
                            chat_id=call.message.chat.id,
                            text="\u041d\u0435 \u043d\u0430\u0448\u0451\u043b \u044d\u0442\u0443 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u043a\u0443 \u0432 legacy-\u0431\u0430\u0437\u0435.",
                        )
                        return
                    await buttons_func.main_menu(
                        sender_id=call.message.chat.id,
                        chat_id=sender_id,
                        message_id=call.message.message_id,
                        chat_suggest=self.chat_suggest,
                        is_send=False,
                        is_anon=(call.message.message_id in self.anonym_send),
                    )
                case "delayed_button":
                    await buttons_func.delayed_post(call)
                case "morning" | "dinner" | "evening" | "night":
                    await buttons_func.delayed_day(
                        call,
                        call.data.split(";")[0],
                        int(call.data.split(";")[1]),
                        self.advertising_data
                    )
                case "back_to_main_menu":
                    chat_id = call.data.split(";")[-1]
                    await buttons_func.main_menu(
                        call.message.chat.id, chat_id,
                        call.message.message_id,
                        self.chat_suggest,
                        is_send=False
                    )
                case "day_choice":
                    time_public = await Utils.get_timestamp_public(call.data.split(";")[2])
                    blocked_until = await self._get_publication_blocked_until(float(time_public))
                    if blocked_until is not None:
                        await self._warn_publication_blocked(call, blocked_until)
                        return
                    await self._safe_answer_callback(call)
                    sender_info = await self.sup_bot.get_chat(int(call.data.split(";")[4]))
                    try:
                        publication_claimed = await self.legacy_moderation_sync.claim_legacy_publication(
                            channel_tg_id=self.channel_id,
                            review_chat_id=call.message.chat.id,
                            review_message_id=call.message.message_id,
                        )
                    except Exception as ex:
                        logger.error(
                            "Refusing delayed legacy publication because the editorial claim failed "
                            "for message {}: {}",
                            call.message.message_id,
                            ex,
                        )
                        await self.sup_bot.send_message(
                            call.message.chat.id,
                            "Не удалось зафиксировать публикацию. Отложенная отправка не создана.",
                        )
                        return
                    if not publication_claimed:
                        logger.warning(
                            "Skipped duplicate delayed legacy publication claim for review message {} "
                            "in channel {}",
                            call.message.message_id,
                            self.channel_id,
                        )
                        await self.sup_bot.send_message(
                            call.message.chat.id,
                            "Сообщение уже обработано или ожидает публикации.",
                        )
                        return
                    if not await save_delayed_post(call):
                        try:
                            await self.legacy_moderation_sync.release_legacy_publication_claim(
                                channel_tg_id=self.channel_id,
                                review_chat_id=call.message.chat.id,
                                review_message_id=call.message.message_id,
                                error_text="legacy delayed publication was not scheduled",
                            )
                        except Exception as release_ex:
                            logger.error(
                                "Failed to release delayed legacy publication claim for message {}: {}",
                                call.message.message_id,
                                release_ex,
                            )
                        return
                    await utils_func.save_post(
                        call,
                        self.channel_id,
                        sender_info,
                        self.bot_info.username,
                    )
                    await self._sync_editorial_submission_status(
                        review_message_id=call.message.message_id,
                        status=SubmissionStatus.CONTENT_CREATED,
                        moderator_note="Handled in legacy moderation: delayed",
                        legacy_scheduled_for=datetime.fromtimestamp(float(time_public), tz=timezone.utc),
                        reviewer_id=call.from_user.id,
                        moderation_decision="approved",
                        moderation_action="schedule_delayed",
                    )
                    logger.debug(call.data)
                    await buttons_func.delayed_buttons_times(
                        call,
                        int(call.data.split(";")[4])
                    )
                case "reject_delayed":
                    review_message_id = call.message.message_id
                    await self.delayed_database.delete_delayed_posts({
                        "bot_id": self.bot_info.id,
                        "message_id": review_message_id,
                    })
                    self.delayed_message.pop(review_message_id, None)
                    await buttons_func.reject_post(
                        call,
                        moderator_id=call.from_user.id,
                        moderator_username=getattr(call.from_user, "username", None),
                        moderator_first_name=getattr(call.from_user, "first_name", None),
                    )
                    await self._sync_editorial_submission_status(
                        review_message_id=review_message_id,
                        status=SubmissionStatus.REJECTED,
                        moderator_note="Handled in legacy moderation: delayed rejected",
                        reviewer_id=call.from_user.id,
                        moderation_decision="rejected",
                        moderation_action="reject_delayed",
                    )
                case "anonym_button":
                    data = {
                        "message_id": call.message.message_id,
                        "chat_id": self.chat_suggest,
                    }
                    logger.debug(call.message.message_id in self.anonym_send)
                    if call.message.message_id in self.anonym_send:
                        self.anonym_send.remove(call.message.message_id)
                        await self.anonym_message_database.delete_posts(data)
                    else:
                        self.anonym_send.add(call.message.message_id)
                        await self.anonym_message_database.add_posts(data)

                    logger.debug(call.message.message_id in self.anonym_send)
                    await buttons_func.main_menu(
                        sender_id=call.message.chat.id,
                        chat_id=call.data.split(";")[-1],
                        message_id=call.message.message_id,
                        chat_suggest=self.chat_suggest,
                        is_anon=(call.message.id in self.anonym_send),
                        is_send=False,
                    )
                case "advertising_button":
                    sender_id = call.data.split(";")[1]
                    info_sender = await self.sup_bot.get_chat(sender_id)
                    media_group = await self._get_review_media_group(
                        review_chat_id=call.message.chat.id,
                        review_message_id=call.message.message_id,
                    )
                    await buttons_func.advertising_button(call)
                    await self.callback_adv_action(
                        call,
                        self.sup_bot,
                        self.channel_username or self.channel_title or str(self.channel_id),
                        info_sender,
                        (
                            media_group.caption
                            if media_group is not None
                            else call.message.text or call.message.caption
                        ),
                    )
                    await self._sync_editorial_submission_status(
                        review_message_id=call.message.message_id,
                        status=SubmissionStatus.ADVERTISING,
                        moderator_note="Handled in legacy moderation: advertising",
                        reviewer_id=call.from_user.id,
                        moderation_action="advertise_submission",
                    )

    async def _send_review_message_to_legacy_chat(self, message: Message):
        try:
            return await (
                MarkupButton(self.sup_bot)
                .main_menu(message.chat.id, message.chat.id, message.message_id, self.chat_suggest)
            )
        except ApiTelegramException as ex:
            if ex.error_code != 403:
                raise
            logger.warning(
                "Failed to forward submission for bot @{} to legacy chat {}: {}. Refreshing chat binding.",
                self.bot_info.username,
                self.chat_suggest,
                ex,
            )
            await self._drop_invalid_chat_suggest(self.chat_suggest)
            self.chat_suggest = await self._refresh_chat_suggest()
            if self.chat_suggest is None:
                await self.sup_bot.send_message(
                    chat_id=settings.general_admin,
                    text=(
                        f"Битая привязка легаси-чата у @{self.bot_info.username} была очищена. "
                        f"Добавьте саббота заново в нужный модер-чат для канала "
                        f"{self.channel_username or self.channel_title or self.channel_id}."
                    ),
                )
                return None
            return await (
                MarkupButton(self.sup_bot)
                .main_menu(message.chat.id, message.chat.id, message.message_id, self.chat_suggest)
            )

    async def _save_incoming_message(self, message: Message, review_message) -> None:
        await Utils().save_incoming_message_with_review(
            message=message,
            channel_id=self.channel_id,
            bot_info=self.bot_info.username,
            review_chat_id=self.chat_suggest,
            review_message_id=getattr(review_message, "message_id", None),
        )

    async def _notify_new_submission(self, review_message) -> None:
        if self.callback_new_submission is None or review_message is None:
            return
        try:
            await self.callback_new_submission(
                channel_tg_id=self.channel_id,
                review_chat_id=self.chat_suggest,
                review_message_id=review_message.message_id,
            )
        except Exception as ex:
            logger.error("Failed to send moderator notifications: {}", ex)

    async def _queue_media_group(self, message: Message) -> None:
        key = (int(message.chat.id), str(message.media_group_id))
        async with self._media_group_lock:
            messages = self._media_group_messages.setdefault(key, [])
            if not any(item.message_id == message.message_id for item in messages):
                messages.append(message)

            previous_task = self._media_group_flush_tasks.get(key)
            if previous_task is not None:
                previous_task.cancel()
            self._media_group_flush_tasks[key] = asyncio.create_task(
                self._flush_media_group_after_delay(key)
            )

    async def _flush_media_group_after_delay(self, key: tuple[int, str]) -> None:
        try:
            await asyncio.sleep(self.MEDIA_GROUP_SETTLE_SECONDS)
        except asyncio.CancelledError:
            return

        async with self._media_group_lock:
            if self._media_group_flush_tasks.get(key) is not asyncio.current_task():
                return
            messages = self._media_group_messages.pop(key, [])
            self._media_group_flush_tasks.pop(key, None)

        if not messages:
            return
        try:
            await self._send_media_group_to_legacy_chat(messages)
        except Exception as ex:
            logger.exception(
                "Failed to forward media group {} for bot @{} to legacy chat {}: {}",
                key[1],
                self.bot_info.username,
                self.chat_suggest,
                ex,
            )

    async def _send_media_group_to_legacy_chat(self, messages: list[Message]) -> None:
        messages = sorted(messages, key=lambda item: item.message_id)
        try:
            copied_messages = await self._copy_media_group_to_current_legacy_chat(messages)
        except ApiTelegramException as ex:
            if ex.error_code != 403:
                raise
            logger.warning(
                "Failed to forward media group for bot @{} to legacy chat {}: {}. Refreshing chat binding.",
                self.bot_info.username,
                self.chat_suggest,
                ex,
            )
            await self._drop_invalid_chat_suggest(self.chat_suggest)
            self.chat_suggest = await self._refresh_chat_suggest()
            if self.chat_suggest is None:
                await self.sup_bot.send_message(
                    chat_id=settings.general_admin,
                    text=(
                        f"Битая привязка легаси-чата у @{self.bot_info.username} была очищена. "
                        f"Добавьте саббота заново в нужный модер-чат для канала "
                        f"{self.channel_username or self.channel_title or self.channel_id}."
                    ),
                )
                return
            copied_messages = await self._copy_media_group_to_current_legacy_chat(messages)

        if len(copied_messages) != len(messages):
            raise RuntimeError(
                f"Telegram copied {len(copied_messages)} of {len(messages)} media group messages"
            )

        markup = await MarkupButton(self.sup_bot).get_main_menu_markup(messages[0].chat.id)
        control_message = await self.sup_bot.send_message(
            chat_id=self.chat_suggest,
            text=(
                f"🖼 Медиагруппа: {len(messages)} влож.\n"
                "Действия ниже применяются ко всему альбому."
            ),
            reply_markup=markup,
        )

        for message in messages:
            await self._save_incoming_message(message, control_message)

        await self._notify_new_submission(control_message)

    async def _copy_media_group_to_current_legacy_chat(self, messages: list[Message]):
        return await self.sup_bot.copy_messages(
            chat_id=self.chat_suggest,
            from_chat_id=messages[0].chat.id,
            message_ids=[message.message_id for message in messages],
        )

    async def _get_review_media_group(
        self,
        *,
        review_chat_id: int,
        review_message_id: int,
    ) -> LegacyMediaGroupReference | None:
        row = await self.legacy_moderation_sync.legacy_reader.find_sender_row_by_review_message(
            channel_id=self.channel_id,
            review_chat_id=review_chat_id,
            review_message_id=review_message_id,
        )
        if row is None or not row.media_group_id or row.chat_id is None:
            return None
        rows = await fetch_sender_media_group_rows(
            self.legacy_moderation_sync.legacy_reader,
            channel_id=self.channel_id,
            source_chat_id=int(row.chat_id),
            media_group_id=row.media_group_id,
        )
        return build_media_group_reference(rows)

    async def _refresh_chat_suggest(self) -> int | None:
        candidates = await self.admins_database.get_chat_admin_rows(self.bot_info.id)
        if not candidates:
            return None

        for candidate_chat_id in candidates:
            if await self._is_active_legacy_chat(candidate_chat_id):
                return candidate_chat_id
            await self._drop_invalid_chat_suggest(candidate_chat_id)
        return None

    async def _is_active_legacy_chat(self, chat_id: int) -> bool:
        try:
            member = await self.sup_bot.get_chat_member(chat_id=chat_id, user_id=self.bot_info.id)
        except ApiTelegramException as ex:
            logger.warning(
                "Failed to validate legacy moderation chat {} for @{}: {}",
                chat_id,
                self.bot_info.username,
                ex,
            )
            return False
        return member.status in ("administrator", "creator", "member")

    async def _drop_invalid_chat_suggest(self, chat_id: int | None) -> None:
        if chat_id is None:
            return
        try:
            await self.admins_database.delete_chat_admins({
                "bot_id": self.bot_info.id,
                "chat_id": chat_id,
            })
            logger.warning(
                "Removed stale legacy moderation chat {} for @{}",
                chat_id,
                self.bot_info.username,
            )
        except Exception as ex:
            logger.error(
                "Failed to delete stale legacy moderation chat {} for @{}: {}",
                chat_id,
                self.bot_info.username,
                ex,
            )

    @logger.catch
    async def check_admin(self, channel_id) -> bool:
        try:
            member = await self.sup_bot.get_chat_member(chat_id=channel_id, user_id=self.bot_info.id)
            return member.status in ("administrator", "creator")
        except ApiTelegramException as ex:
            logger.warning("Failed to check subbot admin by channel id {}: {}", channel_id, ex)

        for chat_ref in (channel_id, self.channel_username):
            try:
                admins = await self.sup_bot.get_chat_administrators(chat_ref)
            except ApiTelegramException as retry_ex:
                logger.warning("Failed to check subbot admins by chat {}: {}", chat_ref, retry_ex)
                continue
            if any(getattr(admin.user, "id", None) == self.bot_info.id for admin in admins):
                return True
        return False

    async def _sync_editorial_submission_status(
        self,
        review_message_id: int,
        status: SubmissionStatus,
        moderator_note: str,
        legacy_scheduled_for: datetime | None = None,
        reviewer_id: int | None = None,
        moderation_decision: str | None = None,
        moderation_action: str = "legacy_status",
    ) -> None:
        if self.chat_suggest is None:
            return
        try:
            await self.legacy_moderation_sync.set_status_for_review_message(
                channel_tg_id=self.channel_id,
                review_chat_id=self.chat_suggest,
                review_message_id=review_message_id,
                status=status,
                moderator_note=moderator_note,
                legacy_scheduled_for=legacy_scheduled_for,
                reviewer_id=reviewer_id,
                moderation_decision=moderation_decision,
                moderation_action=moderation_action,
            )
        except Exception as ex:
            logger.error("Failed to sync legacy moderation action to editorial submission: {}", ex)

    async def _get_publication_blocked_until(self, timestamp: float) -> float | None:
        legacy_until = self.publication_guard.next_timestamp_after_legacy_ad_window(
            timestamp,
            self.advertising_data,
            settings.shift_time_seconds,
        )
        try:
            blackout = await self.publication_guard.get_blackout_for_telegram_channel(
                tg_channel_id=self.channel_id,
                when=datetime.fromtimestamp(timestamp, tz=timezone.utc),
            )
        except Exception as ex:
            logger.error("Failed to check editorial ad blackout for legacy publication: {}", ex)
            blackout = None
        blackout_until = blackout.ends_at.timestamp() if blackout is not None else None
        candidates = [item for item in (legacy_until, blackout_until) if item is not None]
        if not candidates:
            return None
        return max(candidates)

    async def _warn_publication_blocked(self, call: CallbackQuery, blocked_until: float) -> None:
        until_text = await Utils.get_timestamp_to_time(blocked_until)
        text = (
            f"Ad window is active until {until_text}. "
            "Legacy publication is blocked; choose another time or try later."
        )
        try:
            await self._safe_answer_callback(call, text=text[:200], show_alert=True)
        except Exception as ex:
            logger.debug("Failed to answer blocked legacy publication callback: {}", ex)
        await self.sup_bot.send_message(call.message.chat.id, text)

    async def reschedule_delayed_if_publication_blocked(
        self,
        message_id: int,
        sender_id: int | str,
        expected_time: int | float | str | None = None,
    ) -> bool:
        if expected_time is not None and not await self.is_delayed_publication_current(
            message_id,
            sender_id,
            expected_time,
        ):
            return False

        now_timestamp = datetime.now(timezone.utc).timestamp()
        blocked_until = await self._get_publication_blocked_until(now_timestamp)
        if blocked_until is None:
            return False

        new_time = int(max(blocked_until, now_timestamp + settings.const_time_sleep))
        self.delayed_message[message_id] = [new_time, int(sender_id)]
        await self.delayed_database.upsert_delayed_post({
            "bot_id": self.bot_info.id,
            "time_seconds": new_time,
            "message_id": int(message_id),
            "sender_id": int(sender_id),
        })
        await self._sync_editorial_submission_status(
            review_message_id=message_id,
            status=SubmissionStatus.CONTENT_CREATED,
            moderator_note="Handled in legacy moderation: delayed rescheduled by ad window",
            legacy_scheduled_for=datetime.fromtimestamp(float(new_time), tz=timezone.utc),
        )
        try:
            markup = await MarkupButton(self.sup_bot).get_markup(new_time, sender_id)
            await self.sup_bot.edit_message_reply_markup(
                chat_id=self.chat_suggest,
                message_id=message_id,
                reply_markup=markup,
            )
        except Exception as ex:
            logger.error("Failed to refresh delayed post markup after ad-window reschedule: {}", ex)
        logger.info(
            "Rescheduled delayed message {} for channel {} because an ad window is active until {}",
            message_id,
            self.channel_username,
            new_time,
        )
        return True

    async def stop_bot(self) -> None:
        media_group_tasks = list(self._media_group_flush_tasks.values())
        self._media_group_flush_tasks.clear()
        self._media_group_messages.clear()
        for task in media_group_tasks:
            task.cancel()
        if media_group_tasks:
            await asyncio.gather(*media_group_tasks, return_exceptions=True)
        if self.polling_task:
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                logger.info(f"Бот {self.bot_info.username} остановлен")

    async def getter_name(self):
        return self.bot_info.username

    async def getter_delayed_info(self) -> dict:
        return self.delayed_message

    async def is_delayed_publication_current(
        self,
        message_id: int,
        sender_id: int | str,
        expected_time: int | float | str,
    ) -> bool:
        current = self.delayed_message.get(int(message_id))
        if current is None:
            logger.info("Skip stale delayed message {}: it is no longer scheduled", message_id)
            return False

        current_time, current_sender_id = current
        is_current = delayed_publication_matches(self.delayed_message, message_id, sender_id, expected_time)
        if not is_current:
            logger.info(
                "Skip stale delayed message {}: expected time/sender {}:{}, current {}:{}",
                message_id,
                expected_time,
                sender_id,
                current_time,
                current_sender_id,
            )
        return is_current

    async def send_delayed_message(self, message_id, sender_id, expected_time=None) -> bool:
        if expected_time is not None and not await self.is_delayed_publication_current(
            message_id,
            sender_id,
            expected_time,
        ):
            return False

        markup = None
        is_anonymous = message_id in self.anonym_send

        if is_anonymous:
            info_user = await self.sup_bot.get_chat(sender_id)
            if info_user.username is not None:
                button = InlineKeyboardButton(text=f"@{info_user.username}", url=f"https://t.me/{info_user.username}")
                markup = InlineKeyboardMarkup()
                markup.add(button)

        add_signature = await should_add_publication_signature(
            bot_token=self.sup_bot.token,
            channel_id=int(self.channel_id),
        )

        media_group = await self._get_review_media_group(
            review_chat_id=self.chat_suggest,
            review_message_id=int(message_id),
        )

        if media_group is not None and media_group.source_message_ids:
            copied_messages = await self.sup_bot.copy_messages(
                from_chat_id=media_group.source_chat_id,
                chat_id=self.channel_id,
                message_ids=media_group.source_message_ids,
            )
            if not copied_messages:
                raise RuntimeError("Telegram returned no copied delayed media group messages")
            copied_message = copied_messages[0]
            caption_index = min(media_group.caption_index, len(copied_messages) - 1)
            published_caption_message = copied_messages[caption_index]
            try:
                if add_signature:
                    signature_html = publication_signature_html(
                        title=self.channel_title,
                        channel_ref=self.channel_signature_ref,
                    )
                    await self.sup_bot.edit_message_caption(
                        chat_id=self.channel_id,
                        message_id=published_caption_message.message_id,
                        caption=format_publication_html(
                            media_group.caption,
                            signature_html=signature_html,
                        ),
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                elif markup is not None:
                    await self.sup_bot.edit_message_reply_markup(
                        chat_id=self.channel_id,
                        message_id=published_caption_message.message_id,
                        reply_markup=markup,
                    )
            except Exception as caption_ex:
                # copy_messages is not idempotent: after it returns, retrying the
                # whole delayed job would publish the album a second time.
                logger.error(
                    "Copied delayed legacy media group as message {}, but failed to update its "
                    "caption/markup; treating the copy as sent: {}",
                    copied_message.message_id,
                    caption_ex,
                )
        elif not add_signature:
            copied_message = await self.sup_bot.copy_message(
                from_chat_id=self.chat_suggest,
                chat_id=self.channel_id,
                message_id=message_id,
                reply_markup=markup,
            )
        else:
            signature_html = publication_signature_html(
                title=self.channel_title,
                channel_ref=self.channel_signature_ref,
            )
            legacy_row = await self.legacy_moderation_sync.legacy_reader.find_sender_row_by_review_message(
                channel_id=self.channel_id,
                review_chat_id=self.chat_suggest,
                review_message_id=int(message_id),
            )
            source_text = legacy_row.text_post if legacy_row is not None else None
            content_type = (legacy_row.content_type if legacy_row is not None else None) or "media"

            if content_type == "text":
                copied_message = await self.sup_bot.send_message(
                    chat_id=self.channel_id,
                    text=format_publication_html(source_text, signature_html=signature_html),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )
            else:
                copied_message = await self.sup_bot.copy_message(
                    from_chat_id=self.chat_suggest,
                    chat_id=self.channel_id,
                    message_id=message_id,
                    caption=format_publication_html(source_text, signature_html=signature_html),
                    parse_mode="HTML",
                    reply_markup=markup,
                )
        self.delayed_message.pop(message_id, None)
        if is_anonymous:
            self.anonym_send.discard(message_id)
        logger.info(f"send delayed message: {message_id}, username_channel: {self.channel_username}")
        try:
            await self.legacy_moderation_sync.mark_legacy_delayed_published(
                channel_tg_id=self.channel_id,
                review_chat_id=self.chat_suggest,
                review_message_id=message_id,
                telegram_message_id=getattr(copied_message, "message_id", None),
            )
        except Exception as ex:
            logger.error("Failed to mark legacy delayed audit as published: {}", ex)
        try:
            await MarkupButton(self.sup_bot).push_post_button(self.chat_suggest, message_id, sender_id)
        except Exception as ex:
            logger.error("Failed to update legacy delayed markup after publish: {}", ex)
        return True

    async def run_bot(self) -> None:
        self.bot_info = await self.sup_bot.get_me()
        try:
            logger.info(f"[OK] bot @{self.bot_info.username} working")
            self.polling_task = asyncio.create_task(
                self.sup_bot.infinity_polling(
                    timeout=10,
                    request_timeout=settings.telegram_request_timeout_seconds,
                )
            )
        except Exception as ex:
            logger.error(f"bot: @{self.bot_info.username}, mistake: {ex}")
