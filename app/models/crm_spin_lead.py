from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Column
from sqlmodel import Field, SQLModel

from app.models.lead_origin import CRM_KEY_PREFIX


class CrmSpinLead(SQLModel, table=True):
    """A spin lead added or imported in the CRM.

    Spin leads normally come from the read-only workflow database (PostgreSQL spin_lead) and
    are not copied into MySQL. Leads the team adds or imports are stored here instead, with
    the same fields the spin list shows, and GET /spin-leads lists both together.

    additional_details holds the same keys as spin_lead.additional_details (pincode,
    occasionDate, otherWeddingServices, ...), so they appear as "additional_details.<key>".
    In lists these rows have id/lead_key "crm-<id>", which never clashes with a workflow id.
    """
    __tablename__ = "crm_spin_leads"

    id: int | None = Field(default=None, primary_key=True)
    origin: str = Field(max_length=10, index=True)               # added | imported

    # Same columns as spin_lead (hidden ones left out)
    name: str | None = Field(default=None, max_length=150)
    email: str | None = Field(default=None, max_length=150)
    phone: str = Field(max_length=20, index=True)
    phone_e164: str = Field(max_length=16, index=True)          # +919845012345, for duplicate checks
    occasion: str | None = Field(default=None, max_length=100)
    source: str | None = Field(default=None, max_length=50)
    campaign_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    customer_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    branch_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True, index=True))
    consent_at: datetime | None = None
    created_at: datetime = Field(index=True)                     # UTC
    additional_details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    created_by: int | None = Field(default=None, foreign_key="crm_users.id")
    updated_at: datetime
    is_deleted: bool = Field(default=False, index=True)

    @property
    def lead_key(self) -> str:
        return f"{CRM_KEY_PREFIX}{self.id}"
