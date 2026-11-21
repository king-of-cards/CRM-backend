from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Column
from sqlmodel import Field, SQLModel

KOC_KEY_PREFIX = "kd-"


class KocDhruvaLead(SQLModel, table=True):
    """A KOC -> Dhruva lead uploaded in the CRM (the history before the spin cut-off).

    KOC -> Dhruva normally lists workflow spin_lead rows (read-only PostgreSQL) by rule. Rows the
    admins upload are stored here instead, with the same columns as crm_spin_leads (additional_details
    holds occasionType, occasionDate, estimatedBudgetPerCard, quantityRequired, pincode, …). Every
    uploaded row is listed (the budget / quantity rule is only shown, not a filter). Lead keys are
    "kd-<id>"; the section's CRM details, hot flags and activity use source "koc_to_dhruva".
    """
    __tablename__ = "crm_koc_dhruva_leads"

    id: int | None = Field(default=None, primary_key=True)
    origin: str = Field(max_length=10, index=True)               # imported

    name: str | None = Field(default=None, max_length=150)
    email: str | None = Field(default=None, max_length=150)
    phone: str = Field(max_length=20, index=True)
    phone_e164: str = Field(max_length=16, index=True)
    occasion: str | None = Field(default=None, max_length=100)
    source: str | None = Field(default=None, max_length=50)
    campaign_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    customer_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    branch_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True, index=True))
    consent_at: datetime | None = None
    created_at: datetime = Field(index=True)                     # UTC ("Received")
    additional_details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    created_by: int | None = Field(default=None, foreign_key="crm_users.id")
    updated_at: datetime
    is_deleted: bool = Field(default=False, index=True)

    @property
    def lead_key(self) -> str:
        return f"{KOC_KEY_PREFIX}{self.id}"
