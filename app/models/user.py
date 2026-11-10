from datetime import datetime
from enum import Enum

from pydantic import EmailStr
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


# Response shape (never includes the password hash)
class UserRead(UserBase):
    id: int
    role: str
    is_active: bool
    created_at: datetime


class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"
