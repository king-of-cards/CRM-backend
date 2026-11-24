"""CRM details for leads from every source (the sidecar), and vendor assignment.

A KOC -> Vendors lead (spin, whatsapp, wedding, wedding_planner) can have SEVERAL vendors
(vendor_set), each working its own copy of the CRM details (crm_lead_meta.copy_vendor_id): Update,
quoted amount / date, conversion amount, follow-up date, notes and the hot flag (crm_hot_leads.vendor_id).

Who has a lead (vendor_set):
  0. "Unassigned" (auto_assign = False on the vendor-less row): nobody.
  1. main vendor(s): the manual ones (crm_lead_assignments "added"; legacy: crm_lead_meta.vendor_id
     on the vendor-less row) if any, otherwise the main automatic vendor:
       sheet     – the sheet's "leads shared" column names a vendor
       branch    – spin leads: the vendor set on the lead's branch (store)
       suggested – the vendors' area rules (pincode / city), see Vendor
  2. plus every partner rule that matches (PARTNER_RULES; first: the gifts vendor, Kraftyz, for spin
     gift leads received on or after WEDDING_GIFTS_FROM) – "partner"; never replacing anyone
  3. minus the vendors removed by hand (crm_lead_assignments "removed": removing sticks).
Only active vendors come from rules; a vendor added by hand that was later deactivated keeps its name.

Each lead row sent to the frontend gets a "crm" object (attach) with the overall view and one entry
per vendor ("vendors"); for_viewer() turns it into the vendor's own copy for vendor logins:
  {
    "lead_key": "...",
    "vendor": {"id": 2, "name": "Phodeo"} | None,          # the first vendor
    "vendor_source": "manual" | "sheet" | "branch" | "suggested" | "partner" | None,
    "vendors": [{"vendor", "vendor_source", "update_status", "quoted_amount", "quoted_date",
                 "conversion_amount", "follow_up_date", "notes", "updated_at", "updated_by_name", "is_hot"}],
    "suggested_vendor": {...} | None,     # what the area rules say
    "update_status", "quoted_date", "quoted_amount", "conversion_amount", "follow_up_date", "notes",
    "updated_at", "updated_by_name", "is_hot", "auto_assign"      # overall (see merge_copies)
  }
"""
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from app.models.lead_meta import LeadMeta
from app.models.vendor import Vendor

SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
# Sources whose leads are never assigned to a vendor (no rules, no default vendor)
UNASSIGNED_SOURCES = ("koc_to_dhruva",)
# Sources whose gift leads go to the gifts vendor
GIFT_SOURCES = ("spin",)


def gift_vendor_name() -> str | None:
    """The vendor spin gift leads go to (WEDDING_GIFTS_VENDOR_NAME, read on every call), or None."""
    from app.core.config import settings

    return (settings.WEDDING_GIFTS_VENDOR_NAME or "").strip() or None

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


def vendors_by_id(session: Session) -> dict[int, Vendor]:
    """Every vendor (inactive ones too): a lead manually assigned to a vendor that was made
    inactive keeps showing that vendor's name. Automatic rules only ever use active_vendors."""
    return {v.id: v for v in session.exec(select(Vendor))}


def active_vendors(session: Session) -> list[Vendor]:
    return list(session.exec(select(Vendor).where(Vendor.is_active == True).order_by(Vendor.priority, Vendor.id)))  # noqa: E712


VENDOR_CACHE_SECONDS = 30       # as the access cache; cleared with every "vendors" cache clear


def vendor_snapshot(session: Session) -> tuple[list[Vendor], dict[int, Vendor]]:
    """(active_vendors, vendors_by_id) for read paths (Book: lists, visibility, dashboards), from one
    query, cached briefly as detached copies (never added to a session). Vendor and branch changes
    clear it (response_cache.clear("vendors")); writes that check a vendor read the database."""
    from app.core import response_cache

    def build():
        rows = [Vendor(**v.model_dump()) for v in session.exec(select(Vendor))]
        active = sorted((v for v in rows if v.is_active), key=lambda v: (v.priority, v.id))
        return active, {v.id: v for v in rows}

    return response_cache.get(("vendors", "snapshot"), build, fresh=VENDOR_CACHE_SECONDS, stale=VENDOR_CACHE_SECONDS)


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
    default_vendor: Vendor | None = None,
    names: dict[int, str] | None = None,
) -> dict:
    """One CRM row as a crm object (single-row sections such as koc_to_dhruva)."""
    vendor = sheet_vendor or branch_vendor or suggested or default_vendor
    return {
        "lead_key": lead_key,
        "vendor": _vendor_out(vendor),
        "vendor_source": None,
        "suggested_vendor": _vendor_out(suggested),
        **_copy_fields(meta, names),
        "auto_assign": meta.auto_assign is not False if meta else True,
    }


def _copy_fields(meta: LeadMeta | None, names: dict[int, str] | None) -> dict:
    return {
        "update_status": meta.update_status if meta else None,
        "quoted_date": meta.quoted_date.isoformat() if meta and meta.quoted_date else None,
        "follow_up_date": meta.follow_up_date.isoformat() if meta and meta.follow_up_date else None,
        "quoted_amount": meta.quoted_amount if meta else None,
        "conversion_amount": meta.conversion_amount if meta else None,
        "notes": meta.notes if meta else None,
        "updated_at": _iso(meta.updated_at) if meta else None,
        "updated_by_name": (names.get(meta.updated_by) if names is not None and meta and meta.updated_by is not None
                            else None),                    # who last saved the CRM details ("Deleted user")
    }


def assigned_label(crm: dict | None) -> str:
    """The "Assigned to" text on the list screens and exports: every vendor, "Phodeo · auto" for a
    rule-based one (area rules, a partner such as Kraftyz), joined with ", "; or "Unassigned"."""
    entries = (crm or {}).get("vendors")
    if entries is None:                                   # a single-vendor crm object
        entries = [crm] if (crm or {}).get("vendor") else []
    labels = []
    for entry in entries:
        vendor = entry.get("vendor")
        if not vendor:
            continue
        auto = entry.get("vendor_source") in ("suggested", "partner")
        labels.append(f"{vendor['name']} · auto" if auto else vendor["name"])
    return ", ".join(labels) or "Unassigned"


# ── Partner rules: vendors that get a lead next to its main vendor ─────
@dataclass(frozen=True)
class LeadFacts:
    """What the rules look at, for one lead."""
    key: str
    location: str | None = None
    pincode: str | None = None
    sheet_vendor: str | None = None
    branch_vendor_id: int | None = None
    gift: bool = False
    received: datetime | None = None            # naive UTC: workflow created_at, or the upload's Created


@dataclass(frozen=True)
class PartnerRule:
    name: str
    sources: tuple[str, ...]
    vendor_name: Callable[[], str | None]       # the vendor, by name (active vendors only)
    applies: Callable[[LeadFacts], bool]


def gifts_from_utc() -> datetime:
    """WEDDING_GIFTS_FROM (India time) as naive UTC."""
    from datetime import timedelta

    from app.core.config import settings

    value = settings.WEDDING_GIFTS_FROM
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value - timedelta(hours=5, minutes=30)


def _gift_rule(facts: LeadFacts) -> bool:
    return facts.gift and facts.received is not None and facts.received >= gifts_from_utc()


# Add a rule here to give more partners their leads (each one is an extra vendor, never a replacement)
PARTNER_RULES: list[PartnerRule] = [
    PartnerRule("wedding_gifts", GIFT_SOURCES, gift_vendor_name, _gift_rule),
]


def _naive_utc(value: Any) -> datetime | None:
    from app.api.v1.endpoints.spin_leads import to_naive_utc

    return to_naive_utc(value)


# ── Copies and assignments of a set of leads ──────────────────────────
class Book:
    """Everything the vendor sets and crm objects of one source's leads need, loaded at once:
    vendors, every CRM copy, the manual assignments, the hot flags and the editors' names."""

    def __init__(self, session: Session, source: str, keys: Iterable[str] | None = None):
        from app.crud.user import user_names
        from app.models.hot_lead import CrmHotLead
        from app.models.lead_assignment import LeadAssignment

        self.source = source
        self.active, self.by_id = vendor_snapshot(session)
        wanted = None if keys is None else sorted({str(k) for k in keys})
        self.copies: dict[str, dict[int, LeadMeta]] = {}
        self.assigned: dict[str, list[LeadAssignment]] = {}
        self.hot: dict[str, dict[int, Any]] = {}
        if wanted == []:
            self.names = {}
            return
        for chunk in ([None] if wanted is None else list(_chunks(wanted))):
            for m in session.exec(_where(select(LeadMeta), LeadMeta, source, chunk)):
                self.copies.setdefault(m.lead_key, {})[m.copy_vendor_id or 0] = m
            for a in session.exec(_where(select(LeadAssignment), LeadAssignment, source, chunk)
                                  .order_by(LeadAssignment.id)):
                self.assigned.setdefault(a.lead_key, []).append(a)
            for h in session.exec(_where(select(CrmHotLead), CrmHotLead, source, chunk)):
                self.hot.setdefault(h.lead_key, {})[h.vendor_id or 0] = h
        self.names = user_names(session, (m.updated_by for c in self.copies.values() for m in c.values()))

    def main_vendor(self, facts: LeadFacts) -> tuple[Vendor | None, str | None]:
        """The main automatic vendor: sheet, then branch, then the area rules (active vendors)."""
        sheet = vendor_by_name(self.active, facts.sheet_vendor)
        if sheet:
            return sheet, "sheet"
        branch = self.by_id.get(facts.branch_vendor_id) if facts.branch_vendor_id is not None else None
        if branch is not None and branch.is_active:
            return branch, "branch"
        suggested = suggest_vendor(self.active, facts.location, facts.pincode)
        return (suggested, "suggested") if suggested else (None, None)

    def vendor_set(self, facts: LeadFacts) -> list[tuple[Vendor, str]]:
        """[(vendor, how)] for one lead (see the module docstring), in a stable order."""
        if self.source in UNASSIGNED_SOURCES:
            return []
        key = str(facts.key)
        base = self.copies.get(key, {}).get(0)
        if base is not None and base.auto_assign is False:
            return []                                     # "Unassigned": nobody
        rows = self.assigned.get(key, [])
        removed = {a.vendor_id for a in rows if a.state == "removed"}
        added = [self.by_id[a.vendor_id] for a in rows if a.state == "added" and a.vendor_id in self.by_id]
        if not rows and base is not None and base.vendor_id in self.by_id:
            added = [self.by_id[base.vendor_id]]          # legacy manual vendor (before the migration)
        out: list[tuple[Vendor, str]] = [(v, "manual") for v in added]
        if not out:
            vendor, how = self.main_vendor(facts)
            if vendor is not None:
                out.append((vendor, how))
        for rule in PARTNER_RULES:
            if self.source in rule.sources and rule.applies(facts):
                vendor = vendor_by_name(self.active, rule.vendor_name())
                if vendor is not None and all(v.id != vendor.id for v, _ in out):
                    out.append((vendor, "partner"))
        return [(v, how) for v, how in out if v.id not in removed]

    def copy(self, key: str, vendor_id: int) -> LeadMeta | None:
        return self.copies.get(str(key), {}).get(vendor_id)

    def is_hot(self, key: str, vendor_id: int) -> bool:
        return vendor_id in self.hot.get(str(key), {})

    def crm(self, facts: LeadFacts, vendors: list[tuple[Vendor, str]] | None = None) -> dict:
        """The full crm object of one lead (admins / view-only; for_viewer() for vendor logins).
        vendors: its vendor set when already known (single-lead answers)."""
        key = str(facts.key)
        if vendors is None:
            vendors = self.vendor_set(facts)
        base = self.copies.get(key, {}).get(0)
        entries = []
        for vendor, how in vendors:
            meta = self.copy(key, vendor.id)
            entries.append({"vendor": _vendor_out(vendor), "vendor_source": how, **_copy_fields(meta, self.names),
                            "is_hot": self.is_hot(key, vendor.id)})
        merged = merge_copies(list(self.copies.get(key, {}).values()))
        latest = max(self.copies.get(key, {}).values(), key=lambda m: _naive_utc(m.updated_at) or datetime.min,
                     default=None)
        merged["updated_by_name"] = (self.names.get(latest.updated_by) if latest is not None
                                     and latest.updated_by is not None else None)
        suggested = suggest_vendor(self.active, facts.location, facts.pincode) if self.source not in UNASSIGNED_SOURCES else None
        hot = bool(self.hot.get(key))
        return {
            "lead_key": key,
            "vendor": entries[0]["vendor"] if entries else None,
            "vendor_source": entries[0]["vendor_source"] if entries else None,
            "vendors": entries,
            "suggested_vendor": _vendor_out(suggested),
            **merged,
            "notes": base.notes if base else merged["notes"],
            "is_hot": hot,
            "auto_assign": base.auto_assign is not False if base else True,
        }


def _chunks(items: list[str], size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _where(query, model, source: str, keys: list[str] | None):
    query = query.where(model.source == source)
    return query if keys is None else query.where(model.lead_key.in_(keys))


# Overall Update of a lead with several copies: the furthest one (converted if any vendor converted)
STATUS_RANK = ("converted", "purchased_with_us", "interested", "will_call_back", "not_converted",
               "purchased_elsewhere", "not_interested")


def best_status(statuses: Iterable[str | None]) -> str | None:
    found = [s for s in statuses if s]
    if not found:
        return None
    return min(found, key=lambda s: STATUS_RANK.index(s) if s in STATUS_RANK else len(STATUS_RANK))


def merge_copies(copies: list[LeadMeta]) -> dict:
    """The overall CRM values of a lead from all its copies: the furthest Update (best_status), the
    quoted and conversion amounts summed, the latest quoted date, the earliest follow-up, the last
    change. Notes: the first copy's with notes."""
    def total(name):
        values = [getattr(m, name) for m in copies if getattr(m, name) is not None]
        return sum(values) if values else None

    quoted = [m.quoted_date for m in copies if m.quoted_date]
    follow = [m.follow_up_date for m in copies if m.follow_up_date]
    latest = max(copies, key=lambda m: _naive_utc(m.updated_at) or datetime.min, default=None)
    return {
        "update_status": best_status(m.update_status for m in copies),
        "quoted_date": max(quoted).isoformat() if quoted else None,
        "follow_up_date": min(follow).isoformat() if follow else None,
        "quoted_amount": total("quoted_amount"),
        "conversion_amount": total("conversion_amount"),
        "notes": next((m.notes for m in copies if m.notes), None),
        "updated_at": _iso(latest.updated_at) if latest else None,
        "updated_by_name": None,
    }


class Record:
    """A lead's CRM values as one row (kpis.compute, filters, reminders): the overall view, or one
    vendor's copy."""

    def __init__(self, update_status=None, quoted_amount=None, conversion_amount=None, quoted_date=None,
                 follow_up_date=None, notes=None):
        self.update_status = update_status
        self.quoted_amount = quoted_amount
        self.conversion_amount = conversion_amount
        self.quoted_date = quoted_date
        self.follow_up_date = follow_up_date
        self.notes = notes


def _record(copies: list[LeadMeta]) -> Record | None:
    if not copies:
        return None
    if len(copies) == 1:
        return copies[0]                                   # a LeadMeta has the same attributes
    from datetime import date as _date

    merged = merge_copies(copies)
    return Record(merged["update_status"], merged["quoted_amount"], merged["conversion_amount"],
                  _date.fromisoformat(merged["quoted_date"]) if merged["quoted_date"] else None,
                  _date.fromisoformat(merged["follow_up_date"]) if merged["follow_up_date"] else None,
                  merged["notes"])


def records(session: Session, source: str, vendor_id: int | None = None) -> dict[str, Any]:
    """{lead key: CRM values}: overall (every copy of the lead merged: merge_copies), or with
    vendor_id only that vendor's copy. A lead without CRM details is missing (= Pending)."""
    query = select(LeadMeta).where(LeadMeta.source == source)
    if vendor_id is not None:
        query = query.where(LeadMeta.copy_vendor_id == vendor_id)
    by_key: dict[str, list[LeadMeta]] = {}
    for m in session.exec(query):
        by_key.setdefault(m.lead_key, []).append(m)
    return {k: _record(v) for k, v in by_key.items()}


def hot_keys(session: Session, source: str, vendor_id: int | None = None) -> set[str]:
    """Leads with a hot flag on any copy (vendor_id: on that vendor's copy)."""
    from app.models.hot_lead import CrmHotLead

    query = select(CrmHotLead.lead_key).where(CrmHotLead.source == source)
    if vendor_id is not None:
        query = query.where(CrmHotLead.vendor_id == vendor_id)
    return set(session.exec(query).all())


def viewer_copy(viewer) -> int | None:
    """Whose copy a viewer reads: a vendor login its own (vendor id); everyone else the overall view (None)."""
    if viewer is None or getattr(viewer, "sees_all", True):
        return None
    return getattr(viewer, "vendor_id", None)


def for_viewer(item: dict, viewer) -> dict:
    """A list item (with its full crm object) as this viewer may see it: admins and view-only logins
    get everything; a vendor login gets only its own copy (no other vendors)."""
    if viewer_copy(viewer) is None or not item.get("crm"):
        return item
    crm = item["crm"]
    own = next((e for e in crm.get("vendors") or [] if (e.get("vendor") or {}).get("id") == viewer.vendor_id), None)
    if own is None:
        own = {"vendor": None, "vendor_source": None, **_copy_fields(None, None), "is_hot": False}
    item["crm"] = {"lead_key": crm["lead_key"], "vendor": own["vendor"], "vendor_source": own["vendor_source"],
                   "vendors": [own], "suggested_vendor": None,
                   **{k: own[k] for k in ("update_status", "quoted_date", "follow_up_date", "quoted_amount",
                                          "conversion_amount", "notes", "updated_at", "updated_by_name", "is_hot")},
                   "auto_assign": crm.get("auto_assign", True)}
    item["is_hot"] = own["is_hot"]
    item["follow_up_date"] = own["follow_up_date"]
    item["hot_marked_by"] = item["hot_marked_at"] = None
    return item


def attach(
    session: Session,
    source: str,
    items: list[dict],
    key: Callable[[dict], Any],
    location: Callable[[dict], str | None],
    pincode: Callable[[dict], str | None] = lambda _: None,
    sheet_vendor: Callable[[dict], str | None] = lambda _: None,
    branch_vendor: Callable[[dict], int | None] = lambda _: None,
    gift: Callable[[dict], bool] | None = None,
    received: Callable[[dict], Any] | None = None,
) -> list[dict]:
    """Return copies of `items`, each with its full "crm" object (the overall view and one entry per
    vendor; see for_viewer). One query per table for the whole page."""
    book = Book(session, source, [str(key(i)) for i in items if key(i) is not None])
    out = []
    for item in items:
        k = key(item)
        copy = dict(item)
        if k is None:
            copy["crm"] = None
            copy["is_hot"] = False
        else:
            facts = LeadFacts(str(k), location(item), pincode(item), sheet_vendor(item), branch_vendor(item),
                              bool(gift(item)) if gift is not None else False,
                              _naive_utc(received(item)) if received is not None else None)
            crm = book.crm(facts)
            marked = next(iter(book.hot.get(str(k), {}).values()), None)
            copy["follow_up_date"] = crm["follow_up_date"]
            copy["is_hot"] = crm["is_hot"]
            copy["hot_marked_by"] = marked.hot_marked_by if marked else None
            copy["hot_marked_at"] = _iso(marked.hot_marked_at) if marked else None
            copy["crm"] = crm
        out.append(copy)
    return out


def crm_for_key(session: Session, source: str, lead_key: str, entries: list[tuple[int, str]]) -> dict:
    """The full crm object of one lead whose vendors are known (access.vendor_entries)."""
    book = Book(session, source, [lead_key])
    vendors = [(book.by_id[vid], how) for vid, how in entries if vid in book.by_id]
    return book.crm(LeadFacts(str(lead_key)), vendors)


def facts_of(row: tuple) -> LeadFacts:
    """rows: (lead_key, location, pincode, sheet_vendor[, branch_vendor_id[, is_gift[, received]]])."""
    return LeadFacts(str(row[0]), row[1], row[2], row[3], row[4] if len(row) > 4 else None,
                     bool(row[5]) if len(row) > 5 else False, _naive_utc(row[6]) if len(row) > 6 else None)


def vendor_sets(session: Session, source: str, rows: Iterable[tuple]) -> dict[str, list[tuple[Vendor, str]]]:
    """{lead key: [(vendor, how)]} for every lead (rows as in facts_of): visibility, filters, counts."""
    rows = list(rows)
    book = Book(session, source)
    return {str(row[0]): book.vendor_set(facts_of(row)) for row in rows}


def effective_vendor_counts(session: Session, source: str, rows: Iterable[tuple]) -> list[dict]:
    """One dict per (lead, vendor) with that vendor's copy (vendor statistics), and one per lead
    without a vendor ({"vendor": None, …})."""
    rows = list(rows)
    book = Book(session, source)
    out = []
    for row in rows:
        facts = facts_of(row)
        vendors = book.vendor_set(facts)
        if not vendors:
            out.append({"lead_key": facts.key, "vendor": None, "vendor_source": None,
                        **_copy_fields(book.copy(facts.key, 0), None)})
        for vendor, how in vendors:
            out.append({"lead_key": facts.key, "vendor": _vendor_out(vendor), "vendor_source": how,
                        **_copy_fields(book.copy(facts.key, vendor.id), None)})
    return out


# ── Writing assignments and copies ─────────────────────────────────────
def copy_row(session: Session, source: str, lead_key: str, vendor_id: int, create: bool = False) -> LeadMeta | None:
    """One copy of a lead's CRM details (vendor_id 0 = the vendor-less row); create: a new, unsaved one."""
    row = session.exec(select(LeadMeta).where(LeadMeta.source == source, LeadMeta.lead_key == str(lead_key),
                                              LeadMeta.copy_vendor_id == vendor_id)).first()
    if row is None and create:
        row = LeadMeta(source=source, lead_key=str(lead_key), copy_vendor_id=vendor_id, updated_at=_now())
    return row


def _assignment_rows(session: Session, source: str, lead_key: str) -> list:
    """The lead's manual choices; a legacy manual vendor (crm_lead_meta.vendor_id) becomes one."""
    from app.models.lead_assignment import LeadAssignment

    rows = list(session.exec(select(LeadAssignment).where(LeadAssignment.source == source,
                                                          LeadAssignment.lead_key == str(lead_key))
                             .order_by(LeadAssignment.id)))
    base = copy_row(session, source, lead_key, 0)
    if base is not None and base.vendor_id is not None:
        if not rows:
            row = LeadAssignment(source=source, lead_key=str(lead_key), vendor_id=base.vendor_id, state="added",
                                 updated_by=base.updated_by, updated_at=_now())
            session.add(row)
            rows = [row]
        base.vendor_id = None
        session.add(base)
    return rows


def _put(session: Session, rows: list, source: str, lead_key: str, vendor_id: int, state: str, user_id) -> None:
    from app.models.lead_assignment import LeadAssignment

    row = next((r for r in rows if r.vendor_id == vendor_id), None)
    if row is None:
        row = LeadAssignment(source=source, lead_key=str(lead_key), vendor_id=vendor_id, state=state,
                             updated_by=user_id, updated_at=_now())
        rows.append(row)
    row.state, row.updated_by, row.updated_at = state, user_id, _now()
    session.add(row)


def set_manual(session: Session, source: str, lead_key: str, vendor_ids: list[int], user_id: int | None,
               automatic: bool = True) -> None:
    """Replace the lead's manual choices (not committed): vendor_ids become its only manual vendors
    ([] = back to the rules); automatic False = "Unassigned" (nobody, not even a partner rule)."""
    for row in _assignment_rows(session, source, lead_key):
        session.delete(row)
    session.flush()
    rows: list = []
    for vendor_id in vendor_ids:
        _put(session, rows, source, lead_key, vendor_id, "added", user_id)
    base = copy_row(session, source, lead_key, 0, create=not automatic)
    if base is not None:
        base.auto_assign = automatic
        base.updated_at, base.updated_by = _now(), user_id
        session.add(base)


def add_vendor(session: Session, source: str, lead_key: str, vendor_id: int, entries: list[tuple[int, str]],
               user_id: int | None) -> None:
    """Give the lead to one more vendor (not committed). Its current main vendors are kept as manual
    ones (a manual vendor replaces the automatic one, so they are pinned first); an "Unassigned" lead
    becomes assigned again."""
    rows = _assignment_rows(session, source, lead_key)
    if not any(r.state == "added" for r in rows):
        for current, how in entries:
            if how != "partner":
                _put(session, rows, source, lead_key, current, "added", user_id)
    _put(session, rows, source, lead_key, vendor_id, "added", user_id)
    base = copy_row(session, source, lead_key, 0)
    if base is not None and base.auto_assign is False:
        base.auto_assign = True
        session.add(base)


def remove_vendor(session: Session, source: str, lead_key: str, vendor_id: int, entries: list[tuple[int, str]],
                  user_id: int | None) -> None:
    """Take the lead away from one vendor for good (not committed): no rule gives it back. The other
    vendors keep it (main automatic ones are pinned as manual, so they don't change either)."""
    rows = _assignment_rows(session, source, lead_key)
    if not any(r.state == "added" for r in rows):
        for current, how in entries:
            if how != "partner" and current != vendor_id:
                _put(session, rows, source, lead_key, current, "added", user_id)
    _put(session, rows, source, lead_key, vendor_id, "removed", user_id)
