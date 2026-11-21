"""Vendor to KOC: the leads vendors send to KOC (ported from the other CRM backend).

Vendors and vendor logins are this backend's Vendor (app/models/vendor.py) and VendorUser
(app/models/vendor_user.py); the tables are shared.

All datetimes are timezone-aware UTC (SQLModel stores them as UTC).
"""
from datetime import date, datetime
from enum import Enum

from pydantic import ConfigDict, field_validator
from sqlalchemy import Index, Text
from sqlmodel import Field, SQLModel


class UpdateStatus(str, Enum):
    CONVERTED = "converted"
    NOT_CONVERTED = "not_converted"
    PURCHASED_ELSEWHERE = "purchased_elsewhere"
    PURCHASED_WITH_US = "purchased_with_us"
    WILL_CALL_BACK = "will_call_back"
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"


# Readable labels for exports; None (no status yet) is "Pending"
STATUS_LABELS = {
    None: "Pending",
    UpdateStatus.CONVERTED.value: "Converted",
    UpdateStatus.NOT_CONVERTED.value: "Not converted",
    UpdateStatus.PURCHASED_ELSEWHERE.value: "Already purchased somewhere else",
    UpdateStatus.PURCHASED_WITH_US.value: "Already purchased with us",
    UpdateStatus.WILL_CALL_BACK.value: "Will call back",
    UpdateStatus.INTERESTED.value: "Interested",
    UpdateStatus.NOT_INTERESTED.value: "Not interested",
}


# Statuses after which nothing more is expected: their follow-ups are never "due"
CLOSED_STATUSES = frozenset({
    UpdateStatus.CONVERTED.value, UpdateStatus.NOT_CONVERTED.value, UpdateStatus.PURCHASED_ELSEWHERE.value,
    UpdateStatus.PURCHASED_WITH_US.value, UpdateStatus.NOT_INTERESTED.value,
})


class LeadSource(str, Enum):
    MANUAL = "manual"
    BULK_UPLOAD = "bulk_upload"


# ── Tables ─────────────────────────────────────────────────────────────
class VendorToKocLead(SQLModel, table=True):
    __tablename__ = "crm_vendor_to_koc_leads"
    __table_args__ = (
        # Not unique: the same phone with another name, date or city is another lead (see
        # migrations/2026-11-22_vtk_drop_vendor_phone_unique.sql)
        Index("ix_vtk_leads_vendor_phone", "vendor_id", "phone"),
        Index("ix_vtk_leads_vendor_lead_date", "vendor_id", "lead_date"),
    )

    id: int | None = Field(default=None, primary_key=True)
    vendor_id: int = Field(foreign_key="crm_vendors.id")   # indexed by the two indexes above
    lead_date: date | None = None
    name: str | None = Field(default=None, max_length=100)
    phone: str = Field(max_length=10)
    city: str | None = Field(default=None, max_length=100)
    # Admin-only fields. update_status NULL means "pending". Plain text column (validated
    # against UpdateStatus in the app), so adding a status later needs no ALTER TABLE.
    update_status: str | None = Field(default=None, max_length=30)
    additional_comments: str | None = Field(default=None, sa_type=Text)
    follow_up_date: date | None = None
    quoted_amount: int | None = None                       # rupees; admins set it (PATCH)
    conversion_amount: int | None = None                   # rupees; admins set it (PATCH)
    status_updated_by: int | None = Field(default=None, foreign_key="crm_users.id")
    status_updated_at: datetime | None = None
    is_hot: bool = Field(default=False, sa_column_kwargs={"server_default": "0", "nullable": False})
    hot_marked_by: int | None = Field(default=None, foreign_key="crm_users.id")
    hot_marked_at: datetime | None = None
    source: str = Field(max_length=20)                     # LeadSource value
    created_by: int = Field(foreign_key="crm_users.id")
    created_at: datetime
    updated_at: datetime


# ── Vendors API ────────────────────────────────────────────────────────
def _vendor_name(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 100:
        raise ValueError("Name must be 1-100 characters")
    return value


class VendorCreate(SQLModel):
    model_config = ConfigDict(extra="forbid")
    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _vendor_name(v)


class VendorUpdate(SQLModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return None if v is None else _vendor_name(v)


class VendorRead(SQLModel):
    id: int
    name: str
    is_active: bool
    lead_count: int


class VendorUserLink(SQLModel):
    user_id: int
    vendor_id: int
    vendor_name: str


# ── Leads API ──────────────────────────────────────────────────────────
class LeadInput(SQLModel):
    """One lead as sent by the client. Values are validated by app.services.lead_import,
    so a bad value becomes a per-lead error instead of rejecting the whole request."""
    name: str | None = None
    date: str | None = None            # YYYY-MM-DD
    phone: str | int | None = None
    city: str | None = None


class LeadCreate(LeadInput):
    model_config = ConfigDict(extra="forbid")
    vendor_id: int | None = None       # ignored: the vendor comes from the login


class LeadBulkCreate(SQLModel):
    model_config = ConfigDict(extra="forbid")
    vendor_id: int | None = None       # ignored: the vendor comes from the login
    leads: list[LeadInput] = Field(max_length=5000)


class LeadUpdate(SQLModel):
    """PATCH body (admins only). Only the fields sent are changed; null clears a field."""
    model_config = ConfigDict(extra="forbid")
    update_status: UpdateStatus | None = None
    additional_comments: str | None = Field(default=None, max_length=5000)
    follow_up_date: date | None = None
    quoted_amount: int | None = Field(default=None, ge=0, le=100_000_000)        # rupees
    conversion_amount: int | None = Field(default=None, ge=0, le=100_000_000)    # rupees


class VendorLeadRead(SQLModel):
    """What vendor users see: their own leads, with KOC's update read-only. The admin-only
    field (status_updated_by, a user id) is not part of this schema at all."""
    id: int
    vendor_id: int
    vendor_name: str
    lead_date: date | None
    name: str | None
    phone: str
    city: str | None
    source: str
    created_by: int
    created_at: datetime
    is_hot: bool = False
    updated_at: datetime
    update_status: str | None          # null = pending
    additional_comments: str | None
    follow_up_date: date | None
    quoted_amount: int | None = None
    conversion_amount: int | None = None
    status_updated_at: datetime | None
    status_updated_by_name: str | None = None   # who last changed the update (name)


class AdminLeadRead(VendorLeadRead):
    status_updated_by: int | None      # user id


# Fields that must never reach a vendor user (checked by the tests)
ADMIN_ONLY_FIELDS = frozenset(AdminLeadRead.model_fields) - frozenset(VendorLeadRead.model_fields)
