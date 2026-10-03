from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.editorial.models.channel import Channel
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import ReviewDecision
from src.editorial.models.moderation_case import ModerationCase, ModerationCaseEvent
from src.editorial.models.submission import Submission


MODERATION_APPROVED = "approved"
MODERATION_REJECTED = "rejected"


class ModerationCaseService:
    async def record_content_decision(
        self,
        session: AsyncSession,
        *,
        content_item: ContentItem,
        moderator_id: int,
        decision: ReviewDecision,
        source: str,
        action: str,
        occurred_at: datetime | None = None,
    ) -> ModerationCase | None:
        if content_item.origin_submission_id is None:
            return None
        if decision in {ReviewDecision.APPROVE, ReviewDecision.EDIT_AND_APPROVE}:
            return await self.record_submission_decision(
                session,
                submission_id=content_item.origin_submission_id,
                moderator_id=moderator_id,
                decision=MODERATION_APPROVED,
                source=source,
                action=action,
                occurred_at=occurred_at,
            )
        if decision == ReviewDecision.REJECT:
            return await self.record_submission_decision(
                session,
                submission_id=content_item.origin_submission_id,
                moderator_id=moderator_id,
                decision=MODERATION_REJECTED,
                source=source,
                action=action,
                occurred_at=occurred_at,
            )
        if decision == ReviewDecision.HOLD:
            return await self.void_submission_case(
                session,
                submission_id=content_item.origin_submission_id,
                moderator_id=moderator_id,
                source=source,
                action=action,
                occurred_at=occurred_at,
            )
        return None

    async def record_submission_decision(
        self,
        session: AsyncSession,
        *,
        submission_id: int,
        moderator_id: int,
        decision: str,
        source: str,
        action: str,
        occurred_at: datetime | None = None,
    ) -> ModerationCase:
        if decision not in {MODERATION_APPROVED, MODERATION_REJECTED}:
            raise ValueError(f"Unsupported moderation decision: {decision}")

        occurred_at = occurred_at or datetime.now(timezone.utc)
        submission, related, canonical = await self._load_submission_group(session, submission_id)
        channel = await session.get(Channel, submission.channel_id)
        snapshots = self._build_snapshots(related)
        case_key = f"submission:{canonical.id}"

        case = await self._get_locked_case(session, case_key)
        created = False
        if case is None:
            case = ModerationCase(
                case_key=case_key,
                canonical_submission_id=canonical.id,
                channel_id=submission.channel_id,
                channel_tg_id=channel.tg_channel_id if channel is not None else None,
                moderator_id=int(moderator_id),
                decision=decision,
                source=source,
                action=action,
                decided_at=occurred_at,
                finalized_at=occurred_at,
                voided_at=None,
                **snapshots,
            )
            try:
                async with session.begin_nested():
                    session.add(case)
                    await session.flush()
                created = True
            except IntegrityError:
                case = await self._get_locked_case(session, case_key)
                if case is None:
                    raise

        if not created and case.voided_at is None and case.decision == decision:
            # Replayed callbacks and repeated clicks must not transfer the
            # payable action to another moderator or create another event.
            case.canonical_submission_id = canonical.id
            case.channel_id = submission.channel_id
            case.channel_tg_id = channel.tg_channel_id if channel is not None else case.channel_tg_id
            for field, value in snapshots.items():
                setattr(case, field, value)
            await session.flush()
            return case

        if not created:
            case.canonical_submission_id = canonical.id
            case.channel_id = submission.channel_id
            case.channel_tg_id = channel.tg_channel_id if channel is not None else case.channel_tg_id
            case.moderator_id = int(moderator_id)
            case.decision = decision
            case.source = source
            case.action = action
            case.decided_at = occurred_at
            case.finalized_at = occurred_at
            case.voided_at = None
            for field, value in snapshots.items():
                setattr(case, field, value)

        session.add(
            ModerationCaseEvent(
                case_id=case.id,
                moderator_id=int(moderator_id),
                event_type=decision,
                decision=decision,
                source=source,
                action=action,
                occurred_at=occurred_at,
            )
        )
        await session.flush()
        return case

    async def void_submission_case(
        self,
        session: AsyncSession,
        *,
        submission_id: int,
        moderator_id: int,
        source: str,
        action: str,
        occurred_at: datetime | None = None,
    ) -> ModerationCase | None:
        occurred_at = occurred_at or datetime.now(timezone.utc)
        _submission, _related, canonical = await self._load_submission_group(session, submission_id)
        case = await self._get_locked_case(session, f"submission:{canonical.id}")
        if case is None or case.voided_at is not None:
            return case

        case.finalized_at = None
        case.voided_at = occurred_at
        session.add(
            ModerationCaseEvent(
                case_id=case.id,
                moderator_id=int(moderator_id),
                event_type="voided",
                decision=case.decision,
                source=source,
                action=action,
                occurred_at=occurred_at,
            )
        )
        await session.flush()
        return case

    @staticmethod
    async def _get_locked_case(session: AsyncSession, case_key: str) -> ModerationCase | None:
        return await session.scalar(
            select(ModerationCase)
            .where(ModerationCase.case_key == case_key)
            .with_for_update()
        )

    @staticmethod
    async def _load_submission_group(
        session: AsyncSession,
        submission_id: int,
    ) -> tuple[Submission, list[Submission], Submission]:
        submission = await session.get(Submission, submission_id)
        if submission is None:
            raise ValueError(f"Submission {submission_id} not found")

        if submission.media_group_id:
            stmt = (
                select(Submission)
                .where(
                    Submission.channel_id == submission.channel_id,
                    Submission.media_group_id == submission.media_group_id,
                )
                .order_by(Submission.source_message_id.asc(), Submission.id.asc())
            )
            stmt = stmt.where(Submission.source_chat_id == submission.source_chat_id)
            related = list((await session.execute(stmt)).scalars().all())
        else:
            related = [submission]

        if not related:
            related = [submission]
        canonical = min(related, key=lambda item: item.id)
        return submission, related, canonical

    @staticmethod
    def _build_snapshots(related: list[Submission]) -> dict[str, object]:
        canonical = min(related, key=lambda item: item.id)
        author = next((item for item in related if item.source_user_id is not None), canonical)
        text_parts: list[str] = []
        for item in related:
            text = (item.cleaned_text or item.raw_text or "").strip()
            if text and text not in text_parts:
                text_parts.append(text)
        if text_parts:
            message_text = "\n\n".join(text_parts)
        elif len(related) > 1:
            message_text = f"<медиагруппа без подписи, файлов: {len(related)}>"
        else:
            message_text = f"<{canonical.content_type or 'сообщение'} без подписи>"

        return {
            "source_user_id": author.source_user_id,
            "source_username": author.username,
            "source_first_name": author.first_name,
            "source_message_id": canonical.source_message_id,
            "media_group_id": canonical.media_group_id,
            "message_text": message_text,
        }
