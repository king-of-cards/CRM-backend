from datetime import date, datetime

from sqlmodel import Field, SQLModel


class WeddingLead(SQLModel, table=True):
    """One row of the "Wedding & Event Photography (Responses)" Google Sheet, copied by the sync.

    The sheet has two sets of columns (the Google Form's, and a second lowercase set filled by
    another tool); the sync takes whichever is filled in. sheet_key identifies the row across
    syncs (submission time + phone). Rows removed from the sheet are kept with is_deleted=True.
    All datetimes are UTC.
    """
    __tablename__ = "crm_wedding_leads"

    id: int | None = Field(default=None, primary_key=True)
    sheet_key: str = Field(max_length=64, unique=True)
    submitted_at: datetime | None = Field(default=None, index=True)
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32, index=True)     # digits with country code
    phone_raw: str | None = Field(default=None, max_length=50)             # as typed in the sheet
    city: str | None = Field(default=None, max_length=200)
    occasion: str | None = Field(default=None, max_length=100, index=True)
    event_date_text: str | None = Field(default=None, max_length=100)
    event_date: date | None = Field(default=None, index=True)
    budget_text: str | None = Field(default=None, max_length=100)
    budget_amount: int | None = Field(default=None, index=True)             # in rupees, when readable
    shared_with: str | None = Field(default=None, max_length=200, index=True)  # "leads shared" column
    oseid: str | None = Field(default=None, max_length=100)
    is_repeat: bool = Field(default=False)          # same phone already submitted earlier
    sheet_row: int | None = None
    row_hash: str = Field(max_length=40)
    is_deleted: bool = Field(default=False, index=True)
    first_seen_at: datetime
    updated_at: datetime