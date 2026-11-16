from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.crud import user as crud
from app.models.user import Role, UserCreate, UserRead, UserUpdate
from app.models.vendor import Vendor

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: DbSession, _admin: AdminUser):
    """vendor_id (vendor logins, optional): the vendor to link; 404 if it doesn't exist."""
    if payload.vendor_id is not None and db.get(Vendor, payload.vendor_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")
    if crud.get_user_by_email(db, payload.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    try:
        user = crud.create_user(db, payload)
    except crud.EmailAlreadyRegistered:
        # A parallel request created the same email after the check above
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    return crud.get_user_read(db, user.id)


@router.get("", response_model=list[UserRead])
def list_users(
    db: DbSession,
    _admin: AdminUser,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    """Users ordered by id, with their vendor (vendor logins)."""
    return crud.list_users(db, skip, limit)


@router.patch("/{user_id}", response_model=UserRead)
def update_user(user_id: int, payload: UserUpdate, db: DbSession, admin: AdminUser):
    """Block (is_active false) or unblock a login. Blocking revokes all its sessions (refresh
    tokens); its access tokens stop working at once. An admin can't block themselves (400)."""
    user = crud.get_user(db, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id and not payload.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't block your own login")
    crud.set_active(db, user, payload.is_active)
    return crud.get_user_read(db, user_id)


@router.get("/{user_id}", response_model=UserRead)
def get_user(user_id: int, db: DbSession, current_user: CurrentUser):
    # Admins can view anyone; other users can only view themselves.
    if current_user.role != Role.ADMIN.value and current_user.id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    user = crud.get_user_read(db, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user
