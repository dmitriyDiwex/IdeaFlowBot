from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.editorial.models.enums import ContentItemStatus, PublicationStatus, SubmissionStatus
from src.editorial.services.legacy_moderation_sync import LegacyModerationSyncService


class _SessionContext:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


def _service(submission):
    return LegacyModerationSyncService(
        legacy_reader=SimpleNamespace(
            find_sender_row_by_review_message=AsyncMock(return_value=SimpleNamespace(id=99))
        ),
        importer=SimpleNamespace(
            ensure_submission_for_legacy_row=AsyncMock(return_value=submission)
        ),
        moderation=SimpleNamespace(get_related_submissions=AsyncMock(return_value=[submission])),
    )


@pytest.mark.asyncio
async def test_legacy_publication_is_claimed_before_telegram_delivery(monkeypatch) -> None:
    submission = SimpleNamespace(id=12, channel_id=7, text_hash="hash-12")
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[submission, None]),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.editorial.services.legacy_moderation_sync.session_factory",
        lambda: _SessionContext(session),
    )
    service = _service(submission)
    service._get_legacy_delayed_audit_item = AsyncMock(return_value=None)
    service._upsert_legacy_delayed_audit = AsyncMock()

    claimed = await service.claim_legacy_publication(
        channel_tg_id=-10077,
        review_chat_id=-10055,
        review_message_id=503,
    )

    assert claimed is True
    service._upsert_legacy_delayed_audit.assert_awaited_once()
    claim_kwargs = service._upsert_legacy_delayed_audit.await_args.kwargs
    assert claim_kwargs["submission"] is submission
    assert claim_kwargs["moderator_note"] == "Handled in legacy moderation: publication claimed"
    conflict_query = str(session.scalar.await_args_list[1].args[0])
    assert "content_items.channel_id" in conflict_query
    assert "content_items.text_hash" in conflict_query


@pytest.mark.asyncio
async def test_existing_legacy_publication_claim_blocks_second_delivery(monkeypatch) -> None:
    submission = SimpleNamespace(id=12, channel_id=7, text_hash="hash-12")
    audit_item = SimpleNamespace(id=71, status=ContentItemStatus.SCHEDULED)
    log_item = SimpleNamespace(publish_status=PublicationStatus.SCHEDULED)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=submission),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.editorial.services.legacy_moderation_sync.session_factory",
        lambda: _SessionContext(session),
    )
    service = _service(submission)
    service._get_legacy_delayed_audit_item = AsyncMock(return_value=audit_item)
    service._get_legacy_delayed_audit_log = AsyncMock(return_value=log_item)
    service._upsert_legacy_delayed_audit = AsyncMock()

    claimed = await service.claim_legacy_publication(
        channel_tg_id=-10077,
        review_chat_id=-10055,
        review_message_id=503,
    )

    assert claimed is False
    service._upsert_legacy_delayed_audit.assert_not_awaited()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_content_with_same_hash_blocks_legacy_delivery(monkeypatch) -> None:
    submission = SimpleNamespace(id=12, channel_id=7, text_hash="hash-12")
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[submission, 48]),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.editorial.services.legacy_moderation_sync.session_factory",
        lambda: _SessionContext(session),
    )
    service = _service(submission)
    service._get_legacy_delayed_audit_item = AsyncMock(return_value=None)
    service._upsert_legacy_delayed_audit = AsyncMock()

    claimed = await service.claim_legacy_publication(
        channel_tg_id=-10077,
        review_chat_id=-10055,
        review_message_id=503,
    )

    assert claimed is False
    service._upsert_legacy_delayed_audit.assert_not_awaited()
    conflict_query = str(session.scalar.await_args_list[1].args[0])
    assert "content_items.channel_id" in conflict_query
    assert "content_items.text_hash" in conflict_query
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_definite_delivery_failure_releases_legacy_claim(monkeypatch) -> None:
    submission = SimpleNamespace(
        id=12,
        channel_id=7,
        text_hash="hash-12",
        status=SubmissionStatus.CONTENT_CREATED,
        reviewed_at=datetime.now(timezone.utc),
        moderator_note="claimed",
    )
    audit_item = SimpleNamespace(
        id=71,
        status=ContentItemStatus.SCHEDULED,
        scheduled_for=datetime.now(timezone.utc),
    )
    log_item = SimpleNamespace(
        publish_status=PublicationStatus.SCHEDULED,
        retry_after=datetime.now(timezone.utc),
        error_text=None,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=submission),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.editorial.services.legacy_moderation_sync.session_factory",
        lambda: _SessionContext(session),
    )
    service = _service(submission)
    service._get_legacy_delayed_audit_item = AsyncMock(return_value=audit_item)
    service._get_legacy_delayed_audit_log = AsyncMock(return_value=log_item)

    released = await service.release_legacy_publication_claim(
        channel_tg_id=-10077,
        review_chat_id=-10055,
        review_message_id=503,
        error_text="bad request",
    )

    assert released is True
    assert audit_item.status == ContentItemStatus.REJECTED
    assert audit_item.scheduled_for is None
    assert log_item.publish_status == PublicationStatus.CANCELLED
    assert log_item.retry_after is None
    assert "bad request" in log_item.error_text
    assert submission.status == SubmissionStatus.NEW
    assert submission.reviewed_at is None
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_delayed_delivery_claim_is_consumed_before_telegram_call(monkeypatch) -> None:
    submission = SimpleNamespace(id=12, channel_id=7, text_hash="hash-12")
    audit_item = SimpleNamespace(id=71, status=ContentItemStatus.SCHEDULED)
    log_item = SimpleNamespace(
        publish_status=PublicationStatus.SCHEDULED,
        attempt_count=0,
        last_attempt_at=None,
        retry_after=datetime.now(timezone.utc),
        error_text="pending",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=submission),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.editorial.services.legacy_moderation_sync.session_factory",
        lambda: _SessionContext(session),
    )
    service = _service(submission)
    service._get_legacy_delayed_audit_item = AsyncMock(return_value=audit_item)
    service._get_legacy_delayed_audit_log = AsyncMock(return_value=log_item)

    first_claim = await service.claim_legacy_delayed_delivery(
        channel_tg_id=-10077,
        review_chat_id=-10055,
        review_message_id=503,
    )
    second_claim = await service.claim_legacy_delayed_delivery(
        channel_tg_id=-10077,
        review_chat_id=-10055,
        review_message_id=503,
    )

    assert first_claim is True
    assert second_claim is False
    assert log_item.attempt_count == 1
    assert log_item.last_attempt_at is not None
    assert log_item.retry_after is None
    assert log_item.error_text == "Legacy delayed delivery claimed"
    assert session.commit.await_count == 2


@pytest.mark.asyncio
async def test_ambiguous_delayed_delivery_failure_is_held_without_retry(monkeypatch) -> None:
    submission = SimpleNamespace(
        id=12,
        channel_id=7,
        text_hash="hash-12",
        status=SubmissionStatus.CONTENT_CREATED,
        reviewed_at=datetime.now(timezone.utc),
        moderator_note="claimed",
    )
    audit_item = SimpleNamespace(
        id=71,
        status=ContentItemStatus.SCHEDULED,
        scheduled_for=datetime.now(timezone.utc),
    )
    log_item = SimpleNamespace(
        publish_status=PublicationStatus.SCHEDULED,
        retry_after=None,
        error_text="Legacy delayed delivery claimed",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=submission),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.editorial.services.legacy_moderation_sync.session_factory",
        lambda: _SessionContext(session),
    )
    service = _service(submission)
    service._get_legacy_delayed_audit_item = AsyncMock(return_value=audit_item)
    service._get_legacy_delayed_audit_log = AsyncMock(return_value=log_item)

    held = await service.mark_legacy_delayed_delivery_uncertain(
        channel_tg_id=-10077,
        review_chat_id=-10055,
        review_message_id=503,
        error_text="copyMessage exceeded 15 seconds",
    )

    assert held is True
    assert audit_item.status == ContentItemStatus.HOLD
    assert audit_item.scheduled_for is None
    assert log_item.publish_status == PublicationStatus.FAILED
    assert "automatic retry suppressed" in log_item.error_text
    assert submission.status == SubmissionStatus.HOLD
    assert "copyMessage exceeded" in submission.moderator_note
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_publication_is_written_to_shared_sent_log(monkeypatch) -> None:
    scheduled_for = datetime(2026, 9, 12, 12, 18, tzinfo=timezone.utc)
    submission = SimpleNamespace(
        id=12,
        channel_id=7,
        text_hash="hash-12",
        status=SubmissionStatus.CONTENT_CREATED,
    )
    audit_item = SimpleNamespace(
        id=71, channel_id=7, status=ContentItemStatus.SCHEDULED, scheduled_for=scheduled_for
    )
    log_item = SimpleNamespace(
        publish_status=PublicationStatus.SCHEDULED,
        published_at=None,
        telegram_message_id=None,
        retry_after=scheduled_for,
        error_text="pending",
    )
    session = SimpleNamespace(commit=AsyncMock(), add=AsyncMock())
    monkeypatch.setattr(
        "src.editorial.services.legacy_moderation_sync.session_factory",
        lambda: _SessionContext(session),
    )
    service = _service(submission)
    service.moderation_cases = SimpleNamespace(record_submission_decision=AsyncMock())
    service._get_legacy_delayed_audit_item = AsyncMock(return_value=audit_item)
    service._get_legacy_delayed_audit_log = AsyncMock(return_value=log_item)

    finalized = await service.mark_legacy_published(
        channel_tg_id=-10077,
        review_chat_id=-10055,
        review_message_id=503,
        telegram_message_id=701,
        reviewer_id=9001,
    )

    assert finalized is True
    assert audit_item.status == ContentItemStatus.PUBLISHED
    assert log_item.publish_status == PublicationStatus.SENT
    assert log_item.telegram_message_id == 701
    assert log_item.published_at is not None
    assert log_item.retry_after is None
    assert log_item.error_text is None
    assert submission.status == SubmissionStatus.CONTENT_CREATED
    service.moderation_cases.record_submission_decision.assert_awaited_once()
    session.commit.assert_awaited_once()
