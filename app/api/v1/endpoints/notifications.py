"""Per-login notifications for CRM lead assignments and vendor KOC submissions."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbSession, LeadViewer, PageSize
from app.services import notifications as service

router = APIRouter(prefix="/notifications", tags=["notifications"])
NotificationType = Literal["vendor_assigned", "vendor_lead_added"]


@router.get("")
def list_notifications(
    viewer: LeadViewer,
    db: DbSession,
    page: int = Query(1, ge=1),
    page_size: PageSize = 10,
    unread_only: bool = False,
    type: NotificationType | None = None,
):
    return service.list_notifications(db, viewer.user, viewer.vendor_id, page=page, page_size=page_size,
                                      unread_only=unread_only, type_=type)


@router.get("/unread-count")
def unread_count(viewer: LeadViewer, db: DbSession):
    return {"unread_count": service.unread_count(db, viewer.user, viewer.vendor_id)}


@router.post("/{notification_id}/read")
def mark_read(notification_id: int, viewer: LeadViewer, db: DbSession):
    if not service.mark_read(db, viewer.user, viewer.vendor_id, notification_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    return {"id": notification_id, "read": True}


@router.post("/read-all")
def mark_all_read(viewer: LeadViewer, db: DbSession):
    return {"marked_read": service.mark_all_read(db, viewer.user, viewer.vendor_id)}
