"""Shared test setup: environment (no .env, in-memory SQLite), constants and AuthTestCase.

Import this module before anything from app, so the environment is set first.
"""
import os

# Must be set before anything from app is imported
os.environ.update({
    "ENV_FILE": "",                 # never read .env
    "DATABASE_URL": "sqlite://",
    "READ_DB_HOST": "",
    "READ_DATABASE_URL": "",
    "JWT_SECRET_KEY": "test-secret-key-for-unit-tests-only-0123456789",
    "CORS_ORIGINS": '["https://app.example.test"]',
    "REFRESH_COOKIE_SECURE": "true",
    "REFRESH_COOKIE_SAMESITE": "lax",
    "REFRESH_TOKEN_EXPIRE_DAYS": "7",
    "REFRESH_TOKEN_MAX_LIFETIME_DAYS": "30",
    "REFRESH_REUSE_GRACE_SECONDS": "30",
})

import hashlib  # noqa: E402
import unittest  # noqa: E402
from datetime import timedelta  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from pwdlib.hashers.bcrypt import BcryptHasher  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.crud.refresh_token import utcnow  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.refresh_token import RefreshToken  # noqa: E402
from app.models.user import User  # noqa: E402

ORIGIN = "https://app.example.test"
COOKIE = "koc_refresh"
EMAIL = "user@example.com"
PASSWORD = "correct horse battery staple"
GRACE = timedelta(seconds=30)    # matches REFRESH_REUSE_GRACE_SECONDS above
LOGIN, REFRESH, LOGOUT, ME = (f"/api/v1/auth/{p}" for p in ("login", "refresh", "logout", "me"))


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)

        def override_get_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        # https so the Secure cookie is stored and sent; no lifespan (no real database)
        self.client = TestClient(app, base_url="https://testserver")
        self.user_id = self.add_user(EMAIL, hash_password(PASSWORD))

    def tearDown(self):
        app.dependency_overrides.clear()
        self.client.close()
        self.engine.dispose()

    # ── helpers ──
    def add_user(self, email: str, hashed: str, active: bool = True) -> int:
        with Session(self.engine) as s:
            user = User(name="Test", email=email, hashed_password=hashed, is_active=active)
            s.add(user)
            s.commit()
            return user.id

    def login(self, email: str = EMAIL, password: str = PASSWORD):
        return self.client.post(LOGIN, data={"username": email, "password": password})

    def login_token(self) -> str:
        resp = self.login()
        self.assertEqual(resp.status_code, 200)
        return resp.cookies[COOKIE]

    def post_with_cookie(self, url: str, token: str | None, headers: dict | None = None):
        """Send exactly this cookie (or none), never the client's stored one."""
        self.client.cookies.clear()
        h = {"Origin": ORIGIN} if headers is None else dict(headers)
        if token is not None:
            h["Cookie"] = f"{COOKIE}={token}"
        return self.client.post(url, headers=h)

    def tokens(self) -> list[RefreshToken]:
        with Session(self.engine) as s:
            return list(s.exec(select(RefreshToken).order_by(RefreshToken.id)).all())

    def update_token(self, token: str, **values):
        with Session(self.engine) as s:
            row = s.exec(select(RefreshToken).where(
                RefreshToken.token_hash == hashlib.sha256(token.encode()).hexdigest())).one()
            for k, v in values.items():
                setattr(row, k, v)
            s.add(row)
            s.commit()

    def age_revocation(self, token: str, by: timedelta = GRACE + timedelta(seconds=1)):
        """Pretend the token was rotated `by` ago (default: just outside the grace window)."""
        self.update_token(token, revoked_at=utcnow() - by)

    def assert_cookie_cleared(self, resp):
        set_cookie = resp.headers.get("set-cookie", "")
        self.assertIn(f"{COOKIE}=", set_cookie)
        self.assertRegex(set_cookie.lower(), r"max-age=0|expires=thu, 01 jan 1970")

    def assert_family_revoked(self):
        self.assertTrue(all(t.revoked_at is not None for t in self.tokens()))
