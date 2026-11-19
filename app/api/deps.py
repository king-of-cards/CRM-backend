from typing import Annotated, Literal
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import AfterValidator
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


DHRUVA_ONLY = "Dhruva logins can only use the Dhruva leads section."


def get_viewer(user: CurrentUser, db: DbSession) -> Viewer:
    """Admin, or a vendor login linked to its vendor (see app/services/access.py).
    Dhruva logins: 403 (they only use the Dhruva endpoints)."""
    try:
        return access.viewer_for(db, user)
    except access.NotLinked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This login isn't linked to a vendor.")
    except access.DhruvaOnly:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DHRUVA_ONLY)


def require_not_dhruva(user: CurrentUser) -> User:
    """Any login except Dhruva logins (for endpoints that only need a login)."""
    if user.role == Role.DHRUVA.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DHRUVA_ONLY)
    return user


NotDhruvaUser = Annotated[User, Depends(require_not_dhruva)]


def require_dhruva_section(user: CurrentUser) -> User:
    """The Dhruva leads section: admins and Dhruva logins (vendor logins: 403)."""
    if user.role not in (Role.ADMIN.value, Role.DHRUVA.value):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only admins and Dhruva logins can use the Dhruva leads section.")
    return user


DhruvaSectionUser = Annotated[User, Depends(require_dhruva_section)]


# Endpoints that admins and vendor users may call; results are limited to what the viewer may see
LeadViewer = Annotated[Viewer, Depends(get_viewer)]

# Lead lists: page sizes the screens offer (anything else: 422). Default 10.
PAGE_SIZES = (10, 25, 50, 100)


def _check_page_size(value: int) -> int:
    if value not in PAGE_SIZES:
        raise ValueError(f"page_size must be one of {', '.join(map(str, PAGE_SIZES))}")
    return value


PageSize = Annotated[int, Query(description="10, 25, 50 or 100 (default 10)"), AfterValidator(_check_page_size)]


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


def _dashboard_period_dependency(default_all: bool):
    """period=all, or date_from / date_to; nothing sent: the last 30 days (default_all=False)
    or all time (default_all=True)."""
    def dependency(
        period: str | None = Query(None, description='"all" for all time (instead of date_from / date_to)'),
        date_from: str | None = Query(None, description="First day, YYYY-MM-DD (India time), included"),
        date_to: str | None = Query(None, description="Last day, YYYY-MM-DD (India time), included"),
    ):
        if period is not None and period.strip():
            if period.strip().lower() != "all":
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail='period must be "all" (or leave it out and send date_from / date_to)')
            if (date_from or "").strip() or (date_to or "").strip():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="Use either period=all or date_from / date_to, not both")
            return periods.ALL_TIME
        if default_all and not (date_from or "").strip() and not (date_to or "").strip():
            return periods.ALL_TIME
        try:
            return periods.parse_period(date_from, date_to, required=True)
        except periods.PeriodError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return dependency


_dashboard_period = _dashboard_period_dependency(default_all=False)


# Dashboard: period=all, the dates asked for, or the last 30 days. Lists: a period only if dates are given.
DashboardPeriod = Annotated[periods.Period | periods.AllTime, Depends(_dashboard_period)]
# The Dhruva dashboard: the same rules, but all time when no dates are sent
AllTimeDefaultPeriod = Annotated[periods.Period | periods.AllTime, Depends(_dashboard_period_dependency(True))]
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
        update_status: list[str] | None = Query(None, description="Repeatable: converted, not_converted, "
                                                "purchased_elsewhere, purchased_with_us, will_call_back, interested, "
                                                "not_interested, pending"),
        quote: str | None = Query(None, description="Quoted amount: has, none, gt:<amount> or lt:<amount>"),
        conversion: str | None = Query(None, description="Conversion amount: has, none, gt:<amount> or lt:<amount>"),
        origin: list[str] | None = Query(None, description=f"Repeatable: {', '.join(allowed_origins)}"),
        also_in: Literal["yes", "no"] | None = Query(None, description="The phone also exists in another source "
                                                     "or Vendor -> KOC (that you may see)"),
        hot: Literal["yes", "no"] | None = Query(None, description="Only hot leads (yes) or non-hot leads (no)"),
        follow_up: Literal["due", "overdue", "today", "upcoming"] | None = Query(None),
        interest: list[str] | None = Query(None, include_in_schema=False),      # replaced: 400
        converted: str | None = Query(None, include_in_schema=False),           # replaced: 400
    ):
        if interest or converted:
            raise HTTPException(400, "interest and converted were replaced by update_status "
                                     "(e.g. update_status=interested or update_status=converted)")
        return lead_filters.common_filters(period, search, event_from, event_to, upcoming_days, vendor_id, unassigned,
                                           update_status, quote, conversion, origin, allowed_origins, source_label,
                                           also_in, hot, follow_up)
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
