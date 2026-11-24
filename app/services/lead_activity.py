import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from app.models.lead_activity import LeadActivity

logger = logging.getLogger(__name__)
PAGE_SIZE = 100
VENDOR_CRM_FIELDS = {"update_status", "quoted_date", "quoted_amount", "conversion_amount", "notes", "follow_up_date"}
VTK_VISIBLE_FIELDS = {"name", "phone", "city", "lead_date", "source", "update_status", "additional_comments",
                      "follow_up_date", "quoted_amount", "conversion_amount", "status_updated_at"}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    return str(value)[:1000]


def log_changes(db: Session, *, source: str, lead_key: str, changes: dict[str, tuple[Any, Any]],
                actor_user_id: int | None, actor_name: str, actor_role: str,
                action: str = "field_changed", vendor_id: int | None = None) -> None:
    """Best-effort audit insert; callers must commit the business change first.
    vendor_id: the vendor copy the change concerns (None: the lead itself)."""
    rows = []
    now = datetime.now(timezone.utc)
    for field, (old, new) in changes.items():
        if old == new:
            continue
        rows.append(LeadActivity(source=source, lead_key=str(lead_key), action=action, field=field,
                                 old_value=_text(old), new_value=_text(new), actor_user_id=actor_user_id,
                                 actor_name=(actor_name or "Unknown")[:100], actor_role=actor_role,
                                 created_at=now, vendor_id=vendor_id or None))
    if not rows:
        return
    try:
        db.add_all(rows)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not write lead activity for %s/%s", source, lead_key)


def log_action(db: Session, *, source: str, lead_key: str, action: str, field: str | None,
               old: Any, new: Any, actor_user_id: int | None, actor_name: str, actor_role: str,
               vendor_id: int | None = None) -> None:
    log_changes(db, source=source, lead_key=lead_key,
                changes={field or "lead": (old, new)}, actor_user_id=actor_user_id,
                actor_name=actor_name, actor_role=actor_role, action=action, vendor_id=vendor_id)


def log_actions(db: Session, entries: list[dict[str, Any]], *, actor_user_id: int | None, actor_name: str,
                actor_role: str) -> None:
    """Many log_action entries (bulk saves) in one insert and one commit, instead of one commit
    each. Each entry: source, lead_key, action, field, old, new[, vendor_id]. Best effort, like
    log_changes; callers must commit the business change first."""
    from sqlalchemy import insert

    now = datetime.now(timezone.utc)
    rows = [{"source": e["source"], "lead_key": str(e["lead_key"]), "action": e["action"],
             "field": e.get("field") or "lead", "old_value": _text(e.get("old")), "new_value": _text(e.get("new")),
             "actor_user_id": actor_user_id, "actor_name": (actor_name or "Unknown")[:100],
             "actor_role": actor_role, "created_at": now, "vendor_id": e.get("vendor_id") or None}
            for e in entries if e.get("old") != e.get("new")]
    if not rows:
        return
    try:
        db.execute(insert(LeadActivity), rows)           # one multi-row INSERT (executemany)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not write %d lead activity rows", len(rows))


def list_activity(db: Session, *, source: str, lead_key: str, page: int, page_size: int,
                  allowed_fields: set[str] | None = None, vendor_id: int | None = None) -> dict:
    """vendor_id (vendor logins): only the entries of that vendor's copy and of the lead itself."""
    from sqlalchemy import or_

    def narrow(query):
        query = query.where(LeadActivity.source == source, LeadActivity.lead_key == str(lead_key))
        if allowed_fields is not None:
            query = query.where(LeadActivity.field.in_(allowed_fields))
        if vendor_id is not None:
            query = query.where(or_(LeadActivity.vendor_id.is_(None), LeadActivity.vendor_id == vendor_id))
        return query

    query = narrow(select(LeadActivity))
    count_query = narrow(select(func.count()).select_from(LeadActivity))
    total = db.exec(count_query).one()
    rows = list(db.exec(query.order_by(LeadActivity.created_at.desc(), LeadActivity.id.desc())
                        .offset((page - 1) * page_size).limit(page_size)))
    return {"items": [{"actor_name": r.actor_name, "actor_role": r.actor_role, "action": r.action,
                        "vendor_id": r.vendor_id,
                        "field": r.field, "old_value": r.old_value, "new_value": r.new_value,
                        "created_at": (r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None
                                       else r.created_at).isoformat()} for r in rows],
            "total": total, "page": page, "page_size": page_size,
            "pages": (total + page_size - 1) // page_size}


def require_visible(db: Session, viewer, source: str, lead_key: str) -> None:
    from app.services import access
    if not access.can_see(db, viewer, source, lead_key):
        raise HTTPException(404, "Lead not found")
