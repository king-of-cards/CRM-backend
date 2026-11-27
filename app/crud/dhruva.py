"""Database access for Dhruva -> KOC leads: duplicates, writes, reads, list filters."""
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import String, case, cast, false, func, insert, or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, delete, select

from app.models.dhruva import DhruvaEventDate, DhruvaLead, DhruvaPhone
from app.models.hot_lead import CrmHotLead
from app.models.user import User
from app.models.vendor_to_koc import CLOSED_STATUSES, UpdateStatus
from app.crud.user import display_name
from app.services.dhruva_import import CleanLead, dates_text, phones_text

DUPLICATE_REASONS = ("in_file", "existing")
# CleanLead field -> column (the rest have the same name)
COLUMN_OF = {"comments": "additional_comments"}
LEAD_COLUMNS = ("name", "event_type", "venue")
ADMIN_COLUMNS = ("whatsapp_status", "response", "message_sent", "follow_up_notes", "comments", "needs_intervention",
                 "update_status", "quoted_amount", "conversion_amount", "follow_up_date")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "")).strip().lower()


# ── Duplicates ─────────────────────────────────────────────────────────
def check_duplicates(db: Session, leads: list[CleanLead], exclude_id: int | None = None) -> list[dict | None]:
    """One entry per lead: None, or {"reason": "existing", "lead_id"} / {"reason": "in_file", "row_index"}.
    A duplicate: any of its phone numbers (last 10 digits) is one of a saved lead's (archived too)
    or of an earlier lead of the list; a lead without a phone: the same name (any case / spacing)
    and first event date as one without a phone."""
    tails = sorted(set().union(*(lead.tails for lead in leads)) if leads else set())
    by_tail: dict[str, int] = {}
    for i in range(0, len(tails), 500):
        stmt = select(DhruvaPhone.phone, DhruvaPhone.lead_id).where(col(DhruvaPhone.phone).in_(tails[i:i + 500]))
        for tail, lead_id in db.exec(stmt):
            if lead_id != exclude_id:
                by_tail.setdefault(tail, lead_id)
    names = sorted({_norm_name(lead.name) for lead in leads if lead.no_phone and lead.name})
    by_name_date: dict[tuple, int] = {}
    for i in range(0, len(names), 500):
        stmt = select(DhruvaLead).where(col(DhruvaLead.id).not_in(select(DhruvaPhone.lead_id)),
                                        func.lower(func.trim(DhruvaLead.name)).in_(names[i:i + 500]))
        for row in db.exec(stmt):
            if row.id != exclude_id:
                by_name_date.setdefault((_norm_name(row.name), row.event_date), row.id)
    seen_tail: dict[str, int] = {}
    seen_name: dict[tuple, int] = {}
    out: list[dict | None] = []
    for index, lead in enumerate(leads):
        dup = None
        if lead.tails:
            hit = next((by_tail[t] for t in sorted(lead.tails) if t in by_tail), None)
            if hit is not None:
                dup = {"reason": "existing", "lead_id": hit}
            else:
                earlier = next((seen_tail[t] for t in sorted(lead.tails) if t in seen_tail), None)
                if earlier is not None:
                    dup = {"reason": "in_file", "row_index": earlier}
            for t in lead.tails:
                seen_tail.setdefault(t, index)
        elif lead.name:
            key = (_norm_name(lead.name), lead.first_date)
            if key in by_name_date:
                dup = {"reason": "existing", "lead_id": by_name_date[key]}
            elif key in seen_name:
                dup = {"reason": "in_file", "row_index": seen_name[key]}
            seen_name.setdefault(key, index)
        out.append(dup)
    return out


# ── Writes ─────────────────────────────────────────────────────────────
def columns_of(lead: CleanLead) -> dict[str, Any]:
    """The lead's columns for the fields it holds (lead.present)."""
    out: dict[str, Any] = {}
    for key in lead.present:
        if key == "event_dates":
            out["event_date"] = lead.first_date
        elif key == "phones":
            out["phone"] = lead.phones[0] if lead.phones else None
            out["phone_e164"] = f"+91{lead.phones[0]}" if lead.phones else None
        else:
            out[COLUMN_OF.get(key, key)] = getattr(lead, key)
    return out


def set_phones(db: Session, row: DhruvaLead, numbers: list[str]) -> None:
    """Replace the lead's phone numbers (not committed); phone / phone_e164 = the first."""
    if row.id is not None:
        db.exec(delete(DhruvaPhone).where(DhruvaPhone.lead_id == row.id))
    for number in numbers:
        db.add(DhruvaPhone(lead_id=row.id, phone=number, phone_e164=f"+91{number}"))
    row.phone = numbers[0] if numbers else None
    row.phone_e164 = f"+91{numbers[0]}" if numbers else None


def set_dates(db: Session, row: DhruvaLead, dates: list[date]) -> None:
    """Replace the lead's event dates (not committed); event_date = the earliest."""
    if row.id is not None:
        db.exec(delete(DhruvaEventDate).where(DhruvaEventDate.lead_id == row.id))
    for d in sorted(set(dates)):
        db.add(DhruvaEventDate(lead_id=row.id, event_date=d))
    row.event_date = min(dates) if dates else None


def _new_row(lead: CleanLead, source: str, user_id: int, now: datetime) -> DhruvaLead:
    """A new lead's row, with event_date / phone / phone_e164 already the earliest date and the
    first number (what set_dates / set_phones give), so no UPDATE follows the INSERT."""
    row = DhruvaLead(**columns_of(lead), source=source, sent_at=now, created_by=user_id, created_at=now,
                     updated_at=now)
    if any(getattr(lead, k) not in (None, False, "") for k in ADMIN_COLUMNS):
        row.status_updated_by, row.status_updated_at = user_id, now
    row.event_date = min(lead.event_dates) if lead.event_dates else None
    row.phone = lead.phones[0] if lead.phones else None
    row.phone_e164 = f"+91{lead.phones[0]}" if lead.phones else None
    return row


def _insert_children(db: Session, pairs: list[tuple[DhruvaLead, CleanLead]]) -> None:
    """New leads' event dates and phone numbers: one multi-row INSERT per table (rows flushed)."""
    dates = [{"lead_id": row.id, "event_date": d} for row, lead in pairs for d in sorted(set(lead.event_dates))]
    numbers = [{"lead_id": row.id, "phone": n, "phone_e164": f"+91{n}"} for row, lead in pairs for n in lead.phones]
    if dates:
        db.execute(insert(DhruvaEventDate), dates)
    if numbers:
        db.execute(insert(DhruvaPhone), numbers)


def create(db: Session, lead: CleanLead, source: str, user_id: int, commit: bool = True) -> DhruvaLead:
    """A new lead; sent_at = now. KOC fields given (an admin upload) count as its first update."""
    row = create_many(db, [lead], source, user_id)[0]
    if commit:
        db.commit()
        db.refresh(row)
    return row


def create_many(db: Session, leads: list[CleanLead], source: str, user_id: int) -> list[DhruvaLead]:
    """New leads (a bulk save), not committed: the rows in one flush, then their dates and phone
    numbers in one insert each (no per-row DELETE / UPDATE / commit)."""
    now = utcnow()
    rows = [_new_row(lead, source, user_id, now) for lead in leads]
    if not rows:
        return []
    db.add_all(rows)
    db.flush()
    _insert_children(db, list(zip(rows, leads)))
    return rows


def update(db: Session, row: DhruvaLead, changes: dict[str, Any], dates: list[date] | None, admin_changed: bool,
           user_id: int, numbers: list[str] | None = None) -> None:
    now = utcnow()
    for key, value in changes.items():
        setattr(row, key, value.value if hasattr(value, "value") else value)
    if dates is not None:
        set_dates(db, row, dates)
    if numbers is not None:
        set_phones(db, row, numbers)
    if admin_changed:
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


def dates_of(db: Session, ids: list[int]) -> dict[int, list[date]]:
    """{lead id: [its event dates, sorted]}."""
    out: dict[int, list[date]] = {i: [] for i in ids}
    for i in range(0, len(ids), 500):
        stmt = select(DhruvaEventDate.lead_id, DhruvaEventDate.event_date).where(
            col(DhruvaEventDate.lead_id).in_(ids[i:i + 500]))
        for lead_id, d in db.exec(stmt):
            out.setdefault(lead_id, []).append(d)
    return {k: sorted(v) for k, v in out.items()}


def phones_of(db: Session, ids: list[int]) -> dict[int, list[str]]:
    """{lead id: [its phone numbers, in the order saved]}."""
    out: dict[int, list[str]] = {i: [] for i in ids}
    for i in range(0, len(ids), 500):
        stmt = select(DhruvaPhone.lead_id, DhruvaPhone.phone).where(
            col(DhruvaPhone.lead_id).in_(ids[i:i + 500])).order_by(DhruvaPhone.id)
        for lead_id, number in db.exec(stmt):
            out.setdefault(lead_id, []).append(number)
    return out


def _iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def values_of(row: DhruvaLead, dates: list[date], numbers: list[str]) -> dict[str, Any]:
    """The lead's field values (as the activity log and the edit compare them)."""
    return {
        "name": row.name, "phones": phones_text(numbers), "event_dates": dates_text(dates),
        "event_type": row.event_type, "venue": row.venue, "whatsapp_status": row.whatsapp_status,
        "response": row.response, "message_sent": row.message_sent, "follow_up_notes": row.follow_up_notes,
        "comments": row.additional_comments, "needs_intervention": bool(row.needs_intervention),
        "update_status": row.update_status, "quoted_amount": row.quoted_amount,
        "conversion_amount": row.conversion_amount, "follow_up_date": row.follow_up_date,
    }


def serialize(row: DhruvaLead, creator_name: str | None, updater_name: str | None, admin: bool,
              expose_status_updater_id: bool = False, is_hot: bool = False, dates: list[date] | None = None,
              numbers: list[str] | None = None) -> dict:
    """The lead object. Dhruva list/detail routes can include the updater id read-only; creator
    ids are limited to admins."""
    dates = sorted(dates) if dates is not None else ([row.event_date] if row.event_date else [])
    numbers = numbers if numbers is not None else ([row.phone] if row.phone else [])
    out = {
        "id": row.id, "name": row.name, "phones": numbers, "phones_e164": [f"+91{n}" for n in numbers],
        "event_dates": [d.isoformat() for d in dates], "event_date": _iso(dates[0]) if dates else None,
        "event_type": row.event_type, "venue": row.venue, "source": row.source, "sent_at": _iso(row.sent_at),
        "whatsapp_status": row.whatsapp_status, "response": row.response, "message_sent": row.message_sent,
        "follow_up_notes": row.follow_up_notes, "comments": row.additional_comments,
        "needs_intervention": bool(row.needs_intervention), "update_status": row.update_status,
        "quoted_amount": row.quoted_amount, "conversion_amount": row.conversion_amount,
        "follow_up_date": _iso(row.follow_up_date), "status_updated_at": _iso(row.status_updated_at),
        "status_updated_by_name": display_name(updater_name, row.status_updated_by),
        "created_by_name": display_name(creator_name, row.created_by), "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at), "archived": bool(row.is_deleted), "is_hot": bool(is_hot),
    }
    if admin:
        out["created_by"] = row.created_by
    if admin or expose_status_updater_id:
        out["status_updated_by"] = row.status_updated_by
    return out


# ── List, stats, export (one filter definition) ────────────────────────
LIKE_ESCAPE = "/"
SORT_COLUMNS = {
    "created_at": DhruvaLead.created_at, "updated_at": DhruvaLead.updated_at, "sent_at": DhruvaLead.sent_at,
    "event_date": DhruvaLead.event_date, "name": DhruvaLead.name, "venue": DhruvaLead.venue,
    "event_type": DhruvaLead.event_type, "update_status": DhruvaLead.update_status,
    "follow_up_date": DhruvaLead.follow_up_date, "whatsapp_status": DhruvaLead.whatsapp_status,
}


@dataclass
class DhruvaFilters:
    search: str | None = None
    created_from: datetime | None = None          # received (created_at), aware UTC, [from, to)
    created_to: datetime | None = None
    event_from: date | None = None                # any of the lead's event dates in [from, to]
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
    needs_intervention: bool | None = None
    ids_in: set[int] | None = None                # also_in=yes
    ids_not_in: set[int] | None = None            # also_in=no
    hot: bool | None = None


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
        parts = [_like(c, term) for c in (lead.name, lead.venue, lead.event_type, lead.whatsapp_status,
                                          lead.response, lead.message_sent, lead.follow_up_notes,
                                          lead.additional_comments)]
        digits = re.sub(r"\D", "", term)
        if len(digits) >= 3:                     # any of the lead's numbers (the last 10 digits)
            parts.append(col(lead.id).in_(select(DhruvaPhone.lead_id).where(
                col(DhruvaPhone.phone).like(f"%{digits[-10:]}%"))))
        conds.append(or_(*parts))
    if f.created_from is not None:
        conds.append(col(lead.created_at) >= f.created_from)
    if f.created_to is not None:
        conds.append(col(lead.created_at) < f.created_to)
    if f.event_from is not None or f.event_to is not None:
        inner = select(DhruvaEventDate.lead_id)
        if f.event_from is not None:
            inner = inner.where(DhruvaEventDate.event_date >= f.event_from)
        if f.event_to is not None:
            inner = inner.where(DhruvaEventDate.event_date <= f.event_to)
        conds.append(col(lead.id).in_(inner))
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
        has = col(lead.id).in_(select(DhruvaPhone.lead_id))
        conds.append(has if f.has_phone else ~has)
    if f.needs_intervention is not None:
        conds.append(col(lead.needs_intervention) == f.needs_intervention)
    if f.hot is not None:
        marked = cast(col(lead.id), String).in_(select(CrmHotLead.lead_key).where(CrmHotLead.source == "dhruva"))
        conds.append(marked if f.hot else ~marked)
    if f.ids_in is not None:
        conds.append(col(lead.id).in_(sorted(f.ids_in)) if f.ids_in else false())
    if f.ids_not_in:
        conds.append(col(lead.id).not_in(sorted(f.ids_not_in)))
    return conds


def order_by(sort: str, order: str) -> list:
    """Empty values last; event_date = the earliest event date."""
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
    ids = [row[0].id for row in rows]
    dates, numbers = dates_of(db, ids), phones_of(db, ids)
    items = [{"sl_no": offset + i + 1, **serialize(*row, admin=admin, expose_status_updater_id=True,
                                                    dates=dates.get(row[0].id, []),
                                                    numbers=numbers.get(row[0].id, []))}
             for i, row in enumerate(rows)]
    from app.services import hot_leads
    hot_leads.attach(db, "dhruva", items, key_of=lambda item: item["id"])
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": math.ceil(total / page_size)}


def all_rows(db: Session, f: DhruvaFilters, sort: str, order: str) -> list[tuple]:
    return list(db.exec(with_names().where(*conditions(f)).order_by(*order_by(sort, order))).all())


def stats(db: Session, f: DhruvaFilters, today: date) -> dict:
    from app.services import kpis

    rows = db.exec(select(DhruvaLead).where(*conditions(f))).all()
    dates = dates_of(db, [r.id for r in rows])
    numbers = phones_of(db, [r.id for r in rows])
    soon = today + timedelta(days=30)
    by_status = {"pending": 0, **{s.value: 0 for s in UpdateStatus}}
    for r in rows:
        by_status[r.update_status or "pending"] = by_status.get(r.update_status or "pending", 0) + 1
    return {
        "total": len(rows),
        "kpis": kpis.compute(rows, payout=False),             # Dhruva: no payout yet
        "by_status": by_status,
        "no_phone": sum(1 for r in rows if not numbers.get(r.id)),
        "no_event_date": sum(1 for r in rows if not dates.get(r.id)),
        "needs_intervention": sum(1 for r in rows if r.needs_intervention),
        "follow_ups_due": sum(1 for r in rows if r.follow_up_date is not None and r.follow_up_date <= today
                              and (r.update_status is None or r.update_status not in CLOSED_STATUSES)),
        "upcoming_30_days": sum(1 for r in rows if any(today <= d <= soon for d in dates.get(r.id, []))),
    }


def filter_options(db: Session) -> dict:
    """Dropdown values over every listed (not archived) lead, most common first."""
    rows = db.exec(select(DhruvaLead.venue, DhruvaLead.event_type, DhruvaLead.update_status, DhruvaLead.id,
                          DhruvaLead.phone, DhruvaLead.source, DhruvaLead.whatsapp_status,
                          DhruvaLead.needs_intervention).where(col(DhruvaLead.is_deleted) == False)).all()  # noqa: E712
    with_numbers = set(db.exec(select(DhruvaPhone.lead_id).distinct()))

    def ranked(counter: dict[str, list]) -> list[dict]:
        # Spellings that differ only in case or spacing are one value, shown as written most often
        out = []
        for spellings in counter.values():
            best = max(set(spellings), key=lambda s: (spellings.count(s), s))
            out.append({"value": best, "count": len(spellings)})
        return sorted(out, key=lambda o: (-o["count"], o["value"].lower()))

    venues: dict[str, list] = {}
    types: dict[str, list] = {}
    whatsapp: dict[str, list] = {}
    for venue, event_type, _status, _p, _a, _source, wa_status, _need in rows:
        if venue:
            venues.setdefault(re.sub(r"\s+", " ", venue).strip().lower(), []).append(venue)
        for part in (event_type or "").split(","):
            part = part.strip()
            if part:
                types.setdefault(part.lower(), []).append(part)
        if wa_status:
            whatsapp.setdefault(re.sub(r"\s+", " ", wa_status).strip().lower(), []).append(wa_status)
    updates = {"pending": 0, **{s.value: 0 for s in UpdateStatus}}
    for r in rows:
        updates[r[2] or "pending"] = updates.get(r[2] or "pending", 0) + 1
    with_phone = sum(1 for r in rows if r[3] in with_numbers)
    needs = sum(1 for r in rows if r[7])
    return {
        "venues": ranked(venues),
        "event_types": ranked(types),
        "whatsapp_statuses": ranked(whatsapp),
        "updates": [{"value": k, "count": n} for k, n in updates.items()],
        "has_phone": [{"value": "yes", "count": with_phone}, {"value": "no", "count": len(rows) - with_phone}],
        "needs_intervention": [{"value": "yes", "count": needs}, {"value": "no", "count": len(rows) - needs}],
        "sources": [{"value": s, "count": sum(1 for r in rows if r[5] == s)} for s in ("manual", "bulk_upload")],
    }
