"""Vendors (partners leads are assigned to) and their numbers (admin only).

GET   /vendors             all vendors with their area rules
POST  /vendors             add a vendor
PATCH /vendors/{id}        change a vendor (name, areas, pincodes, priority, contact…)
GET   /vendors/stats       per vendor: leads, interested, quoted, converted, quoted value
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.api.deps import AdminUser, DbSession
from app.core import response_cache
from app.db.session import engine, read_engine
from app.models.vendor import Vendor
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.services import lead_crm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vendors", tags=["vendors"])


class VendorIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    coverage: str | None = Field(default=None, max_length=300)
    areas: list[str] = []
    pincodes: list[str] = []
    pincode_prefixes: list[str] = []
    is_fallback: bool = False
    priority: int = Field(default=50, ge=0, le=1000)
    contact_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=254)
    notes: str | None = Field(default=None, max_length=5000)
    is_active: bool = True


class VendorPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    coverage: str | None = Field(default=None, max_length=300)
    areas: list[str] | None = None
    pincodes: list[str] | None = None
    pincode_prefixes: list[str] | None = None
    is_fallback: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    contact_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=254)
    notes: str | None = Field(default=None, max_length=5000)
    is_active: bool | None = None


def _clean_list(values: list[str]) -> list[str]:
    seen, out = set(), []
    for v in values:
        v = (v or "").strip().lower()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _out(v: Vendor) -> dict:
    return {
        "id": v.id,
        "name": v.name,
        "coverage": v.coverage,
        "areas": v.areas or [],
        "pincodes": v.pincodes or [],
        "pincode_prefixes": v.pincode_prefixes or [],
        "is_fallback": v.is_fallback,
        "priority": v.priority,
        "contact_name": v.contact_name,
        "phone": v.phone,
        "email": v.email,
        "notes": v.notes,
        "is_active": v.is_active,
    }


def _changed():
    from app.services.branches import forget_cache

    response_cache.clear("vendors")
    response_cache.clear("spin")  # spin leads' vendors come from branch + vendor rules
    forget_cache()


@router.get("")
def list_vendors(_: AdminUser, db: DbSession, include_inactive: bool = False):
    query = select(Vendor).order_by(Vendor.priority, Vendor.id)
    if not include_inactive:
        query = query.where(Vendor.is_active == True)  # noqa: E712
    return [_out(v) for v in db.exec(query)]


@router.post("", status_code=201)
def create_vendor(body: VendorIn, _: AdminUser, db: DbSession):
    if db.exec(select(Vendor).where(Vendor.name == body.name.strip())).first():
        raise HTTPException(409, "A vendor with that name already exists")
    now = datetime.now(timezone.utc)
    data = body.model_dump()
    data["name"] = data["name"].strip()
    for f in ("areas", "pincodes", "pincode_prefixes"):
        data[f] = _clean_list(data[f])
    vendor = Vendor(**data, created_at=now, updated_at=now)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    _changed()
    return _out(vendor)


@router.patch("/{vendor_id}")
def update_vendor(vendor_id: int, body: VendorPatch, _: AdminUser, db: DbSession):
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(404, "Vendor not found")
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        data["name"] = data["name"].strip()
        clash = db.exec(select(Vendor).where(Vendor.name == data["name"], Vendor.id != vendor_id)).first()
        if clash:
            raise HTTPException(409, "A vendor with that name already exists")
    for f in ("areas", "pincodes", "pincode_prefixes"):
        if f in data and data[f] is not None:
            data[f] = _clean_list(data[f])
    for field, value in data.items():
        setattr(vendor, field, value)
    vendor.updated_at = datetime.now(timezone.utc)
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    _changed()
    return _out(vendor)


# ── Statistics ─────────────────────────────────────────────────────────
def _spin_rows(db: Session) -> list[tuple]:
    """(lead id, location, pincode, None, branch vendor id) for every listed spin lead."""
    if read_engine is None:
        return []
    try:
        from sqlalchemy import select as sa_select

        from app.api.v1.endpoints import spin_leads as sl
        from app.services.branches import branches_by_id

        table = sl._get_table()
        base = sl._required_filter(table)
        json_fields = sl._get_json_fields(table, base)
        pk = list(table.primary_key.columns)[0]
        fields = {f.key.lower(): f for fs in json_fields.values() for f in fs}
        pin_field = next((f for k, f in fields.items() if "pincode" in k or k == "pin"), None)
        city_field = next((f for k, f in fields.items() if "city" in k or "location" in k), None)
        _, branch_expr = sl._branch_field(table, json_fields)
        exprs = [
            pk,
            sl._json_text_expr(table, city_field) if city_field else pk.label("no_city"),
            sl._json_text_expr(table, pin_field) if pin_field else pk.label("no_pin"),
            branch_expr.label("branch") if branch_expr is not None else pk.label("no_branch"),
        ]
        with read_engine.connect() as conn:
            rows = conn.execute(sa_select(*exprs).select_from(table).where(*base)).all()
        branches = branches_by_id(db)
        out = []
        for r in rows:
            branch = branches.get(str(r[3])) if branch_expr is not None and r[3] is not None else None
            out.append(
                (
                    str(r[0]),
                    (r[1] if city_field else None) or (branch or {}).get("city"),
                    r[2] if pin_field else None,
                    None,
                    (branch or {}).get("vendor_id"),
                )
            )
        return out
    except Exception:  # noqa: BLE001
        logger.warning("Vendor stats: spin leads unavailable", exc_info=True)
        return []


def _build_stats() -> dict:
    with Session(engine) as db:
        sources = {
            "spin": _spin_rows(db),
            "whatsapp": [
                (k, c, None, None)
                for k, c in db.exec(select(WhatsAppLead.sheet_key, WhatsAppLead.country).where(WhatsAppLead.is_deleted == False))  # noqa: E712
            ],
            "wedding": [
                (k, c, None, s)
                for k, c, s in db.exec(
                    select(WeddingLead.sheet_key, WeddingLead.city, WeddingLead.shared_with).where(WeddingLead.is_deleted == False)  # noqa: E712
                )
            ],
            "wedding_planner": [
                (k, c or h, None, s)
                for k, c, h, s in db.exec(
                    select(WeddingPlan.plan_id, WeddingPlan.city, WeddingPlan.home_city, WeddingPlan.shared_with).where(
                        WeddingPlan.is_deleted == False  # noqa: E712
                    )
                )
            ],
        }

        blank = lambda: {  # noqa: E731
            "leads": 0, "interested": 0, "not_interested": 0, "other": 0, "follow_up": 0,
            "quoted": 0, "quoted_value": 0, "converted": 0, "converted_value": 0,
            "by_source": defaultdict(int),
        }
        per_vendor: dict[str, dict] = defaultdict(blank)
        unassigned = 0
        for source, rows in sources.items():
            for info in lead_crm.effective_vendor_counts(db, source, rows):
                if not info["vendor"]:
                    unassigned += 1
                    continue
                s = per_vendor[info["vendor"]["name"]]
                s["leads"] += 1
                s["by_source"][source] += 1
                if info["interest"] in s:
                    s[info["interest"]] += 1
                if info["quoted_amount"] or info["quoted_date"]:
                    s["quoted"] += 1
                    s["quoted_value"] += info["quoted_amount"] or 0
                if info["converted"]:
                    s["converted"] += 1
                    s["converted_value"] += info["quoted_amount"] or 0

        vendors = []
        for v in lead_crm.active_vendors(db):
            s = per_vendor.get(v.name) or blank()
            vendors.append(
                {
                    "vendor": {"id": v.id, "name": v.name},
                    **{k: val for k, val in s.items() if k != "by_source"},
                    "by_source": dict(s["by_source"]),
                    "conversion_rate": round(s["converted"] / s["leads"] * 100, 1) if s["leads"] else None,
                }
            )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vendors": vendors,
        "unassigned": unassigned,
    }


@router.get("/stats")
def vendor_stats(_: AdminUser):
    return response_cache.get(("vendors", "stats"), _build_stats, fresh=60, stale=1800)