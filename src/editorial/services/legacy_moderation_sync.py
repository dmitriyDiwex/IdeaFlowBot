from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import or_, select
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.markups import (
    build_advertising_status_markup,
    build_rejection_status_markup,
    build_slot_status_markup,
)
from src.editorial.db.session import session_factory
from src.editorial.models.channel import Channel
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import ContentItemStatus, PublicationStatus, ReviewDecision, SubmissionStatus
from src.editorial.models.publication import PublicationLog
from src.editorial.models.submission import Submission
from src.editorial.services.import_legacy import LegacyImporter
from src.editorial.services.legacy_audit import (
    LEGACY_DELAYED_AUDIT_LOG_MARKER,
    LEGACY_DELAYED_AUDIT_TEMPLATE_KEY,
)
from src.editorial.services.legacy_source import LegacyCollectorReader
from src.editorial.services.moderation import ModerationService
from src.editorial.services.moderation_case_service import ModerationCaseService


LEGACY_DELAYED_DELIVERY_CLAIM_MARKER = "Legacy delayed delivery claimed"


class LegacyModerationSyncService:
    def __init__(
        self,
        legacy_reader: LegacyCollectorReader | None = None,
        importer: LegacyImporter | None = None,
        moderation: ModerationService | None = None,
    ) -> None:
        self.legacy_reader = legacy_reader or LegacyCollectorReader()
        self.importer = importer or LegacyImporter(self.legacy_reader)
        self.moderation = moderation or ModerationService()
        self.moderation_cases = ModerationCaseService()

    async def mark_panel_submission_approved(self, submission_id: int) -> int:
        return await self._sync_panel_review_markup(submission_id, state="approved")

    async def mark_panel_submission_rejected(self, submission_id: int) -> int:
        return await self._sync_panel_review_markup(submission_id, state="rejected")

    async def mark_panel_submission_agent_approved(self, submission_id: int) -> int:
        return await self._sync_panel_review_markup(
            submission_id,
            state="approved",
            moderator_label="agent",
            allow_cancel=True,
        )

    async def mark_panel_submission_agent_rejected(self, submission_id: int) -> int:
        return await self._sync_panel_review_markup(
            submission_id,
            state="rejected",
            moderator_label="agent",
            allow_cancel=True,
        )

    async def mark_panel_submission_advertising(self, submission_id: int) -> int:
        return await self._sync_panel_review_markup(
            submission_id,
            state="advertising",
        )

    async def mark_panel_submission_agent_advertising(self, submission_id: int) -> int:
        return await self._sync_panel_review_markup(
            submission_id,
            state="advertising",
            moderator_label="agent",
        )

    async def mark_panel_submission_banned(self, submission_id: int) -> int:
        return await self._sync_panel_review_markup(submission_id, state="banned")

    async def set_status_for_review_message(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
        status: SubmissionStatus,
        moderator_note: str | None = None,
        legacy_scheduled_for: datetime | None = None,
        reviewer_id: int | None = None,
        moderation_decision: str | None = None,
        moderation_action: str = "legacy_status",
    ) -> bool:
        async with session_factory() as session:
            row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=channel_tg_id,
                review_chat_id=review_chat_id,
                review_message_id=review_message_id,
            )
            if row is None:
                return False

            submission = await self.importer.ensure_submission_for_legacy_row(session, row)
            if submission is None:
                return False

            if legacy_scheduled_for is not None and status == SubmissionStatus.CONTENT_CREATED:
                await self._upsert_legacy_delayed_audit(
                    session=session,
                    submission=submission,
                    scheduled_for=legacy_scheduled_for,
                    moderator_note=moderator_note,
                )
                if reviewer_id is not None and moderation_decision is not None:
                    await self.moderation_cases.record_submission_decision(
                        session,
                        submission_id=submission.id,
                        moderator_id=reviewer_id,
                        decision=moderation_decision,
                        source="legacy",
                        action=moderation_action,
                    )
                    await session.commit()
                return True

            await self.moderation.set_submission_status(
                session=session,
                submission_id=submission.id,
                status=status,
                moderator_note=moderator_note,
            )
            if status == SubmissionStatus.REJECTED:
                await self._cancel_legacy_delayed_audit(
                    session=session,
                    submission=submission,
                )
            if reviewer_id is not None and moderation_decision is not None:
                await self.moderation_cases.record_submission_decision(
                    session,
                    submission_id=submission.id,
                    moderator_id=reviewer_id,
                    decision=moderation_decision,
                    source="legacy",
                    action=moderation_action,
                )
                await session.commit()
            return True

    async def approve_review_message(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
        reviewer_id: int,
    ) -> ContentItem | None:
        async with session_factory() as session:
            row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=channel_tg_id,
                review_chat_id=review_chat_id,
                review_message_id=review_message_id,
            )
            if row is None:
                return None

            submission = await self.importer.ensure_submission_for_legacy_row(session, row)
            if submission is None:
                return None

            channel = await session.get(Channel, submission.channel_id)
            if channel is None or not channel.is_active:
                raise ValueError("Channel is inactive or unlinked")

            item = await self._get_or_create_content_item(session, submission)
            terminal_ready_statuses = {
                ContentItemStatus.APPROVED,
                ContentItemStatus.SCHEDULED,
                ContentItemStatus.PUBLISHED,
            }
            if item.status not in terminal_ready_statuses:
                item = await self.moderation.review_content_item(
                    session=session,
                    content_item_id=item.id,
                    reviewer_id=reviewer_id,
                    decision=ReviewDecision.APPROVE,
                    review_note="Approved in legacy moderation chat for slot pipeline",
                    moderation_source="legacy",
                    moderation_action="approve_to_slot",
                )

            related = await self.moderation.get_related_submissions(session, submission)
            reviewed_at = datetime.now(timezone.utc)
            for related_submission in related:
                related_submission.status = SubmissionStatus.CONTENT_CREATED
                related_submission.reviewed_at = reviewed_at
                related_submission.moderator_note = "Handled in legacy moderation: approved to slot"
            await self.moderation_cases.record_submission_decision(
                session,
                submission_id=submission.id,
                moderator_id=reviewer_id,
                decision="approved",
                source="legacy",
                action="approve_to_slot",
            )
            await session.commit()
            await session.refresh(item)
            return item

    async def cancel_review_message_approval(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
        reviewer_id: int,
    ) -> ContentItem | None:
        async with session_factory() as session:
            row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=channel_tg_id,
                review_chat_id=review_chat_id,
                review_message_id=review_message_id,
            )
            if row is None:
                return None

            submission = await self.importer.ensure_submission_for_legacy_row(session, row)
            if submission is None:
                return None

            item = await self._get_latest_content_item(session, submission)
            if item is None:
                related = await self.moderation.get_related_submissions(session, submission)
                reviewed_at = datetime.now(timezone.utc)
                for related_submission in related:
                    related_submission.status = SubmissionStatus.HOLD
                    related_submission.reviewed_at = reviewed_at
                    related_submission.moderator_note = "Legacy slot approval cancelled"
                await self.moderation_cases.void_submission_case(
                    session,
                    submission_id=submission.id,
                    moderator_id=reviewer_id,
                    source="legacy",
                    action="cancel_approve_to_slot",
                )
                await session.commit()
                return None

            sent_count = await session.scalar(
                select(PublicationLog)
                .where(
                    PublicationLog.content_item_id == item.id,
                    PublicationLog.publish_status == PublicationStatus.SENT,
                )
                .limit(1)
            )
            if sent_count is not None or item.status == ContentItemStatus.PUBLISHED:
                raise ValueError("Content item is already published and cannot be cancelled")

            scheduled_logs = list(
                (
                    await session.execute(
                        select(PublicationLog).where(
                            PublicationLog.content_item_id == item.id,
                            PublicationLog.publish_status == PublicationStatus.SCHEDULED,
                        )
                    )
                ).scalars().all()
            )
            for log_item in scheduled_logs:
                log_item.publish_status = PublicationStatus.CANCELLED
                log_item.error_text = "Legacy slot approval cancelled"

            item.scheduled_for = None
            if item.status != ContentItemStatus.HOLD:
                item = await self.moderation.review_content_item(
                    session=session,
                    content_item_id=item.id,
                    reviewer_id=reviewer_id,
                    decision=ReviewDecision.HOLD,
                    review_note="Legacy slot approval cancelled",
                    moderation_source="legacy",
                    moderation_action="cancel_approve_to_slot",
                )

            related = await self.moderation.get_related_submissions(session, submission)
            reviewed_at = datetime.now(timezone.utc)
            for related_submission in related:
                related_submission.status = SubmissionStatus.HOLD
                related_submission.reviewed_at = reviewed_at
                related_submission.moderator_note = "Legacy slot approval cancelled"
            await self.moderation_cases.void_submission_case(
                session,
                submission_id=submission.id,
                moderator_id=reviewer_id,
                source="legacy",
                action="cancel_approve_to_slot",
            )
            await session.commit()
            await session.refresh(item)
            return item

    async def cancel_review_message_rejection(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
        reviewer_id: int,
    ) -> bool:
        async with session_factory() as session:
            row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=channel_tg_id,
                review_chat_id=review_chat_id,
                review_message_id=review_message_id,
            )
            if row is None:
                return False

            submission = await self.importer.ensure_submission_for_legacy_row(session, row)
            if submission is None:
                return False

            related = await self.moderation.get_related_submissions(session, submission)
            reviewed_at = datetime.now(timezone.utc)
            for related_submission in related:
                related_submission.status = SubmissionStatus.HOLD
                related_submission.reviewed_at = reviewed_at
                related_submission.moderator_note = "Legacy rejection cancelled"

            audit_item = await self._get_legacy_delayed_audit_item(session, submission)
            if audit_item is not None and audit_item.status == ContentItemStatus.REJECTED:
                audit_item.status = ContentItemStatus.HOLD
                audit_item.scheduled_for = None

            await self.moderation_cases.void_submission_case(
                session,
                submission_id=submission.id,
                moderator_id=reviewer_id,
                source="legacy",
                action="cancel_reject",
            )
            await session.commit()
            return True

    async def claim_legacy_publication(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
    ) -> bool:
        """Reserve a review card before Telegram receives a publish call.

        The reservation is committed first. MCP moderation and concurrent
        legacy callbacks therefore see the submission as already handled
        while the Telegram request is in flight.
        """
        async with session_factory() as session:
            row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=channel_tg_id,
                review_chat_id=review_chat_id,
                review_message_id=review_message_id,
            )
            if row is None:
                raise ValueError("Legacy review message is not linked to a submission")

            submission = await self.importer.ensure_submission_for_legacy_row(session, row)
            if submission is None:
                raise ValueError("Legacy review message could not be imported")

            locked_submission = await session.scalar(
                select(Submission)
                .where(Submission.id == submission.id)
                .with_for_update()
            )
            if locked_submission is not None:
                submission = locked_submission

            related = await self.moderation.get_related_submissions(session, submission)
            submission_ids = [item.id for item in related]
            text_hashes = {
                item.text_hash for item in related
                if getattr(item, "text_hash", None)
            }
            audit_item = await self._get_legacy_delayed_audit_item(session, submission)
            if audit_item is not None:
                log_item = await self._get_legacy_delayed_audit_log(session, audit_item.id)
                if audit_item.status in {
                    ContentItemStatus.PENDING_REVIEW,
                    ContentItemStatus.APPROVED,
                    ContentItemStatus.SCHEDULED,
                    ContentItemStatus.PUBLISHED,
                } or (
                    log_item is not None
                    and log_item.publish_status in {
                        PublicationStatus.SCHEDULED,
                        PublicationStatus.SENT,
                    }
                ):
                    await session.commit()
                    return False

            identity_conditions = [ContentItem.origin_submission_id.in_(submission_ids)]
            if text_hashes:
                identity_conditions.append(ContentItem.text_hash.in_(text_hashes))

            conflicting_item_id = await session.scalar(
                select(ContentItem.id)
                .where(
                    ContentItem.channel_id == submission.channel_id,
                    or_(
                        *identity_conditions,
                    ),
                    or_(
                        ContentItem.template_key.is_(None),
                        ContentItem.template_key != LEGACY_DELAYED_AUDIT_TEMPLATE_KEY,
                    ),
                    ContentItem.status.in_(
                        {
                            ContentItemStatus.PENDING_REVIEW,
                            ContentItemStatus.APPROVED,
                            ContentItemStatus.SCHEDULED,
                            ContentItemStatus.PUBLISHED,
                        }
                    ),
                )
                .limit(1)
            )
            if conflicting_item_id is not None:
                await session.commit()
                return False

            await self._upsert_legacy_delayed_audit(
                session=session,
                submission=submission,
                scheduled_for=datetime.now(timezone.utc),
                moderator_note="Handled in legacy moderation: publication claimed",
            )
            return True

    async def release_legacy_publication_claim(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
        error_text: str,
    ) -> bool:
        """Release a reservation after a definite pre-delivery failure."""
        async with session_factory() as session:
            row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=channel_tg_id,
                review_chat_id=review_chat_id,
                review_message_id=review_message_id,
            )
            if row is None:
                return False

            submission = await self.importer.ensure_submission_for_legacy_row(session, row)
            if submission is None:
                return False

            locked_submission = await session.scalar(
                select(Submission)
                .where(Submission.id == submission.id)
                .with_for_update()
            )
            if locked_submission is not None:
                submission = locked_submission

            audit_item = await self._get_legacy_delayed_audit_item(session, submission)
            if audit_item is None or audit_item.status == ContentItemStatus.PUBLISHED:
                await session.commit()
                return False

            log_item = await self._get_legacy_delayed_audit_log(session, audit_item.id)
            if log_item is not None and log_item.publish_status == PublicationStatus.SENT:
                await session.commit()
                return False

            audit_item.status = ContentItemStatus.REJECTED
            audit_item.scheduled_for = None
            if log_item is not None:
                log_item.publish_status = PublicationStatus.CANCELLED
                log_item.retry_after = None
                log_item.error_text = f"Legacy publication claim released: {error_text}"[:4000]

            related = await self.moderation.get_related_submissions(session, submission)
            for item in related:
                item.status = SubmissionStatus.NEW
                item.reviewed_at = None
                item.moderator_note = f"Legacy publication failed before delivery: {error_text}"[:4000]

            await session.commit()
            return True

    async def claim_legacy_delayed_delivery(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
    ) -> bool:
        """Claim one non-idempotent delayed Telegram delivery.

        The claim is committed before Telegram is called. A second collector
        process therefore observes ``attempt_count > 0`` and must not copy the
        same review message again. An ambiguous transport failure intentionally
        keeps the attempt consumed; retrying copyMessage cannot be made safe.
        """
        async with session_factory() as session:
            row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=channel_tg_id,
                review_chat_id=review_chat_id,
                review_message_id=review_message_id,
            )
            if row is None:
                raise ValueError("Legacy review message is not linked to a submission")

            submission = await self.importer.ensure_submission_for_legacy_row(session, row)
            if submission is None:
                raise ValueError("Legacy review message could not be imported")

            locked_submission = await session.scalar(
                select(Submission)
                .where(Submission.id == submission.id)
                .with_for_update()
            )
            if locked_submission is not None:
                submission = locked_submission

            audit_item = await self._get_legacy_delayed_audit_item(session, submission)
            if audit_item is None:
                raise ValueError("Legacy delayed publication audit item is missing")

            log_item = await self._get_legacy_delayed_audit_log(session, audit_item.id)
            if log_item is None:
                raise ValueError("Legacy delayed publication log is missing")

            if (
                audit_item.status != ContentItemStatus.SCHEDULED
                or log_item.publish_status != PublicationStatus.SCHEDULED
                or int(log_item.attempt_count or 0) > 0
            ):
                await session.commit()
                return False

            attempted_at = datetime.now(timezone.utc)
            log_item.attempt_count = 1
            log_item.last_attempt_at = attempted_at
            log_item.retry_after = None
            log_item.error_text = LEGACY_DELAYED_DELIVERY_CLAIM_MARKER
            await session.commit()
            return True

    async def mark_legacy_delayed_delivery_uncertain(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
        error_text: str,
    ) -> bool:
        """Stop automatic retries when Telegram may already have copied a post."""
        async with session_factory() as session:
            row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=channel_tg_id,
                review_chat_id=review_chat_id,
                review_message_id=review_message_id,
            )
            if row is None:
                return False

            submission = await self.importer.ensure_submission_for_legacy_row(session, row)
            if submission is None:
                return False

            locked_submission = await session.scalar(
                select(Submission)
                .where(Submission.id == submission.id)
                .with_for_update()
            )
            if locked_submission is not None:
                submission = locked_submission

            audit_item = await self._get_legacy_delayed_audit_item(session, submission)
            if audit_item is None:
                return False

            log_item = await self._get_legacy_delayed_audit_log(session, audit_item.id)
            if (
                audit_item.status == ContentItemStatus.PUBLISHED
                or (
                    log_item is not None
                    and log_item.publish_status == PublicationStatus.SENT
                )
            ):
                await session.commit()
                return False

            message = (
                "Legacy delayed delivery outcome is uncertain; automatic retry suppressed: "
                f"{error_text}"
            )[:4000]
            audit_item.status = ContentItemStatus.HOLD
            audit_item.scheduled_for = None
            if log_item is not None:
                log_item.publish_status = PublicationStatus.FAILED
                log_item.retry_after = None
                log_item.error_text = message

            reviewed_at = datetime.now(timezone.utc)
            related = await self.moderation.get_related_submissions(session, submission)
            for item in related:
                item.status = SubmissionStatus.HOLD
                item.reviewed_at = reviewed_at
                item.moderator_note = message

            await session.commit()
            return True

    async def mark_legacy_delayed_published(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
        telegram_message_id: int | None = None,
    ) -> bool:
        return await self.mark_legacy_published(
            channel_tg_id=channel_tg_id,
            review_chat_id=review_chat_id,
            review_message_id=review_message_id,
            telegram_message_id=telegram_message_id,
            moderator_note="Handled in legacy moderation: delayed published",
            moderation_action="publish_delayed",
        )

    async def mark_legacy_published(
        self,
        *,
        channel_tg_id: int,
        review_chat_id: int,
        review_message_id: int,
        telegram_message_id: int | None,
        moderator_note: str = "Handled in legacy moderation: published",
        reviewer_id: int | None = None,
        moderation_action: str = "publish_now",
    ) -> bool:
        """Finalize a legacy publication in the shared editorial ledger."""
        async with session_factory() as session:
            row = await self.legacy_reader.find_sender_row_by_review_message(
                channel_id=channel_tg_id,
                review_chat_id=review_chat_id,
                review_message_id=review_message_id,
            )
            if row is None:
                return False

            submission = await self.importer.ensure_submission_for_legacy_row(session, row)
            if submission is None:
                return False

            audit_item = await self._get_legacy_delayed_audit_item(session, submission)
            if audit_item is None:
                audit_item = await self._upsert_legacy_delayed_audit(
                    session=session,
                    submission=submission,
                    scheduled_for=datetime.now(timezone.utc),
                    moderator_note=moderator_note,
                )

            now = datetime.now(timezone.utc)
            audit_item.status = ContentItemStatus.PUBLISHED
            if audit_item.scheduled_for is None:
                audit_item.scheduled_for = now

            log_item = await self._get_legacy_delayed_audit_log(session, audit_item.id)
            if log_item is None:
                log_item = PublicationLog(
                    content_item_id=audit_item.id,
                    channel_id=audit_item.channel_id,
                    scheduled_for=audit_item.scheduled_for or now,
                    publish_status=PublicationStatus.SENT,
                    created_at=now,
                    error_text=LEGACY_DELAYED_AUDIT_LOG_MARKER,
                )
                session.add(log_item)

            log_item.publish_status = PublicationStatus.SENT
            log_item.published_at = now
            log_item.telegram_message_id = telegram_message_id
            log_item.retry_after = None
            log_item.error_text = None

            related = await self.moderation.get_related_submissions(session, submission)
            for item in related:
                item.status = SubmissionStatus.CONTENT_CREATED
                item.reviewed_at = now
                item.moderator_note = moderator_note

            if reviewer_id is not None:
                await self.moderation_cases.record_submission_decision(
                    session,
                    submission_id=submission.id,
                    moderator_id=reviewer_id,
                    decision="approved",
                    source="legacy",
                    action=moderation_action,
                )

            await session.commit()
            return True

    async def _upsert_legacy_delayed_audit(
        self,
        *,
        session,
        submission: Submission,
        scheduled_for: datetime,
        moderator_note: str | None,
    ) -> ContentItem:
        scheduled_for = scheduled_for.astimezone(timezone.utc)
        audit_item = await self._get_legacy_delayed_audit_item(session, submission)
        if audit_item is None:
            audit_item = await self.moderation.create_content_from_submission(
                session=session,
                submission_id=submission.id,
                channel_id=submission.channel_id,
                status=ContentItemStatus.SCHEDULED,
                review_required=False,
                template_key=LEGACY_DELAYED_AUDIT_TEMPLATE_KEY,
                tone_key="legacy_moderation",
                scheduled_for=scheduled_for,
                commit=False,
            )

        audit_item.status = ContentItemStatus.SCHEDULED
        audit_item.scheduled_for = scheduled_for
        audit_item.review_required = False
        audit_item.template_key = LEGACY_DELAYED_AUDIT_TEMPLATE_KEY
        audit_item.tone_key = "legacy_moderation"

        log_item = await self._get_legacy_delayed_audit_log(session, audit_item.id)
        if log_item is None:
            log_item = PublicationLog(
                content_item_id=audit_item.id,
                channel_id=audit_item.channel_id,
                scheduled_for=scheduled_for,
                publish_status=PublicationStatus.SCHEDULED,
                created_at=datetime.now(timezone.utc),
                error_text=LEGACY_DELAYED_AUDIT_LOG_MARKER,
            )
            session.add(log_item)
        else:
            log_item.scheduled_for = scheduled_for
            log_item.publish_status = PublicationStatus.SCHEDULED
            log_item.published_at = None
            log_item.telegram_message_id = None
            log_item.error_text = LEGACY_DELAYED_AUDIT_LOG_MARKER
            log_item.attempt_count = 0
            log_item.last_attempt_at = None
            log_item.retry_after = None

        reviewed_at = datetime.now(timezone.utc)
        related = await self.moderation.get_related_submissions(session, submission)
        for item in related:
            item.status = SubmissionStatus.CONTENT_CREATED
            item.reviewed_at = reviewed_at
            item.moderator_note = moderator_note

        await session.commit()
        await session.refresh(audit_item)
        return audit_item

    async def _cancel_legacy_delayed_audit(
        self,
        *,
        session,
        submission: Submission,
    ) -> None:
        audit_item = await self._get_legacy_delayed_audit_item(session, submission)
        if audit_item is None:
            return

        audit_item.status = ContentItemStatus.REJECTED
        audit_item.scheduled_for = None

        log_item = await self._get_legacy_delayed_audit_log(session, audit_item.id)
        if log_item is not None and log_item.publish_status == PublicationStatus.SCHEDULED:
            log_item.publish_status = PublicationStatus.CANCELLED
            log_item.error_text = "Legacy delayed publication rejected"

        await session.commit()

    async def _get_legacy_delayed_audit_item(
        self,
        session,
        submission: Submission,
    ) -> ContentItem | None:
        related = await self.moderation.get_related_submissions(session, submission)
        submission_ids = [item.id for item in related]
        return await session.scalar(
            select(ContentItem)
            .where(
                ContentItem.origin_submission_id.in_(submission_ids),
                ContentItem.template_key == LEGACY_DELAYED_AUDIT_TEMPLATE_KEY,
            )
            .order_by(ContentItem.created_at.desc())
            .limit(1)
        )

    async def _get_or_create_content_item(self, session, submission: Submission) -> ContentItem:
        existing = await self._get_latest_content_item(session, submission)
        if existing is not None:
            return existing

        return await self.moderation.create_content_from_submission(
            session=session,
            submission_id=submission.id,
            channel_id=submission.channel_id,
            status=ContentItemStatus.PENDING_REVIEW,
        )

    async def _get_latest_content_item(self, session, submission: Submission) -> ContentItem | None:
        related = await self.moderation.get_related_submissions(session, submission)
        submission_ids = [item.id for item in related]
        return await session.scalar(
            select(ContentItem)
            .where(
                ContentItem.origin_submission_id.in_(submission_ids),
                or_(
                    ContentItem.template_key.is_(None),
                    ContentItem.template_key != LEGACY_DELAYED_AUDIT_TEMPLATE_KEY,
                ),
            )
            .order_by(ContentItem.created_at.desc())
            .limit(1)
        )

    @staticmethod
    async def _get_legacy_delayed_audit_log(session, content_item_id: int) -> PublicationLog | None:
        return await session.scalar(
            select(PublicationLog)
            .where(PublicationLog.content_item_id == content_item_id)
            .order_by(PublicationLog.created_at.desc())
            .limit(1)
        )

    async def _sync_panel_review_markup(
        self,
        submission_id: int,
        state: str,
        *,
        moderator_label: str | None = None,
        allow_cancel: bool = False,
    ) -> int:
        async with session_factory() as session:
            submission = await session.get(Submission, submission_id)
            if submission is None:
                return 0

            channel = await session.get(Channel, submission.channel_id)
            if channel is None:
                return 0

            related_submissions = await self.moderation.get_related_submissions(session, submission)
            submission_by_legacy_row_id = {
                item.legacy_row_id: item
                for item in related_submissions
                if item.legacy_row_id is not None
            }

        if not submission_by_legacy_row_id:
            return 0

        legacy_rows = await self.legacy_reader.fetch_sender_rows_by_ids(list(submission_by_legacy_row_id))
        review_rows = [
            row for row in legacy_rows
            if row.review_chat_id is not None and row.review_message_id is not None
        ]
        if not review_rows:
            return 0

        binding = await self.legacy_reader.get_bot_binding(int(channel.tg_channel_id))
        if binding is None:
            return 0

        bot = AsyncTeleBot(binding.bot_api_token)
        updated_count = 0
        for row in review_rows:
            related_submission = submission_by_legacy_row_id.get(row.id)
            markup = self._build_panel_status_markup(
                state=state,
                user_id=row.user_id or (related_submission.source_user_id if related_submission else None),
                username=row.username or (related_submission.username if related_submission else None),
                first_name=row.first_name or (related_submission.first_name if related_submission else None),
                moderator_label=moderator_label,
                allow_cancel=allow_cancel,
            )
            try:
                await bot.edit_message_reply_markup(
                    chat_id=int(row.review_chat_id),
                    message_id=int(row.review_message_id),
                    reply_markup=markup,
                )
                updated_count += 1
            except Exception as ex:
                logger.error(
                    "Failed to sync panel moderation status '{}' to review message {} in chat {}: {}",
                    state,
                    row.review_message_id,
                    row.review_chat_id,
                    ex,
                )
        return updated_count

    @staticmethod
    def _build_panel_status_markup(
        *,
        state: str,
        user_id: int | None,
        username: str | None,
        first_name: str | None,
        moderator_label: str | None = None,
        allow_cancel: bool = False,
    ) -> InlineKeyboardMarkup:
        if moderator_label is not None and state == "approved":
            return build_slot_status_markup(
                sender_id=user_id,
                sender_username=username,
                sender_first_name=first_name,
                moderator_id=0,
                moderator_username=None,
                moderator_first_name=moderator_label,
                state="approved",
                allow_cancel=allow_cancel,
                moderator_callback_data="agent_info",
                always_show_sender=True,
            )
        if moderator_label is not None and state == "rejected":
            return build_rejection_status_markup(
                sender_id=user_id,
                sender_username=username,
                sender_first_name=first_name,
                moderator_id=0,
                moderator_username=None,
                moderator_first_name=moderator_label,
                moderator_callback_data="agent_info",
            )
        if state == "advertising":
            return build_advertising_status_markup(
                sender_id=user_id,
                sender_username=username,
                sender_first_name=first_name,
                moderator_label=moderator_label,
                moderator_callback_data="agent_info" if moderator_label is not None else None,
            )

        labels = {
            "approved": "Одобрено",
            "rejected": "Отклонено",
            "banned": "Забанен",
        }
        author_text = (
            f"@{username}" if username else first_name or (str(user_id) if user_id is not None else "Автор")
        )
        status_label = labels.get(state, "Обработано")
        button_label = f"{author_text} ({status_label})"
        callback_data = f"add_info;{user_id or 0}"

        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton(
                text=button_label,
                callback_data=callback_data,
            )
        )
        return markup
