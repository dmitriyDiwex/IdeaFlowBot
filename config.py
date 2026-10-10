from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re


def _split_int_collection(raw: str) -> list[int]:
    if not raw:
        return []
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def _split_str_collection(raw: str) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _split_env_usernames(raw: str) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in re.split(r"[\s,;]+", raw) if item.strip()]


def _load_repeated_env_values(name: str) -> list[str]:
    values: list[str] = []
    env_file = Path(".env")
    if not env_file.exists():
        return values

    prefix = f"{name}="
    for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.startswith(prefix):
            continue
        values.extend(_split_env_usernames(stripped[len(prefix):]))
    return values


def _load_enabled_subbot_usernames() -> list[str]:
    values = _split_env_usernames(os.getenv("ENABLED_SUBBOT_USERNAMES", ""))
    values.extend(_load_repeated_env_values("ENABLED_SUBBOT_USERNAMES"))
    return _unique_preserve_order(values)


def _optional_int(raw: str) -> int | None:
    value = (raw or "").strip()
    if not value:
        return None
    return int(value)


@dataclass
class Settings:
    api_token_bot: str = os.getenv("BOT_API_TOKEN", "")
    general_admin: int = int(os.getenv("GENERAL_ADMIN", "0"))
    moderators: set[int] = field(default_factory=lambda: set(_split_int_collection(os.getenv("MODERATORS", ""))))
    hello_msg: str = os.getenv(
        "HELLO_MSG",
        "Здравствуйте!\n\nНапишите ваш вопрос или историю, и мы отправим ее на модерацию.",
    )
    ban_msg: str = os.getenv(
        "BAN_MSG",
        "К сожалению, модератор ограничил для вас отправку новых сообщений в эту предложку.",
    )
    send_post_msg: str = os.getenv(
        "SEND_POST_MSG",
        "Спасибо за сообщение. Если модератор его одобрит, мы опубликуем его позже.",
    )
    logging_path: str = os.getenv("LOGGING_PATH", "logs/bot.log")
    statistics_google_spreadsheet_id: str = os.getenv("STATISTICS_GOOGLE_SPREADSHEET_ID", "").strip()
    statistics_google_credentials_file: str = os.getenv("STATISTICS_GOOGLE_CREDENTIALS_FILE", "").strip()
    const_time_sleep: float = float(os.getenv("CONST_TIME_SLEEP", "30"))
    proxy_user: str = os.getenv("PROXY_USER", "")
    proxy_password: str = os.getenv("PROXY_PASSWORD", "")
    proxy_host_port: str = os.getenv("PROXY_HOST_PORT", "")
    shift_time_seconds: int = int(os.getenv("SHIFT_TIME_SECONDS", "3600"))
    sup_bot_limit: int = int(os.getenv("SUP_BOT_LIMIT", "20"))
    max_subbots: int = int(os.getenv("MAX_SUBBOTS", "200"))
    telegram_connections_per_bot: int = int(os.getenv("TELEGRAM_CONNECTIONS_PER_BOT", "4"))
    telegram_connection_overhead: int = int(os.getenv("TELEGRAM_CONNECTION_OVERHEAD", "20"))
    telegram_request_timeout_seconds: int = max(
        1,
        int(os.getenv("TELEGRAM_REQUEST_TIMEOUT_SECONDS", "15")),
    )
    telegram_retry_delay_seconds: int = max(
        1,
        int(os.getenv("TELEGRAM_RETRY_DELAY_SECONDS", "60")),
    )
    enabled_subbot_usernames: list[str] = field(
        default_factory=_load_enabled_subbot_usernames
    )
    media_preview_max_mb: int = int(os.getenv("MEDIA_PREVIEW_MAX_MB", "20"))
    advertiser: list[int] = field(default_factory=lambda: _split_int_collection(os.getenv("ADVERTISER_IDS", "")))
    advertising_manager_username: str = os.getenv("ADVERTISING_MANAGER_USERNAME", "@ivanblk")
    advertising_manager_chat_id: int | None = _optional_int(os.getenv("ADVERTISING_MANAGER_CHAT_ID", ""))
    advertising_bot_token: str = os.getenv(
        "ADVERTISING_BOT_TOKEN",
        "8150027786:AAFvsKzexPaJ6YCEWlHkKgoFtv3giN7rubk",
    )
    advertising_text: str = os.getenv(
        "ADVERTISING_TEXT",
        "Спасибо! Ваш запрос передан рекламному менеджеру. С вами свяжутся отдельно.",
    )

    @property
    def proxies(self) -> dict[str, str | None]:
        if self.proxy_host_port and self.proxy_user and self.proxy_password:
            proxy = f"http://{self.proxy_user}:{self.proxy_password}@{self.proxy_host_port}"
        elif self.proxy_host_port:
            proxy = f"http://{self.proxy_host_port}"
        else:
            proxy = None
        return {
            "http": proxy,
            "https": proxy,
        }


settings = Settings()
