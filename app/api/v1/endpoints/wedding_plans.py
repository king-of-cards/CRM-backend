"""Wedding plans copied from the "Wedding planner" Google Sheet (admin only).
Reads from MySQL only: fast.

GET /wedding-plans         list with filters (app/services/lead_filters.py + planner ones), sort, pages
GET /wedding-plans/stats   numbers for the header, over the same filtered plans
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, false, func
from sqlmodel import col, select

from app.api.deps import DbSession, LeadViewer, PageSize, common_filter_params
from app.models.wedding_plan import WeddingPlan
from app.services import access, also_in, kpis, lead_crm, lead_filters

router = APIRouter(prefix="/wedding-plans", tags=["wedding-plans"])

SORTABLE = {
    "updated": WeddingPlan.updated_in_sheet_at,
    "wedding_date": WeddingPlan.wedding_date,
    "total_budget": WeddingPlan.total_budget,
    "total_guests": WeddingPlan.total_guests,
    "plan_estimate": WeddingPlan.plan_estimate,
    "name": WeddingPlan.name,
}

NOT_SHARED = "__none__"


def _utc(value: datetime | None) -> str | None:
    """MySQL returns naive datetimes; they are stored as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _out(p: WeddingPlan) -> dict:
    return {
        "id": p.id,
        "plan_id": p.plan_id,
        "origin": p.origin,             # sheet | added | imported
        "last_step": p.last_step,
        "updated_at": _utc(p.updated_in_sheet_at),
        "versions": p.versions,
        "name": p.name,
        "phone": p.phone,
        "phone_display": f"+{p.phone}" if p.phone else None,
        "email": p.email,
        "role": p.role,
        "consent_share": p.consent_share,
        "consent_offers": p.consent_offers,
        "wedding_type": p.wedding_type,
        "city": p.city,
        "home_city": p.home_city,
        "place_kind": p.place_kind,
        "wedding_month": p.wedding_month,
        "first_day": p.first_day.isoformat() if p.first_day else None,
        "wedding_date": p.wedding_date.isoformat() if p.wedding_date else None,
        "total_guests": p.total_guests,
        "guests_staying": p.guests_staying,
        "traditions": p.traditions,
        "events": p.events or [],
        "food": p.food,
        "alcohol": p.alcohol,
        "budget_range": p.budget_range,
        "total_budget": p.total_budget,
        "budget_includes_jewellery": p.budget_includes_jewellery,
        "jewellery_outfits": p.jewellery_outfits,
        "plan_estimate": p.plan_estimate,
        "fits_budget": p.fits_budget,
        "priorities": p.priorities or [],
        "shortlisted_vendors": p.shortlisted_vendors,
        "shared_with": p.shared_with,
    }


@dataclass
class PlanFilters:
    common: lead_filters.CommonFilters
    shared_with: str | None = None
    fits_budget: bool | None = None
    last_step: str | None = None
    city: str | None = None
    guests_min: int | None = None
    guests_max: int | None = None
    budget_min: int | None = None
    budget_max: int | None = None


def plan_filters(
    common: Annotated[lead_filters.CommonFilters,
                      Depends(common_filter_params(lead_filters.SHEET_ORIGINS, "Wedding planner leads"))],
    shared_with: str | None = Query(None, max_length=200, description=f"Partner name, or {NOT_SHARED} for not shared"),
    fits_budget: bool | None = None,
    last_step: str | None = Query(None, max_length=50),
    city: str | None = Query(None, max_length=200, description="Wedding city contains (case-insensitive)"),
    guests_min: int | None = Query(None, ge=0),
    guests_max: int | None = Query(None, ge=0),
    budget_min: int | None = Query(None, ge=0, description="Total budget at least (rupees)"),
    budget_max: int | None = Query(None, ge=0, description="Total budget at most (rupees)"),
) -> PlanFilters:
    lead_filters.check_range(guests_min, guests_max, "guests")
    lead_filters.check_range(budget_min, budget_max, "budget")
    return PlanFilters(common, shared_with or None, fits_budget, (last_step or "").lower() or None,
                       (city or "").strip() or None, guests_min, guests_max, budget_min, budget_max)


Filters = Annotated[PlanFilters, Depends(plan_filters)]


@router.get("")
def list_wedding_plans(
    viewer: LeadViewer,
    db: DbSession,
    f: Filters,
    page: int = Query(1, ge=1),
    page_size: PageSize = 10,
    sort: Literal["updated", "wedding_date", "total_budget", "total_guests", "plan_estimate", "name"] = "updated",
    order: Literal["asc", "desc"] = "desc",
):
    filters = build_filters(db, viewer, f)
    total = db.exec(select(func.count()).select_from(WeddingPlan).where(*filters)).one()
    rows = db.exec(
        select(WeddingPlan).where(*filters).order_by(*ordering(sort, order))
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    items = attach_crm(db, [_out(r) for r in rows])
    keys = [i["plan_id"] for i in items]
    also_in.attach(db, viewer, "wedding_planner", items, key_of=lambda i: i["plan_id"],
                   e164_of=also_in.page_phones(db, "wedding_planner", keys))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ── Shared by the list, the stats and the export (GET /crm-leads/wedding_planner/export) ──
def build_filters(db, viewer, f: PlanFilters) -> list:
    """SQL conditions for the plans the viewer may see that match the filters. Archived and
    removed rows (is_deleted) are never listed, counted or exported."""
    c = f.common
    filters = [WeddingPlan.is_deleted == False]  # noqa: E712
    keys = lead_filters.allowed_keys(db, "wedding_planner", viewer, c)
    if keys is not None:
        filters.append(col(WeddingPlan.plan_id).in_(keys) if keys else false())
    if c.period is not None:
        # Arrived in the period (IST days): updated_in_sheet_at is stored in UTC
        start, end = c.period.utc_bounds()
        filters += [WeddingPlan.updated_in_sheet_at >= start, WeddingPlan.updated_in_sheet_at < end]
    if c.event_from is not None:                       # the wedding date (first day, else the month)
        filters.append(WeddingPlan.wedding_date >= c.event_from)
    if c.event_to is not None:
        filters.append(WeddingPlan.wedding_date <= c.event_to)
    if c.origins:
        filters.append(col(WeddingPlan.origin).in_(c.origins))
    if f.shared_with == NOT_SHARED:
        filters.append(WeddingPlan.shared_with.is_(None))
    elif f.shared_with:
        filters.append(WeddingPlan.shared_with == f.shared_with)
    if f.fits_budget is not None:
        filters.append(WeddingPlan.fits_budget == f.fits_budget)
    if f.last_step:
        filters.append(WeddingPlan.last_step == f.last_step)
    if f.city:
        filters.append(lead_filters.contains(WeddingPlan.city, f.city))
    if f.guests_min is not None:
        filters.append(WeddingPlan.total_guests >= f.guests_min)
    if f.guests_max is not None:
        filters.append(WeddingPlan.total_guests <= f.guests_max)
    if f.budget_min is not None:
        filters.append(WeddingPlan.total_budget >= f.budget_min)
    if f.budget_max is not None:
        filters.append(WeddingPlan.total_budget <= f.budget_max)
    if c.search:
        filters.append(lead_filters.search_condition(
            c.search,
            [WeddingPlan.name, WeddingPlan.email, WeddingPlan.city, WeddingPlan.plan_id, WeddingPlan.traditions,
             WeddingPlan.shared_with],
            WeddingPlan.phone,
        ))
    return filters


def ordering(sort: str, order: str) -> list:
    column = SORTABLE[sort]
    return [column.is_(None), column.desc() if order == "desc" else column.asc(), WeddingPlan.id.desc()]


def attach_crm(db, items: list[dict]) -> list[dict]:
    """Add the CRM details (vendor, update, quote, conversion amount) from the sidecar table."""
    return lead_crm.attach(
        db,
        "wedding_planner",
        items,
        key=lambda i: i["plan_id"],
        location=lambda i: i["city"] or i["home_city"],
        sheet_vendor=lambda i: i["shared_with"],
    )


@router.get("/stats")
def wedding_plan_stats(viewer: LeadViewer, db: DbSession, f: Filters):
    """Numbers over the plans the list shows for the same filters (vendors: their own)."""
    active = and_(*build_filters(db, viewer, f))
    now = datetime.now(timezone.utc)

    def count(*conditions) -> int:
        return db.exec(select(func.count()).select_from(WeddingPlan).where(active, *conditions)).one()

    def grouped(column):
        return [
            {"value": value, "count": n}
            for value, n in db.exec(
                select(column, func.count()).where(active, column.is_not(None)).group_by(column).order_by(func.count().desc())
            ).all()
        ]

    total_budget = db.exec(select(func.sum(WeddingPlan.total_budget)).where(active)).one() or 0

    return {
        "total": count(),
        "last_7_days": count(WeddingPlan.updated_in_sheet_at >= now - timedelta(days=7)),
        "upcoming_weddings": count(WeddingPlan.wedding_date >= date.today().replace(day=1)),
        "not_shared": count(WeddingPlan.shared_with.is_(None)),
        "over_budget": count(WeddingPlan.fits_budget == False),  # noqa: E712
        "total_budget": int(total_budget),
        "shared_with": grouped(WeddingPlan.shared_with),
        "last_steps": grouped(WeddingPlan.last_step),
        "kpis": kpis.for_keys(db, "wedding_planner", db.exec(select(WeddingPlan.plan_id).where(active)).all()),
    }


@router.get("/filter-options")
def wedding_plan_filter_options(viewer: LeadViewer, db: DbSession):
    """Values for the filter dropdowns, with counts over the plans the viewer may see (vendor users:
    their own): cities (cleaned, for the contains filter), last steps, leads shared, origins,
    update counts, and vendors (admins only)."""
    from app.api.v1.endpoints.analytics import _clean_place

    visible = access.visible_keys(db, viewer, "wedding_planner")
    query = select(WeddingPlan.plan_id, WeddingPlan.city, WeddingPlan.last_step, WeddingPlan.shared_with,
                   WeddingPlan.origin).where(WeddingPlan.is_deleted == False)  # noqa: E712
    rows = [r for r in db.exec(query) if visible is None or r[0] in visible]
    return {
        "cities": lead_filters.counted(_clean_place(r[1]) for r in rows),
        "last_steps": lead_filters.counted(r[2] for r in rows),
        "shared_with": lead_filters.counted(r[3] for r in rows) + [
            {"value": NOT_SHARED, "count": sum(1 for r in rows if not r[3])}],
        "origins": [{"value": o, "count": sum(1 for r in rows if r[4] == o)} for o in lead_filters.SHEET_ORIGINS],
        "updates": lead_filters.update_options(db, "wedding_planner", {r[0] for r in rows}),
        "vendors": lead_filters.vendor_options(db, viewer),
    }
