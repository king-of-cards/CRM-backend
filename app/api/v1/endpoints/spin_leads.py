"""GET /spin-leads: read-only listing of the PostgreSQL table spin_lead, plus the CRM's own spin leads.

Spin (see `spin_filter`): the listed rows (REQUIRED_JSON_KEYS) created on or after the
SPIN_LIVE_FROM setting (an India date; created_at is naive UTC). Older workflow rows are hidden
from spin everywhere (lists, stats, exports, dashboards, reminders, also_in, vendor visibility,
the upload duplicate check), so the history can be uploaded as CRM spin leads (crm_spin_leads),
which are always listed. Gift leads (otherWeddingServices names "Wedding Gifts & Return Gifts",
see `is_gift`) are ordinary spin leads; only their automatic vendor differs (lead_crm.lead_rules).

A second view, koc_to_dhruva (the Dhruva section "KOC -> Dhruva"), is a rule over the workflow rows
(listed or not) created on or after the same cut-off, plus its own uploaded rows
(crm_koc_dhruva_leads, every one listed); never CRM spin leads: see `koc_filter` / `koc_reason`.

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
import re
import threading
import time as _time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import (
    Boolean, LargeBinary, MetaData, Numeric, String, Table, Text, literal_column,
    ColumnElement, and_, bindparam, case, cast, false, func, null, or_, select, true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

from app.api.deps import DbSession, LeadViewer, PageSize, common_filter_params
from app.core import response_cache
from app.core.config import settings
from app.db.session import read_engine
from app.services import access, lead_filters

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

# Views of the spin_lead table (see the module docstring)
SPIN = "spin"
GIFT_KEY = "otherWeddingServices"           # also the list's "Other wedding services"
GIFT_PHRASE = "Wedding Gifts & Return Gifts"
IST_OFFSET = timedelta(hours=5, minutes=30)

# KOC -> Dhruva: estimatedBudgetPerCard's starting number >= 70, or else quantityRequired's >= 1000 (rule_number)
KOC_TO_DHRUVA = "koc_to_dhruva"
KOC_BUDGET_KEY, KOC_QUANTITY_KEY = "estimatedBudgetPerCard", "quantityRequired"
KOC_BUDGET_MIN, KOC_QUANTITY_MIN = 70, 1000
KOC_REASON_LABELS = {"budget": "Budget ≥ ₹70", "quantity": "Quantity ≥ 1000"}
NUMBER_PATTERN = r"([0-9]+(?:\.[0-9]+)?)"          # the first number anywhere ("UNDER_50" -> 50)
# "UNDER_70", "below 70", "less than 70", "<70": never qualify for the KOC -> Dhruva rule (any case)
BELOW_PATTERN = r"^\s*(?:under|below|less\s*than|<)"
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


def is_gift(value: Any) -> bool:
    """True when an otherWeddingServices value names "Wedding Gifts & Return Gifts"
    (case-insensitive, anywhere in it). The Python twin of `gift_expr`."""
    return GIFT_PHRASE.casefold() in str(value or "").casefold()


def is_gift_details(details: Any) -> bool:
    """A gift lead, from its additional_details (CRM leads and fake rows)."""
    return isinstance(details, dict) and is_gift(details.get(GIFT_KEY))


def gift_expr(table: Table):
    """lower(additional_details ->> 'otherWeddingServices') LIKE '%wedding gifts & return gifts%'
    (NULL when the key is missing)."""
    value = _jsonb(table, REQUIRED_JSON_COLUMN).op("->>", return_type=Text)(bindparam(None, GIFT_KEY, type_=Text))
    pattern = bindparam(None, _like_pattern(GIFT_PHRASE.lower()), type_=Text)
    return func.lower(value).like(pattern, escape=LIKE_ESCAPE)


def split_services(value: Any) -> list[str]:
    """The individual services of an otherWeddingServices value ("A, B" -> ["A", "B"])."""
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


# ── Spin cut-off (SPIN_LIVE_FROM) ──────────────────────────────────────
def spin_live_from_utc() -> datetime:
    """The SPIN_LIVE_FROM India date as the naive UTC instant it starts at (00:00 IST)."""
    return datetime.combine(settings.SPIN_LIVE_FROM, time.min) - IST_OFFSET


def to_naive_utc(value: Any) -> datetime | None:
    """A created_at value (naive UTC, aware, or ISO text) as naive UTC, or None."""
    if isinstance(value, str) and value:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def is_live(created: Any) -> bool:
    """A workflow row created on or after the cut-off (the Python twin of `live_filter`).
    No created date: not live."""
    value = to_naive_utc(created)
    return value is not None and value >= spin_live_from_utc()


def _created_column(table: Table):
    return next((table.c[c] for c in ("created_at", "createdAt", "created") if c in table.c), None)


def live_filter(table: Table) -> list[ColumnElement]:
    """created_at >= the cut-off (naive UTC, as the workflow stores it). Fails closed."""
    created = _created_column(table)
    return [created >= bindparam(None, spin_live_from_utc())] if created is not None else [false()]


def spin_filter(table: Table) -> list[ColumnElement]:
    """The workflow rows in spin: listed (REQUIRED_JSON_KEYS) and created on or after the cut-off."""
    return _required_filter(table) + live_filter(table)


def first_number(value: Any) -> float | None:
    """The first number in a value ("70-100" -> 70, "500_PLUS" -> 500, "1,000" -> 1000), or None.
    The Python twin of `_number_expr`."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    m = re.search(NUMBER_PATTERN, str(value).replace(",", ""))
    return float(m.group(1)) if m else None


def rule_number(value: Any) -> float | None:
    """The KOC -> Dhruva rule's number: the starting (first) number of the value ("70-100" -> 70,
    "40-70" -> 40, "1000-2000" -> 1000, "500 to 1000" -> 500, "2000+" -> 2000, "1,000" -> 1000);
    None for "UNDER_…" / "below …" / "less than …" / "<…" values (never qualify) and values without a
    number. The Python twin of `_rule_number_expr`."""
    if isinstance(value, str) and re.match(BELOW_PATTERN, value, re.IGNORECASE):
        return None
    return first_number(value)


def koc_reason(details: Any) -> str | None:
    """"budget" (estimatedBudgetPerCard's rule_number >= 70, wins when both match), "quantity"
    (quantityRequired's rule_number >= 1000), or None (not a KOC -> Dhruva lead)."""
    if not isinstance(details, dict):
        return None
    budget = rule_number(details.get(KOC_BUDGET_KEY))
    if budget is not None and budget >= KOC_BUDGET_MIN:
        return "budget"
    quantity = rule_number(details.get(KOC_QUANTITY_KEY))
    if quantity is not None and quantity >= KOC_QUANTITY_MIN:
        return "quantity"
    return None


def _detail_text(table: Table, key: str):
    return _jsonb(table, REQUIRED_JSON_COLUMN).op("->>", return_type=Text)(bindparam(None, key, type_=Text))


def _number_expr(table: Table, key: str):
    """CAST(substring(replace(details ->> key, ',', '') FROM NUMBER_PATTERN) AS NUMERIC):
    the first number of a JSON value (NULL when it has none)."""
    text_value = func.replace(_detail_text(table, key), ",", "")
    return cast(func.substring(text_value, bindparam(None, NUMBER_PATTERN, type_=Text)), Numeric)


def _rule_number_expr(table: Table, key: str):
    """CASE WHEN details ->> key ~* BELOW_PATTERN THEN NULL ELSE the first number END (PostgreSQL):
    the rule's number. The SQL twin of `rule_number`."""
    below = _detail_text(table, key).op("~*", return_type=Boolean)(bindparam(None, BELOW_PATTERN, type_=Text))
    return case((below, null()), else_=_number_expr(table, key))


def koc_budget_hit(table: Table):
    return func.coalesce(_rule_number_expr(table, KOC_BUDGET_KEY) >= KOC_BUDGET_MIN, false())


def koc_quantity_hit(table: Table):
    return func.coalesce(_rule_number_expr(table, KOC_QUANTITY_KEY) >= KOC_QUANTITY_MIN, false())


def koc_filter(table: Table) -> list[ColumnElement]:
    """KOC -> Dhruva rows: every row of the table (no required keys) matching the budget or the
    quantity rule."""
    return [or_(koc_budget_hit(table), koc_quantity_hit(table))]


def base_filter(table: Table, flow: str = SPIN) -> list[ColumnElement]:
    """What every listed query of a view applies: spin = `spin_filter`; koc_to_dhruva: its own rule
    over every row (listed or not) created on or after the cut-off (SPIN_LIVE_FROM)."""
    if flow == KOC_TO_DHRUVA:
        return koc_filter(table) + live_filter(table)
    if flow != SPIN:
        raise ValueError(f"unknown flow {flow!r}")
    return spin_filter(table)


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
EVENT_FIELD_NAMES = ("occasiondate", "eventdate", "weddingdate")
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


def spin_filter_options(ids: set[str] | None = None, flow: str = SPIN) -> dict:
    """Values for the filter dropdowns: the budget ranges and pincodes used on the flow's leads, with
    counts. ids: only count these workflow lead ids (a vendor's own leads); None = all."""
    if read_engine is None:
        return {"budgets": [], "pincodes": [], "has_branch": False, "has_pincode": False, "has_budget": False,
                "workflow_branches": {}, "workflow_total": 0, "workflow_other_services": {}}
    try:
        table = _get_table()
        json_fields = _get_json_fields(table, _required_filter(table))
        base = base_filter(table, flow)
        if ids is not None:
            pk_text = cast(list(table.primary_key.columns)[0], Text)
            base = base + [pk_text.in_(sorted(ids)) if ids else false()]
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
        branch_counts: dict[str, int] = {}
        if branch_expr is not None:
            query = select(branch_expr.label("b"), func.count()).select_from(table).where(*base).group_by(literal_column("1"))
            with read_engine.connect() as conn:
                branch_counts = {str(b): n for b, n in conn.execute(query).all() if b not in (None, "")}
        with read_engine.connect() as conn:
            workflow_total = conn.execute(select(func.count()).select_from(table).where(*base)).scalar_one()
        services: dict[str, int] = {}
        if flow == SPIN:
            # GROUP BY 1: same reason as above; each value is split into its services
            query = (select(_detail_text(table, GIFT_KEY).label("s"), func.count()).select_from(table).where(*base)
                     .group_by(literal_column("1")))
            with read_engine.connect() as conn:
                for value, n in conn.execute(query).all():
                    for service in split_services(value):
                        services[service] = services.get(service, 0) + n
        return {
            "budgets": budgets,
            "pincodes": pincodes,
            "has_branch": branch_expr is not None,
            "has_pincode": pin_expr is not None,
            "has_budget": budget_expr is not None,
            "workflow_branches": branch_counts,           # branch id -> workflow leads (merged with CRM leads below)
            "workflow_total": workflow_total,
            "workflow_other_services": services,          # service -> workflow leads (merged with CRM leads below)
        }
    except Exception:  # noqa: BLE001
        logger.warning("spin-leads: filter options unavailable", exc_info=True)
        return {"budgets": [], "pincodes": [], "has_branch": False, "has_pincode": False, "has_budget": False,
                "workflow_branches": {}, "workflow_total": 0, "workflow_other_services": {}}


def spin_branch_counts(flow: str = SPIN) -> dict[str, int]:
    """{branch id (text): number of listed leads of the flow}: workflow leads plus the CRM's own
    (archived ones left out). Workflow part empty if the branch isn't stored there."""
    counts = _workflow_branch_counts(flow)
    try:
        from sqlmodel import Session

        from app.db.session import engine
        from app.services import crm_spin_leads

        with Session(engine) as db:
            for lead in crm_spin_leads.active(db, flow):
                if lead.branch_id is not None:
                    counts[str(lead.branch_id)] = counts.get(str(lead.branch_id), 0) + 1
    except Exception:  # noqa: BLE001
        logger.warning("spin-leads: CRM branch counts unavailable", exc_info=True)
    return counts


def _workflow_branch_counts(flow: str = SPIN) -> dict[str, int]:
    if read_engine is None:
        return {}
    try:
        table = _get_table()
        base = base_filter(table, flow)
        name, expr = _branch_field(table, _get_json_fields(table, _required_filter(table)))
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
    offset: int | None = None,
    limit: int | None = None,
    keys_only: bool = False,
    flow: str = SPIN,
):
    """offset/limit override page/page_size (used to page after the CRM's own spin leads).
    flow: spin or koc_to_dhruva (which rows of spin_lead; see the module docstring).
    keys_only=True: {"keys": [every matching lead id as text], "total"}, no rows (stats).

    filters (all optional):
      pincode        leads whose pincode starts with this
      budget         exact budget value (e.g. "10-20")
      other_services otherWeddingServices contains this (any case)
      ids_in         only these lead ids (from the CRM filters); [] = none
      ids_not_in     never these lead ids
      vendor         {manual_for, manual_other, branch_ids, pins, prefixes, fallback,
                      other_pins, other_prefixes}: leads whose effective vendor is one vendor
    """
    if read_engine is None:
        raise HTTPException(503, "Read database is not configured")

    try:
        table = _get_table()
        json_fields = _get_json_fields(table, _required_filter(table))   # one key list for every view
        base = base_filter(table, flow)         # applied to count and rows
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
    if flt.get("created_from") is not None:
        # Arrival date in a period: created_at is stored in UTC without a time zone
        created_col = next((table.c[c] for c in ("created_at", "createdAt", "created") if c in table.c), None)
        if created_col is None:
            conditions.append(false())
        else:
            conditions += [created_col >= flt["created_from"], created_col < flt["created_to"]]
    if flt.get("event_from") is not None or flt.get("event_to") is not None:
        # Event date: the occasion date (YYYY-MM-DD text in the JSON); other texts never match
        _, event_expr = _named_field(table, json_fields, EVENT_FIELD_NAMES)
        if event_expr is None:
            conditions.append(false())
        else:
            day = func.substr(event_expr, 1, 10)
            conditions.append(event_expr.op("~", is_comparison=True)(bindparam(None, r"^\d{4}-\d{2}-\d{2}", type_=Text)))
            if flt.get("event_from") is not None:
                conditions.append(day >= bindparam(None, flt["event_from"], type_=Text))
            if flt.get("event_to") is not None:
                conditions.append(day <= bindparam(None, flt["event_to"], type_=Text))
    if flt.get("no_workflow"):
        conditions.append(false())                     # origin asked for has no workflow leads
    if flt.get("other_services"):                       # Other wedding services contains (any case)
        pattern = bindparam("services_pattern", _like_pattern(flt["other_services"]), type_=Text)
        conditions.append(_detail_text(table, GIFT_KEY).ilike(pattern, escape=LIKE_ESCAPE))
    if flt.get("occasion"):                             # occasion type contains (any case)
        pattern = bindparam("occasion_pattern", _like_pattern(flt["occasion"]), type_=Text)
        conditions.append(_detail_text(table, "occasionType").ilike(pattern, escape=LIKE_ESCAPE))
    if flt.get("quantity_min") is not None:
        conditions.append(_number_expr(table, KOC_QUANTITY_KEY) >= flt["quantity_min"])
    if flt.get("quantity_max") is not None:
        conditions.append(_number_expr(table, KOC_QUANTITY_KEY) <= flt["quantity_max"])
    if flt.get("reason") == "budget":
        conditions.append(koc_budget_hit(table))
    elif flt.get("reason") == "quantity":
        conditions += [koc_budget_hit(table) == false(), koc_quantity_hit(table)]

    if keys_only:
        try:
            with read_engine.connect() as conn:
                keys = [str(k) for k in conn.execute(select(pk_text).where(*conditions)).scalars()]
        except SQLAlchemyError as exc:
            raise _db_error(exc, "query")
        return {"keys": keys, "total": len(keys)}

    query = (
        select(*(e.label(f"f{i}") for i, e in enumerate(select_exprs)))
        .where(*conditions)
        .order_by(*order_by)
        .limit(page_size if limit is None else limit)
        .offset((page - 1) * page_size if offset is None else offset)
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


def _cache_key(offset, limit, search, sort, order, branch=None, flow=SPIN):
    return ("spin", offset, limit, (search or "").strip().lower(), sort, order, branch or None) + \
        (() if flow == SPIN else (flow,))


def _spin_location_keys(columns: list[dict]) -> tuple[str | None, str | None]:
    """Names of the pincode and city/location fields in this table's output, if any."""
    names = [c["name"] for c in columns]
    lower = {n: n.lower().split(".")[-1] for n in names}
    pin = next((n for n in names if "pincode" in lower[n] or lower[n] == "pin"), None)
    city = next((n for n in names if "city" in lower[n] or "location" in lower[n]), None)
    return pin, city


@dataclass
class SpinFilters:
    common: lead_filters.CommonFilters
    branch: str | None = None
    pincode: str | None = None
    budget: str | None = None
    other_services: str | None = None


def spin_filters(
    common: Annotated[lead_filters.CommonFilters,
                      Depends(common_filter_params(lead_filters.SPIN_ORIGINS, "Spin leads"))],
    branch: str | None = Query(None, description="Only leads from this branch id"),
    pincode: str | None = Query(None, max_length=10, description="Pincode starts with"),
    budget: str | None = Query(None, max_length=50, description="Exact budget value, e.g. 10-20"),
    other_services: str | None = Query(None, max_length=200,
                                       description="Other wedding services contains this (any case), e.g. "
                                                   "Wedding Gifts & Return Gifts"),
) -> SpinFilters:
    pin = "".join(ch for ch in (pincode or "") if ch.isdigit()) or (pincode or "").strip() or None
    return SpinFilters(common, branch or None, pin, budget or None, (other_services or "").strip() or None)


Filters = Annotated[SpinFilters, Depends(spin_filters)]


def filters_for(db, viewer, f: SpinFilters, flow: str = SPIN) -> dict:
    """What list_spin_leads (PostgreSQL) and crm_spin_leads.matching (CRM leads) apply. CRM filters
    and visibility become lead ids, from the same assignment map as vendor visibility. Shared by
    the list and the export."""
    c = f.common
    flt: dict = {}
    if f.pincode:
        flt["pincode"] = f.pincode
    if f.budget:
        flt["budget"] = f.budget
    if getattr(f, "other_services", None):
        flt["other_services"] = f.other_services
    keys = lead_filters.allowed_keys(db, flow, viewer, c)
    if keys is not None:
        flt["ids_in"] = sorted(keys)
    if c.period is not None:
        # Arrived in the period (IST days): created_at is stored in UTC without a time zone
        start, end = c.period.utc_bounds()
        flt["created_from"], flt["created_to"] = start.replace(tzinfo=None), end.replace(tzinfo=None)
    if c.event_from is not None:
        flt["event_from"] = c.event_from.isoformat()
    if c.event_to is not None:
        flt["event_to"] = c.event_to.isoformat()
    if c.origins:
        flt["origins"] = list(c.origins)
        flt["no_workflow"] = "workflow" not in c.origins
    return flt


def filter_options(viewer, db, flow: str = SPIN) -> dict:
    """Values for the filter dropdowns, with counts over the flow's leads the viewer may see (vendor
    users: their own): budgets and pincodes (workflow leads), branches, origins, update counts, and
    vendors (admins only). Also which filters this data supports (has_*)."""
    from app.services import crm_spin_leads
    from app.services.branches import branches_by_id
    from app.services.stores import store_for

    visible = access.visible_keys(db, viewer, flow)
    if visible is None:                       # every lead of the flow (admins; Dhruva logins on koc_to_dhruva)
        base = response_cache.get(("spin", "filter-options") + (() if flow == SPIN else (flow,)),
                                  lambda: spin_filter_options(None, flow), fresh=600, stale=3600)
    else:
        base = spin_filter_options({k for k in visible if not k.startswith("crm-")}, flow)
    crm = [lead for lead in crm_spin_leads.active(db, flow) if visible is None or lead.lead_key in visible]

    branch_counts = dict(base.get("workflow_branches", {}))
    for lead in crm:
        if lead.branch_id is not None:
            branch_counts[str(lead.branch_id)] = branch_counts.get(str(lead.branch_id), 0) + 1
    known = list(branches_by_id(db).values())
    branches = []
    for branch_id, n in branch_counts.items():
        store = store_for(branch_id, known)
        branches.append({"id": branch_id, "name": store["name"] if store else None, "count": n})
    branches.sort(key=lambda b: (-b["count"], str(b["name"] or b["id"]).lower()))

    origins = [{"value": "workflow", "count": base.get("workflow_total", 0)}] + [
        {"value": o, "count": sum(1 for lead in crm if lead.origin == o)} for o in ("added", "imported")
    ]
    keys = visible if visible is not None else set(access.assignments(db, flow))
    out = {k: v for k, v in base.items() if not k.startswith("workflow_")}
    if flow == SPIN:                          # Other wedding services: each service with its lead count
        services = dict(base.get("workflow_other_services", {}))
        for lead in crm:
            for service in split_services((lead.additional_details or {}).get(GIFT_KEY)):
                services[service] = services.get(service, 0) + 1
        out["other_services"] = [{"value": s, "count": n}
                                 for s, n in sorted(services.items(), key=lambda kv: (-kv[1], kv[0].casefold()))]
    return {
        **out,
        "branches": branches,
        "origins": origins,
        "updates": lead_filters.update_options(db, flow, keys, viewer),
        "vendors": lead_filters.vendor_options(db, viewer),
    }


def stats(viewer, db, f: SpinFilters, flow: str = SPIN) -> dict:
    """{total, kpis} over exactly the leads the list shows for the same filters (workflow and
    CRM leads; vendor users: their own). total equals the list's total."""
    from app.services import crm_spin_leads, kpis

    filters = filters_for(db, viewer, f, flow)
    added = crm_spin_leads.matching(db, f.common.search, None, "asc", f.branch, filters, flow)
    workflow = list_spin_leads(None, 1, 1, f.common.search, None, "asc", f.branch, filters or None, keys_only=True,
                               flow=flow)
    keys = [lead.lead_key for lead in added] + list(workflow["keys"])
    from app.services import lead_crm

    return {"total": len(keys), "kpis": kpis.for_keys(db, flow, keys, lead_crm.viewer_copy(viewer))}


def list_page(viewer, db, f: SpinFilters, page: int, page_size: int, sort, order, flow: str = SPIN) -> dict:
    """Filters: app/services/lead_filters.py (common) + branch, pincode, budget. Vendor users only
    ever get their own leads (vendor_id / unassigned are ignored for them)."""
    from app.services import also_in, phones

    data = merged_page(db, (page - 1) * page_size, page_size, f.common.search, sort, order, f.branch,
                       filters_for(db, viewer, f, flow), flow=flow)
    pk = next((c.name for c in _get_table().primary_key.columns), "id")
    keys = [str(i.get(pk)) for i in data["items"]]
    e164_of = also_in.page_phones(db, flow, [k for k in keys if k.startswith("crm-")])   # CRM leads
    for item, key in zip(data["items"], keys):                                          # workflow leads
        if key not in e164_of and item.get("phone"):
            e164 = phones.normalize(str(item["phone"]), lenient=True).e164
            if e164:
                e164_of[key] = e164
    also_in.attach(db, viewer, flow, data["items"], key_of=lambda i: i.get(pk), e164_of=e164_of)
    from app.services import lead_crm

    data["items"] = [lead_crm.for_viewer(i, viewer) for i in data["items"]]     # vendor logins: their own copy
    return {**data, "page": page, "page_size": page_size}


@router.get("/filter-options")
def spin_leads_filter_options(viewer: LeadViewer, db: DbSession):
    """Spin leads: see `filter_options`."""
    return filter_options(viewer, db, SPIN)


@router.get("/stats")
def spin_lead_stats(viewer: LeadViewer, db: DbSession, f: Filters):
    """Spin leads: see `stats`."""
    return stats(viewer, db, f, SPIN)


@router.get("")
def spin_leads_endpoint(
    viewer: LeadViewer,
    db: DbSession,
    f: Filters,
    page: int = Query(1, ge=1),
    page_size: PageSize = 10,
    sort: str | None = Query(None, description="Column name or additional_details.<key>"),
    order: Literal["asc", "desc"] = Query("asc"),
):
    """Spin leads (live workflow rows and every CRM spin lead): see `list_page`."""
    return list_page(viewer, db, f, page, page_size, sort, order, SPIN)


def merged_page(db, start: int, length: int, search, sort, order, branch, filters: dict, use_cache: bool = True,
                flow: str = SPIN) -> dict:
    """Rows [start, start + length) of the combined list of a flow, with CRM details and branch.

    Spin leads added/imported in the CRM (MySQL, archived ones left out) come first, then the
    workflow leads (PostgreSQL); paging runs over both. The CRM ones are read fresh every time.
    An unknown sort name is rejected with 400 by list_spin_leads. Also used by the export."""
    from app.services import crm_spin_leads, lead_crm
    from app.services.branches import branches_by_id

    added = crm_spin_leads.matching(db, search, sort, order, branch, filters, flow)
    added_page = added[start:start + length]
    pg_offset = max(0, start - len(added))
    pg_limit = length - len(added_page)

    if filters or not use_cache:
        # Filters that use the CRM's own data change whenever someone edits a lead: no cache
        data = list_spin_leads(None, 1, length, search, sort, order, branch, filters or None, pg_offset, pg_limit,
                               flow=flow)
    else:
        data = response_cache.get(
            _cache_key(pg_offset, pg_limit, search, sort, order, branch, flow),
            lambda: list_spin_leads(None, 1, length, search, sort, order, branch, None, pg_offset, pg_limit,
                                    flow=flow),
            fresh=FRESH_SECONDS,
            stale=STALE_SECONDS,
        )
    items = [crm_spin_leads.to_item(lead, data["columns"]) for lead in added_page] + [
        {**item, "origin": "workflow"} for item in data["items"]
    ]

    # Sidecar: the workflow database is read-only, so the CRM details (vendor, update,
    # quote, conversion amount) come from our own crm_lead_meta table, matched by the lead's id.
    pk = next((c.name for c in _get_table().primary_key.columns), "id")
    pin_key, city_key = _spin_location_keys(data["columns"])
    branch_key = data.get("branch_column")
    branches = branches_by_id(db)

    def branch_of(item):
        return branches.get(str(item.get(branch_key))) if branch_key and item.get(branch_key) is not None else None

    # Gift leads also go to the gifts partner (lead_crm.PARTNER_RULES): CRM leads from their details,
    # workflow rows from their otherWeddingServices field; received = created_at
    gift_keys = {lead.lead_key for lead in added_page if is_gift_details(lead.additional_details)}
    services_key = next((c["name"] for c in data["columns"] if _norm_name(c["name"]) == _norm_name(GIFT_KEY)), None)

    def gift_of(item) -> bool:
        if str(item.get(pk)) in gift_keys:
            return True
        return item.get("origin") == "workflow" and bool(services_key) and is_gift(item.get(services_key))

    items = lead_crm.attach(
        db,
        flow,
        items,
        key=lambda i: i.get(pk),
        location=lambda i: (i.get(city_key) if city_key else None) or ((branch_of(i) or {}).get("city")),
        pincode=lambda i: str(i.get(pin_key) or "") if pin_key else None,
        branch_vendor=lambda i: (branch_of(i) or {}).get("vendor_id"),
        gift=gift_of if flow == SPIN else None,
        received=lambda i: i.get("created_at"),
    )
    # The lead's branch (store), from the Branches page
    for item in items:
        b = branch_of(item)
        item["branch"] = (
            {"id": b["id"], "name": b["name"], "code": b["code"], "city": b["city"]}
            if b
            else ({"id": item.get(branch_key), "name": f"Branch {item.get(branch_key)}", "code": None, "city": None} if branch_key and item.get(branch_key) is not None else None)
        )
    return {**data, "items": items, "total": data["total"] + len(added)}


def warm_first_pages() -> None:
    """Load the pages the dashboard and the Spin leads tab open with, before anyone asks."""
    if read_engine is None:
        return
    # (offset, limit) as requested when there are no CRM spin leads in front of them
    for args in ((0, 1, None, None, "asc"), (0, 10, None, None, "asc"), (0, 10, None, "created_at", "desc")):
        offset, limit, *rest = args
        response_cache.warm(
            _cache_key(*args),
            lambda o=offset, n=limit, r=rest: list_spin_leads(None, 1, n, *r, None, None, o, n),
        )