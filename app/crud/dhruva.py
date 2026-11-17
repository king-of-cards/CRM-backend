"""Database access for Dhruva leads: duplicates, writes, reads."""
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import case, false, func, or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.models.dhruva import DhruvaLead
from app.models.user import User
from app.models.vendor_to_koc import CLOSED_STATUSES, UpdateStatus
from app.services.dhruva_import import CleanLead

DUPLICATE_REASONS = ("in_file", "existing")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Duplicates ─────────────────────────────────────────────────────────
def _duplicate_text(value: str | None) -> str:
    """Normalize the four identifying text fields for Dhruva's duplicate rule."""
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _duplicate_details(lead: CleanLead) -> tuple[str, str, str, str]:
    return tuple(_duplicate_text(value) for value in
                 (lead.name, lead.venue, lead.event_date_text, lead.event_type))


def _matching_existing(db: Session, leads: list[CleanLead], exclude_id: int | None = None) -> list[DhruvaLead]:
    e164s = set().union(*(lead.e164s for lead in leads)) if leads else set()
    rows: list[DhruvaLead] = []
    numbers = sorted(e164s)
    for i in range(0, len(numbers), 500):
        chunk = numbers[i:i + 500]
        stmt = select(DhruvaLead).where(
            or_(col(DhruvaLead.phone_e164).in_(chunk), col(DhruvaLead.alt_phone_e164).in_(chunk)))
        rows.extend(row for row in db.exec(stmt) if row.id != exclude_id)
    return rows


def check_duplicates(db: Session, leads: list[CleanLead], exclude_id: int | None = None) -> list[dict | None]:
    """One entry per lead: None, or {"reason": "existing", "lead_id"} / {"reason": "in_file", "row_index"}.
    A phone/alt phone match is a duplicate only when name, venue, date text, and event type also
    match after whitespace and case normalization. Leads without a phone are never duplicates."""
    existing = _matching_existing(db, leads, exclude_id)
    seen: dict[tuple[str, tuple[str, str, str, str]], int] = {}
    out: list[dict | None] = []
    for index, lead in enumerate(leads):
        dup = None
        details = _duplicate_details(lead)
        if lead.e164s:
            hit = next((row for row in existing
                        if lead.e164s.intersection({row.phone_e164, row.alt_phone_e164})
                        and details == tuple(_duplicate_text(value) for value in
                            (row.name, row.venue, row.event_date_text, row.event_type))), None)
            if hit is not None:
                dup = {"reason": "existing", "lead_id": hit.id}
            else:
                earlier = next((seen[(phone, details)] for phone in sorted(lead.e164s)
                                if (phone, details) in seen), None)
                if earlier is not None:
                    dup = {"reason": "in_file", "row_index": earlier}
            for phone in lead.e164s:
                seen.setdefault((phone, details), index)
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


def serialize(row: DhruvaLead, creator_name: str | None, updater_name: str | None, admin: bool,
             expose_status_updater_id: bool = False) -> dict:
    """The lead object. Dhruva list/detail routes can include the updater id read-only; creator
    ids are limited to admins."""
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
    if admin or expose_status_updater_id:
        out["status_updated_by"] = row.status_updated_by
    return out


# ── List, stats, export (one filter definition) ────────────────────────
LIKE_ESCAPE = "/"
SORT_COLUMNS = {
    "created_at": DhruvaLead.created_at, "updated_at": DhruvaLead.updated_at, "event_date": DhruvaLead.event_date,
    "name": DhruvaLead.name, "venue": DhruvaLead.venue, "event_type": DhruvaLead.event_type,
    "update_status": DhruvaLead.update_status, "follow_up_date": DhruvaLead.follow_up_date,
}


@dataclass
class DhruvaFilters:
    search: str | None = None
    created_from: datetime | None = None          # received (created_at), aware UTC, [from, to)
    created_to: datetime | None = None
    event_from: date | None = None
    event_to: date | None = None
    venue: str | None = None                      # contains
    event_types: list[str] = field(default_factory=list)   # contains, OR
    statuses: list[str] = field(default_factory=list)      # may contain "pending"
    quote: tuple[str, int | None] | None = None
    conversion: tuple[str, int | None] | None = None
    follow_up_from: date | None = None
    follow_up_to: date | None = None
    follow_up_due_by: date | None = None
    has_phone: bool | None = None
    ids_in: set[int] | None = None                # also_in=yes
    ids_not_in: set[int] | None = None            # also_in=no


def _like(column, text: str):
    escaped = text.replace("/", "//").replace("%", "/%").replace("_", "/_")
    return col(column).ilike(f"%{escaped}%", escape=LIKE_ESCAPE)


def _amount(column, quote: tuple[str, int | None]):
    op, value = quote
    if op == "has":
        return col(column).is_not(None)
    if op == "none":
        return col(column).is_(None)
    return col(column) > value if op == "gt" else col(column) < value


def conditions(f: DhruvaFilters) -> list:
    lead = DhruvaLead
    conds = [col(lead.is_deleted) == False]  # noqa: E712
    term = (f.search or "").strip()
    if term:
        parts = [_like(c, term) for c in (lead.name, lead.venue, lead.event_type, lead.event_date_text,
                                          lead.phone, lead.alt_phone)]
        digits = re.sub(r"\D", "", term)
        if len(digits) >= 3:
            parts += [col(lead.phone_e164).like(f"%{digits}%"), col(lead.alt_phone_e164).like(f"%{digits}%")]
        conds.append(or_(*parts))
    if f.created_from is not None:
        conds.append(col(lead.created_at) >= f.created_from)
    if f.created_to is not None:
        conds.append(col(lead.created_at) < f.created_to)
    if f.event_from is not None:
        conds.append(col(lead.event_date) >= f.event_from)
    if f.event_to is not None:
        conds.append(col(lead.event_date) <= f.event_to)
    if f.venue:
        conds.append(_like(lead.venue, f.venue))
    if f.event_types:
        conds.append(or_(*(_like(lead.event_type, t) for t in f.event_types)))
    if f.statuses:
        named = [s for s in f.statuses if s != "pending"]
        options = [col(lead.update_status).in_(named)] if named else []
        if "pending" in f.statuses:
            options.append(col(lead.update_status).is_(None))
        conds.append(or_(*options))
    if f.quote:
        conds.append(_amount(lead.quoted_amount, f.quote))
    if f.conversion:
        conds.append(_amount(lead.conversion_amount, f.conversion))
    if f.follow_up_from:
        conds.append(col(lead.follow_up_date) >= f.follow_up_from)
    if f.follow_up_to:
        conds.append(col(lead.follow_up_date) <= f.follow_up_to)
    if f.follow_up_due_by is not None:
        conds.append(col(lead.follow_up_date) <= f.follow_up_due_by)
        conds.append(or_(col(lead.update_status).is_(None), col(lead.update_status).not_in(sorted(CLOSED_STATUSES))))
    if f.has_phone is not None:
        has = or_(col(lead.phone_e164).is_not(None), col(lead.alt_phone_e164).is_not(None))
        conds.append(has if f.has_phone else ~has)
    if f.ids_in is not None:
        conds.append(col(lead.id).in_(sorted(f.ids_in)) if f.ids_in else false())
    if f.ids_not_in:
        conds.append(col(lead.id).not_in(sorted(f.ids_not_in)))
    return conds


def order_by(sort: str, order: str) -> list:
    column = SORT_COLUMNS[sort]
    desc = order == "desc"
    return [case((column.is_(None), 1), else_=0), column.desc() if desc else column.asc(),
            col(DhruvaLead.id).desc() if desc else col(DhruvaLead.id).asc()]


def count(db: Session, f: DhruvaFilters) -> int:
    return db.exec(select(func.count()).select_from(DhruvaLead).where(*conditions(f))).one()


def list_page(db: Session, f: DhruvaFilters, sort: str, order: str, page: int, page_size: int, admin: bool) -> dict:
    total = count(db, f)
    offset = (page - 1) * page_size
    rows = db.exec(with_names().where(*conditions(f)).order_by(*order_by(sort, order)).offset(offset).limit(page_size)).all()
    items = [{"sl_no": offset + i + 1, **serialize(*row, admin=admin, expose_status_updater_id=True)}
             for i, row in enumerate(rows)]
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": math.ceil(total / page_size)}


def all_rows(db: Session, f: DhruvaFilters, sort: str, order: str) -> list[tuple]:
    return list(db.exec(with_names().where(*conditions(f)).order_by(*order_by(sort, order))).all())


def stats(db: Session, f: DhruvaFilters, today: date) -> dict:
    from app.services import kpis

    rows = db.exec(select(DhruvaLead).where(*conditions(f))).all()
    by_status = {"pending": 0, **{s.value: 0 for s in UpdateStatus}}
    for r in rows:
        by_status[r.update_status or "pending"] = by_status.get(r.update_status or "pending", 0) + 1
    return {
        "total": len(rows),
        "kpis": kpis.compute(rows),
        "by_status": by_status,
        "no_phone": sum(1 for r in rows if not r.phone_e164 and not r.alt_phone_e164),
        "follow_ups_due": sum(1 for r in rows if r.follow_up_date is not None and r.follow_up_date <= today
                              and (r.update_status is None or r.update_status not in CLOSED_STATUSES)),
        "upcoming_30_days": sum(1 for r in rows if r.event_date is not None
                                and today <= r.event_date <= date.fromordinal(today.toordinal() + 30)),
    }


def filter_options(db: Session) -> dict:
    """Dropdown values over every listed (not archived) lead, most common first."""
    rows = db.exec(select(DhruvaLead.venue, DhruvaLead.event_type, DhruvaLead.update_status, DhruvaLead.phone_e164,
                          DhruvaLead.alt_phone_e164, DhruvaLead.source).where(col(DhruvaLead.is_deleted) == False)).all()  # noqa: E712

    def ranked(counter: dict[str, list]) -> list[dict]:
        # Spellings that differ only in case or spacing are one value, shown as written most often
        out = []
        for spellings in counter.values():
            best = max(set(spellings), key=lambda s: (spellings.count(s), s))
            out.append({"value": best, "count": len(spellings)})
        return sorted(out, key=lambda o: (-o["count"], o["value"].lower()))

    venues: dict[str, list] = {}
    types: dict[str, list] = {}
    for venue, event_type, *_ in rows:
        if venue:
            venues.setdefault(re.sub(r"\s+", " ", venue).strip().lower(), []).append(venue)
        for part in (event_type or "").split(","):
            part = part.strip()
            if part:
                types.setdefault(part.lower(), []).append(part)
    updates = {"pending": 0, **{s.value: 0 for s in UpdateStatus}}
    for r in rows:
        updates[r[2] or "pending"] = updates.get(r[2] or "pending", 0) + 1
    with_phone = sum(1 for r in rows if r[3] or r[4])
    return {
        "venues": ranked(venues),
        "event_types": ranked(types),
        "updates": [{"value": k, "count": n} for k, n in updates.items()],
        "has_phone": [{"value": "yes", "count": with_phone}, {"value": "no", "count": len(rows) - with_phone}],
        "sources": [{"value": s, "count": sum(1 for r in rows if r[5] == s)} for s in ("manual", "bulk_upload")],
    }
