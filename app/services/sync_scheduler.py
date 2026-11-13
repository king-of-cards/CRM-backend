"""Runs the Google Sheet syncs in the background every GOOGLE_SHEETS_SYNC_MINUTES.

Started from main.py's lifespan. Set GOOGLE_SHEETS_SYNC_MINUTES=0 to turn it off
(the "Sync now" button still works). With several server processes in production,
run it in only one of them.
"""
import asyncio
import logging

from sqlmodel import Session

from app.core.config import settings
from app.db.session import engine
from app.services import sheet_sync

logger = logging.getLogger(__name__)


def _sync_all() -> None:
    changed = False
    for source in sheet_sync.syncable_sources():
        try:
            with Session(engine) as session:
                run = sheet_sync.run_sync(session, source, trigger="schedule")
            logger.info(
                "Sheet sync '%s': %s (+%d new, %d updated, %d removed)",
                source, run.status, run.inserted, run.updated, run.removed,
            )
            changed = changed or bool(run.inserted or run.updated or run.removed)
        except sheet_sync.SyncAlreadyRunning:
            logger.info("Sheet sync '%s' skipped: another sync is running", source)
        except Exception:  # noqa: BLE001
            logger.exception("Sheet sync '%s' crashed", source)

    # Keep the dashboard numbers instant: rebuild them now instead of on the next visit
    from app.api.v1.endpoints.analytics import warm_overview

    if changed:
        warm_overview()


async def sync_loop() -> None:
    minutes = settings.GOOGLE_SHEETS_SYNC_MINUTES
    if minutes <= 0 or not sheet_sync.syncable_sources():
        return
    await asyncio.sleep(5)  # let the server finish starting
    while True:
        await asyncio.to_thread(_sync_all)  # blocking work stays off the event loop
        await asyncio.sleep(minutes * 60)