from dataclasses import dataclass

from src.editorial.services.legacy_source import LegacyCollectorReader, LegacySenderRow


@dataclass(slots=True)
class LegacyMediaGroupReference:
    source_chat_id: int
    source_message_ids: list[int]
    caption: str | None
    caption_index: int


async def fetch_sender_media_group_rows(
    legacy_reader: LegacyCollectorReader,
    *,
    channel_id: int,
    source_chat_id: int,
    media_group_id: str,
) -> list[LegacySenderRow]:
    """Return one original incoming Telegram album in source message order."""
    return await legacy_reader.fetch_sender_media_group_rows(
        channel_id=channel_id,
        source_chat_id=source_chat_id,
        media_group_id=media_group_id,
    )


def build_media_group_reference(
    rows: list[LegacySenderRow],
) -> LegacyMediaGroupReference | None:
    source_rows = [
        row
        for row in rows
        if row.chat_id is not None and row.message_id is not None
    ]
    if not source_rows:
        return None

    source_chat_id = int(source_rows[0].chat_id)
    source_rows = [row for row in source_rows if int(row.chat_id) == source_chat_id]
    if not source_rows:
        return None

    caption_index = next(
        (index for index, row in enumerate(source_rows) if (row.text_post or "").strip()),
        0,
    )
    caption = source_rows[caption_index].text_post or None
    return LegacyMediaGroupReference(
        source_chat_id=source_chat_id,
        source_message_ids=[int(row.message_id) for row in source_rows],
        caption=caption,
        caption_index=caption_index,
    )
