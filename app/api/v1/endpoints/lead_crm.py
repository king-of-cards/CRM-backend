"""CRM details on any lead (admin only): assigned vendor, interest, converted, quote, notes.

GET /leads/{source}/{lead_key}/crm   current details
PUT /leads/{source}/{lead_key}/crm   save details (only the fields you send are changed)

source: spin | whatsapp | wedding | wedding_planner
lead_key: the "crm.lead_key" value that every lead row in the lists carries.
The leads themselves are never modified: these details live in crm_lead_meta (sidecar).
"""
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from app.api.deps import AdminUser, DbSession
from app.core import response_cache
from app.models.lead_meta import LeadMeta
from app.models.vendor import Vendor
from app.services import lead_crm

router = APIRouter(prefix="/leads", tags=["lead crm"])

Source = Literal["spin", "whatsapp", "wedding", "wedding_planner"]


class LeadCrmUpdate(BaseModel):
    # Send only what changes. vendor_id: a vendor's id, or null to go back to automatic.
    vendor_id: int | None = None
    interest: Literal["interested", "follow_up", "not_interested"] | None = None
    converted: bool | None = None
    quoted_date: date | None = None
    quoted_amount: int | None = Field(default=None, ge=0, le=100_000_000)
    notes: str | None = Field(default=None, max_length=5000)

    # Optional context so the answer can include the automatic (sheet / area) vendor
    location: str | None = Field(default=None, max_length=300)
    pincode: str | None = Field(default=None, max_length=20)
    sheet_vendor: str | None = Field(default=None, max_length=100)


EDITABLE = ("vendor_id", "interest", "converted", "quoted_date", "quoted_amount", "notes")


def _answer(db, source: str, lead_key: str, meta: LeadMeta | None, body: LeadCrmUpdate | None = None) -> dict:
    vendors = lead_crm.active_vendors(db)
    by_id = {v.id: v for v in vendors}
    return lead_crm.meta_out(
        meta,
        lead_key,
        by_id,
        lead_crm.vendor_by_name(vendors, body.sheet_vendor if body else None),
        lead_crm.suggest_vendor(vendors, body.location if body else None, body.pincode if body else None),
    )


def _find(db, source: str, lead_key: str) -> LeadMeta | None:
    return db.exec(select(LeadMeta).where(LeadMeta.source == source, LeadMeta.lead_key == lead_key)).first()


@router.get("/{source}/{lead_key}/crm")
def get_lead_crm(source: Source, lead_key: str, _: AdminUser, db: DbSession):
    return _answer(db, source, lead_key, _find(db, source, lead_key))


@router.put("/{source}/{lead_key}/crm")
def save_lead_crm(source: Source, lead_key: str, body: LeadCrmUpdate, user: AdminUser, db: DbSession):
    if len(lead_key) > 100:
        raise HTTPException(400, "Lead key is too long")
    changes = {f: getattr(body, f) for f in EDITABLE if f in body.model_fields_set}

    if changes.get("vendor_id") is not None:
        vendor = db.get(Vendor, changes["vendor_id"])
        if vendor is None or not vendor.is_active:
            raise HTTPException(404, "Vendor not found")
    if "notes" in changes and changes["notes"] is not None:
        changes["notes"] = changes["notes"].strip() or None

    now = datetime.now(timezone.utc)
    meta = _find(db, source, lead_key)
    if meta is None:
        meta = LeadMeta(source=source, lead_key=lead_key, updated_at=now)
    if "converted" in changes:
        changes["converted"] = bool(changes["converted"])
        if changes["converted"] and not meta.converted:
            meta.converted_at = now
        elif not changes["converted"]:
            meta.converted_at = None
    for field, value in changes.items():
        setattr(meta, field, value)
    meta.updated_at = now
    meta.updated_by = user.id
    db.add(meta)
    db.commit()
    db.refresh(meta)

    response_cache.clear("vendors")  # vendor numbers changed
    return _answer(db, source, lead_key, meta, body)