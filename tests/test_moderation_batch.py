import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.editorial.db.base import EditorialBase
from src.editorial.mcp.server import mcp, SERVER_INSTRUCTIONS
from src.editorial.models.channel import Channel
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import SubmissionStatus
from src.editorial.models.mcp_moderation import McpModerationAction, McpModerationSnapshot
from src.editorial.models.moderation_case import ModerationCase, ModerationCaseEvent
from src.editorial.models.submission import Submission
from src.editorial.services.mcp_moderation import McpModerationService, ModerationRequest
from src.editorial.services.moderation_batch import ModerationBatchService, REASON_CODES
from src.editorial.services.moderation_playbook import ModerationPlaybook, PLAYBOOK_PATH, PLAYBOOK_URI, playbook
from src.editorial.utils.text import compute_moderation_hash, compute_text_hash


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_, compiler, **kw):
    return "JSON"


START = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
async def batch_env(tmp_path, monkeypatch):
    test_dsn = os.getenv("IDEAFLOW_MCP_TEST_DSN")
    schema = None
    if test_dsn:
        url = make_url(test_dsn)
        if (url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}
                or not (url.database or "").startswith("ideaflow_mcp_test")):
            raise ValueError("PostgreSQL tests require a loopback host and an ideaflow_mcp_test database")
        schema = "mcp_test_" + uuid.uuid4().hex
        engine = create_async_engine(test_dsn, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'moderation.db'}")
    async with engine.begin() as connection:
        if schema:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.run_sync(EditorialBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = [START]
    checked = AsyncMock(return_value=1)
    advertising = AsyncMock()
    actions = SimpleNamespace(
        legacy_moderation_sync=SimpleNamespace(sync_panel_submission_agent_checked=checked),
        send_submission_advertising_reply_v2=advertising,
    )
    monkeypatch.setattr("src.editorial.services.telegram_actions.TelegramEditorialActions", lambda: actions)
    service = ModerationBatchService(session_maker=factory, write_enabled=True, clock=lambda: now[0])
    yield SimpleNamespace(factory=factory, service=service, now=now, checked=checked,
                          advertising=advertising, engine=engine)
    try:
        if schema:
            # This UUID schema was created by this fixture in a dedicated local test database.
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        await engine.dispose()


async def seed(env, rows, *, channel_count=3):
    async with env.factory() as session:
        session.add_all([Channel(
            id=index, tg_channel_id=-1000-index, short_code=f"queue_{index}", title=f"Queue {index}",
            is_active=True,
        ) for index in range(1, channel_count+1)])
        await session.flush()
        for index, values in enumerate(rows, 1):
            values = dict(values)
            text = values.pop("text", f"Осмысленный вопрос номер {index}")
            session.add(Submission(
                id=index, channel_id=values.pop("channel_id", 1),
                created_at=values.pop("created_at", START - timedelta(days=10) + timedelta(seconds=index)),
                raw_text=text, cleaned_text=text, text_hash=compute_text_hash(text),
                legacy_source="test", **values,
            ))
        await session.commit()


def decision(row, value="approve", code="ok"):
    return {"row": row, "decision": value, "reason_code": code}


async def commit(env, snapshot, decisions):
    return await env.service.commit_moderation_batch(
        snapshot_id=snapshot["snapshot_id"], decisions=decisions, user_confirmed=True,
    )


def test_playbook_bytes_metadata_and_sections():
    assert playbook.content == PLAYBOOK_PATH.read_bytes().decode("utf-8")
    assert playbook.sha256 == hashlib.sha256(PLAYBOOK_PATH.read_bytes()).hexdigest()
    assert playbook.get()["section"] == "core"
    assert "## 11." not in playbook.get()["content"]
    for section, heading in [("core", "## 1."), ("decisions", "## 6."),
                             ("examples", "Исторический пример"), ("workflow", "## 12.")]:
        assert heading in playbook.get(section)["content"]
    assert playbook.get("all")["content_type"] == "text/markdown"
    assert "свежие указания пользователя важнее" in SERVER_INSTRUCTIONS


def test_archived_version_survives_new_release(tmp_path):
    current = tmp_path / "MODERATION_AGENT_PLAYBOOK.md"
    current.write_bytes(PLAYBOOK_PATH.read_bytes())
    archives = tmp_path / "moderation_playbooks"
    archives.mkdir()
    (archives / f"{playbook.version}.md").write_bytes(current.read_bytes())
    old = ModerationPlaybook(current)
    current.write_bytes(current.read_bytes().replace(b"1.0.0-draft", b"1.0.1-draft"))
    new = ModerationPlaybook(current)
    assert new.archives[old.version] == old.content
    # Editing a released version without a bump is rejected at startup.
    current.write_bytes(PLAYBOOK_PATH.read_bytes() + b"\nchanged")
    with pytest.raises(ValueError, match="immutable"):
        ModerationPlaybook(current)


@pytest.mark.asyncio
async def test_mcp_catalog_preserves_old_api_and_readonly_resources():
    resources = await mcp.list_resources()
    current = next(item for item in resources if str(item.uri) == PLAYBOOK_URI)
    assert current.mimeType == "text/markdown"
    content = await mcp.read_resource(PLAYBOOK_URI)
    assert list(content)[0].content == playbook.content
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert {
        "list_proposal_queues", "list_pending_submissions", "get_submission",
        "list_human_moderation_examples", "apply_moderation_batch", "verify_moderation_batch",
        "prepare_moderation_batch", "commit_moderation_batch", "list_pending_summary",
        "save_moderation_batch_draft", "get_moderation_batch", "get_moderation_playbook",
    } <= tools.keys()
    assert tools["commit_moderation_batch"].annotations.destructiveHint
    assert not tools["prepare_moderation_batch"].annotations.readOnlyHint
    assert tools["commit_moderation_batch"].inputSchema["properties"]["user_confirmed"]["default"] is False


@pytest.mark.asyncio
async def test_global_oldest_sql_exclusions_media_refill_and_no_author(batch_env):
    rows = [{"channel_id": index % 3 + 1, "text": "x" * 500 + str(index)}
            for index in range(170)]
    rows[:10] = [{"channel_id": 1, "content_type": "photo", "media_group_id": "album"} for _ in range(10)]
    await seed(batch_env, rows)
    async with batch_env.factory() as session:
        excluded = await session.get(Channel, 3)
        excluded.short_code = "@MiSiSfOrEvEr_BoT"
        await session.commit()
    snapshot = await batch_env.service.prepare_moderation_batch(limit=100)
    assert snapshot["selected_count"] == 100
    assert snapshot["skipped_media_count"] == 1
    assert snapshot["rows"][0]["submission_id"] == 11
    assert {row["channel"] for row in snapshot["rows"]} == {"queue_1", "queue_2"}
    assert all(len(row["text_preview"]) <= 300 for row in snapshot["rows"])
    assert all("author" not in row and "username" not in row for row in snapshot["rows"])
    assert "content" not in snapshot and playbook.content not in json.dumps(snapshot, ensure_ascii=False)
    async with batch_env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Submission).where(
            Submission.status != SubmissionStatus.NEW,
        )) == 0
        assert await session.scalar(select(func.count()).select_from(McpModerationAction)) == 0
    restored_service = ModerationBatchService(session_maker=batch_env.factory)
    assert (await restored_service.get_moderation_batch(snapshot["snapshot_id"]))["rows"] == snapshot["rows"]


@pytest.mark.asyncio
async def test_sql_exclusion_uses_bot_binding_even_with_different_short_code(batch_env):
    await seed(batch_env, [{"bot_username": "@LoVeSpOlIsPb_BoT"}, {"channel_id": 2}])
    result = await batch_env.service.prepare_moderation_batch()
    assert [row["channel"] for row in result["rows"]] == ["queue_2"]


@pytest.mark.asyncio
async def test_album_collapsed_before_limit_and_null_chat_not_merged(batch_env):
    await seed(batch_env, [
        {"media_group_id": "a", "content_type": "photo", "text": ""},
        {"media_group_id": "a", "content_type": "photo", "text": "Подпись"},
        {"media_group_id": "a", "source_chat_id": 123, "content_type": "photo"},
        {}, {},
    ])
    snapshot = await batch_env.service.prepare_moderation_batch(media="include", limit=4)
    assert [row["submission_id"] for row in snapshot["rows"]] == [1, 3, 4, 5]
    assert snapshot["rows"][0]["text_preview"] == "Подпись"
    assert snapshot["rows"][0]["media_item_count"] == 2
    assert snapshot["rows"][1]["media_item_count"] == 1


@pytest.mark.asyncio
async def test_summary_has_no_text_sorted_counts_hold_optin(batch_env):
    await seed(batch_env, [
        {}, {"channel_id": 2}, {"channel_id": 2}, {"status": SubmissionStatus.HOLD},
        {"content_type": "photo"}, {"content_type": "photo", "media_group_id": "g"},
        {"content_type": "photo", "media_group_id": "g"}, {"source_chat_id": -100},
    ])
    result = await batch_env.service.list_pending_summary(include_hold_count=True, include_media_count=True)
    assert result["total_pending"] == 3
    assert result["queues"][0] == {"channel": "queue_2", "pending_count": 2}
    assert result["excluded_media_count"] == 2 and result["hold_count"] == 1
    assert "text" not in json.dumps(result)
    held = await batch_env.service.prepare_moderation_batch(include_hold=True)
    assert held["selected_count"] == 4


@pytest.mark.asyncio
async def test_duplicate_hashes_history_and_url_distinction(batch_env):
    await seed(batch_env, [
        {"text": "Когда заселение?", "status": SubmissionStatus.CONTENT_CREATED},
        {"text": "Когда заселение?"}, {"text": "Когда заселение!!!"},
        {"text": "Когда заселение?", "channel_id": 2},
        {"text": "Смотрите https://t.me/first"}, {"text": "Смотрите https://t.me/second"},
        {"text": "Несвязанный опрос"},
    ])
    async with batch_env.factory() as session:
        session.add_all([
            ModerationCase(case_key="submission:1", canonical_submission_id=1, channel_id=1,
                           message_text="Когда заселение?", moderator_id=99, decision="approved",
                           source="panel", action="approve", decided_at=START, finalized_at=START),
            ModerationCase(case_key="submission:7", canonical_submission_id=7, channel_id=1,
                           message_text="Несвязанный опрос", moderator_id=99, decision="rejected",
                           source="panel", action="reject", decided_at=START, finalized_at=START),
        ])
        await session.commit()
    snapshot = await batch_env.service.prepare_moderation_batch()
    rows = {row["submission_id"]: row for row in snapshot["rows"]}
    assert rows[2]["duplicate"] == {"submission_id": 1, "kind": "exact", "status": "content_created"}
    assert rows[3]["duplicate"]["kind"] == "normalized"
    assert rows[2]["relevant_history"] == [{"decision": "approved", "text_preview": "Когда заселение?"}]
    assert rows[4]["duplicate"] is None
    assert rows[6]["duplicate"] is None
    assert all("Несвязанный" not in str(row["relevant_history"]) for row in rows.values())
    assert compute_moderation_hash("Привет?") == compute_moderation_hash("Привет!!!")


@pytest.mark.asyncio
async def test_known_susu_duplicates_skipped_without_changing_status(batch_env):
    await seed(batch_env, [{"text": "Когда домой поедешь?"} for _ in range(3)] + [{}])
    async with batch_env.factory() as session:
        channel = await session.get(Channel, 1)
        channel.title = "ЮУрГУ"
        await session.commit()
    result = await batch_env.service.prepare_moderation_batch()
    assert [row["submission_id"] for row in result["rows"]] == [1, 4]


@pytest.mark.asyncio
async def test_commit_explicit_confirmation_write_gate_and_draft_roundtrip(batch_env):
    await seed(batch_env, [{}, {}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    args = dict(snapshot_id=snapshot["snapshot_id"], decisions=[decision(1), decision(2)])
    with pytest.raises(PermissionError, match="confirmation"):
        await batch_env.service.commit_moderation_batch(**args)
    batch_env.service.write_enabled = False
    with pytest.raises(PermissionError, match="disabled"):
        await batch_env.service.commit_moderation_batch(**args, user_confirmed=True)
    batch_env.service.write_enabled = True
    await batch_env.service.save_moderation_batch_draft(
        snapshot_id=snapshot["snapshot_id"], proposals=args["decisions"],
        user_changes=[decision(2, "reject", "survey")],
    )
    restored = await batch_env.service.get_moderation_batch(snapshot["snapshot_id"])
    assert restored["user_changes"] == [decision(2, "reject", "survey")]
    result = await commit(batch_env, snapshot, [decision(1), decision(2, "reject", "survey")])
    assert result["applied"] == result["verified"] == 2
    assert result["telegram_sync_warnings"] == 0
    async with batch_env.factory() as session:
        operation = await session.scalar(select(McpModerationAction).where(McpModerationAction.decision == "reject"))
        assert operation.reason == REASON_CODES["survey"]
        saved = await session.get(McpModerationSnapshot, snapshot["snapshot_id"])
        assert saved.proposals == args["decisions"]
        assert saved.user_changes == [decision(2, "reject", "survey")]


@pytest.mark.asyncio
async def test_idempotency_changed_request_and_restart(batch_env):
    await seed(batch_env, [{}, {}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    decisions = [decision(1), decision(2, "advertising", "advertising")]
    first = await commit(batch_env, snapshot, decisions)
    batch_env.service = ModerationBatchService(session_maker=batch_env.factory, write_enabled=True,
                                               clock=lambda: batch_env.now[0])
    replay = await commit(batch_env, snapshot, list(reversed(decisions)))
    assert first == replay
    batch_env.advertising.assert_awaited_once()
    assert batch_env.checked.await_count == 2
    with pytest.raises(ValueError, match="immutable"):
        await commit(batch_env, snapshot, [decision(1, "reject"), decisions[1]])
    async with batch_env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(ContentItem)) == 1
        assert await session.scalar(select(func.count()).select_from(McpModerationAction)) == 2


@pytest.mark.asyncio
async def test_conflicts_human_hold_same_status_text_change_and_album_change(batch_env):
    await seed(batch_env, [
        {}, {}, {}, {"media_group_id": "a", "content_type": "photo"},
        {"media_group_id": "a", "content_type": "photo"},
    ])
    snapshot = await batch_env.service.prepare_moderation_batch(media="include")
    async with batch_env.factory() as session:
        human = await session.get(Submission, 1)
        human.status = SubmissionStatus.CONTENT_CREATED
        held = await session.get(Submission, 2)
        held.reviewed_at = START
        held.moderator_note = "Человек повторно проверил"
        edited = await session.get(Submission, 3)
        edited.raw_text = edited.cleaned_text = "Изменённое содержимое"
        album_member = await session.get(Submission, 5)
        album_member.status = SubmissionStatus.REJECTED
        await session.commit()
    result = await commit(batch_env, snapshot, [decision(index) for index in range(1, 5)])
    assert result["applied"] == 0 and result["conflicts"] == 4
    batch_env.checked.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_history_survives_reopen_and_is_not_selected(batch_env):
    await seed(batch_env, [{}, {"status": SubmissionStatus.HOLD}])
    async with batch_env.factory() as session:
        human_case = ModerationCase(
            case_key="submission:1", canonical_submission_id=1, channel_id=1,
            message_text="Старое решение", moderator_id=9, decision="approved", source="mcp_codex",
            action="approve", decided_at=START, voided_at=START,
        )
        session.add(human_case)
        await session.flush()
        session.add(ModerationCaseEvent(case_id=human_case.id, moderator_id=9, event_type="voided",
                                        source="panel", action="cancel", occurred_at=START))
        held = await session.get(Submission, 2)
        held.reviewed_at = START
        held.moderator_note = "Held by human"
        await session.commit()
    assert (await batch_env.service.prepare_moderation_batch(include_hold=True))["selected_count"] == 0


@pytest.mark.asyncio
async def test_limiter_20_sliding_seconds_persistent_across_api_and_restart(batch_env):
    await seed(batch_env, [{} for _ in range(25)] + [{"channel_id": 2}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    decisions = [decision(index, "reject", "survey") for index in range(1, 27)]
    first = await commit(batch_env, snapshot, decisions)
    assert first["applied"] == 21 and first["pending"] == 5 and first["state"] == "applying"
    assert first["retry_after_seconds"] == 60
    batch_env.now[0] += timedelta(seconds=59, milliseconds=900)
    batch_env.service = ModerationBatchService(session_maker=batch_env.factory, write_enabled=True,
                                               clock=lambda: batch_env.now[0])
    early = await commit(batch_env, snapshot, decisions)
    assert early["applied"] == 21 and early["pending"] == 5
    batch_env.now[0] += timedelta(milliseconds=100)
    finished = await commit(batch_env, snapshot, decisions)
    assert finished["applied"] == 26 and finished["pending"] == 0
    assert batch_env.checked.await_count == 26


@pytest.mark.asyncio
async def test_legacy_apply_cannot_bypass_shared_limiter(batch_env):
    await seed(batch_env, [{} for _ in range(21)])
    snapshot = await batch_env.service.prepare_moderation_batch(limit=20)
    await commit(batch_env, snapshot, [decision(i, "reject", "survey") for i in range(1, 21)])
    legacy = McpModerationService(session_maker=batch_env.factory, write_enabled=True,
                                  clock=lambda: batch_env.now[0])
    result = await legacy.apply_batch(
        batch_id="old-api", actions=[ModerationRequest(21, "reject", "Опрос", SubmissionStatus.NEW)],
        dry_run=False,
    )
    assert result["outcomes"] == {"deferred": 1}


@pytest.mark.asyncio
async def test_concurrent_same_snapshot_does_not_repeat_actions(batch_env):
    await seed(batch_env, [{}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    results = await asyncio.gather(*[commit(batch_env, snapshot, [decision(1)]) for _ in range(2)])
    assert all(result["applied"] == 1 for result in results)
    batch_env.checked.assert_awaited_once()
    async with batch_env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(ContentItem)) == 1


@pytest.mark.asyncio
async def test_policy_change_does_not_recompute_confirmed_snapshot(batch_env):
    await seed(batch_env, [{}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    original = batch_env.service.policy
    batch_env.service.policy = SimpleNamespace(
        version="new-version", sha256="different", metadata=lambda: {"version": "new-version"},
    )
    result = await commit(batch_env, snapshot, [decision(1, "reject", "survey")])
    assert result["policy_version"] == original.version
    assert "policy_warning" in result
    assert result["applied"] == 1


@pytest.mark.asyncio
async def test_expiry_only_blocks_unconfirmed_snapshots_and_skip_does_not_write(batch_env):
    await seed(batch_env, [{}, {}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    batch_env.now[0] += timedelta(days=2)
    with pytest.raises(ValueError, match="expired"):
        await commit(batch_env, snapshot, [decision(1)])
    fresh = await batch_env.service.prepare_moderation_batch()
    result = await commit(batch_env, fresh, [decision(1, "skip", "manual_review"), decision(2, "hold", "manual_review")])
    assert result["skipped"] == 1 and result["applied"] == 1
    assert result["verified"] == 1
    async with batch_env.factory() as session:
        assert (await session.get(Submission, 1)).status == SubmissionStatus.NEW


@pytest.mark.asyncio
async def test_telegram_partial_failure_and_uncertain_dispatch_are_reported_without_resend(batch_env):
    await seed(batch_env, [{}, {}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    batch_env.checked.side_effect = ValueError("Telegram cards verified 1/2")
    first = await commit(batch_env, snapshot, [decision(1, "advertising", "advertising")])
    assert first["applied"] == 1 and first["telegram_sync_warnings"] == 1
    await commit(batch_env, snapshot, [decision(1, "advertising", "advertising")])
    batch_env.advertising.assert_awaited_once()
    # Simulate a process dying after recording external dispatch.
    other = await batch_env.service.prepare_moderation_batch()
    batch_env.checked.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await commit(batch_env, other, [decision(1, "advertising", "advertising")])
    calls = batch_env.advertising.await_count
    restored = await commit(batch_env, other, [decision(1, "advertising", "advertising")])
    assert restored["telegram_sync_warnings"] == 1
    assert batch_env.advertising.await_count == calls


@pytest.mark.asyncio
async def test_final_verification_detects_external_status_change(batch_env):
    await seed(batch_env, [{}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    result = await commit(batch_env, snapshot, [decision(1)])
    async with batch_env.factory() as session:
        item = await session.get(Submission, 1)
        item.status = SubmissionStatus.REJECTED
        await session.commit()
    verified = await commit(batch_env, snapshot, [decision(1)])
    assert result["verified"] == 1
    assert verified["failed"] == 1
    assert verified["exceptions"][0]["type"] == "verification"


@pytest.mark.asyncio
async def test_compact_100_row_text_budget_and_validation(batch_env):
    await seed(batch_env, [{"text": "z" * 500 + str(i)} for i in range(100)])
    snapshot = await batch_env.service.prepare_moderation_batch()
    text_chars = sum(len(row["text_preview"]) +
                     sum(len(item["text_preview"]) for item in row["relevant_history"])
                     for row in snapshot["rows"])
    assert snapshot["selected_count"] == 100 and text_chars <= 35000
    for decisions in [[decision(101)], [decision(1), decision(1)],
                      [{"row": 1, "decision": "approve", "reason_code": "invented"}]]:
        with pytest.raises(ValueError):
            await commit(batch_env, snapshot, decisions)
    with pytest.raises(ValueError, match="mandatory"):
        await batch_env.service.commit_moderation_batch(
            snapshot_id=snapshot["snapshot_id"], decisions=[decision(1)],
            user_confirmed=True, verify_after_apply=False,
        )


@pytest.mark.asyncio
async def test_crash_before_telegram_dispatch_resumes_without_reapplying(batch_env, monkeypatch):
    await seed(batch_env, [{}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    original_sync = batch_env.service._sync_legacy_panel
    monkeypatch.setattr(batch_env.service, "_sync_legacy_panel", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await commit(batch_env, snapshot, [decision(1, "advertising", "advertising")])
    batch_env.advertising.assert_not_awaited()
    monkeypatch.setattr(batch_env.service, "_sync_legacy_panel", original_sync)
    resumed = await commit(batch_env, snapshot, [decision(1, "advertising", "advertising")])
    assert resumed["applied"] == 1 and resumed["telegram_sync_warnings"] == 0
    batch_env.advertising.assert_awaited_once()
    async with batch_env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(McpModerationAction)) == 1


@pytest.mark.asyncio
async def test_pending_resumes_after_expiry_of_original_approval_window(batch_env):
    await seed(batch_env, [{} for _ in range(21)])
    snapshot = await batch_env.service.prepare_moderation_batch()
    decisions = [decision(i, "reject", "survey") for i in range(1, 22)]
    assert (await commit(batch_env, snapshot, decisions))["pending"] == 1
    batch_env.now[0] += timedelta(days=2)
    assert (await commit(batch_env, snapshot, decisions))["pending"] == 0


@pytest.mark.asyncio
async def test_prepare_tied_dates_orders_by_submission_id_and_protects_whole_album(batch_env):
    await seed(batch_env, [
        {"created_at": START, "channel_id": 2},
        {"created_at": START, "channel_id": 1},
        {"created_at": START, "media_group_id": "a", "content_type": "photo"},
        {"created_at": START, "media_group_id": "a", "content_type": "photo",
         "status": SubmissionStatus.REJECTED},
    ])
    snapshot = await batch_env.service.prepare_moderation_batch(media="include")
    assert [row["submission_id"] for row in snapshot["rows"]] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["partial", "missing", "unacknowledged", "not_modified"])
async def test_strict_telegram_card_verification_is_not_just_success_count(batch_env, monkeypatch, failure):
    from src.editorial.services.legacy_moderation_sync import LegacyModerationSyncService

    await seed(batch_env, [{"legacy_row_id": 1, "media_group_id": "a", "content_type": "photo"},
                           {"legacy_row_id": 2, "media_group_id": "a", "content_type": "photo"}])
    rows = [SimpleNamespace(id=i, review_chat_id=123, review_message_id=i+100,
                            user_id=3, username="sender", first_name="Sender") for i in (1, 2)]
    if failure == "missing":
        rows.pop()
    reader = SimpleNamespace(
        fetch_sender_rows_by_ids=AsyncMock(return_value=rows),
        get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="fake")),
    )
    sync = LegacyModerationSyncService(legacy_reader=reader, importer=SimpleNamespace())
    monkeypatch.setattr("src.editorial.services.legacy_moderation_sync.session_factory", batch_env.factory)

    async def edit(**kwargs):
        if failure == "partial" and kwargs["message_id"] == 102:
            raise TimeoutError("timed out")
        if failure == "unacknowledged":
            return True
        if failure == "not_modified":
            raise ValueError("Bad Request: message is not modified")
        return SimpleNamespace(reply_markup=kwargs["reply_markup"])

    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(side_effect=edit), close_session=AsyncMock())
    monkeypatch.setattr("src.editorial.services.legacy_moderation_sync.AsyncTeleBot", lambda token: bot)
    if failure == "not_modified":
        assert await sync.sync_panel_submission_agent_checked(1, decision="approve") == 2
    else:
        with pytest.raises(ValueError, match="missing" if failure == "missing" else "verified"):
            await sync.sync_panel_submission_agent_checked(1, decision="approve")


@pytest.mark.asyncio
async def test_database_rollback_is_logged_and_does_not_consume_rate_limit(batch_env, monkeypatch):
    await seed(batch_env, [{}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    monkeypatch.setattr(batch_env.service, "_apply_decision", AsyncMock(side_effect=ValueError("synthetic failure")))
    result = await commit(batch_env, snapshot, [decision(1)])
    assert result["failed"] == 1 and result["applied"] == 0
    batch_env.checked.assert_not_awaited()
    async with batch_env.factory() as session:
        assert (await session.get(Submission, 1)).status == SubmissionStatus.NEW
        assert await batch_env.service._rate_retry_after(session, 1) == 0


def test_gold_fixture_is_versioned_and_keeps_policy_separate_from_classifier():
    fixture = json.loads((Path(__file__).parent / "fixtures/moderation_gold_v1.json").read_text(encoding="utf-8"))
    assert fixture["policy_version"] == playbook.version
    assert len(fixture["cases"]) == 14
    for example in fixture["cases"]:
        assert example["reason_code"] in REASON_CODES
        assert example["decision"] in {"approve", "reject", "advertising", "hold"}
        assert example["playbook_section"].split(".")[0] in {str(i) for i in range(1, 20)}
    assert {example["id"] for example in fixture["cases"]} >= {
        "student_question", "rude_joke", "hate", "survey", "meaningless", "admin_only",
        "admin_intro", "advertising_request", "unknown_group", "small_private_service",
        "past_event", "exact_duplicate", "normalized_duplicate", "media",
    }


def test_new_migration_offline_upgrade_and_downgrade(capsys):
    from alembic import command
    from alembic.config import Config
    from io import StringIO

    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    command.upgrade(config, "20261003_27:20261003_28", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE mcp_moderation_snapshots" in sql
    assert "moderation_normalized_hash" in sql
    assert "sha256(convert_to" in sql
    assert "rate_limit_at" in sql
    assert "CREATE INDEX ix_submissions_mcp_order" in sql
    output.seek(0)
    output.truncate()
    command.downgrade(config, "20261003_28:20261003_27", sql=True)
    assert "DROP TABLE mcp_moderation_snapshots" in output.getvalue()


@pytest.mark.asyncio
async def test_strict_advertising_manager_failure_is_observable(monkeypatch):
    from src.editorial.services import advertising
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[None, TimeoutError("manager timeout")]))
    monkeypatch.setattr(advertising, "resolve_advertising_targets", lambda: [123])
    monkeypatch.setattr(advertising, "_build_advertising_alert_bot", lambda: None)
    with pytest.raises(ValueError, match="manager notification"):
        await advertising.send_advertising_flow(
            bot=bot, recipient_user_id=1, channel_label="test", source_text="Реклама",
            sender_username=None, sender_first_name=None, strict=True,
        )
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_null_channel_title_does_not_skip_other_queues_known_phrase(batch_env):
    await seed(batch_env, [{"text": "Когда домой поедешь?"}, {"text": "Когда домой поедешь?"}])
    async with batch_env.factory() as session:
        channel = await session.get(Channel, 1)
        channel.title = None
        await session.commit()
    snapshot = await batch_env.service.prepare_moderation_batch()
    assert snapshot["selected_count"] == 2


@pytest.mark.asyncio
async def test_sliding_window_does_not_reset_at_wall_clock_minute(batch_env):
    await seed(batch_env, [{} for _ in range(21)])
    batch_env.now[0] += timedelta(seconds=30)
    snapshot = await batch_env.service.prepare_moderation_batch()
    decisions = [decision(i, "reject", "survey") for i in range(1, 22)]
    first = await commit(batch_env, snapshot, decisions)
    assert first["applied"] == 20 and first["pending"] == 1
    batch_env.now[0] += timedelta(seconds=30)
    next_minute = await commit(batch_env, snapshot, decisions)
    assert next_minute["applied"] == 20 and next_minute["pending"] == 1
    assert next_minute["retry_after_seconds"] == 30


@pytest.mark.asyncio
async def test_concurrent_services_share_limiter_capacity(batch_env):
    await seed(batch_env, [{} for _ in range(22)])
    other_factory = async_sessionmaker(batch_env.engine, expire_on_commit=False)
    other_service = McpModerationService(
        session_maker=other_factory, write_enabled=True, clock=lambda: batch_env.now[0],
    )
    results = await asyncio.gather(
        batch_env.service.apply_batch(
            batch_id="concurrent-a",
            actions=[ModerationRequest(i, "reject", "Опрос", SubmissionStatus.NEW) for i in range(1, 12)],
            dry_run=False,
        ),
        other_service.apply_batch(
            batch_id="concurrent-b",
            actions=[ModerationRequest(i, "reject", "Опрос", SubmissionStatus.NEW) for i in range(12, 23)],
            dry_run=False,
        ),
    )
    assert sum(result["outcomes"].get("applied", 0) for result in results) == 20
    assert sum(result["outcomes"].get("deferred", 0) for result in results) == 2


@pytest.mark.asyncio
async def test_current_album_updates_one_shared_control_card(batch_env, monkeypatch):
    from src.editorial.services.legacy_moderation_sync import LegacyModerationSyncService

    await seed(batch_env, [{"legacy_row_id": i, "media_group_id": "a", "content_type": "photo"} for i in (1, 2)])
    rows = [SimpleNamespace(id=i, review_chat_id=123, review_message_id=100,
                            user_id=3, username=None, first_name=None) for i in (1, 2)]
    reader = SimpleNamespace(
        fetch_sender_rows_by_ids=AsyncMock(return_value=rows),
        get_bot_binding=AsyncMock(return_value=SimpleNamespace(bot_api_token="fake")),
    )
    sync = LegacyModerationSyncService(legacy_reader=reader, importer=SimpleNamespace())
    monkeypatch.setattr("src.editorial.services.legacy_moderation_sync.session_factory", batch_env.factory)

    async def edit(**kwargs):
        return SimpleNamespace(reply_markup=kwargs["reply_markup"])

    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(side_effect=edit), close_session=AsyncMock())
    monkeypatch.setattr("src.editorial.services.legacy_moderation_sync.AsyncTeleBot", lambda token: bot)
    assert await sync.sync_panel_submission_agent_checked(1, decision="reject") == 1
    bot.edit_message_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("IDEAFLOW_MCP_TEST_DSN"), reason="No isolated local PostgreSQL test database")
async def test_postgresql_migration_roundtrip_and_unicode_backfill(batch_env):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    await seed(batch_env, [{"text": "Привет, мир!!!"},
                           {"text": "Привет — мир"},
                           {"text": "Ссылка https://t.me/first?start=123"}])
    path = Path(__file__).resolve().parents[1] / "alembic/versions/20261003_28_mcp_moderation_snapshots.py"
    spec = importlib.util.spec_from_file_location("mcp_snapshot_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def roundtrip(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()

    async with batch_env.engine.begin() as connection:
        await connection.run_sync(roundtrip)
    async with batch_env.factory() as session:
        for submission in (await session.execute(select(Submission))).scalars():
            assert submission.moderation_normalized_hash == compute_moderation_hash(submission.cleaned_text)


@pytest.mark.asyncio
async def test_human_hold_retaining_mcp_note_is_protected_but_agent_hold_can_be_selected(batch_env):
    await seed(batch_env, [{}])
    snapshot = await batch_env.service.prepare_moderation_batch()
    assert (await commit(batch_env, snapshot, [decision(1, "hold", "manual_review")]))["applied"] == 1
    assert (await batch_env.service.prepare_moderation_batch(include_hold=True))["selected_count"] == 1
    async with batch_env.factory() as session:
        operation = await session.scalar(select(McpModerationAction))
        item = await session.get(Submission, 1)
        # A subsequent human review can retain both status and the previous note.
        item.reviewed_at = batch_env.service._utc(operation.completed_at) + timedelta(seconds=1)
        await session.commit()
    assert (await batch_env.service.prepare_moderation_batch(include_hold=True))["selected_count"] == 0
    legacy = await batch_env.service.apply_batch(
        batch_id="retained-note",
        actions=[ModerationRequest(1, "approve", "Подходит", SubmissionStatus.HOLD)],
        dry_run=False,
    )
    assert legacy["outcomes"] == {"skipped": 1}
