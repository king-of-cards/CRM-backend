"""CRM details on any lead: assigned vendor, update, quote, conversion amount, notes.
Admins and vendor users; see save_lead_crm for who may change what.

GET /leads/{source}/{lead_key}/crm   current details
PUT /leads/{source}/{lead_key}/crm   save details (only the fields you send are changed)

source: spin | whatsapp | wedding | wedding_planner
lead_key: the "crm.lead_key" value that every lead row in the lists carries.
The leads themselves are never modified: these details live in crm_lead_meta (sidecar).
"""
import logging
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlmodel import select

from app.api.deps import DbSession, LeadViewer
from app.core import response_cache
from app.models.lead_meta import LeadMeta
from app.models.lead_origin import CRM_KEY_PREFIX
from app.models.vendor import Vendor
from app.services import access, hot_leads, lead_crm, notifications, lead_activity
from app.services.crm_leads import service as crm_leads

router = APIRouter(prefix="/leads", tags=["lead crm"])
logger = logging.getLogger(__name__)

Source = Literal["spin", "whatsapp", "wedding", "wedding_planner"]
MAX_AMOUNT = 100_000_000          # rupees, for quoted_amount and conversion_amount


class LeadCrmUpdate(BaseModel):
    # Send only what changes. vendor_id: a vendor's id, or null to go back to automatic.
    vendor_id: int | None = None
    # The same 7 values as Vendor -> KOC; null = Pending
    update_status: Literal["converted", "not_converted", "purchased_elsewhere", "purchased_with_us",
                           "will_call_back", "interested", "not_interested"] | None = None
    quoted_date: date | None = None
    quoted_amount: int | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    conversion_amount: int | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    notes: str | None = Field(default=None, max_length=5000)
    follow_up_date: date | None = None

    # Optional context so the answer can include the automatic (sheet / area) vendor
    location: str | None = Field(default=None, max_length=300)
    pincode: str | None = Field(default=None, max_length=20)
    sheet_vendor: str | None = Field(default=None, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def _no_old_fields(cls, data):
        """interest / converted were replaced by update_status: reject them (422), so an old
        frontend fails visibly instead of saving nothing."""
        if isinstance(data, dict):
            old = [f for f in ("interest", "converted") if f in data]
            if old:
                raise ValueError(f"{' and '.join(old)} {'was' if len(old) == 1 else 'were'} replaced by "
                                 "update_status (converted, not_converted, purchased_elsewhere, purchased_with_us, "
                                 "will_call_back, interested, not_interested or null)")
        return data


EDITABLE = ("vendor_id", "update_status", "quoted_date", "quoted_amount", "conversion_amount", "notes", "follow_up_date")


def _is_gift_lead(db, source: str, lead_key: str) -> bool:
    """A spin gift lead (it goes to the gifts vendor): CRM leads from their details, workflow rows
    from the workflow index (False when it can't be read)."""
    if source != "spin":
        return False
    if lead_key.startswith(CRM_KEY_PREFIX):
        from app.models.crm_spin_lead import CrmSpinLead
        from app.services import crm_spin_leads

        number = lead_key[len(CRM_KEY_PREFIX):]
        lead = db.get(CrmSpinLead, int(number)) if number.isdigit() else None
        return lead is not None and crm_spin_leads.is_gift(lead)
    from app.services import also_in

    row = also_in.workflow_row(lead_key)
    return bool(row and row[2])


def _answer(db, source: str, lead_key: str, meta: LeadMeta | None, body: LeadCrmUpdate | None = None) -> dict:
    vendors = lead_crm.active_vendors(db)
    by_id = {v.id: v for v in vendors}
    rules, default = lead_crm.lead_rules(source, vendors, gift=_is_gift_lead(db, source, lead_key))
    return lead_crm.meta_out(
        meta,
        lead_key,
        by_id,
        lead_crm.vendor_by_name(vendors, body.sheet_vendor if body else None) if rules else None,
        lead_crm.suggest_vendor(vendors, body.location if body else None,
                                body.pincode if body else None) if rules else None,
        default_vendor=default,
    )


def _find(db, source: str, lead_key: str) -> LeadMeta | None:
    return db.exec(select(LeadMeta).where(LeadMeta.source == source, LeadMeta.lead_key == lead_key)).first()


@router.get("/{source}/{lead_key}/crm")
def get_lead_crm(source: Source, lead_key: str, viewer: LeadViewer, db: DbSession):
    if not access.can_see(db, viewer, source, lead_key):
        raise HTTPException(404, "Lead not found")
    answer = _answer(db, source, lead_key, _find(db, source, lead_key))
    answer["is_hot"] = hot_leads.is_hot(db, source, lead_key)
    return answer


@router.put("/{source}/{lead_key}/crm")
def save_lead_crm(source: Source, lead_key: str, body: LeadCrmUpdate, viewer: LeadViewer, db: DbSession):
    """Who may change what:
      vendor_id                                                               admins only
      update_status, quoted_amount, quoted_date, conversion_amount, notes     admins and the assigned vendor's users
    A vendor user can't see other vendors' or unassigned leads: those answer 404.
    updated_by records who saved."""
    if len(lead_key) > 100:
        raise HTTPException(400, "Lead key is too long")
    # crm-… keys belong to leads added/imported in the CRM: they must exist (archived is fine).
    # Workflow ids and sheet keys are accepted as before.
    if lead_key.startswith(CRM_KEY_PREFIX) and crm_leads.find_crm_lead(db, source, lead_key) is None:
        raise HTTPException(404, "Lead not found")
    if not access.can_see(db, viewer, source, lead_key):
        raise HTTPException(404, "Lead not found")
    changes = {f: getattr(body, f) for f in EDITABLE if f in body.model_fields_set}
    if not viewer.is_admin and "vendor_id" in changes:
        raise HTTPException(403, "Only an admin can assign leads.")

    if changes.get("vendor_id") is not None:
        vendor = db.get(Vendor, changes["vendor_id"])
        if vendor is None or not vendor.is_active:
            raise HTTPException(404, "Vendor not found")
    if "notes" in changes and changes["notes"] is not None:
        changes["notes"] = changes["notes"].strip() or None

    now = datetime.now(timezone.utc)
    # Read before mutating so notifications are tied only to a real new assignment.
    meta = _find(db, source, lead_key)
    previous_vendor_id = meta.vendor_id if meta else None
    if meta is None:
        meta = LeadMeta(source=source, lead_key=lead_key, updated_at=now)
    old_values = {field: getattr(meta, field) for field in changes}
    for field, value in changes.items():
        setattr(meta, field, value)
    if "vendor_id" in changes:
        # A vendor = manual assignment; null = back to automatic (also for leads uploaded unassigned)
        meta.auto_assign = True
    meta.updated_at = now
    meta.updated_by = viewer.user.id
    db.add(meta)
    db.commit()
    db.refresh(meta)
    for field, value in changes.items():
        if old_values[field] == value:
            continue
        lead_activity.log_changes(db, source=source, lead_key=lead_key,
            changes={field: (old_values[field], value)}, actor_user_id=viewer.user.id,
            actor_name=viewer.user.name, actor_role=viewer.user.role,
            action="assigned" if field == "vendor_id" else "field_changed")

    response_cache.clear("vendors")  # vendor numbers changed
    response_cache.clear("analytics")
    access.forget()                  # who may see the lead can have changed
    answer = _answer(db, source, lead_key, meta, body)
    answer["is_hot"] = hot_leads.is_hot(db, source, lead_key)
    if ("vendor_id" in changes and changes["vendor_id"] is not None
            and changes["vendor_id"] != previous_vendor_id):
        try:
            notifications.notify_vendor_assigned(
                db, source=source, lead_key=lead_key, vendor_id=changes["vendor_id"],
                actor_user_id=viewer.user.id, actor_name=viewer.user.name,
            )
        except Exception:
            db.rollback()
            logger.exception("Could not create vendor assignment notification")
    return answer


@router.delete("/{source}/{lead_key}/hot")
def unmark_lead_hot(source: Source, lead_key: str, viewer: LeadViewer, db: DbSession):
    if not access.can_see(db, viewer, source, lead_key) or not hot_leads.lead_exists(db, source, lead_key):
        raise HTTPException(404, "Lead not found")
    was_hot = hot_leads.is_hot(db, source, lead_key)
    result = hot_leads.set_hot(db, source, lead_key, viewer.user.id, False)
    if was_hot:
        lead_activity.log_action(db, source=source, lead_key=lead_key, action="hot_off", field="is_hot",
            old=True, new=False, actor_user_id=viewer.user.id, actor_name=viewer.user.name, actor_role=viewer.user.role)
    return result


@router.post("/{source}/{lead_key}/hot")
def mark_lead_hot(source: Source, lead_key: str, viewer: LeadViewer, db: DbSession):
    if not access.can_see(db, viewer, source, lead_key) or not hot_leads.lead_exists(db, source, lead_key):
        raise HTTPException(404, "Lead not found")
    was_hot = hot_leads.is_hot(db, source, lead_key)
    result = hot_leads.set_hot(db, source, lead_key, viewer.user.id, True)
    if not was_hot:
        lead_activity.log_action(db, source=source, lead_key=lead_key, action="hot_on", field="is_hot",
            old=False, new=True, actor_user_id=viewer.user.id, actor_name=viewer.user.name, actor_role=viewer.user.role)
    return result


@router.get("/{source}/{lead_key}/activity")
def get_activity(source: Source, lead_key: str, viewer: LeadViewer, db: DbSession,
                 page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    if lead_key not in access.assignments(db, source) or not access.can_see(db, viewer, source, lead_key):
        raise HTTPException(404, "Lead not found")
    allowed = None if viewer.sees_all else lead_activity.VENDOR_CRM_FIELDS
    return lead_activity.list_activity(db, source=source, lead_key=lead_key, page=page,
                                       page_size=page_size, allowed_fields=allowed)
