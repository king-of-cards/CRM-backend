from datetime import datetime

from pydantic import EmailStr
from sqlalchemy import Column, DateTime, func
from sqlmodel import Field, SQLModel


# Shared fields: used by the table and by the request/response models
class UserBase(SQLModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr = Field(max_length=255, unique=True, index=True)


# table=True makes this a real database table
class User(UserBase, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime, server_default=func.now(), nullable=False),
    )


# Request body for POST /users (no id or created_at)
class UserCreate(UserBase):
    pass


# Response shape
class UserRead(UserBase):
    id: int
    created_at: datetime