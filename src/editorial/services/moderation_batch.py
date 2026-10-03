from __future__ import annotations

from datetime import timedelta, timezone
import uuid

from sqlalchemy import String, and_, case, cast, func, literal, or_, select
from sqlalchemy.orm import aliased

from src.editorial.config import settings
from src.editorial.models.channel import Channel
from src.editorial.models.mcp_moderation import McpModerationAction, McpModerationSnapshot
from src.editorial.models.moderation_case import ModerationCase
from src.editorial.models.submission import Submission
from src.editorial.models.enums import SubmissionStatus
from src.editorial.services.mcp_moderation import MCP_MODERATION_SOURCE, McpModerationService, ModerationRequest


REASON_CODES = {
    "ok": "Допустимое осмысленное сообщение",
    "duplicate": "Повтор сообщения",
    "survey": "Опрос или поиск респондентов",
    "admin_only": "Только обращение к администрации",
    "advertising": "Запрос или коммерческое размещение рекламы",
    "native_promo": "Неясная нативная реклама канала или группы",
    "meaningless": "Нет осмысленного содержания",
    "outdated": "Объявление потеряло актуальность",
    "hate_or_incite": "Групповая ненависть или опасный призыв",
    "targeted_threat": "Реальная адресная угроза",
    "minor_sexual": "Сексуальный контент с несовершеннолетними",
    "media_review": "Требуется просмотр медиа",
    "manual_review": "Неоднозначный случай",
}
PROFILE = "student_default_v1"
MSK = timezone(timedelta(hours=3))


class ModerationBatchService(McpModerationService):
    """Persistent global batches built on the existing atomic moderation facade."""

    def __init__(self, *, excluded_bots=None, **kwargs):
        super().__init__(**kwargs)
        self.excluded_bots = tuple(
            name.strip().lstrip("@").lower()
            for name in (settings.mcp_excluded_bots if excluded_bots is None else excluded_bots)
        )

    @staticmethod
    def _bot_name(column):
        return func.lower(func.ltrim(func.trim(func.coalesce(column, "")), "@"))

    def _allowed_channels(self):
        other = aliased(Submission)
        # A queue is excluded even if its short code differs from the bot username.
        return and_(
            self._bot_name(Channel.short_code).not_in(self.excluded_bots),
            self._bot_name(Channel.title).not_in(self.excluded_bots),
            ~select(other.id).where(
                other.channel_id == Channel.id,
                self._bot_name(other.bot_username).in_(self.excluded_bots),
            ).exists(),
        )

    def _known_duplicate_skip(self):
        older = aliased(Submission)
        text = func.trim(func.coalesce(func.nullif(Submission.cleaned_text, ""), Submission.raw_text, ""))
        earlier_text = func.trim(func.coalesce(func.nullif(older.cleaned_text, ""), older.raw_text, ""))
        return and_(
            or_(func.lower(func.coalesce(Channel.title, "")).like("%юургу%"),
                func.coalesce(Channel.title, "").like("%ЮУрГУ%"),
                func.lower(Channel.short_code).like("%susu%")),
            text == "Когда домой поедешь?",
            select(older.id).where(
                older.channel_id == Submission.channel_id,
                earlier_text == text,
                or_(older.source_chat_id.is_(None), older.source_chat_id >= 0),
                or_(older.created_at < Submission.created_at,
                    and_(older.created_at == Submission.created_at, older.id < Submission.id)),
            ).exists(),
        )

    def _groups(self, media="include"):
        human = or_(
            self._human_history_filter(),
            self._human_review_filter(),
        )
        if media == "exclude":
            # The common text-only path needs no windows over the historical archive.
            return select(
                Submission.id, Submission.channel_id, cast(Submission.status, String).label("status"),
                Submission.created_at.label("submitted_at"), literal(0).label("has_media"),
            ).join(Channel, Channel.id == Submission.channel_id).where(
                Submission.status.in_([SubmissionStatus.NEW, SubmissionStatus.HOLD]),
                Submission.content_type == "text", Submission.media_group_id.is_(None),
                self._visible_submission_filter(), self._allowed_channels(),
                ~human, ~self._known_duplicate_skip(),
            ).subquery()
        # Windows see the complete album (including non-pending members) before LIMIT.
        # SQLAlchemy's concatenation operator also works in the SQLite test backend.
        group = case(
            (Submission.media_group_id.is_not(None), "album:" + Submission.media_group_id),
            else_="submission:" + cast(Submission.id, String),
        )
        partition = [Submission.channel_id, Submission.source_chat_id, group]
        peer = aliased(Submission)
        pending_album = select(peer.id).where(
            peer.channel_id == Submission.channel_id,
            peer.media_group_id == Submission.media_group_id,
            peer.source_chat_id.is_not_distinct_from(Submission.source_chat_id),
            peer.status.in_([SubmissionStatus.NEW, SubmissionStatus.HOLD]),
            or_(peer.source_chat_id.is_(None), peer.source_chat_id >= 0),
        ).exists()
        status = cast(Submission.status, String)
        base = select(
            Submission.id, Submission.channel_id, status.label("status"),
            func.min(Submission.created_at).over(partition_by=partition).label("submitted_at"),
            func.row_number().over(partition_by=partition, order_by=Submission.id).label("rank"),
            func.max(case((or_(Submission.content_type != "text",
                               Submission.media_group_id.is_not(None)), 1), else_=0))
                .over(partition_by=partition).label("has_media"),
            func.max(case((or_(~Submission.status.in_([SubmissionStatus.NEW, SubmissionStatus.HOLD]),
                               human, self._known_duplicate_skip()), 1), else_=0))
                .over(partition_by=partition).label("blocked"),
            func.min(status).over(partition_by=partition).label("min_status"),
            func.max(status).over(partition_by=partition).label("max_status"),
        ).join(Channel, Channel.id == Submission.channel_id).where(
            self._visible_submission_filter(), self._allowed_channels(),
            or_(Submission.status.in_([SubmissionStatus.NEW, SubmissionStatus.HOLD]),
                and_(Submission.media_group_id.is_not(None), pending_album)),
        ).subquery()
        return select(base).where(
            base.c.rank == 1, base.c.blocked == 0, base.c.min_status == base.c.max_status,
        ).subquery()

    async def list_pending_summary(self, *, profile=PROFILE, include_hold=False, media="exclude",
                                   include_media_count=False, include_hold_count=False):
        self._validate_profile(profile, media)
        groups = self._groups("include" if media == "include" or include_media_count or include_hold_count else "exclude")
        async with self.session_maker() as session:
            rows = (await session.execute(
                select(groups.c.channel_id, groups.c.status, groups.c.has_media, func.count())
                .group_by(groups.c.channel_id, groups.c.status, groups.c.has_media)
            )).all()
            channels = list((await session.execute(select(Channel).where(
                Channel.id.in_({row.channel_id for row in rows}),
            ))).scalars())
        names = {channel.id: channel.short_code for channel in channels}
        counts = {}
        media_count = hold_count = 0
        for channel_id, status, has_media, count in rows:
            if status == "hold":
                hold_count += count
            if status == "new" or include_hold:
                if has_media:
                    media_count += count
                if media == "include" or not has_media:
                    counts[channel_id] = counts.get(channel_id, 0) + count
        result = {
            **self.policy.response_metadata(), "profile": profile,
            "total_pending": sum(counts.values()),
            "queues": [{"channel": names[key], "pending_count": count}
                       for key, count in sorted(counts.items(), key=lambda pair: (-pair[1], names[pair[0]]))],
        }
        if include_media_count:
            result["excluded_media_count"] = media_count if media == "exclude" else 0
        if include_hold_count:
            result["hold_count"] = hold_count
        return result

    async def prepare_moderation_batch(self, *, profile=PROFILE, limit=100, order="oldest",
                                       media="exclude", text_limit=300, include_hold=False):
        self._validate_profile(profile, media)
        if order not in {"oldest", "newest"}:
            raise ValueError("order must be oldest or newest")
        if not 1 <= limit <= settings.mcp_snapshot_max_size or not 1 <= text_limit <= 300:
            raise ValueError("limit must be within snapshot maximum; text_limit must be 1-300")
        groups = self._groups(media)
        media_groups = self._groups() if media == "exclude" else groups
        status_filter = groups.c.status.in_(["new", "hold"] if include_hold else ["new"])
        ordering = groups.c.submitted_at.asc() if order == "oldest" else groups.c.submitted_at.desc()
        stmt = select(groups).where(status_filter)
        if media == "exclude":
            stmt = stmt.where(groups.c.has_media == 0)
        stmt = stmt.order_by(ordering, groups.c.id).limit(limit)
        async with self.session_maker() as session:
            selected = (await session.execute(stmt)).all()
            skipped_media = await session.scalar(select(func.count()).select_from(media_groups).where(
                media_groups.c.status.in_(["new", "hold"] if include_hold else ["new"]),
                media_groups.c.has_media == 1,
            )) if media == "exclude" else 0
            selected_ids = [row.id for row in selected]
            canonical = list((await session.execute(
                select(Submission).where(Submission.id.in_(selected_ids)),
            )).scalars())
            conditions = [Submission.id.in_(selected_ids)]
            for item in canonical:
                if item.media_group_id:
                    conditions.append(and_(
                        Submission.channel_id == item.channel_id,
                        Submission.source_chat_id == item.source_chat_id,
                        Submission.media_group_id == item.media_group_id,
                    ))
            members = list((await session.execute(
                select(Submission).where(or_(*conditions)).order_by(Submission.id),
            )).scalars())
            channels = list((await session.execute(select(Channel).where(
                Channel.id.in_({item.channel_id for item in canonical}),
            ))).scalars())
            by_id = {item.id: item for item in members}
            names = {item.id: item.short_code for item in channels}
            duplicates, histories = await self._context(session, canonical)
            stored = []
            history_budget = 5000
            for row_number, selected_row in enumerate(selected, 1):
                item = by_id[selected_row.id]
                related = [candidate for candidate in members
                           if (candidate.id == item.id or
                               (item.media_group_id and candidate.channel_id == item.channel_id
                                and candidate.source_chat_id == item.source_chat_id
                                and candidate.media_group_id == item.media_group_id))]
                text = next((candidate.cleaned_text or candidate.raw_text for candidate in related
                             if candidate.cleaned_text or candidate.raw_text), "")
                history = []
                for example in histories.get(item.id, [])[:3]:
                    length = len(example["text_preview"])
                    if length <= history_budget:
                        history.append(example)
                        history_budget -= length
                public = {
                    "row": row_number, "submission_id": item.id, "channel": names[item.channel_id],
                    "submitted_at_msk": self._utc(selected_row.submitted_at).astimezone(MSK).strftime("%Y-%m-%d %H:%M"),
                    "text_preview": self._truncate(text.strip(), text_limit),
                    "duplicate": duplicates.get(item.id), "relevant_history": history,
                }
                if selected_row.has_media:
                    public["requires_human_media_review"] = True
                    public["media_item_count"] = len(related)
                stored.append({
                    "public": public, "channel_id": item.channel_id, "expected_status": selected_row.status,
                    "members": [self._member_revision(candidate) for candidate in related],
                })
            now = await self._now(session)
            snapshot = McpModerationSnapshot(
                snapshot_id=str(uuid.uuid4()), created_at=now,
                expires_at=now + timedelta(hours=settings.mcp_snapshot_ttl_hours),
                profile=profile, policy_version=self.policy.version, policy_sha256=self.policy.sha256,
                rows=stored, state="awaiting_approval", proposals=[], user_changes=[], decisions=[],
                summary={"skipped_media_count": int(skipped_media)},
            )
            session.add(snapshot)
            await session.commit()
            return self._snapshot_payload(snapshot)

    async def _context(self, session, items):
        hashes = {item.text_hash for item in items if item.text_hash}
        normalized = {item.moderation_normalized_hash for item in items if item.moderation_normalized_hash}
        channel_ids = {item.channel_id for item in items}
        if not items:
            return {}, {}
        # Bound repeated texts at the SQL layer, retaining the earliest original.
        ranked = select(
            Submission.id,
            func.row_number().over(
                partition_by=[Submission.channel_id, Submission.moderation_normalized_hash],
                order_by=[Submission.created_at, Submission.id],
            ).label("rank"),
        ).where(
            Submission.channel_id.in_(channel_ids), self._visible_submission_filter(),
            Submission.content_type == "text", Submission.media_group_id.is_(None),
            or_(Submission.text_hash.in_(hashes), Submission.moderation_normalized_hash.in_(normalized)),
        ).subquery()
        candidates = list((await session.execute(select(Submission).join(
            ranked, ranked.c.id == Submission.id,
        ).where(ranked.c.rank <= 3))).scalars())
        # Only lexical identities are used; unrelated recent decisions never enter context.
        history_ranked = select(
            ModerationCase.id,
            func.row_number().over(
                partition_by=[ModerationCase.channel_id, Submission.moderation_normalized_hash],
                order_by=ModerationCase.finalized_at.desc(),
            ).label("rank"),
        ).join(Submission, Submission.id == ModerationCase.canonical_submission_id).where(
            Submission.channel_id.in_(channel_ids),
            Submission.moderation_normalized_hash.in_(normalized),
            ModerationCase.source != MCP_MODERATION_SOURCE,
            ModerationCase.finalized_at.is_not(None), ModerationCase.voided_at.is_(None),
        ).subquery()
        cases = (await session.execute(select(ModerationCase, Submission.moderation_normalized_hash)
            .join(history_ranked, history_ranked.c.id == ModerationCase.id)
            .join(Submission, Submission.id == ModerationCase.canonical_submission_id)
            .where(history_ranked.c.rank <= 3))).all()
        duplicates, histories = {}, {}
        for item in items:
            originals = [other for other in candidates
                         if other.channel_id == item.channel_id
                         and (self._utc(other.created_at), other.id) < (self._utc(item.created_at), item.id)
                         and other.moderation_normalized_hash
                         and other.moderation_normalized_hash == item.moderation_normalized_hash]
            if originals:
                original = min(originals, key=lambda other: (self._utc(other.created_at), other.id))
                duplicates[item.id] = {
                    "submission_id": original.id,
                    "kind": "exact" if (original.cleaned_text or original.raw_text or "").strip()
                        == (item.cleaned_text or item.raw_text or "").strip() else "normalized",
                    "status": self._status_value(original.status),
                }
            histories[item.id] = [
                {"decision": human_case.decision, "text_preview": self._truncate(human_case.message_text, 120)}
                for human_case, text_hash in cases
                if human_case.channel_id == item.channel_id and text_hash == item.moderation_normalized_hash
            ]
        return duplicates, histories

    async def get_moderation_batch(self, snapshot_id):
        async with self.session_maker() as session:
            snapshot = await self._snapshot(session, snapshot_id)
            return self._snapshot_payload(snapshot)

    async def save_moderation_batch_draft(self, *, snapshot_id, proposals, user_changes=None):
        async with self.session_maker() as session:
            snapshot = await self._snapshot(session, snapshot_id, lock=True)
            if snapshot.state != "awaiting_approval":
                raise ValueError("Only an unconfirmed snapshot may be edited")
            if self._utc(snapshot.expires_at) <= await self._now(session):
                snapshot.state = "stale"
                await session.commit()
                raise ValueError("Snapshot expired; prepare and show a new batch")
            snapshot.proposals = self._validate_decisions(snapshot, proposals)
            if user_changes is not None:
                snapshot.user_changes = self._validate_decisions(snapshot, user_changes)
            await session.commit()
            return {"snapshot_id": snapshot_id, "state": snapshot.state,
                    "saved_proposals": len(snapshot.proposals), "saved_user_changes": len(snapshot.user_changes),
                    **self._snapshot_policy(snapshot)}

    async def commit_moderation_batch(self, *, snapshot_id, decisions, user_confirmed=False,
                                      verify_after_apply=True):
        if not user_confirmed:
            raise PermissionError("Explicit user confirmation is required (user_confirmed=true)")
        if not self.write_enabled:
            raise PermissionError("MCP write actions are disabled")
        if not verify_after_apply:
            raise ValueError("Final verification is mandatory")
        async with self.session_maker() as session:
            snapshot = await self._snapshot(session, snapshot_id, lock=True)
            decisions = self._validate_decisions(snapshot, decisions)
            if snapshot.state == "stale":
                raise ValueError("Snapshot is stale")
            if snapshot.confirmed_at is not None:
                if decisions != snapshot.decisions:
                    raise ValueError("Confirmed snapshot decisions are immutable")
            else:
                if self._utc(snapshot.expires_at) <= await self._now(session):
                    snapshot.state = "stale"
                    await session.commit()
                    raise ValueError("Snapshot expired; prepare and show a new batch")
                snapshot.decisions = decisions
                if snapshot.proposals:
                    proposed = {entry["row"]: entry for entry in snapshot.proposals}
                    snapshot.user_changes = [entry for entry in decisions if proposed.get(entry["row"]) != entry]
                else:
                    snapshot.proposals = decisions
                snapshot.confirmed_at = await self._now(session)
                snapshot.state = "applying"
                await session.commit()
            rows = {entry["public"]["row"]: entry for entry in snapshot.rows}
        deferred = {}
        for entry in decisions:
            if entry["decision"] == "skip":
                continue
            row = rows[entry["row"]]
            # Process other queues even when one queue has exhausted its window.
            result = await self._apply_one(
                request_id=f"snapshot-{snapshot_id}:{entry['row']}",
                batch_id=f"snapshot-{snapshot_id}",
                action=ModerationRequest(
                    submission_id=row["public"]["submission_id"], decision=entry["decision"],
                    reason=REASON_CODES[entry["reason_code"]],
                    expected_status=SubmissionStatus(row["expected_status"]),
                ),
                dry_run=False, expected_members=row["members"],
            )
            if result["outcome"] == "deferred":
                deferred[entry["row"]] = result["retry_after_seconds"]
        return await self._final_summary(snapshot_id, deferred)

    async def _final_summary(self, snapshot_id, deferred):
        async with self.session_maker() as session:
            snapshot = await self._snapshot(session, snapshot_id, lock=True)
            operations = list((await session.execute(select(McpModerationAction).where(
                McpModerationAction.batch_id == f"snapshot-{snapshot_id}",
            ))).scalars())
            by_request = {operation.request_id: operation for operation in operations}
            rows = {entry["public"]["row"]: entry for entry in snapshot.rows}
            ids = {member["id"] for row in snapshot.rows for member in row["members"]}
            actual = {item.id: item for item in (await session.execute(select(Submission).where(
                Submission.id.in_(ids),
            ))).scalars()}
            result = {
                "snapshot_id": snapshot_id, **self._snapshot_policy(snapshot),
                "requested": len(snapshot.decisions), "applied": 0, "conflicts": 0,
                "failed": 0, "skipped": 0, "pending": 0, "telegram_sync_warnings": 0,
                "verified": 0, "exceptions": [],
            }
            for decision in snapshot.decisions:
                row_number = decision["row"]
                if decision["decision"] == "skip":
                    result["skipped"] += 1
                    continue
                operation = by_request.get(f"snapshot-{snapshot_id}:{row_number}")
                if operation is None:
                    result["pending"] += 1
                    continue
                if operation.outcome == "applied":
                    result["applied"] += 1
                    members = [actual.get(member["id"]) for member in rows[row_number]["members"]]
                    if any(item is None or self._status_value(item.status) != operation.resulting_status for item in members):
                        result["failed"] += 1
                        result["applied"] -= 1
                        result["exceptions"].append({"row": row_number, "type": "verification",
                                                     "error": "Recorded result differs from current group status"})
                    else:
                        result["verified"] += 1
                    if operation.warning_text or operation.telegram_sync_state not in {"verified", "not_required"}:
                        result["telegram_sync_warnings"] += 1
                        result["exceptions"].append({
                            "row": row_number, "type": "telegram_sync",
                            "error": operation.warning_text or "Telegram dispatch is incomplete or uncertain; not automatically resent",
                        })
                elif operation.outcome == "skipped":
                    result["conflicts"] += 1
                    result["exceptions"].append({"row": row_number, "type": "conflict", "error": operation.error_text})
                else:
                    result["failed"] += 1
                    result["exceptions"].append({"row": row_number, "type": "failed", "error": operation.error_text})
            snapshot.state = "applying" if result["pending"] else "applied"
            result["state"] = snapshot.state
            if result["pending"]:
                result["retry_after_seconds"] = min(deferred.values()) if deferred else 1
            snapshot.summary = {**result, "skipped_media_count": snapshot.summary.get("skipped_media_count", 0)}
            await session.commit()
            return result

    async def _snapshot(self, session, snapshot_id, lock=False):
        stmt = select(McpModerationSnapshot).where(McpModerationSnapshot.snapshot_id == snapshot_id)
        if lock:
            stmt = stmt.with_for_update()
        snapshot = await session.scalar(stmt)
        if snapshot is None:
            raise ValueError("Unknown snapshot_id")
        return snapshot

    def _snapshot_policy(self, snapshot):
        metadata = {"policy_version": snapshot.policy_version, "policy": {
            "version": snapshot.policy_version, "sha256": snapshot.policy_sha256,
            "resource_uri": f"ideaflow://moderation/playbook/{snapshot.policy_version}",
        }}
        if (snapshot.policy_version, snapshot.policy_sha256) != (self.policy.version, self.policy.sha256):
            metadata["policy_warning"] = "Deployed playbook changed; snapshot policy and decisions were preserved"
            metadata["current_policy"] = self.policy.metadata()
        return metadata

    def _snapshot_payload(self, snapshot):
        result = {
            "snapshot_id": snapshot.snapshot_id, **self._snapshot_policy(snapshot),
            "profile": snapshot.profile, "state": snapshot.state,
            "expires_at": self._utc(snapshot.expires_at).isoformat(),
            "selected_count": len(snapshot.rows),
            "skipped_media_count": snapshot.summary.get("skipped_media_count", 0),
            "rows": [entry["public"] for entry in snapshot.rows],
            "proposals": snapshot.proposals, "user_changes": snapshot.user_changes,
        }
        if snapshot.confirmed_at is not None:
            result["application_summary"] = snapshot.summary
        return result

    @staticmethod
    def _validate_profile(profile, media):
        if profile != PROFILE:
            raise ValueError("Unknown moderation profile")
        if media not in {"include", "exclude"}:
            raise ValueError("media must be include or exclude")

    @staticmethod
    def _validate_decisions(snapshot, decisions):
        allowed = {entry["public"]["row"] for entry in snapshot.rows}
        seen = set()
        result = []
        for entry in decisions:
            if set(entry) != {"row", "decision", "reason_code"}:
                raise ValueError("Each decision requires only row, decision and reason_code")
            if type(entry["row"]) is not int or entry["row"] not in allowed or entry["row"] in seen:
                raise ValueError("Unknown or repeated snapshot row")
            if entry["decision"] not in {"approve", "reject", "hold", "advertising", "skip"}:
                raise ValueError("Unsupported decision")
            if entry["reason_code"] not in REASON_CODES:
                raise ValueError("Unknown reason_code")
            seen.add(entry["row"])
            result.append(dict(entry))
        return sorted(result, key=lambda entry: entry["row"])
