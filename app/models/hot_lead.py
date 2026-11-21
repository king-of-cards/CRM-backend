from datetime import datetime

from sqlmodel import Field, SQLModel
from sqlalchemy import UniqueConstraint


class CrmHotLead(SQLModel, table=True):
    """Hot flags for the four CRM sources, including read-only Spin workflow rows."""
    __tablename__ = "crm_hot_leads"
    __table_args__ = (
        UniqueConstraint("source", "lead_key", name="uq_crm_hot_leads_source_key"),
    )

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(max_length=30)
    lead_key: str = Field(max_length=100)
    hot_marked_by: int = Field()   # user id; no foreign key: history outlives deleted logins
    hot_marked_at: datetime
