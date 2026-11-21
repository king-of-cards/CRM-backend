"""Team (edit and delete logins), vendor management with lead counts, and creator / updater names
on leads ("Deleted user" when the login is gone). In-memory SQLite only. Run:

    python -m unittest discover -s tests -t . -v
"""
import unittest
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from tests.test_crm_leads_api import API, NOW, ApiTestBase

from app.core import response_cache
from app.core.security import verify_password
from app.models.dhruva import DhruvaLead
from app.models.lead_activity import LeadActivity
from app.models.lead_meta import LeadMeta
from app.models.notification import CrmNotification, CrmNotificationRead
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_to_koc import VendorToKocLead
from app.models.vendor_user import VendorUser

PASSWORD = "correct horse battery staple"


class Base(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with Session(self.engine, expire_on_commit=False) as s:
            self.users["viewer"] = User(name="Viewer", email="viewer@example.com", hashed_password="x", role="viewer")
            s.add(self.users["viewer"])
            s.commit()

    def call(self, method, path, who="admin", **kw):
        with self.logged_in(who):
            return self.client.request(method, f"{API}{path}", **kw)

    def new_vendor_login(self, email="new@example.com", vendor="phodeo") -> dict:
        resp = self.call("POST", "/users", json={"name": "New", "email": email, "password": PASSWORD, "role": "vendor",
                                                 "vendor_id": self.vendor_ids[vendor]})
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()


# ── 1. Team ────────────────────────────────────────────────────────────
class TeamTests(Base):
    def test_patch_every_field(self):
        user = self.new_vendor_login()
        url = f"/users/{user['id']}"
        resp = self.call("PATCH", url, json={"name": "Renamed", "email": "Renamed@Example.com",
                                            "vendor_id": self.vendor_ids["rj"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((resp.json()["name"], resp.json()["email"], resp.json()["vendor_name"]),
                         ("Renamed", "renamed@example.com", "RJ"))
        self.assertEqual(self.call("PATCH", url, json={"email": "PHODEO@example.com"}).status_code, 409)  # taken
        self.assertEqual(self.call("PATCH", url, json={"vendor_id": 999}).status_code, 404)
        with Session(self.engine) as s:
            ohp = s.get(Vendor, self.vendor_ids["ohp"])
            ohp.is_active = False
            s.add(ohp)
            s.commit()
        self.assertEqual(self.call("PATCH", url, json={"vendor_id": self.vendor_ids["ohp"]}).status_code, 422)
        self.assertEqual(self.call("PATCH", url, json={"password": "short"}).status_code, 422)
        with Session(self.engine) as s:          # a session to revoke
            s.add(RefreshToken(user_id=user["id"], token_hash="h" * 64, family_id="f",
                               expires_at=NOW + timedelta(days=7), created_at=NOW, family_created_at=NOW))
            s.commit()
        self.assertEqual(self.call("PATCH", url, json={"password": "a much better one"}).status_code, 200)
        with Session(self.engine) as s:
            row = s.get(User, user["id"])
            self.assertTrue(verify_password("a much better one", row.hashed_password))
            self.assertIsNotNone(s.exec(select(RefreshToken).where(RefreshToken.user_id == user["id"])).one().revoked_at)
        # Role: vendor -> view only drops the vendor link; back to vendor needs a vendor_id
        self.assertEqual(self.call("PATCH", url, json={"role": "viewer", "vendor_id": self.vendor_ids["rj"]}).status_code, 422)
        resp = self.call("PATCH", url, json={"role": "viewer"})
        self.assertEqual((resp.status_code, resp.json()["role"], resp.json()["vendor_id"]), (200, "viewer", None))
        self.assertEqual(self.call("PATCH", url, json={"role": "vendor"}).status_code, 422)
        resp = self.call("PATCH", url, json={"role": "vendor", "vendor_id": self.vendor_ids["phodeo"], "is_active": False})
        self.assertEqual((resp.json()["role"], resp.json()["vendor_name"], resp.json()["is_active"]),
                         ("vendor", "Phodeo", False))
        for body in ({}, {"name": None}, {"nickname": "x"}):
            self.assertEqual(self.call("PATCH", url, json=body).status_code, 422, body)

    def test_own_login_and_permissions(self):
        me = f"/users/{self.admin.id}"
        resp = self.call("PATCH", me, json={"role": "viewer"})
        self.assertEqual((resp.status_code, resp.json()["detail"]), (400, "You can't change your own role"))
        self.assertEqual(self.call("PATCH", me, json={"is_active": False}).status_code, 400)
        self.assertEqual(self.call("PATCH", me, json={"name": "Boss"}).status_code, 200)       # the rest is fine
        self.assertEqual(self.call("DELETE", me).json()["detail"], "You can't delete your own login")
        for who in ("phodeo", "viewer"):
            self.assertEqual(self.call("PATCH", f"/users/{self.users['rj'].id}", who, json={"name": "x"}).status_code, 403)
            self.assertEqual(self.call("DELETE", f"/users/{self.users['rj'].id}", who).status_code, 403)
        self.assertEqual(self.call("POST", "/users", json={"name": "N", "email": "n@example.com", "password": PASSWORD,
                                                           "role": "vendor"}).status_code, 422)   # needs a vendor

    def test_last_active_admin_is_kept(self):
        with Session(self.engine, expire_on_commit=False) as s:
            other = User(name="Old admin", email="old@example.com", hashed_password="x", role="admin", is_active=False)
            s.add(other)
            s.commit()
        self.current = other               # an inactive admin can't log in; acting as one only to reach the rule
        resp = self.client.delete(f"{API}/users/{self.admin.id}")
        self.assertEqual((resp.status_code, resp.json()["detail"]), (409, "You can't delete the last active admin"))
        self.current = self.admin
        self.assertEqual(self.call("DELETE", f"/users/{other.id}").status_code, 204)          # inactive admin: fine

    def test_delete_keeps_history_as_deleted_user(self):
        user = self.new_vendor_login()
        uid = user["id"]
        key = self.add_ok("wedding")["lead_key"]
        when = datetime(2026, 11, 20, tzinfo=timezone.utc)
        with Session(self.engine) as s:
            s.add(RefreshToken(user_id=uid, token_hash="t" * 64, family_id="f", expires_at=when + timedelta(days=7), family_created_at=when,
                               created_at=when))
            inbox = CrmNotification(recipient_user_id=uid, type="x", title="t", body="b", actor_user_id=self.admin.id,
                                    actor_name="Admin", created_at=when)
            broadcast = CrmNotification(recipient_vendor_id=self.vendor_ids["phodeo"], type="x", title="t", body="b",
                                        actor_user_id=uid, actor_name="New", created_at=when)
            s.add_all([inbox, broadcast])
            s.commit()
            s.add(CrmNotificationRead(notification_id=broadcast.id, user_id=uid, read_at=when))
            s.add(LeadActivity(source="wedding", lead_key=key, action="field_changed", actor_user_id=uid,
                               actor_name="New", actor_role="vendor", created_at=when))
            meta = s.exec(select(LeadMeta).where(LeadMeta.lead_key == key)).first() or LeadMeta(
                source="wedding", lead_key=key, updated_at=when)
            meta.updated_by, meta.update_status = uid, "interested"
            s.add(meta)
            s.add(VendorToKocLead(vendor_id=self.vendor_ids["phodeo"], phone="9845090001", source="manual",
                                  created_by=uid, status_updated_by=uid, update_status="interested",
                                  created_at=when, updated_at=when))
            s.add(DhruvaLead(name="D", source="manual", created_by=uid, status_updated_by=uid, created_at=when,
                             updated_at=when))
            s.commit()
        crm = next(i for i in self.list_items("wedding") if i["crm"]["lead_key"] == key)["crm"]
        self.assertEqual(crm["updated_by_name"], "New")
        self.assertEqual(self.call("DELETE", f"/users/{uid}").status_code, 204)
        with Session(self.engine) as s:
            self.assertIsNone(s.get(User, uid))
            for model, column in ((RefreshToken, RefreshToken.user_id), (VendorUser, VendorUser.user_id),
                                  (CrmNotificationRead, CrmNotificationRead.user_id),
                                  (CrmNotification, CrmNotification.recipient_user_id)):
                self.assertEqual(s.exec(select(model).where(column == uid)).all(), [], model.__name__)
            self.assertEqual(len(s.exec(select(CrmNotification)).all()), 1)          # the broadcast stays
            self.assertEqual(len(s.exec(select(LeadActivity).where(LeadActivity.actor_user_id == uid)).all()), 1)
            vtk_id = s.exec(select(VendorToKocLead.id)).one()
            dhruva_id = s.exec(select(DhruvaLead.id)).one()
        response_cache.clear()
        crm = next(i for i in self.list_items("wedding") if i["crm"]["lead_key"] == key)["crm"]
        self.assertEqual(crm["updated_by_name"], "Deleted user")
        activity = self.call("GET", f"/leads/wedding/{key}/activity").json()["items"]
        self.assertIn("New", [a["actor_name"] for a in activity])                     # its stored name
        for who in ("admin", "viewer"):
            lead = self.call("GET", f"/vendor-to-koc/leads/{vtk_id}", who).json()
            self.assertEqual((lead["created_by_name"], lead["status_updated_by_name"]), ("Deleted user", "Deleted user"))
        dhruva = self.call("GET", f"/dhruva/leads/{dhruva_id}").json()
        self.assertEqual((dhruva["created_by_name"], dhruva["status_updated_by_name"]), ("Deleted user", "Deleted user"))
        self.assertEqual(self.call("DELETE", f"/users/{uid}").status_code, 404)


# ── 2. Vendors ─────────────────────────────────────────────────────────
class VendorTests(Base):
    def vendors(self, **params) -> dict:
        return {v["name"]: v for v in self.call("GET", "/vendors", params=params).json()}

    def test_create_rename_activate_with_lead_counts(self):
        self.assertEqual(self.call("POST", "/vendors", json={"name": " phodeo "}).status_code, 409)   # any case
        created = self.call("POST", "/vendors", json={"name": "Kraftyz"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual((created.json()["lead_count"], created.json()["vtk_lead_count"], created.json()["is_active"]),
                         (0, 0, True))
        kid = created.json()["id"]
        self.assertEqual(self.call("PATCH", f"/vendors/{kid}", json={"name": "OHP "}).status_code, 409)
        self.assertEqual(self.call("PATCH", f"/vendors/{kid}", json={"name": "kraftyz"}).status_code, 200)  # itself
        key = self.add_ok("wedding")["lead_key"]                                       # Phodeo (Bangalore)
        with Session(self.engine) as s:
            s.add(VendorToKocLead(vendor_id=self.vendor_ids["phodeo"], phone="9845090002", source="manual",
                                  created_by=self.admin.id, created_at=NOW, updated_at=NOW))
            s.commit()
        response_cache.clear()
        phodeo = self.vendors()["Phodeo"]
        self.assertEqual((phodeo["lead_count"], phodeo["vtk_lead_count"]), (1, 1))
        # Deactivating keeps a manual assignment (and its name on the lead)
        self.assertEqual(self.put_crm("admin", "wedding", key, {"vendor_id": kid}).status_code, 200)
        resp = self.call("PATCH", f"/vendors/{kid}", json={"is_active": False})
        self.assertEqual((resp.status_code, resp.json()["is_active"], resp.json()["lead_count"]), (200, False, 1))
        self.assertNotIn("kraftyz", self.vendors())
        self.assertEqual(self.vendors(include_inactive="true")["kraftyz"]["lead_count"], 1)
        item = next(i for i in self.list_items("wedding") if i["crm"]["lead_key"] == key)
        self.assertEqual((item["crm"]["vendor"]["name"], item["crm"]["vendor_source"]), ("kraftyz", "manual"))
        self.assertEqual(self.call("POST", "/vendors", "viewer", json={"name": "X"}).status_code, 403)


# ── 3. Names on leads ──────────────────────────────────────────────────
class NamesTests(Base):
    def test_view_only_logins_see_creator_and_updater_names(self):
        with Session(self.engine) as s:
            s.add(VendorToKocLead(vendor_id=self.vendor_ids["phodeo"], phone="9845090003", source="manual",
                                  created_by=self.users["phodeo"].id, status_updated_by=self.admin.id,
                                  update_status="interested", created_at=NOW, updated_at=NOW))
            s.add(VendorToKocLead(vendor_id=self.vendor_ids["phodeo"], phone="9845090004", source="manual",
                                  created_by=self.users["phodeo"].id, created_at=NOW, updated_at=NOW))
            s.commit()
        for who in ("viewer", "phodeo"):
            items = {i["phone"]: i for i in self.call("GET", "/vendor-to-koc/leads", who).json()["items"]}
            self.assertEqual((items["9845090003"]["created_by_name"], items["9845090003"]["status_updated_by_name"]),
                             ("phodeo", "Admin"), who)
            self.assertEqual(items["9845090004"]["status_updated_by_name"], None)       # nobody updated it
        key = self.add_ok("wedding")["lead_key"]
        self.put_crm("admin", "wedding", key, {"update_status": "interested"})
        with self.logged_in("viewer"):
            item = next(i for i in self.list_items("wedding") if i["crm"]["lead_key"] == key)
        self.assertEqual(item["crm"]["updated_by_name"], "Admin")
        self.assertEqual(self.call("GET", f"/leads/wedding/{key}/crm", "viewer").json()["updated_by_name"], "Admin")


if __name__ == "__main__":
    unittest.main()
