import logging
from collections import Counter
from datetime import date
from fastapi import APIRouter, HTTPException, Query
from sqlmodel import Session, select

from app.api.deps import DbSession, NotDhruvaUser
from app.models.dhruva import DhruvaLead
from app.models.lead_meta import LeadMeta
from app.models.vendor import Vendor
from app.models.vendor_to_koc import VendorToKocLead
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.services import access, periods
from app.services.access import Viewer
from app.services.lead_sources import LEAD_SOURCES, parse_sources

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reminders", tags=["follow-up reminders"])
SOURCES = LEAD_SOURCES


def _details_for_crm_source(db: Session, source: str, keys: set[str]) -> dict[str, tuple[str | None, str | None]]:
    if not keys:
        return {}
    if source == "whatsapp":
        rows = db.exec(select(WhatsAppLead.sheet_key, WhatsAppLead.phone).where(
            WhatsAppLead.sheet_key.in_(keys), WhatsAppLead.is_deleted == False)).all()  # noqa: E712
        return {str(k): (None, phone) for k, phone in rows}
    if source == "wedding":
        rows = db.exec(select(WeddingLead.sheet_key, WeddingLead.name, WeddingLead.phone).where(
            WeddingLead.sheet_key.in_(keys), WeddingLead.is_deleted == False)).all()  # noqa: E712
        return {str(k): (name, phone) for k, name, phone in rows}
    if source == "wedding_planner":
        rows = db.exec(select(WeddingPlan.plan_id, WeddingPlan.name, WeddingPlan.phone).where(
            WeddingPlan.plan_id.in_(keys), WeddingPlan.is_deleted == False)).all()  # noqa: E712
        return {str(k): (name, phone) for k, name, phone in rows}
    from app.models.crm_spin_lead import CrmSpinLead
    from app.models.lead_origin import CRM_KEY_PREFIX
    local = {k for k in keys if k.startswith(CRM_KEY_PREFIX)}
    output = {}
    ids = [int(k[len(CRM_KEY_PREFIX):]) for k in local if k[len(CRM_KEY_PREFIX):].isdigit()]
    if ids:
        for row in db.exec(select(CrmSpinLead).where(CrmSpinLead.id.in_(ids), CrmSpinLead.is_deleted == False)):  # noqa: E712
            output[row.lead_key] = (row.name, row.phone)
    return output


def _workflow_spin_details(keys: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """Read workflow names and phones in capped batches through the existing Spin list query."""
    if not keys:
        return {}
    from app.api.v1.endpoints import spin_leads as sl
    output = {}
    for start in range(0, len(keys), 200):
        batch = keys[start:start + 200]
        try:
            result = sl.list_spin_leads(None, 1, len(batch), filters={"ids_in": batch}, offset=0, limit=len(batch))
        except Exception:
            logger.exception("Could not load workflow Spin reminder details")
            continue
        pk_name = list(sl._get_table().primary_key.columns)[0].name
        name_field = next((field for field in result["columns"] if field["name"].lower().split(".")[-1] in
                           {"name", "fullname", "customername", "clientname"}), None)
        phone_field = next((field for field in result["columns"] if field["name"].lower().split(".")[-1] in
                            {"phone", "mobile", "phonenumber", "mobilenumber", "contactnumber"}), None)
        if not name_field or not phone_field:
            continue
        for item in result["items"]:
            raw_key = item.get(pk_name)
            key = str(raw_key) if raw_key is not None else ""
            if key:
                output[key] = (item.get(name_field["name"]), item.get(phone_field["name"]))
    return output


def _reminder_rows(db: Session, viewer: Viewer | None, dhruva_only: bool = False) -> list[dict]:
    today = periods.today_ist()
    rows: list[dict] = []
    for source in ("spin", "whatsapp", "wedding", "wedding_planner"):
        visible = access.visible_keys(db, viewer, source) if viewer is not None else None
        assignments = access.assignments(db, source) if viewer is not None else {}
        metas = list(db.exec(select(LeadMeta).where(LeadMeta.source == source, LeadMeta.follow_up_date.is_not(None))))
        existing_keys = set(assignments)
        candidate = [m for m in metas if m.lead_key in existing_keys
                     and (visible is None or m.lead_key in visible)
                     and m.update_status not in {"converted", "not_interested"}]
        keys = {m.lead_key for m in candidate}
        details = _details_for_crm_source(db, source, keys)
        if source == "spin":
            workflow = [key for key in keys if not key.startswith("crm-") and key.isdigit()]
            details.update(_workflow_spin_details(workflow))
        vendor_by_id = {v.id: v.name for v in db.exec(select(Vendor))} if viewer is None or viewer.is_admin else {}
        for meta in candidate:
            detail = details.get(meta.lead_key)
            if detail is None:
                continue
            name, phone = detail
            assigned = assignments.get(meta.lead_key)
            rows.append({"source": source, "lead_key": meta.lead_key, "name": name, "phone": phone,
                         "follow_up_date": meta.follow_up_date, "update_status": meta.update_status,
                         "vendor_name": vendor_by_id.get(assigned) if viewer is not None and viewer.is_admin else None})

    vtk_stmt = select(VendorToKocLead, Vendor.name).join(Vendor, Vendor.id == VendorToKocLead.vendor_id)
    if viewer is not None and not viewer.is_admin:
        vtk_stmt = vtk_stmt.where(VendorToKocLead.vendor_id == viewer.vendor_id)
    for lead, vendor_name in db.exec(vtk_stmt.where(VendorToKocLead.follow_up_date.is_not(None))):
        if lead.update_status in {"converted", "not_interested"}:
            continue
        rows.append({"source": "vendor_to_koc", "lead_key": str(lead.id), "name": lead.name,
                     "phone": lead.phone, "follow_up_date": lead.follow_up_date,
                     "update_status": lead.update_status,
                     "vendor_name": vendor_name if viewer is not None and viewer.is_admin else None})

    if viewer is None or viewer.is_admin or dhruva_only:
        dhruva_query = select(DhruvaLead).where(DhruvaLead.follow_up_date.is_not(None), DhruvaLead.is_deleted == False)  # noqa: E712
        for lead in db.exec(dhruva_query):
            if lead.update_status in {"converted", "not_interested"}:
                continue
            rows.append({"source": "dhruva", "lead_key": str(lead.id), "name": lead.name,
                         "phone": lead.phone, "follow_up_date": lead.follow_up_date,
                         "update_status": lead.update_status, "vendor_name": None})
    return rows


def _make_response(rows: list[dict], days: int, admin: bool) -> dict:
    today = periods.today_ist()
    max_date = date.fromordinal(today.toordinal() + days)
    kept = [r for r in rows if r["follow_up_date"] <= max_date]
    kept.sort(key=lambda r: (0 if r["follow_up_date"] < today else 1,
                             r["follow_up_date"] if r["follow_up_date"] < today else r["follow_up_date"],
                             r["source"], r["lead_key"]))
    counts = Counter("overdue" if r["follow_up_date"] < today else "today" if r["follow_up_date"] == today else "upcoming"
                     for r in kept)
    items = []
    for row in kept:
        item = {"source": row["source"], "lead_key": row["lead_key"], "name": row["name"],
                "phone": row["phone"], "follow_up_date": row["follow_up_date"].isoformat(),
                "days_overdue": (today - row["follow_up_date"]).days}
        if admin:
            item["update_status"] = row["update_status"]
            if row["source"] != "dhruva":
                item["vendor_name"] = row["vendor_name"]
        items.append(item)
    return {"items": items, "counts": {"overdue": counts["overdue"], "today": counts["today"],
                                          "upcoming": counts["upcoming"]}, "days": days}


@router.get("/follow-ups")
def follow_up_reminders(user: NotDhruvaUser, db: DbSession, days: int = Query(7, ge=0, le=30),
                        sources: str | None = Query(None, description="Comma-separated reminder sources")):
    try:
        viewer = access.viewer_for(db, user)
    except access.NotLinked:
        raise HTTPException(403, "This login isn't linked to a vendor.")
    requested = parse_sources(sources)
    rows = [row for row in _reminder_rows(db, viewer) if row["source"] in requested]
    return _make_response(rows, days, viewer.is_admin)


