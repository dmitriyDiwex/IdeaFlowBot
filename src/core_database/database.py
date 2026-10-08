import asyncio
import sqlite3

from loguru import logger
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import insert, select, and_, delete, update, Delete, func

from src.core_database.models.base import Base
from src.core_database.models.banned_user import BannedUser
from src.core_database.models.bots_data import BotsData
from src.core_database.models.chat_admins import ChatAdmins
from src.core_database.models.service_message import ServiceMessage
from src.core_database.models.users import UserData
from src.core_database.models.delayed_posts import DelayedPost
from src.core_database.models.anonym_message import AnonymMessage
from src.core_database.models.db_helper import db_helper
from src.core_database.models.advertising import Advertising
from src.core_database.models.sender_info import SenderData
from src.core_database.models.admin_actions import AdminActionData
from src.core_database.models.bot_admins import BotAdmin
from src.core_database.config import settings


POSTGRES_BIGINT_COLUMNS = {
    "admin_actions_data": ["message_id", "chat_id", "admin_id", "timestamp"],
    "advertising": ["channel_id", "post_id", "time"],
    "anonym_message": ["message_id", "chat_id"],
    "banned_user": ["id_user", "id_channel", "bot_id"],
    "bots_data": ["channel_id"],
    "bot_admins": ["user_id"],
    "chat_admins": ["bot_id", "chat_id"],
    "delayed_posts": ["bot_id", "time_seconds", "message_id", "sender_id"],
    "sender_info": [
        "user_id",
        "channel_id",
        "message_id",
        "chat_id",
        "preview_file_size",
        "review_chat_id",
        "review_message_id",
        "timestamp",
    ],
    "service_message": ["bot_id"],
    "users": ["user_id"],
}

LEGACY_SQLITE_IMPORT_MARKER_TABLE = "legacy_sqlite_import_marker"


async def create_table():
    async with db_helper.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_legacy_schema()


async def ensure_legacy_schema() -> None:
    should_migrate_from_sqlite = False
    async with db_helper.engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            await conn.run_sync(Base.metadata.create_all)
            await _ensure_legacy_sqlite_import_marker_table(conn)
            await _ensure_postgres_bigint_columns(conn)
            should_migrate_from_sqlite = True
        else:
            result = await conn.exec_driver_sql("PRAGMA table_info(sender_info)")
            columns = {row[1] for row in result.fetchall()}

            if "content_type" not in columns:
                await conn.exec_driver_sql(
                    "ALTER TABLE sender_info ADD COLUMN content_type VARCHAR DEFAULT 'text'"
                )
            if "media_group_id" not in columns:
                await conn.exec_driver_sql(
                    "ALTER TABLE sender_info ADD COLUMN media_group_id VARCHAR"
                )
            if "preview_file_id" not in columns:
                await conn.exec_driver_sql(
                    "ALTER TABLE sender_info ADD COLUMN preview_file_id VARCHAR"
                )
            if "preview_file_size" not in columns:
                await conn.exec_driver_sql(
                    "ALTER TABLE sender_info ADD COLUMN preview_file_size INTEGER"
                )
            if "entities_json" not in columns:
                await conn.exec_driver_sql(
                    "ALTER TABLE sender_info ADD COLUMN entities_json TEXT"
                )
            if "review_chat_id" not in columns:
                await conn.exec_driver_sql(
                    "ALTER TABLE sender_info ADD COLUMN review_chat_id INTEGER"
                )
            if "review_message_id" not in columns:
                await conn.exec_driver_sql(
                    "ALTER TABLE sender_info ADD COLUMN review_message_id INTEGER"
                )

            result = await conn.exec_driver_sql("PRAGMA table_info(service_message)")
            service_columns = {row[1] for row in result.fetchall()}
            if "send_post_message" not in service_columns:
                await conn.exec_driver_sql(
                    "ALTER TABLE service_message ADD COLUMN send_post_message VARCHAR DEFAULT ''"
                )

    if should_migrate_from_sqlite:
        await migrate_legacy_sqlite_to_current_db()


def _sqlite_table_exists(sqlite_conn: sqlite3.Connection, table_name: str) -> bool:
    row = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _sqlite_table_row_count(sqlite_conn: sqlite3.Connection, table_name: str) -> int:
    if not _sqlite_table_exists(sqlite_conn, table_name):
        return 0
    row = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(row[0]) if row else 0


def _fetch_sqlite_rows(sqlite_conn: sqlite3.Connection, table) -> list[dict]:
    if not _sqlite_table_exists(sqlite_conn, table.name):
        return []

    sqlite_conn.row_factory = sqlite3.Row
    source_columns = {
        row[1]
        for row in sqlite_conn.execute(f"PRAGMA table_info({table.name})").fetchall()
    }
    target_columns = [column.name for column in table.columns]
    selected_columns = [column_name for column_name in target_columns if column_name in source_columns]
    if not selected_columns:
        return []

    query = f"SELECT {', '.join(selected_columns)} FROM {table.name}"
    rows = sqlite_conn.execute(query).fetchall()
    payload: list[dict] = []
    for row in rows:
        item = {column_name: row[column_name] for column_name in selected_columns}
        _normalize_legacy_row_values(table.name, item)
        payload.append(item)
    return payload


def _normalize_legacy_row_values(table_name: str, item: dict) -> None:
    for column_name in POSTGRES_BIGINT_COLUMNS.get(table_name, []):
        if column_name not in item:
            continue

        value = item[column_name]
        if value is None or isinstance(value, int):
            continue

        if isinstance(value, str):
            normalized_value = value.strip()
            if not normalized_value:
                item[column_name] = None
                continue
            item[column_name] = int(normalized_value)
            continue

        item[column_name] = int(value)


async def _set_postgres_sequence(conn, table_name: str) -> None:
    await conn.exec_driver_sql(
        f"""
        SELECT setval(
            pg_get_serial_sequence('"{table_name}"', 'id'),
            COALESCE((SELECT MAX(id) FROM "{table_name}"), 1),
            EXISTS(SELECT 1 FROM "{table_name}")
        )
        """
    )


async def _ensure_postgres_bigint_columns(conn) -> None:
    result = await conn.exec_driver_sql(
        """
        SELECT table_name, column_name, udt_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
        """
    )
    existing_column_types = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in result.fetchall()
    }

    for table_name, column_names in POSTGRES_BIGINT_COLUMNS.items():
        for column_name in column_names:
            current_type = existing_column_types.get((table_name, column_name))
            if current_type is None or current_type == "int8":
                continue

            logger.info(
                "Converting legacy PostgreSQL column {}.{} from {} to BIGINT",
                table_name,
                column_name,
                current_type,
            )
            await conn.exec_driver_sql(
                f'''
                ALTER TABLE "{table_name}"
                ALTER COLUMN "{column_name}" TYPE BIGINT
                USING NULLIF("{column_name}"::text, '')::bigint
                '''
            )


async def _legacy_source_needs_sync(conn, sqlite_conn: sqlite3.Connection) -> bool:
    for table in Base.metadata.sorted_tables:
        source_rows = _sqlite_table_row_count(sqlite_conn, table.name)
        if source_rows == 0:
            continue

        target_rows = await conn.scalar(select(func.count()).select_from(table))
        if int(target_rows or 0) < source_rows:
            return True

    return False


async def _ensure_legacy_sqlite_import_marker_table(conn) -> None:
    await conn.exec_driver_sql(
        f"""
        CREATE TABLE IF NOT EXISTS {LEGACY_SQLITE_IMPORT_MARKER_TABLE} (
            id INTEGER PRIMARY KEY,
            imported_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )


async def _legacy_sqlite_import_was_marked(conn) -> bool:
    row = await conn.exec_driver_sql(
        f"SELECT 1 FROM {LEGACY_SQLITE_IMPORT_MARKER_TABLE} WHERE id = 1 LIMIT 1"
    )
    return row.first() is not None


async def _mark_legacy_sqlite_imported(conn) -> None:
    await conn.exec_driver_sql(
        f"""
        INSERT INTO {LEGACY_SQLITE_IMPORT_MARKER_TABLE} (id)
        VALUES (1)
        ON CONFLICT (id) DO NOTHING
        """
    )


async def _current_postgres_legacy_tables_have_data(conn) -> bool:
    for table in Base.metadata.sorted_tables:
        target_rows = await conn.scalar(select(func.count()).select_from(table))
        if int(target_rows or 0) > 0:
            return True
    return False


def _sqlite_source_has_rows(sqlite_conn: sqlite3.Connection) -> bool:
    for table in Base.metadata.sorted_tables:
        if _sqlite_table_row_count(sqlite_conn, table.name) > 0:
            return True
    return False


async def migrate_legacy_sqlite_to_current_db() -> None:
    source_path = settings.legacy_sqlite_path
    if not source_path.exists():
        return

    async with db_helper.engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            return

        await _ensure_legacy_sqlite_import_marker_table(conn)
        if await _legacy_sqlite_import_was_marked(conn):
            return

        with sqlite3.connect(source_path) as sqlite_conn:
            sqlite_conn.row_factory = sqlite3.Row
            if not _sqlite_source_has_rows(sqlite_conn):
                await _mark_legacy_sqlite_imported(conn)
                return

            if await _current_postgres_legacy_tables_have_data(conn):
                logger.info(
                    "Skipping legacy SQLite import because PostgreSQL already has data; marking import as completed"
                )
                await _mark_legacy_sqlite_imported(conn)
                return

            for table in Base.metadata.sorted_tables:
                rows = _fetch_sqlite_rows(sqlite_conn, table)
                if not rows:
                    continue

                logger.info("Migrating {} legacy rows into {}", len(rows), table.name)
                for start in range(0, len(rows), 500):
                    chunk = rows[start:start + 500]
                    stmt = pg_insert(table).values(chunk).on_conflict_do_nothing(index_elements=[table.c.id])
                    await conn.execute(stmt)

            for table in Base.metadata.sorted_tables:
                await _set_postgres_sequence(conn, table.name)
            await _mark_legacy_sqlite_imported(conn)


async def drop_table():
    async with db_helper.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class CrudUserData:
    @staticmethod
    async def get_user_data(user_id: int = None, bot_username: str = None) -> list:
        async with db_helper.engine.connect() as conn:
            if user_id is None and bot_username is None:
                return (await conn.execute(select(UserData))).fetchall()
            elif bot_username is None:
                return (await conn.execute(select(UserData).filter(UserData.user_id == user_id))).fetchall()
            elif user_id is None:
                return (await conn.execute(select(UserData).filter(UserData.bot_username == bot_username))).fetchall()
            else:
                return (await conn.execute(
                    select(UserData)
                    .filter(and_(UserData.user_id == user_id, UserData.bot_username == bot_username))
                )).fetchall()

    @staticmethod
    async def insert_user(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            dialect_insert = {
                "postgresql": pg_insert,
                "sqlite": sqlite_insert,
            }.get(conn.dialect.name, insert)
            stmt = dialect_insert(UserData).values(data)
            if conn.dialect.name in {"postgresql", "sqlite"}:
                # Concurrent handlers or a stale cache may register the same pair.
                # Ignore only that unique key; other integrity errors must surface.
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=[UserData.user_id, UserData.bot_username],
                )
            await conn.execute(stmt)
            await conn.commit()

    @staticmethod
    async def delete_user_data(user_id: int, bot_username: str) -> None:
        async with db_helper.engine.connect() as conn:
            stmt = (
                delete(UserData)
                .filter(and_(
                    UserData.user_id == user_id,
                    UserData.bot_username == bot_username
                ))
            )
            await conn.execute(stmt)
            await conn.commit()


class CrudBannedUser:
    @staticmethod
    async def get_banned_users(id_user: int = None, id_channel: int = None) -> list:
        async with db_helper.engine.connect() as conn:
            if id_user is None and id_channel is None:
                return (await conn.execute(select(BannedUser))).fetchall()
            elif id_user is None and id_channel is not None:
                return (await conn.execute(select(BannedUser).filter(BannedUser.id_channel == id_channel))).fetchall()
            elif id_channel is None and id_user is not None:
                return (await conn.execute(select(BannedUser).filter(BannedUser.id_user == id_user))).fetchall()
            else:
                return (await conn.execute(
                    select(BannedUser)
                    .filter(and_(
                        BannedUser.id_channel == id_channel,
                        BannedUser.id_user == id_user
                    ))
                )).fetchall()

    @staticmethod
    async def add_banned_user(user: dict) -> None:
        async with db_helper.engine.connect() as conn:
            stmt = (
                insert(BannedUser).values(user)
            )
            await conn.execute(stmt)
            await conn.commit()

    @staticmethod
    async def delete_banned_user(user: dict) -> None:
        async with db_helper.engine.connect() as conn:
            stmt = (
                delete(BannedUser)
                .filter(and_(
                    BannedUser.id_user == user["id_user"],
                    BannedUser.id_channel == user["id_channel"]
                ))
            )
            await conn.execute(stmt)
            await conn.commit()

    # Telegram user IDs are positive and channel IDs are negative. The zero
    # channel/bot pair represents a network-wide ban, including future bots.
    GLOBAL_CHANNEL_ID = 0

    @staticmethod
    async def is_user_banned(id_user: int, id_channel: int) -> bool:
        async with db_helper.engine.connect() as conn:
            return await conn.scalar(
                select(BannedUser.id).where(
                    BannedUser.id_user == id_user,
                    BannedUser.id_channel.in_([id_channel, CrudBannedUser.GLOBAL_CHANNEL_ID]),
                ).limit(1)
            ) is not None

    @staticmethod
    async def add_global_ban(id_user: int) -> bool:
        async with db_helper.engine.connect() as conn:
            dialect_insert = {"postgresql": pg_insert, "sqlite": sqlite_insert}[conn.dialect.name]
            stmt = dialect_insert(BannedUser).values(id_user=id_user, id_channel=0, bot_id=0)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[BannedUser.id_user, BannedUser.id_channel, BannedUser.bot_id],
            ).returning(BannedUser.id)
            created = (await conn.execute(stmt)).scalar_one_or_none() is not None
            await conn.commit()
            return created

    @staticmethod
    async def delete_all_user_bans(id_user: int) -> int:
        async with db_helper.engine.connect() as conn:
            rows = (await conn.execute(
                delete(BannedUser).where(BannedUser.id_user == id_user).returning(BannedUser.id)
            )).all()
            await conn.commit()
            return len(rows)

    @staticmethod
    def _latest_usernames():
        return select(
            SenderData.user_id, SenderData.username,
            func.row_number().over(
                partition_by=SenderData.user_id,
                order_by=[SenderData.timestamp.desc(), SenderData.id.desc()],
            ).label("position"),
        ).subquery()

    @staticmethod
    async def get_last_username(id_user: int) -> str | None:
        async with db_helper.engine.connect() as conn:
            return await conn.scalar(
                select(SenderData.username).where(SenderData.user_id == id_user)
                .order_by(SenderData.timestamp.desc(), SenderData.id.desc()).limit(1)
            )

    @staticmethod
    async def find_user_ids_by_username(username: str) -> list[int]:
        latest = CrudBannedUser._latest_usernames()
        async with db_helper.engine.connect() as conn:
            return list((await conn.execute(select(latest.c.user_id).where(
                latest.c.position == 1,
                func.lower(func.ltrim(latest.c.username, "@")) == username.lower(),
            ).order_by(latest.c.user_id))).scalars().all())

    @staticmethod
    async def list_banned_users_page(page: int = 0, page_size: int = 20):
        async with db_helper.engine.connect() as conn:
            total = int(await conn.scalar(select(func.count(func.distinct(BannedUser.id_user)))) or 0)
            page = min(max(0, page), max(0, (total-1)//page_size))
            latest_username = (
                select(SenderData.username).where(SenderData.user_id == BannedUser.id_user)
                .order_by(SenderData.timestamp.desc(), SenderData.id.desc()).limit(1).scalar_subquery()
            )
            rows = (await conn.execute(
                select(BannedUser.id_user, latest_username.label("username"))
                .group_by(BannedUser.id_user).order_by(BannedUser.id_user)
                .offset(page*page_size).limit(page_size)
            )).all()
            return rows, total, page


class CrudBotsData:
    @staticmethod
    async def get_bots_info() -> list:
        async with db_helper.engine.connect() as conn:
            return (await conn.execute(select(BotsData))).fetchall()

    @staticmethod
    async def add_bots_info(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(insert(BotsData).values(data))
            await conn.commit()

    @staticmethod
    async def delete_bots_info(data: dict) -> None:
        bot_username = str(data["bot_username"]).replace("@", "")
        async with db_helper.engine.connect() as conn:
            await conn.execute(
                delete(BotsData)
                .filter(and_(
                    BotsData.channel_id == data["channel_id"],
                    BotsData.bot_username.in_([bot_username, f"@{bot_username}"]),
                ))
            )
            await conn.commit()


class CrudChatAdmins:
    @staticmethod
    async def get_chat_admins(bot: int = None, chat: int = None) -> list | int:
        async with db_helper.engine.connect() as conn:
            if bot is not None and chat is None:
                result = (
                    await conn.execute(
                        select(ChatAdmins.chat_id)
                        .filter(ChatAdmins.bot_id == bot)
                        .order_by(ChatAdmins.id.desc())
                    )
                ).scalars().all()
                return result[0] if result else None
            if bot is None and chat is None:
                return (
                    await conn.execute(
                        select(ChatAdmins.id, ChatAdmins.bot_id, ChatAdmins.chat_id)
                    )
                ).fetchall()
            elif bot is None:
                return (
                    await conn.execute(
                        select(ChatAdmins.id, ChatAdmins.bot_id, ChatAdmins.chat_id)
                        .filter(ChatAdmins.chat_id == chat)
                    )
                ).fetchall()
            else:
                return (
                    await conn.execute(
                        select(ChatAdmins.id, ChatAdmins.bot_id, ChatAdmins.chat_id)
                        .filter(and_(
                            ChatAdmins.chat_id == chat,
                            ChatAdmins.bot_id == bot
                        ))
                    )
                ).fetchall()

    @staticmethod
    async def add_chat_admins(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(insert(ChatAdmins).values(data))
            await conn.commit()

    @staticmethod
    async def get_chat_admin_rows(bot_id: int) -> list:
        async with db_helper.engine.connect() as conn:
            return (
                await conn.execute(
                    select(ChatAdmins.chat_id)
                    .filter(ChatAdmins.bot_id == bot_id)
                    .order_by(ChatAdmins.id.desc())
                )
            ).scalars().all()

    @staticmethod
    async def delete_chat_admins(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(
                delete(ChatAdmins)
                .filter(and_(
                    ChatAdmins.chat_id == data["chat_id"],
                    ChatAdmins.bot_id == data["bot_id"],
                ))
            )
            await conn.commit()


class CrudServiceMessage:
    @staticmethod
    async def get_service_message(bot_id: int) -> list:
        async with db_helper.engine.connect() as conn:
            return (await conn.execute(select(ServiceMessage).filter(ServiceMessage.bot_id == bot_id))).fetchall()

    @staticmethod
    async def add_service_message(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(insert(ServiceMessage).values(data))
            await conn.commit()

    @staticmethod
    async def update_service_message(
            bot_id: int,
            hello_message: str,
            ban_user_message: str,
            send_post_message: str
    ) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(
                update(ServiceMessage)
                .filter(ServiceMessage.bot_id == bot_id)
                .values(
                    hello_message=hello_message,
                    ban_user_message=ban_user_message,
                    send_post_message=send_post_message
                )
            )
            await conn.commit()


class CrudDelayedPosts:
    @staticmethod
    async def get_delayed_posts(bot_id=None) -> list:
        async with db_helper.engine.connect() as conn:
            if bot_id is None:
                return (await conn.execute(select(DelayedPost))).fetchall()
            else:
                return (await conn.execute(select(DelayedPost).filter(DelayedPost.bot_id == bot_id))).fetchall()

    @staticmethod
    async def add_delayed_posts(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(insert(DelayedPost).values(data))
            await conn.commit()

    @staticmethod
    async def upsert_delayed_post(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(
                delete(DelayedPost)
                .filter(and_(DelayedPost.bot_id == data["bot_id"], DelayedPost.message_id == data["message_id"]))
            )
            await conn.execute(insert(DelayedPost).values(data))
            await conn.commit()

    @staticmethod
    async def setter_post(bot_id: int, new_time_seconds: int, message_id: int) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(
                update(DelayedPost)
                .filter(and_(DelayedPost.bot_id == bot_id, DelayedPost.message_id == message_id))
                .values(
                    time_seconds=new_time_seconds,
                )
            )
            await conn.commit()

    @staticmethod
    async def delete_delayed_posts(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(
                delete(DelayedPost)
                .filter(and_(DelayedPost.bot_id == data['bot_id'], DelayedPost.message_id == data['message_id']))
            )
            await conn.commit()


class CrudAnonymMessage:
    @staticmethod
    async def get_posts(chat_id=None, message_id=None) -> list | tuple:
        async with (db_helper.engine.connect() as conn):
            if chat_id is None and message_id is None:
                return (await conn.execute(select(AnonymMessage))).fetchall()
            if chat_id is None:
                return (await conn.execute(
                    select(AnonymMessage).filter(AnonymMessage.message_id == message_id)
                )).fetchall()
            if message_id is None:
                return (await conn.execute(
                    select(AnonymMessage).filter(AnonymMessage.chat_id == chat_id)
                ))
            else:
                return (await conn.execute(
                    select(AnonymMessage)
                    .filter(and_(AnonymMessage.message_id == message_id, AnonymMessage.chat_id == chat_id))
                )).fetchall()[0]

    @staticmethod
    async def delete_posts(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(
                Delete(AnonymMessage)
                .filter(and_(
                    data["message_id"] == AnonymMessage.message_id,
                    data["chat_id"] == AnonymMessage.chat_id,
                ))
            )
            await conn.commit()

    @staticmethod
    async def add_posts(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(insert(AnonymMessage).values(data))
            await conn.commit()

    @staticmethod
    async def check_item(message_id, chat_id) -> bool:
        async with db_helper.engine.connect() as conn:
            items = await conn.execute(
                select(AnonymMessage)
                .filter(and_(AnonymMessage.message_id == message_id, AnonymMessage.chat_id == chat_id))
            )
            return bool(items.fetchall())


class CrudAdvertising:
    @staticmethod
    async def get_advertising(channel_id=None, post_id=None) -> list | tuple:
        async with db_helper.engine.connect() as conn:
            if channel_id is None and post_id is None:
                return (await conn.execute(select(Advertising))).fetchall()
            if channel_id is None:
                return (await conn.execute(
                    select(Advertising).filter(Advertising.post_id == post_id)
                )).fetchall()
            if post_id is None:
                return (await conn.execute(
                    select(Advertising).filter(Advertising.channel_id == channel_id)
                )).fetchall()
            else:
                return (await conn.execute(
                    select(Advertising)
                    .filter(and_(Advertising.post_id == post_id, Advertising.channel_id == channel_id))
                )).fetchall()

    @staticmethod
    async def add_advertising(channel_id: int, post_id: int, time: int) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(insert(Advertising).values(
                post_id=post_id,
                channel_id=channel_id,
                time=time,
            ))
            await conn.commit()

    @staticmethod
    async def delete_advertising(channel_id: int, post_id: int) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(
                delete(Advertising)
                .filter(and_(Advertising.post_id == post_id, Advertising.channel_id == channel_id))
            )
            await conn.commit()


class CrudPostData:
    def __init__(self, table):
        self.table = table

    async def get_public_posts(self) -> list:
        async with db_helper.engine.connect() as conn:
            return (await conn.execute(select(self.table))).fetchall()

    async def add_public_posts(self, data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(insert(self.table).values(data))
            await conn.commit()

    async def get_first_by_filters(self, **filters):
        async with db_helper.engine.connect() as conn:
            stmt = select(self.table)
            for key, value in filters.items():
                stmt = stmt.filter(getattr(self.table, key) == value)
            return (await conn.execute(stmt.limit(1))).scalars().first()


class CrudBotAdmins:
    @staticmethod
    async def get_admins(user_id: int = None) -> list:
        async with db_helper.engine.connect() as conn:
            if user_id is None:
                return (await conn.execute(select(BotAdmin))).fetchall()
            return (await conn.execute(select(BotAdmin).filter(BotAdmin.user_id == user_id))).fetchall()

    @staticmethod
    async def add_admin(data: dict) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(insert(BotAdmin).values(data))
            await conn.commit()

    @staticmethod
    async def delete_admin(user_id: int) -> None:
        async with db_helper.engine.connect() as conn:
            await conn.execute(delete(BotAdmin).filter(BotAdmin.user_id == user_id))
            await conn.commit()


async def main():
    db = CrudAdvertising()
    await db.delete_advertising(
        channel_id=-1003091383282,
        post_id=373,
    )

if __name__ == '__main__':
    asyncio.run(create_table())
