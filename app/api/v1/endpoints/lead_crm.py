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
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlmodel import select

from app.api.deps import AdminUser, DbSession, LeadViewer
from app.core import response_cache
from app.models.lead_meta import LeadMeta
from app.models.lead_origin import CRM_KEY_PREFIX
from app.models.vendor import Vendor
from app.services import access, hot_leads, lead_crm, notifications, lead_activity
from app.crud.user import user_names
from app.services.crm_leads import service as crm_leads

router = APIRouter(prefix="/leads", tags=["lead crm"])
logger = logging.getLogger(__name__)

Source = Literal["spin", "whatsapp", "wedding", "wedding_planner"]
MAX_AMOUNT = 100_000_000          # rupees, for quoted_amount and conversion_amount


class LeadCrmUpdate(BaseModel):
    # Send only what changes. vendor_id: a vendor's id, or null to go back to automatic.
    vendor_id: int | None = None
    # Admins: the lead's manual vendors, all at once ([] = back to automatic). Each must be active.
    vendor_ids: list[int] | None = Field(default=None, max_length=20)
    # Admins: "unassigned" = stays unassigned (no automatic rule ever assigns it); "automatic" = the
    # automatic rules again. Not together with vendor_id.
    assigned_to: Literal["unassigned", "automatic"] | None = None
    # The same 7 values as Vendor -> KOC; null = Pending
    update_status: Literal["converted", "not_converted", "purchased_elsewhere", "purchased_with_us",
                           "will_call_back", "interested", "not_interested"] | None = None
    quoted_date: date | None = None
    quoted_amount: int | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    conversion_amount: int | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    # Payout (commission, admins only): % of the conversion amount, 0-100 with 2 decimals. The server
    # works out payout_amount itself (lead_crm.apply_payout); a payout_amount sent is accepted and replaced.
    payout_percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    payout_amount: int | None = Field(default=None, ge=0, le=MAX_AMOUNT)
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

    @model_validator(mode="after")
    def _one_assignment(self):
        if len({"vendor_id", "vendor_ids", "assigned_to"} & self.model_fields_set) > 1:
            raise ValueError("Send only one of vendor_id, vendor_ids and assigned_to")
        return self


EDITABLE = ("vendor_id", "vendor_ids", "update_status", "quoted_date", "quoted_amount", "conversion_amount",
            "payout_percent", "payout_amount", "notes", "follow_up_date")
COPY_FIELDS = ("update_status", "quoted_date", "quoted_amount", "conversion_amount", "payout_percent", "payout_amount",
               "notes", "follow_up_date")
PAYOUT_FIELDS = {"payout_percent", "payout_amount"}          # admins only


def _require_lead(db, viewer, source: str, lead_key: str) -> None:
    if len(lead_key) > 100:
        raise HTTPException(400, "Lead key is too long")
    # crm-… keys belong to leads added/imported in the CRM: they must exist (archived is fine).
    # Workflow ids and sheet keys are accepted as before.
    if lead_key.startswith(CRM_KEY_PREFIX) and crm_leads.find_crm_lead(db, source, lead_key) is None:
        raise HTTPException(404, "Lead not found")
    if not access.can_see(db, viewer, source, lead_key):
        raise HTTPException(404, "Lead not found")


def _entries(db, source: str, lead_key: str) -> list[tuple[int, str]]:
    """The lead's vendors ([(vendor id, how)]; the same map visibility uses)."""
    return access.vendor_entries(db, source).get(str(lead_key), [])


def _copy_vendor(db, viewer, source: str, lead_key: str, vendor: int | None) -> int:
    """Whose copy a request works on: a vendor login's own. Admins: the one asked for (?vendor=, one of
    the lead's vendors; 0 = the vendor-less row); without ?vendor= the lead's only vendor, or the
    vendor-less row when it has none (a lead with several vendors needs ?vendor=: 422)."""
    if not viewer.sees_all:
        return viewer.vendor_id
    ids = [vid for vid, _ in _entries(db, source, lead_key)]
    if vendor is None:
        if len(ids) > 1:
            raise HTTPException(422, "This lead has several vendors: choose whose copy with ?vendor=<vendor id> "
                                     "(0 = the lead's own row).")
        return ids[0] if ids else 0
    if vendor != 0 and vendor not in ids:
        raise HTTPException(422, "That vendor doesn't have this lead.")
    return vendor


def _answer(db, viewer, source: str, lead_key: str) -> dict:
    """The lead's crm object as this viewer may see it (every copy for admins and view-only, the
    vendor's own copy for vendor logins)."""
    crm = lead_crm.crm_for_key(db, source, lead_key, _entries(db, source, lead_key))
    return lead_crm.for_viewer({"crm": crm}, viewer)["crm"]


def _labels(db, source: str, lead_key: str) -> str:
    access.forget()
    entries = _entries(db, source, lead_key)
    if not entries:
        base = lead_crm.copy_row(db, source, lead_key, 0)
        return "Unassigned" if base is not None and base.auto_assign is False else "Nobody"
    by_id = lead_crm.vendors_by_id(db)
    return lead_crm.assigned_label({"vendors": [{"vendor": {"name": by_id[vid].name if vid in by_id else str(vid)},
                                                 "vendor_source": how} for vid, how in entries]})


def _forget() -> None:
    response_cache.clear("vendors")  # vendor numbers changed
    response_cache.clear("analytics")
    access.forget()                  # who may see the lead can have changed


def _notify(db, viewer, source: str, lead_key: str, vendor_ids) -> None:
    for vendor_id in vendor_ids:
        try:
            notifications.notify_vendor_assigned(db, source=source, lead_key=lead_key, vendor_id=vendor_id,
                                                 actor_user_id=viewer.user.id, actor_name=viewer.user.name)
        except Exception:
            db.rollback()
            logger.exception("Could not create vendor assignment notification")


@router.get("/{source}/{lead_key}/crm")
def get_lead_crm(source: Source, lead_key: str, viewer: LeadViewer, db: DbSession):
    """The lead's CRM details: for admins and view-only logins the overall view plus one entry per
    vendor ("vendors"); for a vendor login its own copy."""
    if not access.can_see(db, viewer, source, lead_key):
        raise HTTPException(404, "Lead not found")
    return _answer(db, viewer, source, lead_key)


@router.put("/{source}/{lead_key}/crm")
def save_lead_crm(source: Source, lead_key: str, body: LeadCrmUpdate, viewer: LeadViewer, db: DbSession,
                  vendor: int | None = Query(None, description="Admins: whose copy to change (one of the lead's "
                                                               "vendors, 0 = the lead's own row); default: its "
                                                               "only vendor, or its own row when it has none")):
    """Who may change what:
      vendor_id (one manual vendor; null = automatic), vendor_ids (several manual vendors; [] =
      automatic), assigned_to ("unassigned" | "automatic")                                      admins only
      update_status, quoted_amount, quoted_date, conversion_amount, notes, follow_up_date          one copy:
        a vendor login its own; admins the one in ?vendor= (default: the lead's only vendor's, or the
        lead's own row when it has no vendor; a lead with several vendors needs ?vendor=)
    A vendor user can't see other vendors' or unassigned leads: those answer 404."""
    _require_lead(db, viewer, source, lead_key)
    changes = {f: getattr(body, f) for f in EDITABLE if f in body.model_fields_set}
    assigned_to = body.assigned_to if "assigned_to" in body.model_fields_set else None
    if "vendor_id" in changes:                     # one manual vendor = a list of one
        changes["vendor_ids"] = [changes.pop("vendor_id")] if changes["vendor_id"] is not None else []
    if not viewer.is_admin and ("vendor_ids" in changes or assigned_to is not None):
        raise HTTPException(403, "Only an admin can assign leads.")
    if not viewer.is_admin and PAYOUT_FIELDS & set(changes):
        raise HTTPException(403, "Only an admin can set the payout.")
    if "vendor_ids" in changes:
        changes["vendor_ids"] = list(dict.fromkeys(changes["vendor_ids"] or []))
        for vendor_id in changes["vendor_ids"]:
            _active_vendor(db, vendor_id)
    if "notes" in changes and changes["notes"] is not None:
        changes["notes"] = changes["notes"].strip() or None

    before_ids = {vid for vid, _ in _entries(db, source, lead_key)}
    copy_changes = {f: v for f, v in changes.items() if f in COPY_FIELDS}
    copy_id = _copy_vendor(db, viewer, source, lead_key, vendor) if copy_changes else None
    old_label = _labels(db, source, lead_key) if ("vendor_ids" in changes or assigned_to is not None) else None
    old_values = {}
    if copy_changes:
        meta = lead_crm.copy_row(db, source, lead_key, copy_id, create=True)
        old_values = {f: getattr(meta, f) for f in copy_changes}
        old_payout = meta.payout_amount
        for field, value in copy_changes.items():
            setattr(meta, field, value)
        # The payout in rupees always comes from the conversion amount and the % (also when only the
        # conversion amount changed); a change to it is saved and logged like the others
        if PAYOUT_FIELDS & set(copy_changes) or "conversion_amount" in copy_changes:
            lead_crm.apply_payout(meta)
            if meta.payout_amount != old_payout or "payout_amount" in copy_changes:
                copy_changes["payout_amount"] = meta.payout_amount
                old_values["payout_amount"] = old_payout
        meta.updated_at = datetime.now(timezone.utc)
        meta.updated_by = viewer.user.id
        db.add(meta)
    if "vendor_ids" in changes:
        # The lead's manual vendors (they replace the automatic one; partner rules still add theirs);
        # [] / null = back to automatic (also for unassigned leads)
        lead_crm.set_manual(db, source, lead_key, changes["vendor_ids"], viewer.user.id, automatic=True)
    if assigned_to is not None:
        # "unassigned": nobody (no rule assigns it); "automatic": the rules again, no manual choices
        lead_crm.set_manual(db, source, lead_key, [], viewer.user.id, automatic=assigned_to == "automatic")
    db.commit()
    for field, value in copy_changes.items():
        if old_values[field] != value:
            lead_activity.log_changes(db, source=source, lead_key=lead_key, changes={field: (old_values[field], value)},
                                      actor_user_id=viewer.user.id, actor_name=viewer.user.name,
                                      actor_role=viewer.user.role, vendor_id=copy_id)
    _forget()
    if old_label is not None:
        new_label = _labels(db, source, lead_key)
        lead_activity.log_changes(db, source=source, lead_key=lead_key,
            changes={"assigned_to" if assigned_to is not None else "vendor_id": (old_label, new_label)},
            actor_user_id=viewer.user.id, actor_name=viewer.user.name, actor_role=viewer.user.role, action="assigned")
        _notify(db, viewer, source, lead_key, {vid for vid, _ in _entries(db, source, lead_key)} - before_ids)
    return _answer(db, viewer, source, lead_key)


def _active_vendor(db, vendor_id: int) -> Vendor:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(404, "Vendor not found")
    if not vendor.is_active:
        raise HTTPException(422, "This vendor is inactive; choose an active vendor.")
    return vendor


@router.post("/{source}/{lead_key}/vendors/{vendor_id}")
def add_lead_vendor(source: Source, lead_key: str, vendor_id: int, viewer: AdminUser, db: DbSession):
    """Admins: give the lead to one more vendor (an active one). The lead's current vendors keep it;
    an "Unassigned" lead becomes assigned again (the partner rules apply too)."""
    from app.services.access import viewer_for

    view = viewer_for(db, viewer)
    _require_lead(db, view, source, lead_key)
    _active_vendor(db, vendor_id)
    before = _labels(db, source, lead_key)
    before_ids = {vid for vid, _ in _entries(db, source, lead_key)}
    lead_crm.add_vendor(db, source, lead_key, vendor_id, _entries(db, source, lead_key), viewer.id)
    db.commit()
    _forget()
    lead_activity.log_changes(db, source=source, lead_key=lead_key, changes={"vendors": (before, _labels(db, source, lead_key))},
                              actor_user_id=viewer.id, actor_name=viewer.name, actor_role=viewer.role,
                              action="assigned", vendor_id=vendor_id)
    _notify(db, view, source, lead_key, {vid for vid, _ in _entries(db, source, lead_key)} - before_ids)
    return _answer(db, view, source, lead_key)


@router.delete("/{source}/{lead_key}/vendors/{vendor_id}")
def remove_lead_vendor(source: Source, lead_key: str, vendor_id: int, viewer: AdminUser, db: DbSession):
    """Admins: take the lead away from one vendor. It sticks: no rule gives it back. The vendor's copy
    is kept (and comes back if the vendor is added again)."""
    from app.services.access import viewer_for

    view = viewer_for(db, viewer)
    _require_lead(db, view, source, lead_key)
    entries = _entries(db, source, lead_key)
    if vendor_id not in [vid for vid, _ in entries]:
        raise HTTPException(404, "That vendor doesn't have this lead.")
    before = _labels(db, source, lead_key)
    lead_crm.remove_vendor(db, source, lead_key, vendor_id, entries, viewer.id)
    db.commit()
    _forget()
    lead_activity.log_changes(db, source=source, lead_key=lead_key, changes={"vendors": (before, _labels(db, source, lead_key))},
                              actor_user_id=viewer.id, actor_name=viewer.name, actor_role=viewer.role,
                              action="unassigned", vendor_id=vendor_id)
    return _answer(db, view, source, lead_key)


def _set_hot(db, viewer, source: str, lead_key: str, vendor: int | None, hot: bool) -> dict:
    if not access.can_see(db, viewer, source, lead_key) or not hot_leads.lead_exists(db, source, lead_key):
        raise HTTPException(404, "Lead not found")
    copy_id = _copy_vendor(db, viewer, source, lead_key, vendor)
    was_hot = hot_leads.is_hot(db, source, lead_key, copy_id)
    result = hot_leads.set_hot(db, source, lead_key, viewer.user.id, hot, copy_id)
    if was_hot != hot:
        lead_activity.log_action(db, source=source, lead_key=lead_key, action="hot_on" if hot else "hot_off",
            field="is_hot", old=was_hot, new=hot, actor_user_id=viewer.user.id, actor_name=viewer.user.name,
            actor_role=viewer.user.role, vendor_id=copy_id)
    return result


@router.delete("/{source}/{lead_key}/hot")
def unmark_lead_hot(source: Source, lead_key: str, viewer: LeadViewer, db: DbSession,
                    vendor: int | None = Query(None, description="Admins: whose copy (none = the vendor-less row)")):
    return _set_hot(db, viewer, source, lead_key, vendor, False)


@router.post("/{source}/{lead_key}/hot")
def mark_lead_hot(source: Source, lead_key: str, viewer: LeadViewer, db: DbSession,
                  vendor: int | None = Query(None, description="Admins: whose copy (none = the vendor-less row)")):
    """The hot star of one copy: a vendor login's own; admins the one in ?vendor= (or the vendor-less row)."""
    return _set_hot(db, viewer, source, lead_key, vendor, True)


@router.get("/{source}/{lead_key}/activity")
def get_activity(source: Source, lead_key: str, viewer: LeadViewer, db: DbSession,
                 page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    """Admins and view-only: every entry (each with the vendor copy it concerns, vendor_id). Vendor
    logins: the CRM fields of their own copy and of the lead itself."""
    if lead_key not in access.assignments(db, source) or not access.can_see(db, viewer, source, lead_key):
        raise HTTPException(404, "Lead not found")
    allowed = None if viewer.sees_all else lead_activity.VENDOR_CRM_FIELDS
    return lead_activity.list_activity(db, source=source, lead_key=lead_key, page=page, page_size=page_size,
                                       allowed_fields=allowed, vendor_id=None if viewer.sees_all else viewer.vendor_id)
