"""Branches (stores) for spin leads: defaults, import from the workflow database, lookups.

The 12 stores below come from the workflow database's spin_branch table (export of
13 Nov 2026). They are created once in crm_branches; after that, edit them on the Branches
page. import_from_workflow() adds any new spin_branch rows automatically.

Vendor of a branch (and so of its spin leads):
  - the vendor picked for the branch on the Branches page, if any;
  - otherwise AUTOMATIC: the vendors' area rules applied to the branch's name and city
    (RJ: Chamrajpet; Phodeo: Bangalore; OHP: everything else). So a new store in
    Bangalore goes to Phodeo by itself, and changing a vendor's rules updates its branches.
"""
import logging
import threading
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models.branch import Branch
from app.models.vendor import Vendor

logger = logging.getLogger(__name__)

# id = spin_branch.id (the number stored on each spin lead)
DEFAULT_BRANCHES = [
    {"id": 1, "name": "Chamrajpet", "code": "CHA-001", "slug": "chamrajpet", "city": "Bangalore", "vendor": "RJ"},
    {"id": 2, "name": "HSR Layout", "code": "HSR 002", "slug": "hsr-layout", "city": "Bangalore", "vendor": "Phodeo"},
    {"id": 3, "name": "Hoodi(whitefield)", "code": "Hoodi-003", "slug": "hoodi-whitefield", "city": "Bangalore", "vendor": "Phodeo"},
    {"id": 4, "name": "Jayanagar", "code": "JNG-004", "slug": "jayanagar", "city": "Bangalore", "vendor": "Phodeo"},
    {"id": 5, "name": "Bommasandra", "code": "BMSD-005", "slug": "bommasandra", "city": "Bangalore", "vendor": "Phodeo"},
    {"id": 6, "name": "Sahakar Nagar", "code": "SKN-006", "slug": "sahakar-nagar", "city": "Bangalore", "vendor": "Phodeo"},
    {"id": 7, "name": "Hyderabad - Kondhapur", "code": "HYB-007", "slug": "hyderabad", "city": "Hyderabad", "vendor": "OHP"},
    {"id": 8, "name": "Visakhapatnam", "code": "VIZ-008", "slug": "visakhapatnam", "city": "Visakhapatnam", "vendor": "OHP"},
    {"id": 9, "name": "Hubballi", "code": "HUBL-009", "slug": "hubballi", "city": "Hubballi", "vendor": "OHP"},
    {"id": 10, "name": "Mysore", "code": "MYS-010", "slug": "mysore", "city": "Mysore", "vendor": "OHP"},
    {"id": 11, "name": "Chitradurga", "code": "CTRD-011", "slug": "chitradurga", "city": "Chitradurga", "vendor": "OHP"},
    {"id": 12, "name": "Banjara Hills Hyderabad", "code": "BANJARA-009", "slug": "banjara-hills-hyderabad", "city": "Hyderabad", "vendor": "OHP"},
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def seed_branches(session: Session) -> int:
    """Create the default stores that don't exist yet. Returns how many were added.
    Their vendor is left automatic (area rules), which gives RJ / Phodeo / OHP as listed."""
    existing = {b.id for b in session.exec(select(Branch))}
    now = _now()
    added = 0
    for b in DEFAULT_BRANCHES:
        if b["id"] in existing:
            continue
        data = {k: v for k, v in b.items() if k != "vendor"}
        session.add(Branch(**data, vendor_id=None, source="workflow", created_at=now, updated_at=now))
        added += 1
    if added:
        session.commit()
    return added


def import_from_workflow(session: Session) -> dict:
    """Add stores from the workflow database's spin_branch table that aren't here yet.
    Existing stores are left as they are (your edits win). Returns {"added": n, "available": bool}."""
    from app.db.session import read_engine

    if read_engine is None:
        return {"added": 0, "available": False}
    try:
        from sqlalchemy import MetaData, Table
        from sqlalchemy import select as sa_select

        from app.core.config import settings

        table = Table("spin_branch", MetaData(), autoload_with=read_engine, schema=settings.READ_DB_SCHEMA or None)
        with read_engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(sa_select(table))]
    except Exception:  # noqa: BLE001
        logger.warning("Branch import: spin_branch not readable", exc_info=True)
        return {"added": 0, "available": False}

    existing = {b.id for b in session.exec(select(Branch))}
    now = _now()
    added = 0
    for r in rows:
        if r.get("id") in existing or r.get("deleted"):
            continue
        session.add(
            Branch(
                id=r["id"],
                name=(r.get("name") or f"Branch {r['id']}").strip(),
                code=r.get("code"),
                slug=r.get("slug"),
                address=r.get("address"),
                contact_number=r.get("contact_number"),
                manager_name=r.get("manager_name"),
                is_active=bool(r.get("active", True)),
                source="workflow",
                created_at=now,
                updated_at=now,
            )
        )
        added += 1
    if added:
        session.commit()
    return {"added": added, "available": True}


# Small cache of branches for attaching to every spin lead page
_cache: dict = {"at": None, "by_id": {}}
_lock = threading.Lock()


def auto_vendor(vendors: list[Vendor], branch: Branch) -> Vendor | None:
    """The vendor the area rules give a branch, from its name and city."""
    from app.services.lead_crm import suggest_vendor

    location = ", ".join(p for p in (branch.name, branch.address, branch.city) if p)
    return suggest_vendor(vendors, location)


def branches_by_id(session: Session, max_age: float = 30) -> dict[str, dict]:
    """{"1": {"id": 1, "name": "Chamrajpet", "code": ..., "city": ..., "vendor_id": ..., "vendor_auto": bool}}
    vendor_id is the branch's own vendor, or the automatic one from the area rules."""
    import time

    from app.services.lead_crm import active_vendors

    with _lock:
        if _cache["at"] is not None and time.monotonic() - _cache["at"] < max_age:
            return _cache["by_id"]
    vendors = active_vendors(session)
    active_ids = {v.id for v in vendors}
    by_id = {}
    for b in session.exec(select(Branch)):
        fixed = b.vendor_id if b.vendor_id in active_ids else None
        auto = None if fixed else auto_vendor(vendors, b)
        by_id[str(b.id)] = {
            "id": b.id,
            "name": b.name,
            "code": b.code,
            "city": b.city,
            "vendor_id": fixed or (auto.id if auto else None),
            "vendor_auto": fixed is None,
            "is_active": b.is_active,
        }
    with _lock:
        _cache.update(at=time.monotonic(), by_id=by_id)
    return by_id


def forget_cache() -> None:
    with _lock:
        _cache["at"] = None