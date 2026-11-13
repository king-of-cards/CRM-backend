from datetime import datetime

from sqlmodel import Field, SQLModel


class Branch(SQLModel, table=True):
    """A King of Cards store (branch). The id is the same id the spin wheel stores on each lead
    (the workflow database's spin_branch.id), so spin leads can show their branch.

    vendor_id: the vendor that handles this store's leads (RJ for Chamrajpet, Phodeo for the
    other Bangalore stores, OHP elsewhere). Spin leads from a branch go to that vendor unless
    someone assigns another vendor in the CRM.
    """
    __tablename__ = "crm_branches"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=150)
    code: str | None = Field(default=None, max_length=50)
    slug: str | None = Field(default=None, max_length=150)
    city: str | None = Field(default=None, max_length=100, index=True)
    address: str | None = Field(default=None, max_length=500)
    contact_number: str | None = Field(default=None, max_length=40)
    manager_name: str | None = Field(default=None, max_length=150)
    vendor_id: int | None = Field(default=None, foreign_key="crm_vendors.id", index=True)
    is_active: bool = True
    source: str = Field(default="manual", max_length=20)  # "workflow" (spin_branch) or "manual"
    created_at: datetime
    updated_at: datetime