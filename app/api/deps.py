from typing import Annotated
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session

from app.core.config import settings
from app.core.security import decode_access_token
from app.crud import user as crud
from app.db.session import get_db
from app.models.user import Role, User

# tokenUrl makes the "Authorize" button in /docs work
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], db: DbSession) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise credentials_error

    user = crud.get_user(db, user_id)
    if user is None or not user.is_active:
        raise credentials_error
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != Role.ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def _origin_of(url: str | None) -> str | None:
    """'https://app.example.com/path' -> 'https://app.example.com'; None if not a URL."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def require_trusted_origin(request: Request) -> None:
    """CSRF check for endpoints that use the refresh cookie.

    The Origin header (or Referer if Origin is absent) must be one of CORS_ORIGINS.
    """
    allowed = {_origin_of(o) for o in settings.CORS_ORIGINS}
    origin = request.headers.get("origin")
    source = _origin_of(origin) if origin is not None else _origin_of(request.headers.get("referer"))
    if source is None or source not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin not allowed")
