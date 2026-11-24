"""Dhruva -> KOC leads (crm_dhruva_leads): leads Dhruva logins (role "dhruva") add, and admins upload.

Lead fields (admins and Dhruva logins): name (required), phone numbers (any number, optional, 10
digits each; in crm_dhruva_phones; phone / phone_e164 hold the first, for reminders), event dates
(one or more real dates, in crm_dhruva_event_dates; event_date holds the earliest, for sorting),
event type, venue.
KOC fields (admins only; Dhruva logins read them): WhatsApp status, response, message sent,
follow-up notes, comments (column additional_comments), needs Dhruva intervention, Update (the
same 7 values as Spin, NULL = Pending), quoted amount, conversion amount, follow-up date.
sent_at: when the lead was sent to KOC (added or uploaded); set by the CRM, never edited.
Every create and change is logged in crm_lead_activity (source "dhruva").
All datetimes are timezone-aware UTC (SQLModel stores them as UTC).
"""
from datetime import date as Date
from datetime import datetime

from pydantic import ConfigDict
from sqlalchemy import Text
from sqlmodel import Field, SQLModel

from app.models.vendor_to_koc import UpdateStatus

NAME_MAX, VENUE_MAX, PHONE_MAX, EVENT_TYPE_MAX = 200, 300, 32, 200
WHATSAPP_STATUS_MAX, MESSAGE_SENT_MAX, NOTES_MAX = 100, 500, 5000
MAX_EVENT_DATES = 20
MAX_PHONES = 20
MAX_AMOUNT = 100_000_000


class DhruvaLead(SQLModel, table=True):
    __tablename__ = "crm_dhruva_leads"

    id: int | None = Field(default=None, primary_key=True)
    name: str | None = Field(default=None, max_length=NAME_MAX)
    venue: str | None = Field(default=None, max_length=VENUE_MAX)
    event_date: Date | None = Field(default=None, index=True)          # the earliest of the event dates
    phone: str | None = Field(default=None, max_length=PHONE_MAX)       # the first phone number (10 digits)
    phone_e164: str | None = Field(default=None, max_length=16, index=True)
    event_type: str | None = Field(default=None, max_length=EVENT_TYPE_MAX)
    source: str = Field(max_length=20)                      # manual | bulk_upload
    sent_at: datetime | None = Field(default=None, index=True)   # added / uploaded (never edited)
    # KOC fields (admins only)
    whatsapp_status: str | None = Field(default=None, max_length=WHATSAPP_STATUS_MAX)
    response: str | None = Field(default=None, sa_type=Text)
    message_sent: str | None = Field(default=None, max_length=MESSAGE_SENT_MAX)
    follow_up_notes: str | None = Field(default=None, sa_type=Text)
    additional_comments: str | None = Field(default=None, sa_type=Text)     # "Comments"
    needs_intervention: bool = Field(default=False, index=True)              # needs Dhruva intervention
    update_status: str | None = Field(default=None, max_length=30, index=True)
    quoted_amount: int | None = None                        # rupees
    conversion_amount: int | None = None                    # rupees
    follow_up_date: Date | None = None
    status_updated_by: int | None = Field(default=None)   # user id; no foreign key: history outlives deleted logins
    status_updated_at: datetime | None = None
    created_by: int = Field()   # user id; no foreign key: history outlives deleted logins
    created_at: datetime = Field(index=True)
    updated_at: datetime
    is_deleted: bool = Field(default=False, index=True)


class DhruvaEventDate(SQLModel, table=True):
    """One event date of a Dhruva -> KOC lead (a lead has one or more)."""
    __tablename__ = "crm_dhruva_event_dates"

    id: int | None = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="crm_dhruva_leads.id", index=True)
    event_date: Date = Field(index=True)


class DhruvaPhone(SQLModel, table=True):
    """One phone number of a Dhruva -> KOC lead (a lead has any number of them)."""
    __tablename__ = "crm_dhruva_phones"

    id: int | None = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="crm_dhruva_leads.id", index=True)
    phone: str = Field(max_length=10, index=True)                    # 10 digits
    phone_e164: str = Field(max_length=16, index=True)               # +91…


# ── Request bodies ─────────────────────────────────────────────────────
LEAD_FIELDS = ("name", "phones", "event_dates", "event_type", "venue")
ADMIN_FIELDS = ("whatsapp_status", "response", "message_sent", "follow_up_notes", "comments", "needs_intervention",
                "update_status", "quoted_amount", "conversion_amount", "follow_up_date")
BASIC_FIELDS = LEAD_FIELDS                                  # older name


class DhruvaLeadInput(SQLModel):
    """A lead from the add form or the bulk save (the upload's values). phones: 10-digit numbers,
    a list or one comma-separated text. event_dates: real dates (YYYY-MM-DD), a list or one
    comma-separated text. Amounts: rupees (150000, ₹1,50,000, 1.5 lakh).
    update_status: a key (will_call_back) or its label (Will call back). needs_intervention: true /
    false or Yes / No."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    phones: list[str | int] | str | int | None = None
    event_dates: list[str | Date] | str | None = None
    event_type: str | None = None
    venue: str | None = None
    whatsapp_status: str | None = None
    response: str | None = None
    message_sent: str | None = None
    follow_up_notes: str | None = None
    comments: str | None = None
    needs_intervention: bool | str | None = None
    update_status: str | None = None
    quoted_amount: int | str | None = None
    conversion_amount: int | str | None = None
    follow_up_date: str | Date | None = None


class DhruvaLeadCreate(DhruvaLeadInput):
    allow_duplicate: bool = False          # save even if a phone (or name + first date) already exists


class DhruvaBulkCreate(SQLModel):
    model_config = ConfigDict(extra="forbid")
    leads: list[DhruvaLeadInput] = Field(max_length=5000)


class DhruvaLeadUpdate(DhruvaLeadInput):
    """PATCH: only the fields sent change; null clears one (name can't be cleared). Lead fields:
    admins and Dhruva logins. KOC fields: admins only (Dhruva logins get 403). phones and
    event_dates: the full new list (add or remove numbers / dates by sending the list)."""
    allow_duplicate: bool = False


__all__ = ["DhruvaLead", "DhruvaEventDate", "DhruvaPhone", "DhruvaLeadInput", "DhruvaLeadCreate", "DhruvaBulkCreate",
           "DhruvaLeadUpdate", "LEAD_FIELDS", "ADMIN_FIELDS", "UpdateStatus", "MAX_AMOUNT"]
