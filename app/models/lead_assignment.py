from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class LeadAssignment(SQLModel, table=True):
    """Manual vendor choices on a KOC -> Vendors lead (see lead_crm.vendor_set).

      added    the vendor has the lead (manual). While a lead has any added vendor, they replace
               the main automatic vendor (sheet / branch / area); partner rules (e.g. Kraftyz for
               gift leads) still add theirs.
      removed  the vendor never gets the lead from a rule again (removing sticks)
    """
    __tablename__ = "crm_lead_assignments"
    __table_args__ = (UniqueConstraint("source", "lead_key", "vendor_id", name="uq_crm_lead_assignments"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(max_length=30, index=True)
    lead_key: str = Field(max_length=100, index=True)
    vendor_id: int = Field(index=True)
    state: str = Field(max_length=10)                 # added | removed
    updated_by: int | None = Field(default=None)      # user id (no foreign key: logins can be deleted)
    updated_at: datetime
