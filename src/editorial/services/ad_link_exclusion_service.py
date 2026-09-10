from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.editorial.models.ad_link_exclusion import AdLinkExclusion


MAX_AD_LINK_LENGTH = 2048
_TRAILING_MESSAGE_PUNCTUATION = ".,!?;:)]}>\u00bb\u201d\u2019"
_TELEGRAM_HOSTS = {"t.me", "telegram.me"}


def normalize_ad_link(value: str) -> str:
    """Return a stable comparison form for one HTTP(S) or Telegram link."""

    raw_value = str(value or "").strip()
    if not raw_value:
        raise ValueError("Ссылка не может быть пустой.")
    if len(raw_value) > MAX_AD_LINK_LENGTH:
        raise ValueError(f"Ссылка не должна быть длиннее {MAX_AD_LINK_LENGTH} символов.")
    if any(character.isspace() for character in raw_value):
        raise ValueError("Отправьте одну ссылку без пробелов.")

    raw_value = raw_value.rstrip(_TRAILING_MESSAGE_PUNCTUATION)
    if raw_value.startswith("@"):
        username = raw_value[1:].strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,}", username):
            raise ValueError("Некорректная Telegram-ссылка или имя канала.")
        raw_value = f"https://t.me/{username}"
    elif not re.match(r"^[a-z][a-z0-9+.-]*://", raw_value, flags=re.IGNORECASE):
        raw_value = f"https://{raw_value}"

    parsed = urlsplit(raw_value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Поддерживаются только ссылки http:// и https://.")
    if parsed.username or parsed.password:
        raise ValueError("Ссылки с логином или паролем не поддерживаются.")

    try:
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Некорректный адрес ссылки.") from exc
    if host.startswith("www."):
        host = host[4:]
    if not host or ("." not in host and host not in _TELEGRAM_HOSTS and host != "localhost"):
        raise ValueError("Некорректный адрес ссылки.")
    if host == "telegram.me":
        host = "t.me"

    if port is not None and not (
        parsed.scheme.lower() == "http" and port == 80
        or parsed.scheme.lower() == "https" and port == 443
    ):
        host = f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    if host == "t.me" and path:
        path_parts = path.split("/")
        if len(path_parts) > 1 and re.fullmatch(r"[A-Za-z0-9_]{5,}", path_parts[1]):
            path_parts[1] = path_parts[1].lower()
        path = "/".join(path_parts)
    normalized = urlunsplit(("https", host, path, parsed.query, "")).removeprefix("https://")
    if len(normalized) > MAX_AD_LINK_LENGTH:
        raise ValueError(f"Ссылка не должна быть длиннее {MAX_AD_LINK_LENGTH} символов.")
    return normalized


def normalized_ad_link_is_excluded(link: str, exclusions: Iterable[str]) -> bool:
    try:
        normalized_link = normalize_ad_link(link)
    except ValueError:
        return False

    normalized_exclusions: set[str] = set()
    for exclusion in exclusions:
        try:
            normalized_exclusions.add(normalize_ad_link(exclusion))
        except ValueError:
            continue
    return normalized_link in normalized_exclusions


class AdLinkExclusionService:
    async def list_exclusions(self, session: AsyncSession) -> list[AdLinkExclusion]:
        result = await session.execute(
            select(AdLinkExclusion).order_by(
                AdLinkExclusion.normalized_url.asc(),
                AdLinkExclusion.id.asc(),
            )
        )
        return list(result.scalars().all())

    async def list_normalized_links(self, session: AsyncSession) -> set[str]:
        result = await session.execute(select(AdLinkExclusion.normalized_url))
        return set(result.scalars().all())

    async def add_exclusion(
        self,
        session: AsyncSession,
        *,
        url: str,
        created_by: int | None,
    ) -> tuple[AdLinkExclusion, bool]:
        cleaned_url = str(url or "").strip()
        normalized_url = normalize_ad_link(cleaned_url)
        existing = await session.scalar(
            select(AdLinkExclusion)
            .where(AdLinkExclusion.normalized_url == normalized_url)
            .limit(1)
        )
        if existing is not None:
            return existing, False

        exclusion = AdLinkExclusion(
            url=cleaned_url,
            normalized_url=normalized_url,
            created_by=created_by,
        )
        session.add(exclusion)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(AdLinkExclusion)
                .where(AdLinkExclusion.normalized_url == normalized_url)
                .limit(1)
            )
            if existing is None:
                raise
            return existing, False
        await session.refresh(exclusion)
        return exclusion, True

    async def delete_exclusion(self, session: AsyncSession, *, url: str) -> AdLinkExclusion:
        normalized_url = normalize_ad_link(url)
        exclusion = await session.scalar(
            select(AdLinkExclusion)
            .where(AdLinkExclusion.normalized_url == normalized_url)
            .limit(1)
        )
        if exclusion is None:
            raise ValueError("Этой ссылки нет в списке исключений.")

        await session.delete(exclusion)
        await session.commit()
        return exclusion
