from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from src.editorial.services.google_statistics_export import (
    GoogleStatisticsExportResult,
    GoogleStatisticsExportService,
    STATISTICS_METADATA_KEY,
)
from src.editorial.services.statistics_export import ChannelStatisticsRow, StatisticsExportService
from src.master import MasterBot


def service():
    return GoogleStatisticsExportService(spreadsheet_id="spreadsheet-id", credentials_file="key.json")


def owned_sheet(sheet_id, sheet_date, title=None):
    return {
        "properties": {"sheetId": sheet_id, "title": title or f"Статистика {sheet_date}"},
        "developerMetadata": [{
            "metadataKey": STATISTICS_METADATA_KEY, "metadataValue": sheet_date,
        }],
    }


def values():
    return StatisticsExportService.build_sheet_rows([
        ChannelStatisticsRow("=literal title", "@tag", 123, None, 4, 2),
    ])


@pytest.mark.parametrize("today", [date(2026, 10, 10), date(2027, 1, 1), date(2028, 3, 1)])
def test_retention_keeps_today_and_previous_29_days_only(today):
    cutoff = today - timedelta(days=29)
    sheets = [
        owned_sheet(1, (cutoff - timedelta(days=1)).isoformat()),
        owned_sheet(2, cutoff.isoformat()),
        owned_sheet(3, today.isoformat()),
        owned_sheet(4, (today + timedelta(days=1)).isoformat()),
        {"properties": {"sheetId": 5, "title": "Manual notes 2020-01-01"}},
        # Titles alone never grant permission to delete a sheet.
        {"properties": {"sheetId": 6, "title": "Статистика 2020-01-01"}},
        owned_sheet(7, "invalid"),
    ]
    batch, result = service()._build_batch(sheets, values(), today)
    assert [item["deleteSheet"]["sheetId"] for item in batch if "deleteSheet" in item] == [1]
    assert result.sheet_id == 3
    assert result.sheets_deleted == 1
    assert not any("addSheet" in item for item in batch)


def test_new_daily_sheet_contains_data_and_retention_in_one_batch():
    batch, result = service()._build_batch(
        [owned_sheet(42, "2026-08-01")], values(), date(2026, 10, 10),
    )
    properties = batch[0]["addSheet"]["properties"]
    assert properties["sheetId"] == result.sheet_id
    assert result.sheet_id != 42
    assert result.sheet_title == "Статистика 2026-10-10"
    assert properties["gridProperties"]["frozenRowCount"] == 1
    metadata = batch[1]["createDeveloperMetadata"]["developerMetadata"]
    assert metadata["location"]["sheetId"] == result.sheet_id
    assert metadata["metadataValue"] == "2026-10-10"
    assert batch[-1] == {"deleteSheet": {"sheetId": 42}}

    update = next(item["updateCells"] for item in batch if "updateCells" in item)
    cells = update["rows"][1]["values"]
    assert cells[0]["userEnteredValue"] == {"stringValue": "=literal title"}
    assert cells[2]["userEnteredValue"] == {"numberValue": 123}
    assert "userEnteredValue" not in cells[3]
    assert cells[4]["userEnteredValue"] == {"numberValue": 4}
    assert cells[5]["userEnteredValue"] == {"numberValue": 2}
    assert "backgroundColor" in cells[0]["userEnteredFormat"]


def test_retry_reuses_renamed_daily_sheet_and_clears_old_rows():
    sheet = owned_sheet(7, "2026-10-10", title="Renamed daily report")
    batch, result = service()._build_batch([sheet], values(), date(2026, 10, 10))
    assert result.sheet_id == 7
    assert result.sheet_title == "Renamed daily report"
    assert not any("addSheet" in item or "createDeveloperMetadata" in item for item in batch)
    update = next(item["updateCells"] for item in batch if "updateCells" in item)
    # An explicit range clears stale cells; rowCount also shrinks the grid.
    assert update["range"]["endRowIndex"] == 2
    assert batch[0]["updateSheetProperties"]["properties"]["gridProperties"]["rowCount"] == 2


def test_unowned_title_collision_is_rejected():
    with pytest.raises(ValueError, match="not owned"):
        service()._build_batch(
            [{"properties": {"sheetId": 1, "title": "Статистика 2026-10-10"}}],
            values(), date(2026, 10, 10),
        )


def test_empty_statistics_still_have_header_filter_and_valid_grid():
    batch, _ = service()._build_batch(
        [], StatisticsExportService.build_sheet_rows([]), date(2026, 10, 10),
    )
    assert batch[0]["addSheet"]["properties"]["gridProperties"]["rowCount"] == 2
    update = next(item["updateCells"] for item in batch if "updateCells" in item)
    assert len(update["rows"]) == 1
    basic_filter = next(item["setBasicFilter"] for item in batch if "setBasicFilter" in item)
    assert basic_filter["filter"]["range"]["endRowIndex"] == 1


def test_transport_reads_metadata_then_posts_single_atomic_update():
    exporter = service()
    client = MagicMock()
    client.get.return_value.json.return_value = {"sheets": [owned_sheet(1, "2026-08-01")]}
    session = MagicMock()
    session.__enter__.return_value = client
    with patch.object(exporter, "_authorized_session", return_value=session):
        result = exporter._upload_rows_sync(values(), date(2026, 10, 10))
    client.get.assert_called_once()
    client.post.assert_called_once()
    posted = client.post.call_args.kwargs["json"]["requests"]
    assert any("updateCells" in item for item in posted)
    assert any("deleteSheet" in item for item in posted)
    assert result.sheets_deleted == 1
    assert client.get.call_args.kwargs["timeout"] == 30
    assert client.post.call_args.kwargs["timeout"] == 30
    session.__exit__.assert_called_once()


def test_failed_metadata_read_never_mutates_spreadsheet():
    exporter = service()
    client = MagicMock()
    client.get.return_value.raise_for_status.side_effect = requests.HTTPError("read failed")
    session = MagicMock()
    session.__enter__.return_value = client
    with patch.object(exporter, "_authorized_session", return_value=session):
        with pytest.raises(requests.HTTPError):
            exporter._upload_rows_sync(values(), date(2026, 10, 10))
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_export_uses_moscow_date_even_when_utc_is_previous_day():
    exporter = service()
    result = GoogleStatisticsExportResult("daily", 1, 0)
    with patch.object(exporter, "_upload_rows_sync", return_value=result) as upload:
        assert await exporter.export_rows(
            [], now=datetime(2026, 10, 9, 23, 10, tzinfo=timezone.utc),
        ) == result
    assert upload.call_args.args[1] == date(2026, 10, 10)


@pytest.mark.asyncio
async def test_timeout_retry_rereads_metadata_and_updates_existing_sheet():
    exporter = service()
    client = MagicMock()
    committed_sheet = owned_sheet(7, "2026-10-10")
    client.get.return_value.json.side_effect = [
        {"sheets": []}, {"sheets": [committed_sheet]},
    ]
    # First POST was committed by Google, but its response was lost.
    client.post.side_effect = [requests.Timeout("response lost"), MagicMock()]
    session = MagicMock()
    session.__enter__.return_value = client
    with patch.object(exporter, "_authorized_session", return_value=session), patch(
        "src.editorial.services.google_statistics_export.asyncio.sleep", new_callable=AsyncMock,
    ):
        result = await exporter.export_rows(
            [], now=datetime(2026, 10, 10, tzinfo=timezone.utc),
        )
    assert result.sheet_id == 7
    assert client.get.call_count == 2
    first, second = [call.kwargs["json"]["requests"] for call in client.post.call_args_list]
    assert any("addSheet" in item for item in first)
    assert not any("addSheet" in item for item in second)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,attempts", [(403, 1), (429, 3), (503, 3)])
async def test_http_errors_have_bounded_retries(status, attempts):
    exporter = service()
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError("export failed", response=response)
    with patch.object(exporter, "_upload_rows_sync", side_effect=error) as upload, patch(
        "src.editorial.services.google_statistics_export.asyncio.sleep", new_callable=AsyncMock,
    ):
        with pytest.raises(requests.HTTPError):
            await exporter.export_rows([])
    assert upload.call_count == attempts


@pytest.mark.asyncio
async def test_disabled_export_does_not_start_http_requests():
    exporter = GoogleStatisticsExportService(spreadsheet_id="id", credentials_file="")
    assert not exporter.enabled
    with patch.object(exporter, "_upload_rows_sync") as upload:
        with pytest.raises(ValueError, match="configured"):
            await exporter.export_rows([])
    upload.assert_not_called()


def daily_bot(enabled=True):
    bot = MasterBot.__new__(MasterBot)
    bot.editorial_actions = MagicMock()
    bot.editorial_actions.google_statistics_export_service.enabled = enabled
    bot.editorial_actions.record_daily_subscriber_snapshots = AsyncMock(return_value=SimpleNamespace(
        channels_checked=1, subscriber_counts_updated=1, snapshots_recorded=1,
        snapshots_deleted=0, failed=0,
    ))
    bot.editorial_actions.list_channels = AsyncMock(return_value=[
        SimpleNamespace(id=42, tg_channel_id=-10042, title="Stored title", short_code="stored-tag"),
    ])
    bot.editorial_actions.export_channel_statistics_to_google_sheets = AsyncMock(
        return_value=GoogleStatisticsExportResult("daily", 7, 0),
    )
    bot._channel_title_from_runtime = MagicMock(return_value="Runtime title")
    bot._channel_label_from_runtime = MagicMock(return_value="@runtime")
    return bot


@pytest.mark.asyncio
async def test_daily_export_starts_after_committed_subscriber_update():
    bot = daily_bot()
    events = []

    async def snapshot():
        events.append("snapshot")
        return SimpleNamespace(channels_checked=1, subscriber_counts_updated=1,
                               snapshots_recorded=1, snapshots_deleted=0, failed=0)

    async def export(**kwargs):
        events.append("export")
        assert kwargs == {"channel_titles": {42: "Runtime title"}, "channel_tags": {42: "@runtime"}}
        return GoogleStatisticsExportResult("daily", 7, 0)

    bot.editorial_actions.record_daily_subscriber_snapshots.side_effect = snapshot
    bot.editorial_actions.export_channel_statistics_to_google_sheets.side_effect = export
    await bot._run_daily_statistics_update()
    assert events == ["snapshot", "export"]


@pytest.mark.asyncio
async def test_disabled_google_integration_still_updates_subscribers():
    bot = daily_bot(enabled=False)
    await bot._run_daily_statistics_update()
    bot.editorial_actions.record_daily_subscriber_snapshots.assert_awaited_once()
    bot.editorial_actions.list_channels.assert_not_awaited()
    bot.editorial_actions.export_channel_statistics_to_google_sheets.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_failure_prevents_export():
    bot = daily_bot()
    bot.editorial_actions.record_daily_subscriber_snapshots.side_effect = RuntimeError("database failed")
    with pytest.raises(RuntimeError, match="database failed"):
        await bot._run_daily_statistics_update()
    bot.editorial_actions.export_channel_statistics_to_google_sheets.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_failure_does_not_abort_daily_scheduler():
    bot = daily_bot()
    bot.editorial_actions.export_channel_statistics_to_google_sheets.side_effect = RuntimeError("Google failed")
    await bot._run_daily_statistics_update()
    bot.editorial_actions.record_daily_subscriber_snapshots.assert_awaited_once()


def test_daily_update_remains_scheduled_at_0200_moscow():
    from zoneinfo import ZoneInfo
    now = datetime(2026, 10, 10, 1, 50, tzinfo=ZoneInfo("Europe/Moscow"))
    assert MasterBot._next_subscriber_snapshot_run_at(now) == now.replace(hour=2, minute=0)
    assert MasterBot._next_subscriber_snapshot_run_at(now.replace(hour=2, minute=0)) == (
        now + timedelta(days=1)
    ).replace(hour=2, minute=0)


@pytest.mark.asyncio
async def test_action_reads_statistics_then_releases_database_before_google_upload():
    from src.editorial.services.telegram_actions import TelegramEditorialActions

    actions = TelegramEditorialActions.__new__(TelegramEditorialActions)
    actions.statistics_export_service = MagicMock()
    rows = [ChannelStatisticsRow("Channel", "@channel", 321, 2, 1, 0)]
    actions.statistics_export_service._build_rows = AsyncMock(return_value=rows)
    actions.google_statistics_export_service = MagicMock()
    session = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    expected = GoogleStatisticsExportResult("daily", 1, 0)
    now = datetime(2026, 10, 10, 0, 30, tzinfo=timezone.utc)

    async def upload(export_rows, **kwargs):
        context.__aexit__.assert_awaited_once()
        assert export_rows is rows
        assert kwargs == {"now": now}
        return expected

    actions.google_statistics_export_service.export_rows = AsyncMock(side_effect=upload)
    with patch("src.editorial.services.telegram_actions.session_factory", return_value=context):
        result = await actions.export_channel_statistics_to_google_sheets(
            channel_titles={42: "Channel"}, channel_tags={42: "@channel"}, now=now,
        )
    assert result == expected
    actions.statistics_export_service._build_rows.assert_awaited_once_with(
        session, channel_titles={42: "Channel"}, channel_tags={42: "@channel"},
        delta_days=7, now=now,
    )
