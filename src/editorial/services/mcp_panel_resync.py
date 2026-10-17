from __future__ import annotations

import asyncio
from collections import Counter
import math
import time
import uuid

from sqlalchemy import select

from src.editorial.models.channel import Channel
from src.editorial.models.mcp_moderation import McpModerationAction, McpPanelSyncAttempt
from src.editorial.models.submission import Submission
from src.editorial.services.mcp_moderation import BATCH_ID_PATTERN, _LOCAL_CHANNEL_LOCKS


class McpPanelResyncService:
    """Explicit, bounded retries of keyboards, not of moderation or user messages."""

    CARD_TIMEOUT_SECONDS = 8
    CALL_BUDGET_SECONDS = 15
    LEASE_SECONDS = 60
    # Original dispatch can include an advertising send with the legacy HTTP timeout.
    ORIGINAL_DISPATCH_GRACE_SECONDS = 600

    def __init__(self, moderation_service):
        self.owner = moderation_service

    async def resync_batch(
        self, *, batch_id: str, operation_ids: list[int],
        dry_run: bool = True, user_confirmed: bool = False,
    ) -> dict:
        if not BATCH_ID_PATTERN.fullmatch(batch_id):
            raise ValueError("Invalid batch_id (1..120 ASCII letters, digits, _.:-)")
        if (not 1 <= len(operation_ids) <= 100
                or any(type(value) is not int or value <= 0 for value in operation_ids)
                or len(set(operation_ids)) != len(operation_ids)):
            raise ValueError("operation_ids must contain 1..100 distinct positive integers")
        if not dry_run and (not user_confirmed or not self.owner.write_enabled):
            raise ValueError("Panel synchronization requires user_confirmed=true and MCP writes enabled")

        deadline = time.monotonic() + self.CALL_BUDGET_SECONDS
        results = []
        for operation_id in operation_ids:
            if time.monotonic() >= deadline:
                results.append(self._result(operation_id, "deferred", "Call time budget reached", retry=1))
            else:
                results.append(await self._one(batch_id, operation_id, dry_run))
        deferred = [row for row in results if row["state"] in {"deferred", "processing"}]
        return {
            "batch_id": batch_id, "dry_run": dry_run, "panel_only": True,
            "advertising_reply_retried": False,
            "state": "applying" if deferred else (
                "completed_with_warnings" if any(row["state"] == "warning" for row in results) else "completed"
            ),
            "requested": len(results), "outcomes": dict(Counter(row["state"] for row in results)),
            "retry_after_seconds": max((row["retry_after_seconds"] for row in deferred), default=0),
            "results": results,
        }

    async def verify_batch(self, batch_id: str) -> dict:
        """Audit read only; Telegram state is the last checked edit, not a live read."""
        async with self.owner.session_maker() as session:
            attempts = list(await session.scalars(select(McpPanelSyncAttempt).where(
                McpPanelSyncAttempt.batch_id == batch_id,
            ).order_by(McpPanelSyncAttempt.id)))
            results = [self._attempt_payload(item, replayed=True) for item in attempts]
        return {"batch_id": batch_id, "panel_only": True, "results": results,
                "outcomes": dict(Counter(row["state"] for row in results))}

    async def _one(self, batch_id, operation_id, dry_run):
        async with self.owner.session_maker() as session:
            operation = await session.get(McpModerationAction, operation_id)
            if operation is None or operation.channel_id is None:
                return self._result(operation_id, "skipped", "Operation or channel not found")
            channel_id = operation.channel_id
            local = session.bind.dialect.name == "sqlite"
            engine_key = id(session.bind)
        if local:
            # Match the SQLite test fallback used by normal moderation.
            async with _LOCAL_CHANNEL_LOCKS[(engine_key, channel_id)]:
                return await self._claim_and_sync(batch_id, operation_id, channel_id, dry_run)
        return await self._claim_and_sync(batch_id, operation_id, channel_id, dry_run)

    async def _claim_and_sync(self, batch_id, operation_id, channel_id, dry_run):
        owner = self.owner
        # Key by operation, NOT by array position: reordering or resuming a subset is safe.
        request_id = f"{batch_id}:{operation_id}"
        async with owner.session_maker() as session:
            await session.scalar(select(Channel).where(Channel.id == channel_id).with_for_update())
            operation = await session.scalar(select(McpModerationAction).where(
                McpModerationAction.id == operation_id,
            ).with_for_update())
            existing = await session.scalar(select(McpPanelSyncAttempt).where(
                McpPanelSyncAttempt.request_id == request_id,
            ))
            if existing is not None and existing.state != "processing" and not dry_run:
                return self._attempt_payload(existing, replayed=True)
            now = await owner._now(session)
            active = await session.scalar(select(McpPanelSyncAttempt).where(
                McpPanelSyncAttempt.operation_id == operation_id,
                McpPanelSyncAttempt.state == "processing",
            ).order_by(McpPanelSyncAttempt.id.desc()).limit(1))
            error = await self._protection_error(session, operation)
            if error:
                # A cancelled worker must not reserve capacity forever after a
                # human has subsequently changed the submission.
                if (not dry_run and active is not None
                        and (now - owner._utc(active.rate_limit_at)).total_seconds() >= self.LEASE_SECONDS):
                    active.state = "skipped"
                    active.error_text = error
                    active.completed_at = now
                    if operation is not None and operation.telegram_sync_state == "resyncing":
                        operation.telegram_sync_state = "warning"
                        operation.warning_text = error
                    await session.commit()
                return self._result(operation_id, "skipped", error)
            if operation.telegram_sync_state == "verified":
                return self._result(operation_id, "skipped", "Card already verified")
            if operation.telegram_sync_state not in {"pending", "warning", "dispatching", "resyncing"}:
                return self._result(operation_id, "skipped", "Operation does not require panel synchronization")

            if operation.telegram_sync_state == "dispatching":
                started = operation.rate_limit_at or operation.completed_at or operation.created_at
                remaining = self.ORIGINAL_DISPATCH_GRACE_SECONDS - (now - owner._utc(started)).total_seconds()
                if remaining > 0:
                    return self._result(operation_id, "deferred", "Original dispatch may still be running",
                                        retry=math.ceil(remaining))
            if active is not None:
                remaining = self.LEASE_SECONDS - (now - owner._utc(active.rate_limit_at)).total_seconds()
                if remaining > 0:
                    return self._result(operation_id, "deferred", "Panel synchronization already running",
                                        retry=math.ceil(remaining))
            if dry_run:
                return self._result(operation_id, "eligible", "Only the Telegram card will be updated")

            retry = await owner._rate_retry_after(
                session, channel_id, recovering_operation_id=operation_id,
                recovering_attempt_id=active.id if active else None,
            )
            if retry:
                return self._result(operation_id, "deferred", "Channel limit: 20 operations per 60 seconds",
                                    retry=retry)
            if active is not None and active is not existing:
                active.state = "warning"
                active.error_text = "Expired lease superseded by an explicit card-only retry"
                active.completed_at = now
            token = str(uuid.uuid4())
            if existing is None:
                existing = McpPanelSyncAttempt(
                    request_id=request_id, batch_id=batch_id, operation_id=operation_id,
                    channel_id=channel_id, created_at=now,
                )
                session.add(existing)
            existing.state = "processing"
            existing.claim_token = token
            existing.rate_limit_at = now
            existing.completed_at = None
            existing.error_text = None
            operation.telegram_sync_state = "resyncing"
            await session.commit()
            attempt_id = existing.id
        return await self._sync_claim(attempt_id, token, operation_id, channel_id)

    async def _protection_error(self, session, operation):
        owner = self.owner
        if operation is None or operation.dry_run or operation.outcome != "applied":
            return "Only actually applied MCP operations may be resynchronized"
        if operation.decision not in {"approve", "reject", "advertising"}:
            return "Hold and unsupported decisions have no processed card to restore"
        submission = await session.scalar(select(Submission).where(
            Submission.id == operation.submission_id, owner._visible_submission_filter(),
        ))
        if submission is None or submission.channel_id != operation.channel_id:
            return "Submission missing, hidden or moved to another channel"
        related = await owner.moderation.get_related_submissions(session, submission, lock=True)
        if (not related or any(owner._status_value(item.status) != operation.resulting_status for item in related)
                or await owner._human_decided(session, related)):
            return "Human decision or current status conflicts with the recorded MCP decision"
        newer = await session.scalar(select(McpModerationAction.id).where(
            McpModerationAction.submission_id.in_([item.id for item in related]),
            McpModerationAction.id > operation.id,
            McpModerationAction.outcome == "applied", McpModerationAction.dry_run.is_(False),
        ).limit(1))
        if newer is not None:
            return "A newer MCP decision supersedes this operation"
        return None

    async def _sync_claim(self, attempt_id, token, operation_id, channel_id):
        owner = self.owner
        async with owner.session_maker() as session:
            # Recheck under locks immediately before the edit. Keep submission locks
            # through the bounded call, so normal human moderation cannot overtake it.
            await session.scalar(select(Channel).where(Channel.id == channel_id).with_for_update())
            operation = await session.scalar(select(McpModerationAction).where(
                McpModerationAction.id == operation_id,
            ).with_for_update())
            attempt = await session.scalar(select(McpPanelSyncAttempt).where(
                McpPanelSyncAttempt.id == attempt_id,
            ).with_for_update())
            if attempt.state != "processing" or attempt.claim_token != token:
                return self._attempt_payload(attempt, replayed=True)
            error = await self._protection_error(session, operation)
            if error:
                attempt.state = "skipped"
                attempt.error_text = error
                operation.telegram_sync_state = "warning"
                operation.warning_text = error
            else:
                try:
                    from src.editorial.services.telegram_actions import TelegramEditorialActions

                    # Deliberately NEVER call _apply_decision or send_*_reply here.
                    count = await asyncio.wait_for(
                        TelegramEditorialActions().legacy_moderation_sync.sync_panel_submission_agent_checked(
                            operation.submission_id, decision=operation.decision,
                        ), timeout=self.CARD_TIMEOUT_SECONDS,
                    )
                    if count < 1:
                        raise ValueError("No Telegram moderation cards were synchronized")
                    attempt.legacy_sync_count = count
                    attempt.state = "verified"
                    operation.legacy_sync_count = count
                    operation.telegram_sync_state = "verified"
                    # A card retry cannot confirm delivery of an advertising reply.
                    if operation.decision != "advertising":
                        operation.warning_text = None
                except Exception as exc:
                    attempt.state = "warning"
                    attempt.error_text = owner._truncate(
                        f"Card-only synchronization failed ({type(exc).__name__}): {exc}", 4000,
                    )
                    operation.telegram_sync_state = "warning"
                    operation.warning_text = attempt.error_text
            attempt.completed_at = await owner._now(session)
            # Includes completion, just like original moderation, in the rolling limit.
            attempt.rate_limit_at = attempt.completed_at
            await session.commit()
            return self._attempt_payload(attempt)

    @staticmethod
    def _result(operation_id, state, error=None, *, retry=0):
        return {"operation_id": operation_id, "state": state, "detail": error,
                "retry_after_seconds": retry}

    @staticmethod
    def _attempt_payload(attempt, *, replayed=False):
        return {
            "operation_id": attempt.operation_id, "attempt_id": attempt.id,
            "batch_id": attempt.batch_id, "channel_id": attempt.channel_id,
            "state": attempt.state, "detail": attempt.error_text,
            "legacy_sync_count": attempt.legacy_sync_count,
            "created_at": attempt.created_at.isoformat(),
            "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
            "retry_after_seconds": 1 if attempt.state == "processing" else 0,
            "idempotent_replay": replayed,
        }
