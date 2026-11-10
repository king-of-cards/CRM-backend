from sqlmodel import Session, select

from app.core.security import DUMMY_HASH, hash_password, verify_password
from app.models.user import User, UserCreate


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.exec(select(User).where(User.email == email.strip().lower())).first()


def list_users(db: Session, skip: int = 0, limit: int = 100) -> list[User]:
    return list(db.exec(select(User).offset(skip).limit(limit)).all())


def create_user(db: Session, data: UserCreate) -> User:
    user = User(
        name=data.name,
        email=data.email.strip().lower(),
        hashed_password=hash_password(data.password),
        role=data.role.value,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    """Returns the user if email + password are correct and the account is active."""
    user = get_user_by_email(db, email)
    if user is None or not user.hashed_password:
        verify_password(password, DUMMY_HASH)  # same timing as a real check
        return None
    if not verify_password(password, user.hashed_password):
        return None
    if not user.is_active:
        return None
    return user
