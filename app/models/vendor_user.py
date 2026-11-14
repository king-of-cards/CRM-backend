from sqlmodel import Field, SQLModel


class VendorUser(SQLModel, table=True):
    """Which vendor a vendor login belongs to (one vendor per login).

    The table already exists in the CRM database; this model only reads it. A vendor login
    without a row here can't see any leads.
    """
    __tablename__ = "crm_vendor_users"

    user_id: int = Field(primary_key=True, foreign_key="crm_users.id")
    vendor_id: int = Field(foreign_key="crm_vendors.id", index=True)
