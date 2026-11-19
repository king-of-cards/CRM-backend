from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, not_
from sqlmodel import Session, select

from app.models.notification import CrmNotification, CrmNotificationRead
from app.models.user import Role, User
from app.models.vendor import Vendor

NOTIFICATION_TYPES = ("vendor_assigned", "vendor_lead_added")
RETENTION_DAYS = 90


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def cleanup_expired(db: Session) -> None:
    cutoff = _now() - timedelta(days=RETENTION_DAYS)
    ids = select(CrmNotification.id).where(CrmNotification.created_at < cutoff)
    db.exec(delete(CrmNotificationRead).where(CrmNotificationRead.notification_id.in_(ids)))
    db.exec(delete(CrmNotification).where(CrmNotification.created_at < cutoff))
    db.commit()


def _lead_name(db: Session, source: str, lead_key: str) -> str:
    if source == "spin":
        from app.services.crm_leads import service

        crm_lead = service.find_crm_lead(db, source, lead_key)
        if crm_lead is not None and crm_lead.name:
            return crm_lead.name
        if lead_key.isdigit():
            try:
                from sqlalchemy import select as sa_select

                from app.api.v1.endpoints import spin_leads
                from app.db.session import read_engine

                if read_engine is not None:
                    table = spin_leads._get_table()
                    key_col = next(iter(table.primary_key.columns))
                    name_col = next((c for c in table.columns if c.name.lower() in ("name", "lead_name", "customer_name")), None)
                    if name_col is not None:
                        with read_engine.connect() as conn:
                            value = conn.execute(sa_select(name_col).where(key_col == int(lead_key))).scalar_one_or_none()
                        if value:
                            return str(value)
            except Exception:
                pass
    else:
        from app.models.wedding_lead import WeddingLead
        from app.models.wedding_plan import WeddingPlan
        from app.models.whatsapp_lead import WhatsAppLead

        model, key_col = {
            "whatsapp": (WhatsAppLead, WhatsAppLead.sheet_key),
            "wedding": (WeddingLead, WeddingLead.sheet_key),
            "wedding_planner": (WeddingPlan, WeddingPlan.plan_id),
        }[source]
        row = db.exec(select(model).where(key_col == lead_key)).first()
        if row is not None:
            name = getattr(row, "name", None)
            if name:
                return name
            phone = getattr(row, "phone", None)
            if phone:
                return str(phone)
    return f"{source} lead {lead_key}"


def notify_vendor_assigned(db: Session, *, source: str, lead_key: str, vendor_id: int,
                           actor_user_id: int, actor_name: str) -> int:
    cleanup_expired(db)
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        return 0
    lead_name = _lead_name(db, source, str(lead_key))
    db.add(CrmNotification(
        recipient_vendor_id=vendor_id, type="vendor_assigned", title="Lead assigned to your vendor",
        body=f"{lead_name} ({source}) was assigned to {vendor.name} by {actor_name}.",
        source=source, lead_key=str(lead_key), lead_id=int(lead_key) if str(lead_key).isdigit() else None,
        actor_user_id=actor_user_id, actor_name=actor_name, vendor_id=vendor_id, created_at=_now(),
    ))
    db.commit()
    return 1


def notify_admins_vendor_added(db: Session, *, vendor_id: int, actor_user_id: int, actor_name: str,
                               added: int, lead_id: int | None = None, lead_name: str | None = None) -> int:
    cleanup_expired(db)
    vendor = db.get(Vendor, vendor_id)
    if vendor is None or added < 1:
        return 0
    admins = db.exec(select(User).where(User.role == Role.ADMIN.value, User.is_active == True,  # noqa: E712
                                         User.id != actor_user_id)).all()
    now = _now()
    if lead_name and added == 1:
        title = "Vendor added a lead to KOC"
        body = f"{vendor.name} added {lead_name} to KOC; added by {actor_name}."
    else:
        title = f"{added} leads added to KOC"
        body = f"{added} leads added by {vendor.name}; added by {actor_name}."
    db.add_all(CrmNotification(
        recipient_user_id=admin.id, type="vendor_lead_added", title=title, body=body,
        source="vendor_to_koc", lead_key=str(lead_id) if lead_id is not None else None,
        lead_id=lead_id, actor_user_id=actor_user_id, actor_name=actor_name,
        vendor_id=vendor_id, created_at=now,
    ) for admin in admins)
    db.commit()
    return len(admins)


def _visible_query(db: Session, user: User, vendor_id: int | None):
    query = select(CrmNotification)
    if user.role == Role.ADMIN.value:
        return query.where(CrmNotification.recipient_user_id == user.id)
    return query.where(CrmNotification.recipient_vendor_id == vendor_id)


def _read_exists(user_id: int):
    return select(CrmNotificationRead.id).where(
        CrmNotificationRead.notification_id == CrmNotification.id,
        CrmNotificationRead.user_id == user_id,
    ).exists()


def list_notifications(db: Session, user: User, vendor_id: int | None, *, page: int,
                       page_size: int, unread_only: bool, type_: str | None) -> dict:
    cleanup_expired(db)
    query = _visible_query(db, user, vendor_id)
    if type_:
        query = query.where(CrmNotification.type == type_)
    if unread_only:
        query = query.where(CrmNotification.read_at.is_(None) if user.role == Role.ADMIN.value
                            else not_(_read_exists(user.id)))
    total = db.exec(select(func.count()).select_from(query.subquery())).one()
    rows = db.exec(query.order_by(CrmNotification.created_at.desc(), CrmNotification.id.desc())
                   .offset((page - 1) * page_size).limit(page_size)).all()
    read_ids = set()
    if user.role != Role.ADMIN.value and rows:
        read_ids = set(db.exec(select(CrmNotificationRead.notification_id).where(
            CrmNotificationRead.user_id == user.id,
            CrmNotificationRead.notification_id.in_([r.id for r in rows]),
        )).all())
    items = []
    for row in rows:
        read = row.read_at is not None if user.role == Role.ADMIN.value else row.id in read_ids
        items.append({
            "id": row.id, "type": row.type, "title": row.title, "body": row.body,
            "source": row.source, "lead_key": row.lead_key, "lead_id": row.lead_id,
            "lead_ref": {"source": row.source, "lead_key": row.lead_key, "lead_id": row.lead_id}
                        if row.source else None,
            "vendor_id": row.vendor_id, "actor_user_id": row.actor_user_id, "actor_name": row.actor_name,
            "created_at": _iso(row.created_at), "read": read,
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": (total + page_size - 1) // page_size}


def unread_count(db: Session, user: User, vendor_id: int | None) -> int:
    cleanup_expired(db)
    query = _visible_query(db, user, vendor_id)
    if user.role == Role.ADMIN.value:
        query = query.where(CrmNotification.read_at.is_(None))
    else:
        query = query.where(not_(_read_exists(user.id)))
    return db.exec(select(func.count()).select_from(query.subquery())).one()


def mark_read(db: Session, user: User, vendor_id: int | None, notification_id: int) -> bool:
    cleanup_expired(db)
    row = db.exec(_visible_query(db, user, vendor_id).where(CrmNotification.id == notification_id)).first()
    if row is None:
        return False
    if user.role == Role.ADMIN.value:
        if row.read_at is None:
            row.read_at = _now()
            db.add(row)
    else:
        found = db.exec(select(CrmNotificationRead.id).where(
            CrmNotificationRead.notification_id == row.id, CrmNotificationRead.user_id == user.id
        )).first()
        if found is None:
            db.add(CrmNotificationRead(notification_id=row.id, user_id=user.id, read_at=_now()))
    db.commit()
    return True


def mark_all_read(db: Session, user: User, vendor_id: int | None) -> int:
    cleanup_expired(db)
    query = _visible_query(db, user, vendor_id)
    if user.role == Role.ADMIN.value:
        rows = db.exec(query.where(CrmNotification.read_at.is_(None))).all()
        now = _now()
        for row in rows:
            row.read_at = now
            db.add(row)
    else:
        rows = db.exec(query.where(not_(_read_exists(user.id)))).all()
        now = _now()
        db.add_all(CrmNotificationRead(notification_id=row.id, user_id=user.id, read_at=now) for row in rows)
    db.commit()
    return len(rows)
