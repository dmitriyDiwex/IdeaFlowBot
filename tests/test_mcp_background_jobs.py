import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from test_moderation_batch import batch_env, seed, decision, START  # shared isolated DB fixture
from src.editorial.config import DEFAULT_MCP_EXCLUDED_BOTS, settings
from src.editorial.models.enums import SubmissionStatus
from src.editorial.models.mcp_moderation import McpModerationAction, McpModerationJob, McpModerationSnapshot
from src.editorial.models.submission import Submission
from src.editorial.services.mcp_moderation import ModerationRequest
from src.editorial.services.moderation_batch import ModerationBatchService
from src.editorial.services.moderation_worker import ModerationJobWorker
from src.editorial.services.telegram_resilience import TelegramAPIError


async def queue(env, count=1, *, decisions=None, rows=None, changes=None):
    preview = await env.service.prepare_moderation_batch(limit=count)
    proposed = decisions or [decision(i) for i in range(1, count+1)]
    await env.service.save_moderation_batch_draft(snapshot_id=preview["snapshot_id"], proposals=proposed)
    await env.service.enqueue_moderation_batch(snapshot_id=preview["snapshot_id"],
                                              rows=rows, user_changes=changes, user_confirmed=True)
    return preview


def worker(env):
    return ModerationJobWorker(env.service, concurrency=1, budget_seconds=120)


def test_profile_contains_all_67_user_exclusions():
    names = DEFAULT_MCP_EXCLUDED_BOTS.split(",")
    assert len(names) == len(set(names)) == 67
    assert {"mpgusegodnya_bot", "psniu_podsl_bot", "confession_lovepavlovaa", "misisforever_bot"} <= set(names)


@pytest.mark.asyncio
async def test_prepare_300_is_one_global_compact_snapshot_without_moderation(batch_env):
    await seed(batch_env, [{"channel_id": i % 3 + 1, "text": "x"*400+str(i)} for i in range(305)])
    preview = await batch_env.service.prepare_moderation_batch(limit=300, compact=True)
    assert preview["selected_count"] == len(preview["rows"]) == 300
    assert preview["rows"][0]["row"] == 1 and preview["rows"][-1]["row"] == 300
    assert all(len(row["text_preview"]) <= 300 and row["text_truncated"] for row in preview["rows"])
    assert all("submission_id" not in row and "author" not in row for row in preview["rows"])
    full = await batch_env.service.get_moderation_message(preview["snapshot_id"], 1)
    assert len(full["untrusted_text"]) > 300 and "author" not in full
    async with batch_env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(McpModerationAction)) == 0


@pytest.mark.asyncio
async def test_enqueue_requires_confirmation_and_does_not_call_telegram(batch_env):
    await seed(batch_env, [{}, {}])
    preview = await batch_env.service.prepare_moderation_batch()
    await batch_env.service.save_moderation_batch_draft(
        snapshot_id=preview["snapshot_id"], proposals=[decision(1), decision(2)],
    )
    with pytest.raises(PermissionError):
        await batch_env.service.enqueue_moderation_batch(snapshot_id=preview["snapshot_id"])
    result = await batch_env.service.enqueue_moderation_batch(
        snapshot_id=preview["snapshot_id"], user_changes=[decision(2, "reject", "user_override")], user_confirmed=True,
    )
    assert result["state"] == "queued" and result["pending"] == 2
    assert "rows" not in result and "exceptions" not in result
    async with batch_env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(McpModerationAction)) == 0
        assert all(item.status == SubmissionStatus.NEW for item in await session.scalars(select(Submission)))
    batch_env.checked.assert_not_awaited()
    result = await worker(batch_env).run_once()
    assert result["state"] == "completed" and result["telegram_cards_verified"] == 2
    assert result["decision_counts"] == {"approve": 1, "reject": 1}
    # Repeated confirmation/polling cannot execute the job twice.
    await batch_env.service.enqueue_moderation_batch(snapshot_id=preview["snapshot_id"], user_confirmed=True)
    assert await worker(batch_env).run_once() is None
    assert (await batch_env.service.get_moderation_batch(preview["snapshot_id"]))["state"] == "applied"
    assert batch_env.checked.await_count == 2


@pytest.mark.asyncio
async def test_worker_300_items_and_status_is_small(batch_env):
    await seed(batch_env, [{"channel_id": i % 30 + 1} for i in range(300)], channel_count=30)
    preview = await queue(batch_env, 300)
    result = await worker(batch_env).run_once()
    assert result["state"] == "completed"
    assert result["applied"] == result["verified"] == result["telegram_cards_verified"] == 300
    assert len(str(result)) < 2200
    batch_env.now[0] += timedelta(hours=25)
    assert (await batch_env.service.get_moderation_batch_status(preview["snapshot_id"]))["applied"] == 300


@pytest.mark.asyncio
async def test_partial_authorization_can_only_expand_with_another_confirmation(batch_env):
    await seed(batch_env, [{}, {}, {}, {}])
    preview = await queue(batch_env, 4, rows=[1, 2])
    result = await worker(batch_env).run_once()
    assert result["applied"] == 2 and result["remaining_unconfirmed_rows"] == 2
    assert await worker(batch_env).run_once() is None
    with pytest.raises(PermissionError):
        await batch_env.service.enqueue_moderation_batch(snapshot_id=preview["snapshot_id"], rows=[3, 4])
    await batch_env.service.enqueue_moderation_batch(snapshot_id=preview["snapshot_id"], rows=[3, 4], user_confirmed=True)
    assert (await worker(batch_env).run_once())["applied"] == 4
    assert batch_env.checked.await_count == 4
    with pytest.raises(ValueError, match="immutable"):
        await batch_env.service.enqueue_moderation_batch(
            snapshot_id=preview["snapshot_id"], user_changes=[decision(1, "reject")], user_confirmed=True,
        )


@pytest.mark.asyncio
async def test_worker_restart_retries_card_but_never_advertising_send(batch_env):
    await seed(batch_env, [{}])
    preview = await queue(batch_env, decisions=[decision(1, "advertising", "advertising")])
    batch_env.advertising.side_effect = TimeoutError("sendMessage timed out")
    batch_env.checked.side_effect = TimeoutError("editMessageReplyMarkup timed out")
    first = await worker(batch_env).run_once()
    assert first["state"] == "waiting"
    assert first["advertising_delivery_warnings"] == [1]
    assert await worker(batch_env).run_once() is None
    batch_env.now[0] += timedelta(seconds=6)
    batch_env.checked.side_effect = None
    batch_env.service = ModerationBatchService(session_maker=batch_env.factory, write_enabled=True,
                                              clock=lambda: batch_env.now[0])
    final = await worker(batch_env).run_once()
    assert final["state"] == "completed_with_warnings"
    assert final["telegram_cards_verified"] == 1 and final["advertising_delivery_warnings"] == [1]
    batch_env.advertising.assert_awaited_once()
    assert (await batch_env.service.get_moderation_batch_status(preview["snapshot_id"]))["pending"] == 0
    async with batch_env.factory() as session:
        action = await session.scalar(select(McpModerationAction))
        assert action.advertising_reply_state == "uncertain" and action.advertising_warning_text


@pytest.mark.asyncio
async def test_sliding_window_blocks_only_same_channel_and_continues_automatically(batch_env):
    await seed(batch_env, [{} for _ in range(21)] + [{"channel_id": 2}])
    await queue(batch_env, 22)
    first = await worker(batch_env).run_once()
    assert first["applied"] == 21 and first["pending"] == 1 and first["state"] == "waiting"
    batch_env.now[0] += timedelta(seconds=59)
    assert await worker(batch_env).run_once() is None
    batch_env.now[0] += timedelta(seconds=1)
    assert (await worker(batch_env).run_once())["applied"] == 22
    assert batch_env.checked.await_count == 22


@pytest.mark.asyncio
async def test_no_human_decision_overwrite_after_approval(batch_env):
    await seed(batch_env, [{}])
    await queue(batch_env)
    async with batch_env.factory() as session:
        item = await session.get(Submission, 1)
        item.status = SubmissionStatus.REJECTED
        item.moderator_note = "Human moderator"
        item.reviewed_at = START
        await session.commit()
    final = await worker(batch_env).run_once()
    assert final["conflicts"] == 1 and final["applied"] == 0
    batch_env.checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_permanent_errors_are_not_hammered_and_429_backoff_is_preserved(batch_env):
    await seed(batch_env, [{}, {}])
    await queue(batch_env, 2)
    batch_env.checked.side_effect = [
        ValueError("400 Bad Request: message to edit not found"),
        TelegramAPIError({"error_code": 429, "parameters": {"retry_after": 90}}),
    ]
    assert (await worker(batch_env).run_once())["state"] == "waiting"
    batch_env.now[0] += timedelta(seconds=89)
    assert await worker(batch_env).run_once() is None
    batch_env.now[0] += timedelta(seconds=1)
    batch_env.checked.side_effect = None
    result = await worker(batch_env).run_once()
    assert result["state"] == "completed_with_warnings" and result["telegram_cards_verified"] == 1
    assert batch_env.checked.await_count == 3


@pytest.mark.asyncio
async def test_job_claim_lease_restart_and_write_gate(batch_env):
    await seed(batch_env, [{}])
    await queue(batch_env)
    batch_env.service.write_enabled = False
    assert await worker(batch_env)._claim() is None
    batch_env.service.write_enabled = True
    first, second = await asyncio.gather(worker(batch_env)._claim(), worker(batch_env)._claim())
    claim = first or second
    assert (first is None) != (second is None)
    batch_env.now[0] += timedelta(seconds=121)
    new = await worker(batch_env)._claim()
    assert new[1] != claim[1]
    assert not await worker(batch_env)._renew(claim[0], claim[1])


@pytest.mark.asyncio
async def test_worker_cancellation_makes_ad_delivery_uncertain_and_recoverable(batch_env):
    await seed(batch_env, [{}])
    await queue(batch_env, decisions=[decision(1, "advertising", "advertising")])
    started = asyncio.Event()

    async def hung(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    batch_env.advertising.side_effect = hung
    task = asyncio.create_task(worker(batch_env).run_once())
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    batch_env.now[0] += timedelta(seconds=6)
    assert (await worker(batch_env).run_once())["telegram_cards_verified"] == 1
    batch_env.advertising.assert_awaited_once()


@pytest.mark.asyncio
async def test_managed_dispatch_recovers_at_bounded_deadline_not_10_minutes(batch_env):
    await seed(batch_env, [{}])
    preview = await queue(batch_env, decisions=[decision(1, "reject")])
    snapshot_id = preview["snapshot_id"]
    await batch_env.service._apply_one(
        request_id=f"snapshot-{snapshot_id}:1", batch_id=f"snapshot-{snapshot_id}",
        action=ModerationRequest(1, "reject", "Допустимое осмысленное сообщение", SubmissionStatus.NEW),
        dry_run=False, defer_telegram=True,
    )
    async with batch_env.factory() as session:
        item = await session.scalar(select(McpModerationAction))
        item.telegram_sync_state = "dispatching"
        item.dispatch_deadline_at = batch_env.now[0] + timedelta(seconds=30)
        await session.commit()
    first = await worker(batch_env).run_once()
    assert first["state"] == "waiting"
    batch_env.now[0] += timedelta(seconds=31)
    assert (await worker(batch_env).run_once())["telegram_cards_verified"] == 1


def test_background_migration_offline_is_additive_and_does_not_enqueue_old_work():
    from alembic import command
    from alembic.config import Config
    from io import StringIO
    output = StringIO()
    command.upgrade(Config("alembic.ini", output_buffer=output), "20261017_29:20261020_30", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE mcp_moderation_jobs" in sql and "advertising_reply_state" in sql
    assert "UPDATE submissions" not in sql and "INSERT INTO mcp_moderation_jobs" not in sql


@pytest.mark.asyncio
async def test_mcp_api_uses_saved_draft_and_row_override_without_resending_all_decisions(batch_env, monkeypatch):
    from src.editorial.mcp import server
    monkeypatch.setattr(server, "service", batch_env.service)
    await seed(batch_env, [{}, {}])
    preview = await server.prepare_moderation_batch(limit=2)
    assert "submission_id" not in preview["rows"][0]
    snapshot_id = preview["snapshot_id"]
    await server.save_moderation_batch_draft(
        snapshot_id=snapshot_id, proposals=[server.SnapshotDecisionInput(**decision(i)) for i in [1, 2]],
    )
    result = await server.commit_moderation_batch(
        snapshot_id=snapshot_id, user_confirmed=True,
        user_changes=[server.SnapshotDecisionInput(**decision(2, "reject", "user_override"))],
    )
    assert result["state"] == "queued"
    batch_env.checked.assert_not_awaited()
    await worker(batch_env).run_once()
    result = await server.get_moderation_batch(snapshot_id)
    assert "rows" not in result and result["applied"] == 2


@pytest.mark.asyncio
async def test_exhausted_card_retries_stop_with_explicit_warning(batch_env, monkeypatch):
    monkeypatch.setattr(settings, "mcp_card_retry_attempts", 2)
    await seed(batch_env, [{}])
    await queue(batch_env)
    batch_env.checked.side_effect = TimeoutError("Network timed out")
    assert (await worker(batch_env).run_once())["state"] == "waiting"
    batch_env.now[0] += timedelta(seconds=6)
    assert (await worker(batch_env).run_once())["state"] == "waiting"
    batch_env.now[0] += timedelta(seconds=11)
    result = await worker(batch_env).run_once()
    assert result["state"] == "completed_with_warnings"
    assert result["telegram_cards_pending"] == 1
    assert await worker(batch_env).run_once() is None
    assert batch_env.checked.await_count == 3


@pytest.mark.asyncio
async def test_expired_unconfirmed_snapshot_is_not_enqueued(batch_env):
    await seed(batch_env, [{}])
    preview = await batch_env.service.prepare_moderation_batch()
    batch_env.now[0] += timedelta(hours=25)
    with pytest.raises(ValueError, match="expired"):
        await batch_env.service.enqueue_moderation_batch(
            snapshot_id=preview["snapshot_id"], decisions=[decision(1)], user_confirmed=True,
        )
    async with batch_env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(McpModerationJob)) == 0


@pytest.mark.asyncio
async def test_budget_cannot_mark_committed_but_undispatched_cards_completed(batch_env, monkeypatch):
    await seed(batch_env, [{}])
    preview = await queue(batch_env)
    snapshot_id = preview["snapshot_id"]
    await batch_env.service._apply_one(
        request_id=f"snapshot-{snapshot_id}:1", batch_id=f"snapshot-{snapshot_id}",
        action=ModerationRequest(1, "approve", "Допустимое осмысленное сообщение", SubmissionStatus.NEW),
        dry_run=False, defer_telegram=True,
    )
    paused = worker(batch_env)
    monkeypatch.setattr(paused, "_process", AsyncMock(return_value=[]))
    assert (await paused.run_once())["state"] == "waiting"
    batch_env.now[0] += timedelta(seconds=2)
    assert (await worker(batch_env).run_once())["state"] == "completed"


@pytest.mark.asyncio
async def test_hold_and_skip_do_not_require_processed_telegram_cards(batch_env):
    await seed(batch_env, [{}, {}])
    await queue(batch_env, 2, decisions=[decision(1, "hold", "manual_review"), decision(2, "skip")])
    result = await worker(batch_env).run_once()
    assert result["state"] == "completed" and result["applied"] == 1 and result["skipped"] == 1
    assert result["telegram_cards_pending"] == result["telegram_cards_verified"] == 0
    batch_env.checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_saved_user_changes_are_not_lost_by_a_later_small_override(batch_env):
    await seed(batch_env, [{}, {}])
    preview = await batch_env.service.prepare_moderation_batch()
    await batch_env.service.save_moderation_batch_draft(
        snapshot_id=preview["snapshot_id"], proposals=[decision(1), decision(2)],
        user_changes=[decision(1, "reject", "user_override")],
    )
    await batch_env.service.enqueue_moderation_batch(
        snapshot_id=preview["snapshot_id"], user_changes=[decision(2, "reject", "user_override")], user_confirmed=True,
    )
    assert (await worker(batch_env).run_once())["decision_counts"] == {"reject": 2}


@pytest.mark.asyncio
async def test_successful_explicit_card_recovery_clears_derived_job_warning(batch_env):
    await seed(batch_env, [{}])
    preview = await queue(batch_env)
    batch_env.checked.side_effect = ValueError("400 Bad Request: missing card")
    assert (await worker(batch_env).run_once())["state"] == "completed_with_warnings"
    async with batch_env.factory() as session:
        operation = await session.scalar(select(McpModerationAction))
        operation.telegram_sync_state = "verified"
        operation.legacy_sync_count = 1
        await session.commit()
    final = await batch_env.service.get_moderation_batch_status(preview["snapshot_id"])
    assert final["state"] == "completed" and final["exception_count"] == 0
