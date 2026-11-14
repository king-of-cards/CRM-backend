"""Wedding plans copied from the "Wedding planner" Google Sheet (admin only).
Reads from MySQL only: fast.

GET /wedding-plans         list with search, filters, sort and pages
GET /wedding-plans/stats   numbers for dashboard cards and filter options
"""
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import and_, false, func, or_
from sqlmodel import col, select

from app.api.deps import DbSession, LeadViewer
from app.models.wedding_plan import WeddingPlan
from app.services import access, lead_crm

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


@router.get("")
def list_wedding_plans(
    viewer: LeadViewer,
    db: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    search: str | None = Query(None, max_length=100),
    shared_with: str | None = Query(None, max_length=200, description=f"Partner name, or {NOT_SHARED} for not shared"),
    fits_budget: bool | None = None,
    last_step: str | None = Query(None, max_length=50),
    sort: Literal["updated", "wedding_date", "total_budget", "total_guests", "plan_estimate", "name"] = "updated",
    order: Literal["asc", "desc"] = "desc",
):
    filters = build_filters(search, shared_with, fits_budget, last_step, access.visible_keys(db, viewer, "wedding_planner"))
    total = db.exec(select(func.count()).select_from(WeddingPlan).where(*filters)).one()
    rows = db.exec(
        select(WeddingPlan).where(*filters).order_by(*ordering(sort, order))
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {"items": attach_crm(db, [_out(r) for r in rows]), "total": total, "page": page, "page_size": page_size}


# ── Shared with the export (GET /crm-leads/wedding_planner/export) ──────
def build_filters(search: str | None, shared_with: str | None, fits_budget: bool | None, last_step: str | None, keys: set[str] | None = None) -> list:
    """Archived and removed rows (is_deleted) are never listed or exported.
    keys: the lead keys the viewer may see (None = all, for admins)."""
    filters = [WeddingPlan.is_deleted == False]  # noqa: E712
    if keys is not None:
        filters.append(col(WeddingPlan.plan_id).in_(keys) if keys else false())
    if shared_with == NOT_SHARED:
        filters.append(WeddingPlan.shared_with.is_(None))
    elif shared_with:
        filters.append(WeddingPlan.shared_with == shared_with)
    if fits_budget is not None:
        filters.append(WeddingPlan.fits_budget == fits_budget)
    if last_step:
        filters.append(WeddingPlan.last_step == last_step.lower())
    if search and search.strip():
        like = f"%{search.strip()}%"
        digits = "".join(ch for ch in search if ch.isdigit())
        conditions = [
            col(WeddingPlan.name).like(like),
            col(WeddingPlan.email).like(like),
            col(WeddingPlan.city).like(like),
            col(WeddingPlan.plan_id).like(like),
            col(WeddingPlan.traditions).like(like),
            col(WeddingPlan.shared_with).like(like),
        ]
        if digits:
            conditions.append(col(WeddingPlan.phone).like(f"%{digits}%"))
        filters.append(or_(*conditions))
    return filters


def ordering(sort: str, order: str) -> list:
    column = SORTABLE[sort]
    return [column.is_(None), column.desc() if order == "desc" else column.asc(), WeddingPlan.id.desc()]


def attach_crm(db, items: list[dict]) -> list[dict]:
    """Add the CRM details (vendor, interest, quote, converted) from the sidecar table."""
    return lead_crm.attach(
        db,
        "wedding_planner",
        items,
        key=lambda i: i["plan_id"],
        location=lambda i: i["city"] or i["home_city"],
        sheet_vendor=lambda i: i["shared_with"],
    )


@router.get("/stats")
def wedding_plan_stats(viewer: LeadViewer, db: DbSession):
    # Only the leads the viewer may see (vendors: their own)
    active = and_(*build_filters(None, None, None, None, access.visible_keys(db, viewer, "wedding_planner")))
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
    }