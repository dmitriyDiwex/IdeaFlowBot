from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import suppress
from datetime import timedelta
import time
import uuid

from sqlalchemy import or_, select

from src.editorial.config import settings
from src.editorial.models.enums import SubmissionStatus
from src.editorial.models.mcp_moderation import McpModerationAction, McpModerationJob
from src.editorial.services.mcp_moderation import ModerationRequest
from src.editorial.services.mcp_panel_resync import McpPanelResyncService
from src.editorial.services.moderation_batch import ModerationBatchService, REASON_CODES

_LOCAL_CLAIM_LOCKS = defaultdict(asyncio.Lock)


class ModerationJobWorker:
    """Durable snapshot executor. No AI decisions and no unconfirmed work.

    Jobs use leases and fencing; actions keep their stable snapshot:row keys.
    A retry updates only a card, NEVER an advertising reply or a publication.
    Different channels may run concurrently; one channel is processed serially.
    """

    LEASE_SECONDS = 120

    def __init__(self, service=None, *, concurrency=None, budget_seconds=None):
        self.service = service or ModerationBatchService()
        self.repair = McpPanelResyncService(self.service)
        self.repair.CARD_TIMEOUT_SECONDS = settings.mcp_card_timeout_seconds
        self.concurrency = concurrency or settings.mcp_worker_concurrency
        self.budget_seconds = budget_seconds or settings.mcp_worker_budget_seconds

    async def _claim(self):
        if not self.service.write_enabled:
            return None
        async with self.service.session_maker() as session:
            engine_key, local = id(session.bind), session.bind.dialect.name == "sqlite"
        if local:
            async with _LOCAL_CLAIM_LOCKS[engine_key]:
                return await self._claim_locked()
        return await self._claim_locked()

    async def _claim_locked(self):
        async with self.service.session_maker() as session:
            now = await self.service._now(session)
            job = await session.scalar(select(McpModerationJob).where(
                McpModerationJob.state.in_(["queued", "running", "waiting"]),
                McpModerationJob.next_run_at <= now,
                or_(McpModerationJob.lease_until.is_(None), McpModerationJob.lease_until <= now),
            ).order_by(McpModerationJob.next_run_at, McpModerationJob.created_at)
                .with_for_update(skip_locked=True).limit(1))
            if job is None:
                return None
            snapshot = await self.service._snapshot(session, job.snapshot_id)
            if snapshot.confirmed_at is None:
                job.state = "failed"
                job.error_text = "Unconfirmed snapshot cannot be executed"
                await session.commit()
                return None
            token = str(uuid.uuid4())
            job.state, job.lease_token = "running", token
            job.lease_until = now + timedelta(seconds=self.LEASE_SECONDS)
            job.updated_at = now
            await session.commit()
            return job.snapshot_id, token, list(job.authorized_rows), dict(job.card_retries)

    async def _renew(self, snapshot_id, token):
        async with self.service.session_maker() as session:
            job = await session.scalar(select(McpModerationJob).where(
                McpModerationJob.snapshot_id == snapshot_id,
            ).with_for_update())
            if job.lease_token != token:
                return False
            now = await self.service._now(session)
            job.lease_until = now + timedelta(seconds=self.LEASE_SECONDS)
            job.updated_at = now
            await session.commit()
            return True

    async def _heartbeat(self, snapshot_id, token, lost):
        while True:
            await asyncio.sleep(10)
            try:
                renewed = await self._renew(snapshot_id, token)
            except Exception:
                renewed = False
            if not renewed:
                lost.set()
                return

    async def run_once(self):
        claim = await self._claim()
        if claim is None:
            return None
        snapshot_id, token, authorized, retries = claim
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(snapshot_id, token, lost))
        try:
            delays = await self._process(snapshot_id, token, authorized, retries, lost)
            if lost.is_set():
                raise RuntimeError("Job lease heartbeat was lost")
            result = await self.service._final_summary(snapshot_id, {}, persist=False, selected_rows=set(authorized))
            async with self.service.session_maker() as session:
                outstanding = list(await session.scalars(select(McpModerationAction.id).where(
                    McpModerationAction.request_id.in_([f"snapshot-{snapshot_id}:{row}" for row in authorized]),
                    McpModerationAction.outcome == "applied",
                    McpModerationAction.telegram_sync_state.not_in(["verified", "not_required"]),
                )))
            async with self.service.session_maker() as session:
                # Same lock order as enqueue: snapshot, then job. Avoid a
                # PostgreSQL deadlock when the user authorizes another part.
                snapshot = await self.service._snapshot(session, snapshot_id, lock=True)
                job = await session.scalar(select(McpModerationJob).where(
                    McpModerationJob.snapshot_id == snapshot_id,
                ).with_for_update())
                if job.lease_token != token:
                    return {"snapshot_id": snapshot_id, "state": "lease_lost"}
                now = await self.service._now(session)
                extended = set(job.authorized_rows) != set(authorized)
                pending_cards = result["telegram_cards_pending"]
                unfinished_retries = any(not retries.get(str(op_id), {}).get("terminal") for op_id in outstanding)
                waiting = extended or result["pending"] or (pending_cards and unfinished_retries)
                warnings = (result["failed"] or result["conflicts"] or pending_cards
                            or result["advertising_delivery_warnings"])
                job.state = "waiting" if waiting else "completed_with_warnings" if warnings else "completed"
                job.next_run_at = now + timedelta(seconds=min(delays, default=1))
                job.card_retries = retries
                job.error_text = None
                job.lease_token = job.lease_until = None
                job.updated_at = now
                snapshot.state = "applying" if waiting else "applied"
                snapshot.summary = {**result, "job_state": job.state,
                                    "skipped_media_count": snapshot.summary.get("skipped_media_count", 0)}
                await session.commit()
            return await self.service.get_moderation_batch_status(snapshot_id)
        except asyncio.CancelledError:
            # A process crash leaves the persisted lease recoverable. Normal
            # shutdown releases it; action-level idempotency protects partial work.
            await self._release_after_error(snapshot_id, token, "Worker cancelled")
            raise
        except Exception as exc:
            await self._release_after_error(snapshot_id, token, type(exc).__name__)
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _release_after_error(self, snapshot_id, token, error):
        async with self.service.session_maker() as session:
            job = await session.scalar(select(McpModerationJob).where(
                McpModerationJob.snapshot_id == snapshot_id,
            ).with_for_update())
            if job and job.lease_token == token:
                now = await self.service._now(session)
                job.state = "waiting"
                job.lease_token = job.lease_until = None
                job.next_run_at = now + timedelta(seconds=5)
                job.error_text = error[:200]
                await session.commit()

    async def _process(self, snapshot_id, token, authorized, retries, lost):
        async with self.service.session_maker() as session:
            snapshot = await self.service._snapshot(session, snapshot_id)
            rows = {row["public"]["row"]: row for row in snapshot.rows}
            decisions = [entry for entry in snapshot.decisions if entry["row"] in authorized]
            operations = list(await session.scalars(select(McpModerationAction).where(
                McpModerationAction.batch_id == f"snapshot-{snapshot_id}",
            )))
        existing = {operation.request_id: self.service._operation_payload(operation) for operation in operations}
        by_channel = defaultdict(list)
        for entry in decisions:
            if entry["decision"] != "skip":
                by_channel[rows[entry["row"]]["channel_id"]].append(entry)
        semaphore = asyncio.Semaphore(self.concurrency)
        deadline = time.monotonic() + self.budget_seconds
        delays = []

        async def process_channel(entries):
            async with semaphore:
                for entry in entries:
                    if lost.is_set() or time.monotonic() >= deadline:
                        delays.append(1)
                        break
                    row = rows[entry["row"]]
                    request_id = f"snapshot-{snapshot_id}:{entry['row']}"
                    operation = existing.get(request_id)
                    if operation is None:
                        operation = await self.service._apply_one(
                            request_id=request_id, batch_id=f"snapshot-{snapshot_id}", dry_run=False,
                            defer_telegram=True, expected_members=row["members"],
                            action=ModerationRequest(row["public"]["submission_id"], entry["decision"],
                                                     REASON_CODES[entry["reason_code"]],
                                                     SubmissionStatus(row["expected_status"])),
                        )
                    if operation["outcome"] == "deferred":
                        delays.append(operation["retry_after_seconds"])
                        break
                    if operation["outcome"] != "applied":
                        continue
                    op_id = operation["operation_id"]
                    key = str(op_id)
                    state = operation["telegram_sync_state"]
                    if state in {"verified", "not_required"}:
                        retries.pop(key, None)
                        continue
                    retry = retries.get(key, {})
                    if retry.get("terminal"):
                        continue
                    async with self.service.session_maker() as session:
                        now = await self.service._now(session)
                    if retry.get("due_at"):
                        from datetime import datetime
                        due = self.service._utc(datetime.fromisoformat(retry["due_at"]))
                        if due > now:
                            delays.append(max(1, (due-now).total_seconds()))
                            continue
                    if state == "pending":
                        # Exactly one bounded original dispatch; ads are claimed
                        # durably before send and never re-sent by card recovery.
                        operation = await self.service._sync_legacy_panel(op_id, entry["decision"], guarded=True)
                        retry_count = 0
                    else:
                        retry_count = retry.get("attempts", 0) + 1
                        answer = await self.repair.resync_batch(
                            batch_id=f"job-{snapshot_id}-card-{op_id}-{retry_count}", operation_ids=[op_id],
                            dry_run=False, user_confirmed=True,
                        )
                        outcome = answer["results"][0]
                        if outcome["state"] in {"deferred", "processing"}:
                            delays.append(max(1, outcome["retry_after_seconds"]))
                            retries[key] = {**retry, "attempts": retry_count-1}
                            continue
                        if outcome["state"] == "skipped" and "already verified" not in (outcome.get("detail") or "").lower():
                            retries[key] = {"attempts": retry_count, "terminal": True,
                                            "error": outcome.get("detail")}
                            continue
                        async with self.service.session_maker() as session:
                            current = await session.get(McpModerationAction, op_id)
                            operation = self.service._operation_payload(current)
                    if operation["telegram_sync_state"] == "verified":
                        retries.pop(key, None)
                        continue
                    terminal = (not operation.get("telegram_error_transient")
                                or retry_count >= settings.mcp_card_retry_attempts)
                    delay = max(operation.get("telegram_retry_after_seconds") or 0,
                                min(300, settings.mcp_card_retry_base_seconds * 2**min(retry_count, 6)))
                    retries[key] = {"attempts": retry_count, "terminal": terminal,
                                    "due_at": (now+timedelta(seconds=delay)).isoformat()}
                    if not terminal:
                        delays.append(delay)
        # Do not release a job lease while sibling channel tasks still run.
        async with asyncio.TaskGroup() as tasks:
            for entries in by_channel.values():
                tasks.create_task(process_channel(entries))
        return delays


async def run_worker():
    import logging
    from src.telegram_runtime import telegram_process_session
    logging.basicConfig(level=settings.editorial_log_level.upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger(__name__).info("MCP moderation worker started; writes enabled=%s, concurrency=%s",
                                     settings.mcp_write_enabled, settings.mcp_worker_concurrency)
    worker = ModerationJobWorker()
    async with telegram_process_session():
        while True:
            try:
                result = await worker.run_once()
                if result is not None:
                    logging.getLogger(__name__).info("MCP job %s: %s", result["snapshot_id"], result["state"])
            except Exception:
                # Do not log raw SDK errors containing request payloads or credentials.
                logging.getLogger(__name__).error("MCP moderation worker tick failed; durable job will resume")
            await asyncio.sleep(settings.mcp_worker_tick_seconds)


if __name__ == "__main__":
    asyncio.run(run_worker())
