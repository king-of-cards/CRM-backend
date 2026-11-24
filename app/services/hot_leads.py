from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models.hot_lead import CrmHotLead

def hot_keys(db: Session, source: str, vendor_id: int | None = None) -> set[str]:
    """Leads with a hot flag on any copy (vendor_id: on that vendor's copy; 0 = the vendor-less row)."""
    query = select(CrmHotLead.lead_key).where(CrmHotLead.source == source)
    if vendor_id is not None:
        query = query.where(CrmHotLead.vendor_id == vendor_id)
    return set(db.exec(query).all())


def is_hot(db: Session, source: str, lead_key: str, vendor_id: int | None = None) -> bool:
    """Hot on any copy, or (vendor_id) on that vendor's copy."""
    query = select(CrmHotLead.id).where(CrmHotLead.source == source, CrmHotLead.lead_key == str(lead_key))
    if vendor_id is not None:
        query = query.where(CrmHotLead.vendor_id == vendor_id)
    return db.exec(query).first() is not None


def _iso_utc(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def attach(db: Session, source: str, items: list[dict], key_of) -> list[dict]:
    keys = {str(key_of(item)) for item in items if key_of(item) is not None}
    marked = set()
    if keys:
        marked = set(db.exec(select(CrmHotLead.lead_key).where(
            CrmHotLead.source == source, CrmHotLead.lead_key.in_(keys)
        )).all())
    for item in items:
        item["is_hot"] = str(key_of(item)) in marked if key_of(item) is not None else False
    return items


def set_hot(db: Session, source: str, lead_key: str, actor_user_id: int, hot: bool, vendor_id: int = 0) -> dict:
    """The hot star of one copy (vendor_id; 0 = the vendor-less row, as koc_to_dhruva uses)."""
    row = db.exec(select(CrmHotLead).where(
        CrmHotLead.source == source, CrmHotLead.lead_key == str(lead_key), CrmHotLead.vendor_id == vendor_id
    )).first()
    if hot:
        if row is None:
            row = CrmHotLead(source=source, lead_key=str(lead_key), vendor_id=vendor_id,
                             hot_marked_by=actor_user_id, hot_marked_at=datetime.now(timezone.utc))
            db.add(row)
        else:
            row.hot_marked_by = actor_user_id
            row.hot_marked_at = datetime.now(timezone.utc)
            db.add(row)
    elif row is not None:
        db.delete(row)
    db.commit()
    return {"source": source, "lead_key": str(lead_key), "is_hot": hot, "vendor_id": vendor_id or None,
            "hot_marked_by": actor_user_id if hot else None,
            "hot_marked_at": _iso_utc(row.hot_marked_at) if hot and row else None}


def lead_exists(db: Session, source: str, lead_key: str) -> bool:
    """Check active list membership through the same source access map used for visibility."""
    from app.services import access

    return str(lead_key) in access.assignments(db, source)
