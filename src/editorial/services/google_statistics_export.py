from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import secrets
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
import requests

from src.editorial.services.statistics_export import (
    ChannelStatisticsRow,
    DEFAULT_STATISTICS_DELTA_DAYS,
    StatisticsExportService,
)


STATISTICS_TIMEZONE = ZoneInfo("Europe/Moscow")
STATISTICS_RETENTION_DAYS = 30
STATISTICS_METADATA_KEY = "ideaflow_channel_statistics_date"
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
REQUEST_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class GoogleStatisticsExportResult:
    sheet_title: str
    sheet_id: int
    sheets_deleted: int


class GoogleStatisticsExportService:
    def __init__(self, *, spreadsheet_id: str, credentials_file: str) -> None:
        self.spreadsheet_id = spreadsheet_id.strip()
        self.credentials_file = credentials_file.strip()

    @property
    def enabled(self) -> bool:
        return bool(self.spreadsheet_id and self.credentials_file)

    async def export_rows(
        self,
        rows: list[ChannelStatisticsRow],
        *,
        delta_days: int = DEFAULT_STATISTICS_DELTA_DAYS,
        now: datetime | None = None,
    ) -> GoogleStatisticsExportResult:
        if not self.enabled:
            raise ValueError("Google statistics spreadsheet and credentials file must be configured")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        export_date = now.astimezone(STATISTICS_TIMEZONE).date()
        values = StatisticsExportService.build_sheet_rows(rows, delta_days=delta_days)
        for attempt in range(3):
            try:
                # Authentication and HTTP requests are synchronous; keep bot polling responsive.
                return await asyncio.to_thread(self._upload_rows_sync, values, export_date)
            except requests.RequestException as exc:
                status = exc.response.status_code if exc.response is not None else None
                retryable = (
                    isinstance(exc, (requests.ConnectionError, requests.Timeout))
                    or status in {429, 500, 502, 503, 504}
                )
                if not retryable or attempt == 2:
                    raise
                # Re-read sheet metadata on retries, including after an ambiguous POST timeout.
                logger.warning("Retrying Google statistics export after request failure (status={})", status)
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError("Google statistics export exhausted its attempts")

    def _authorized_session(self):
        # Existing bot features also work when this optional integration is disabled.
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials

        credentials = Credentials.from_service_account_file(
            self.credentials_file, scopes=[GOOGLE_SHEETS_SCOPE],
        )
        return AuthorizedSession(credentials, refresh_timeout=REQUEST_TIMEOUT_SECONDS)

    def _upload_rows_sync(
        self, values: list[list[str | int | None]], export_date: date,
    ) -> GoogleStatisticsExportResult:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
        with self._authorized_session() as client:
            response = client.get(
                url,
                params={"fields": "sheets(properties(sheetId,title),developerMetadata(metadataKey,metadataValue))"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            requests_body, result = self._build_batch(
                response.json().get("sheets", []), values, export_date,
            )
            response = client.post(
                f"{url}:batchUpdate",
                json={"requests": requests_body},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return result

    @staticmethod
    def _sheet_date(sheet: dict[str, Any]) -> date | None:
        for metadata in sheet.get("developerMetadata", []):
            if metadata.get("metadataKey") == STATISTICS_METADATA_KEY:
                try:
                    return date.fromisoformat(metadata.get("metadataValue", ""))
                except (ValueError, TypeError):
                    continue
        return None

    def _build_batch(
        self,
        sheets: list[dict[str, Any]],
        values: list[list[str | int | None]],
        export_date: date,
    ) -> tuple[list[dict[str, Any]], GoogleStatisticsExportResult]:
        title = f"Статистика {export_date.isoformat()}"
        cutoff = export_date - timedelta(days=STATISTICS_RETENTION_DAYS - 1)
        existing = next((sheet for sheet in sheets if self._sheet_date(sheet) == export_date), None)
        expired = [
            sheet for sheet in sheets
            if (sheet_date := self._sheet_date(sheet)) is not None and sheet_date < cutoff
        ]
        batch: list[dict[str, Any]] = []
        grid = {"rowCount": max(len(values), 2), "columnCount": 6, "frozenRowCount": 1}
        if existing is None:
            if any(sheet["properties"]["title"] == title for sheet in sheets):
                raise ValueError(f"Sheet '{title}' already exists and is not owned by the statistics exporter")
            used_ids = {sheet["properties"]["sheetId"] for sheet in sheets}
            sheet_id = secrets.randbelow(2 ** 31)
            while sheet_id in used_ids:
                sheet_id = secrets.randbelow(2 ** 31)
            batch.extend([
                {"addSheet": {"properties": {
                    "sheetId": sheet_id, "title": title, "index": 0, "gridProperties": grid,
                }}},
                {"createDeveloperMetadata": {"developerMetadata": {
                    "metadataKey": STATISTICS_METADATA_KEY,
                    "metadataValue": export_date.isoformat(),
                    "location": {"sheetId": sheet_id},
                    "visibility": "DOCUMENT",
                }}},
            ])
        else:
            sheet_id = existing["properties"]["sheetId"]
            title = existing["properties"]["title"]
            batch.append({"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": grid},
                "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount",
            }})

        cells = []
        colors = ["DBECD3", "D4E6ED", "EEE3CD", "F1D7C6", "E4D8E2"]
        for index, row in enumerate(values):
            row_format: dict[str, Any] = {"verticalAlignment": "MIDDLE"}
            if index == 0:
                row_format["textFormat"] = {"bold": True}
                row_format["wrapStrategy"] = "WRAP"
            else:
                style_id = StatisticsExportService._subscriber_style_id(row[2])
                if style_id >= 2:
                    color = colors[style_id - 2]
                    row_format["backgroundColor"] = {
                        key: int(color[offset:offset + 2], 16) / 255
                        for key, offset in [("red", 0), ("green", 2), ("blue", 4)]
                    }
            row_cells = []
            for value in row:
                cell: dict[str, Any] = {"userEnteredFormat": row_format}
                if value is not None:
                    # stringValue keeps Telegram names/tags literal, including leading '='.
                    cell["userEnteredValue"] = (
                        {"numberValue": value} if isinstance(value, int) else {"stringValue": str(value)}
                    )
                row_cells.append(cell)
            cells.append({"values": row_cells})
        batch.extend([
            {"updateCells": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": grid["rowCount"],
                          "startColumnIndex": 0, "endColumnIndex": 6},
                "rows": cells, "fields": "userEnteredValue,userEnteredFormat",
            }},
            {"setBasicFilter": {"filter": {"range": {
                "sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": len(values),
                "startColumnIndex": 0, "endColumnIndex": 6,
            }}}},
        ])
        for column, width in enumerate([260, 180, 140, 160, 260, 260]):
            batch.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": column, "endIndex": column + 1},
                "properties": {"pixelSize": width}, "fields": "pixelSize",
            }})
        # Creation, data, formatting and retention are one atomic Google Sheets operation.
        batch.extend({"deleteSheet": {"sheetId": sheet["properties"]["sheetId"]}} for sheet in expired)
        return batch, GoogleStatisticsExportResult(title, sheet_id, len(expired))
