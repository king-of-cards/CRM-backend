"""GET /spin-leads: admin-only, read-only listing of the PostgreSQL table spin_lead."""
import threading
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import LargeBinary, MetaData, String, Table, Text, cast, false, func, or_, select
from sqlalchemy.exc import InterfaceError, NoSuchTableError, OperationalError

from app.api.deps import AdminUser
from app.core.config import settings
from app.db.session import read_engine

router = APIRouter(prefix="/spin-leads", tags=["spin leads"])

TABLE_NAME = "spin_lead"

_table: Table | None = None
_table_lock = threading.Lock()


def _get_table() -> Table:
    """Reflect spin_lead once and cache it, so all columns come from the database."""
    global _table
    if _table is None:
        with _table_lock:
            if _table is None:
                _table = Table(
                    TABLE_NAME, MetaData(),
                    schema=settings.READ_DB_SCHEMA, autoload_with=read_engine,
                )
    return _table


def _type_name(col) -> str:
    try:
        return str(col.type.compile(dialect=read_engine.dialect))
    except Exception:
        return type(col.type).__name__


def _to_json(value: Any) -> Any:
    if isinstance(value, datetime):   # check before date: datetime is a subclass of date
        return value.isoformat()      # as stored, no timezone conversion
    if isinstance(value, (date, time)):
        return value.isoformat()      # date -> YYYY-MM-DD, no time added
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, list):       # PostgreSQL arrays
        return [_to_json(v) for v in value]
    return value                      # str/int/float/bool/None and JSON (dict/list) pass through


@router.get("")
def list_spin_leads(
    _admin: AdminUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None, description="Case-insensitive match across text columns"),
    sort: str | None = Query(None, description="Column name to sort by"),
    order: Literal["asc", "desc"] = Query("asc"),
):
    if read_engine is None:
        raise HTTPException(503, "Read database is not configured (set READ_DB_HOST in .env)")

    try:
        table = _get_table()
    except NoSuchTableError:
        raise HTTPException(404, f"Table '{settings.READ_DB_SCHEMA}.{TABLE_NAME}' not found")
    except (OperationalError, InterfaceError) as exc:
        raise HTTPException(503, f"Read database unreachable: {exc.__class__.__name__}")

    # Binary columns are skipped entirely (not selected, not returned)
    columns = [c for c in table.columns if not isinstance(c.type, LargeBinary)]

    # Sort: only reflected column names are accepted; the name is never put into SQL text
    if sort is not None and sort not in {c.name for c in table.columns}:
        raise HTTPException(400, f"Invalid sort column '{sort}'")
    sort_cols = [table.c[sort]] if sort else []
    sort_cols += [c for c in table.primary_key.columns if c.name != sort]  # stable paging
    order_by = [c.desc() if order == "desc" else c.asc() for c in sort_cols]

    # Search: bound parameter, with % and _ escaped (autoescape)
    conditions = []
    term = (search or "").strip()
    if term:
        text_cols = [c for c in columns if isinstance(c.type, String)]
        conditions.append(
            or_(*(cast(c, Text).icontains(term, autoescape=True) for c in text_cols))
            if text_cols else false()
        )

    try:
        with read_engine.connect() as conn:
            total = conn.execute(
                select(func.count()).select_from(table).where(*conditions)
            ).scalar_one()
            rows = conn.execute(
                select(*columns).where(*conditions).order_by(*order_by)
                .limit(page_size).offset((page - 1) * page_size)
            ).mappings().all()
    except (OperationalError, InterfaceError) as exc:
        raise HTTPException(503, f"Read database unreachable: {exc.__class__.__name__}")

    return {
        "columns": [{"name": c.name, "type": _type_name(c)} for c in columns],
        "items": [{k: _to_json(v) for k, v in row.items()} for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
