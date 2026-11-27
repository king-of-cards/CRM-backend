"""Database access for Vendor to KOC leads and vendors."""
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import case, false, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_to_koc import (
    CLOSED_STATUSES, AdminLeadRead, LeadSource, UpdateStatus, VendorLeadRead, VendorRead, VendorToKocLead,
)
from app.models.vendor_user import VendorUser
from app.services.vendor_lead_import import CleanLead, signature

LIKE_ESCAPE = "/"
PHONE_LIKE_RE = re.compile(r"^[\d\s+\-()]*\d[\d\s+\-()]*$")   # digits with phone punctuation only
PHONE_LOOKUP_CHUNK = 1000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DuplicateLead(Exception):
    """This vendor already has this lead (the same phone, name, date and city)."""


class DuplicateVendorName(Exception):
    pass


# ── Leads: writing ─────────────────────────────────────────────────────
class LeadKeys:
    """(phone, name, date, city) of leads, looked up the duplicate way: the same phone and no
    conflicting field (a field empty on either side is ignored; lead_values.compatible)."""

    def __init__(self, keys=()):
        self._by_phone: dict[str, list[tuple]] = {}
        for key in keys:
            self.add(key)

    def add(self, key: tuple) -> None:
        self._by_phone.setdefault(key[0], []).append(key[1:])

    def __contains__(self, key: tuple) -> bool:
        from app.services.lead_values import compatible

        return any(compatible(key[1:], other) for other in self._by_phone.get(key[0], []))


def existing_keys(db: Session, vendor_id: int, phones: set[str]) -> LeadKeys:
    """This vendor's leads with these phones: a new lead is a duplicate when its phone matches and
    none of its filled fields (name, date, city) differs from one of them."""
    found = LeadKeys()
    phone_list = sorted(phones)
    for i in range(0, len(phone_list), PHONE_LOOKUP_CHUNK):
        chunk = phone_list[i:i + PHONE_LOOKUP_CHUNK]
        for row in db.exec(select(VendorToKocLead).where(VendorToKocLead.vendor_id == vendor_id,
                                                         col(VendorToKocLead.phone).in_(chunk))):
            found.add(lead_key(CleanLead(name=row.name, date=row.lead_date, date_display=None, phone=row.phone,
                                         city=row.city)))
    return found


def lead_key(lead: CleanLead) -> tuple:
    return (lead.phone, *signature(lead))


def _row(vendor_id: int, lead: CleanLead, source: LeadSource, user_id: int, now: datetime) -> dict:
    return {
        "vendor_id": vendor_id, "lead_date": lead.date, "name": lead.name, "phone": lead.phone,
        "city": lead.city, "source": source.value, "created_by": user_id,
        "created_at": now, "updated_at": now,
    }


def insert_new(db: Session, vendor_id: int, leads: list[CleanLead], user_id: int) -> list[VendorToKocLead]:
    """Insert valid leads that aren't duplicates (checked again here, in the same transaction).
    Returns the rows inserted."""
    now = utcnow()
    existing = existing_keys(db, vendor_id, {lead.phone for lead in leads})
    new = []
    for lead in leads:
        if lead_key(lead) not in existing:
            existing.add(lead_key(lead))
            new.append(lead)
    rows = [VendorToKocLead(**_row(vendor_id, lead, LeadSource.BULK_UPLOAD, user_id, now)) for lead in new]
    if not rows:
        return []
    db.add_all(rows)
    db.flush()
    ids = [row.id for row in rows]
    db.commit()
    # Read the saved rows back in one query (not one refresh per row), in insert order
    by_id = {row.id: row for row in db.exec(select(VendorToKocLead).where(col(VendorToKocLead.id).in_(ids)))}
    return [by_id[i] for i in ids]


def create_lead(db: Session, vendor_id: int, lead: CleanLead, user_id: int) -> int:
    if lead_key(lead) in existing_keys(db, vendor_id, {lead.phone}):
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
    "is_hot": VendorToKocLead.is_hot,
}


@dataclass
class LeadFilters:
    vendor_ids: list[int] | None = None      # None: no vendor filter
    search: str | None = None
    statuses: list[str] | None = None        # may contain "pending" (= NULL)
    created_from: datetime | None = None     # arrival (created_at), aware UTC, [from, to)
    created_to: datetime | None = None
    lead_date_from: date | None = None       # the lead's own date
    lead_date_to: date | None = None
    follow_up_from: date | None = None
    follow_up_to: date | None = None
    follow_up_due_by: date | None = None     # follow_up_date <= this, open statuses only
    city: str | None = None                  # contains, ignoring case
    sources: list[str] | None = None         # bulk_upload / manual
    ids_in: set[int] | None = None           # only these lead ids (also_in=yes)
    ids_not_in: set[int] | None = None       # never these lead ids (also_in=no)
    hot: bool | None = None
    quote: tuple[str, int | None] | None = None       # has / none / gt / lt over quoted_amount
    conversion: tuple[str, int | None] | None = None  # has / none / gt / lt over conversion_amount


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
    if f.created_from is not None:
        conds.append(col(lead.created_at) >= f.created_from)
    if f.created_to is not None:
        conds.append(col(lead.created_at) < f.created_to)
    if f.lead_date_from:
        conds.append(col(lead.lead_date) >= f.lead_date_from)
    if f.lead_date_to:
        conds.append(col(lead.lead_date) <= f.lead_date_to)
    if f.follow_up_due_by is not None:
        conds.append(col(lead.follow_up_date) <= f.follow_up_due_by)
        conds.append(or_(col(lead.update_status).is_(None), col(lead.update_status).not_in(sorted(CLOSED_STATUSES))))
    if f.city:
        escaped_city = f.city.replace("/", "//").replace("%", "/%").replace("_", "/_")
        conds.append(col(lead.city).ilike(f"%{escaped_city}%", escape=LIKE_ESCAPE))
    if f.sources:
        conds.append(col(lead.source).in_(f.sources))
    if f.hot is not None:
        conds.append(col(lead.is_hot).is_(f.hot))
    if f.quote:
        op, value = f.quote
        conds.append(col(lead.quoted_amount).is_not(None) if op == "has" else
                     col(lead.quoted_amount).is_(None) if op == "none" else
                     col(lead.quoted_amount) > value if op == "gt" else col(lead.quoted_amount) < value)
    if f.conversion:
        op, value = f.conversion
        conds.append(col(lead.conversion_amount).is_not(None) if op == "has" else
                     col(lead.conversion_amount).is_(None) if op == "none" else
                     col(lead.conversion_amount) > value if op == "gt" else col(lead.conversion_amount) < value)
    if f.ids_in is not None:
        conds.append(col(lead.id).in_(sorted(f.ids_in)) if f.ids_in else false())
    if f.ids_not_in:
        conds.append(col(lead.id).not_in(sorted(f.ids_not_in)))
    if f.follow_up_from:
        conds.append(col(lead.follow_up_date) >= f.follow_up_from)
    if f.follow_up_to:
        conds.append(col(lead.follow_up_date) <= f.follow_up_to)
    return conds


def _order_by(sort: str, order: str) -> list:
    desc = order == "desc"
    column = SORT_COLUMNS[sort]
    if sort == "is_hot":
        return [column.desc(), col(VendorToKocLead.id).desc() if desc else col(VendorToKocLead.id).asc()]
    # Empty values last in both directions. MySQL has no NULLS LAST, so sort on
    # "is empty" first; this works on MySQL and SQLite alike.
    return [
        case((column.is_(None), 1), else_=0),
        column.desc() if desc else column.asc(),
        col(VendorToKocLead.id).desc() if desc else col(VendorToKocLead.id).asc(),
    ]


def _lead_dict(lead: VendorToKocLead, vendor_name: str, updater_name: str | None,
               creator_name: str | None = None) -> dict:
    from app.crud.user import display_name

    return {**lead.model_dump(), "vendor_name": vendor_name,
            "status_updated_by_name": display_name(updater_name, lead.status_updated_by),
            "created_by_name": display_name(creator_name, lead.created_by)}


def _with_names():
    """(lead, vendor name, updater name, creator name) in one query, no N+1."""
    updater, creator = aliased(User), aliased(User)
    return (select(VendorToKocLead, Vendor.name, updater.name, creator.name)
            .join(Vendor, col(Vendor.id) == VendorToKocLead.vendor_id)
            .outerjoin(updater, col(updater.id) == VendorToKocLead.status_updated_by)
            .outerjoin(creator, col(creator.id) == VendorToKocLead.created_by))


def serialize(lead: VendorToKocLead, vendor_name: str, admin: bool, updater_name: str | None = None,
              creator_name: str | None = None) -> dict:
    """Admins get AdminLeadRead; vendor users get VendorLeadRead (no status_updated_by user id).
    Names: "Deleted user" when the login is gone."""
    schema = AdminLeadRead if admin else VendorLeadRead
    return schema.model_validate(_lead_dict(lead, vendor_name, updater_name, creator_name)).model_dump(mode="json")


def count_leads(db: Session, f: LeadFilters) -> int:
    return db.exec(select(func.count()).select_from(VendorToKocLead).where(*_conditions(f))).one()


def lead_stats(db: Session, f: LeadFilters, admin: bool, today: date) -> dict:
    """Header numbers over the same filtered leads as the list. Vendor users (whose filters are
    always their own vendor) get by_status and follow_ups_due too, but not by_vendor."""
    lead = VendorToKocLead
    conds = _conditions(f)
    by_source = dict(db.exec(select(lead.source, func.count()).where(*conds).group_by(lead.source)).all())
    out = {"total": count_leads(db, f),
           "by_source": {s.value: by_source.get(s.value, 0) for s in LeadSource}}
    from app.services import kpis

    by_status = dict(db.exec(select(lead.update_status, func.count()).where(*conds).group_by(lead.update_status)).all())
    out["kpis"] = kpis.compute(db.exec(select(lead).where(*conds)).all())
    due = db.exec(select(func.count()).select_from(lead).where(
        *conds, col(lead.follow_up_date) <= today,
        or_(col(lead.update_status).is_(None), col(lead.update_status).not_in(sorted(CLOSED_STATUSES))))).one()
    out["by_status"] = {"pending": by_status.get(None, 0), **{s.value: by_status.get(s.value, 0) for s in UpdateStatus}}
    out["follow_ups_due"] = due
    if not admin:
        return out
    by_vendor = db.exec(
        select(Vendor.id, Vendor.name, func.count()).select_from(lead)
        .join(Vendor, col(Vendor.id) == lead.vendor_id).where(*conds)
        .group_by(col(Vendor.id), Vendor.name).order_by(func.count().desc(), Vendor.name)
    ).all()
    out["by_vendor"] = [{"vendor_id": vid, "vendor_name": name, "count": n} for vid, name, n in by_vendor]
    return out


def export_rows(db: Session, f: LeadFilters, sort: str, order: str, admin: bool) -> list[tuple]:
    """All matching leads in list order, as tuples: (vendor_name, lead_date, name, phone, city,
    update_status, additional_comments, follow_up_date, quoted_amount, conversion_amount,
    updater_name, updater_email, status_updated_at). The table leaves the vendor out for vendor users (their filters are
    always their own vendor) and shows only the updater's name to them, never an email."""
    lead = VendorToKocLead
    updater = aliased(User)
    stmt = (
        select(Vendor.name, lead.lead_date, lead.name, lead.phone, lead.city,
               lead.update_status, lead.additional_comments, lead.follow_up_date,
               lead.quoted_amount, lead.conversion_amount,
               updater.name, updater.email if admin else updater.name, lead.status_updated_at, lead.is_hot)
        .join(Vendor, col(Vendor.id) == lead.vendor_id)
        .outerjoin(updater, col(updater.id) == lead.status_updated_by)
    )
    return list(db.exec(stmt.where(*_conditions(f)).order_by(*_order_by(sort, order))).all())


def set_hot(db: Session, lead: VendorToKocLead, actor_user_id: int, hot: bool) -> None:
    lead.is_hot = hot
    lead.hot_marked_by = actor_user_id if hot else None
    lead.hot_marked_at = utcnow() if hot else None
    db.add(lead)
    db.commit()


def list_leads(db: Session, f: LeadFilters, sort: str, order: str,
               page: int, page_size: int, admin: bool) -> dict:
    conds = _conditions(f)
    total = count_leads(db, f)
    offset = (page - 1) * page_size
    rows = db.exec(
        _with_names()                                                 # one query, no N+1
        .where(*conds)
        .order_by(*_order_by(sort, order))
        .offset(offset)
        .limit(page_size)
    ).all()
    items = [{"sl_no": offset + i + 1, **serialize(lead, vendor_name, admin, updater_name, creator_name)}
             for i, (lead, vendor_name, updater_name, creator_name) in enumerate(rows)]
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": math.ceil(total / page_size)}


def get_lead(db: Session, lead_id: int, vendor_id: int | None = None
             ) -> tuple[VendorToKocLead, str, str | None, str | None] | None:
    """The lead, its vendor name, the updater's and the creator's names. With vendor_id, only that
    vendor's lead is found."""
    stmt = _with_names().where(VendorToKocLead.id == lead_id)
    if vendor_id is not None:
        stmt = stmt.where(VendorToKocLead.vendor_id == vendor_id)
    return db.exec(stmt).first()


def update_lead(db: Session, lead: VendorToKocLead, changes: dict, admin_id: int, stamp_status: bool = True) -> None:
    """stamp_status=False (a vendor setting its payout %): status_updated_by / _at stay as they were."""
    now = utcnow()
    for key, value in changes.items():
        setattr(lead, key, value.value if hasattr(value, "value") else value)
    # Payout in rupees: always worked out from the conversion amount and the % (the vendor's commission)
    if {"payout_percent", "payout_amount", "conversion_amount"} & set(changes):
        from app.services.lead_crm import apply_payout

        apply_payout(lead)
    if stamp_status:
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
    from app.core import response_cache
    from app.services import access

    db.add(vendor)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise DuplicateVendorName() from None
    response_cache.clear("vendors")       # cached vendor lists (lead_crm.vendor_snapshot)
    access.forget()                       # an inactive vendor no longer gets leads
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


def unlink_user(db: Session, user_id: int, vendor_id: int) -> bool:
    """Removes the login's link to this vendor. False if it isn't linked to that vendor."""
    link = db.get(VendorUser, user_id)
    if link is None or link.vendor_id != vendor_id:
        return False
    db.delete(link)
    db.commit()
    return True


def link_user(db: Session, user_id: int, vendor_id: int) -> None:
    link = db.get(VendorUser, user_id)
    if link is None:
        db.add(VendorUser(user_id=user_id, vendor_id=vendor_id))
    else:
        link.vendor_id = vendor_id
        db.add(link)
    db.commit()
