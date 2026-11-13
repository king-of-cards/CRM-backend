from datetime import datetime

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel


class Vendor(SQLModel, table=True):
    """A partner leads are assigned to (OHP, Phodeo, RJ…) and the area it covers.

    Automatic assignment checks vendors by `priority` (lowest first); the first match wins:
      - pincodes:          exact pincodes, e.g. ["560018"]
      - pincode_prefixes:  pincodes starting with these, e.g. ["560"] for Bangalore
      - areas:             words found in the lead's city/location, e.g. ["chamrajpet"]
      - is_fallback:       gets every lead with a known location that nobody else matched
    """
    __tablename__ = "crm_vendors"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=100, unique=True)
    coverage: str | None = Field(default=None, max_length=300)        # human description
    areas: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    pincodes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    pincode_prefixes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    is_fallback: bool = False
    priority: int = 100
    contact_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=254)
    notes: str | None = Field(default=None, sa_column=Column(Text))
    is_active: bool = True
    created_at: datetime
    updated_at: datetime