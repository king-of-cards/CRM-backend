from datetime import datetime

from sqlmodel import Field, SQLModel


class RefreshToken(SQLModel, table=True):
    """One refresh token. Only the SHA-256 hash is stored, never the token itself.

    Tokens from one login share a family_id. Each refresh revokes the current token
    and creates its successor (replaced_by_id). All datetimes are timezone-aware UTC;
    SQLModel stores them as UTC (a plain DATETIME column on MySQL).
    """
    __tablename__ = "crm_refresh_tokens"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="crm_users.id", index=True, ondelete="CASCADE")
    token_hash: str = Field(max_length=64, unique=True)        # SHA-256 hex
    family_id: str = Field(max_length=36, index=True)          # UUID4 string
    created_at: datetime
    expires_at: datetime
    family_created_at: datetime
    revoked_at: datetime | None = None
    replaced_by_id: int | None = None   # no foreign key, so cleanup can delete rows in any order
