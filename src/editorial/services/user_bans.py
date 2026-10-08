from __future__ import annotations

from dataclasses import dataclass
import re

from src.core_database.database import CrudBannedUser


@dataclass(frozen=True, slots=True)
class BanUserIdentity:
    user_id: int
    username: str | None

    @property
    def label(self) -> str:
        return f"@{self.username or 'None'} | TG ID: {self.user_id}"


def known_username(value: str | None) -> str | None:
    username = (value or "").strip().lstrip("@")
    return None if not username or username.lower() == "none" else username


class UserBanService:
    def __init__(self, repository: CrudBannedUser | None = None):
        self.repository = repository or CrudBannedUser()

    async def resolve_user(self, value: str) -> BanUserIdentity:
        value = value.strip()
        if re.fullmatch(r"[0-9]+", value):
            if len(value) > 19 or not 0 < int(value) <= 2**63-1:
                raise ValueError("Telegram ID должен быть положительным числом допустимого размера.")
            user_id = int(value)
        else:
            username = value.removeprefix("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username) or username.lower() == "none":
                raise ValueError("Введите @username или положительный Telegram ID пользователя.")
            ids = await self.repository.find_user_ids_by_username(username)
            if not ids:
                raise ValueError("Username не найден среди последних данных авторов предложек. Введите Telegram ID.")
            if len(ids) != 1:
                raise ValueError("С этим username найдено несколько Telegram ID. Введите нужный ID из списка банов или карточки сообщения.")
            user_id = ids[0]
        username = known_username(await self.repository.get_last_username(user_id))
        return BanUserIdentity(user_id, username)

    async def ban(self, user_id: int) -> bool:
        return await self.repository.add_global_ban(user_id)

    async def unban(self, user_id: int) -> int:
        return await self.repository.delete_all_user_bans(user_id)

    async def list_users(self, page: int = 0, page_size: int = 20):
        rows, total, page = await self.repository.list_banned_users_page(page, page_size)
        return [BanUserIdentity(row.id_user, known_username(row.username)) for row in rows], total, page
