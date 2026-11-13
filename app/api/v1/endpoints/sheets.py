"""Google Sheets endpoints (admin only).

GET  /sheets/sources              configured sheets + their last sync
GET  /sheets/{source}/worksheets  tabs in one sheet
GET  /sheets/{source}/data        live rows from the sheet (search + pages), straight from Google
POST /sheets/{source}/sync        copy the sheet into the database now
"""
import logging

import gspread
from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from app.api.deps import AdminUser, DbSession
from app.services import google_sheets, sheet_sync
from app.services.google_sheets import SheetsNotConfigured, UnknownSource

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sheets", tags=["google-sheets"])


def translate_sheets_error(exc: Exception) -> HTTPException:
    """Turn Google / gspread errors into clear API errors."""
    if isinstance(exc, SheetsNotConfigured):
        return HTTPException(503, str(exc))
    if isinstance(exc, UnknownSource):
        return HTTPException(404, str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(503, "The Google service account key file was not found on the server.")
    if isinstance(exc, gspread.exceptions.SpreadsheetNotFound):
        return HTTPException(404, "Spreadsheet not found. Check its id and share it with the service account email.")
    if isinstance(exc, gspread.exceptions.WorksheetNotFound):
        return HTTPException(404, "That tab does not exist in the spreadsheet.")
    if isinstance(exc, gspread.exceptions.APIError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 403:
            return HTTPException(403, "The service account has no access to this sheet. Share it with the service account email.")
        if status == 429:
            return HTTPException(503, "Google Sheets rate limit reached. Try again in a minute.")
        logger.warning("Google Sheets API error: %s", exc)
        return HTTPException(502, "Google Sheets returned an error.")
    logger.exception("Unexpected Google Sheets error")
    return HTTPException(502, "Could not read the Google Sheet.")


def _utc(value):
    """MySQL returns naive datetimes; they are stored as UTC."""
    if value is None:
        return None
    return value.isoformat() + ("Z" if value.tzinfo is None else "")


def _run_summary(run) -> dict | None:
    if run is None:
        return None
    return {
        "status": run.status,
        "trigger": run.trigger,
        "started_at": _utc(run.started_at),
        "finished_at": _utc(run.finished_at),
        "rows_read": run.rows_read,
        "inserted": run.inserted,
        "updated": run.updated,
        "unchanged": run.unchanged,
        "removed": run.removed,
        "error": run.error,
    }


@router.get("/sources")
def list_sources(_: AdminUser, db: DbSession):
    runs = sheet_sync.last_runs(db)
    return {
        "sources": [
            {
                "source": s,
                "syncable": s in sheet_sync.SYNCERS,
                "latest_sync": _run_summary(runs.get(s, {}).get("latest")),
                "last_success": _run_summary(runs.get(s, {}).get("last_success")),
            }
            for s in google_sheets.sources()
        ]
    }


# Plain `def`: gspread is blocking, so FastAPI runs these in a worker thread.
@router.get("/{source}/worksheets")
def worksheets(source: str, _: AdminUser):
    try:
        return {"worksheets": google_sheets.list_worksheets(source)}
    except Exception as exc:  # noqa: BLE001
        raise translate_sheets_error(exc) from exc


@router.get("/{source}/data")
def sheet_data(
    source: str,
    _: AdminUser,
    worksheet: str | None = Query(None, description="Tab name; the first tab if omitted"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    search: str | None = Query(None, max_length=200),
    refresh: bool = Query(False, description="Skip the cache and read the sheet again"),
):
    try:
        data = google_sheets.read_sheet(source, worksheet, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        raise translate_sheets_error(exc) from exc

    rows = [{k: v for k, v in r.items() if k != "_row"} for r in data["rows"]]
    if search and search.strip():
        needle = search.strip().lower()
        rows = [r for r in rows if any(needle in str(v).lower() for v in r.values())]

    total = len(rows)
    start = (page - 1) * page_size
    return {
        "source": source,
        "sheet": data["sheet"],
        "worksheet": data["worksheet"],
        "fetched_at": data["fetched_at"],
        "columns": [{"name": c, "type": "text"} for c in data["columns"]],
        "items": rows[start : start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/{source}/sync")
async def sync_now(source: str, _: AdminUser, db: DbSession):
    try:
        run = await run_in_threadpool(sheet_sync.run_sync, db, source, "manual")
    except sheet_sync.SyncAlreadyRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    except UnknownSource as exc:
        raise HTTPException(404, str(exc)) from exc
    summary = _run_summary(run)
    if run.status == "success" and (run.inserted or run.updated or run.removed):
        from app.api.v1.endpoints.analytics import warm_overview

        warm_overview()  # dashboard numbers include the new rows right away
    if run.status == "failed":
        raise HTTPException(502, f"Sync failed: {run.error}")
    return summary