"""GET /spin-leads: admin-only, read-only listing of the PostgreSQL table spin_lead.

Settings you may want to adjust (module level, below):
  HIDDEN_COLUMNS       table columns that are never selected, returned, searched or sorted
  HIDDEN_JSON_KEYS     keys inside expanded JSON columns, excluded the same way
  JSON_EXPAND_COLUMNS  jsonb columns returned as separate "<column>.<key>" fields (raw column never returned)
  RANGE_SORT_KEYS      JSON keys holding ranges like "10-20", sorted by their first number
  REQUIRED_JSON_KEYS   keys in additional_details that must have a non-blank value;
                       leads without one are never listed, counted, searched or discovered
"""
import json
import logging
import threading
import time as _time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import (
    Boolean, LargeBinary, MetaData, Numeric, String, Table, Text,
    ColumnElement, bindparam, case, cast, false, func, or_, select, true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

from app.api.deps import AdminUser
from app.core.config import settings
from app.db.session import read_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/spin-leads", tags=["spin leads"])

TABLE_NAME = "spin_lead"

# ── Settings ────────────────────────────────────────────────────────────
HIDDEN_COLUMNS: set[str] = {"session_id", "consent_version", "created_by_user_id"}
HIDDEN_JSON_KEYS: set[str] = set()
JSON_EXPAND_COLUMNS: list[str] = ["additional_details"]
RANGE_SORT_KEYS: set[str] = {"estimatedBudgetPerCard", "serviceBudget"}
REQUIRED_JSON_KEYS: set[str] = {"otherWeddingServices"}
REQUIRED_JSON_COLUMN = "additional_details"   # the JSON column REQUIRED_JSON_KEYS refer to
BLANK_PATTERN = r"^\s*$"                      # empty or whitespace-only text

JSON_KEYS_CACHE_SECONDS = 600    # re-discover JSON keys every 10 minutes
JSON_KEYS_MAX = 100              # at most this many keys per JSON column (most common first)
JSON_TYPE_THRESHOLD = 0.95       # a type must cover 95% of non-null values, else "mixed"
RANGE_PATTERN = r"^\s*([0-9]+(?:\.[0-9]+)?)"   # first number in "10-20", "10_20", ...
LIKE_ESCAPE = "/"

_JSONB_TYPES = {
    "number": "number", "boolean": "boolean", "string": "string",
    "object": "json", "array": "json",
}


@dataclass(frozen=True)
class JsonField:
    source: str   # JSON column, e.g. "additional_details"
    key: str      # top-level key
    type: str     # number | boolean | string | json | mixed | range

    @property
    def name(self) -> str:
        return f"{self.source}.{self.key}"


# ── Reflection and key discovery (cached) ──────────────────────────────
_table: Table | None = None
_table_lock = threading.Lock()
_json_cache: dict[str, tuple[float, list[JsonField]]] = {}   # source -> (expires_at, fields)
_json_lock = threading.Lock()


def _get_table() -> Table:
    """Reflect spin_lead once and cache it, so columns come from the database."""
    global _table
    if _table is None:
        with _table_lock:
            if _table is None:
                _table = Table(
                    TABLE_NAME, MetaData(),
                    schema=settings.READ_DB_SCHEMA, autoload_with=read_engine,
                )
    return _table


def _jsonb(table: Table, source: str):
    col = table.c[source]
    return col if isinstance(col.type, JSONB) else cast(col, JSONB)


class RequiredFilterError(Exception):
    """REQUIRED_JSON_KEYS can't be applied (column missing or hidden). Fails closed."""


def _required_filter(table: Table) -> list[ColumnElement]:
    """Base filter applied to every query: each required key must have a non-blank value.

    additional_details ->> :key IS NOT NULL AND additional_details ->> :key !~ :blank
    JSON null and a missing key both give SQL NULL from ->>, so both are excluded.
    """
    if not REQUIRED_JSON_KEYS:
        return []
    if REQUIRED_JSON_COLUMN not in table.c or REQUIRED_JSON_COLUMN in HIDDEN_COLUMNS:
        # Never fall back to listing all leads
        raise RequiredFilterError(REQUIRED_JSON_COLUMN)
    conditions = []
    for key in sorted(REQUIRED_JSON_KEYS):
        value = _jsonb(table, REQUIRED_JSON_COLUMN).op("->>", return_type=Text)(
            bindparam(None, key, type_=Text)
        )
        conditions.append(value.is_not(None))
        conditions.append(
            value.op("!~", is_comparison=True)(bindparam(None, BLANK_PATTERN, type_=Text))
        )
    return conditions


def _discovery_query(table: Table, source: str, base: list[ColumnElement]):
    """Rows of (key, jsonb_typeof(value), count) across the filtered leads."""
    doc = _jsonb(table, source)
    # Anything that is not an object (SQL NULL, JSON null) becomes {} so jsonb_each can't fail
    obj = case((func.jsonb_typeof(doc) == "object", doc), else_=func.jsonb_build_object())
    kv = func.jsonb_each(obj).table_valued("key", "value").lateral("kv")
    typ = func.jsonb_typeof(kv.c["value"])
    return (
        select(kv.c["key"], typ, func.count())
        .select_from(table.join(kv, true()))
        .where(*base)
        .group_by(kv.c["key"], typ)
    )


def _detect_type(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    if total:
        best, n = max(counts.items(), key=lambda item: item[1])
        if n >= JSON_TYPE_THRESHOLD * total:
            return best
    return "mixed"


def _discover(conn, table: Table, source: str, base: list[ColumnElement]) -> list[JsonField]:
    seen: dict[str, int] = {}                 # key -> rows containing it
    types: dict[str, dict[str, int]] = {}     # key -> {mapped type: count}, nulls ignored
    for key, typ, n in conn.execute(_discovery_query(table, source, base)):
        if key in HIDDEN_JSON_KEYS:
            continue
        seen[key] = seen.get(key, 0) + n
        mapped = _JSONB_TYPES.get(typ)
        if mapped:
            per_key = types.setdefault(key, {})
            per_key[mapped] = per_key.get(mapped, 0) + n
    keys = sorted(seen, key=lambda k: (-seen[k], k))[:JSON_KEYS_MAX]
    return [
        JsonField(source, k, "range" if k in RANGE_SORT_KEYS else _detect_type(types.get(k, {})))
        for k in keys
    ]


def _get_json_fields(table: Table, base: list[ColumnElement]) -> dict[str, list[JsonField]]:
    """Expanded fields per JSON column. Missing or hidden columns are skipped silently."""
    sources = [s for s in JSON_EXPAND_COLUMNS if s in table.c and s not in HIDDEN_COLUMNS]

    def expired(s: str) -> bool:
        return s not in _json_cache or _json_cache[s][0] <= _time.monotonic()

    if any(expired(s) for s in sources):
        with _json_lock:
            stale = [s for s in sources if expired(s)]
            if stale:
                with read_engine.connect() as conn:
                    for s in stale:
                        fields = _discover(conn, table, s, base)
                        _json_cache[s] = (_time.monotonic() + JSON_KEYS_CACHE_SECONDS, fields)
    return {s: _json_cache[s][1] for s in sources}


# ── Expressions ────────────────────────────────────────────────────────
def _json_value_expr(table: Table, f: JsonField):
    """source -> :key (jsonb), key as a bound parameter."""
    return _jsonb(table, f.source).op("->", return_type=JSONB)(bindparam(None, f.key, type_=Text))


def _json_text_expr(table: Table, f: JsonField):
    """source ->> :key (text), key as a bound parameter."""
    return _jsonb(table, f.source).op("->>", return_type=Text)(bindparam(None, f.key, type_=Text))


def _json_sort_expr(table: Table, f: JsonField):
    v = _json_value_expr(table, f)
    if f.type == "number":
        return case((func.jsonb_typeof(v) == "number", cast(v, Numeric)))
    if f.type == "boolean":
        return case((func.jsonb_typeof(v) == "boolean", cast(v, Boolean)))
    if f.type == "range":
        return cast(func.substring(_json_text_expr(table, f), RANGE_PATTERN), Numeric)
    if f.type in ("string", "mixed"):
        return _json_text_expr(table, f)
    return None                                               # json: not sortable


def _like_pattern(term: str) -> str:
    escaped = term.replace("/", "//").replace("%", "/%").replace("_", "/_")
    return f"%{escaped}%"


# ── Value conversion ───────────────────────────────────────────────────
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


def _json_field_value(value: Any) -> Any:
    """pg8000 already decodes jsonb: str/int/float/bool/None stay as they are."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)   # object/array


def _type_name(col) -> str:
    try:
        return str(col.type.compile(dialect=read_engine.dialect))
    except Exception:
        return type(col.type).__name__


# ── Errors: generic responses, server-side log without SQL or values ───
def _db_error(exc: Exception, stage: str) -> HTTPException:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ())
    sqlstate = args[0].get("C") if args and isinstance(args[0], dict) else None
    logger.error("spin-leads %s failed: %s (SQLSTATE %s)", stage, type(exc).__name__, sqlstate)
    if sqlstate == "57014":   # query_canceled: statement_timeout
        return HTTPException(503, "Read database query timed out")
    return HTTPException(503, "Read database unavailable")


# ── Endpoint ───────────────────────────────────────────────────────────
@router.get("")
def list_spin_leads(
    _admin: AdminUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None, description="Case-insensitive match across visible text fields"),
    sort: str | None = Query(None, description="Column name or additional_details.<key>"),
    order: Literal["asc", "desc"] = Query("asc"),
):
    if read_engine is None:
        raise HTTPException(503, "Read database is not configured")

    try:
        table = _get_table()
        base = _required_filter(table)          # applied to discovery, count and rows
        json_fields = _get_json_fields(table, base)
    except RequiredFilterError:
        logger.error("spin-leads: REQUIRED_JSON_COLUMN is missing or hidden; refusing to list leads")
        raise HTTPException(503, "Read database unavailable")
    except NoSuchTableError:
        logger.error("spin-leads: table not found in the read database")
        raise HTTPException(404, "Table not found")
    except SQLAlchemyError as exc:
        raise _db_error(exc, "metadata")

    # Output layout, in table order. Hidden and binary columns are never selected;
    # JSON columns are replaced by their expanded fields.
    columns_meta: list[dict] = []
    select_exprs = []
    converters = []
    sortable: dict[str, Any] = {}       # name -> sort expression (None = not sortable)
    text_exprs = []                     # searchable expressions

    for col in table.columns:
        if col.name in HIDDEN_COLUMNS or isinstance(col.type, LargeBinary):
            continue
        if col.name in JSON_EXPAND_COLUMNS:
            for f in json_fields.get(col.name, []):
                expr = _json_value_expr(table, f)
                columns_meta.append({
                    "name": f.name, "type": f.type, "json_field": True,
                    "source": f.source, "key": f.key,
                })
                select_exprs.append(expr)
                converters.append(_json_field_value)
                sortable[f.name] = _json_sort_expr(table, f)
                text_exprs.append(_json_text_expr(table, f))
            continue
        columns_meta.append({"name": col.name, "type": _type_name(col)})
        select_exprs.append(col)
        converters.append(_to_json)
        sortable[col.name] = col
        if isinstance(col.type, String):
            text_exprs.append(cast(col, Text))

    # Sort: only names listed in "columns" (json-typed fields excluded); empty values last
    order_by = []
    if sort is not None:
        sort_expr = sortable.get(sort)
        if sort_expr is None:
            raise HTTPException(400, "Invalid sort column")
        order_by.append((sort_expr.desc() if order == "desc" else sort_expr.asc()).nulls_last())
    for pk in table.primary_key.columns:                      # stable paging
        if pk.name != sort:
            order_by.append(pk.desc() if order == "desc" else pk.asc())

    # Search: one bound parameter, % _ / escaped, ILIKE on visible fields only.
    # The required-key base filter always comes first.
    conditions = list(base)
    term = (search or "").strip()
    if term:
        pattern = bindparam("search_pattern", _like_pattern(term), type_=Text)
        conditions.append(
            or_(*(e.ilike(pattern, escape=LIKE_ESCAPE) for e in text_exprs))
            if text_exprs else false()
        )

    query = (
        select(*(e.label(f"f{i}") for i, e in enumerate(select_exprs)))
        .where(*conditions)
        .order_by(*order_by)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    count_query = select(func.count()).select_from(table).where(*conditions)

    try:
        with read_engine.connect() as conn:
            total = conn.execute(count_query).scalar_one()
            rows = conn.execute(query).all()
    except SQLAlchemyError as exc:
        raise _db_error(exc, "query")

    names = [m["name"] for m in columns_meta]
    return {
        "columns": columns_meta,
        "items": [
            {name: conv(value) for name, conv, value in zip(names, converters, row)}
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
