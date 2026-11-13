from datetime import datetime

from sqlmodel import Field, SQLModel


class SheetSyncRun(SQLModel, table=True):
    """Log of every Google Sheet → database sync (one row per run)."""
    __tablename__ = "crm_sheet_sync_runs"

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(max_length=50, index=True)
    trigger: str = Field(max_length=20)                 # "schedule" | "manual"
    started_at: datetime
    finished_at: datetime | None = None
    status: str = Field(max_length=20, default="running")  # "running" | "success" | "failed"
    rows_read: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    error: str | None = Field(default=None, max_length=1000)