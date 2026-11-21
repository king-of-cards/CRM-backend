from datetime import datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class CrmNotification(SQLModel, table=True):
    __tablename__ = "crm_notifications"
    __table_args__ = (
        CheckConstraint(
            "(recipient_user_id IS NOT NULL AND recipient_vendor_id IS NULL) OR "
            "(recipient_user_id IS NULL AND recipient_vendor_id IS NOT NULL)",
            name="ck_crm_notifications_one_recipient",
        ),
        Index("ix_crm_notifications_user_created", "recipient_user_id", "created_at"),
        Index("ix_crm_notifications_vendor_created", "recipient_vendor_id", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    recipient_user_id: int | None = Field(default=None, foreign_key="crm_users.id")
    recipient_vendor_id: int | None = Field(default=None, foreign_key="crm_vendors.id")
    type: str = Field(max_length=40, index=True)
    title: str = Field(max_length=200)
    body: str = Field(max_length=1000)
    source: str | None = Field(default=None, max_length=30)
    lead_key: str | None = Field(default=None, max_length=100)
    lead_id: int | None = None
    actor_user_id: int = Field()   # user id; no foreign key: history outlives deleted logins
    actor_name: str = Field(max_length=100)
    vendor_id: int | None = Field(default=None, foreign_key="crm_vendors.id", index=True)
    created_at: datetime
    # Used for individually addressed admin notifications. Vendor broadcasts use the per-user
    # crm_notification_reads table below.
    read_at: datetime | None = None


class CrmNotificationRead(SQLModel, table=True):
    __tablename__ = "crm_notification_reads"
    __table_args__ = (
        UniqueConstraint("notification_id", "user_id", name="uq_crm_notification_read_user"),
        Index("ix_crm_notification_reads_user", "user_id", "read_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    notification_id: int = Field(foreign_key="crm_notifications.id", index=True)
    user_id: int = Field(foreign_key="crm_users.id", index=True)
    read_at: datetime
