from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.crud import user as crud
from app.models.user import Role, UserCreate, UserRead, UserUpdate
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser


def _active_vendor(db, vendor_id: int) -> Vendor:
    """The vendor to link a vendor login to: 404 unknown, 422 inactive."""
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")
    if not vendor.is_active:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This vendor is inactive; choose an active vendor.")
    return vendor


def _forget_access() -> None:
    from app.services import access

    access.forget()             # who sees which lead depends on the logins' vendors

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: DbSession, _admin: AdminUser):
    """vendor_id (vendor logins, required): an active vendor to link; 404 unknown, 422 inactive."""
    if payload.vendor_id is not None:
        _active_vendor(db, payload.vendor_id)
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
    """Send only what changes: name, email (409 if another login has it), role, vendor_id (required
    for vendor logins, an active vendor; removed when the role isn't vendor), password (a new one,
    8+ characters), is_active. A new password, a role change or a block signs the login out
    everywhere (its access tokens stop working at once). An admin can't change their own role or
    block themselves (400)."""
    user = crud.get_user(db, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if not payload.model_fields_set:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing to change")
    changes = {f: getattr(payload, f) for f in payload.model_fields_set if f != "vendor_id"}
    if "role" in changes:
        changes["role"] = changes["role"].value
    if user.id == admin.id:
        if changes.get("role", user.role) != user.role:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't change your own role")
        if changes.get("is_active") is False:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't block your own login")
    if "email" in changes:
        other = crud.get_user_by_email(db, changes["email"])
        if other is not None and other.id != user.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    role = changes.get("role", user.role)
    current = db.get(VendorUser, user.id)
    if role == Role.VENDOR.value:
        vendor_id = payload.vendor_id if "vendor_id" in payload.model_fields_set else (current.vendor_id if current else None)
        if vendor_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A vendor login needs a vendor_id")
        if "vendor_id" in payload.model_fields_set:
            _active_vendor(db, vendor_id)
    else:
        if payload.vendor_id is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "vendor_id is only allowed when role is vendor")
        vendor_id = None
    try:
        crud.update_user(db, user, changes, vendor_id, link=True)
    except crud.EmailAlreadyRegistered:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    _forget_access()
    return crud.get_user_read(db, user_id)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, db: DbSession, admin: AdminUser):
    """Delete a login: its sessions, vendor link, notification read marks and own inbox go with it.
    History keeps the user id and shows "Deleted user". Refused (409) for your own login and for
    the last active admin."""
    user = crud.get_user(db, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You can't delete your own login")
    if user.role == Role.ADMIN.value and user.is_active and crud.active_admin_count(db) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "You can't delete the last active admin")
    crud.delete_user(db, user)
    _forget_access()


@router.get("/{user_id}", response_model=UserRead)
def get_user(user_id: int, db: DbSession, current_user: CurrentUser):
    # Admins can view anyone; other users can only view themselves.
    if current_user.role != Role.ADMIN.value and current_user.id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    user = crud.get_user_read(db, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user
