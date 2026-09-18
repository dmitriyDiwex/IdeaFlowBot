from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.editorial.models.content import ContentItem
from src.editorial.models.enums import (
    ContentItemStatus,
    ContentSourceType,
    ReviewDecision,
    SubmissionStatus,
)
from src.editorial.models.review import Review
from src.editorial.models.submission import Submission
from src.editorial.services.moderation_case_service import ModerationCaseService
from src.editorial.services.tag_service import TagService
from src.editorial.utils.media import build_media_fingerprint, is_media_submission
from src.editorial.utils.text import clean_text, compute_raw_text_hash, compute_text_hash, normalize_text


class ModerationService:
    def __init__(self, tag_service: TagService | None = None) -> None:
        self.tag_service = tag_service or TagService()
        self.moderation_cases = ModerationCaseService()

    @staticmethod
    def _visible_submission_filter():
        return or_(Submission.source_chat_id.is_(None), Submission.source_chat_id >= 0)

    @staticmethod
    def _submission_group_key(submission: Submission) -> tuple | None:
        if not submission.media_group_id:
            return None
        return (
            submission.channel_id,
            submission.source_chat_id,
            submission.media_group_id,
        )

    async def get_related_submissions(
        self,
        session: AsyncSession,
        submission: Submission,
    ) -> list[Submission]:
        group_key = self._submission_group_key(submission)
        if group_key is None:
            return [submission]

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
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    def collapse_media_groups(submissions: list[Submission]) -> list[Submission]:
        collapsed: list[Submission] = []
        group_positions: dict[tuple, int] = {}
        for submission in submissions:
            group_key = submission.channel_id, submission.source_chat_id, submission.media_group_id
            if submission.media_group_id is None:
                collapsed.append(submission)
                continue
            if group_key not in group_positions:
                group_positions[group_key] = len(collapsed)
                collapsed.append(submission)
                continue
            existing_index = group_positions[group_key]
            existing_item = collapsed[existing_index]
            existing_has_text = bool((existing_item.cleaned_text or existing_item.raw_text or "").strip())
            current_has_text = bool((submission.cleaned_text or submission.raw_text or "").strip())
            if current_has_text and not existing_has_text:
                collapsed[existing_index] = submission
        return collapsed

    @staticmethod
    def _build_media_placeholder(submission: Submission, group_size: int) -> str:
        if submission.media_group_id:
            return f"<медиа-группа: {group_size} влож.>"
        labels = {
            "photo": "<фото без подписи>",
            "video": "<видео без подписи>",
            "animation": "<gif без подписи>",
        }
        return labels.get(submission.content_type, "<медиа без подписи>")

    @staticmethod
    def _build_submission_fingerprint(
        submission: Submission,
        group_size: int,
        text_value: str,
    ) -> tuple[str, str]:
        if is_media_submission(submission):
            return build_media_fingerprint(submission)

        normalized = normalize_text(text_value)
        text_hash = compute_text_hash(text_value)
        if normalized and text_hash:
            return normalized, text_hash

        cleaned_text = clean_text(text_value)
        if cleaned_text and submission.content_type == "text":
            return cleaned_text, compute_raw_text_hash(cleaned_text) or ""

        return build_media_fingerprint(submission)

    @staticmethod
    def _template_key_for_submission(submission: Submission) -> str:
        if submission.media_group_id:
            return "submission_media_group"
        if submission.content_type in {"photo", "video", "animation"}:
            return f"submission_{submission.content_type}"
        return "submission_plain"

    async def list_submissions(
        self,
        session: AsyncSession,
        status: SubmissionStatus | None = None,
        limit: int | None = 50,
    ) -> list[Submission]:
        stmt = (
            select(Submission)
            .where(self._visible_submission_filter())
            .order_by(Submission.created_at.desc())
        )
        if status is not None:
            stmt = stmt.where(Submission.status == status)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def set_submission_status(
        self,
        session: AsyncSession,
        submission_id: int,
        status: SubmissionStatus,
        moderator_note: str | None = None,
        commit: bool = True,
    ) -> Submission:
        submission = await session.get(Submission, submission_id)
        if submission is None:
            raise ValueError(f"Submission {submission_id} not found")
        related = await self.get_related_submissions(session, submission)
        reviewed_at = datetime.now(timezone.utc)
        for item in related:
            item.status = status
            item.moderator_note = moderator_note
            item.reviewed_at = reviewed_at
        if commit:
            await session.commit()
            await session.refresh(submission)
        else:
            await session.flush()
        return submission

    async def create_content_from_submission(
        self,
        session: AsyncSession,
        submission_id: int,
        channel_id: int | None = None,
        body_text: str | None = None,
        status: ContentItemStatus = ContentItemStatus.PENDING_REVIEW,
        review_required: bool = True,
        template_key: str | None = None,
        tone_key: str = "community",
        scheduled_for: datetime | None = None,
        commit: bool = True,
    ) -> ContentItem:
        submission = await session.get(Submission, submission_id)
        if submission is None:
            raise ValueError(f"Submission {submission_id} not found")

        related_submissions = await self.get_related_submissions(session, submission)
        group_size = len(related_submissions)
        text_candidates = [
            (item.cleaned_text or item.raw_text or "").strip()
            for item in related_submissions
        ]
        source_text = next((item for item in text_candidates if item), "")
        content_text = (body_text or source_text).strip()
        if not content_text:
            content_text = self._build_media_placeholder(submission, group_size)

        tags, primary_tag = await self.tag_service.apply_tags_to_content_cache(session, content_text)
        normalized_text, text_hash = self._build_submission_fingerprint(submission, group_size, content_text)
        item = ContentItem(
            channel_id=channel_id or submission.channel_id,
            source_type=ContentSourceType.SUBMISSION,
            origin_submission_id=submission.id,
            body_text=content_text,
            normalized_text=normalized_text,
            text_hash=text_hash,
            primary_tag=primary_tag,
            tags=tags,
            template_key=template_key or self._template_key_for_submission(submission),
            tone_key=tone_key,
            review_required=review_required,
            status=status,
            scheduled_for=scheduled_for,
        )
        session.add(item)
        reviewed_at = datetime.now(timezone.utc)
        for related_item in related_submissions:
            related_item.status = SubmissionStatus.CONTENT_CREATED
            related_item.reviewed_at = reviewed_at
        if commit:
            await session.commit()
            await session.refresh(item)
        else:
            await session.flush()
        return item

    async def list_content_items(
        self,
        session: AsyncSession,
        status: ContentItemStatus | None = None,
        limit: int = 50,
    ) -> list[ContentItem]:
        stmt = select(ContentItem).order_by(ContentItem.created_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(ContentItem.status == status)
        return list((await session.execute(stmt)).scalars().all())

    async def review_content_item(
        self,
        session: AsyncSession,
        content_item_id: int,
        reviewer_id: int,
        decision: ReviewDecision,
        review_note: str | None = None,
        edited_text: str | None = None,
        moderation_source: str = "api",
        moderation_action: str = "content_review",
        commit: bool = True,
    ) -> ContentItem:
        item = await session.get(ContentItem, content_item_id)
        if item is None:
            raise ValueError(f"Content item {content_item_id} not found")

        if item.status in {ContentItemStatus.SCHEDULED, ContentItemStatus.PUBLISHED}:
            if decision in {ReviewDecision.APPROVE, ReviewDecision.EDIT_AND_APPROVE}:
                await self.moderation_cases.record_content_decision(
                    session,
                    content_item=item,
                    moderator_id=reviewer_id,
                    decision=ReviewDecision.APPROVE,
                    source=moderation_source,
                    action=moderation_action,
                )
                if commit:
                    await session.commit()
                    await session.refresh(item)
                else:
                    await session.flush()
                return item
            raise ValueError("Scheduled or published content cannot be reviewed again")

        target_status = {
            ReviewDecision.APPROVE: ContentItemStatus.APPROVED,
            ReviewDecision.REJECT: ContentItemStatus.REJECTED,
            ReviewDecision.HOLD: ContentItemStatus.HOLD,
        }.get(decision)
        if target_status is not None and item.status == target_status:
            await self.moderation_cases.record_content_decision(
                session,
                content_item=item,
                moderator_id=reviewer_id,
                decision=decision,
                source=moderation_source,
                action=moderation_action,
            )
            if commit:
                await session.commit()
                await session.refresh(item)
            else:
                await session.flush()
            return item

        if decision == ReviewDecision.APPROVE:
            item.status = ContentItemStatus.APPROVED
        elif decision == ReviewDecision.REJECT:
            item.status = ContentItemStatus.REJECTED
        elif decision == ReviewDecision.HOLD:
            item.status = ContentItemStatus.HOLD
        elif decision == ReviewDecision.EDIT_AND_APPROVE:
            if not edited_text:
                raise ValueError("edited_text is required for edit_and_approve")
            tags, primary_tag = await self.tag_service.apply_tags_to_content_cache(session, edited_text)
            item.body_text = edited_text
            normalized_text = normalize_text(edited_text)
            text_hash = compute_text_hash(edited_text)
            if normalized_text and text_hash:
                item.normalized_text = normalized_text
                item.text_hash = text_hash
            else:
                item.normalized_text = clean_text(edited_text)
                item.text_hash = compute_raw_text_hash(edited_text) or ""
            item.tags = tags
            item.primary_tag = primary_tag
            item.status = ContentItemStatus.APPROVED
        else:
            raise ValueError(f"Unsupported review decision for content items: {decision}")

        session.add(
            Review(
                content_item_id=item.id,
                reviewer_id=reviewer_id,
                decision=decision,
                review_note=review_note,
                edited_text=edited_text,
                created_at=datetime.now(timezone.utc),
            )
        )
        await self.moderation_cases.record_content_decision(
            session,
            content_item=item,
            moderator_id=reviewer_id,
            decision=decision,
            source=moderation_source,
            action=moderation_action,
        )
        if commit:
            await session.commit()
            await session.refresh(item)
        else:
            await session.flush()
        return item
