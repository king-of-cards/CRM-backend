"""WhatsApp leads copied from the Google Sheet (admin only). Reads from MySQL only: fast.

GET /whatsapp-leads         list with search, country filter, sort and pages
GET /whatsapp-leads/stats   numbers for dashboard cards
"""
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import func, or_
from sqlmodel import col, select

from app.api.deps import AdminUser, DbSession
from app.models.whatsapp_lead import WhatsAppLead
from app.services import lead_crm

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
        "country": lead.country,
        "phone": lead.phone,
        "phone_display": f"+{lead.phone}",
        "event_date": lead.event_date.isoformat() if lead.event_date else None,
        "event_date_text": lead.event_date_text,
        "submitted_at": _utc(lead.submitted_at),
        "first_seen_at": _utc(lead.first_seen_at),
        "sheet_row": lead.sheet_row,
    }


@router.get("")
def list_whatsapp_leads(
    _: AdminUser,
    db: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    search: str | None = Query(None, max_length=100),
    country: str | None = Query(None, max_length=100),
    sort: Literal["submitted_at", "event_date", "country", "phone", "first_seen_at"] = "submitted_at",
    order: Literal["asc", "desc"] = "desc",
):
    filters = [WhatsAppLead.is_deleted == False]  # noqa: E712
    if country:
        filters.append(WhatsAppLead.country == country)
    if search and search.strip():
        like = f"%{search.strip()}%"
        filters.append(
            or_(
                col(WhatsAppLead.country).like(like),
                col(WhatsAppLead.phone).like(f"%{''.join(ch for ch in search if ch.isdigit()) or search.strip()}%"),
                col(WhatsAppLead.event_date_text).like(like),
            )
        )

    total = db.exec(select(func.count()).select_from(WhatsAppLead).where(*filters)).one()

    column = SORTABLE[sort]
    # Rows without a value go last in both directions; id keeps the order stable
    ordering = [column.is_(None), column.desc() if order == "desc" else column.asc(), WhatsAppLead.id.desc()]
    rows = db.exec(
        select(WhatsAppLead).where(*filters).order_by(*ordering).offset((page - 1) * page_size).limit(page_size)
    ).all()

    # Add the CRM details (vendor, interest, quote, converted) from the sidecar table
    items = lead_crm.attach(db, "whatsapp", [_out(r) for r in rows], key=lambda i: i["lead_key"], location=lambda i: i["country"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/stats")
def whatsapp_lead_stats(_: AdminUser, db: DbSession):
    active = WhatsAppLead.is_deleted == False  # noqa: E712
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