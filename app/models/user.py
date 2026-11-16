from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, EmailStr, model_validator
from sqlalchemy import Column, DateTime, func
from sqlmodel import Field, SQLModel


class Role(str, Enum):
    ADMIN = "admin"
    VENDOR = "vendor"


# Shared fields: used by the table and by the request/response models
class UserBase(SQLModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr = Field(max_length=255, unique=True, index=True)


# table=True makes this a real database table
class User(UserBase, table=True):
    __tablename__ = "crm_users"

    id: int | None = Field(default=None, primary_key=True)
    hashed_password: str | None = Field(default=None, max_length=255)
    role: str = Field(default=Role.VENDOR.value, max_length=20)
    is_active: bool = Field(default=True)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime, server_default=func.now(), nullable=False),
    )


# Request body for POST /users (plain password in, never stored)
class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)
    role: Role = Role.VENDOR
    # Vendor logins: the vendor to link (optional; without it the login stays unlinked and gets
    # 403 on lead endpoints until linked). Not allowed for admins.
    vendor_id: int | None = None

    @model_validator(mode="after")
    def _vendor_link(self):
        if self.role == Role.ADMIN and self.vendor_id is not None:
            raise ValueError("vendor_id is only allowed when role is vendor")
        return self


# Response shape (never includes the password hash)
class UserRead(UserBase):
    id: int
    role: str
    is_active: bool
    created_at: datetime
    vendor_id: int | None = None      # vendor logins only (from crm_vendor_users)
    vendor_name: str | None = None


# Request body for PATCH /users/{id}
class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")      # unknown fields (role, email...) answer 422
    is_active: bool     # false = blocked: can't log in; its sessions are revoked


class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"
