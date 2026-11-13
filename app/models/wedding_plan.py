from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel


class WeddingPlan(SQLModel, table=True):
    """One wedding plan from the "Wedding planner" Google Sheet, copied by the sync.

    The planner saves a new row every time the visitor moves on, so the same Plan ID can
    appear many times; only the most recent row per Plan ID is kept here (`versions` counts them).
    Rows removed from the sheet are kept with is_deleted=True. All datetimes are UTC.
    """
    __tablename__ = "crm_wedding_plans"

    id: int | None = Field(default=None, primary_key=True)
    plan_id: str = Field(max_length=64, unique=True)            # e.g. KOC-QP6J5A
    session: str | None = Field(default=None, max_length=64)
    last_step: str | None = Field(default=None, max_length=50, index=True)  # pdf, whatsapp…
    updated_in_sheet_at: datetime | None = Field(default=None, index=True)
    versions: int = 1                                            # rows in the sheet for this plan

    # Who
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32, index=True)   # digits with country code
    email: str | None = Field(default=None, max_length=254)
    role: str | None = Field(default=None, max_length=100)               # "You are the": Bride, Sibling…
    consent_share: bool | None = None
    consent_offers: bool | None = None

    # Wedding
    wedding_type: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=200, index=True)
    home_city: str | None = Field(default=None, max_length=200)
    place_kind: str | None = Field(default=None, max_length=200)
    wedding_month: str | None = Field(default=None, max_length=50)       # "December 2027"
    first_day: date | None = None
    wedding_date: date | None = Field(default=None, index=True)          # first day, else 1st of the month
    total_guests: int | None = None
    guests_staying: int | None = None
    traditions: str | None = Field(default=None, max_length=300)
    events: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    food: str | None = Field(default=None, max_length=100)
    alcohol: str | None = Field(default=None, max_length=50)

    # Money
    budget_range: str | None = Field(default=None, max_length=100)
    total_budget: int | None = Field(default=None, index=True)
    budget_includes_jewellery: bool | None = None
    jewellery_outfits: int | None = None
    plan_estimate: int | None = None
    fits_budget: bool | None = Field(default=None, index=True)

    priorities: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    shortlisted_vendors: str | None = Field(default=None, sa_column=Column(Text))
    shared_with: str | None = Field(default=None, max_length=200, index=True)   # "Leads shared"

    sheet_row: int | None = None
    row_hash: str = Field(max_length=40)
    is_deleted: bool = Field(default=False, index=True)
    first_seen_at: datetime
    updated_at: datetime