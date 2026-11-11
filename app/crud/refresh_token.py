"""Refresh tokens: issue, rotate and revoke.

Only SHA-256 hashes are stored and tokens are compared by looking up their hash.
Raw tokens, hashes and cookies are never logged.
"""
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, update
from sqlmodel import Session, col, select

from app.core.config import settings
from app.models.refresh_token import RefreshToken
from app.models.user import User

logger = logging.getLogger(__name__)

# Revoked or expired tokens are kept this long (for reuse detection), then deleted at login
RETENTION_DAYS = 30


class RefreshError(Exception):
    """The refresh token can't be used. Always answered with 401."""


@dataclass(frozen=True)
class IssuedToken:
    raw: str              # goes into the cookie only
    expires_at: datetime  # aware UTC


def utcnow() -> datetime:
    """Aware UTC. SQLModel stores it as UTC (plain DATETIME on MySQL) and returns aware UTC."""
    return datetime.now(timezone.utc)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _issue(db: Session, user_id: int, family_id: str, family_created_at: datetime,
           now: datetime) -> tuple[RefreshToken, IssuedToken]:
    raw = secrets.token_urlsafe(32)
    # Never valid beyond the family's maximum lifetime
    expires_at = min(
        now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        family_created_at + timedelta(days=settings.REFRESH_TOKEN_MAX_LIFETIME_DAYS),
    )
    row = RefreshToken(
        user_id=user_id, token_hash=hash_token(raw), family_id=family_id,
        created_at=now, expires_at=expires_at, family_created_at=family_created_at,
    )
    db.add(row)
    return row, IssuedToken(raw, expires_at)


def _revoke_family(db: Session, family_id: str, now: datetime) -> None:
    db.exec(
        update(RefreshToken)
        .where(col(RefreshToken.family_id) == family_id, col(RefreshToken.revoked_at).is_(None))
        .values(revoked_at=now)
    )


def _find_locked(db: Session, raw: str | None) -> RefreshToken | None:
    """Look up a token by its hash with a row lock (SELECT ... FOR UPDATE)."""
    if not raw:
        return None
    return db.exec(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)).with_for_update()
    ).first()


def create_family(db: Session, user_id: int) -> IssuedToken:
    """Login: start a new session family. Also deletes this user's old tokens. Commits."""
    now = utcnow()
    cutoff = now - timedelta(days=max(RETENTION_DAYS, settings.REFRESH_TOKEN_MAX_LIFETIME_DAYS))
    db.exec(
        delete(RefreshToken).where(
            col(RefreshToken.user_id) == user_id,
            or_(col(RefreshToken.expires_at) < cutoff, col(RefreshToken.revoked_at) < cutoff),
        )
    )
    _, issued = _issue(db, user_id, str(uuid.uuid4()), now, now)
    db.commit()
    return issued


def _family_is_active(db: Session, family_id: str) -> bool:
    """True while the family still has an unrevoked token (logout revokes them all)."""
    return db.exec(
        select(RefreshToken.id)
        .where(col(RefreshToken.family_id) == family_id, col(RefreshToken.revoked_at).is_(None))
        .limit(1)
    ).first() is not None


def _within_grace(token: RefreshToken, now: datetime) -> bool:
    """Revoked by rotation (not by logout or family revocation) in the last grace seconds."""
    return (
        token.replaced_by_id is not None
        and token.revoked_at is not None
        and now - token.revoked_at <= timedelta(seconds=settings.REFRESH_REUSE_GRACE_SECONDS)
    )


def rotate(db: Session, raw: str | None) -> tuple[int, str, IssuedToken]:
    """Refresh: validate the token and replace it, in one transaction.

    Returns (user_id, role, new token). Raises RefreshError after committing any
    revocation (reuse, expiry, family too old, user missing or inactive).
    """
    now = utcnow()
    token = _find_locked(db, raw)
    if token is None:
        db.rollback()
        raise RefreshError("unknown")

    user = db.get(User, token.user_id)
    family_limit = token.family_created_at + timedelta(days=settings.REFRESH_TOKEN_MAX_LIFETIME_DAYS)
    usable = (token.expires_at > now and family_limit > now
              and user is not None and user.is_active)

    if token.revoked_at is not None:
        family_active = _family_is_active(db, token.family_id)
        in_grace = _within_grace(token, now)
        # Grace window: a parallel tab refreshed with the same token moments ago.
        # Issue another token in the same family instead of treating it as theft.
        if usable and family_active and in_grace:
            user_id, role = user.id, user.role
            _, issued = _issue(db, token.user_id, token.family_id, token.family_created_at, now)
            db.commit()
            return user_id, role, issued
        # Otherwise revoke the whole family. Only a rotated token presented again after
        # the grace window, while its family is still active, indicates a copied token.
        # After logout or family revocation it's routine and not logged.
        if family_active and token.replaced_by_id is not None and not in_grace:
            logger.warning("Refresh token reuse detected; session family revoked (user id %s)",
                           token.user_id)
        _revoke_family(db, token.family_id, now)
        db.commit()
        raise RefreshError("reused")

    if not usable:
        _revoke_family(db, token.family_id, now)
        db.commit()
        raise RefreshError("invalid")

    user_id, role = user.id, user.role
    token.revoked_at = now
    new_row, issued = _issue(db, token.user_id, token.family_id, token.family_created_at, now)
    db.flush()                         # assigns new_row.id
    token.replaced_by_id = new_row.id
    db.commit()
    return user_id, role, issued


def revoke_family_of(db: Session, raw: str | None) -> None:
    """Logout: revoke the whole family of this token, if it exists. Safe to call twice."""
    token = _find_locked(db, raw)
    if token is None:
        db.rollback()
        return
    _revoke_family(db, token.family_id, utcnow())
    db.commit()
