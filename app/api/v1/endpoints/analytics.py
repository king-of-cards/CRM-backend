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

from app.api.deps import AllTimeDefaultPeriod, DashboardPeriod, DbSession, DhruvaSectionUser, LeadViewer
from app.core import response_cache
from app.services import access, kpis, periods
from app.db.session import engine, read_engine
from app.models.lead_meta import LeadMeta
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.models.vendor_to_koc import VendorToKocLead

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
    part = re.sub(r"[\s·]+$", "", part).strip()
    part = re.sub(r"\s+", " ", part)
    return part.title() if part else None


_CITY_ALIASES = {
    "bangalore": "Bengaluru", "bengaluru": "Bengaluru", "banglore": "Bengaluru",
    "@bengaluru": "Bengaluru",
    "hyderabad": "Hyderabad", "hydrabad": "Hyderabad", "hyderbad": "Hyderabad",
    "gurgaon": "Gurugram", "gurugram": "Gurugram", "bombay": "Mumbai", "mumbai": "Mumbai",
    "madras": "Chennai", "chennai": "Chennai", "calcutta": "Kolkata", "kolkata": "Kolkata",
    "mysore": "Mysuru", "mysuru": "Mysuru", "trivandrum": "Thiruvananthapuram",
    "thiruvananthapuram": "Thiruvananthapuram", "cochin": "Kochi", "kochi": "Kochi",
    "pune": "Pune", "poona": "Pune",
}


def _city_key(value: str | None) -> str | None:
    cleaned = _clean_place(value)
    return _CITY_ALIASES.get(cleaned.casefold(), cleaned) if cleaned else None


def _top_cities(rows: list[tuple[str, str | None]]) -> list[dict]:
    counts: dict[str, Counter] = defaultdict(Counter)
    for source, value in rows:
        city = _city_key(value)
        if city:
            counts[city][source] += 1
    ranked = sorted(counts.items(), key=lambda pair: (-sum(pair[1].values()), pair[0].casefold()))[:10]
    return [{"city": city, "count": sum(by_source.values()), "by_source": dict(by_source)}
            for city, by_source in ranked]


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
        from app.services.stores import pincode_area

        rows.append({
            "key": lead.lead_key,
            "created": lead.created_at,
            "occasion_type": details.get("occasionType"),
            "occasion_date": details.get("occasionDate"),
            "city": details.get("city") or details.get("location") or pincode_area(details.get("pincode")),
            "is_workflow": False,
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
        city_column = next((c for c in table.columns if sl._norm_name(c.name) in ("city", "location")), None)
        city_field = next((fields[k] for k in ("city", "location") if k in fields), None)
        pin_column = next((c for c in table.columns if sl._norm_name(c.name) in sl.PINCODE_FIELD_NAMES), None)
        pin_field = next((fields[k] for k in sl.PINCODE_FIELD_NAMES if k in fields), None)
        pin_expr = (pin_column if pin_column is not None else
                    sl._json_text_expr(table, pin_field) if pin_field is not None else None)

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
        if city_column is not None:
            exprs.append(city_column.label("city"))
            names.append("city")
        elif city_field is not None:
            exprs.append(sl._json_text_expr(table, city_field).label("city"))
            names.append("city")
        if pin_expr is not None:
            exprs.append(pin_expr.label("pincode"))
            names.append("pincode")
        if len(exprs) == 1:
            return []

        with read_engine.connect() as conn:
            rows = conn.execute(sa_select(*exprs).select_from(table).where(*base)).all()
        return [{**dict(zip(names, r)), "key": str(r[0]), "is_workflow": True} for r in rows]
    except Exception:  # noqa: BLE001
        logger.warning("Spin analytics unavailable", exc_info=True)
        return None


def _all_source_city_data(db: Session, period, vtk_rows: list[tuple] | None = None,
                          all_time: bool = False) -> dict:
    """Build the shared all-source city aggregate for analytics responses that expose Top cities."""
    wedding = db.exec(select(WeddingLead.submitted_at, WeddingLead.city).where(
        WeddingLead.is_deleted == False  # noqa: E712
    )).all()
    planner = db.exec(select(WeddingPlan.updated_in_sheet_at, WeddingPlan.city).where(
        WeddingPlan.is_deleted == False  # noqa: E712
    )).all()
    if vtk_rows is None:
        vtk_rows = db.exec(select(VendorToKocLead.created_at, VendorToKocLead.city)).all()
    spin = _spin_rows(db)
    keep = (lambda _created: True) if all_time else (
        lambda created: period.contains(periods.ist_date(created)))
    city_rows = [("wedding", city) for created, city in wedding if keep(created)]
    city_rows += [("wedding_planner", city) for created, city in planner
                  if keep(created)]
    city_rows += [("vendor_to_koc", city) for created, city in vtk_rows
                  if keep(created)]
    if spin is not None:
        from app.services.stores import pincode_area
        city_rows += [("spin", row.get("city") or pincode_area(row.get("pincode")))
                      for row in spin if keep(row.get("created"))]
    sources = (["spin", "wedding", "wedding_planner", "vendor_to_koc"] if spin is not None else
               ["wedding", "wedding_planner", "vendor_to_koc"])
    return {
        "top_cities": _top_cities(city_rows),
        "top_cities_sources": sources,
        "top_cities_no_city": sum(1 for _, city in city_rows if not _city_key(city)),
        "top_cities_no_city_by_source": {
            source: sum(1 for row_source, city in city_rows if row_source == source and not _city_key(city))
            for source in sources
        },
    }


# ── Build the overview ─────────────────────────────────────────────────
def _counts(rows_by_source: dict[str, list], keep) -> dict[str, int]:
    return {s: sum(1 for arrival in rows if keep(arrival)) for s, rows in rows_by_source.items()}


def _outcome(metas: list, hot_count: int = 0) -> dict:
    """The KPIs (app/services/kpis.py) over a set of leads (one LeadMeta or None per lead), plus
    "leads" (= total, kept for the existing screen)."""
    numbers = kpis.compute(metas)
    converted_amounts = [m.conversion_amount for m in metas if m is not None
                         and m.update_status == "converted" and m.conversion_amount is not None]
    return {"leads": numbers["total"], **numbers,
            "conversion_amount_count": len(converted_amounts),
            "conversion_amount_total": sum(converted_amounts),
            "quotes_count": numbers["quoted_count"], "hot_count": hot_count}


def _outcomes(db: Session, keys_by_source: dict[str, list[str]], spin_available: bool) -> dict:
    """{source: outcome, "all": outcome}; spin is None when the workflow database can't be read."""
    out: dict[str, Any] = {}
    every: list = []
    for source in SOURCES:
        if source == "spin" and not spin_available:
            out[source] = None
            continue
        metas = {m.lead_key: m for m in db.exec(select(LeadMeta).where(LeadMeta.source == source))}
        per_lead = [metas.get(k) for k in keys_by_source[source]]
        from app.services import hot_leads
        hot = hot_leads.hot_keys(db, source)
        hot_count = sum(1 for key in keys_by_source[source] if str(key) in hot)
        out[source] = _outcome(per_lead, hot_count)
        every += per_lead
    from app.services import hot_leads
    all_hot = sum((part or {}).get("hot_count", 0) for part in out.values())
    out["all"] = _outcome(every, all_hot)
    return out


def _change(new: int, previous: int) -> int | None:
    return round((new - previous) / previous * 100) if previous else None


def _build(db: Session, keys: dict[str, set[str]] | None = None,
           period: periods.Period | periods.AllTime | None = None, vtk_vendor_id: int | None = None) -> dict:
    """keys: {source: lead keys} to limit every number to (a vendor's own leads); None = all.
    period: the dashboard's date range (default: the last 30 days).

    Arrival date of a lead (India calendar day): spin created_at, International and Wedding
    submitted_at, Wedding planner updated_in_sheet_at (the sheet's "Last updated"); added and
    imported leads use the same columns (filled with the entry time, or the uploaded value).
    Every number covers the leads that arrived in the period; this_week / last_week keep their
    original meaning (last 7 days vs the 7 before) until the screen stops using them."""
    period = period or periods.default_period()
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
    if isinstance(period, periods.AllTime):
        # All time: every lead the viewer can see (also those without an arrival date); the period
        # runs from the earliest arrival to today, with monthly inflow points and no comparison
        known = [d for days in arrival.values() for d in days if d is not None]
        period = period.resolve(min(known) if known else None, today)

        def in_period(_):
            return True
        previous = None
    else:
        in_period = period.contains
        previous = period.previous()

    # Every number follows the period: totals are the leads that arrived in it
    new = _counts(arrival, in_period)
    before = _counts(arrival, previous.contains) if previous else None
    totals = dict(new)
    if spin is None:
        totals["spin"] = None
    this_week = _counts(arrival, lambda d: d is not None and week_start <= d <= today)
    last_week = _counts(arrival, lambda d: d is not None and prev_week_start <= d < week_start)

    # Inflow: one point per day, per 7 days for long periods, per month for all time
    buckets = period.buckets()
    trend = [{"date": start.isoformat(), "end": end.isoformat(), **dict.fromkeys(SOURCES, 0)} for start, end in buckets]
    first = {start: i for i, (start, _) in enumerate(buckets)}
    for source, days in arrival.items():
        for d in days:
            if d is not None and period.contains(d):
                trend[first[period.bucket_start(d)]][source] += 1

    # Upcoming events of the leads that arrived in the period (events from today on)
    months = []
    y, m = today.year, today.month
    for _ in range(UPCOMING_MONTHS):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    upcoming = {k: dict.fromkeys(SOURCES, 0) for k in months}
    for source, items in events.items():
        for event, d in zip(items, arrival[source]):
            if not in_period(d) or event is None or event < today.replace(day=1):
                continue
            key = f"{event.year:04d}-{event.month:02d}"
            if key in upcoming:
                upcoming[key][source] += 1

    # Everything below: leads that arrived in the period
    wa_p = [r for r, d in zip(wa, arrival["whatsapp"]) if in_period(d)]
    wd_p = [r for r, d in zip(wd, arrival["wedding"]) if in_period(d)]
    wp_p = [r for r, d in zip(wp, arrival["wedding_planner"]) if in_period(d)]
    spin_p = [r for r, d in zip(spin or [], arrival["spin"]) if in_period(d)]

    # CRM outcomes of the period's leads: crm_lead_meta matched by the same lead keys as the lists
    period_keys = {
        "whatsapp": [r[-1] for r in wa_p],
        "wedding": [r[-1] for r in wd_p],
        "wedding_planner": [r[-1] for r in wp_p],
        "spin": [str(r.get("key")) for r in spin_p],
    }
    outcomes = _outcomes(db, period_keys, spin_available=spin is not None)

    city_rows: list[tuple[str, str | None]] = [("wedding", r[2]) for r in wd_p]
    city_rows += [("wedding_planner", r[2]) for r in wp_p]
    from app.services.stores import pincode_area
    city_rows += [("spin", r.get("city") or pincode_area(r.get("pincode"))) for r in spin_p]
    vtk_stmt = select(VendorToKocLead.created_at, VendorToKocLead.city)
    if vtk_vendor_id is not None:
        vtk_stmt = vtk_stmt.where(VendorToKocLead.vendor_id == vtk_vendor_id)
    vtk_rows = [(created, city) for created, city in db.exec(vtk_stmt).all()
                if in_period(periods.ist_date(created))]
    city_rows += [("vendor_to_koc", city) for _, city in vtk_rows]
    top_cities = _top_cities(city_rows)
    cities_no_city = sum(1 for _, city in city_rows if not _city_key(city))
    top_cities_sources = (["spin", "wedding", "wedding_planner", "vendor_to_koc"] if spin is not None else
                          ["wedding", "wedding_planner", "vendor_to_koc"])
    cities_no_city_by_source = {
        source: sum(1 for row_source, city in city_rows if row_source == source and not _city_key(city))
        for source in top_cities_sources
    }
    countries = Counter((r[2] or "").strip() for r in wa_p if r[2])
    occasions = Counter((r[3] or "").strip().title() for r in wd_p if r[3])
    occasions.update((str(r.get("occasion_type") or "")).strip().title() for r in spin_p if r.get("occasion_type"))
    occasions.pop("", None)

    partners = Counter(r[5] for r in wd_p if r[5])
    partners.update(r[7] for r in wp_p if r[7])
    shareable = len(wd_p) + len(wp_p)
    not_shared = sum(1 for r in wd_p if not r[5]) + sum(1 for r in wp_p if not r[7])

    photo_budgets = [r[4] for r in wd_p if r[4]]
    guests = [r[6] for r in wp_p if r[6]]
    fits = Counter("fits" if r[5] is True else "over" if r[5] is False else "unknown" for r in wp_p)

    # Needs attention: among the leads that arrived in the period
    open_not_shared = not_shared
    open_over_budget = sum(1 for r in wp_p if r[5] is False)
    # Per-source parts (drill-down): wedding and planner leads are the shareable ones
    share_by_source = {
        "wedding": {"shareable": len(wd_p), "not_shared": sum(1 for r in wd_p if not r[5])},
        "wedding_planner": {"shareable": len(wp_p), "not_shared": sum(1 for r in wp_p if not r[7])},
    }
    for part in share_by_source.values():
        part["shared"] = part["shareable"] - part["not_shared"]
    attention_by_source = {
        "wedding": {"not_shared": share_by_source["wedding"]["not_shared"], "over_budget": 0},
        "wedding_planner": {"not_shared": share_by_source["wedding_planner"]["not_shared"],
                            "over_budget": open_over_budget},
    }
    for part in attention_by_source.values():
        part["total"] = part["not_shared"] + part["over_budget"]

    def money(values: list[int]) -> dict:
        return {
            "count": len(values),
            "total": sum(values),
            "average": round(statistics.mean(values)) if values else None,
            "median": round(statistics.median(values)) if values else None,
        }

    known_totals = [v for v in totals.values() if v is not None]
    new_all = sum(new.values())
    before_all = sum(before.values()) if before is not None else None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "today": today.isoformat(),
        "period": period.as_dict(),
        "not_filtered": [],                       # every number follows the period
        "spin_available": spin is not None,
        "totals": {**totals, "all": sum(known_totals)},
        "new_in_period": {**new, "all": new_all},
        # No comparison for all time
        "previous_period": {**before, "all": before_all} if before is not None else None,
        "change_pct": ({**{s: _change(new[s], before[s]) for s in SOURCES}, "all": _change(new_all, before_all)}
                       if before is not None else None),
        # The six KPIs over every source's leads of the period (outcomes has them per source)
        "kpis": {**{k: outcomes["all"][k] for k in kpis.KEYS},
                 "conversion_amount_count": outcomes["all"]["conversion_amount_count"],
                 "conversion_amount_total": outcomes["all"]["conversion_amount_total"],
                 "quotes_count": outcomes["all"]["quotes_count"], "hot_count": outcomes["all"]["hot_count"]},
        "source_mix": [
            {"source": s, "count": new[s], "pct": round(new[s] / new_all * 100) if new_all else 0} for s in SOURCES
        ],
        # Kept with their original meaning (last 7 days vs the 7 before) until the screen uses the fields above
        "this_week": {**this_week, "all": sum(this_week.values())},
        "last_week": {**last_week, "all": sum(last_week.values())},
        "trend": trend,
        "upcoming_by_month": [{"month": k, **v} for k, v in upcoming.items()],
        "outcomes": outcomes,
        "needs_attention": {
            "not_shared": open_not_shared,
            "over_budget": open_over_budget,
            "total": open_not_shared + open_over_budget,
            "by_source": attention_by_source,
        },
        "top_cities": top_cities,
        "top_cities_sources": top_cities_sources,
        "top_cities_no_city": cities_no_city,
        "top_cities_no_city_by_source": cities_no_city_by_source,
        "top_countries": _top(countries),
        "occasions": _top(occasions, 6),
        "partners": _top(partners, 8),
        "sharing": {
            "shareable": shareable,
            "shared": shareable - not_shared,
            "not_shared": not_shared,
            "rate": round((shareable - not_shared) / shareable * 100) if shareable else None,
            "by_source": share_by_source,
        },
        "photography_budget": money(photo_budgets),
        "guests": {"average": round(statistics.mean(guests)) if guests else None, "total": sum(guests)},
        "budget_fit": dict(fits),
        "repeats": sum(1 for r in wd_p if r[6]),
    }


def _cache_key(scope, period) -> tuple:
    """Per viewer scope (admin, or the vendor: its logins see the same leads) and per period."""
    if isinstance(period, periods.AllTime):
        return ("analytics", "overview", scope, "all", periods.today_ist().isoformat())
    return ("analytics", "overview", scope, period.date_from.isoformat(), period.date_to.isoformat())


def _builder(bind, keys, period, vtk_vendor_id=None):
    """A build that opens its own session, so a background refresh never uses a closed one."""
    def build():
        with Session(bind) as session:
            return _build(session, keys, period, vtk_vendor_id)
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
    return response_cache.get(_cache_key(("vendor", viewer.vendor_id), period),
                              _builder(bind, keys, period, viewer.vendor_id),
                              fresh=FRESH_SECONDS, stale=FRESH_SECONDS)


# ── Vendor -> KOC dashboard ────────────────────────────────────────────
def _vtk_build(db: Session, vendor_id: int | None, period, admin: bool) -> dict:
    """GET /analytics/vendor-to-koc. Arrival date = created_at (IST day). vendor_id: a vendor
    login's own leads only (no previous_received, per_vendor, top_cities, by_source, latest);
    admin: every vendor and every number."""
    from app.crud.vendor_to_koc import serialize
    from app.models.vendor import Vendor
    from app.models.vendor_to_koc import CLOSED_STATUSES, LeadSource, UpdateStatus, VendorToKocLead

    today = periods.today_ist()
    all_time = isinstance(period, periods.AllTime)
    from sqlalchemy.orm import aliased
    from app.models.user import User

    updater = aliased(User)
    stmt = (select(VendorToKocLead, Vendor.name, updater.name)
            .join(Vendor, Vendor.id == VendorToKocLead.vendor_id)
            .outerjoin(updater, updater.id == VendorToKocLead.status_updated_by))
    if vendor_id is not None:
        stmt = stmt.where(VendorToKocLead.vendor_id == vendor_id)
    named = db.exec(stmt).all()
    updater_of = {lead.id: who for lead, _, who in named}
    rows = [(lead, name) for lead, name, _ in named]
    arrival = [periods.ist_date(lead.created_at) for lead, _ in rows]

    if isinstance(period, periods.AllTime):
        known = [d for d in arrival if d is not None]
        period = period.resolve(min(known) if known else None, today)

        def in_period(_):
            return True
        previous = None
    else:
        in_period = period.contains
        previous = period.previous()

    current = [(lead, name) for (lead, name), d in zip(rows, arrival) if in_period(d)]
    received = len(current)
    before = sum(1 for d in arrival if previous is not None and previous.contains(d)) if previous else None

    # Inflow per vendor: day / week / month points like the overview
    buckets = period.buckets()
    first = {start: i for i, (start, _) in enumerate(buckets)}
    inflow = [{"date": s.isoformat(), "end": e.isoformat(), "total": 0, "vendors": {}} for s, e in buckets]
    legend: dict[int, str] = {}
    for (lead, name), d in zip(rows, arrival):
        if d is not None and period.contains(d) and in_period(d):
            point = inflow[first[period.bucket_start(d)]]
            point["total"] += 1
            point["vendors"][str(lead.vendor_id)] = point["vendors"].get(str(lead.vendor_id), 0) + 1
            legend[lead.vendor_id] = name

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "today": today.isoformat(),
        "period": period.as_dict(),
        "received": received,
        "change_pct": _change(received, before) if before is not None else None,
        "kpis": kpis.compute(lead for lead, _ in current),      # the period's leads (vendors: their own)
        "inflow": inflow,
        "inflow_vendors": [{"id": vid, "name": legend[vid]} for vid in sorted(legend, key=lambda v: legend[v].lower())],
    }
    out["kpis"]["quotes_count"] = out["kpis"]["quoted_count"]
    out["kpis"]["hot_count"] = sum(1 for lead, _ in current if lead.is_hot)
    converted_with_amount = [lead.conversion_amount for lead, _ in current
                             if lead.update_status == UpdateStatus.CONVERTED.value
                             and lead.conversion_amount is not None]
    out["kpis"]["conversion_amount_count"] = len(converted_with_amount)
    out["kpis"]["conversion_amount_total"] = sum(converted_with_amount)
    def is_open(lead) -> bool:
        return lead.update_status is None or lead.update_status not in CLOSED_STATUSES

    status_of = [lead.update_status for lead, _ in current]
    converted = status_of.count(UpdateStatus.CONVERTED.value)
    engaged_statuses = {UpdateStatus.INTERESTED.value, UpdateStatus.WILL_CALL_BACK.value, UpdateStatus.CONVERTED.value}

    per_vendor: dict[int, dict] = {}
    for lead, name in current:
        v = per_vendor.setdefault(lead.vendor_id, {"vendor_id": lead.vendor_id, "vendor_name": name, "leads": 0,
                                                   "interested": 0, "converted": 0, "pending": 0, "_days": []})
        v["leads"] += 1
        v["interested"] += lead.update_status == UpdateStatus.INTERESTED.value
        v["converted"] += lead.update_status == UpdateStatus.CONVERTED.value
        v["pending"] += lead.update_status is None
        if lead.update_status is not None and lead.status_updated_at is not None and lead.created_at is not None:
            created = lead.created_at if lead.created_at.tzinfo else lead.created_at.replace(tzinfo=timezone.utc)
            updated = lead.status_updated_at if lead.status_updated_at.tzinfo else lead.status_updated_at.replace(tzinfo=timezone.utc)
            v["_days"].append((updated - created).total_seconds() / 86400)
    vendors_out = []
    for v in sorted(per_vendor.values(), key=lambda x: (-x["leads"], x["vendor_name"].lower())):
        days = v.pop("_days")
        v["converted_pct"] = round(v["converted"] / v["leads"] * 100, 1) if v["leads"] else None
        v["average_days_to_update"] = round(sum(days) / len(days), 1) if days else None
        vendors_out.append(v)

    # Follow-ups (NOT limited to the period): open leads with a follow-up date
    due = [(lead, name) for lead, name in rows if lead.follow_up_date is not None and is_open(lead)]
    due.sort(key=lambda pair: (pair[0].follow_up_date, pair[0].id))
    latest = sorted(current, key=lambda pair: (pair[0].created_at, pair[0].id), reverse=True)[:6]

    # Everyone (vendor logins: over their own leads only)
    out.update({
        "not_filtered": ["follow_ups_due", "next_follow_ups"],
        "pending": status_of.count(None),
        "follow_ups_due": sum(1 for lead, _ in due if lead.follow_up_date <= today),
        "converted": converted,
        "converted_pct": round(converted / received * 100, 1) if received else None,
        "purchased_with_us": status_of.count(UpdateStatus.PURCHASED_WITH_US.value),
        "by_status": {"pending": status_of.count(None), **{s.value: status_of.count(s.value) for s in UpdateStatus}},
        "funnel": {
            "received": received,
            "updated": sum(1 for s in status_of if s is not None),
            "engaged": sum(1 for s in status_of if s in engaged_statuses),
            "converted": converted,
        },
        "next_follow_ups": [serialize(lead, name, admin, updater_of[lead.id]) for lead, name in due[:10]],
    })
    if not admin:
        return out
    city_data = _all_source_city_data(db, period, all_time=all_time)
    out.update({
        "previous_received": before,
        "per_vendor": vendors_out,
        **city_data,
        "by_source": {s.value: sum(1 for lead, _ in current if lead.source == s.value) for s in LeadSource},
        "latest": [serialize(lead, name, True, updater_of[lead.id]) for lead, name in latest],
    })
    return out


@router.get("/vendor-to-koc")
def vendor_to_koc_overview(viewer: LeadViewer, db: DbSession, period: DashboardPeriod, refresh: bool = Query(False)):
    """Vendor -> KOC dashboard for a period (same rules as /analytics/overview). Admins: every vendor
    and every number. Vendor logins: their own leads, without previous_received, per_vendor,
    top_cities, by_source and latest."""
    if refresh:
        response_cache.clear("analytics")
    scope = "admin" if viewer.is_admin else ("vendor", viewer.vendor_id)
    bind = db.get_bind()

    def build():
        with Session(bind) as session:
            return _vtk_build(session, None if viewer.is_admin else viewer.vendor_id, period, viewer.is_admin)

    key = ("analytics", "vendor-to-koc", scope) + _cache_key(scope, period)[3:]
    return response_cache.get(key, build, fresh=FRESH_SECONDS, stale=FRESH_SECONDS)


# ── Dhruva dashboard ───────────────────────────────────────────────────
@router.get("/dhruva")
def dhruva_overview(user: DhruvaSectionUser, db: DbSession, period: AllTimeDefaultPeriod):
    """Dhruva leads dashboard. The shared period rules, but all time by default; received date =
    created_at (IST day). Admins and Dhruva logins (read-only); vendor logins: 403."""
    from app.crud import dhruva as dcrud
    from app.models.dhruva import DhruvaLead
    from app.models.user import Role
    from app.models.vendor_to_koc import UpdateStatus

    today = periods.today_ist()
    named = db.exec(dcrud.with_names().where(DhruvaLead.is_deleted == False)).all()  # noqa: E712
    arrival = [periods.ist_date(row.created_at) for row, _, _ in named]
    if isinstance(period, periods.AllTime):
        known = [d for d in arrival if d is not None]
        period = period.resolve(min(known) if known else None, today)

        def in_period(_):
            return True
        previous = None
    else:
        in_period = period.contains
        previous = period.previous()
    current = [(row, c, u) for (row, c, u), d in zip(named, arrival) if in_period(d)]
    leads = [row for row, _, _ in current]
    received = len(leads)
    before = sum(1 for d in arrival if previous.contains(d)) if previous else None

    numbers = kpis.compute(leads)
    numbers["quotes_count"] = numbers["quoted_count"]
    converted_with_amount = [r.conversion_amount for r in leads
                             if r.update_status == UpdateStatus.CONVERTED.value and r.conversion_amount is not None]
    numbers["conversion_amount_count"] = len(converted_with_amount)
    numbers["conversion_amount_total"] = sum(converted_with_amount)
    from app.services import hot_leads
    hot_keys = hot_leads.hot_keys(db, "dhruva")
    numbers["hot_count"] = sum(1 for r in leads if str(r.id) in hot_keys)
    numbers["pending"] = sum(1 for r in leads if r.update_status is None)
    numbers["upcoming_30_days"] = sum(1 for r in leads if r.event_date is not None
                                      and today <= r.event_date <= today + timedelta(days=30))

    buckets = period.buckets()
    first = {start: i for i, (start, _) in enumerate(buckets)}
    inflow = [{"date": s.isoformat(), "end": e.isoformat(), "total": 0} for s, e in buckets]
    for row, d in zip(named, arrival):
        if d is not None and period.contains(d) and in_period(d):
            inflow[first[period.bucket_start(d)]]["total"] += 1

    types: Counter = Counter()
    for r in leads:
        types.update({p.strip().lower() for p in (r.event_type or "").split(",") if p.strip()})
    venue_names: dict[str, Counter] = {}
    for r in leads:
        if r.venue:
            venue_names.setdefault(" ".join(r.venue.split()).lower(), Counter())[r.venue] += 1
    venues = sorted(({"value": spellings.most_common(1)[0][0], "count": sum(spellings.values())}
                     for spellings in venue_names.values()), key=lambda v: (-v["count"], v["value"].lower()))
    months = Counter(f"{r.event_date.year:04d}-{r.event_date.month:02d}" for r in leads if r.event_date)
    admin = user.role == Role.ADMIN.value
    latest = sorted(current, key=lambda t: (t[0].created_at, t[0].id), reverse=True)[:6]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "today": today.isoformat(),
        "period": period.as_dict(),
        "received": received,
        "previous_period": before,
        "change_pct": _change(received, before) if before is not None else None,
        "kpis": numbers,
        "by_event_type": [{"value": k, "count": n} for k, n in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))],
        "top_venues": venues[:10],
        "events_by_month": [{"month": m, "count": months[m]} for m in sorted(months)],
        "no_event_date": sum(1 for r in leads if r.event_date is None),
        "inflow": inflow,
        "by_status": {"pending": numbers["pending"],
                      **{s.value: sum(1 for r in leads if r.update_status == s.value) for s in UpdateStatus}},
        "latest": [dcrud.serialize(*t, admin=admin, is_hot=str(t[0].id) in hot_keys) for t in latest],
    }
