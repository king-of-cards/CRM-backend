"""Auth tests: login, refresh-token rotation, logout, CSRF check, password handling.

Runs against an in-memory SQLite database only. The .env file is never read and no
real database is contacted. Run from the project root:

    python -m unittest discover -s tests -t . -v
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
LOGIN, REFRESH, LOGOUT, ME =(f"/api/v1/auth/{p}" for p in ("login", "refresh", "logout", "me"))


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


class LoginTests(AuthTestCase):
    def test_login_sets_cookie(self):
        resp = self.login()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(set(body), {"access_token", "token_type"})
        self.assertEqual(body["token_type"], "bearer")

        raw = resp.cookies[COOKIE]
        header = resp.headers["set-cookie"].lower()
        for part in ("httponly", "secure", "samesite=lax", "path=/api/v1/auth"):
            self.assertIn(part, header)
        max_age = int(header.split("max-age=")[1].split(";")[0])
        self.assertTrue(7 * 86400 - 60 <= max_age <= 7 * 86400)

        [row] = self.tokens()
        self.assertEqual(row.token_hash, hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotIn(raw, (row.token_hash, row.family_id))   # raw token never stored
        self.assertIsNone(row.revoked_at)

    def test_access_token_still_works_for_me(self):
        access = self.login().json()["access_token"]
        resp = self.client.get(ME, headers={"Authorization": f"Bearer {access}"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["email"], EMAIL)

    def test_wrong_password_401_and_no_cookie(self):
        resp = self.login(password="wrong password")
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn(COOKIE, resp.cookies)
        self.assertEqual(self.tokens(), [])

    def test_housekeeping_deletes_old_tokens_on_login(self):
        now = utcnow()
        with Session(self.engine) as s:
            for i, (expires, revoked) in enumerate([
                (now - timedelta(days=31), None),                       # expired long ago
                (now + timedelta(days=1), now - timedelta(days=31)),    # revoked long ago
                (now + timedelta(days=1), now - timedelta(days=1)),     # revoked recently: kept
            ]):
                s.add(RefreshToken(user_id=self.user_id, token_hash=f"{i:064d}",
                                   family_id=f"family-{i}", created_at=now - timedelta(days=40),
                                   expires_at=expires, family_created_at=now - timedelta(days=40),
                                   revoked_at=revoked))
            s.commit()
        self.login_token()
        remaining = {t.family_id for t in self.tokens()}
        self.assertNotIn("family-0", remaining)
        self.assertNotIn("family-1", remaining)
        self.assertIn("family-2", remaining)
        self.assertEqual(len(remaining), 2)   # family-2 + the new login


class RefreshTests(AuthTestCase):
    def test_refresh_rotates_and_old_cookie_fails(self):
        old = self.login_token()
        resp = self.post_with_cookie(REFRESH, old)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.json()), {"access_token", "token_type"})
        new = resp.cookies[COOKIE]
        self.assertNotEqual(new, old)

        old_row, new_row = self.tokens()
        self.assertIsNotNone(old_row.revoked_at)
        self.assertEqual(old_row.replaced_by_id, new_row.id)
        self.assertEqual(old_row.family_id, new_row.family_id)
        self.assertEqual(old_row.family_created_at, new_row.family_created_at)
        self.assertIsNone(new_row.revoked_at)

        self.age_revocation(old)                       # outside the grace window
        reused = self.post_with_cookie(REFRESH, old)
        self.assertEqual(reused.status_code, 401)
        self.assert_cookie_cleared(reused)

    def test_reuse_revokes_whole_family(self):
        old = self.login_token()
        new = self.post_with_cookie(REFRESH, old).cookies[COOKIE]
        self.age_revocation(old)                       # outside the grace window
        with self.assertLogs("app.crud.refresh_token", "WARNING") as logs:
            self.assertEqual(self.post_with_cookie(REFRESH, old).status_code, 401)   # reuse
        logged = "\n".join(logs.output)
        self.assertIn("reuse detected", logged)
        for secret in (old, new, hashlib.sha256(old.encode()).hexdigest()):
            self.assertNotIn(secret, logged)                   # no tokens or hashes in logs
        self.assert_family_revoked()
        self.assertEqual(self.post_with_cookie(REFRESH, new).status_code, 401)   # successor dead too

    def test_missing_and_unknown_cookie_401(self):
        for token in (None, "not-a-real-token"):
            resp = self.post_with_cookie(REFRESH, token)
            self.assertEqual(resp.status_code, 401)
            self.assert_cookie_cleared(resp)

    def test_expired_token_fails_and_revokes_family(self):
        token = self.login_token()
        self.update_token(token, expires_at=utcnow() - timedelta(seconds=1))
        resp = self.post_with_cookie(REFRESH, token)
        self.assertEqual(resp.status_code, 401)
        self.assert_cookie_cleared(resp)
        self.assert_family_revoked()

    def test_family_older_than_max_lifetime_fails(self):
        token = self.login_token()
        self.update_token(token, family_created_at=utcnow() - timedelta(days=30, seconds=1))
        self.assertEqual(self.post_with_cookie(REFRESH, token).status_code, 401)
        self.assert_family_revoked()

    def test_inactive_user_fails_and_revokes_family(self):
        token = self.login_token()
        with Session(self.engine) as s:
            user = s.get(User, self.user_id)
            user.is_active = False
            s.add(user)
            s.commit()
        resp = self.post_with_cookie(REFRESH, token)
        self.assertEqual(resp.status_code, 401)
        self.assert_cookie_cleared(resp)
        self.assert_family_revoked()

    def test_new_token_capped_at_family_max_lifetime(self):
        token = self.login_token()
        family_start = utcnow() - timedelta(days=29)
        self.update_token(token, family_created_at=family_start)
        resp = self.post_with_cookie(REFRESH, token)
        self.assertEqual(resp.status_code, 200)
        new_row = self.tokens()[-1]
        self.assertLessEqual(new_row.expires_at, family_start + timedelta(days=30))


class GraceWindowTests(AuthTestCase):
    def test_reuse_within_grace_succeeds(self):
        old = self.login_token()
        tab1 = self.post_with_cookie(REFRESH, old).cookies[COOKIE]      # first tab rotates
        with self.assertNoLogs("app.crud.refresh_token", "WARNING"):
            resp = self.post_with_cookie(REFRESH, old)                  # second tab, same cookie
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.json()), {"access_token", "token_type"})
        tab2 = resp.cookies[COOKIE]
        self.assertNotIn(tab2, (old, tab1))

        rows = self.tokens()
        self.assertEqual(len({r.family_id for r in rows}), 1)           # same family
        self.assertEqual(sum(r.revoked_at is None for r in rows), 2)    # tab1 and tab2 active
        for token in (tab1, tab2):                                      # both keep working
            self.assertEqual(self.post_with_cookie(REFRESH, token).status_code, 200)

    def test_reuse_after_grace_revokes_family(self):
        old = self.login_token()
        new = self.post_with_cookie(REFRESH, old).cookies[COOKIE]
        self.age_revocation(old)
        with self.assertLogs("app.crud.refresh_token", "WARNING"):
            resp = self.post_with_cookie(REFRESH, old)
        self.assertEqual(resp.status_code, 401)
        self.assert_cookie_cleared(resp)
        self.assert_family_revoked()
        self.assertEqual(self.post_with_cookie(REFRESH, new).status_code, 401)

    def test_reuse_of_logged_out_token_fails_within_grace(self):
        # Logged out directly (no replaced_by_id)
        token = self.login_token()
        self.assertEqual(self.post_with_cookie(LOGOUT, token).status_code, 204)
        self.assertEqual(self.post_with_cookie(REFRESH, token).status_code, 401)

        # Rotated seconds ago, then the session logged out: the old token still has
        # replaced_by_id and a recent revoked_at, but the family is dead
        old = self.login_token()
        new = self.post_with_cookie(REFRESH, old).cookies[COOKIE]
        self.assertEqual(self.post_with_cookie(LOGOUT, new).status_code, 204)
        with self.assertNoLogs("app.crud.refresh_token", "WARNING"):   # routine, not theft
            resp = self.post_with_cookie(REFRESH, old)
        self.assertEqual(resp.status_code, 401)
        self.assert_cookie_cleared(resp)
        self.assert_family_revoked()

    def test_grace_respects_family_max_lifetime(self):
        old = self.login_token()
        self.post_with_cookie(REFRESH, old)
        family_start = utcnow() - timedelta(days=29)
        self.update_token(old, family_created_at=family_start)
        resp = self.post_with_cookie(REFRESH, old)                      # within grace
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(self.tokens()[-1].expires_at, family_start + timedelta(days=30))

        self.update_token(old, family_created_at=utcnow() - timedelta(days=30, seconds=1))
        self.assertEqual(self.post_with_cookie(REFRESH, old).status_code, 401)
        self.assert_family_revoked()

    def test_grace_does_not_apply_to_inactive_user(self):
        old = self.login_token()
        self.post_with_cookie(REFRESH, old)
        with Session(self.engine) as s:
            user = s.get(User, self.user_id)
            user.is_active = False
            s.add(user)
            s.commit()
        self.assertEqual(self.post_with_cookie(REFRESH, old).status_code, 401)
        self.assert_family_revoked()


class LogoutTests(AuthTestCase):
    def test_logout_revokes_and_refresh_fails(self):
        token = self.login_token()
        resp = self.post_with_cookie(LOGOUT, token)
        self.assertEqual(resp.status_code, 204)
        self.assert_cookie_cleared(resp)
        self.assert_family_revoked()
        with self.assertNoLogs("app.crud.refresh_token", "WARNING"):   # routine, not theft
            self.assertEqual(self.post_with_cookie(REFRESH, token).status_code, 401)

    def test_logout_twice_and_without_cookie_is_safe(self):
        token = self.login_token()
        self.assertEqual(self.post_with_cookie(LOGOUT, token).status_code, 204)
        self.assertEqual(self.post_with_cookie(LOGOUT, token).status_code, 204)
        self.assertEqual(self.post_with_cookie(LOGOUT, None).status_code, 204)


class CsrfTests(AuthTestCase):
    def test_wrong_origin_403_and_token_untouched(self):
        token = self.login_token()
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {},
                        {"Referer": "https://evil.example/page"}):
            for url in (REFRESH, LOGOUT):
                resp = self.post_with_cookie(url, token, headers=headers)
                self.assertEqual(resp.status_code, 403, (url, headers))
        self.assertIsNone(self.tokens()[0].revoked_at)        # nothing was revoked
        self.assertEqual(self.post_with_cookie(REFRESH, token).status_code, 200)

    def test_referer_from_allowed_origin_is_accepted(self):
        token = self.login_token()
        resp = self.post_with_cookie(REFRESH, token, headers={"Referer": f"{ORIGIN}/dashboard"})
        self.assertEqual(resp.status_code, 200)


class PasswordTests(AuthTestCase):
    def test_overlong_passwords_return_401(self):
        bcrypt_hash = BcryptHasher(rounds=4).hash("short-password")
        self.add_user("legacy@example.com", bcrypt_hash)
        cases = [
            ("legacy@example.com", "x" * 200),   # bcrypt, over 128 characters (was 500)
            ("legacy@example.com", "x" * 73),    # bcrypt, over 72 bytes (was 500)
            (EMAIL, "x" * 129),                   # Argon2, over 128 characters
            ("nobody@example.com", "x" * 200),   # unknown user
        ]
        for email, password in cases:
            resp = self.login(email, password)
            self.assertEqual(resp.status_code, 401, (email, len(password)))
        self.assertEqual(self.tokens(), [])

    def test_bcrypt_is_rehashed_to_argon2(self):
        user_id = self.add_user("legacy@example.com", BcryptHasher(rounds=4).hash(PASSWORD))
        self.assertEqual(self.login("legacy@example.com", PASSWORD).status_code, 200)
        with Session(self.engine) as s:
            self.assertTrue(s.get(User, user_id).hashed_password.startswith("$argon2"))
        self.assertEqual(self.login("legacy@example.com", PASSWORD).status_code, 200)


if __name__ == "__main__":
    unittest.main()
