from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import timedelta
from typing import TypeVar

import aiohttp

from src.editorial.config import settings


T = TypeVar("T")


class TelegramOperationTimeout(TimeoutError):
    """A bounded Telegram operation exceeded the application's wall-clock limit."""


class PartialTelegramCopyError(RuntimeError):
    """Some destination messages exist; retrying the album would duplicate them."""

    def __init__(self, expected_count: int, copied_message_ids: list[int]) -> None:
        self.copied_message_ids = copied_message_ids
        super().__init__(
            f"Telegram copied {len(copied_message_ids)} of {expected_count} album messages; "
            f"destination IDs: {copied_message_ids}. Automatic retry is disabled."
        )


def validate_album_copy(expected_count: int, copied_message_ids: list[int]) -> None:
    if not copied_message_ids:
        raise RuntimeError("Telegram returned no copied media group messages")
    if len(copied_message_ids) != expected_count or len(set(copied_message_ids)) != expected_count:
        raise PartialTelegramCopyError(expected_count, copied_message_ids)


class TelegramAPIError(RuntimeError):
    """Telegram Bot API returned a structured non-success response."""

    def __init__(self, result_json: dict) -> None:
        self.result_json = result_json
        self.error_code = result_json.get("error_code")
        super().__init__(result_json.get("description") or str(result_json))


async def run_telegram_operation(
    awaitable: Awaitable[T],
    *,
    operation: str,
    timeout_seconds: int | None = None,
) -> T:
    timeout = timeout_seconds or settings.telegram_request_timeout_seconds
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as exc:
        raise TelegramOperationTimeout(
            f"Telegram {operation} exceeded {timeout} seconds"
        ) from exc


def _exception_chain(exc: BaseException):
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_transient_telegram_error(exc: BaseException) -> bool:
    transient_names = {
        "ClientConnectorError",
        "ClientOSError",
        "ConnectionResetError",
        "RequestTimeout",
        "ServerDisconnectedError",
        "TelegramOperationTimeout",
    }
    transient_markers = (
        "429",
        "500 internal server error",
        "502 bad gateway",
        "503 service unavailable",
        "504 gateway timeout",
        "connection reset",
        "connection refused",
        "network is unreachable",
        "request timeout",
        "server disconnected",
        "temporary failure",
        "timed out",
        "too many requests",
    )

    for item in _exception_chain(exc):
        if isinstance(item, (TelegramOperationTimeout, asyncio.TimeoutError, aiohttp.ClientError)):
            return True
        if item.__class__.__name__ in transient_names:
            return True
        error_code = getattr(item, "error_code", None)
        if error_code == 429 or (isinstance(error_code, int) and error_code >= 500):
            return True
        text_value = str(item).lower()
        if any(marker in text_value for marker in transient_markers):
            return True
    return False


def is_telegram_message_not_modified(exc: BaseException) -> bool:
    """Return whether Telegram rejected an idempotent message edit."""
    for item in _exception_chain(exc):
        if "message is not modified" in str(item).lower():
            return True
    return False


def telegram_retry_after_seconds(exc: BaseException) -> int | None:
    for item in _exception_chain(exc):
        result_json = getattr(item, "result_json", None)
        if not isinstance(result_json, dict):
            continue
        parameters = result_json.get("parameters")
        if not isinstance(parameters, dict):
            continue
        retry_after = parameters.get("retry_after")
        try:
            return max(1, int(retry_after))
        except (TypeError, ValueError):
            continue
    return None


def publisher_retry_delay(attempt_count: int, exc: BaseException) -> timedelta:
    exponent = min(max(attempt_count - 1, 0), 4)
    seconds = min(
        settings.publisher_retry_base_seconds * (2**exponent),
        settings.publisher_retry_max_seconds,
    )
    telegram_delay = telegram_retry_after_seconds(exc)
    if telegram_delay is not None:
        seconds = max(seconds, telegram_delay)
    return timedelta(seconds=seconds)
