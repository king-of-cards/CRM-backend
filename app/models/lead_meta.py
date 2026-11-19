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
    # False = never assigned automatically (sheet / branch / area rules): the lead stays
    # unassigned until an admin picks a vendor. Uploaded rows with an empty "Assigned to".
    auto_assign: bool = Field(default=True, sa_column_kwargs={"server_default": "1"})
    # "Update": the same 7 values as Vendor -> KOC (app/models/vendor_to_koc.UpdateStatus); None = Pending
    update_status: str | None = Field(default=None, max_length=30, index=True)
    quoted_date: date | None = None
    follow_up_date: date | None = None
    quoted_amount: int | None = None                                         # rupees
    conversion_amount: int | None = None                                     # rupees
    # Legacy (replaced by update_status, see migrations/2026-11-16_update_and_amounts.sql): kept in
    # the table for the rollback; the API never reads them and only writes the insert defaults.
    interest: str | None = Field(default=None, max_length=30)
    converted: bool = Field(default=False)
    converted_at: datetime | None = None
    notes: str | None = Field(default=None, sa_column=Column(Text))

    updated_at: datetime
    updated_by: int | None = Field(default=None, foreign_key="crm_users.id")
