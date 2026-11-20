"""Cross-source duplicates: where else a lead's phone exists ("also_in").

Places: the 5 sources (spin and wedding_gifts — workflow and CRM leads, each in its own flow —,
whatsapp, wedding, wedding_planner), Vendor -> KOC and Dhruva leads. Phones are compared as E.164 (phone_e164; Vendor -> KOC's
10-digit numbers as +91…; Dhruva: phone and alt phone). Archived and removed rows count (like
the upload duplicate check).

Visibility: admins see every place. A vendor login only sees places it can see itself: its own
leads in the 4 sources (the same assignment map the lists use, app/services/access.py) and its
own Vendor -> KOC leads; never Dhruva leads. A Dhruva login sees no other section's leads, so its
also_in is always empty. Other vendors' and unassigned leads are never revealed.

  attach(...)       per list page: one query per source for the page's phones
  matching_keys()   the whole source, for the also_in=yes|no filter (lists, stats, exports)
"""
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import or_
from sqlmodel import Session, col, select

from app.core import response_cache
from app.services import access, phones

logger = logging.getLogger(__name__)

SOURCES = ("spin", "wedding_gifts", "whatsapp", "wedding", "wedding_planner")
SPIN_FLOWS = ("spin", "wedding_gifts")          # both stored in crm_spin_leads / spin_lead
VTK = "vendor_to_koc"
DHRUVA = "dhruva"
PLACES = (*SOURCES, VTK, DHRUVA)
WORKFLOW_CACHE_SECONDS = 300


def vtk_e164(phone: str | None) -> str | None:
    return f"+91{phone}" if phone else None


def _tables():
    from app.services.crm_leads.service import TABLES
    return TABLES


def _crm_rows(db: Session, source: str, condition=None):
    """(place, lead key, e164) of the CRM-stored leads of a source; crm_spin_leads rows are read
    once (source "spin") and placed in their own flow."""
    model, key_col, to_key, _ = _key_column(source)
    if source in SPIN_FLOWS:
        from app.services.crm_spin_leads import flow_of
        query = select(model) if condition is None else select(model).where(condition(model))
        return [(flow_of(lead), lead.lead_key, lead.phone_e164) for lead in db.exec(query)]
    query = select(key_col, model.phone_e164)
    if condition is not None:
        query = query.where(condition(model))
    return [(source, to_key(k), e) for k, e in db.exec(query)]


CRM_TABLE_SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")   # one table each


def _key_column(source: str):
    """(model, key column, column value -> lead key, lead key -> column value). CRM spin leads are
    keyed "crm-<id>" (a property, not a column)."""
    model, key_name, _ = _tables()[source]
    if key_name is None:                                     # spin: CrmSpinLead
        from app.models.lead_origin import CRM_KEY_PREFIX

        def from_key(k: str):
            rest = k[len(CRM_KEY_PREFIX):] if k.startswith(CRM_KEY_PREFIX) else ""
            return int(rest) if rest.isdigit() else None
        return model, model.id, lambda v: f"{CRM_KEY_PREFIX}{v}", from_key
    return model, getattr(model, key_name), str, str


class _Visibility:
    """Which matches a viewer may see (cached per request)."""

    def __init__(self, db: Session, viewer):
        self.db, self.viewer = db, viewer
        self._keys: dict[str, set[str] | None] = {}

    def sees(self, place: str, key: str, vendor_id: int | None = None) -> bool:
        if self.viewer.sees_all:
            return True
        if self.viewer.vendor_id is None or place == DHRUVA:
            return False                     # Dhruva logins: no other section; vendors: never Dhruva
        if place == VTK:
            return vendor_id == self.viewer.vendor_id
        if place not in self._keys:
            self._keys[place] = access.visible_keys(self.db, self.viewer, place)
        return key in self._keys[place]


# ── Per page ───────────────────────────────────────────────────────────
def _matches(db: Session, e164s: set[str]) -> list[tuple[str, str, str, int | None, str | None]]:
    """(place, key, e164, vendor_id, vendor_name) of every lead with one of these phones."""
    from app.models.vendor import Vendor
    from app.models.vendor_to_koc import VendorToKocLead
    from app.services.crm_leads import service

    out: list[tuple] = []
    if not e164s:
        return out
    chunk = sorted(e164s)
    for source in CRM_TABLE_SOURCES:                         # one query per table
        for place, key, e164 in _crm_rows(db, source, lambda m: col(m.phone_e164).in_(chunk)):
            out.append((place, key, e164, None, None))
    try:                                                     # workflow (read-only PostgreSQL), per flow
        for e164, hits in service.workflow_phones(e164s).items():
            out += [(flow, str(i), e164, None, None) for i, flow in hits]
    except service.WorkflowUnavailable:
        logger.warning("also_in: workflow database unavailable; spin workflow leads left out")
    indian = sorted({e[3:] for e in e164s if e.startswith("+91") and len(e) == 13})
    if indian:
        rows = db.exec(select(VendorToKocLead.id, VendorToKocLead.phone, VendorToKocLead.vendor_id, Vendor.name)
                       .join(Vendor, col(Vendor.id) == VendorToKocLead.vendor_id)
                       .where(col(VendorToKocLead.phone).in_(indian)))
        out += [(VTK, str(i), vtk_e164(p), v, name) for i, p, v, name in rows]
    from app.models.dhruva import DhruvaLead
    for lead_id, phone, alt in db.exec(select(DhruvaLead.id, DhruvaLead.phone_e164, DhruvaLead.alt_phone_e164).where(
            or_(col(DhruvaLead.phone_e164).in_(chunk), col(DhruvaLead.alt_phone_e164).in_(chunk)))):
        out += [(DHRUVA, str(lead_id), e, None, None) for e in {phone, alt} if e in e164s]
    return out


def page_phones(db: Session, source: str, keys: list[str]) -> dict[str, str]:
    """{lead key: e164} for leads of one of the 4 sources stored in the CRM (one query)."""
    if not keys:
        return {}
    model, key_col, to_key, from_key = _key_column(source)
    values = [v for v in (from_key(k) for k in keys) if v is not None]
    if not values:
        return {}
    return {to_key(k): e for k, e in db.exec(select(key_col, model.phone_e164).where(col(key_col).in_(values))) if e}


def _phones_of(value) -> set[str]:
    if not value:
        return set()
    return {value} if isinstance(value, str) else {e for e in value if e}


def attach(db: Session, viewer, place: str, items: list[dict], key_of: Callable[[dict], Any],
           e164_of: dict[str, Any]) -> list[dict]:
    """Add "also_in" to every item: [{source, count[, vendors]}] for the OTHER places where the
    item's phone exists and the viewer may see it. e164_of: {item key: e164, or a set of them
    (Dhruva: phone and alt phone)}. A lead found by two of the item's phones counts once."""
    matches = _matches(db, set().union(*(_phones_of(v) for v in e164_of.values())) if e164_of else set())
    by_phone: dict[str, list[tuple]] = {}
    for m in matches:
        by_phone.setdefault(m[2], []).append(m)
    vis = _Visibility(db, viewer)
    for item in items:
        key = str(key_of(item))
        found: dict[str, dict] = {}
        seen: set[tuple[str, str]] = set()
        hits = [m for e in sorted(_phones_of(e164_of.get(key))) for m in by_phone.get(e, [])]
        for other, other_key, _, vendor_id, vendor_name in hits:
            if other == place or (other, other_key) in seen or not vis.sees(other, other_key, vendor_id):
                continue
            seen.add((other, other_key))
            entry = found.setdefault(other, {"source": other, "count": 0})
            entry["count"] += 1
            if other == VTK and viewer.sees_all:
                entry.setdefault("vendors", set()).add(vendor_name)
        item["also_in"] = [
            {**found[p], **({"vendors": sorted(found[p]["vendors"])} if "vendors" in found[p] else {})}
            for p in PLACES if p in found
        ]
    return items


# ── Whole source (the also_in filter) ──────────────────────────────────
def _workflow_index() -> dict[str, tuple[str, str]]:
    """{workflow lead id: (e164, flow)} for every workflow lead (cached)."""
    def build():
        from sqlalchemy import select as sa_select

        from app.api.v1.endpoints import spin_leads as sl
        from app.db.session import read_engine

        if read_engine is None:
            return {}
        table = sl._get_table()
        pk = list(table.primary_key.columns)[0]
        gift = sl.gift_expr(table).label("is_gift")
        with read_engine.connect() as conn:
            return {str(i): (e, sl.WEDDING_GIFTS if g else sl.SPIN)
                    for i, p, g in conn.execute(sa_select(pk, table.c.phone, gift))
                    if (e := phones.normalize(p, lenient=True).e164)}
    try:
        return response_cache.get(("also_in", "workflow"), build, fresh=WORKFLOW_CACHE_SECONDS,
                                  stale=WORKFLOW_CACHE_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("also_in: workflow database unavailable; spin workflow leads left out", exc_info=True)
        return {}


def index(db: Session) -> dict[str, list[tuple[str, str, int | None]]]:
    """{place: [(key, e164, vendor_id)]} for every lead of every place (vendor_id: Vendor -> KOC).
    A Dhruva lead appears once per phone (phone and alt phone)."""
    from app.models.dhruva import DhruvaLead
    from app.models.vendor_to_koc import VendorToKocLead

    out: dict[str, list[tuple[str, str, int | None]]] = {source: [] for source in SOURCES}
    for source in CRM_TABLE_SOURCES:
        for place, key, e164 in _crm_rows(db, source):
            if e164:
                out[place].append((key, e164, None))
    for flow in SPIN_FLOWS:
        out[flow] += [(k, e, None) for k, e in workflow_phone_index(flow).items()]
    out[VTK] = [(str(i), vtk_e164(p), v) for i, p, v in
                db.exec(select(VendorToKocLead.id, VendorToKocLead.phone, VendorToKocLead.vendor_id)) if p]
    out[DHRUVA] = [(str(i), e, None) for i, phone, alt in
                   db.exec(select(DhruvaLead.id, DhruvaLead.phone_e164, DhruvaLead.alt_phone_e164))
                   for e in {phone, alt} if e]
    return out


def workflow_phone_index(flow: str = "spin") -> dict[str, str]:
    """{workflow lead id: e164} for the workflow leads of one flow (spin or wedding_gifts)."""
    return {k: e for k, (e, f) in _workflow_index().items() if f == flow}


def matching_keys(db: Session, viewer, place: str) -> set[str]:
    """Keys of `place` (lead keys; Vendor -> KOC: lead ids as text) whose phone exists in another
    place the viewer may see."""
    idx = index(db)
    vis = _Visibility(db, viewer)
    elsewhere = {e for other in PLACES if other != place
                 for key, e, vendor_id in idx[other] if vis.sees(other, key, vendor_id)}
    return {key for key, e, _ in idx[place] if e in elsewhere}
