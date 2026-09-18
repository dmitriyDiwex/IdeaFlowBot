from __future__ import annotations

from typing import TYPE_CHECKING

from src.editorial.utils.text import compute_raw_text_hash

if TYPE_CHECKING:
    from src.editorial.models.submission import Submission


def is_media_submission(submission: Submission) -> bool:
    return bool(submission.media_group_id) or submission.content_type != "text"


def build_media_fingerprint(submission: Submission) -> tuple[str, str]:
    """Identify the Telegram message or album independently of its caption."""
    if submission.media_group_id:
        message_ref = f"album {submission.media_group_id}"
    elif submission.source_chat_id is not None and submission.source_message_id is not None:
        message_ref = f"message {submission.source_message_id}"
    else:
        message_ref = f"submission {submission.id}"
    fingerprint = (
        f"telegram media {submission.channel_id} "
        f"{submission.source_chat_id or 0} {message_ref}"
    )
    return fingerprint, compute_raw_text_hash(fingerprint) or ""
