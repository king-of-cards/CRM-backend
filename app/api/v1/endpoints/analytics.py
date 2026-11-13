"""GET /analytics/overview: one call with everything the dashboard charts need (admin only).

Combines the four lead sources:
  spin            PostgreSQL spin_lead (read-only workflow database)
  whatsapp        International Google Sheet  (crm_whatsapp_leads)
  wedding         Wedding Google Sheet        (crm_wedding_leads)
  wedding_planner Wedding planner Sheet       (crm_wedding_plans)

The tables are small (thousands of rows), so rows are loaded once and grouped in Python,
which works the same on every database. The result is cached for CACHE_SECONDS.
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

from app.api.deps import AdminUser
from app.core import response_cache
from app.db.session import engine, read_engine
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])

SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
CACHE_KEY = ("analytics", "overview")
FRESH_SECONDS = 60        # answered from memory, no rebuild
STALE_SECONDS = 6 * 3600  # answered from memory, rebuilt in the background
TREND_DAYS = 30
UPCOMING_MONTHS = 12


# ── Helpers ────────────────────────────────────────────────────────────
def _as_date(value: Any) -> date | None:
    """datetime (naive = UTC) -> India calendar date; date stays a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone(timedelta(hours=5, minutes=30))).date()
    if isinstance(value, date):
        return value
    return None


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
def _spin_rows() -> list[dict] | None:
    """[{created, occasion_type, occasion_date}] for every listed spin lead, or None if unavailable."""
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

        exprs = []
        names = []
        if created_col is not None:
            exprs.append(created_col.label("created"))
            names.append("created")
        if type_field is not None:
            exprs.append(sl._json_text_expr(table, type_field).label("occasion_type"))
            names.append("occasion_type")
        if date_field is not None:
            exprs.append(sl._json_text_expr(table, date_field).label("occasion_date"))
            names.append("occasion_date")
        if not exprs:
            return []

        with read_engine.connect() as conn:
            rows = conn.execute(sa_select(*exprs).select_from(table).where(*base)).all()
        return [dict(zip(names, r)) for r in rows]
    except Exception:  # noqa: BLE001
        logger.warning("Spin analytics unavailable", exc_info=True)
        return None


# ── Build the overview ─────────────────────────────────────────────────
def _build(db: Session) -> dict:
    today = _as_date(datetime.now(timezone.utc))
    week_start = today - timedelta(days=6)           # last 7 days, today included
    prev_week_start = week_start - timedelta(days=7)
    trend_start = today - timedelta(days=TREND_DAYS - 1)

    # Load the sheet tables (only the columns needed)
    wa = db.exec(
        select(WhatsAppLead.submitted_at, WhatsAppLead.event_date, WhatsAppLead.country).where(
            WhatsAppLead.is_deleted == False  # noqa: E712
        )
    ).all()
    wd = db.exec(
        select(
            WeddingLead.submitted_at, WeddingLead.event_date, WeddingLead.city, WeddingLead.occasion,
            WeddingLead.budget_amount, WeddingLead.shared_with, WeddingLead.is_repeat,
        ).where(WeddingLead.is_deleted == False)  # noqa: E712
    ).all()
    wp = db.exec(
        select(
            WeddingPlan.updated_in_sheet_at, WeddingPlan.wedding_date, WeddingPlan.city, WeddingPlan.total_budget,
            WeddingPlan.plan_estimate, WeddingPlan.fits_budget, WeddingPlan.total_guests, WeddingPlan.shared_with,
            WeddingPlan.last_step,
        ).where(WeddingPlan.is_deleted == False)  # noqa: E712
    ).all()
    spin = _spin_rows()

    # (created date, event date) per source
    timeline: dict[str, list[tuple[date | None, date | None]]] = {
        "whatsapp": [(_as_date(r[0]), r[1]) for r in wa],
        "wedding": [(_as_date(r[0]), r[1]) for r in wd],
        "wedding_planner": [(_as_date(r[0]), r[1]) for r in wp],
        "spin": [(_as_date(r.get("created")), _parse_iso(r.get("occasion_date"))) for r in (spin or [])],
    }
    totals = {"whatsapp": len(wa), "wedding": len(wd), "wedding_planner": len(wp), "spin": len(spin) if spin is not None else None}

    # Inflow: per day for the trend, plus this week vs the week before
    trend = {trend_start + timedelta(days=i): dict.fromkeys(SOURCES, 0) for i in range(TREND_DAYS)}
    this_week = dict.fromkeys(SOURCES, 0)
    last_week = dict.fromkeys(SOURCES, 0)
    for source, items in timeline.items():
        for created, _ in items:
            if created is None:
                continue
            if created in trend:
                trend[created][source] += 1
            if week_start <= created <= today:
                this_week[source] += 1
            elif prev_week_start <= created < week_start:
                last_week[source] += 1

    # Upcoming events: per month for the next 12 months, and the next 30 days
    months = []
    y, m = today.year, today.month
    for _ in range(UPCOMING_MONTHS):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    upcoming = {k: dict.fromkeys(SOURCES, 0) for k in months}
    next_30 = dict.fromkeys(SOURCES, 0)
    for source, items in timeline.items():
        for _, event in items:
            if event is None or event < today.replace(day=1):
                continue
            key = f"{event.year:04d}-{event.month:02d}"
            if key in upcoming:
                upcoming[key][source] += 1
            if today <= event <= today + timedelta(days=30):
                next_30[source] += 1

    # Places, countries, occasions
    cities = Counter(c for c in (_clean_place(r[2]) for r in wd) if c)
    cities.update(c for c in (_clean_place(r[2]) for r in wp) if c)
    countries = Counter((r[2] or "").strip() for r in wa if r[2])
    occasions = Counter((r[3] or "").strip().title() for r in wd if r[3])
    occasions.update((str(r.get("occasion_type") or "")).strip().title() for r in (spin or []) if r.get("occasion_type"))
    occasions.pop("", None)

    # Partners (Wedding + Wedding planner)
    partners = Counter(r[5] for r in wd if r[5])
    partners.update(r[7] for r in wp if r[7])
    shareable = len(wd) + len(wp)
    not_shared = sum(1 for r in wd if not r[5]) + sum(1 for r in wp if not r[7])

    # Money and size
    photo_budgets = [r[4] for r in wd if r[4]]
    wedding_budgets = [r[3] for r in wp if r[3]]
    guests = [r[6] for r in wp if r[6]]
    fits = Counter("fits" if r[5] is True else "over" if r[5] is False else "unknown" for r in wp)

    def money(values: list[int]) -> dict:
        return {
            "count": len(values),
            "total": sum(values),
            "average": round(statistics.mean(values)) if values else None,
            "median": round(statistics.median(values)) if values else None,
        }

    known_totals = [v for v in totals.values() if v is not None]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "today": today.isoformat(),
        "spin_available": spin is not None,
        "totals": {**totals, "all": sum(known_totals)},
        "this_week": {**this_week, "all": sum(this_week.values())},
        "last_week": {**last_week, "all": sum(last_week.values())},
        "trend": [{"date": d.isoformat(), **counts} for d, counts in trend.items()],
        "upcoming_by_month": [{"month": k, **v} for k, v in upcoming.items()],
        "upcoming_30_days": {**next_30, "all": sum(next_30.values())},
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
        "repeats": sum(1 for r in wd if r[6]),
    }


def build_overview() -> dict:
    with Session(engine) as db:
        return _build(db)


def warm_overview() -> None:
    """Rebuild in the background (called at startup and after every sheet sync)."""
    response_cache.warm(CACHE_KEY, build_overview)


@router.get("/overview")
def analytics_overview(_: AdminUser, refresh: bool = Query(False)):
    if refresh:
        response_cache.clear("analytics")
    return response_cache.get(CACHE_KEY, build_overview, fresh=FRESH_SECONDS, stale=STALE_SECONDS)