"""Dhruva leads (crm_dhruva_leads): leads that Dhruva logins (role "dhruva") add or upload.

The lead's own fields are kept as typed (event_date_text) next to what the CRM reads from them
(event_date, the E.164 phones). The admin fields (update_status ... status_updated_at) work like
Vendor -> KOC's: the same 7 Update values, NULL = Pending; only admins change them.
All datetimes are timezone-aware UTC (SQLModel stores them as UTC).
"""
from datetime import date as Date
from datetime import datetime

from pydantic import ConfigDict
from sqlalchemy import Text
from sqlmodel import Field, SQLModel

from app.models.vendor_to_koc import UpdateStatus

NAME_MAX, VENUE_MAX, DATE_TEXT_MAX, PHONE_MAX, EVENT_TYPE_MAX = 200, 300, 200, 32, 200
MAX_AMOUNT = 100_000_000


class DhruvaLead(SQLModel, table=True):
    __tablename__ = "crm_dhruva_leads"

    id: int | None = Field(default=None, primary_key=True)
    name: str | None = Field(default=None, max_length=NAME_MAX)
    venue: str | None = Field(default=None, max_length=VENUE_MAX)
    event_date_text: str | None = Field(default=None, max_length=DATE_TEXT_MAX)   # as typed
    event_date: Date | None = Field(default=None, index=True)                     # when clear
    phone: str | None = Field(default=None, max_length=PHONE_MAX)                 # as written
    alt_phone: str | None = Field(default=None, max_length=PHONE_MAX)
    phone_e164: str | None = Field(default=None, max_length=16, index=True)
    alt_phone_e164: str | None = Field(default=None, max_length=16, index=True)
    event_type: str | None = Field(default=None, max_length=EVENT_TYPE_MAX)
    source: str = Field(max_length=20)                      # manual | bulk_upload
    # Admin-only fields
    update_status: str | None = Field(default=None, max_length=30, index=True)
    additional_comments: str | None = Field(default=None, sa_type=Text)
    quoted_amount: int | None = None                        # rupees
    conversion_amount: int | None = None                    # rupees
    follow_up_date: Date | None = None
    status_updated_by: int | None = Field(default=None, foreign_key="crm_users.id")
    status_updated_at: datetime | None = None
    created_by: int = Field(foreign_key="crm_users.id")
    created_at: datetime = Field(index=True)
    updated_at: datetime
    is_deleted: bool = Field(default=False, index=True)


# ── Request bodies ─────────────────────────────────────────────────────
class DhruvaLeadInput(SQLModel):
    """A lead from the add form or the bulk save. phone may hold two numbers (line break, comma
    or "/"), as in the sheets; alt_phone is the second number when sent separately. date: as
    typed ("Dec 4th", "Feb 24&25th 2027"), DD-MM-YYYY or YYYY-MM-DD."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    venue: str | None = None
    date: str | None = None
    phone: str | int | None = None
    alt_phone: str | int | None = None
    event_type: str | None = None


class DhruvaLeadCreate(DhruvaLeadInput):
    allow_duplicate: bool = False          # save even if the phone / name + date already exists


class DhruvaBulkCreate(SQLModel):
    model_config = ConfigDict(extra="forbid")
    leads: list[DhruvaLeadInput] = Field(max_length=5000)


BASIC_FIELDS = ("name", "venue", "date", "phone", "alt_phone", "event_type")
ADMIN_FIELDS = ("update_status", "additional_comments", "quoted_amount", "conversion_amount", "follow_up_date")


class DhruvaLeadUpdate(SQLModel):
    """PATCH: only the fields sent change; null clears one. Basic fields: admins and Dhruva
    logins. Admin fields: admins only (Dhruva logins get 403)."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    venue: str | None = None
    date: str | None = None
    phone: str | int | None = None
    alt_phone: str | int | None = None
    event_type: str | None = None
    update_status: UpdateStatus | None = None
    additional_comments: str | None = Field(default=None, max_length=5000)
    quoted_amount: int | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    conversion_amount: int | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    follow_up_date: Date | None = None
    allow_duplicate: bool = False
