import logging

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from app.core.security import (
    DUMMY_HASH, MAX_PASSWORD_LENGTH, hash_password, verify_and_update_password, verify_password,
)
from app.models.user import User, UserCreate, UserRead
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser

logger = logging.getLogger(__name__)


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.exec(select(User).where(User.email == email.strip().lower())).first()


def _with_vendor():
    """Users with their vendor link (if any), in one query."""
    return (
        select(User, VendorUser.vendor_id, Vendor.name)
        .outerjoin(VendorUser, VendorUser.user_id == User.id)
        .outerjoin(Vendor, Vendor.id == VendorUser.vendor_id)
    )


def _to_read(user: User, vendor_id: int | None, vendor_name: str | None) -> UserRead:
    data = user.model_dump(exclude={"hashed_password"})
    return UserRead(**data, vendor_id=vendor_id, vendor_name=vendor_name)


def get_user_read(db: Session, user_id: int) -> UserRead | None:
    row = db.exec(_with_vendor().where(User.id == user_id)).first()
    return _to_read(*row) if row else None


def list_users(db: Session, skip: int = 0, limit: int = 100) -> list[UserRead]:
    rows = db.exec(_with_vendor().order_by(User.id).offset(skip).limit(limit)).all()
    return [_to_read(*row) for row in rows]


class EmailAlreadyRegistered(Exception):
    """The email is taken (also raised when a parallel request inserted it first)."""


def create_user(db: Session, data: UserCreate) -> User:
    """Creates the user and, when a vendor_id is given (vendor logins), its crm_vendor_users link
    in one transaction. The caller checks that data.vendor_id exists."""
    user = User(
        name=data.name,
        email=data.email.strip().lower(),
        hashed_password=hash_password(data.password),
        role=data.role.value,
    )
    db.add(user)
    try:
        db.flush()                      # assigns user.id
        if data.vendor_id is not None:
            db.add(VendorUser(user_id=user.id, vendor_id=data.vendor_id))
        db.commit()
    except IntegrityError:
        # The email is the only unique column of crm_users
        db.rollback()
        raise EmailAlreadyRegistered() from None
    db.refresh(user)
    return user


def set_active(db: Session, user: User, is_active: bool) -> None:
    """Block (False) or unblock (True) a login. Blocking also revokes all its sessions, in the
    same transaction; blocked logins can't log in, refresh, or use an access token."""
    from app.crud import refresh_token

    user.is_active = is_active
    db.add(user)
    if not is_active:
        refresh_token.revoke_all_for_user(db, user.id)
    db.commit()


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
