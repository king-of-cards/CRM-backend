"""The view-only role ("viewer", shown as "View only").

Reads everything an admin can read; every POST / PUT / PATCH / DELETE answers 403 with
"View-only logins cannot change data." (except login, refresh, logout). In-memory SQLite and fake
workflow data only (see test_crm_leads_api / dhruva_fixtures). Real access tokens are used (the
get_current_user override of ApiTestBase is removed after seeding), so the checks run exactly as
in production. Run:

    python -m unittest discover -s tests -t . -v
"""
import io
import re
import unittest
from datetime import datetime, timezone
from unittest import mock

from tests.base import ORIGIN, PASSWORD, AuthTestCase, LOGIN, LOGOUT, ME, REFRESH, COOKIE
from tests.dhruva_fixtures import DhruvaTestBase, sample_workbook
from tests.test_crm_leads_api import SAMPLE, ApiTestBase

from openpyxl import load_workbook  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.api import deps  # noqa: E402
from app.api.deps import VIEW_ONLY, VIEWER_WRITE_EXCEPTIONS, get_current_user  # noqa: E402
from app.api.v1.endpoints import vendors as vendors_ep  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models.dhruva import DhruvaLead  # noqa: E402
from app.models.lead_meta import LeadMeta  # noqa: E402
from app.models.notification import CrmNotification  # noqa: E402
from app.models.user import Role, User  # noqa: E402
from app.models.vendor_to_koc import VendorToKocLead  # noqa: E402
from app.models.wedding_lead import WeddingLead  # noqa: E402
from app.services import access  # noqa: E402
from app.services import notifications as notification_service  # noqa: E402

API = "/api/v1"
VOLATILE = {"generated_at"}


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class ViewerTestBase(DhruvaTestBase, unittest.TestCase):
    """Admin, vendor logins (rj, phodeo, ohp), Dhruva logins and one viewer; a lead in every
    section (the same phone in Wedding, Vendor -> KOC and Dhruva, for also_in)."""

    def setUp(self):
        super().setUp()
        with Session(self.engine, expire_on_commit=False) as s:
            self.users["viewer"] = User(name="Viewer", email="viewer@example.com", hashed_password="x",
                                        role=Role.VIEWER.value)
            s.add(self.users["viewer"])
            s.commit()
            s.refresh(self.users["viewer"])
        # Seed through the API as the existing tests do (get_current_user override)
        self.keys = {"spin": self.import_spin({"assigned_to": "Phodeo · auto", **SAMPLE["spin"]})["lead_key"]}
        for source in ("whatsapp", "wedding", "wedding_planner"):
            added = ApiTestBase.add(self, source, SAMPLE[source])        # DhruvaTestBase.add is the Dhruva one
            self.assertEqual(added.status_code, 201, added.text)
            self.keys[source] = added.json()["lead"]["lead_key"]
        self.assertEqual(self.put_crm("admin", "wedding", self.keys["wedding"],
                                      {"update_status": "interested", "quoted_amount": 15000}).status_code, 200)
        with self.logged_in("admin"):
            self.assertEqual(self.client.post(f"{API}/leads/wedding/{self.keys['wedding']}/hot").status_code, 200)
        dhruva = self.add(who="dhruva", name="Dhruva Lead", venue="Hall", date="Dec 4th", phone="9845010003",
                          event_type="wedding")
        self.assertEqual(dhruva.status_code, 201, dhruva.text)
        self.dhruva_id = dhruva.json()["id"]
        now = utc(2026, 11, 15, 5, 0)
        with Session(self.engine) as s:
            vtk = VendorToKocLead(vendor_id=self.vendor_ids["phodeo"], phone="9845010003", name="KOC Lead",
                                  city="Bengaluru", source="manual", created_by=self.admin.id, created_at=now,
                                  updated_at=now, update_status="interested", status_updated_at=now,
                                  status_updated_by=self.admin.id)
            s.add(vtk)
            s.commit()
            self.vtk_id = vtk.id
        # From here on: real bearer tokens
        app.dependency_overrides.pop(get_current_user, None)
        self.tokens = {who: create_access_token(u.id, u.role) for who, u in self.users.items()}

    def req(self, who, method, path, **kw):
        headers = {"Authorization": f"Bearer {self.tokens[who]}"} if who else {}
        return self.client.request(method, f"{API}{path}", headers=headers, **kw)

    @staticmethod
    def body(resp):
        kind = resp.headers.get("content-type", "")
        if "json" in kind:
            data = resp.json()
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k not in VOLATILE}
            return data
        if "spreadsheetml" in kind:
            ws = load_workbook(io.BytesIO(resp.content)).active
            return [[c.value for c in row] for row in ws.iter_rows()]
        return resp.text


# ── 1. Reads: the same as an admin ─────────────────────────────────────
class ViewerReadsTests(ViewerTestBase):
    def read_paths(self) -> list[str]:
        w, k = self.keys["wedding"], self.keys
        paths = [
            # Four sources: lists, stats, filter options, also_in filter
            "/spin-leads?page_size=100", "/spin-leads/stats", "/spin-leads/filter-options",
            "/whatsapp-leads?page_size=100", "/whatsapp-leads/stats", "/whatsapp-leads/filter-options",
            "/wedding-leads?page_size=100", "/wedding-leads/stats", "/wedding-leads/filter-options",
            "/wedding-leads?page_size=100&also_in=yes",
            "/wedding-plans?page_size=100", "/wedding-plans/stats", "/wedding-plans/filter-options",
            # Lead details, CRM, activity (admin-only fields)
            f"/leads/wedding/{w}/crm", f"/leads/wedding/{w}/activity",
            f"/crm-leads/wedding/{w}", "/crm-leads/wedding/fields",
            # Vendors -> KOC
            "/vendor-to-koc/leads?page_size=100", "/vendor-to-koc/leads/stats", f"/vendor-to-koc/leads/{self.vtk_id}",
            f"/vendor-to-koc/leads/{self.vtk_id}/activity", "/vendor-to-koc/vendors",
            "/vendor-to-koc/leads?page_size=100&also_in=yes",
            # Dhruva
            "/dhruva/leads?page_size=100", "/dhruva/leads/stats", "/dhruva/leads/filter-options",
            f"/dhruva/leads/{self.dhruva_id}", f"/dhruva/leads/{self.dhruva_id}/activity",
            "/dhruva/reminders/follow-ups",
            # Dashboards (refresh allowed: it only clears memory)
            "/analytics/overview", "/analytics/overview?refresh=true", "/analytics/vendor-to-koc",
            "/analytics/vendor-to-koc?period=all", "/analytics/dhruva",
            # Reminders, vendors, branches
            "/reminders/follow-ups", "/reminders/follow-ups?sources=wedding,vendor_to_koc,dhruva",
            "/vendors?include_inactive=true", "/branches",
        ]
        for source in k:
            paths.append(f"/crm-leads/{source}/fields")
        return paths

    def test_every_read_matches_the_admin_response(self):
        for path in self.read_paths():
            admin = self.req("admin", "GET", path)
            viewer = self.req("viewer", "GET", path)
            self.assertEqual(admin.status_code, 200, f"{path}: {admin.text[:200]}")
            self.assertEqual(viewer.status_code, 200, f"{path}: {viewer.text[:200]}")
            self.assertEqual(self.body(viewer), self.body(admin), path)

    def test_admin_only_fields_are_included(self):
        vtk = self.req("viewer", "GET", f"/vendor-to-koc/leads/{self.vtk_id}").json()
        self.assertEqual(vtk["status_updated_by"], self.admin.id)
        dash = self.req("viewer", "GET", "/analytics/vendor-to-koc?period=all").json()
        for key in ("previous_received", "per_vendor", "top_cities", "by_source", "latest"):
            self.assertIn(key, dash)
        items = self.req("viewer", "GET", "/wedding-leads?page_size=100").json()["items"]
        lead = next(i for i in items if i["crm"]["lead_key"] == self.keys["wedding"])
        self.assertTrue(lead["also_in"])                                   # Vendor -> KOC and Dhruva phone
        self.assertIn("vendor_to_koc", {ref["source"] for ref in lead["also_in"]})
        # A vendor login still sees less (unchanged)
        vendor = self.req("phodeo", "GET", f"/vendor-to-koc/leads/{self.vtk_id}").json()
        self.assertNotIn("status_updated_by", vendor)

    def test_exports_and_templates(self):
        paths = [f"/crm-leads/{s}/export?format={fmt}" for s in self.keys for fmt in ("xlsx", "csv")]
        paths += ["/vendor-to-koc/leads/export", "/dhruva/leads/export", "/vendor-to-koc/leads/template",
                  "/dhruva/leads/template"]
        paths += [f"/crm-leads/{s}/template?format={fmt}" for s in self.keys for fmt in ("xlsx", "csv")]
        for path in paths:
            admin = self.req("admin", "GET", path)
            viewer = self.req("viewer", "GET", path)
            self.assertEqual((admin.status_code, viewer.status_code), (200, 200), f"{path}: {viewer.text[:200]}")
            self.assertEqual(viewer.headers["content-type"], admin.headers["content-type"], path)
            self.assertEqual(self.body(viewer), self.body(admin), path)

    def test_admin_stats_allowed(self):
        with mock.patch.object(vendors_ep, "_build_stats", lambda: {"vendors": []}):
            self.assertEqual(self.req("viewer", "GET", "/vendors/stats").status_code, 200)
            self.assertEqual(self.req("phodeo", "GET", "/vendors/stats").status_code, 403)   # unchanged

    def test_team_settings_and_db_explorer_are_denied(self):
        for path in ("/users", "/sheets/sources", "/sheets/wedding/worksheets", "/sheets/wedding/data",
                     "/read/health", "/read/schemas", "/read/tables"):
            resp = self.req("viewer", "GET", path)
            self.assertEqual(resp.status_code, 403, f"{path}: {resp.text}")
        own = self.req("viewer", "GET", f"/users/{self.users['viewer'].id}")
        self.assertEqual((own.status_code, own.json()["role"]), (200, "viewer"))
        self.assertEqual(self.req("viewer", "GET", f"/users/{self.admin.id}").status_code, 404)  # like vendors
        me = self.req("viewer", "GET", "/auth/me").json()
        self.assertEqual((me["role"], me["vendor"]), ("viewer", None))

    def test_no_notifications_and_no_cleanup(self):
        with Session(self.engine) as s:
            notification_service.notify_admins_vendor_added(
                s, vendor_id=self.vendor_ids["phodeo"], actor_user_id=self.users["phodeo"].id, actor_name="Phodeo",
                added=1, lead_id=self.vtk_id)
            self.assertTrue(s.exec(select(CrmNotification)).all())
        with mock.patch.object(notification_service, "cleanup_expired") as cleanup:
            listed = self.req("viewer", "GET", "/notifications").json()
            count = self.req("viewer", "GET", "/notifications/unread-count").json()
        self.assertEqual(listed["items"], [])
        self.assertEqual(listed["total"], 0)
        self.assertEqual(count, {"unread_count": 0})
        cleanup.assert_not_called()
        self.assertGreater(self.req("admin", "GET", "/notifications/unread-count").json()["unread_count"], 0)


# ── 2. Writes: 403 everywhere, nothing changes ─────────────────────────
class ViewerWritesTests(ViewerTestBase):
    def write_calls(self) -> list[tuple]:
        w, d, v = self.keys["wedding"], self.dhruva_id, self.vtk_id
        upload = {"files": {"file": ("leads.xlsx", sample_workbook())}}
        return [
            ("POST", "/users", {"json": {"name": "X", "email": "x@example.com", "password": "password123",
                                          "role": "viewer"}}),
            ("PATCH", f"/users/{self.admin.id}", {"json": {"is_active": False}}),
            ("PATCH", f"/users/{self.users['viewer'].id}", {"json": {"is_active": True}}),
            ("POST", "/branches", {"json": {"name": "B", "code": "B1", "city": "X"}}),
            ("PATCH", "/branches/1", {"json": {"name": "B"}}),
            ("POST", "/branches/import", {}),
            ("POST", "/vendors", {"json": {"name": "V"}}),
            ("PATCH", f"/vendors/{self.vendor_ids['phodeo']}", {"json": {"is_active": False}}),
            ("POST", "/vendor-to-koc/vendors", {"json": {"name": "V"}}),
            ("PATCH", f"/vendor-to-koc/vendors/{self.vendor_ids['phodeo']}", {"json": {"is_active": False}}),
            ("PUT", f"/vendor-to-koc/vendors/{self.vendor_ids['phodeo']}/users/{self.users['viewer'].id}", {}),
            ("DELETE", f"/vendor-to-koc/vendors/{self.vendor_ids['phodeo']}/users/{self.users['phodeo'].id}", {}),
            ("PATCH", f"/vendor-to-koc/leads/{v}", {"json": {"update_status": "converted"}}),
            ("POST", "/vendor-to-koc/upload/preview", upload),
            ("POST", "/vendor-to-koc/leads/bulk", {"json": {"leads": []}}),
            ("POST", "/vendor-to-koc/leads", {"json": {"phone": "9845099999", "name": "N"}}),
            ("POST", f"/vendor-to-koc/leads/{v}/hot", {}),
            ("DELETE", f"/vendor-to-koc/leads/{v}/hot", {}),
            ("POST", "/crm-leads/wedding/import/preview", upload),
            ("POST", "/crm-leads/wedding/import", {"json": {"rows": []}}),
            ("POST", "/crm-leads/wedding", {"json": {"values": {"phone": "9845099998"}}}),
            ("PATCH", f"/crm-leads/wedding/{w}", {"json": {"values": {"name": "Changed"}}}),
            ("POST", f"/crm-leads/wedding/{w}/archive", {}),
            ("POST", f"/crm-leads/wedding/{w}/unarchive", {}),
            ("PUT", f"/leads/wedding/{w}/crm", {"json": {"update_status": "converted"}}),
            ("POST", f"/leads/wedding/{w}/hot", {}),
            ("DELETE", f"/leads/wedding/{w}/hot", {}),
            ("POST", "/dhruva/upload/preview", upload),
            ("POST", "/dhruva/leads/bulk", {"json": {"leads": []}}),
            ("POST", "/dhruva/leads", {"json": {"name": "N", "phone": "9845099997"}}),
            ("PATCH", f"/dhruva/leads/{d}", {"json": {"name": "Changed"}}),
            ("POST", f"/dhruva/leads/{d}/hot", {}),
            ("DELETE", f"/dhruva/leads/{d}/hot", {}),
            ("POST", f"/dhruva/leads/{d}/archive", {}),
            ("POST", f"/dhruva/leads/{d}/unarchive", {}),
            ("POST", "/notifications/1/read", {}),
            ("POST", "/notifications/read-all", {}),
            ("POST", "/sheets/wedding/sync", {}),
        ]

    def snapshot(self) -> dict:
        with Session(self.engine) as s:
            return {model.__name__: [r.model_dump() for r in s.exec(select(model)).all()]
                    for model in (User, WeddingLead, LeadMeta, VendorToKocLead, DhruvaLead, CrmNotification)}

    def test_every_write_endpoint_answers_403_and_changes_nothing(self):
        before = self.snapshot()
        for method, path, kw in self.write_calls():
            resp = self.req("viewer", method, path, **kw)
            self.assertEqual(resp.status_code, 403, f"{method} {path}: {resp.text}")
            self.assertEqual(resp.json(), {"detail": VIEW_ONLY}, f"{method} {path}")
        self.assertEqual(self.snapshot(), before)

    def test_invalid_bodies_and_unknown_ids_still_answer_403(self):
        for method, path in (("PUT", "/leads/wedding/nope/crm"), ("PATCH", "/dhruva/leads/999999"),
                             ("POST", "/users"), ("POST", "/crm-leads/wedding")):
            resp = self.req("viewer", method, path, content=b"not json")
            self.assertEqual((resp.status_code, resp.json()), (403, {"detail": VIEW_ONLY}), f"{method} {path}")

    def test_every_write_route_is_covered_by_the_central_block(self):
        """A write endpoint added later is blocked without any change: every POST / PUT / PATCH /
        DELETE operation in the OpenAPI schema (except login, refresh, logout) answers 403 to a viewer."""
        exempt = {f"{API}{p}" for p in VIEWER_WRITE_EXCEPTIONS}
        operations = [(method.upper(), path) for path, item in app.openapi()["paths"].items()
                      for method in item if method in ("post", "put", "patch", "delete")
                      and path.startswith(API) and path not in exempt]
        self.assertGreaterEqual(len(operations), 37)                        # 37 today (plus the 3 auth exceptions)
        before = self.snapshot()
        for method, path in operations:
            url = re.sub(r"\{source\}", "wedding", path)
            url = re.sub(r"\{[^}]+\}", "1", url)[len(API):]
            resp = self.req("viewer", method, url)
            self.assertEqual((resp.status_code, resp.json()), (403, {"detail": VIEW_ONLY}), f"{method} {path}")
        self.assertEqual(self.snapshot(), before)
        for path in exempt:                                                 # the three exceptions exist
            self.assertIn(path, app.openapi()["paths"])

    def test_no_token_or_a_bad_token_is_left_to_the_endpoint(self):
        w = self.keys["wedding"]
        self.assertEqual(self.req(None, "PUT", f"/leads/wedding/{w}/crm", json={}).status_code, 401)
        bad = self.client.put(f"{API}/leads/wedding/{w}/crm", json={}, headers={"Authorization": "Bearer nope"})
        self.assertEqual(bad.status_code, 401)

    def test_admin_vendor_and_dhruva_writes_unchanged(self):
        w = self.keys["wedding"]
        self.assertEqual(self.req("admin", "PUT", f"/leads/wedding/{w}/crm",
                                  json={"update_status": "converted"}).status_code, 200)
        self.assertEqual(self.req("phodeo", "POST", f"/vendor-to-koc/leads/{self.vtk_id}/hot").status_code, 200)
        self.assertEqual(self.req("dhruva", "PATCH", f"/dhruva/leads/{self.dhruva_id}",
                                  json={"venue": "New hall"}).status_code, 200)
        self.assertEqual(self.req("admin", "POST", f"/dhruva/leads/{self.dhruva_id}/archive").status_code, 200)
        self.assertEqual(self.req("phodeo", "POST", "/notifications/read-all").status_code, 200)
        # Their own existing 403s keep their messages
        self.assertEqual(self.req("phodeo", "POST", "/vendors", json={"name": "V"}).json()["detail"],
                         "Admin access required")


# ── 3. Users: created by admins, validated, blocked, admin-only writes ─
class ViewerUsersTests(ViewerTestBase):
    def test_admin_creates_a_viewer(self):
        resp = self.req("admin", "POST", "/users", json={"name": "View", "email": "view2@example.com",
                                                         "password": "password123", "role": "viewer"})
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual((resp.json()["role"], resp.json()["vendor_id"], resp.json()["is_active"]),
                         ("viewer", None, True))
        listed = {u["email"]: u["role"] for u in self.req("admin", "GET", "/users").json()}
        self.assertEqual(listed["view2@example.com"], "viewer")

    def test_vendor_id_on_a_viewer_is_422(self):
        resp = self.req("admin", "POST", "/users", json={"name": "View", "email": "view3@example.com",
                                                         "password": "password123", "role": "viewer",
                                                         "vendor_id": self.vendor_ids["phodeo"]})
        self.assertEqual(resp.status_code, 422, resp.text)
        unknown = self.req("admin", "POST", "/users", json={"name": "X", "email": "x3@example.com",
                                                            "password": "password123", "role": "readonly"})
        self.assertEqual(unknown.status_code, 422)

    def test_block_and_unblock_a_viewer(self):
        viewer_id = self.users["viewer"].id
        self.assertEqual(self.req("viewer", "GET", "/wedding-leads").status_code, 200)
        blocked = self.req("admin", "PATCH", f"/users/{viewer_id}", json={"is_active": False})
        self.assertEqual((blocked.status_code, blocked.json()["is_active"]), (200, False))
        self.assertEqual(self.req("viewer", "GET", "/wedding-leads").status_code, 401)
        unblocked = self.req("admin", "PATCH", f"/users/{viewer_id}", json={"is_active": True})
        self.assertEqual((unblocked.status_code, unblocked.json()["is_active"]), (200, True))
        self.assertEqual(self.req("viewer", "GET", "/wedding-leads").status_code, 200)

    def test_only_admins_write_users(self):
        body = {"name": "X", "email": "x4@example.com", "password": "password123", "role": "viewer"}
        target = self.users["rj"].id
        for who in ("phodeo", "dhruva", "viewer"):
            created = self.req(who, "POST", "/users", json=body)
            self.assertEqual(created.status_code, 403, who)
            for user_id in (target, self.users[who].id):
                patched = self.req(who, "PATCH", f"/users/{user_id}", json={"is_active": False})
                self.assertEqual(patched.status_code, 403, who)
        self.assertEqual(self.req("viewer", "POST", "/users", json=body).json()["detail"], VIEW_ONLY)
        # Roles: admins may change another login's role; everyone else gets 403
        self.assertEqual(self.req("admin", "PATCH", f"/users/{target}", json={"role": "boss"}).status_code, 422)
        self.assertEqual(self.req("viewer", "PATCH", f"/users/{self.users['viewer'].id}",
                                  json={"role": "admin"}).status_code, 403)
        with Session(self.engine) as s:
            self.assertEqual(s.get(User, self.users["viewer"].id).role, "viewer")
            self.assertIsNone(s.exec(select(User).where(User.email == "x4@example.com")).first())


class ViewerHelperTests(unittest.TestCase):
    def test_sees_all_and_is_admin(self):
        def viewer_of(role):
            return access.Viewer(User(id=1, name="x", email="x@example.com", role=role), None)
        self.assertEqual((viewer_of("viewer").sees_all, viewer_of("viewer").is_admin), (True, False))
        self.assertEqual((viewer_of("admin").sees_all, viewer_of("admin").is_admin), (True, True))
        self.assertEqual((viewer_of("dhruva").sees_all, viewer_of("vendor").sees_all), (False, False))

    def test_viewer_for_needs_no_vendor_link(self):
        user = User(id=7, name="v", email="v@example.com", role="viewer")
        self.assertEqual(access.viewer_for(mock.Mock(), user), access.Viewer(user, None))

    def test_read_admin_dependency(self):
        for role in ("admin", "viewer"):
            self.assertEqual(deps.require_read_admin(User(id=1, name="x", email="x@example.com", role=role)).role, role)
        for role in ("vendor", "dhruva"):
            with self.assertRaises(Exception):
                deps.require_read_admin(User(id=1, name="x", email="x@example.com", role=role))


# ── 4. Auth exceptions: login, refresh, logout ─────────────────────────
class ViewerAuthTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        with Session(self.engine) as s:
            s.add(User(name="Viewer", email="viewer@example.com", hashed_password=hash_password(PASSWORD),
                       role="viewer"))
            s.commit()

    def test_login_refresh_logout_work_for_a_viewer(self):
        login = self.client.post(LOGIN, data={"username": "viewer@example.com", "password": PASSWORD})
        self.assertEqual(login.status_code, 200, login.text)
        bearer = {"Authorization": f"Bearer {login.json()['access_token']}"}
        self.assertEqual(self.client.get(ME, headers=bearer).json()["role"], "viewer")
        refreshed = self.client.post(REFRESH, headers={"Origin": ORIGIN, **bearer})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertIn("access_token", refreshed.json())
        logout = self.client.post(LOGOUT, headers={"Origin": ORIGIN, **bearer})
        self.assertEqual(logout.status_code, 204, logout.text)
        self.assertIsNone(self.client.cookies.get(COOKIE))
        self.assertEqual(self.client.post(REFRESH, headers={"Origin": ORIGIN}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
