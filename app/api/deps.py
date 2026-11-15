from typing import Annotated, Literal
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session

from app.core.config import settings
from app.core.security import decode_access_token
from app.crud import user as crud
from app.db.session import get_db
from app.models.user import Role, User
from app.services import access, periods
from app.services.access import Viewer

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


def get_viewer(user: CurrentUser, db: DbSession) -> Viewer:
    """Admin, or a vendor login linked to its vendor (see app/services/access.py)."""
    try:
        return access.viewer_for(db, user)
    except access.NotLinked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This login isn't linked to a vendor.")


# Endpoints that admins and vendor users may call; results are limited to what the viewer may see
LeadViewer = Annotated[Viewer, Depends(get_viewer)]


def _period(required: bool):
    def dependency(
        date_from: str | None = Query(None, description="First day, YYYY-MM-DD (India time), included"),
        date_to: str | None = Query(None, description="Last day, YYYY-MM-DD (India time), included"),
    ):
        try:
            return periods.parse_period(date_from, date_to, required=required)
        except periods.PeriodError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return dependency


# Dashboard: the period asked for, or the last 30 days. Lists: a period only if dates are given.
DashboardPeriod = Annotated[periods.Period, Depends(_period(required=True))]
OptionalPeriod = Annotated[periods.Period | None, Depends(_period(required=False))]


def common_filter_params(allowed_origins: tuple[str, ...], source_label: str):
    """The common list filters (see app/services/lead_filters.py) as a dependency; each source's
    list, export and stats endpoints use the same one."""
    from app.services import lead_filters

    def dependency(
        period: OptionalPeriod,
        search: str | None = Query(None, max_length=100, description="Name, phone, email (where the source has them)"),
        event_from: str | None = Query(None, description="Event date from, YYYY-MM-DD (included)"),
        event_to: str | None = Query(None, description="Event date to, YYYY-MM-DD (included)"),
        upcoming_days: int | None = Query(None, description="Event from today to today + N days"),
        vendor_id: list[int] | None = Query(None, description="Assigned to (repeatable); admins only"),
        unassigned: bool = Query(False, description="No vendor; admins only"),
        interest: list[str] | None = Query(None, description="Repeatable: interested, not_interested, other, follow_up, not_set"),
        converted: Literal["yes", "no"] | None = Query(None),
        quote: str | None = Query(None, description="has, none, gt:<amount> or lt:<amount>"),
        origin: list[str] | None = Query(None, description=f"Repeatable: {', '.join(allowed_origins)}"),
    ):
        return lead_filters.common_filters(period, search, event_from, event_to, upcoming_days, vendor_id, unassigned,
                                           interest, converted, quote, origin, allowed_origins, source_label)
    return dependency


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
