"""WhatsApp leads copied from the Google Sheet (admin only). Reads from MySQL only: fast.

GET /whatsapp-leads         list with filters (see app/services/lead_filters.py + country), sort, pages
GET /whatsapp-leads/stats   numbers for the header, over the same filtered leads
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, false, func
from sqlmodel import col, select

from app.api.deps import DbSession, LeadViewer, common_filter_params
from app.models.whatsapp_lead import WhatsAppLead
from app.services import lead_crm, lead_filters

router = APIRouter(prefix="/whatsapp-leads", tags=["whatsapp-leads"])

SORTABLE = {
    "submitted_at": WhatsAppLead.submitted_at,
    "event_date": WhatsAppLead.event_date,
    "country": WhatsAppLead.country,
    "phone": WhatsAppLead.phone,
    "first_seen_at": WhatsAppLead.first_seen_at,
}


def _utc(value: datetime | None) -> str | None:
    """MySQL returns naive datetimes; they are stored as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _out(lead: WhatsAppLead) -> dict:
    return {
        "id": lead.id,
        "lead_key": lead.sheet_key,
        "origin": lead.origin,          # sheet | added | imported
        "country": lead.country,
        "phone": lead.phone,
        "phone_display": f"+{lead.phone}",
        "event_date": lead.event_date.isoformat() if lead.event_date else None,
        "event_date_text": lead.event_date_text,
        "submitted_at": _utc(lead.submitted_at),
        "first_seen_at": _utc(lead.first_seen_at),
        "sheet_row": lead.sheet_row,
    }


@dataclass
class WhatsAppFilters:
    common: lead_filters.CommonFilters
    country: str | None = None


def whatsapp_filters(
    common: Annotated[lead_filters.CommonFilters,
                      Depends(common_filter_params(lead_filters.SHEET_ORIGINS, "International leads"))],
    country: str | None = Query(None, max_length=100),
) -> WhatsAppFilters:
    return WhatsAppFilters(common, (country or "").strip() or None)


Filters = Annotated[WhatsAppFilters, Depends(whatsapp_filters)]


@router.get("")
def list_whatsapp_leads(
    viewer: LeadViewer,
    db: DbSession,
    f: Filters,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    sort: Literal["submitted_at", "event_date", "country", "phone", "first_seen_at"] = "submitted_at",
    order: Literal["asc", "desc"] = "desc",
):
    filters = build_filters(db, viewer, f)
    total = db.exec(select(func.count()).select_from(WhatsAppLead).where(*filters)).one()
    rows = db.exec(
        select(WhatsAppLead).where(*filters).order_by(*ordering(sort, order))
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {"items": attach_crm(db, [_out(r) for r in rows]), "total": total, "page": page, "page_size": page_size}


# ── Shared by the list, the stats and the export (GET /crm-leads/whatsapp/export) ──
def build_filters(db, viewer, f: WhatsAppFilters) -> list:
    """SQL conditions for the leads the viewer may see that match the filters. Archived and
    removed rows (is_deleted) are never listed, counted or exported."""
    c = f.common
    filters = [WhatsAppLead.is_deleted == False]  # noqa: E712
    keys = lead_filters.allowed_keys(db, "whatsapp", viewer, c)
    if keys is not None:
        filters.append(col(WhatsAppLead.sheet_key).in_(keys) if keys else false())
    if c.period is not None:
        # Arrived in the period (IST days): submitted_at is stored in UTC
        start, end = c.period.utc_bounds()
        filters += [WhatsAppLead.submitted_at >= start, WhatsAppLead.submitted_at < end]
    if c.event_from is not None:
        filters.append(WhatsAppLead.event_date >= c.event_from)
    if c.event_to is not None:
        filters.append(WhatsAppLead.event_date <= c.event_to)
    if c.origins:
        filters.append(col(WhatsAppLead.origin).in_(c.origins))
    if f.country:
        filters.append(WhatsAppLead.country == f.country)
    if c.search:
        # No name or email in this sheet: phone digits, plus country and the event date text
        filters.append(lead_filters.search_condition(
            c.search, [WhatsAppLead.country, WhatsAppLead.event_date_text], WhatsAppLead.phone))
    return filters


def ordering(sort: str, order: str) -> list:
    # Rows without a value go last in both directions; id keeps the order stable
    column = SORTABLE[sort]
    return [column.is_(None), column.desc() if order == "desc" else column.asc(), WhatsAppLead.id.desc()]


def attach_crm(db, items: list[dict]) -> list[dict]:
    """Add the CRM details (vendor, interest, quote, converted) from the sidecar table."""
    return lead_crm.attach(db, "whatsapp", items, key=lambda i: i["lead_key"], location=lambda i: i["country"])


@router.get("/stats")
def whatsapp_lead_stats(viewer: LeadViewer, db: DbSession, f: Filters):
    """Numbers over the leads the list shows for the same filters (vendors: their own)."""
    active = and_(*build_filters(db, viewer, f))
    now = datetime.now(timezone.utc)

    total = db.exec(select(func.count()).select_from(WhatsAppLead).where(active)).one()
    last_7_days = db.exec(
        select(func.count()).select_from(WhatsAppLead).where(active, WhatsAppLead.submitted_at >= now - timedelta(days=7))
    ).one()
    upcoming_events = db.exec(
        select(func.count()).select_from(WhatsAppLead).where(active, WhatsAppLead.event_date >= date.today())
    ).one()
    top_countries = db.exec(
        select(WhatsAppLead.country, func.count().label("n"))
        .where(active, WhatsAppLead.country.is_not(None))
        .group_by(WhatsAppLead.country)
        .order_by(func.count().desc())
        .limit(5)
    ).all()

    return {
        "total": total,
        "last_7_days": last_7_days,
        "upcoming_events": upcoming_events,
        "top_countries": [{"country": c, "count": n} for c, n in top_countries],
    }
