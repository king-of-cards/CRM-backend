import logging
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

from app.core.config import settings

logger = logging.getLogger(__name__)

MAX_PASSWORD_LENGTH = 128   # characters; longer login passwords always fail
BCRYPT_MAX_BYTES = 72       # bcrypt 5.x raises ValueError above this

# Verifies both Argon2 and bcrypt hashes; new passwords are always hashed with Argon2
# (the first hasher in the list).
password_hash = PasswordHash((Argon2Hasher(), BcryptHasher()))

# Used when the email doesn't exist, so a failed login takes the same time
# whether or not the account exists (prevents guessing valid emails).
DUMMY_HASH = password_hash.hash("dummy-password-for-timing")


def hash_password(plain_password: str) -> str:
    return password_hash.hash(plain_password)


def verify_and_update_password(
    plain_password: str, hashed_password: str
) -> tuple[bool, str | None]:
    """Check a password. Never raises for bad input.

    Returns (valid, new_hash). new_hash is set when the stored hash should be
    replaced: a bcrypt hash (upgraded to Argon2) or outdated Argon2 parameters.
    Overlong passwords, passwords too long for a bcrypt hash and unrecognised
    hashes all count as a failed check.
    """
    try:
        if len(plain_password) > MAX_PASSWORD_LENGTH:
            return False, None
        if (BcryptHasher.identify(hashed_password)
                and len(plain_password.encode("utf-8")) > BCRYPT_MAX_BYTES):
            password_hash.verify(plain_password, DUMMY_HASH)  # similar timing to a real check
            return False, None
        return password_hash.verify_and_update(plain_password, hashed_password)
    except (ValueError, PwdlibError) as exc:
        # ValueError: bcrypt length limit, invalid encoding. PwdlibError: unknown hash format.
        logger.warning("Password check failed: %s", type(exc).__name__)
        return False, None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return verify_and_update_password(plain_password, hashed_password)[0]


def create_access_token(user_id: int, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Raises jwt.InvalidTokenError (incl. expired) if the token is not valid."""
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
