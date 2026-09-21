from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.editorial.mcp.server import BearerTokenMiddleware, ModerationActionInput
from src.editorial.models.enums import ReviewDecision, SubmissionStatus
from src.editorial.models.mcp_moderation import McpModerationAction
from src.editorial.services.mcp_moderation import (
    MCP_MODERATION_SOURCE,
    McpModerationService,
    ModerationRequest,
)


def _request(
    *,
    submission_id: int = 10,
    decision: str = "approve",
    reason: str = "Подходит правилам канала",
    expected_status: SubmissionStatus = SubmissionStatus.NEW,
) -> ModerationRequest:
    return ModerationRequest(
        submission_id=submission_id,
        decision=decision,
        reason=reason,
        expected_status=expected_status,
    )


def _submission(submission_id: int = 10):
    return SimpleNamespace(
        id=submission_id,
        channel_id=3,
        status=SubmissionStatus.NEW,
        moderator_note=None,
        reviewed_at=None,
    )


def test_batch_validation_rejects_duplicate_submission_ids():
    service = McpModerationService(write_enabled=True, max_batch_size=20)

    with pytest.raises(ValueError, match="only once"):
        service._validate_batch(
            "batch-1",
            [_request(submission_id=10), _request(submission_id=10, decision="reject")],
        )


def test_batch_validation_requires_pending_expected_status():
    service = McpModerationService(write_enabled=True)
    action = _request()
    object.__setattr__(action, "expected_status", SubmissionStatus.REJECTED)

    with pytest.raises(ValueError, match="new or hold"):
        service._validate_batch("batch-2", [action])


@pytest.mark.asyncio
async def test_approve_uses_atomic_moderation_calls_and_ai_source():
    service = McpModerationService(write_enabled=True, actor_id=0)
    submission = _submission()
    related = [submission]
    content_item = SimpleNamespace(id=77)
    service.moderation.create_content_from_submission = AsyncMock(return_value=content_item)
    service.moderation.review_content_item = AsyncMock(return_value=content_item)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        flush=AsyncMock(),
    )

    content_item_id = await service._apply_decision(
        session=session,
        submission=submission,
        related=related,
        action=_request(),
    )

    assert content_item_id == 77
    create_kwargs = service.moderation.create_content_from_submission.await_args.kwargs
    assert create_kwargs["commit"] is False
    review_kwargs = service.moderation.review_content_item.await_args.kwargs
    assert review_kwargs["commit"] is False
    assert review_kwargs["reviewer_id"] == 0
    assert review_kwargs["decision"] == ReviewDecision.APPROVE
    assert review_kwargs["moderation_source"] == MCP_MODERATION_SOURCE
    assert submission.status == SubmissionStatus.CONTENT_CREATED
    assert submission.moderator_note.startswith("Codex MCP:")
    session.flush.assert_awaited()
    content_query = str(session.scalar.await_args.args[0])
    assert "content_items.template_key IS NULL" in content_query
    assert "content_items.template_key !=" in content_query


@pytest.mark.asyncio
async def test_reject_uses_ai_source_without_committing_inside_helper():
    service = McpModerationService(write_enabled=True, actor_id=0)
    submission = _submission()
    service.moderation.set_submission_status = AsyncMock(return_value=submission)
    service.moderation_cases.record_submission_decision = AsyncMock()
    session = SimpleNamespace(flush=AsyncMock())

    result = await service._apply_decision(
        session=session,
        submission=submission,
        related=[submission],
        action=_request(decision="reject", reason="Явная реклама"),
    )

    assert result is None
    status_kwargs = service.moderation.set_submission_status.await_args.kwargs
    assert status_kwargs["status"] == SubmissionStatus.REJECTED
    assert status_kwargs["commit"] is False
    case_kwargs = service.moderation_cases.record_submission_decision.await_args.kwargs
    assert case_kwargs["source"] == MCP_MODERATION_SOURCE
    assert case_kwargs["moderator_id"] == 0


@pytest.mark.asyncio
async def test_hold_voids_payable_case():
    service = McpModerationService(write_enabled=True, actor_id=0)
    submission = _submission()
    service.moderation.set_submission_status = AsyncMock(return_value=submission)
    service.moderation_cases.void_submission_case = AsyncMock()
    session = SimpleNamespace(flush=AsyncMock())

    result = await service._apply_decision(
        session=session,
        submission=submission,
        related=[submission],
        action=_request(decision="hold", reason="Нужно проверить фотографию"),
    )

    assert result is None
    status_kwargs = service.moderation.set_submission_status.await_args.kwargs
    assert status_kwargs["status"] == SubmissionStatus.HOLD
    assert status_kwargs["commit"] is False
    void_kwargs = service.moderation_cases.void_submission_case.await_args.kwargs
    assert void_kwargs["source"] == MCP_MODERATION_SOURCE


@pytest.mark.asyncio
async def test_advertising_sets_terminal_status_and_voids_payable_case():
    service = McpModerationService(write_enabled=True, actor_id=0)
    submission = _submission()
    service.moderation.set_submission_status = AsyncMock(return_value=submission)
    service.moderation_cases.void_submission_case = AsyncMock()
    session = SimpleNamespace(flush=AsyncMock())

    result = await service._apply_decision(
        session=session,
        submission=submission,
        related=[submission],
        action=_request(
            decision="advertising",
            reason="Коммерческое предложение для размещения",
        ),
    )

    assert result is None
    status_kwargs = service.moderation.set_submission_status.await_args.kwargs
    assert status_kwargs["status"] == SubmissionStatus.ADVERTISING
    assert status_kwargs["commit"] is False
    void_kwargs = service.moderation_cases.void_submission_case.await_args.kwargs
    assert void_kwargs["source"] == MCP_MODERATION_SOURCE
    assert void_kwargs["action"] == "advertise_submission"


@pytest.mark.asyncio
async def test_advertising_telegram_side_effects_reuse_existing_flow():
    actions = SimpleNamespace(
        send_submission_advertising_reply_v2=AsyncMock(),
        sync_panel_submission_agent_advertising=AsyncMock(return_value=2),
    )

    sync_count = await McpModerationService._apply_telegram_side_effects(
        actions,
        submission_id=15,
        decision="advertising",
    )

    assert sync_count == 2
    actions.send_submission_advertising_reply_v2.assert_awaited_once_with(15)
    actions.sync_panel_submission_agent_advertising.assert_awaited_once_with(15)


def test_operation_idempotency_requires_exact_same_arguments():
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    operation = McpModerationAction(
        id=1,
        request_id="batch-3:1",
        batch_id="batch-3",
        requested_submission_id=10,
        submission_id=10,
        channel_id=3,
        actor_id=0,
        decision="approve",
        reason="Подходит правилам канала",
        dry_run=False,
        expected_status="new",
        previous_status="new",
        resulting_status="content_created",
        outcome="applied",
        content_item_id=77,
        legacy_sync_count=1,
        created_at=now,
        completed_at=now,
    )

    assert McpModerationService._operation_matches(operation, _request(), False)
    assert not McpModerationService._operation_matches(
        operation,
        _request(decision="reject"),
        False,
    )
    assert not McpModerationService._operation_matches(operation, _request(), True)


def test_mcp_input_rejects_unknown_fields_and_strips_reason():
    item = ModerationActionInput.model_validate(
        {
            "submission_id": 10,
            "decision": "hold",
            "reason": "  Нужна проверка  ",
            "expected_status": "new",
        }
    )
    assert item.reason == "Нужна проверка"

    advertising = ModerationActionInput.model_validate(
        {
            "submission_id": 11,
            "decision": "advertising",
            "reason": "Запрос на размещение рекламы",
            "expected_status": "new",
        }
    )
    assert advertising.decision == "advertising"

    with pytest.raises(ValueError):
        ModerationActionInput.model_validate(
            {
                "submission_id": 10,
                "decision": "approve",
                "reason": "Подходит",
                "expected_status": "new",
                "publish_now": True,
            }
        )


async def _invoke_middleware(configured_token: str | None, presented_token: str | None, path="/mcp"):
    calls = []

    async def wrapped(scope, receive, send):
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = BearerTokenMiddleware(wrapped, configured_token)
    headers = []
    if presented_token is not None:
        headers.append((b"authorization", f"Bearer {presented_token}".encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers,
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await middleware(scope, receive, send)
    status = next(item["status"] for item in messages if item["type"] == "http.response.start")
    return status, calls


@pytest.mark.asyncio
async def test_bearer_middleware_rejects_missing_or_wrong_token():
    token = "s" * 32
    missing_status, missing_calls = await _invoke_middleware(token, None)
    wrong_status, wrong_calls = await _invoke_middleware(token, "wrong")

    assert missing_status == 401
    assert wrong_status == 401
    assert missing_calls == []
    assert wrong_calls == []


@pytest.mark.asyncio
async def test_bearer_middleware_accepts_exact_token_and_health_is_safe():
    token = "s" * 32
    accepted_status, accepted_calls = await _invoke_middleware(token, token)
    health_status, health_calls = await _invoke_middleware(token, None, path="/health")
    unconfigured_status, _ = await _invoke_middleware(None, None)

    assert accepted_status == 204
    assert len(accepted_calls) == 1
    assert health_status == 200
    assert health_calls == []
    assert unconfigured_status == 503

    weak_status, _ = await _invoke_middleware("too-short", "too-short")
    assert weak_status == 503
