from datetime import datetime

from sqlalchemy import Index, Text
from sqlmodel import Field, SQLModel


class LeadActivity(SQLModel, table=True):
    __tablename__ = "crm_lead_activity"
    __table_args__ = (Index("ix_crm_lead_activity_source_key_created", "source", "lead_key", "created_at"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(max_length=30, index=True)
    lead_key: str = Field(max_length=100, index=True)
    action: str = Field(max_length=30, index=True)
    field: str | None = Field(default=None, max_length=100)
    old_value: str | None = Field(default=None, sa_type=Text)
    new_value: str | None = Field(default=None, sa_type=Text)
    actor_user_id: int | None = Field(default=None, foreign_key="crm_users.id")
    actor_name: str = Field(max_length=100)
    actor_role: str = Field(max_length=30)
    created_at: datetime
