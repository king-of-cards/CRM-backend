"""Cross-source duplicates: where else a lead's phone exists ("also_in").

Places: the 4 sources (spin — the live workflow rows, see spin_leads.spin_filter, and every CRM
spin lead —, whatsapp, wedding, wedding_planner), Vendor -> KOC, Dhruva leads and KOC -> Dhruva
(workflow rows of any date, see koc_dhruva). Phones are compared as E.164 (phone_e164; Vendor ->
KOC's 10-digit numbers as +91…; Dhruva: phone and alt phone). Archived and removed rows count (like
the upload duplicate check). Workflow rows before the spin cut-off are never a spin match.

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

SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
SPIN = "spin"
VTK = "vendor_to_koc"
DHRUVA = "dhruva"
KOC_TO_DHRUVA = "koc_to_dhruva"      # workflow spin_lead rows KOC shares with Dhruva (same lead keys)
PLACES = (*SOURCES, VTK, DHRUVA, KOC_TO_DHRUVA)
WORKFLOW_CACHE_SECONDS = 300


def vtk_e164(phone: str | None) -> str | None:
    return f"+91{phone}" if phone else None


def _tables():
    from app.services.crm_leads.service import TABLES
    return TABLES


def _crm_rows(db: Session, source: str, condition=None):
    """(place, lead key, e164) of the CRM-stored leads of a source (crm_spin_leads: spin)."""
    model, key_col, to_key, _ = _key_column(source)
    if source == SPIN:
        query = select(model) if condition is None else select(model).where(condition(model))
        return [(SPIN, lead.lead_key, lead.phone_e164) for lead in db.exec(query)]
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
        if self.viewer.vendor_id is None or place in (DHRUVA, KOC_TO_DHRUVA):
            return False                     # Dhruva logins: no other section; vendors: never Dhruva
        if place == VTK:
            return vendor_id == self.viewer.vendor_id
        if place not in self._keys:
            self._keys[place] = access.visible_keys(self.db, self.viewer, place)
        return key in self._keys[place]


# ── Per page ───────────────────────────────────────────────────────────
def _koc_copies(db: Session, workflow: list[tuple[str, str]]) -> list[tuple]:
    """KOC -> Dhruva has no phones of its own: its leads are workflow rows (same keys, any date).
    workflow: (lead id, e164) of every workflow row found."""
    from app.services import koc_dhruva

    koc_keys = koc_dhruva.keys(db)
    return [(KOC_TO_DHRUVA, key, e164, None, None) for key, e164 in workflow if key in koc_keys]


def _matches(db: Session, e164s: set[str], include_koc: bool = False) -> list[tuple[str, str, str, int | None, str | None]]:
    """(place, key, e164, vendor_id, vendor_name) of every lead with one of these phones.
    include_koc: also KOC -> Dhruva (only admins and view-only logins may see it)."""
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
    workflow: list[tuple[str, str]] = []
    try:                                                     # workflow (read-only PostgreSQL): every row
        for e164, hits in service.workflow_phones(e164s).items():
            workflow += [(str(h.lead_id), e164) for h in hits]
            out += [(SPIN, str(h.lead_id), e164, None, None) for h in hits if h.in_spin]
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
    if include_koc:
        out += _koc_copies(db, workflow)
    return out


def _listed_spin_keys(db: Session) -> set[str]:
    """The keys actually listed in spin (live workflow rows, CRM spin leads not archived): on
    KOC -> Dhruva's own list, a spin match counts only when that lead is in the spin list."""
    return set(access.assignments(db, SPIN))


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
    matches = _matches(db, set().union(*(_phones_of(v) for v in e164_of.values())) if e164_of else set(),
                       include_koc=bool(viewer.sees_all))
    by_phone: dict[str, list[tuple]] = {}
    for m in matches:
        by_phone.setdefault(m[2], []).append(m)
    vis = _Visibility(db, viewer)
    listed = _listed_spin_keys(db) if place == KOC_TO_DHRUVA else None
    for item in items:
        key = str(key_of(item))
        found: dict[str, dict] = {}
        seen: set[tuple[str, str]] = set()
        hits = [m for e in sorted(_phones_of(e164_of.get(key))) for m in by_phone.get(e, [])]
        for other, other_key, _, vendor_id, vendor_name in hits:
            if other == place or (other, other_key) in seen or not vis.sees(other, other_key, vendor_id):
                continue
            if listed is not None and other == SPIN and other_key not in listed:
                continue                     # a lead not in the spin list (KOC -> Dhruva only)
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
def _workflow_index() -> dict[str, tuple[str | None, bool, bool]]:
    """{workflow lead id: (e164 or None, in spin, gift lead)} for every workflow row, any date
    (cached). in spin: listed and created on or after the cut-off (spin_leads.spin_filter)."""
    def build():
        from sqlalchemy import and_, false, func
        from sqlalchemy import select as sa_select

        from app.api.v1.endpoints import spin_leads as sl
        from app.db.session import read_engine

        if read_engine is None:
            return {}
        table = sl._get_table()
        pk = list(table.primary_key.columns)[0]
        gift = func.coalesce(sl.gift_expr(table), false()).label("is_gift")
        in_spin = func.coalesce(and_(*sl.spin_filter(table)), false()).label("in_spin")
        with read_engine.connect() as conn:
            return {str(i): (phones.normalize(p, lenient=True).e164 if p else None, bool(s), bool(g))
                    for i, p, s, g in conn.execute(sa_select(pk, table.c.phone, in_spin, gift))}
    try:
        return response_cache.get(("also_in", "workflow"), build, fresh=WORKFLOW_CACHE_SECONDS,
                                  stale=WORKFLOW_CACHE_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("also_in: workflow database unavailable; spin workflow leads left out", exc_info=True)
        return {}


def index(db: Session, include_koc: bool = False) -> dict[str, list[tuple[str, str, int | None]]]:
    """{place: [(key, e164, vendor_id)]} for every lead of every place (vendor_id: Vendor -> KOC).
    A Dhruva lead appears once per phone (phone and alt phone). include_koc: also KOC -> Dhruva."""
    from app.models.dhruva import DhruvaLead
    from app.models.vendor_to_koc import VendorToKocLead

    out: dict[str, list[tuple[str, str, int | None]]] = {source: [] for source in SOURCES}
    for source in CRM_TABLE_SOURCES:
        for place, key, e164 in _crm_rows(db, source):
            if e164:
                out[place].append((key, e164, None))
    out[SPIN] += [(k, e, None) for k, e in workflow_phone_index(SPIN).items()]
    out[VTK] = [(str(i), vtk_e164(p), v) for i, p, v in
                db.exec(select(VendorToKocLead.id, VendorToKocLead.phone, VendorToKocLead.vendor_id)) if p]
    out[DHRUVA] = [(str(i), e, None) for i, phone, alt in
                   db.exec(select(DhruvaLead.id, DhruvaLead.phone_e164, DhruvaLead.alt_phone_e164))
                   for e in {phone, alt} if e]
    out[KOC_TO_DHRUVA] = []
    if include_koc:
        from app.services import koc_dhruva

        koc_keys = koc_dhruva.keys(db)
        out[KOC_TO_DHRUVA] = [(k, e, None) for k, e in workflow_phone_index("all").items() if k in koc_keys]
    return out


def workflow_phone_index(scope: str = SPIN) -> dict[str, str]:
    """{workflow lead id: e164}: scope "spin" = the rows in spin (listed, on or after the cut-off),
    "all" = every row (KOC -> Dhruva)."""
    return {k: e for k, (e, in_spin, _) in _workflow_index().items() if e and (scope == "all" or in_spin)}


def workflow_row(lead_key: str) -> tuple[str | None, bool, bool] | None:
    """(e164, in spin, gift lead) of one workflow row, or None (unknown, or the workflow database
    can't be read)."""
    return _workflow_index().get(str(lead_key))


def matching_keys(db: Session, viewer, place: str) -> set[str]:
    """Keys of `place` (lead keys; Vendor -> KOC: lead ids as text) whose phone exists in another
    place the viewer may see."""
    idx = index(db, include_koc=bool(viewer.sees_all) or place == KOC_TO_DHRUVA)
    vis = _Visibility(db, viewer)
    listed = _listed_spin_keys(db) if place == KOC_TO_DHRUVA else None
    elsewhere = {e for other in PLACES if other != place
                 for key, e, vendor_id in idx[other] if vis.sees(other, key, vendor_id)
                 and (listed is None or other != SPIN or key in listed)}
    return {key for key, e, _ in idx[place] if e in elsewhere}
