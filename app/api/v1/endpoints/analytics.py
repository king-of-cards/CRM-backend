"""GET /analytics/overview: one call with everything the dashboard needs, for a period.
Admins see every lead, vendor users only their own.

Combines the four lead sources:
  spin            PostgreSQL spin_lead (read-only workflow database)
  whatsapp        International Google Sheet  (crm_whatsapp_leads)
  wedding         Wedding Google Sheet        (crm_wedding_leads)
  wedding_planner Wedding planner Sheet       (crm_wedding_plans)

The tables are small (thousands of rows), so rows are loaded once and grouped in Python,
which works the same on every database. Results are cached per viewer and period.
"""
import logging
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.api.deps import DashboardPeriod, DbSession, LeadViewer
from app.core import response_cache
from app.services import access, periods
from app.db.session import engine, read_engine
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])

SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
FRESH_SECONDS = 60        # answered from memory, no rebuild
STALE_SECONDS = 6 * 3600  # answered from memory, rebuilt in the background
UPCOMING_MONTHS = 12


# ── Helpers ────────────────────────────────────────────────────────────
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _parse_iso(text: Any) -> date | None:
    m = _ISO_DATE.match(str(text or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _clean_place(text: str | None) -> str | None:
    """'Shamirpet, Hyderabad' -> 'Hyderabad'; 'SILIGURI' -> 'Siliguri'; pincodes dropped."""
    if not text:
        return None
    part = re.sub(r"\d{5,6}", "", text).split(",")[-1].strip()
    part = re.sub(r"\s+", " ", part)
    return part.title() if part else None


def _top(counter: Counter, n: int = 8) -> list[dict]:
    return [{"value": k, "count": v} for k, v in counter.most_common(n)]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ── Spin leads (PostgreSQL, read-only) ─────────────────────────────────
def _spin_rows(db: Session) -> list[dict] | None:
    """[{key, created, occasion_type, occasion_date}] for every listed spin lead (the workflow's and
    the CRM's own, archived ones left out), or None if the workflow database is unavailable."""
    rows = _workflow_spin_rows()
    if rows is None:
        return None
    from app.services import crm_spin_leads

    for lead in crm_spin_leads.active(db):
        details = lead.additional_details or {}
        rows.append({
            "key": lead.lead_key,
            "created": lead.created_at,
            "occasion_type": details.get("occasionType"),
            "occasion_date": details.get("occasionDate"),
        })
    return rows


def _workflow_spin_rows() -> list[dict] | None:
    if read_engine is None:
        return None
    try:
        # Reuse the spin-leads endpoint's table reflection, base filter and JSON key discovery
        from app.api.v1.endpoints import spin_leads as sl

        table = sl._get_table()
        base = sl._required_filter(table)
        fields = {_norm(f.key): f for fs in sl._get_json_fields(table, base).values() for f in fs}

        created_col = next((table.c[c] for c in ("created_at", "createdAt", "created") if c in table.c), None)
        type_field = next((fields[k] for k in ("occasiontype", "eventtype", "occasion") if k in fields), None)
        date_field = next((fields[k] for k in ("occasiondate", "eventdate", "weddingdate") if k in fields), None)

        pk = list(table.primary_key.columns)[0]
        exprs = [pk.label("key")]
        names = ["key"]
        if created_col is not None:
            exprs.append(created_col.label("created"))
            names.append("created")
        if type_field is not None:
            exprs.append(sl._json_text_expr(table, type_field).label("occasion_type"))
            names.append("occasion_type")
        if date_field is not None:
            exprs.append(sl._json_text_expr(table, date_field).label("occasion_date"))
            names.append("occasion_date")
        if len(exprs) == 1:
            return []

        with read_engine.connect() as conn:
            rows = conn.execute(sa_select(*exprs).select_from(table).where(*base)).all()
        return [{**dict(zip(names, r)), "key": str(r[0])} for r in rows]
    except Exception:  # noqa: BLE001
        logger.warning("Spin analytics unavailable", exc_info=True)
        return None


# ── Build the overview ─────────────────────────────────────────────────
def _counts(rows_by_source: dict[str, list], keep) -> dict[str, int]:
    return {s: sum(1 for arrival in rows if keep(arrival)) for s, rows in rows_by_source.items()}


def _change(new: int, previous: int) -> int | None:
    return round((new - previous) / previous * 100) if previous else None


def _build(db: Session, keys: dict[str, set[str]] | None = None, period: periods.Period | None = None) -> dict:
    """keys: {source: lead keys} to limit every number to (a vendor's own leads); None = all.
    period: the dashboard's date range (default: the last 30 days).

    Arrival date of a lead (India calendar day): spin created_at, International and Wedding
    submitted_at, Wedding planner updated_in_sheet_at (the sheet's "Last updated"); added and
    imported leads use the same columns (filled with the entry time, or the uploaded value)."""
    period = period or periods.default_period()
    previous = period.previous()
    today = periods.today_ist()
    week_start = today - timedelta(days=6)           # this_week / last_week: last 7 days vs the 7 before
    prev_week_start = week_start - timedelta(days=7)

    # Load the sheet tables (only the columns needed); the arrival time is the first column
    wa = db.exec(
        select(WhatsAppLead.submitted_at, WhatsAppLead.event_date, WhatsAppLead.country, WhatsAppLead.sheet_key).where(
            WhatsAppLead.is_deleted == False  # noqa: E712
        )
    ).all()
    wd = db.exec(
        select(
            WeddingLead.submitted_at, WeddingLead.event_date, WeddingLead.city, WeddingLead.occasion,
            WeddingLead.budget_amount, WeddingLead.shared_with, WeddingLead.is_repeat, WeddingLead.sheet_key,
        ).where(WeddingLead.is_deleted == False)  # noqa: E712
    ).all()
    wp = db.exec(
        select(
            WeddingPlan.updated_in_sheet_at, WeddingPlan.wedding_date, WeddingPlan.city, WeddingPlan.total_budget,
            WeddingPlan.plan_estimate, WeddingPlan.fits_budget, WeddingPlan.total_guests, WeddingPlan.shared_with,
            WeddingPlan.last_step, WeddingPlan.plan_id,
        ).where(WeddingPlan.is_deleted == False)  # noqa: E712
    ).all()
    spin = _spin_rows(db)
    if keys is not None:
        wa = [r for r in wa if r[-1] in keys["whatsapp"]]
        wd = [r for r in wd if r[-1] in keys["wedding"]]
        wp = [r for r in wp if r[-1] in keys["wedding_planner"]]
        spin = None if spin is None else [r for r in spin if str(r.get("key")) in keys["spin"]]

    arrival = {
        "whatsapp": [periods.ist_date(r[0]) for r in wa],
        "wedding": [periods.ist_date(r[0]) for r in wd],
        "wedding_planner": [periods.ist_date(r[0]) for r in wp],
        "spin": [periods.ist_date(r.get("created")) for r in (spin or [])],
    }
    events = {
        "whatsapp": [r[1] for r in wa],
        "wedding": [r[1] for r in wd],
        "wedding_planner": [r[1] for r in wp],
        "spin": [_parse_iso(r.get("occasion_date")) for r in (spin or [])],
    }
    in_period = period.contains

    # Totals: every lead up to the end of the period (leads without an arrival date count too)
    totals = _counts(arrival, lambda d: d is None or d <= period.date_to)
    if spin is None:
        totals["spin"] = None
    new = _counts(arrival, in_period)
    before = _counts(arrival, previous.contains)
    this_week = _counts(arrival, lambda d: d is not None and week_start <= d <= today)
    last_week = _counts(arrival, lambda d: d is not None and prev_week_start <= d < week_start)

    # Inflow: one point per day, or per 7 days for long periods
    buckets = period.buckets()
    trend = [{"date": start.isoformat(), "end": end.isoformat(), **dict.fromkeys(SOURCES, 0)} for start, end in buckets]
    first = {start: i for i, (start, _) in enumerate(buckets)}
    step = 7 if period.granularity == "week" else 1
    for source, days in arrival.items():
        for d in days:
            if in_period(d):
                start = period.date_from + timedelta(days=((d - period.date_from).days // step) * step)
                trend[first[start]][source] += 1

    # Upcoming events (NOT filtered by the period): from today, across all of the viewer's leads
    months = []
    y, m = today.year, today.month
    for _ in range(UPCOMING_MONTHS):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    upcoming = {k: dict.fromkeys(SOURCES, 0) for k in months}
    next_30 = dict.fromkeys(SOURCES, 0)
    for source, items in events.items():
        for event in items:
            if event is None or event < today.replace(day=1):
                continue
            key = f"{event.year:04d}-{event.month:02d}"
            if key in upcoming:
                upcoming[key][source] += 1
            if today <= event <= today + timedelta(days=30):
                next_30[source] += 1

    # Everything below: leads that arrived in the period
    wa_p = [r for r, d in zip(wa, arrival["whatsapp"]) if in_period(d)]
    wd_p = [r for r, d in zip(wd, arrival["wedding"]) if in_period(d)]
    wp_p = [r for r, d in zip(wp, arrival["wedding_planner"]) if in_period(d)]
    spin_p = [r for r, d in zip(spin or [], arrival["spin"]) if in_period(d)]

    cities = Counter(c for c in (_clean_place(r[2]) for r in wd_p) if c)
    cities.update(c for c in (_clean_place(r[2]) for r in wp_p) if c)
    countries = Counter((r[2] or "").strip() for r in wa_p if r[2])
    occasions = Counter((r[3] or "").strip().title() for r in wd_p if r[3])
    occasions.update((str(r.get("occasion_type") or "")).strip().title() for r in spin_p if r.get("occasion_type"))
    occasions.pop("", None)

    partners = Counter(r[5] for r in wd_p if r[5])
    partners.update(r[7] for r in wp_p if r[7])
    shareable = len(wd_p) + len(wp_p)
    not_shared = sum(1 for r in wd_p if not r[5]) + sum(1 for r in wp_p if not r[7])

    photo_budgets = [r[4] for r in wd_p if r[4]]
    wedding_budgets = [r[3] for r in wp_p if r[3]]
    guests = [r[6] for r in wp_p if r[6]]
    fits = Counter("fits" if r[5] is True else "over" if r[5] is False else "unknown" for r in wp_p)

    # Needs attention (NOT filtered by the period): everything still open
    open_not_shared = sum(1 for r in wd if not r[5]) + sum(1 for r in wp if not r[7])
    open_over_budget = sum(1 for r in wp if r[5] is False)

    def money(values: list[int]) -> dict:
        return {
            "count": len(values),
            "total": sum(values),
            "average": round(statistics.mean(values)) if values else None,
            "median": round(statistics.median(values)) if values else None,
        }

    known_totals = [v for v in totals.values() if v is not None]
    new_all = sum(new.values())
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "today": today.isoformat(),
        "period": period.as_dict(),
        "not_filtered": ["upcoming_30_days", "upcoming_by_month", "needs_attention"],
        "spin_available": spin is not None,
        "totals": {**totals, "all": sum(known_totals)},
        "new_in_period": {**new, "all": new_all},
        "previous_period": {**before, "all": sum(before.values())},
        "change_pct": {**{s: _change(new[s], before[s]) for s in SOURCES}, "all": _change(new_all, sum(before.values()))},
        "source_mix": [
            {"source": s, "count": new[s], "pct": round(new[s] / new_all * 100) if new_all else 0} for s in SOURCES
        ],
        # Kept with their original meaning (last 7 days vs the 7 before) until the screen uses the fields above
        "this_week": {**this_week, "all": sum(this_week.values())},
        "last_week": {**last_week, "all": sum(last_week.values())},
        "trend": trend,
        "upcoming_by_month": [{"month": k, **v} for k, v in upcoming.items()],
        "upcoming_30_days": {**next_30, "all": sum(next_30.values())},
        "needs_attention": {
            "not_shared": open_not_shared,
            "over_budget": open_over_budget,
            "total": open_not_shared + open_over_budget,
        },
        "top_cities": _top(cities),
        "top_countries": _top(countries),
        "occasions": _top(occasions, 6),
        "partners": _top(partners, 8),
        "sharing": {
            "shareable": shareable,
            "shared": shareable - not_shared,
            "not_shared": not_shared,
            "rate": round((shareable - not_shared) / shareable * 100) if shareable else None,
        },
        "photography_budget": money(photo_budgets),
        "wedding_budget": money(wedding_budgets),
        "guests": {"average": round(statistics.mean(guests)) if guests else None, "total": sum(guests)},
        "budget_fit": dict(fits),
        "repeats": sum(1 for r in wd_p if r[6]),
    }


def _cache_key(scope, period: periods.Period) -> tuple:
    """Per viewer scope (admin, or the vendor: its logins see the same leads) and per period."""
    return ("analytics", "overview", scope, period.date_from.isoformat(), period.date_to.isoformat())


def _builder(bind, keys, period):
    """A build that opens its own session, so a background refresh never uses a closed one."""
    def build():
        with Session(bind) as session:
            return _build(session, keys, period)
    return build


def build_overview() -> dict:
    period = periods.default_period()
    return _builder(engine, None, period)()


def warm_overview() -> None:
    """Rebuild the admin's default period in the background (at startup and after every sheet sync)."""
    period = periods.default_period()
    response_cache.warm(_cache_key("admin", period), _builder(engine, None, period))


@router.get("/overview")
def analytics_overview(viewer: LeadViewer, db: DbSession, period: DashboardPeriod, refresh: bool = Query(False)):
    """Admins: every lead. Vendor users: only their own leads. date_from / date_to: the period
    (IST days, inclusive; default the last 30 days); see `_build` for what each number covers."""
    if refresh:
        response_cache.clear("analytics")
    bind = db.get_bind()
    if viewer.is_admin:
        return response_cache.get(_cache_key("admin", period), _builder(bind, None, period),
                                  fresh=FRESH_SECONDS, stale=STALE_SECONDS)
    keys = {source: access.visible_keys(db, viewer, source) for source in SOURCES}
    return response_cache.get(_cache_key(("vendor", viewer.vendor_id), period), _builder(bind, keys, period),
                              fresh=FRESH_SECONDS, stale=FRESH_SECONDS)
