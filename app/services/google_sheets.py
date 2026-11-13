"""Read-only access to Google Sheets through a Google service account.

Setup (once):
  1. Google Cloud: enable the "Google Sheets API" and create a service account with a JSON key.
  2. Share each sheet with the service account's email (Viewer is enough).
  3. .env:
       GOOGLE_SERVICE_ACCOUNT_FILE=google-service-account.json
       GOOGLE_SHEETS={"whatsapp": "<sheet id>", "another": "<sheet id>"}
     The sheet id is the long part of the sheet's URL between /d/ and /edit.

Each sheet gets a short name ("source"). The API only ever reads sheets listed in GOOGLE_SHEETS.
Results are cached in memory for GOOGLE_SHEET_CACHE_SECONDS (the Sheets API is slow and has quotas).
"""
import threading
import time
from datetime import datetime, timezone
from typing import Any

import gspread

from app.core.config import settings


class SheetsNotConfigured(Exception):
    """The key file setting or the GOOGLE_SHEETS list is missing in .env."""


class UnknownSource(Exception):
    """The requested source name is not in GOOGLE_SHEETS."""


_client: gspread.Client | None = None
_client_lock = threading.Lock()

_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def sources() -> list[str]:
    """Names of the configured sheets, e.g. ["whatsapp"]."""
    return list(settings.GOOGLE_SHEETS.keys())


def _sheet_id(source: str) -> str:
    if not settings.GOOGLE_SHEETS:
        raise SheetsNotConfigured("No Google Sheets are configured on the server (GOOGLE_SHEETS).")
    try:
        return settings.GOOGLE_SHEETS[source]
    except KeyError:
        raise UnknownSource(f"Unknown sheet '{source}'. Configured: {', '.join(sources())}") from None


def _get_client() -> gspread.Client:
    global _client
    if not settings.GOOGLE_SERVICE_ACCOUNT_FILE:
        raise SheetsNotConfigured("Google Sheets is not configured on the server (GOOGLE_SERVICE_ACCOUNT_FILE).")
    with _client_lock:
        if _client is None:
            # Read-only scope: the backend can never change a sheet
            _client = gspread.service_account(
                filename=settings.GOOGLE_SERVICE_ACCOUNT_FILE,
                scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
            )
        return _client


def _spreadsheet(source: str) -> gspread.Spreadsheet:
    return _get_client().open_by_key(_sheet_id(source))


def list_worksheets(source: str) -> list[dict[str, Any]]:
    """The tabs in one sheet: [{ id, title, rows, cols }]."""
    return [
        {"id": ws.id, "title": ws.title, "rows": ws.row_count, "cols": ws.col_count}
        for ws in _spreadsheet(source).worksheets()
    ]


def _unique_headers(raw: list[str], width: int) -> list[str]:
    """First row as column names; blank or repeated headers get a safe name."""
    headers: list[str] = []
    seen: dict[str, int] = {}
    for i in range(width):
        name = (raw[i] if i < len(raw) else "").strip() or f"Column {i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        headers.append(name)
    return headers


def read_sheet(source: str, worksheet: str | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """All rows of one tab (the first tab if worksheet is None), using row 1 as headers.

    Returns { source, sheet, worksheet, columns, rows, fetched_at }.
    Each row also has "_row": its row number in the sheet.
    """
    key = (source, worksheet or "")
    now = time.monotonic()

    if not refresh:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < settings.GOOGLE_SHEET_CACHE_SECONDS:
                return hit[1]

    sheet = _spreadsheet(source)
    ws = sheet.worksheet(worksheet) if worksheet else sheet.get_worksheet(0)
    values = ws.get_all_values()  # list of rows, each a list of cell strings (as displayed)

    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    if values:
        width = max(len(r) for r in values)
        columns = _unique_headers(values[0], width)
        for row_number, raw in enumerate(values[1:], start=2):
            cells = raw + [""] * (width - len(raw))
            if not any(c.strip() for c in cells):
                continue  # skip completely empty rows
            row: dict[str, Any] = dict(zip(columns, cells))
            row["_row"] = row_number
            rows.append(row)

    data = {
        "source": source,
        "sheet": sheet.title,
        "worksheet": ws.title,
        "columns": columns,
        "rows": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    with _cache_lock:
        _cache[key] = (now, data)
    return data