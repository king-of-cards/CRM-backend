import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.core.security import (
    DUMMY_HASH, MAX_PASSWORD_LENGTH, hash_password, verify_and_update_password, verify_password,
)
from app.models.user import User, UserCreate

logger = logging.getLogger(__name__)


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
    if len(password) > MAX_PASSWORD_LENGTH:
        return None  # rejected before any lookup, so the response is the same for every email
    user = get_user_by_email(db, email)
    if user is None or not user.hashed_password:
        verify_password(password, DUMMY_HASH)  # same timing as a real check
        return None
    valid, new_hash = verify_and_update_password(password, user.hashed_password)
    if not valid or not user.is_active:
        return None
    if new_hash:
        _save_upgraded_hash(db, user, new_hash)
    return user


def _save_upgraded_hash(db: Session, user: User, new_hash: str) -> None:
    """Replace a bcrypt (or outdated Argon2) hash after a successful login.
    A failure here is logged and must not block the login."""
    user_id = user.id
    try:
        user.hashed_password = new_hash
        db.add(user)
        db.commit()
        db.refresh(user)
    except SQLAlchemyError:
        db.rollback()
        logger.warning("Could not save upgraded password hash for user id %s", user_id)
