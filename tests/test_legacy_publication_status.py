from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.editorial.models.enums import ContentItemStatus
from src.legacy_publication_status import LegacyPublicationStatusService


class _SessionContext:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Result:
    def __init__(self, rows=None, scalar_rows=None) -> None:
        self.rows = list(rows or [])
        self.scalar_rows = list(scalar_rows or [])

    def all(self):
        return self.rows

    def scalars(self):
        return _Result(rows=self.scalar_rows)


@pytest.mark.asyncio
async def test_published_content_updates_legacy_review_with_approving_admin(monkeypatch) -> None:
    item = SimpleNamespace(
        id=11,
        channel_id=7,
        text_hash="same-text",
        status=ContentItemStatus.PUBLISHED,
        origin_submission_id=101,
    )
    submission = SimpleNamespace(
        id=101,
        channel_id=7,
        content_type="text",
        legacy_row_id=55,
        source_user_id=1001,
    )
    channel = SimpleNamespace(tg_channel_id=-1007)
    approval = SimpleNamespace(reviewer_id=987654321)
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[item, submission, channel]),
        scalar=AsyncMock(return_value=approval),
        execute=AsyncMock(
            side_effect=[
                _Result(),
                _Result(scalar_rows=[]),
                _Result(scalar_rows=[]),
            ]
        ),
        add_all=Mock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.legacy_publication_status.session_factory",
        lambda: _SessionContext(session),
    )

    moderation = SimpleNamespace(get_related_submissions=AsyncMock(return_value=[submission]))
    review_row = SimpleNamespace(
        id=55,
        user_id=1001,
        review_chat_id=-100123,
        review_message_id=4321,
    )
    reader = SimpleNamespace(
        fetch_sender_rows_by_ids=AsyncMock(return_value=[review_row]),
        get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="1:token")),
    )
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(
            return_value=SimpleNamespace(
                user=SimpleNamespace(username="review_admin", first_name="Admin")
            )
        ),
        get_chat=AsyncMock(),
        edit_message_reply_markup=AsyncMock(return_value=True),
        close_session=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.editorial.services.telegram_publisher.AsyncTeleBot",
        lambda _token: bot,
    )

    updated = await LegacyPublicationStatusService(
        legacy_reader=reader,
        moderation=moderation,
    ).mark_content_item_published(item.id)

    assert updated == 1
    bot.get_chat_member.assert_awaited_once_with(review_row.review_chat_id, approval.reviewer_id)
    bot.get_chat.assert_not_awaited()
    edit_kwargs = bot.edit_message_reply_markup.await_args.kwargs
    assert edit_kwargs["chat_id"] == review_row.review_chat_id
    assert edit_kwargs["message_id"] == review_row.review_message_id
    assert edit_kwargs["reply_markup"].to_dict()["inline_keyboard"] == [
        [{"text": "👤 1001", "callback_data": "add_info;1001"}],
        [{"text": "✅ @review_admin (опубликовано)", "callback_data": "add_info;987654321"}],
    ]
    bot.close_session.assert_awaited_once()
    synced_sources = session.add_all.call_args.args[0]
    assert [(source.content_item_id, source.submission_id, source.role) for source in synced_sources] == [
        (item.id, submission.id, "legacy_published")
    ]


@pytest.mark.asyncio
async def test_published_text_updates_approved_duplicate_and_cancels_its_slot(monkeypatch) -> None:
    item = SimpleNamespace(
        id=11,
        channel_id=7,
        text_hash="same-text",
        status=ContentItemStatus.PUBLISHED,
        origin_submission_id=101,
    )
    submission = SimpleNamespace(
        id=101,
        channel_id=7,
        content_type="text",
        legacy_row_id=55,
        source_user_id=1001,
        username="first_author",
        first_name="First",
    )
    duplicate_submission = SimpleNamespace(
        id=102,
        channel_id=7,
        content_type="text",
        legacy_row_id=56,
        source_user_id=1002,
        username="second_author",
        first_name="Second",
    )
    duplicate_item = SimpleNamespace(
        id=12,
        status=ContentItemStatus.SCHEDULED,
        scheduled_for=object(),
    )
    original_approval = SimpleNamespace(reviewer_id=9001)
    duplicate_approval = SimpleNamespace(content_item_id=12, reviewer_id=9002)
    duplicate_log = SimpleNamespace(
        publish_status="scheduled",
        error_text=None,
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[item, submission, SimpleNamespace(tg_channel_id=-1007)]),
        scalar=AsyncMock(return_value=original_approval),
        execute=AsyncMock(
            side_effect=[
                _Result(rows=[(duplicate_item, duplicate_submission)]),
                _Result(scalar_rows=[duplicate_approval]),
                _Result(scalar_rows=[duplicate_log]),
                _Result(scalar_rows=[]),
                _Result(scalar_rows=[]),
            ]
        ),
        add_all=Mock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.legacy_publication_status.session_factory",
        lambda: _SessionContext(session),
    )

    moderation = SimpleNamespace(get_related_submissions=AsyncMock(return_value=[submission]))
    review_rows = [
        SimpleNamespace(
            id=55,
            user_id=1001,
            username="first_author",
            first_name="First",
            review_chat_id=-100123,
            review_message_id=4321,
        ),
        SimpleNamespace(
            id=56,
            user_id=1002,
            username="second_author",
            first_name="Second",
            review_chat_id=-100123,
            review_message_id=4322,
        ),
    ]
    reader = SimpleNamespace(
        fetch_sender_rows_by_ids=AsyncMock(return_value=review_rows),
        get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="1:token")),
    )

    async def get_chat_member(_chat_id, moderator_id):
        return SimpleNamespace(
            user=SimpleNamespace(username=f"admin{moderator_id}", first_name="Admin")
        )

    bot = SimpleNamespace(
        get_chat_member=AsyncMock(side_effect=get_chat_member),
        get_chat=AsyncMock(),
        edit_message_reply_markup=AsyncMock(return_value=True),
        close_session=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.editorial.services.telegram_publisher.AsyncTeleBot",
        lambda _token: bot,
    )

    updated = await LegacyPublicationStatusService(
        legacy_reader=reader,
        moderation=moderation,
    ).mark_content_item_published(item.id)

    assert updated == 2
    assert duplicate_log.publish_status == "cancelled"
    assert "content item 11" in duplicate_log.error_text
    assert duplicate_item.status == ContentItemStatus.APPROVED
    assert duplicate_item.scheduled_for is None
    assert [call.kwargs["message_id"] for call in bot.edit_message_reply_markup.await_args_list] == [
        4321,
        4322,
    ]
    duplicate_markup = bot.edit_message_reply_markup.await_args_list[1].kwargs[
        "reply_markup"
    ].to_dict()
    assert duplicate_markup["inline_keyboard"][1][0]["text"] == (
        "✅ @admin9002 (опубликовано)"
    )
    synced_sources = session.add_all.call_args.args[0]
    assert {
        (source.content_item_id, source.submission_id, source.role) for source in synced_sources
    } == {
        (item.id, submission.id, "legacy_published"),
        (item.id, duplicate_submission.id, "legacy_published"),
    }


@pytest.mark.asyncio
async def test_reconcile_prioritizes_stale_duplicates_and_deduplicates_item_ids(monkeypatch) -> None:
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar_rows=[11, 12]),
                _Result(scalar_rows=[12, 13]),
            ]
        )
    )
    monkeypatch.setattr(
        "src.legacy_publication_status.session_factory",
        lambda: _SessionContext(session),
    )
    service = LegacyPublicationStatusService(
        legacy_reader=SimpleNamespace(),
        moderation=SimpleNamespace(),
    )
    service.mark_content_item_published = AsyncMock(return_value=1)

    updated = await service.reconcile_published_review_statuses(limit=5)

    assert updated == 3
    assert [call.args[0] for call in service.mark_content_item_published.await_args_list] == [
        11,
        12,
        13,
    ]
