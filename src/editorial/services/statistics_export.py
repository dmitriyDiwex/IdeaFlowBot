from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core_database.config import BASE_DIR
from src.editorial.models.channel import Channel, ChannelSubscriberSnapshot
from src.editorial.models.submission import Submission
from src.editorial.models.enums import SubmissionStatus


MAX_STATISTICS_DELTA_DAYS = 14
DEFAULT_STATISTICS_DELTA_DAYS = 7


def validate_statistics_delta_days(delta_days: int | str) -> int:
    try:
        value = int(delta_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("Delta days must be an integer") from exc
    if value < 1 or value > MAX_STATISTICS_DELTA_DAYS:
        raise ValueError(f"Delta days must be from 1 to {MAX_STATISTICS_DELTA_DAYS}")
    return value


@dataclass(slots=True)
class ChannelStatisticsRow:
    title: str
    tag: str
    subscriber_count: int | None
    delta_count: int | None
    submission_count: int
    pending_submission_count: int = 0


class StatisticsExportService:
    def __init__(self, export_dir: Path | None = None) -> None:
        self.export_dir = export_dir or (BASE_DIR / "exports")

    async def export_channel_statistics(
        self,
        session: AsyncSession,
        *,
        channel_titles: dict[int, str | None] | None = None,
        channel_tags: dict[int, str | None] | None = None,
        delta_days: int = DEFAULT_STATISTICS_DELTA_DAYS,
        now: datetime | None = None,
    ) -> Path:
        self.export_dir.mkdir(parents=True, exist_ok=True)
        now = now or datetime.now(timezone.utc)
        delta_days = validate_statistics_delta_days(delta_days)
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        export_path = self.export_dir / f"channel_statistics_{timestamp}.xlsx"

        rows = await self._build_rows(
            session,
            channel_titles=channel_titles or {},
            channel_tags=channel_tags or {},
            delta_days=delta_days,
            now=now,
        )
        self._write_xlsx(export_path, rows, delta_days=delta_days)
        return export_path

    async def _build_rows(
        self,
        session: AsyncSession,
        *,
        channel_titles: dict[int, str | None],
        channel_tags: dict[int, str | None],
        delta_days: int,
        now: datetime,
    ) -> list[ChannelStatisticsRow]:
        delta_days = validate_statistics_delta_days(delta_days)
        channels = list(
            (
                await session.execute(
                    select(Channel)
                    .where(Channel.is_active.is_(True))
                    .order_by(Channel.id.asc())
                )
            ).scalars().all()
        )
        submission_counts = await self._submission_counts(
            session,
            channel_ids=[channel.id for channel in channels],
            started_at=now - timedelta(days=delta_days),
            ended_at=now,
        )
        pending_counts = await self._pending_submission_counts(
            session,
            channel_ids=[channel.id for channel in channels],
            ended_at=now,
        )
        rows: list[ChannelStatisticsRow] = []

        for channel in channels:
            latest_snapshot = await self._latest_snapshot(session, channel.id, before=now)
            current_count = (
                latest_snapshot.subscriber_count
                if latest_snapshot is not None
                else channel.subscriber_count
            )

            delta_count = None
            if latest_snapshot is not None:
                baseline = await self._latest_snapshot(
                    session,
                    channel.id,
                    before=latest_snapshot.checked_at - timedelta(days=delta_days),
                )
                if baseline is not None:
                    delta_count = latest_snapshot.subscriber_count - baseline.subscriber_count

            title = (
                channel_titles.get(channel.id)
                or channel.title
                or channel.short_code
                or str(channel.tg_channel_id)
            )
            tag = channel_tags.get(channel.id) or channel.short_code or str(channel.tg_channel_id)
            rows.append(
                ChannelStatisticsRow(
                    title=title,
                    tag=tag,
                    subscriber_count=current_count,
                    delta_count=delta_count,
                    submission_count=submission_counts.get(channel.id, 0),
                    pending_submission_count=pending_counts.get(channel.id, 0),
                )
            )
        return rows

    @staticmethod
    async def _submission_counts(
        session: AsyncSession,
        *,
        channel_ids: list[int],
        started_at: datetime,
        ended_at: datetime,
    ) -> dict[int, int]:
        if not channel_ids:
            return {}

        result = await session.execute(
            select(Submission.channel_id, func.count(Submission.id))
            .where(
                Submission.channel_id.in_(channel_ids),
                Submission.created_at >= started_at,
                Submission.created_at <= ended_at,
                or_(Submission.source_chat_id.is_(None), Submission.source_chat_id >= 0),
            )
            .group_by(Submission.channel_id)
        )
        return {int(channel_id): int(count) for channel_id, count in result.all()}

    @staticmethod
    async def _pending_submission_counts(
        session: AsyncSession,
        *,
        channel_ids: list[int],
        ended_at: datetime,
    ) -> dict[int, int]:
        if not channel_ids:
            return {}

        result = await session.execute(
            select(Submission.channel_id, func.count(Submission.id))
            .where(
                Submission.channel_id.in_(channel_ids),
                Submission.status.in_([SubmissionStatus.NEW, SubmissionStatus.HOLD]),
                Submission.created_at <= ended_at,
                or_(Submission.source_chat_id.is_(None), Submission.source_chat_id >= 0),
            )
            .group_by(Submission.channel_id)
        )
        return {int(channel_id): int(count) for channel_id, count in result.all()}

    @staticmethod
    async def _latest_snapshot(
        session: AsyncSession,
        channel_id: int,
        before: datetime | None,
    ) -> ChannelSubscriberSnapshot | None:
        stmt = (
            select(ChannelSubscriberSnapshot)
            .where(ChannelSubscriberSnapshot.channel_id == channel_id)
            .order_by(desc(ChannelSubscriberSnapshot.checked_at))
            .limit(1)
        )
        if before is not None:
            stmt = stmt.where(ChannelSubscriberSnapshot.checked_at <= before)
        return await session.scalar(stmt)

    def _write_xlsx(
        self,
        path: Path,
        rows: list[ChannelStatisticsRow],
        *,
        delta_days: int = DEFAULT_STATISTICS_DELTA_DAYS,
    ) -> None:
        sheet_rows = self.build_sheet_rows(rows, delta_days=delta_days)

        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", self._content_types_xml())
            archive.writestr("_rels/.rels", self._root_rels_xml())
            archive.writestr("xl/workbook.xml", self._workbook_xml())
            archive.writestr("xl/_rels/workbook.xml.rels", self._workbook_rels_xml())
            archive.writestr("xl/styles.xml", self._styles_xml())
            archive.writestr("xl/worksheets/sheet1.xml", self._sheet_xml(sheet_rows))

    @staticmethod
    def build_sheet_rows(
        rows: list[ChannelStatisticsRow],
        *,
        delta_days: int = DEFAULT_STATISTICS_DELTA_DAYS,
    ) -> list[list[str | int | None]]:
        delta_days = validate_statistics_delta_days(delta_days)
        sorted_rows = sorted(
            rows,
            key=lambda row: (
                row.subscriber_count is None,
                -(row.subscriber_count or 0),
                row.title.casefold(),
            ),
        )
        sheet_rows: list[list[str | int | None]] = [
            [
                "\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u043a\u0430\u043d\u0430\u043b\u0430",
                "\u0422\u044d\u0433 \u043a\u0430\u043d\u0430\u043b\u0430",
                "\u041f\u043e\u0434\u043f\u0438\u0441\u0447\u0438\u043a\u0438",
                f"\u0418\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0435 \u0437\u0430 {delta_days} \u0434\u043d.",
                f"\u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439 \u0432 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u043a\u0443 \u0437\u0430 {delta_days} \u0434\u043d.",
                "Необработанных сообщений",
            ],
        ]
        sheet_rows.extend(
            [row.title, row.tag, row.subscriber_count, row.delta_count, row.submission_count,
             row.pending_submission_count]
            for row in sorted_rows
        )

        return sheet_rows

    @staticmethod
    def _cell_ref(row_index: int, col_index: int) -> str:
        letters = ""
        value = col_index
        while value:
            value, remainder = divmod(value - 1, 26)
            letters = chr(65 + remainder) + letters
        return f"{letters}{row_index}"

    @staticmethod
    def _subscriber_style_id(subscriber_count: int | None) -> int:
        if subscriber_count is None or subscriber_count < 1:
            return 0
        if subscriber_count >= 1000:
            return 2
        if subscriber_count >= 500:
            return 3
        if subscriber_count >= 100:
            return 4
        if subscriber_count >= 50:
            return 5
        return 6

    def _sheet_xml(self, rows: list[list[str | int | None]]) -> str:
        row_xml = []
        for row_index, row in enumerate(rows, start=1):
            cells = []
            row_style_id = (
                1
                if row_index == 1
                else self._subscriber_style_id(
                    row[2] if len(row) > 2 and isinstance(row[2], int) else None
                )
            )
            for col_index, value in enumerate(row, start=1):
                ref = self._cell_ref(row_index, col_index)
                style = f' s="{row_style_id}"' if row_style_id else ""
                if value is None:
                    cells.append(f'<c r="{ref}"{style}/>')
                elif isinstance(value, int):
                    cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
                else:
                    text = escape(str(value))
                    cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{text}</t></is></c>')
            row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

        filter_ref = f"A1:F{max(len(rows), 1)}"
        sort_state = (
            f'<sortState ref="{filter_ref}"><sortCondition ref="C2:C{len(rows)}" '
            'descending="1"/></sortState>'
            if len(rows) > 1
            else ""
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
            'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            '<cols>'
            '<col min="1" max="1" width="34" customWidth="1"/>'
            '<col min="2" max="2" width="24" customWidth="1"/>'
            '<col min="3" max="4" width="18" customWidth="1"/>'
            '<col min="5" max="6" width="32" customWidth="1"/>'
            '</cols>'
            f'<sheetData>{"".join(row_xml)}</sheetData>'
            f'<autoFilter ref="{filter_ref}">{sort_state}</autoFilter>'
            '</worksheet>'
        )

    @staticmethod
    def _content_types_xml() -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>'
        )

    @staticmethod
    def _root_rels_xml() -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        )

    @staticmethod
    def _workbook_xml() -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="\u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430" sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>'
        )

    @staticmethod
    def _workbook_rels_xml() -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>'
        )

    @staticmethod
    def _styles_xml() -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="7">'
            '<fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFDBECD3"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFD4E6ED"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFEEE3CD"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFF1D7C6"/><bgColor indexed="64"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFE4D8E2"/><bgColor indexed="64"/></patternFill></fill>'
            '</fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="7"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="2" borderId="0" xfId="0" applyFill="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="5" borderId="0" xfId="0" applyFill="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="6" borderId="0" xfId="0" applyFill="1"/>'
            '</cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'
        )
