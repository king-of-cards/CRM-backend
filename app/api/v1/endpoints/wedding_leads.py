"""Wedding & event photography leads copied from the Google Sheet (admin only).
Reads from MySQL only: fast.

GET /wedding-leads         list with filters (app/services/lead_filters.py + wedding ones), sort, pages
GET /wedding-leads/stats   numbers for the header, over the same filtered leads
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, false, func
from sqlmodel import col, select

from app.api.deps import DbSession, LeadViewer, PageSize, common_filter_params
from app.models.wedding_lead import WeddingLead
from app.services import access, also_in, kpis, lead_crm, lead_filters

router = APIRouter(prefix="/wedding-leads", tags=["wedding-leads"])

SORTABLE = {
    "submitted_at": WeddingLead.submitted_at,
    "event_date": WeddingLead.event_date,
    "budget_amount": WeddingLead.budget_amount,
    "name": WeddingLead.name,
    "city": WeddingLead.city,
}

NOT_SHARED = "__none__"  # shared_with filter value for leads not shared with anyone yet


def _utc(value: datetime | None) -> str | None:
    """MySQL returns naive datetimes; they are stored as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _out(lead: WeddingLead) -> dict:
    return {
        "id": lead.id,
        "lead_key": lead.sheet_key,
        "origin": lead.origin,          # sheet | added | imported
        "name": lead.name,
        "email": lead.email,
        "phone": lead.phone,
        "phone_display": f"+{lead.phone}" if lead.phone else lead.phone_raw,
        "city": lead.city,
        "occasion": lead.occasion,
        "event_date": lead.event_date.isoformat() if lead.event_date else None,
        "event_date_text": lead.event_date_text,
        "budget_amount": lead.budget_amount,
        "budget_text": lead.budget_text,
        "shared_with": lead.shared_with,
        "oseid": lead.oseid,
        "is_repeat": lead.is_repeat,
        "submitted_at": _utc(lead.submitted_at),
        "sheet_row": lead.sheet_row,
    }


@dataclass
class WeddingFilters:
    common: lead_filters.CommonFilters
    occasion: str | None = None
    shared_with: str | None = None
    hide_repeats: bool = False
    city: str | None = None
    budget_min: int | None = None
    budget_max: int | None = None


def wedding_filters(
    common: Annotated[lead_filters.CommonFilters,
                      Depends(common_filter_params(lead_filters.SHEET_ORIGINS, "Wedding leads"))],
    occasion: str | None = Query(None, max_length=100),
    shared_with: str | None = Query(None, max_length=200, description=f"Partner name, or {NOT_SHARED} for not shared"),
    hide_repeats: bool = False,
    city: str | None = Query(None, max_length=200, description="City contains (case-insensitive)"),
    budget_min: int | None = Query(None, ge=0, description="Budget at least (rupees)"),
    budget_max: int | None = Query(None, ge=0, description="Budget at most (rupees)"),
) -> WeddingFilters:
    lead_filters.check_range(budget_min, budget_max, "budget")
    return WeddingFilters(common, occasion or None, shared_with or None, hide_repeats, (city or "").strip() or None,
                          budget_min, budget_max)


Filters = Annotated[WeddingFilters, Depends(wedding_filters)]


@router.get("")
def list_wedding_leads(
    viewer: LeadViewer,
    db: DbSession,
    f: Filters,
    page: int = Query(1, ge=1),
    page_size: PageSize = 10,
    sort: Literal["submitted_at", "event_date", "budget_amount", "name", "city"] = "submitted_at",
    order: Literal["asc", "desc"] = "desc",
):
    filters = build_filters(db, viewer, f)
    total = db.exec(select(func.count()).select_from(WeddingLead).where(*filters)).one()
    rows = db.exec(
        select(WeddingLead).where(*filters).order_by(*ordering(sort, order))
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    items = [lead_crm.for_viewer(i, viewer) for i in attach_crm(db, [_out(r) for r in rows])]   # vendors: own copy
    keys = [i["lead_key"] for i in items]
    also_in.attach(db, viewer, "wedding", items, key_of=lambda i: i["lead_key"],
                   e164_of=also_in.page_phones(db, "wedding", keys))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ── Shared by the list, the stats and the export (GET /crm-leads/wedding/export) ──
def build_filters(db, viewer, f: WeddingFilters) -> list:
    """SQL conditions for the leads the viewer may see that match the filters. Archived and
    removed rows (is_deleted) are never listed, counted or exported."""
    c = f.common
    filters = [WeddingLead.is_deleted == False]  # noqa: E712
    keys = lead_filters.allowed_keys(db, "wedding", viewer, c)
    if keys is not None:
        filters.append(col(WeddingLead.sheet_key).in_(keys) if keys else false())
    if c.period is not None:
        # Arrived in the period (IST days): submitted_at is stored in UTC
        start, end = c.period.utc_bounds()
        filters += [WeddingLead.submitted_at >= start, WeddingLead.submitted_at < end]
    if c.event_from is not None:
        filters.append(WeddingLead.event_date >= c.event_from)
    if c.event_to is not None:
        filters.append(WeddingLead.event_date <= c.event_to)
    if c.origins:
        filters.append(col(WeddingLead.origin).in_(c.origins))
    if f.occasion:
        filters.append(WeddingLead.occasion == f.occasion)
    if f.shared_with == NOT_SHARED:
        filters.append(WeddingLead.shared_with.is_(None))
    elif f.shared_with:
        filters.append(WeddingLead.shared_with == f.shared_with)
    if f.hide_repeats:
        filters.append(WeddingLead.is_repeat == False)  # noqa: E712
    if f.city:
        filters.append(lead_filters.contains(WeddingLead.city, f.city))
    if f.budget_min is not None:
        filters.append(WeddingLead.budget_amount >= f.budget_min)
    if f.budget_max is not None:
        filters.append(WeddingLead.budget_amount <= f.budget_max)
    if c.search:
        filters.append(lead_filters.search_condition(
            c.search,
            [WeddingLead.name, WeddingLead.email, WeddingLead.city, WeddingLead.occasion, WeddingLead.shared_with],
            WeddingLead.phone,
        ))
    return filters


def ordering(sort: str, order: str) -> list:
    column = SORTABLE[sort]
    return [column.is_(None), column.desc() if order == "desc" else column.asc(), WeddingLead.id.desc()]


def attach_crm(db, items: list[dict]) -> list[dict]:
    """Add the CRM details (vendor, update, quote, conversion amount) from the sidecar table."""
    return lead_crm.attach(
        db,
        "wedding",
        items,
        key=lambda i: i["lead_key"],
        location=lambda i: i["city"],
        sheet_vendor=lambda i: i["shared_with"],
    )


@router.get("/stats")
def wedding_lead_stats(viewer: LeadViewer, db: DbSession, f: Filters):
    """Numbers over the leads the list shows for the same filters (vendors: their own)."""
    active = and_(*build_filters(db, viewer, f))
    now = datetime.now(timezone.utc)

    def count(*conditions) -> int:
        return db.exec(select(func.count()).select_from(WeddingLead).where(active, *conditions)).one()

    def grouped(column):
        return [
            {"value": value, "count": n}
            for value, n in db.exec(
                select(column, func.count()).where(active, column.is_not(None)).group_by(column).order_by(func.count().desc())
            ).all()
        ]

    return {
        "total": count(),
        "last_7_days": count(WeddingLead.submitted_at >= now - timedelta(days=7)),
        "upcoming_events": count(WeddingLead.event_date >= date.today()),
        "not_shared": count(WeddingLead.shared_with.is_(None)),
        "repeats": count(WeddingLead.is_repeat == True),  # noqa: E712
        "occasions": grouped(WeddingLead.occasion),
        "shared_with": grouped(WeddingLead.shared_with),
        "kpis": kpis.for_keys(db, "wedding", db.exec(select(WeddingLead.sheet_key).where(active)).all(), lead_crm.viewer_copy(viewer)),
    }


@router.get("/filter-options")
def wedding_filter_options(viewer: LeadViewer, db: DbSession):
    """Values for the filter dropdowns, with counts over the leads the viewer may see (vendor users:
    their own): occasions, cities (cleaned: "Shamirpet, Hyderabad" -> "Hyderabad", for the contains
    filter), leads shared, origins, update counts, and vendors (admins only)."""
    from app.api.v1.endpoints.analytics import _clean_place

    visible = access.visible_keys(db, viewer, "wedding")
    query = select(WeddingLead.sheet_key, WeddingLead.occasion, WeddingLead.city, WeddingLead.shared_with,
                   WeddingLead.origin).where(WeddingLead.is_deleted == False)  # noqa: E712
    rows = [r for r in db.exec(query) if visible is None or r[0] in visible]
    return {
        "occasions": lead_filters.counted((r[1] or "").strip() for r in rows),
        "cities": lead_filters.counted(_clean_place(r[2]) for r in rows),
        "shared_with": lead_filters.counted(r[3] for r in rows) + [
            {"value": NOT_SHARED, "count": sum(1 for r in rows if not r[3])}],
        "origins": [{"value": o, "count": sum(1 for r in rows if r[4] == o)} for o in lead_filters.SHEET_ORIGINS],
        "updates": lead_filters.update_options(db, "wedding", {r[0] for r in rows}, viewer),
        "vendors": lead_filters.vendor_options(db, viewer),
    }
