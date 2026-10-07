from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, String, Table
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.editorial.models.channel import Channel
from src.editorial.models.enums import SubmissionStatus
from src.editorial.services.statistics_export import (
    ChannelStatisticsRow,
    StatisticsExportService,
    validate_statistics_delta_days,
)


def test_statistics_export_writes_minimal_xlsx(tmp_path) -> None:
    export_path = tmp_path / "stats.xlsx"
    service = StatisticsExportService(export_dir=tmp_path)

    service._write_xlsx(
        export_path,
        [
            ChannelStatisticsRow(
                title="Channel A",
                tag="@channel_a",
                subscriber_count=123,
                delta_count=7,
                submission_count=11,
                pending_submission_count=3,
            )
        ],
        delta_days=10,
    )

    with ZipFile(export_path) as archive:
        names = set(archive.namelist())
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

    assert "[Content_Types].xml" in names
    assert "xl/workbook.xml" in names
    assert "xl/worksheets/sheet1.xml" in names
    assert "Название канала" in sheet_xml
    assert "Изменение за 10 дн." in sheet_xml
    assert "Сообщений в предложку за 10 дн." in sheet_xml
    assert "Channel A" in sheet_xml
    assert "<v>123</v>" in sheet_xml
    assert "<v>7</v>" in sheet_xml
    assert "<v>11</v>" in sheet_xml
    assert 'autoFilter ref="A1:F2"' in sheet_xml
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    sheet = ElementTree.fromstring(sheet_xml)
    assert sheet.find('x:sheetData/x:row/x:c[@r="F1"]/x:is/x:t', namespace).text == "Необработанных сообщений"
    assert sheet.find('x:sheetData/x:row/x:c[@r="F2"]/x:v', namespace).text == "3"


def test_statistics_export_sorts_and_colors_subscriber_bands(tmp_path) -> None:
    export_path = tmp_path / "stats_bands.xlsx"
    service = StatisticsExportService(export_dir=tmp_path)
    subscriber_counts = [49, 1000, None, 500, 99, 499, 50, 1, 100]

    service._write_xlsx(
        export_path,
        [
            ChannelStatisticsRow(
                title=f"Channel {subscriber_count}",
                tag=f"@channel_{subscriber_count}",
                subscriber_count=subscriber_count,
                delta_count=0,
                submission_count=0,
            )
            for subscriber_count in subscriber_counts
        ],
    )

    with ZipFile(export_path) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        styles_xml = archive.read("xl/styles.xml").decode("utf-8")

    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    sheet = ElementTree.fromstring(sheet_xml)
    data_rows = sheet.findall("x:sheetData/x:row", namespace)[1:]
    ordered_counts = [
        int(cell.text) if cell is not None else None
        for row in data_rows
        for cell in [row.find("x:c[3]/x:v", namespace)]
    ]
    assert ordered_counts == [1000, 500, 499, 100, 99, 50, 49, 1, None]

    expected_styles = ["2", "3", "4", "4", "5", "5", "6", "6", None]
    for row, expected_style in zip(data_rows, expected_styles, strict=True):
        assert {cell.get("s") for cell in row.findall("x:c", namespace)} == {expected_style}

    sort_condition = sheet.find("x:autoFilter/x:sortState/x:sortCondition", namespace)
    assert sort_condition is not None
    assert sort_condition.get("ref") == "C2:C10"
    assert sort_condition.get("descending") == "1"

    styles = ElementTree.fromstring(styles_xml)
    fill_colors = [
        color.get("rgb")
        for color in styles.findall("x:fills/x:fill/x:patternFill/x:fgColor", namespace)
    ]
    assert fill_colors == [
        "FFDBECD3",
        "FFD4E6ED",
        "FFEEE3CD",
        "FFF1D7C6",
        "FFE4D8E2",
    ]


@pytest.mark.asyncio
async def test_statistics_rows_count_real_submissions_in_requested_period() -> None:
    now = datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc)
    channel = Channel(
        id=42,
        tg_channel_id=-100123,
        title="Channel A",
        short_code="channel_a",
        subscriber_count=123,
    )
    channel_without_submissions = Channel(
        id=43,
        tg_channel_id=-100124,
        title="Channel B",
        short_code="channel_b",
        subscriber_count=456,
    )
    channels_result = MagicMock()
    channels_result.scalars.return_value.all.return_value = [channel, channel_without_submissions]
    counts_result = MagicMock()
    counts_result.all.return_value = [(channel.id, 5)]
    session = MagicMock()
    pending_result = MagicMock()
    pending_result.all.return_value = [(channel.id, 3)]
    session.execute = AsyncMock(side_effect=[channels_result, counts_result, pending_result])
    session.scalar = AsyncMock(return_value=None)

    rows = await StatisticsExportService()._build_rows(
        session,
        channel_titles={},
        channel_tags={},
        delta_days=5,
        now=now,
    )

    assert rows == [
        ChannelStatisticsRow(
            title="Channel A",
            tag="channel_a",
            subscriber_count=123,
            delta_count=None,
            submission_count=5,
            pending_submission_count=3,
        ),
        ChannelStatisticsRow(
            title="Channel B",
            tag="channel_b",
            subscriber_count=456,
            delta_count=None,
            submission_count=0,
        ),
    ]
    count_stmt = session.execute.await_args_list[1].args[0]
    compiled_params = list(count_stmt.compile().params.values())
    assert now - timedelta(days=5) in compiled_params
    assert now in compiled_params
    assert [channel.id, channel_without_submissions.id] in compiled_params
    sql = str(count_stmt)
    assert "submissions.created_at >=" in sql
    assert "submissions.created_at <=" in sql
    assert "GROUP BY submissions.channel_id" in sql
    assert "submissions.source_chat_id IS NULL" in sql


@pytest.mark.asyncio
async def test_submission_counts_skip_query_without_channels() -> None:
    session = MagicMock()
    session.execute = AsyncMock()

    counts = await StatisticsExportService._submission_counts(
        session,
        channel_ids=[],
        started_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert counts == {}
    session.execute.assert_not_awaited()


def test_statistics_delta_days_validation() -> None:
    assert validate_statistics_delta_days("14") == 14

    with pytest.raises(ValueError):
        validate_statistics_delta_days("15")

    with pytest.raises(ValueError):
        validate_statistics_delta_days("abc")


@pytest.mark.asyncio
async def test_pending_counts_include_all_new_and_held_messages_at_export_time(tmp_path) -> None:
    # A minimal SQL table exercises the real aggregate without PostgreSQL-only JSONB fields.
    metadata = MetaData()
    submissions = Table(
        "submissions", metadata,
        Column("id", Integer, primary_key=True),
        Column("channel_id", Integer, nullable=False),
        Column("status", String, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("source_chat_id", BigInteger),
    )
    now = datetime(2026, 10, 7, 9, tzinfo=timezone.utc)
    cases = [
        (42, SubmissionStatus.NEW, now - timedelta(days=60), None),
        (42, SubmissionStatus.HOLD, now - timedelta(days=20), 123),
        (42, SubmissionStatus.NEW, now, 123),
        (42, SubmissionStatus.HOLD, now + timedelta(seconds=1), 123),
        (42, SubmissionStatus.NEW, now, -100123),
        (42, SubmissionStatus.HOLD, now, -100123),
        (43, SubmissionStatus.NEW, now, 456),
        (44, SubmissionStatus.NEW, now, 789),
    ]
    cases.extend(
        (42, status, now, 123)
        for status in SubmissionStatus
        if status not in {SubmissionStatus.NEW, SubmissionStatus.HOLD}
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'statistics.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(submissions.insert(), [
                {"id": index, "channel_id": channel_id, "status": status.value,
                 "created_at": created_at, "source_chat_id": source_chat_id}
                for index, (channel_id, status, created_at, source_chat_id) in enumerate(cases, start=1)
            ])
        sessions = async_sessionmaker(engine)
        async with sessions() as session:
            counts = await StatisticsExportService._pending_submission_counts(
                session, channel_ids=[42, 43, 45], ended_at=now,
            )
        assert counts == {42: 3, 43: 1}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pending_counts_skip_query_without_channels() -> None:
    session = MagicMock()
    session.execute = AsyncMock()
    assert await StatisticsExportService._pending_submission_counts(
        session, channel_ids=[], ended_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
    ) == {}
    session.execute.assert_not_awaited()
