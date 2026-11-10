from fastapi import APIRouter, HTTPException, status

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.crud import user as crud
from app.models.user import Role, UserCreate, UserRead

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: DbSession, _admin: AdminUser):
    if crud.get_user_by_email(db, payload.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    return crud.create_user(db, payload)


@router.get("", response_model=list[UserRead])
def list_users(db: DbSession, _admin: AdminUser, skip: int = 0, limit: int = 100):
    return crud.list_users(db, skip, limit)


@router.get("/{user_id}", response_model=UserRead)
def get_user(user_id: int, db: DbSession, current_user: CurrentUser):
    # Admins can view anyone; other users can only view themselves.
    if current_user.role != Role.ADMIN.value and current_user.id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    user = crud.get_user(db, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user
