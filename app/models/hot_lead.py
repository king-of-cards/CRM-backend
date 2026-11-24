from datetime import datetime

from sqlmodel import Field, SQLModel
from sqlalchemy import Index, UniqueConstraint


class CrmHotLead(SQLModel, table=True):
    """Hot flags for the four CRM sources, including read-only Spin workflow rows: one per vendor copy
    (vendor_id; 0 = the vendor-less row, which koc_to_dhruva uses)."""
    __tablename__ = "crm_hot_leads"
    __table_args__ = (
        UniqueConstraint("source", "lead_key", "vendor_id", name="uq_crm_hot_leads_source_key_vendor"),
        # A vendor's hot leads of a source (hot filter, dashboards): migrations/2026-11-24_performance_indexes.sql
        Index("ix_crm_hot_leads_source_vendor", "source", "vendor_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(max_length=30)
    lead_key: str = Field(max_length=100)
    vendor_id: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    hot_marked_by: int = Field()   # user id; no foreign key: history outlives deleted logins
    hot_marked_at: datetime
