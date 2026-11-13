from datetime import date, datetime

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class LeadMeta(SQLModel, table=True):
    """CRM details we add to a lead, stored next to it (the "sidecar").

    The leads themselves stay where they are: spin leads in the read-only workflow database,
    the others in the Google Sheets (copied into crm_* tables). This table only holds what
    the team adds, keyed by (source, lead_key), so the original data is never modified.

      source    spin | whatsapp | wedding | wedding_planner
      lead_key  stable id in that source: spin lead id, sheet row key, or Plan ID
    """
    __tablename__ = "crm_lead_meta"
    __table_args__ = (UniqueConstraint("source", "lead_key", name="uq_lead_meta_source_key"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(max_length=30, index=True)
    lead_key: str = Field(max_length=100, index=True)

    vendor_id: int | None = Field(default=None, foreign_key="crm_vendors.id", index=True)  # manual assignment
    interest: str | None = Field(default=None, max_length=30, index=True)  # interested | follow_up | not_interested
    converted: bool = Field(default=False, index=True)
    converted_at: datetime | None = None
    quoted_date: date | None = None
    quoted_amount: int | None = None                                         # rupees
    notes: str | None = Field(default=None, sa_column=Column(Text))

    updated_at: datetime
    updated_by: int | None = Field(default=None, foreign_key="crm_users.id")