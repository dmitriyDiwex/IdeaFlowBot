from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.editorial.models.channel import Channel
from src.editorial.models.confession import ConfessionPasteCandidate, ConfessionPublisher
from src.editorial.models.enums import ContentFamily, PasteDeliveryMode, PasteStatus
from src.editorial.models.paste import PasteLibrary
from src.editorial.utils.text import compute_raw_text_hash, normalize_text


class ConfessionService:
    BIND_CODE_LIFETIME = timedelta(minutes=30)
    CANDIDATE_PENDING = "pending"
    CANDIDATE_APPROVED = "approved"
    CANDIDATE_REJECTED = "rejected"

    @staticmethod
    def _build_storage_paste(
        *,
        storage_chat_id: int,
        storage_message_id: int,
        content_type: str,
        body_text: str | None,
        created_by: int | None,
    ) -> PasteLibrary:
        clean_body = (body_text or "").strip()
        if not clean_body:
            clean_body = f"<{content_type}>"
        first_line = next((line.strip() for line in clean_body.splitlines() if line.strip()), clean_body)
        title = first_line[:120]
        identity = f"telegram:{int(storage_chat_id)}:{int(storage_message_id)}"
        return PasteLibrary(
            title=title,
            body_text=clean_body,
            normalized_text=normalize_text(clean_body) or identity,
            text_hash=compute_raw_text_hash(identity) or identity,
            content_family=ContentFamily.CONFESSION.value,
            delivery_mode=PasteDeliveryMode.TELEGRAM_COPY.value,
            storage_chat_id=int(storage_chat_id),
            storage_message_id=int(storage_message_id),
            storage_content_type=content_type,
            tags=[],
            primary_tag=None,
            status=PasteStatus.ACTIVE,
            global_cooldown_days=0,
            per_channel_cooldown_days=0,
            allow_all_channels=True,
            created_by=created_by,
        )

    async def _validated_publisher(
        self,
        session: AsyncSession,
        *,
        publisher_id: int,
        storage_chat_id: int,
    ) -> ConfessionPublisher:
        publisher = await session.get(ConfessionPublisher, publisher_id)
        if publisher is None or not publisher.is_active:
            raise ValueError("Саббот признавашек не активен.")
        if publisher.storage_chat_id != int(storage_chat_id):
            raise ValueError("Сообщение пришло не из подключённого чата паст.")
        return publisher

    async def _find_storage_paste(
        self,
        session: AsyncSession,
        *,
        storage_chat_id: int,
        storage_message_id: int,
    ) -> PasteLibrary | None:
        return await session.scalar(
            select(PasteLibrary)
            .where(
                PasteLibrary.storage_chat_id == int(storage_chat_id),
                PasteLibrary.storage_message_id == int(storage_message_id),
            )
            .limit(1)
        )

    async def get_active_publisher(self, session: AsyncSession) -> ConfessionPublisher | None:
        return await session.scalar(
            select(ConfessionPublisher)
            .where(ConfessionPublisher.is_active.is_(True))
            .order_by(ConfessionPublisher.updated_at.desc(), ConfessionPublisher.id.desc())
            .limit(1)
        )

    async def configure_publisher(
        self,
        session: AsyncSession,
        *,
        bot_api_token: str,
        bot_user_id: int,
        bot_username: str | None,
        created_by: int | None,
    ) -> ConfessionPublisher:
        clean_token = bot_api_token.strip()
        if not clean_token:
            raise ValueError("Пустой токен бота.")

        await session.execute(update(ConfessionPublisher).values(is_active=False))
        publisher = await session.scalar(
            select(ConfessionPublisher)
            .where(
                or_(
                    ConfessionPublisher.bot_user_id == int(bot_user_id),
                    ConfessionPublisher.bot_api_token == clean_token,
                )
            )
            .limit(1)
        )
        if publisher is None:
            publisher = ConfessionPublisher(
                bot_api_token=clean_token,
                bot_user_id=int(bot_user_id),
                created_by=created_by,
            )
            session.add(publisher)

        publisher.bot_api_token = clean_token
        publisher.bot_user_id = int(bot_user_id)
        publisher.bot_username = (bot_username or "").strip().lstrip("@") or None
        publisher.is_active = True
        publisher.bind_code = secrets.token_hex(4).upper()
        publisher.bind_expires_at = datetime.now(timezone.utc) + self.BIND_CODE_LIFETIME
        await session.commit()
        await session.refresh(publisher)
        return publisher

    async def refresh_bind_code(
        self,
        session: AsyncSession,
        publisher_id: int,
    ) -> ConfessionPublisher:
        publisher = await session.get(ConfessionPublisher, publisher_id)
        if publisher is None or not publisher.is_active:
            raise ValueError("Активный саббот признавашек не найден.")
        publisher.bind_code = secrets.token_hex(4).upper()
        publisher.bind_expires_at = datetime.now(timezone.utc) + self.BIND_CODE_LIFETIME
        await session.commit()
        await session.refresh(publisher)
        return publisher

    async def bind_storage_chat(
        self,
        session: AsyncSession,
        *,
        publisher_id: int,
        bind_code: str,
        storage_chat_id: int,
        storage_chat_title: str | None,
    ) -> ConfessionPublisher:
        publisher = await session.get(ConfessionPublisher, publisher_id)
        if publisher is None or not publisher.is_active:
            raise ValueError("Этот саббот признавашек больше не активен.")

        expires_at = publisher.bind_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if (
            not publisher.bind_code
            or publisher.bind_code.upper() != bind_code.strip().upper()
            or expires_at is None
            or expires_at <= datetime.now(timezone.utc)
        ):
            raise ValueError("Код подключения неверный или уже истёк.")

        publisher.storage_chat_id = int(storage_chat_id)
        publisher.storage_chat_title = (storage_chat_title or "").strip() or None
        publisher.bind_code = None
        publisher.bind_expires_at = None
        await session.commit()
        await session.refresh(publisher)
        return publisher

    async def create_storage_paste(
        self,
        session: AsyncSession,
        *,
        publisher_id: int,
        storage_chat_id: int,
        storage_message_id: int,
        content_type: str,
        body_text: str | None,
        created_by: int | None,
    ) -> tuple[PasteLibrary, bool]:
        await self._validated_publisher(
            session,
            publisher_id=publisher_id,
            storage_chat_id=storage_chat_id,
        )
        existing = await self._find_storage_paste(
            session,
            storage_chat_id=storage_chat_id,
            storage_message_id=storage_message_id,
        )
        if existing is not None:
            return existing, False

        paste = self._build_storage_paste(
            storage_chat_id=storage_chat_id,
            storage_message_id=storage_message_id,
            content_type=content_type,
            body_text=body_text,
            created_by=created_by,
        )
        session.add(paste)
        await session.commit()
        await session.refresh(paste)
        return paste, True

    async def create_candidate(
        self,
        session: AsyncSession,
        *,
        publisher_id: int,
        storage_chat_id: int,
        storage_message_id: int,
        content_type: str,
        body_text: str | None,
        submitted_by: int | None,
    ) -> tuple[ConfessionPasteCandidate | None, bool]:
        await self._validated_publisher(
            session,
            publisher_id=publisher_id,
            storage_chat_id=storage_chat_id,
        )
        existing_paste = await self._find_storage_paste(
            session,
            storage_chat_id=storage_chat_id,
            storage_message_id=storage_message_id,
        )
        if existing_paste is not None:
            return None, False

        existing_candidate = await session.scalar(
            select(ConfessionPasteCandidate)
            .where(
                ConfessionPasteCandidate.storage_chat_id == int(storage_chat_id),
                ConfessionPasteCandidate.storage_message_id == int(storage_message_id),
            )
            .limit(1)
        )
        if existing_candidate is not None:
            return existing_candidate, False

        candidate = ConfessionPasteCandidate(
            publisher_id=int(publisher_id),
            storage_chat_id=int(storage_chat_id),
            storage_message_id=int(storage_message_id),
            content_type=content_type,
            body_text=(body_text or "").strip() or None,
            submitted_by=submitted_by,
            status=self.CANDIDATE_PENDING,
        )
        session.add(candidate)
        await session.commit()
        await session.refresh(candidate)
        return candidate, True

    async def set_candidate_prompt_message(
        self,
        session: AsyncSession,
        *,
        candidate_id: int,
        prompt_message_id: int,
    ) -> None:
        candidate = await session.get(ConfessionPasteCandidate, candidate_id)
        if candidate is None or candidate.status != self.CANDIDATE_PENDING:
            return
        candidate.prompt_message_id = int(prompt_message_id)
        await session.commit()

    async def approve_candidate(
        self,
        session: AsyncSession,
        *,
        candidate_id: int,
        reviewed_by: int | None,
    ) -> tuple[PasteLibrary, bool]:
        candidate = await session.scalar(
            select(ConfessionPasteCandidate)
            .where(ConfessionPasteCandidate.id == int(candidate_id))
            .with_for_update()
        )
        if candidate is None:
            raise ValueError("Запрос на добавление пасты не найден.")
        if candidate.status == self.CANDIDATE_REJECTED:
            raise ValueError("Эта паста уже была отклонена.")

        existing_paste = await self._find_storage_paste(
            session,
            storage_chat_id=candidate.storage_chat_id,
            storage_message_id=candidate.storage_message_id,
        )
        if candidate.status == self.CANDIDATE_APPROVED:
            if existing_paste is None:
                raise ValueError("Паста отмечена добавленной, но запись библиотеки не найдена.")
            return existing_paste, False

        await self._validated_publisher(
            session,
            publisher_id=candidate.publisher_id,
            storage_chat_id=candidate.storage_chat_id,
        )
        created = existing_paste is None
        paste = existing_paste
        if paste is None:
            paste = self._build_storage_paste(
                storage_chat_id=candidate.storage_chat_id,
                storage_message_id=candidate.storage_message_id,
                content_type=candidate.content_type,
                body_text=candidate.body_text,
                created_by=candidate.submitted_by,
            )
            session.add(paste)
            await session.flush()

        candidate.status = self.CANDIDATE_APPROVED
        candidate.paste_id = paste.id
        candidate.reviewed_by = reviewed_by
        candidate.reviewed_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(candidate)
        await session.refresh(paste)
        return paste, created

    async def reject_candidate(
        self,
        session: AsyncSession,
        *,
        candidate_id: int,
        reviewed_by: int | None,
    ) -> tuple[ConfessionPasteCandidate, bool]:
        candidate = await session.scalar(
            select(ConfessionPasteCandidate)
            .where(ConfessionPasteCandidate.id == int(candidate_id))
            .with_for_update()
        )
        if candidate is None:
            raise ValueError("Запрос на добавление пасты не найден.")
        if candidate.status == self.CANDIDATE_APPROVED:
            raise ValueError("Эта паста уже добавлена в базу.")
        if candidate.status == self.CANDIDATE_REJECTED:
            return candidate, False

        candidate.status = self.CANDIDATE_REJECTED
        candidate.reviewed_by = reviewed_by
        candidate.reviewed_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(candidate)
        return candidate, True

    async def list_pastes(
        self,
        session: AsyncSession,
        *,
        limit: int | None = None,
    ) -> list[PasteLibrary]:
        stmt = (
            select(PasteLibrary)
            .where(
                PasteLibrary.content_family == ContentFamily.CONFESSION.value,
                PasteLibrary.status == PasteStatus.ACTIVE,
            )
            .order_by(PasteLibrary.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def list_channels(self, session: AsyncSession) -> list[Channel]:
        return list(
            (
                await session.execute(
                    select(Channel)
                    .where(
                        Channel.content_family == ContentFamily.CONFESSION.value,
                        Channel.is_active.is_(True),
                    )
                    .order_by(Channel.id.asc())
                )
            ).scalars().all()
        )

    async def ensure_channel(
        self,
        session: AsyncSession,
        *,
        tg_channel_id: int,
        title: str | None,
        username: str | None,
    ) -> Channel:
        channel = await session.scalar(
            select(Channel).where(Channel.tg_channel_id == int(tg_channel_id)).limit(1)
        )
        if channel is not None and channel.content_family != ContentFamily.CONFESSION.value:
            raise ValueError("Этот Telegram-канал уже подключён как обычная подслушка.")

        clean_username = (username or "").strip().lstrip("@")
        if channel is None:
            base_code = f"confession_{clean_username or abs(int(tg_channel_id))}"
            short_code = base_code[:120]
            suffix = 2
            while await session.scalar(select(Channel.id).where(Channel.short_code == short_code).limit(1)):
                suffix_text = f"_{suffix}"
                short_code = f"{base_code[:120 - len(suffix_text)]}{suffix_text}"
                suffix += 1
            channel = Channel(
                tg_channel_id=int(tg_channel_id),
                short_code=short_code,
                content_family=ContentFamily.CONFESSION.value,
            )
            session.add(channel)

        channel.title = (title or "").strip() or channel.title
        channel.is_active = True
        channel.content_family = ContentFamily.CONFESSION.value
        # Confession channels use the same settings/profile and slot machinery
        # as ordinary channels. Content-family filtering in PasteService keeps
        # the two paste libraries isolated without a second settings system.
        if channel.settings_profile_id is None:
            channel.settings_profile_auto_enabled = True
        await session.commit()
        await session.refresh(channel)
        return channel
