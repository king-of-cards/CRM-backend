from datetime import date, datetime

from sqlmodel import Field, SQLModel


class WhatsAppLead(SQLModel, table=True):
    """One row of the "International Phone Numbers (WhatsApp)" Google Sheet, copied by the sync.

    sheet_key identifies the sheet row across syncs (phone + submission time), because
    row numbers change when the sheet is sorted. Rows removed from the sheet are kept
    with is_deleted=True. All datetimes are UTC.
    """
    __tablename__ = "crm_whatsapp_leads"

    id: int | None = Field(default=None, primary_key=True)
    sheet_key: str = Field(max_length=64, unique=True)          # SHA-1 of phone + submission time
    country: str | None = Field(default=None, max_length=100, index=True)
    phone: str = Field(max_length=32, index=True)               # digits only, with country code
    event_date_text: str | None = Field(default=None, max_length=100)  # exactly as typed in the sheet
    event_date: date | None = Field(default=None, index=True)   # parsed when the text is clear
    submitted_at: datetime | None = Field(default=None, index=True)
    sheet_row: int | None = None                                # row number at the last sync
    row_hash: str = Field(max_length=40)                        # detects edits in the sheet
    is_deleted: bool = Field(default=False, index=True)
    first_seen_at: datetime
    updated_at: datetime