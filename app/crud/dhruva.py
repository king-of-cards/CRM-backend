"""Database access for Dhruva leads: duplicates, writes, reads."""
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.models.dhruva import DhruvaLead
from app.models.user import User
from app.services.dhruva_import import CleanLead, name_date_key

DUPLICATE_REASONS = ("in_file", "existing")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Duplicates ─────────────────────────────────────────────────────────
def existing_phones(db: Session, e164s: set[str], exclude_id: int | None = None) -> dict[str, int]:
    """{e164: lead id} for existing leads (archived included) whose phone or alt phone is one of these."""
    found: dict[str, int] = {}
    numbers = sorted(e164s)
    for i in range(0, len(numbers), 500):
        chunk = numbers[i:i + 500]
        stmt = select(DhruvaLead.id, DhruvaLead.phone_e164, DhruvaLead.alt_phone_e164).where(
            or_(col(DhruvaLead.phone_e164).in_(chunk), col(DhruvaLead.alt_phone_e164).in_(chunk)))
        for lead_id, phone, alt in db.exec(stmt):
            if lead_id == exclude_id:
                continue
            for e in (phone, alt):
                if e in e164s:
                    found.setdefault(e, lead_id)
    return found


def existing_name_dates(db: Session, exclude_id: int | None = None) -> dict[tuple, int]:
    """{(name, date) key: lead id} over every existing lead with a name (archived included)."""
    out: dict[tuple, int] = {}
    stmt = select(DhruvaLead.id, DhruvaLead.name, DhruvaLead.event_date, DhruvaLead.event_date_text).where(
        col(DhruvaLead.name).is_not(None))
    for lead_id, name, event_date, text in db.exec(stmt):
        key = name_date_key(name, event_date, text)
        if key and lead_id != exclude_id:
            out.setdefault(key, lead_id)
    return out


def check_duplicates(db: Session, leads: list[CleanLead], exclude_id: int | None = None) -> list[dict | None]:
    """One entry per lead: None, or {"reason": "existing", "lead_id"} / {"reason": "in_file", "row_index"}.
    With a phone: phone or alt phone matches an existing lead or an earlier lead of the list.
    Without a phone: the same name and event date."""
    phones_found = existing_phones(db, set().union(*(l.e164s for l in leads)) if leads else set(), exclude_id)
    name_dates = existing_name_dates(db, exclude_id) if any(l.no_phone for l in leads) else {}
    seen_phone: dict[str, int] = {}
    seen_name: dict[tuple, int] = {}
    out: list[dict | None] = []
    for index, lead in enumerate(leads):
        dup = None
        if not lead.no_phone:
            hit = next((phones_found[e] for e in sorted(lead.e164s) if e in phones_found), None)
            if hit is not None:
                dup = {"reason": "existing", "lead_id": hit}
            else:
                earlier = next((seen_phone[e] for e in sorted(lead.e164s) if e in seen_phone), None)
                if earlier is not None:
                    dup = {"reason": "in_file", "row_index": earlier}
            for e in lead.e164s:
                seen_phone.setdefault(e, index)
        else:
            key = name_date_key(lead.name, lead.event_date, lead.event_date_text)
            if key is not None:
                if key in name_dates:
                    dup = {"reason": "existing", "lead_id": name_dates[key]}
                elif key in seen_name:
                    dup = {"reason": "in_file", "row_index": seen_name[key]}
                seen_name.setdefault(key, index)
        out.append(dup)
    return out


# ── Writes ─────────────────────────────────────────────────────────────
def lead_columns(lead: CleanLead) -> dict[str, Any]:
    return {"name": lead.name, "venue": lead.venue, "event_date": lead.event_date,
            "event_date_text": lead.event_date_text, "phone": lead.phone, "alt_phone": lead.alt_phone,
            "phone_e164": lead.phone_e164, "alt_phone_e164": lead.alt_phone_e164, "event_type": lead.event_type}


def create(db: Session, lead: CleanLead, source: str, user_id: int, commit: bool = True) -> DhruvaLead:
    now = utcnow()
    row = DhruvaLead(**lead_columns(lead), source=source, created_by=user_id, created_at=now, updated_at=now)
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def update(db: Session, row: DhruvaLead, basic: dict[str, Any], admin_changes: dict[str, Any], user_id: int) -> None:
    now = utcnow()
    for key, value in basic.items():
        setattr(row, key, value)
    if admin_changes:
        for key, value in admin_changes.items():
            setattr(row, key, value.value if hasattr(value, "value") else value)
        row.status_updated_by = user_id
        row.status_updated_at = now
    row.updated_at = now
    db.add(row)
    db.commit()
    db.refresh(row)


def set_archived(db: Session, row: DhruvaLead, archived: bool) -> None:
    row.is_deleted = archived
    row.updated_at = utcnow()
    db.add(row)
    db.commit()


# ── Reads ──────────────────────────────────────────────────────────────
def with_names():
    """(lead, creator name, updater name): one query, no N+1."""
    creator, updater = aliased(User), aliased(User)
    return (select(DhruvaLead, creator.name, updater.name)
            .outerjoin(creator, col(creator.id) == DhruvaLead.created_by)
            .outerjoin(updater, col(updater.id) == DhruvaLead.status_updated_by))


def get(db: Session, lead_id: int) -> tuple[DhruvaLead, str | None, str | None] | None:
    return db.exec(with_names().where(DhruvaLead.id == lead_id)).first()


def _iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def serialize(row: DhruvaLead, creator_name: str | None, updater_name: str | None, admin: bool) -> dict:
    """The lead object. Everyone gets the admin fields (Dhruva logins read-only); user ids of the
    updater only for admins."""
    out = {
        "id": row.id, "name": row.name, "venue": row.venue,
        "event_date": _iso(row.event_date), "event_date_text": row.event_date_text,
        "phone": row.phone, "alt_phone": row.alt_phone, "phone_e164": row.phone_e164,
        "alt_phone_e164": row.alt_phone_e164, "event_type": row.event_type, "source": row.source,
        "created_by_name": creator_name, "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
        "archived": bool(row.is_deleted),
        "update_status": row.update_status, "additional_comments": row.additional_comments,
        "quoted_amount": row.quoted_amount, "conversion_amount": row.conversion_amount,
        "follow_up_date": _iso(row.follow_up_date), "status_updated_at": _iso(row.status_updated_at),
        "status_updated_by_name": updater_name,
    }
    if admin:
        out["created_by"] = row.created_by
        out["status_updated_by"] = row.status_updated_by
    return out
