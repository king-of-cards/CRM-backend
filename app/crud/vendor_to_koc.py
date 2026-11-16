"""Database access for Vendor to KOC leads and vendors."""
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import case, func, insert, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_to_koc import AdminLeadRead, LeadSource, VendorLeadRead, VendorRead, VendorToKocLead
from app.models.vendor_user import VendorUser
from app.services.vendor_lead_import import CleanLead

LIKE_ESCAPE = "/"
PHONE_LIKE_RE = re.compile(r"^[\d\s+\-()]*\d[\d\s+\-()]*$")   # digits with phone punctuation only
INSERT_CHUNK = 1000
PHONE_LOOKUP_CHUNK = 1000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DuplicateLead(Exception):
    """This vendor already has a lead with this phone."""


class DuplicateVendorName(Exception):
    pass


# ── Leads: writing ─────────────────────────────────────────────────────
def existing_phones(db: Session, vendor_id: int, phones: set[str]) -> set[str]:
    found: set[str] = set()
    phone_list = sorted(phones)
    for i in range(0, len(phone_list), PHONE_LOOKUP_CHUNK):
        chunk = phone_list[i:i + PHONE_LOOKUP_CHUNK]
        found.update(db.exec(
            select(VendorToKocLead.phone)
            .where(VendorToKocLead.vendor_id == vendor_id, col(VendorToKocLead.phone).in_(chunk))
        ).all())
    return found


def _row(vendor_id: int, lead: CleanLead, source: LeadSource, user_id: int, now: datetime) -> dict:
    return {
        "vendor_id": vendor_id, "lead_date": lead.date, "name": lead.name, "phone": lead.phone,
        "city": lead.city, "source": source.value, "created_by": user_id,
        "created_at": now, "updated_at": now,
    }


def insert_ignoring_duplicates(db: Session, vendor_id: int, leads: list[CleanLead],
                               user_id: int) -> int:
    """Insert valid leads; rows whose (vendor_id, phone) already exists are skipped by the
    database, so a parallel request can't cause an error. Returns the number inserted."""
    now = utcnow()
    stmt = (insert(VendorToKocLead)
            .prefix_with("IGNORE", dialect="mysql")
            .prefix_with("OR IGNORE", dialect="sqlite"))
    added = 0
    for i in range(0, len(leads), INSERT_CHUNK):
        rows = [_row(vendor_id, lead, LeadSource.BULK_UPLOAD, user_id, now)
                for lead in leads[i:i + INSERT_CHUNK]]
        result = db.connection().execute(stmt, rows)   # same transaction as the session
        added += max(result.rowcount, 0)
    db.commit()
    return added


def create_lead(db: Session, vendor_id: int, lead: CleanLead, user_id: int) -> int:
    if existing_phones(db, vendor_id, {lead.phone}):
        raise DuplicateLead()
    row = VendorToKocLead(**_row(vendor_id, lead, LeadSource.MANUAL, user_id, utcnow()))
    db.add(row)
    try:
        db.commit()
    except IntegrityError:          # a parallel request saved the same phone first
        db.rollback()
        raise DuplicateLead() from None
    return row.id


# ── Leads: reading ─────────────────────────────────────────────────────
SORT_COLUMNS = {
    "lead_date": VendorToKocLead.lead_date,
    "name": VendorToKocLead.name,
    "city": VendorToKocLead.city,
    "phone": VendorToKocLead.phone,
    "update_status": VendorToKocLead.update_status,
    "follow_up_date": VendorToKocLead.follow_up_date,
    "created_at": VendorToKocLead.created_at,
    "updated_at": VendorToKocLead.updated_at,
    "vendor_name": Vendor.name,
}


@dataclass
class LeadFilters:
    vendor_ids: list[int] | None = None      # None: no vendor filter
    search: str | None = None
    statuses: list[str] | None = None        # may contain "pending" (= NULL)
    date_from: date | None = None
    date_to: date | None = None
    follow_up_from: date | None = None
    follow_up_to: date | None = None


def _conditions(f: LeadFilters) -> list:
    lead = VendorToKocLead
    conds = []
    if f.vendor_ids is not None:
        conds.append(col(lead.vendor_id).in_(f.vendor_ids))
    term = (f.search or "").strip()
    if term:
        escaped = term.replace("/", "//").replace("%", "/%").replace("_", "/_")
        pattern = f"%{escaped}%"
        matches = [col(lead.name).ilike(pattern, escape=LIKE_ESCAPE),
                   col(lead.city).ilike(pattern, escape=LIKE_ESCAPE),
                   col(lead.phone).like(pattern, escape=LIKE_ESCAPE)]
        digits = re.sub(r"\D", "", term)
        if PHONE_LIKE_RE.match(term) and digits != term:   # "98450 12345" matches the phone too
            matches.append(col(lead.phone).like(f"%{digits}%"))
        conds.append(or_(*matches))
    if f.statuses:
        named = [s for s in f.statuses if s != "pending"]
        options = []
        if named:
            options.append(col(lead.update_status).in_(named))
        if "pending" in f.statuses:
            options.append(col(lead.update_status).is_(None))
        conds.append(or_(*options))
    if f.date_from:
        conds.append(col(lead.lead_date) >= f.date_from)
    if f.date_to:
        conds.append(col(lead.lead_date) <= f.date_to)
    if f.follow_up_from:
        conds.append(col(lead.follow_up_date) >= f.follow_up_from)
    if f.follow_up_to:
        conds.append(col(lead.follow_up_date) <= f.follow_up_to)
    return conds


def _order_by(sort: str, order: str) -> list:
    desc = order == "desc"
    column = SORT_COLUMNS[sort]
    # Empty values last in both directions. MySQL has no NULLS LAST, so sort on
    # "is empty" first; this works on MySQL and SQLite alike.
    return [
        case((column.is_(None), 1), else_=0),
        column.desc() if desc else column.asc(),
        col(VendorToKocLead.id).desc() if desc else col(VendorToKocLead.id).asc(),
    ]


def _lead_dict(lead: VendorToKocLead, vendor_name: str) -> dict:
    return {**lead.model_dump(), "vendor_name": vendor_name}


def serialize(lead: VendorToKocLead, vendor_name: str, admin: bool) -> dict:
    """Admins get AdminLeadRead; vendor users get VendorLeadRead, which has no admin fields."""
    schema = AdminLeadRead if admin else VendorLeadRead
    return schema.model_validate(_lead_dict(lead, vendor_name)).model_dump(mode="json")


def count_leads(db: Session, f: LeadFilters) -> int:
    return db.exec(select(func.count()).select_from(VendorToKocLead).where(*_conditions(f))).one()


def export_rows(db: Session, f: LeadFilters, sort: str, order: str, admin: bool) -> list[tuple]:
    """All matching leads in list order, as tuples. Vendor users get only
    (lead_date, name, phone, city); admin-only columns aren't even selected for them.
    Admins get (vendor_name, lead_date, name, phone, city, update_status,
    additional_comments, follow_up_date, updater_name, updater_email, status_updated_at)."""
    lead = VendorToKocLead
    if admin:
        updater = aliased(User)
        stmt = (
            select(Vendor.name, lead.lead_date, lead.name, lead.phone, lead.city,
                   lead.update_status, lead.additional_comments, lead.follow_up_date,
                   updater.name, updater.email, lead.status_updated_at)
            .join(Vendor, col(Vendor.id) == lead.vendor_id)
            .outerjoin(updater, col(updater.id) == lead.status_updated_by)
        )
    else:
        stmt = (select(lead.lead_date, lead.name, lead.phone, lead.city)
                .join(Vendor, col(Vendor.id) == lead.vendor_id))   # for sort=vendor_name
    return list(db.exec(stmt.where(*_conditions(f)).order_by(*_order_by(sort, order))).all())


def list_leads(db: Session, f: LeadFilters, sort: str, order: str,
               page: int, page_size: int, admin: bool) -> dict:
    conds = _conditions(f)
    total = count_leads(db, f)
    offset = (page - 1) * page_size
    rows = db.exec(
        select(VendorToKocLead, Vendor.name)
        .join(Vendor, col(Vendor.id) == VendorToKocLead.vendor_id)   # one query, no N+1
        .where(*conds)
        .order_by(*_order_by(sort, order))
        .offset(offset)
        .limit(page_size)
    ).all()
    items = [{"sl_no": offset + i + 1, **serialize(lead, vendor_name, admin)}
             for i, (lead, vendor_name) in enumerate(rows)]
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": math.ceil(total / page_size)}


def get_lead(db: Session, lead_id: int, vendor_id: int | None = None
             ) -> tuple[VendorToKocLead, str] | None:
    """The lead and its vendor name. With vendor_id, only that vendor's lead is found."""
    stmt = (select(VendorToKocLead, Vendor.name)
            .join(Vendor, col(Vendor.id) == VendorToKocLead.vendor_id)
            .where(VendorToKocLead.id == lead_id))
    if vendor_id is not None:
        stmt = stmt.where(VendorToKocLead.vendor_id == vendor_id)
    return db.exec(stmt).first()


def update_lead(db: Session, lead: VendorToKocLead, changes: dict, admin_id: int) -> None:
    now = utcnow()
    for key, value in changes.items():
        setattr(lead, key, value.value if hasattr(value, "value") else value)
    lead.status_updated_by = admin_id
    lead.status_updated_at = now
    lead.updated_at = now
    db.add(lead)
    db.commit()


# ── Vendors ────────────────────────────────────────────────────────────
def list_vendors(db: Session) -> list[VendorRead]:
    rows = db.exec(
        select(Vendor, func.count(col(VendorToKocLead.id)))
        .outerjoin(VendorToKocLead, col(VendorToKocLead.vendor_id) == Vendor.id)
        .group_by(col(Vendor.id))
        .order_by(col(Vendor.name))
    ).all()
    return [VendorRead(id=v.id, name=v.name, is_active=v.is_active, lead_count=n) for v, n in rows]


def vendor_read(db: Session, vendor: Vendor) -> VendorRead:
    count = db.exec(select(func.count()).select_from(VendorToKocLead)
                    .where(VendorToKocLead.vendor_id == vendor.id)).one()
    return VendorRead(id=vendor.id, name=vendor.name, is_active=vendor.is_active, lead_count=count)


def _name_taken(db: Session, name: str, except_id: int | None = None) -> bool:
    stmt = select(Vendor.id).where(func.lower(Vendor.name) == name.lower())
    if except_id is not None:
        stmt = stmt.where(Vendor.id != except_id)
    return db.exec(stmt).first() is not None


def _commit_vendor(db: Session, vendor: Vendor) -> Vendor:
    db.add(vendor)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DuplicateVendorName() from None
    db.refresh(vendor)
    return vendor


def create_vendor(db: Session, name: str) -> Vendor:
    if _name_taken(db, name):
        raise DuplicateVendorName()
    now = utcnow()
    return _commit_vendor(db, Vendor(name=name, is_active=True, created_at=now, updated_at=now))


def update_vendor(db: Session, vendor: Vendor, name: str | None, is_active: bool | None) -> Vendor:
    if name is not None:
        if _name_taken(db, name, except_id=vendor.id):
            raise DuplicateVendorName()
        vendor.name = name
    if is_active is not None:
        vendor.is_active = is_active
    vendor.updated_at = utcnow()
    return _commit_vendor(db, vendor)


def link_user(db: Session, user_id: int, vendor_id: int) -> None:
    link = db.get(VendorUser, user_id)
    if link is None:
        db.add(VendorUser(user_id=user_id, vendor_id=vendor_id))
    else:
        link.vendor_id = vendor_id
        db.add(link)
    db.commit()
