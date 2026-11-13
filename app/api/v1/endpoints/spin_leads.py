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
    Boolean, LargeBinary, MetaData, Numeric, String, Table, Text, literal_column,
    ColumnElement, and_, bindparam, case, cast, false, func, or_, select, true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

from app.api.deps import AdminUser, DbSession
from app.core import response_cache
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


# ── Query (uncached) ───────────────────────────────────────────────────
# ── Branch (store) of a lead ───────────────────────────────────────────
BRANCH_FIELD_NAMES = ("branchid", "spinbranchid", "branch", "storeid", "store")


def _norm_name(name: str) -> str:
    return "".join(ch for ch in name.lower().split(".")[-1] if ch.isalnum())


def _branch_field(table: Table, json_fields: dict) -> tuple[str | None, Any]:
    """(output name, text expression) of the column holding the lead's branch id, if any.
    A real column (e.g. branch_id) wins over a key inside a JSON column (e.g. branchId)."""
    for wanted in BRANCH_FIELD_NAMES:
        for col in table.columns:
            if col.name not in HIDDEN_COLUMNS and _norm_name(col.name) == wanted:
                return col.name, cast(col, Text)
        for fields in json_fields.values():
            for f in fields:
                if _norm_name(f.key) == wanted:
                    return f.name, _json_text_expr(table, f)
    return None, None


PINCODE_FIELD_NAMES = ("pincode", "pin", "zipcode", "postalcode")
BUDGET_FIELD_NAMES = ("estimatedbudgetpercard", "servicebudget", "budget")


def _named_field(table: Table, json_fields: dict, wanted_names: tuple[str, ...]) -> tuple[str | None, Any]:
    """(output name, text expression) of the first column / JSON key with one of these names."""
    for wanted in wanted_names:
        for col in table.columns:
            if col.name not in HIDDEN_COLUMNS and _norm_name(col.name) == wanted:
                return col.name, cast(col, Text)
        for fields in json_fields.values():
            for f in fields:
                if _norm_name(f.key) == wanted:
                    return f.name, _json_text_expr(table, f)
    return None, None


def spin_filter_options() -> dict:
    """Values for the filter dropdowns: the budget ranges and pincodes used on spin leads, with counts."""
    if read_engine is None:
        return {"budgets": [], "pincodes": [], "has_branch": False, "has_pincode": False, "has_budget": False}
    try:
        table = _get_table()
        base = _required_filter(table)
        json_fields = _get_json_fields(table, base)
        _, branch_expr = _branch_field(table, json_fields)
        _, pin_expr = _named_field(table, json_fields, PINCODE_FIELD_NAMES)
        _, budget_expr = _named_field(table, json_fields, BUDGET_FIELD_NAMES)
        budgets = []
        if budget_expr is not None:
            # GROUP BY 1: the JSON key is a bound parameter, so repeating the expression
            # would make PostgreSQL see two different expressions
            query = select(budget_expr.label("v"), func.count()).select_from(table).where(*base).group_by(literal_column("1"))
            with read_engine.connect() as conn:
                rows = [(v, n) for v, n in conn.execute(query).all() if v not in (None, "")]

            def first_number(v):
                digits = "".join(ch if ch.isdigit() else " " for ch in str(v)).split()
                return int(digits[0]) if digits else 10**9

            budgets = [{"value": v, "count": n} for v, n in sorted(rows, key=lambda r: (first_number(r[0]), str(r[0])))]
        pincodes = []
        if pin_expr is not None:
            # GROUP BY 1: same reason as above
            query = select(pin_expr.label("p"), func.count()).select_from(table).where(*base).group_by(literal_column("1"))
            with read_engine.connect() as conn:
                rows = [(str(p).strip(), n) for p, n in conn.execute(query).all() if p not in (None, "") and str(p).strip()]
            merged: dict[str, int] = {}
            for p, n in rows:
                merged[p] = merged.get(p, 0) + n
            pincodes = [{"value": p, "count": n} for p, n in sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))][:1000]
        return {
            "budgets": budgets,
            "pincodes": pincodes,
            "has_branch": branch_expr is not None,
            "has_pincode": pin_expr is not None,
            "has_budget": budget_expr is not None,
        }
    except Exception:  # noqa: BLE001
        logger.warning("spin-leads: filter options unavailable", exc_info=True)
        return {"budgets": [], "pincodes": [], "has_branch": False, "has_pincode": False, "has_budget": False}


def spin_branch_counts() -> dict[str, int]:
    """{branch id (text): number of listed spin leads}. Empty if the branch isn't stored."""
    if read_engine is None:
        return {}
    try:
        table = _get_table()
        base = _required_filter(table)
        name, expr = _branch_field(table, _get_json_fields(table, base))
        if expr is None:
            return {}
        query = select(expr.label("b"), func.count()).select_from(table).where(*base).group_by(literal_column("1"))
        with read_engine.connect() as conn:
            return {str(b): n for b, n in conn.execute(query).all() if b is not None}
    except Exception:  # noqa: BLE001
        logger.warning("spin-leads: branch counts unavailable", exc_info=True)
        return {}


def list_spin_leads(
    _admin: Any = None,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    sort: str | None = None,
    order: Literal["asc", "desc"] = "asc",
    branch: str | None = None,
    filters: dict | None = None,
):
    """filters (all optional):
      pincode        leads whose pincode starts with this
      budget         exact budget value (e.g. "10-20")
      ids_in         only these lead ids (from the CRM filters); [] = none
      ids_not_in     never these lead ids
      vendor         {manual_for, manual_other, branch_ids, pins, prefixes, fallback,
                      other_pins, other_prefixes}: leads whose effective vendor is one vendor
    """
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

    # Branch filter: only leads from this store
    branch_name, branch_expr = _branch_field(table, json_fields)
    if branch not in (None, ""):
        conditions.append(branch_expr == str(branch) if branch_expr is not None else false())

    # Other filters (pincode, budget, and the CRM-based ones turned into lead ids)
    flt = filters or {}
    pk_text = cast(list(table.primary_key.columns)[0], Text)
    _, pin_expr = _named_field(table, json_fields, PINCODE_FIELD_NAMES)
    _, budget_expr = _named_field(table, json_fields, BUDGET_FIELD_NAMES)

    if flt.get("pincode"):
        pin_pattern = bindparam("pin_pattern", _like_pattern(flt["pincode"]).strip("%") + "%", type_=Text)
        conditions.append(pin_expr.ilike(pin_pattern, escape=LIKE_ESCAPE) if pin_expr is not None else false())
    if flt.get("budget"):
        conditions.append(budget_expr == str(flt["budget"]) if budget_expr is not None else false())
    if flt.get("ids_in") is not None:
        ids = [str(i) for i in flt["ids_in"]]
        conditions.append(pk_text.in_(ids) if ids else false())
    if flt.get("ids_not_in"):
        conditions.append(pk_text.not_in([str(i) for i in flt["ids_not_in"]]))
    if flt.get("vendor"):
        v = flt["vendor"]
        # 1) the lead's branch belongs to this vendor
        parts = []
        if branch_expr is not None and v["branch_ids"]:
            parts.append(branch_expr.in_([str(b) for b in v["branch_ids"]]))
        # 2) no branch: the vendor's pincode rules (fallback = no other vendor's pincode matches)
        if pin_expr is not None:
            no_branch = or_(branch_expr.is_(None), branch_expr == "") if branch_expr is not None else true()
            if v["fallback"]:
                others = [pin_expr.in_(v["other_pins"])] if v["other_pins"] else []
                others += [pin_expr.like(f"{p}%") for p in v["other_prefixes"]]
                pin_rule = and_(pin_expr.is_not(None), pin_expr != "", *(~o for o in others))
            else:
                matches = [pin_expr.in_(v["pins"])] if v["pins"] else []
                matches += [pin_expr.like(f"{p}%") for p in v["prefixes"]]
                # …but not pincodes a vendor checked earlier claims (RJ's 560018 isn't Phodeo's)
                earlier = [pin_expr.in_(v["before_pins"])] if v.get("before_pins") else []
                earlier += [pin_expr.like(f"{p}%") for p in v.get("before_prefixes", [])]
                pin_rule = and_(or_(*matches), *(~e for e in earlier)) if matches else false()
            parts.append(and_(no_branch, pin_rule))
        automatic = or_(*parts) if parts else false()
        if v["manual_other"]:
            automatic = and_(pk_text.not_in(v["manual_other"]), automatic)
        conditions.append(or_(pk_text.in_(v["manual_for"]), automatic) if v["manual_for"] else automatic)

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
        "branch_column": branch_name,
    }


# ── Endpoint (cached) ──────────────────────────────────────────────────
# The workflow database is remote, so each page is kept in memory: repeat requests are
# answered instantly and refreshed in the background (see app/core/response_cache.py).
FRESH_SECONDS = 60
STALE_SECONDS = 1800


def _cache_key(page, page_size, search, sort, order, branch=None):
    return ("spin", page, page_size, (search or "").strip().lower(), sort, order, branch or None)


def _spin_location_keys(columns: list[dict]) -> tuple[str | None, str | None]:
    """Names of the pincode and city/location fields in this table's output, if any."""
    names = [c["name"] for c in columns]
    lower = {n: n.lower().split(".")[-1] for n in names}
    pin = next((n for n in names if "pincode" in lower[n] or lower[n] == "pin"), None)
    city = next((n for n in names if "city" in lower[n] or "location" in lower[n]), None)
    return pin, city


QUOTE_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


def _crm_filters(db, pincode, budget, vendor_id, interest, converted, quoted_op, quoted_value) -> dict:
    """Turn the filter choices into conditions the read-only database understands.
    CRM fields (interest, converted, quote, assigned) live in our crm_lead_meta table, so
    they become lists of lead ids."""
    from sqlmodel import select as sm_select

    from app.models.lead_meta import LeadMeta
    from app.services import lead_crm
    from app.services.branches import branches_by_id

    flt: dict = {}
    if pincode and pincode.strip():
        flt["pincode"] = "".join(ch for ch in pincode if ch.isdigit()) or pincode.strip()
    if budget:
        flt["budget"] = budget

    uses_meta = interest or converted or quoted_op or vendor_id is not None
    if not uses_meta:
        return flt
    metas = list(db.exec(sm_select(LeadMeta).where(LeadMeta.source == "spin")))
    include: set[str] | None = None
    exclude: set[str] = set()

    def narrow(keys: set[str]) -> None:
        nonlocal include
        include = keys if include is None else include & keys

    if interest == "none":
        exclude |= {m.lead_key for m in metas if m.interest}
    elif interest:
        narrow({m.lead_key for m in metas if m.interest == interest})

    if converted == "yes":
        narrow({m.lead_key for m in metas if m.converted})
    elif converted == "no":
        exclude |= {m.lead_key for m in metas if m.converted}

    if quoted_op == "set":
        narrow({m.lead_key for m in metas if m.quoted_amount is not None})
    elif quoted_op == "unset":
        exclude |= {m.lead_key for m in metas if m.quoted_amount is not None}
    elif quoted_op and quoted_value is not None:
        cmp = QUOTE_OPS[quoted_op]
        narrow({m.lead_key for m in metas if m.quoted_amount is not None and cmp(m.quoted_amount, quoted_value)})

    if include is not None:
        flt["ids_in"] = sorted(include - exclude)
    elif exclude:
        flt["ids_not_in"] = sorted(exclude)

    if vendor_id is not None:
        vendors = lead_crm.active_vendors(db)
        target = next((v for v in vendors if v.id == vendor_id), None)
        if target is None:
            flt["ids_in"] = []
        else:
            others = [v for v in vendors if v.id != vendor_id and not v.is_fallback]
            before = [v for v in others if (v.priority, v.id) < (target.priority, target.id)]
            flt["vendor"] = {
                "manual_for": sorted(m.lead_key for m in metas if m.vendor_id == vendor_id),
                "manual_other": sorted(m.lead_key for m in metas if m.vendor_id and m.vendor_id != vendor_id),
                "branch_ids": [b["id"] for b in branches_by_id(db).values() if b["vendor_id"] == vendor_id],
                "pins": list(target.pincodes or []),
                "prefixes": list(target.pincode_prefixes or []),
                "fallback": bool(target.is_fallback),
                "other_pins": sorted({p for v in others for p in (v.pincodes or [])}),
                "other_prefixes": sorted({p for v in others for p in (v.pincode_prefixes or [])}),
                "before_pins": sorted({p for v in before for p in (v.pincodes or [])}),
                "before_prefixes": sorted({p for v in before for p in (v.pincode_prefixes or [])}),
            }
    return flt


@router.get("/filter-options")
def spin_leads_filter_options(_admin: AdminUser):
    """Budget values (with counts) for the filter dropdown, and which filters this data supports."""
    return response_cache.get(("spin", "filter-options"), spin_filter_options, fresh=600, stale=3600)


@router.get("")
def spin_leads_endpoint(
    _admin: AdminUser,
    db: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None, description="Case-insensitive match across visible text fields"),
    sort: str | None = Query(None, description="Column name or additional_details.<key>"),
    order: Literal["asc", "desc"] = Query("asc"),
    branch: str | None = Query(None, description="Only leads from this branch id"),
    pincode: str | None = Query(None, max_length=10, description="Pincode starts with"),
    budget: str | None = Query(None, max_length=50, description="Exact budget value, e.g. 10-20"),
    vendor_id: int | None = Query(None, description="Assigned to this vendor (CRM, branch or area rules)"),
    interest: Literal["interested", "not_interested", "other", "follow_up", "none"] | None = None,
    converted: Literal["yes", "no"] | None = None,
    quoted_op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "set", "unset"] | None = None,
    quoted_value: int | None = Query(None, ge=0),
):
    filters = _crm_filters(db, pincode, budget, vendor_id, interest, converted, quoted_op, quoted_value)
    if filters:
        # Filters that use the CRM's own data change whenever someone edits a lead: no cache
        data = list_spin_leads(None, page, page_size, search, sort, order, branch, filters)
    else:
        data = response_cache.get(
            _cache_key(page, page_size, search, sort, order, branch),
            lambda: list_spin_leads(None, page, page_size, search, sort, order, branch),
            fresh=FRESH_SECONDS,
            stale=STALE_SECONDS,
        )

    # Sidecar: the workflow database is read-only, so the CRM details (vendor, interest,
    # quote, converted) come from our own crm_lead_meta table, matched by the lead's id.
    from app.services import lead_crm
    from app.services.branches import branches_by_id

    pk = next((c.name for c in _get_table().primary_key.columns), "id")
    pin_key, city_key = _spin_location_keys(data["columns"])
    branch_key = data.get("branch_column")
    branches = branches_by_id(db)

    def branch_of(item):
        return branches.get(str(item.get(branch_key))) if branch_key and item.get(branch_key) is not None else None

    items = lead_crm.attach(
        db,
        "spin",
        data["items"],
        key=lambda i: i.get(pk),
        location=lambda i: (i.get(city_key) if city_key else None) or ((branch_of(i) or {}).get("city")),
        pincode=lambda i: str(i.get(pin_key) or "") if pin_key else None,
        branch_vendor=lambda i: (branch_of(i) or {}).get("vendor_id"),
    )
    # The lead's branch (store), from the Branches page
    for item in items:
        b = branch_of(item)
        item["branch"] = (
            {"id": b["id"], "name": b["name"], "code": b["code"], "city": b["city"]}
            if b
            else ({"id": item.get(branch_key), "name": f"Branch {item.get(branch_key)}", "code": None, "city": None} if branch_key and item.get(branch_key) is not None else None)
        )
    return {**data, "items": items}


def warm_first_pages() -> None:
    """Load the pages the dashboard and the Spin leads tab open with, before anyone asks."""
    if read_engine is None:
        return
    for args in ((1, 1, None, None, "asc"), (1, 10, None, None, "asc"), (1, 10, None, "created_at", "desc")):
        response_cache.warm(_cache_key(*args), lambda a=args: list_spin_leads(None, *a))