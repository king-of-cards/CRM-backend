"""CRM details for leads from every source (the sidecar), and vendor assignment.

Each lead row sent to the frontend gets a "crm" object:
  {
    "lead_key": "...",
    "vendor": {"id": 2, "name": "Phodeo"} | None,
    "vendor_source": "manual" | "sheet" | "branch" | "suggested" | None,
    "suggested_vendor": {...} | None,     # what the area rules say
    "interest": "interested" | "not_interested" | "other" | "follow_up" | None,
    "converted": bool, "quoted_date": "2026-11-20" | None, "quoted_amount": 45000 | None,
    "notes": str | None, "updated_at": iso | None,
  }

Who a lead is assigned to, in order:
  1. manual    – chosen in the CRM (stored in crm_lead_meta)
  2. sheet     – the sheet's "leads shared" column names a vendor
  3. branch    – spin leads: the vendor set on the lead's branch (store)
  4. suggested – the vendors' area rules (pincode / city), see Vendor
"""
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from app.models.lead_meta import LeadMeta
from app.models.vendor import Vendor

SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
INTEREST_VALUES = ("interested", "not_interested", "other", "follow_up")

# The three partners and the areas they cover. Created once if the vendors table is empty;
# after that, edit them on the Vendors page.
DEFAULT_VENDORS = [
    {
        "name": "RJ",
        "coverage": "Chamrajpet (Bangalore)",
        "areas": ["chamrajpet", "chamarajpet", "chamraj pet", "chamaraj pet"],
        "pincodes": ["560018"],
        "pincode_prefixes": [],
        "is_fallback": False,
        "priority": 10,
    },
    {
        "name": "Phodeo",
        "coverage": "All Bangalore stores except Chamrajpet",
        "areas": ["bangalore", "bengaluru", "banglore", "bangaluru", "blr"],
        "pincodes": [],
        "pincode_prefixes": ["560"],
        "is_fallback": False,
        "priority": 20,
    },
    {
        "name": "OHP",
        "coverage": "All stores outside Bangalore",
        "areas": [],
        "pincodes": [],
        "pincode_prefixes": [],
        "is_fallback": True,
        "priority": 100,
    },
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed_vendors(session: Session) -> None:
    if session.exec(select(Vendor).limit(1)).first():
        return
    now = _now()
    for v in DEFAULT_VENDORS:
        session.add(Vendor(**v, created_at=now, updated_at=now))
    session.commit()


def active_vendors(session: Session) -> list[Vendor]:
    return list(session.exec(select(Vendor).where(Vendor.is_active == True).order_by(Vendor.priority, Vendor.id)))  # noqa: E712


# ── Area rules ─────────────────────────────────────────────────────────
_PINCODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def _norm_text(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "


def suggest_vendor(vendors: list[Vendor], location: str | None, pincode: str | None = None) -> Vendor | None:
    """First vendor (by priority) whose pincodes, pincode prefixes or area words match.
    The fallback vendor only applies when the lead has some location at all."""
    location = (location or "").strip()
    pins = set(_PINCODE.findall(f"{pincode or ''} {location}"))
    text = _norm_text(location) if location else ""
    if not pins and not text.strip():
        return None
    fallback = None
    for v in vendors:
        if v.is_fallback:
            fallback = fallback or v
            continue
        if pins & set(v.pincodes or []):
            return v
        if any(p.startswith(prefix) for p in pins for prefix in (v.pincode_prefixes or []) if prefix):
            return v
        if text and any(_norm_text(area) in text for area in (v.areas or []) if area.strip()):
            return v
    return fallback


def vendor_by_name(vendors: list[Vendor], name: str | None) -> Vendor | None:
    """Match the sheet's 'leads shared' text ('OHP', 'phodeo ') to a vendor."""
    if not name:
        return None
    key = name.strip().lower()
    return next((v for v in vendors if v.name.lower() == key), None)


# ── Attaching CRM details to lead rows ─────────────────────────────────
def _vendor_out(v: Vendor | None) -> dict | None:
    return {"id": v.id, "name": v.name} if v else None


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def meta_out(
    meta: LeadMeta | None,
    lead_key: str,
    vendors_by_id: dict[int, Vendor],
    sheet_vendor: Vendor | None,
    suggested: Vendor | None,
    branch_vendor: Vendor | None = None,
) -> dict:
    manual = vendors_by_id.get(meta.vendor_id) if meta and meta.vendor_id else None
    if manual:
        vendor, how = manual, "manual"
    elif sheet_vendor:
        vendor, how = sheet_vendor, "sheet"
    elif branch_vendor:
        vendor, how = branch_vendor, "branch"
    elif suggested:
        vendor, how = suggested, "suggested"
    else:
        vendor, how = None, None
    return {
        "lead_key": lead_key,
        "vendor": _vendor_out(vendor),
        "vendor_source": how,
        "suggested_vendor": _vendor_out(suggested),
        "interest": meta.interest if meta else None,
        "converted": bool(meta.converted) if meta else False,
        "converted_at": _iso(meta.converted_at) if meta else None,
        "quoted_date": meta.quoted_date.isoformat() if meta and meta.quoted_date else None,
        "quoted_amount": meta.quoted_amount if meta else None,
        "notes": meta.notes if meta else None,
        "updated_at": _iso(meta.updated_at) if meta else None,
    }


def attach(
    session: Session,
    source: str,
    items: list[dict],
    key: Callable[[dict], Any],
    location: Callable[[dict], str | None],
    pincode: Callable[[dict], str | None] = lambda _: None,
    sheet_vendor: Callable[[dict], str | None] = lambda _: None,
    branch_vendor: Callable[[dict], int | None] = lambda _: None,
) -> list[dict]:
    """Return copies of `items`, each with a "crm" object. One query for the whole page."""
    vendors = active_vendors(session)
    by_id = {v.id: v for v in vendors}
    keys = [str(key(i)) for i in items if key(i) is not None]
    metas = {}
    if keys:
        metas = {
            m.lead_key: m
            for m in session.exec(select(LeadMeta).where(LeadMeta.source == source, LeadMeta.lead_key.in_(keys)))
        }
    out = []
    for item in items:
        k = key(item)
        copy = dict(item)
        if k is None:
            copy["crm"] = None
        else:
            k = str(k)
            copy["crm"] = meta_out(
                metas.get(k),
                k,
                by_id,
                vendor_by_name(vendors, sheet_vendor(item)),
                suggest_vendor(vendors, location(item), pincode(item)),
                by_id.get(branch_vendor(item)),
            )
        out.append(copy)
    return out


def effective_vendor_counts(session: Session, source: str, rows: Iterable[tuple]) -> list[dict]:
    """rows: (lead_key, location, pincode, sheet_vendor[, branch_vendor_id]). Returns one dict
    per lead with the effective vendor and the lead's CRM status, for vendor statistics."""
    vendors = active_vendors(session)
    by_id = {v.id: v for v in vendors}
    metas = {m.lead_key: m for m in session.exec(select(LeadMeta).where(LeadMeta.source == source))}
    result = []
    for row in rows:
        lead_key, loc, pin, shared = row[:4]
        branch_vendor_id = row[4] if len(row) > 4 else None
        meta = metas.get(str(lead_key))
        info = meta_out(
            meta,
            str(lead_key),
            by_id,
            vendor_by_name(vendors, shared),
            suggest_vendor(vendors, loc, pin),
            by_id.get(branch_vendor_id),
        )
        result.append(info)
    return result