from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.editorial.config import settings
from src.editorial.models.ad_blackout import ChannelAdBlackout
from src.editorial.models.channel import Channel
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import (
    ContentFamily,
    ContentItemStatus,
    ContentSourceType,
    PasteDeliveryMode,
    PublicationStatus,
)
from src.editorial.models.paste import PasteLibrary
from src.editorial.models.publication import PublicationLog
from src.editorial.models.submission import Submission
from src.editorial.services.legacy_audit import LEGACY_DELAYED_AUDIT_TEMPLATE_KEY
from src.editorial.services.legacy_source import LegacyCollectorReader, LegacySenderRow
from src.legacy_publication_status import LegacyPublicationStatusService
from src.editorial.services.paste_service import PasteService
from src.editorial.services.confession_service import ConfessionService
from src.editorial.services.publication_signature import (
    channel_publication_signature_html,
    format_publication_html,
    publication_signature_enabled,
    should_add_publication_signature,
)
from src.editorial.services.scheduler import SchedulerService
from src.editorial.services.telegram_publisher import TelegramPublisherAdapter
from src.editorial.services.telegram_resilience import (
    is_transient_telegram_error,
    publisher_retry_delay,
)
from src.editorial.utils.text import clean_text


TELEGRAM_MEDIA_CAPTION_MAX_LENGTH = 1024


@dataclass(slots=True)
class PublisherRunResult:
    attempted: int = 0
    sent: int = 0
    deferred: int = 0
    failed: int = 0


@dataclass(slots=True)
class ChannelPublicationSignature:
    title: str | None
    ref: str | None


class PublisherService:
    def __init__(
        self,
        telegram_adapter: TelegramPublisherAdapter | None = None,
        legacy_reader: LegacyCollectorReader | None = None,
        paste_service: PasteService | None = None,
        legacy_publication_status: LegacyPublicationStatusService | None = None,
        confession_service: ConfessionService | None = None,
        scheduler: SchedulerService | None = None,
    ) -> None:
        self.telegram_adapter = telegram_adapter or TelegramPublisherAdapter()
        self.legacy_reader = legacy_reader or LegacyCollectorReader()
        self.paste_service = paste_service or PasteService()
        self.confession_service = confession_service or ConfessionService()
        self.scheduler = scheduler or SchedulerService()
        self.legacy_publication_status = (
            legacy_publication_status or LegacyPublicationStatusService(self.legacy_reader)
        )
        self._channel_signature_cache: dict[tuple[str, int], ChannelPublicationSignature] = {}

    @staticmethod
    def _submission_author_signature(submission: Submission) -> str:
        return f"@{submission.username}" if submission.username else "@None"

    @staticmethod
    def _channel_signature(channel: Channel, resolved_tag: str | None = None) -> str:
        if resolved_tag:
            return resolved_tag
        short_code = (channel.short_code or "").strip()
        if short_code:
            return short_code if short_code.startswith("@") else f"@{short_code}"
        return f"channel {channel.id}"

    async def resolve_channel_signature(self, bot_token: str, channel: Channel) -> str:
        try:
            resolved_tag = await self.telegram_adapter.get_chat_tag(
                bot_token=bot_token,
                channel_id=channel.tg_channel_id,
            )
        except Exception as ex:
            if is_transient_telegram_error(ex):
                raise
            logger.warning("Failed to resolve Telegram username for channel {}: {}", channel.id, ex)
            resolved_tag = None
        return self._channel_signature(channel, resolved_tag)

    async def resolve_channel_publication_signature(
        self,
        bot_token: str,
        channel: Channel,
    ) -> ChannelPublicationSignature:
        cache_key = (bot_token.split(":", 1)[0], int(channel.tg_channel_id))
        cached = self._channel_signature_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            chat_info = await self.telegram_adapter.get_chat_info(
                bot_token=bot_token,
                channel_id=channel.tg_channel_id,
            )
        except Exception as ex:
            if is_transient_telegram_error(ex):
                raise
            logger.warning("Failed to resolve Telegram chat info for channel {}: {}", channel.id, ex)
            return ChannelPublicationSignature(
                title=channel.title,
                ref=self._channel_signature(channel, None),
            )
        resolved = ChannelPublicationSignature(
            title=chat_info.title or channel.title,
            ref=self._channel_signature(channel, chat_info.tag or chat_info.invite_link),
        )
        self._channel_signature_cache[cache_key] = resolved
        return resolved

    @staticmethod
    def publication_parse_mode() -> str | None:
        return "HTML" if publication_signature_enabled() else None

    @staticmethod
    def channel_signature_parse_mode(add_channel_signature: bool) -> str | None:
        return "HTML" if add_channel_signature else None

    async def should_add_channel_signature(self, bot_token: str, channel: Channel) -> bool:
        return await should_add_publication_signature(
            bot_token=bot_token,
            channel_id=int(channel.tg_channel_id),
        )

    def format_publication_text(
        self,
        text: str | None,
        channel: Channel,
        submission: Submission | None = None,
        channel_signature: str | None = None,
        channel_title: str | None = None,
        add_channel_signature: bool = True,
    ) -> str:
        parts: list[str] = []
        cleaned_text = (text or "").strip()
        if cleaned_text:
            parts.append(cleaned_text)
        if submission is not None and not submission.is_anonymous:
            parts.append(self._submission_author_signature(submission))
        if not publication_signature_enabled() or not add_channel_signature:
            return "\n\n".join(parts)
        signature_html = channel_publication_signature_html(
            channel,
            channel_signature,
            title=channel_title,
        )
        return format_publication_html(
            "\n\n".join(parts),
            signature_html=signature_html,
        )

    @staticmethod
    def _can_copy_submission_verbatim(
        submission: Submission,
        content_item: ContentItem,
        source_text: str,
    ) -> bool:
        if submission.source_chat_id is None or submission.source_message_id is None:
            return False
        source_text_clean = clean_text(source_text)
        body_text_clean = clean_text(content_item.body_text or "")

        if submission.content_type == "text":
            return bool(source_text_clean) and source_text_clean == body_text_clean

        if submission.content_type in {"photo", "video", "animation"}:
            if source_text_clean:
                return source_text_clean == body_text_clean
            body_text = (content_item.body_text or "").strip()
            return not body_text or (body_text.startswith("<") and body_text.endswith(">"))

        return False

    @staticmethod
    def _get_related_submission_source_text(related_rows: list[Submission]) -> str:
        for item in related_rows:
            text_value = (item.cleaned_text or item.raw_text or "").strip()
            if text_value:
                return text_value
        return ""

    async def _get_related_legacy_rows(self, related_rows: list[Submission]) -> list[LegacySenderRow]:
        legacy_row_ids = [item.legacy_row_id for item in related_rows if item.legacy_row_id is not None]
        if not legacy_row_ids:
            return []
        legacy_rows = await self.legacy_reader.fetch_sender_rows_by_ids(legacy_row_ids)
        row_map = {row.id: row for row in legacy_rows}
        return [row_map[item.legacy_row_id] for item in related_rows if item.legacy_row_id in row_map]

    @staticmethod
    def _pick_single_copy_source(
        submission: Submission,
        legacy_row: LegacySenderRow | None,
    ) -> tuple[int, int]:
        if legacy_row and legacy_row.review_chat_id is not None and legacy_row.review_message_id is not None:
            return int(legacy_row.review_chat_id), int(legacy_row.review_message_id)
        if submission.source_chat_id is None or submission.source_message_id is None:
            raise ValueError("Submission has no source chat or message id")
        return int(submission.source_chat_id), int(submission.source_message_id)

    async def _publish_submission_based_item(
        self,
        session: AsyncSession,
        content_item: ContentItem,
        channel: Channel,
        bot_token: str,
    ) -> int:
        origin_paste_id = getattr(content_item, "origin_paste_id", None)
        if origin_paste_id is not None:
            paste = await session.get(PasteLibrary, origin_paste_id)
            if paste is not None and paste.delivery_mode == PasteDeliveryMode.TELEGRAM_COPY.value:
                if paste.storage_chat_id is None or paste.storage_message_id is None:
                    raise ValueError(f"Confession paste {paste.id} has no storage message reference")
                return await self.telegram_adapter.copy_message(
                    bot_token=bot_token,
                    channel_id=channel.tg_channel_id,
                    from_chat_id=int(paste.storage_chat_id),
                    message_id=int(paste.storage_message_id),
                )

        add_channel_signature = await self.should_add_channel_signature(bot_token, channel)
        channel_publication_signature = (
            await self.resolve_channel_publication_signature(bot_token, channel)
            if add_channel_signature
            else None
        )
        channel_signature = channel_publication_signature.ref if channel_publication_signature else None
        channel_title = channel_publication_signature.title if channel_publication_signature else None
        parse_mode = self.channel_signature_parse_mode(add_channel_signature)
        if content_item.origin_submission_id is None:
            return await self.telegram_adapter.send_text(
                bot_token=bot_token,
                channel_id=channel.tg_channel_id,
                text=self.format_publication_text(
                    content_item.body_text,
                    channel,
                    channel_signature=channel_signature,
                    channel_title=channel_title,
                    add_channel_signature=add_channel_signature,
                ),
                parse_mode=parse_mode,
                disable_web_page_preview=add_channel_signature,
            )

        submission = await session.get(Submission, content_item.origin_submission_id)
        if submission is None:
            raise ValueError(f"Submission {content_item.origin_submission_id} not found")

        related_rows = [submission]
        related_legacy_rows = await self._get_related_legacy_rows(related_rows)

        if submission.media_group_id:
            stmt = (
                select(Submission)
                .where(
                    Submission.channel_id == submission.channel_id,
                    Submission.media_group_id == submission.media_group_id,
                )
                .order_by(Submission.source_message_id.asc(), Submission.id.asc())
            )
            if submission.source_chat_id is not None:
                stmt = stmt.where(Submission.source_chat_id == submission.source_chat_id)
            related_rows = list(((await session.execute(stmt)).scalars().all()))
            related_legacy_rows = await self._get_related_legacy_rows(related_rows)
            review_rows = [
                row for row in related_legacy_rows
                if row.review_chat_id is not None and row.review_message_id is not None
            ]
            source_message_ids = [
                int(item.source_message_id)
                for item in related_rows
                if item.source_message_id is not None
            ]
            review_message_ids = [int(row.review_message_id) for row in review_rows]
            if (
                submission.source_chat_id is not None
                and len(source_message_ids) == len(related_rows)
            ):
                from_chat_id = int(submission.source_chat_id)
                message_ids = source_message_ids
            elif (
                len(review_rows) == len(related_rows)
                and len({row.review_chat_id for row in review_rows}) == 1
                and len(set(review_message_ids)) == len(review_message_ids)
            ):
                from_chat_id = int(review_rows[0].review_chat_id)
                message_ids = review_message_ids
            else:
                if submission.source_chat_id is None:
                    raise ValueError("Media group submission has no source chat or message ids")
                raise ValueError("Media group submission has incomplete source message ids")
            source_text = self._get_related_submission_source_text(related_rows)
            formatted_caption = self.format_publication_text(
                source_text,
                channel,
                submission,
                channel_signature=channel_signature,
                channel_title=channel_title,
                add_channel_signature=add_channel_signature,
            )
            caption_too_long = len(formatted_caption) > TELEGRAM_MEDIA_CAPTION_MAX_LENGTH
            if caption_too_long:
                logger.warning(
                    "Will preserve the original caption for media group content item {}: "
                    "formatted caption length {} exceeds Telegram limit {}",
                    content_item.id,
                    len(formatted_caption),
                    TELEGRAM_MEDIA_CAPTION_MAX_LENGTH,
                )
            copied_message_ids = await self.telegram_adapter.copy_messages(
                bot_token=bot_token,
                channel_id=channel.tg_channel_id,
                from_chat_id=from_chat_id,
                message_ids=message_ids,
            )
            if not copied_message_ids:
                raise RuntimeError("Telegram returned no copied media group messages")

            if formatted_caption != source_text and not caption_too_long:
                caption_index = next(
                    (
                        index for index, item in enumerate(related_rows)
                        if (item.cleaned_text or item.raw_text or "").strip()
                    ),
                    0,
                )
                caption_index = min(caption_index, len(copied_message_ids) - 1)
                try:
                    await self.telegram_adapter.edit_message_caption(
                        bot_token=bot_token,
                        channel_id=channel.tg_channel_id,
                        message_id=copied_message_ids[caption_index],
                        caption=formatted_caption,
                        parse_mode=parse_mode,
                    )
                except Exception as ex:
                    # copyMessages has already created the album in Telegram. Retrying
                    # the publication after a caption-only failure would duplicate it.
                    logger.error(
                        "Copied media group content item {}, but failed to update its caption; "
                        "treating the copy as sent to prevent duplicate publication: {}",
                        content_item.id,
                        ex,
                    )
            return copied_message_ids[0]

        if submission.content_type in {"photo", "video", "animation"}:
            source_text = self._get_related_submission_source_text(related_rows)
            if self._can_copy_submission_verbatim(submission, content_item, source_text):
                from_chat_id, message_id = self._pick_single_copy_source(
                    submission=submission,
                    legacy_row=related_legacy_rows[0] if related_legacy_rows else None,
                )
                telegram_message_id = await self.telegram_adapter.copy_message(
                    bot_token=bot_token,
                    channel_id=channel.tg_channel_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    caption=self.format_publication_text(
                        source_text,
                        channel,
                        submission,
                        channel_signature=channel_signature,
                        channel_title=channel_title,
                        add_channel_signature=add_channel_signature,
                    ),
                    parse_mode=parse_mode,
                )
                logger.info(
                    "Published content item {} via copy_message from {}:{}",
                    content_item.id,
                    from_chat_id,
                    message_id,
                )
                return telegram_message_id
            caption_text = content_item.body_text.strip()
            if not source_text and caption_text.startswith("<") and caption_text.endswith(">"):
                caption_text = ""
            if submission.source_chat_id is None or submission.source_message_id is None:
                raise ValueError("Media submission has no source chat or message id")
            return await self.telegram_adapter.copy_message(
                bot_token=bot_token,
                channel_id=channel.tg_channel_id,
                from_chat_id=int(submission.source_chat_id),
                message_id=int(submission.source_message_id),
                caption=self.format_publication_text(
                    caption_text,
                    channel,
                    submission,
                    channel_signature=channel_signature,
                    channel_title=channel_title,
                    add_channel_signature=add_channel_signature,
                ),
                parse_mode=parse_mode,
            )

        source_text = self._get_related_submission_source_text(related_rows)
        if self._can_copy_submission_verbatim(submission, content_item, source_text):
            legacy_row = related_legacy_rows[0] if related_legacy_rows else None
            source_text_value = (legacy_row.text_post if legacy_row and legacy_row.text_post is not None else source_text)
            text_to_send = self.format_publication_text(
                source_text_value,
                channel,
                submission,
                channel_signature=channel_signature,
                channel_title=channel_title,
                add_channel_signature=add_channel_signature,
            )
            telegram_message_id = await self.telegram_adapter.send_text(
                bot_token=bot_token,
                channel_id=channel.tg_channel_id,
                text=text_to_send,
                parse_mode=parse_mode,
                disable_web_page_preview=add_channel_signature,
            )
            logger.info("Published content item {} via send_text from source text", content_item.id)
            return telegram_message_id

        telegram_message_id = await self.telegram_adapter.send_text(
            bot_token=bot_token,
            channel_id=channel.tg_channel_id,
            text=self.format_publication_text(
                content_item.body_text,
                channel,
                submission,
                channel_signature=channel_signature,
                channel_title=channel_title,
                add_channel_signature=add_channel_signature,
            ),
            parse_mode=parse_mode,
            disable_web_page_preview=add_channel_signature,
        )
        logger.info(
            "Published content item {} via plain send_text fallback",
            content_item.id,
        )
        return telegram_message_id

    async def _get_channel_ad_blackout(
        self,
        session: AsyncSession,
        channel_id: int,
        when: datetime,
    ) -> ChannelAdBlackout | None:
        when_utc = when.astimezone(timezone.utc)
        return await session.scalar(
            select(ChannelAdBlackout)
            .where(
                ChannelAdBlackout.channel_id == channel_id,
                ChannelAdBlackout.starts_at <= when_utc,
                ChannelAdBlackout.ends_at > when_utc,
            )
            .order_by(ChannelAdBlackout.ends_at.desc())
            .limit(1)
        )

    async def _resolve_publication_bot_token(
        self,
        session: AsyncSession,
        content_item: ContentItem,
        channel: Channel,
    ) -> str:
        origin_paste_id = getattr(content_item, "origin_paste_id", None)
        if origin_paste_id is not None:
            paste = await session.get(PasteLibrary, origin_paste_id)
            if paste is not None and paste.delivery_mode == PasteDeliveryMode.TELEGRAM_COPY.value:
                publisher = await self.confession_service.get_active_publisher(session)
                if publisher is None:
                    raise ValueError("Active confession publisher bot not found")
                if publisher.storage_chat_id != paste.storage_chat_id:
                    raise ValueError("Confession paste belongs to a different storage chat")
                return publisher.bot_api_token

        if getattr(channel, "content_family", None) == ContentFamily.CONFESSION.value:
            raise ValueError("Confession channel can only publish Telegram-copy confession pastes")

        binding = await self.legacy_reader.get_bot_binding(channel.tg_channel_id)
        if binding is None:
            raise ValueError(f"Legacy bot binding for channel {channel.tg_channel_id} not found")
        return binding.bot_api_token

    @staticmethod
    def defer_transient_publication(
        log_item: PublicationLog,
        content_item: ContentItem,
        exc: BaseException,
        attempted_at: datetime,
    ) -> None:
        delay = publisher_retry_delay(log_item.attempt_count, exc)
        log_item.publish_status = PublicationStatus.SCHEDULED
        log_item.retry_after = attempted_at + delay
        log_item.error_text = f"Transient Telegram error; retry scheduled: {exc}"[:4000]
        content_item.status = ContentItemStatus.SCHEDULED
        content_item.scheduled_for = log_item.scheduled_for

    async def run(
        self,
        session: AsyncSession,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> PublisherRunResult:
        now = now or datetime.now(timezone.utc)
        result = PublisherRunResult()
        run_started = perf_counter()
        batch_size = limit or settings.publisher_batch_size

        for _ in range(batch_size):
            current_time = now
            log_item = await session.scalar(
                select(PublicationLog)
                .where(
                    PublicationLog.publish_status == PublicationStatus.SCHEDULED,
                    PublicationLog.scheduled_for <= current_time,
                    or_(
                        PublicationLog.retry_after.is_(None),
                        PublicationLog.retry_after <= current_time,
                    ),
                    PublicationLog.content_item_id.not_in(
                        select(ContentItem.id).where(
                            ContentItem.template_key == LEGACY_DELAYED_AUDIT_TEMPLATE_KEY
                        )
                    ),
                )
                .order_by(PublicationLog.scheduled_for.asc(), PublicationLog.id.asc())
                .limit(1)
                # Claim only one row while Telegram is called. A slow channel no
                # longer locks an entire publisher batch.
                .with_for_update(skip_locked=True)
            )
            if log_item is None:
                break

            content_item = await session.get(ContentItem, log_item.content_item_id)
            channel = await session.get(Channel, log_item.channel_id)
            if content_item is None or channel is None:
                result.attempted += 1
                log_item.publish_status = PublicationStatus.FAILED
                log_item.error_text = "Missing content item or channel"
                result.failed += 1
                await session.commit()
                continue

            if not channel.is_active:
                result.attempted += 1
                log_item.publish_status = PublicationStatus.CANCELLED
                log_item.error_text = "Channel is inactive or unlinked"
                if content_item.status == ContentItemStatus.SCHEDULED:
                    content_item.status = ContentItemStatus.APPROVED
                    content_item.scheduled_for = None
                logger.info(
                    "Cancelled publication log {} for inactive channel {}",
                    log_item.id,
                    channel.id,
                )
                await session.commit()
                continue

            blackout = await self._get_channel_ad_blackout(session, channel.id, current_time)
            if blackout is not None:
                log_item.retry_after = blackout.ends_at
                log_item.error_text = f"Deferred by ad blackout until {blackout.ends_at.isoformat()}"
                result.deferred += 1
                logger.info(
                    "Deferred publication log {} for channel {} until ad blackout ends at {}",
                    log_item.id,
                    channel.id,
                    blackout.ends_at,
                )
                await session.commit()
                continue

            if content_item.source_type == ContentSourceType.PASTE:
                content_item = await self.scheduler.replace_scheduled_paste_with_live_candidate(
                    session,
                    log_item=log_item,
                    scheduled_item=content_item,
                    channel=channel,
                    eligible_at=current_time,
                )

            result.attempted += 1
            log_item.attempt_count = int(log_item.attempt_count or 0) + 1
            log_item.last_attempt_at = current_time

            published_content_item_id: int | None = None
            operation_started = perf_counter()
            try:
                bot_token = await self._resolve_publication_bot_token(session, content_item, channel)
                telegram_message_id = await self._publish_submission_based_item(
                    session=session,
                    content_item=content_item,
                    channel=channel,
                    bot_token=bot_token,
                )
                log_item.telegram_message_id = telegram_message_id
                log_item.publish_status = PublicationStatus.SENT
                log_item.published_at = current_time
                log_item.retry_after = None
                log_item.error_text = None
                content_item.status = ContentItemStatus.PUBLISHED
                if content_item.origin_paste_id is not None:
                    await self.paste_service.register_usage(
                        session=session,
                        paste_id=content_item.origin_paste_id,
                        channel_id=channel.id,
                        content_item_id=content_item.id,
                    )
                result.sent += 1
                published_content_item_id = int(content_item.id)
            except Exception as ex:
                if is_transient_telegram_error(ex):
                    self.defer_transient_publication(
                        log_item=log_item,
                        content_item=content_item,
                        exc=ex,
                        attempted_at=current_time,
                    )
                    result.deferred += 1
                    await session.commit()
                    logger.warning(
                        "Deferred publication log {} after transient Telegram error in {:.2f}s; retry after {}: {}",
                        log_item.id,
                        perf_counter() - operation_started,
                        log_item.retry_after,
                        ex,
                    )
                    # Treat the first transport failure as a circuit breaker for
                    # this run. The next container pass will continue with other
                    # due rows while this one observes its retry_after value.
                    break

                logger.exception("Failed to publish content item {}", content_item.id)
                log_item.publish_status = PublicationStatus.FAILED
                log_item.retry_after = None
                log_item.error_text = str(ex)[:4000]
                content_item.status = ContentItemStatus.HOLD
                content_item.scheduled_for = None
                result.failed += 1

            await session.commit()
            if published_content_item_id is not None:
                try:
                    await self.legacy_publication_status.mark_content_item_published(
                        published_content_item_id
                    )
                except Exception as ex:
                    logger.error(
                        "Published content item {}, but failed to update its legacy review status: {}",
                        published_content_item_id,
                        ex,
                    )

        reconcile_legacy_statuses = getattr(
            self.legacy_publication_status,
            "reconcile_published_review_statuses",
            None,
        )
        if reconcile_legacy_statuses is not None:
            try:
                await reconcile_legacy_statuses(limit=20)
            except Exception as ex:
                logger.error("Failed to reconcile legacy publication statuses: {}", ex)

        logger.info(
            "Publisher run completed in {:.2f}s: attempted={}, sent={}, deferred={}, failed={}",
            perf_counter() - run_started,
            result.attempted,
            result.sent,
            result.deferred,
            result.failed,
        )
        return result
