from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, DbSession, require_trusted_origin
from app.core.config import settings
from app.core.security import create_access_token
from app.crud import refresh_token as tokens
from app.crud import user as crud
from app.models.user import Token, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])

# The refresh cookie is only sent to /api/v1/auth/* (login, refresh, logout)
REFRESH_COOKIE_PATH = "/api/v1/auth"


def _set_refresh_cookie(response: Response, issued: tokens.IssuedToken) -> None:
    max_age = max(0, int((issued.expires_at - tokens.utcnow()).total_seconds()))
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=issued.raw,
        max_age=max_age,
        path=REFRESH_COOKIE_PATH,
        secure=settings.REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        secure=settings.REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
    )


def _refresh_rejected() -> JSONResponse:
    response = JSONResponse(
        {"detail": "Invalid refresh token"},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )
    _clear_refresh_cookie(response)
    return response


@router.post("/login", response_model=Token)
def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()], response: Response, db: DbSession
):
    # The OAuth2 form calls the field "username"; we put the email there.
    user = crud.authenticate(db, form.username, form.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id, role = user.id, user.role
    _set_refresh_cookie(response, tokens.create_family(db, user_id))
    return Token(access_token=create_access_token(user_id, role))


@router.post(
    "/refresh",
    response_model=Token,
    dependencies=[Depends(require_trusted_origin)],
    responses={401: {"description": "Missing, invalid, expired or reused refresh token"},
               403: {"description": "Origin not allowed"}},
)
def refresh(request: Request, response: Response, db: DbSession):
    try:
        user_id, role, issued = tokens.rotate(db, request.cookies.get(settings.REFRESH_COOKIE_NAME))
    except tokens.RefreshError:
        return _refresh_rejected()
    _set_refresh_cookie(response, issued)
    return Token(access_token=create_access_token(user_id, role))


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_trusted_origin)],
    responses={403: {"description": "Origin not allowed"}},
)
def logout(request: Request, db: DbSession) -> Response:
    tokens.revoke_family_of(db, request.cookies.get(settings.REFRESH_COOKIE_NAME))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response)
    return response


@router.get("/me", response_model=UserRead)
def read_me(current_user: CurrentUser):
    return current_user
