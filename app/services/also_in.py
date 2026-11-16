"""Cross-source duplicates: where else a lead's phone exists ("also_in").

Places: the 4 sources (spin workflow and CRM spin leads, whatsapp, wedding, wedding_planner) and
Vendor -> KOC. Phones are compared as E.164 (phone_e164; Vendor -> KOC's 10-digit numbers as
+91…). Archived and removed rows count (like the upload duplicate check).

Visibility: admins see every place. A vendor login only sees places it can see itself: its own
leads in the 4 sources (the same assignment map the lists use, app/services/access.py) and its
own Vendor -> KOC leads. Other vendors' and unassigned leads are never revealed.

  attach(...)       per list page: one query per source for the page's phones
  matching_keys()   the whole source, for the also_in=yes|no filter (lists, stats, exports)
"""
import logging
from collections.abc import Callable
from typing import Any

from sqlmodel import Session, col, select

from app.core import response_cache
from app.services import access, phones

logger = logging.getLogger(__name__)

SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
VTK = "vendor_to_koc"
PLACES = (*SOURCES, VTK)
WORKFLOW_CACHE_SECONDS = 300


def vtk_e164(phone: str | None) -> str | None:
    return f"+91{phone}" if phone else None


def _tables():
    from app.services.crm_leads.service import TABLES
    return TABLES


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
        if self.viewer.is_admin:
            return True
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
    for source in SOURCES:                                   # one query per source
        model, key_col, to_key, _ = _key_column(source)
        for key, e164 in db.exec(select(key_col, model.phone_e164).where(col(model.phone_e164).in_(chunk))):
            out.append((source, to_key(key), e164, None, None))
    try:                                                     # spin workflow (read-only PostgreSQL)
        for e164, ids in service.workflow_phones(e164s).items():
            out += [("spin", str(i), e164, None, None) for i in ids]
    except service.WorkflowUnavailable:
        logger.warning("also_in: workflow database unavailable; spin workflow leads left out")
    indian = sorted({e[3:] for e in e164s if e.startswith("+91") and len(e) == 13})
    if indian:
        rows = db.exec(select(VendorToKocLead.id, VendorToKocLead.phone, VendorToKocLead.vendor_id, Vendor.name)
                       .join(Vendor, col(Vendor.id) == VendorToKocLead.vendor_id)
                       .where(col(VendorToKocLead.phone).in_(indian)))
        out += [(VTK, str(i), vtk_e164(p), v, name) for i, p, v, name in rows]
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


def attach(db: Session, viewer, place: str, items: list[dict], key_of: Callable[[dict], Any],
           e164_of: dict[str, str]) -> list[dict]:
    """Add "also_in" to every item: [{source, count[, vendors]}] for the OTHER places where the
    item's phone exists and the viewer may see it. e164_of: {item key: e164}."""
    matches = _matches(db, {e for e in e164_of.values() if e})
    by_phone: dict[str, list[tuple]] = {}
    for m in matches:
        by_phone.setdefault(m[2], []).append(m)
    vis = _Visibility(db, viewer)
    for item in items:
        key = str(key_of(item))
        e164 = e164_of.get(key)
        found: dict[str, dict] = {}
        for other, other_key, _, vendor_id, vendor_name in by_phone.get(e164, []) if e164 else []:
            if other == place or not vis.sees(other, other_key, vendor_id):
                continue
            entry = found.setdefault(other, {"source": other, "count": 0})
            entry["count"] += 1
            if other == VTK and viewer.is_admin:
                entry.setdefault("vendors", set()).add(vendor_name)
        item["also_in"] = [
            {**found[p], **({"vendors": sorted(found[p]["vendors"])} if "vendors" in found[p] else {})}
            for p in PLACES if p in found
        ]
    return items


# ── Whole source (the also_in filter) ──────────────────────────────────
def _workflow_index() -> dict[str, str]:
    """{spin workflow lead id: e164} for every workflow lead (cached)."""
    def build():
        from sqlalchemy import select as sa_select

        from app.api.v1.endpoints import spin_leads as sl
        from app.db.session import read_engine

        if read_engine is None:
            return {}
        table = sl._get_table()
        pk = list(table.primary_key.columns)[0]
        with read_engine.connect() as conn:
            return {str(i): e for i, p in conn.execute(sa_select(pk, table.c.phone))
                    if (e := phones.normalize(p, lenient=True).e164)}
    try:
        return response_cache.get(("also_in", "workflow"), build, fresh=WORKFLOW_CACHE_SECONDS,
                                  stale=WORKFLOW_CACHE_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("also_in: workflow database unavailable; spin workflow leads left out", exc_info=True)
        return {}


def index(db: Session) -> dict[str, dict[str, tuple[str, int | None]]]:
    """{place: {key: (e164, vendor_id)}} for every lead of every place (vendor_id: Vendor -> KOC)."""
    from app.models.vendor_to_koc import VendorToKocLead

    out: dict[str, dict[str, tuple[str, int | None]]] = {}
    for source in SOURCES:
        model, key_col, to_key, _ = _key_column(source)
        out[source] = {to_key(k): (e, None) for k, e in db.exec(select(key_col, model.phone_e164)) if e}
    out["spin"].update({k: (e, None) for k, e in workflow_phone_index().items()})
    out[VTK] = {str(i): (vtk_e164(p), v) for i, p, v in
                db.exec(select(VendorToKocLead.id, VendorToKocLead.phone, VendorToKocLead.vendor_id)) if p}
    return out


def workflow_phone_index() -> dict[str, str]:
    return _workflow_index()


def matching_keys(db: Session, viewer, place: str) -> set[str]:
    """Keys of `place` (lead keys; Vendor -> KOC: lead ids as text) whose phone exists in another
    place the viewer may see."""
    idx = index(db)
    vis = _Visibility(db, viewer)
    elsewhere = {e for other in PLACES if other != place
                 for key, (e, vendor_id) in idx[other].items() if vis.sees(other, key, vendor_id)}
    return {key for key, (e, _) in idx[place].items() if e in elsewhere}
