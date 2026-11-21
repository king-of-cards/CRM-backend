"""Vendor source visibility (/auth/me sources, overview per source), the manual "Unassigned" choice
on PUT /leads/{source}/{lead_key}/crm, and every vendor + Dhruva on /analytics/vendor-to-koc.
In-memory SQLite and the fake workflow database only. Run:

    python -m unittest discover -s tests -t . -v
"""
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from sqlmodel import Session, select

from tests.test_crm_leads_api import API, NOW, ApiTestBase

from app.models.dhruva import DhruvaLead
from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_to_koc import VendorToKocLead
from app.models.wedding_lead import WeddingLead
from app.services import periods

GIFT = "Wedding Gifts & Return Gifts"
FOUR = ["spin", "whatsapp", "wedding", "wedding_planner"]


class Base(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with Session(self.engine, expire_on_commit=False) as s:
            for name, role in (("viewer", "viewer"), ("dhruva", "dhruva")):
                self.users[name] = User(name=name, email=f"{name}@example.com", hashed_password="x", role=role)
                s.add(self.users[name])
            s.commit()

    def get(self, path, who="admin", status=200, **params):
        with self.logged_in(who):
            resp = self.client.get(f"{API}{path}", params=params)
        self.assertEqual(resp.status_code, status, f"{path}: {resp.text}")
        return resp.json()

    def sources(self, who) -> list[str]:
        return self.get("/auth/me", who)["sources"]


# ── 1. Sources per vendor ──────────────────────────────────────────────
class SourcesTests(Base):
    def setUp(self):
        super().setUp()
        # Phodeo: spin (pincode 560011) and wedding (Bangalore); OHP: whatsapp (UAE) and planner (Udaipur);
        # RJ: the workflow spin row 101 (Chamrajpet, 560018)
        self.keys = {source: self.add_ok(source)["lead_key"] for source in FOUR}

    def test_auth_me_sources_per_role(self):
        self.assertEqual(self.sources("admin"), FOUR)
        self.assertEqual(self.sources("viewer"), FOUR)
        self.assertEqual(self.sources("phodeo"), ["spin", "wedding"])
        self.assertEqual(self.sources("ohp"), ["whatsapp", "wedding_planner"])
        self.assertEqual(self.sources("rj"), ["spin"])
        self.assertEqual(self.sources("unlinked"), [])
        self.assertEqual(self.sources("dhruva"), [])

    def test_sources_follow_reassignment(self):
        resp = self.put_crm("admin", "wedding", self.keys["wedding"], {"vendor_id": self.vendor_ids["rj"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.sources("rj"), ["spin", "wedding"])
        self.assertEqual(self.sources("phodeo"), ["spin"])
        self.put_crm("admin", "spin", self.keys["spin"], {"assigned_to": "unassigned"})
        self.assertEqual(self.sources("phodeo"), [])

    def test_overview_only_has_the_vendors_sources(self):
        o = self.get("/analytics/overview", "phodeo", period="all", sources="wedding,wedding_planner")
        self.assertEqual(o["sources"], ["spin", "wedding"])
        for name in ("totals", "new_in_period", "this_week", "last_week", "outcomes"):
            self.assertEqual(set(o[name]), {"spin", "wedding", "all"}, name)
        self.assertEqual([m["source"] for m in o["source_mix"]], ["spin", "wedding"])
        self.assertTrue(all(not {"whatsapp", "wedding_planner"} & set(p) for p in o["trend"]))
        self.assertTrue(all(not {"whatsapp", "wedding_planner"} & set(p) for p in o["upcoming_by_month"]))
        self.assertEqual(o["top_cities_sources"], ["wedding"])          # planner isn't Phodeo's: left out
        self.assertEqual(o["totals"]["wedding"], 1)
        admin = self.get("/analytics/overview", period="all")
        self.assertEqual(admin["sources"], FOUR)
        self.assertEqual(set(admin["totals"]), {*FOUR, "all"})


# ── 2. Manual "Unassigned" ─────────────────────────────────────────────
class UnassignTests(Base):
    def setUp(self):
        super().setUp()
        with Session(self.engine) as s:
            s.add(Vendor(name="Kraftyz", priority=50, created_at=NOW, updated_at=NOW))
            s.commit()

    def crm(self, source, key, who="admin"):
        return self.get(f"/leads/{source}/{key}/crm", who)

    def assigned(self, source, key):
        """As the list shows it (the single GET has no location, so no area rule)."""
        crm = next(i for i in self.list_items(source) if i["crm"]["lead_key"] == key)["crm"]
        return (crm["vendor"] or {}).get("name"), crm["vendor_source"], crm["auto_assign"]

    def test_unassigned_beats_every_automatic_rule_and_automatic_restores(self):
        spin = self.add_ok("spin")["lead_key"]                                   # branch rule: Phodeo
        gift = self.import_spin({"phone": "9845011111", "otherWeddingServices": GIFT,
                                 "assigned_to": "Automatic"})["lead_key"]         # gift default: Kraftyz
        wedding = self.add_ok("wedding")["lead_key"]
        with Session(self.engine) as s:                                           # sheet rule: OHP
            lead = s.exec(select(WeddingLead).where(WeddingLead.sheet_key == wedding)).one()
            lead.shared_with = "OHP"
            s.add(lead)
            s.commit()
        cases = [("spin", spin, ("Phodeo", "branch")), ("spin", gift, ("Kraftyz", "source_default")),
                 ("spin", "101", ("RJ", "branch")), ("wedding", wedding, ("OHP", "sheet"))]
        for source, key, (vendor, how) in cases:
            self.assertEqual(self.assigned(source, key)[:2], (vendor, how), key)
            resp = self.put_crm("admin", source, key, {"assigned_to": "unassigned"})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual((resp.json()["vendor"], resp.json()["auto_assign"]), (None, False))
            self.assertEqual(self.assigned(source, key), (None, None, False), key)
            self.assertEqual(self.crm(source, key)["vendor"], None)              # the single GET agrees
            self.put_crm("admin", source, key, {"assigned_to": "automatic"})
            self.assertEqual(self.assigned(source, key), (vendor, how, True), key)
        # The vendor loses an unassigned lead
        self.put_crm("admin", "spin", spin, {"assigned_to": "unassigned"})
        self.get(f"/leads/spin/{spin}/crm", "phodeo", status=404)

    def test_manual_vendor_then_unassigned_errors_and_activity(self):
        spin = self.add_ok("spin")["lead_key"]
        self.put_crm("admin", "spin", spin, {"vendor_id": self.vendor_ids["rj"]})
        self.put_crm("admin", "spin", spin, {"assigned_to": "unassigned"})
        self.assertEqual(self.assigned("spin", spin), (None, None, False))
        self.assertEqual(self.put_crm("admin", "spin", spin, {"assigned_to": "unassigned",
                                                              "vendor_id": self.vendor_ids["rj"]}).status_code, 422)
        self.assertEqual(self.put_crm("admin", "spin", spin, {"assigned_to": "nobody"}).status_code, 422)
        self.put_crm("admin", "spin", spin, {"assigned_to": "automatic"})                 # back to Phodeo
        self.assertEqual(self.put_crm("phodeo", "spin", spin, {"assigned_to": "unassigned"}).status_code, 403)
        self.assertEqual(self.put_crm("phodeo", "spin", spin, {"update_status": "interested"}).status_code, 200)
        changes = [(a["field"], a["old_value"], a["new_value"]) for a in
                   self.get(f"/leads/spin/{spin}/activity")["items"] if a["action"] == "assigned"]
        self.assertIn(("assigned_to", "RJ", "Unassigned"), changes)
        self.assertIn(("assigned_to", "Unassigned", "Automatic"), changes)


# ── 3. Every vendor and Dhruva on /analytics/vendor-to-koc ─────────────
class VendorToKocTests(Base):
    def setUp(self):
        super().setUp()
        self.today = mock.patch.object(periods, "today_ist", lambda: date(2026, 11, 21))
        self.today.start()
        when = datetime(2026, 11, 10, 5, 0, tzinfo=timezone.utc)
        old = datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)
        admin = self.users["admin"].id
        with Session(self.engine) as s:
            idle = Vendor(name="Idle", created_at=NOW, updated_at=NOW, is_active=False)        # no leads: hidden
            gone = Vendor(name="Gone", created_at=NOW, updated_at=NOW, is_active=False)        # inactive, has leads
            s.add_all([idle, gone])
            s.commit()
            for vendor_id, phone, status in ((self.vendor_ids["phodeo"], "9845020001", "converted"),
                                             (gone.id, "9845020002", None)):
                s.add(VendorToKocLead(vendor_id=vendor_id, phone=phone, source="manual", update_status=status,
                                      created_by=admin, created_at=when, updated_at=when))
            for status, created, deleted in (("interested", when, False), ("converted", when, False),
                                             (None, when, False), (None, when, True), ("converted", old, False)):
                s.add(DhruvaLead(name="D", source="manual", update_status=status, created_by=admin,
                                 created_at=created, updated_at=created, is_deleted=deleted))
            s.commit()

    def tearDown(self):
        self.today.stop()
        super().tearDown()

    def test_every_active_vendor_and_the_dhruva_summary(self):
        o = self.get("/analytics/vendor-to-koc", date_from="2026-11-01", date_to="2026-11-21")
        rows = {v["vendor_name"]: v for v in o["per_vendor"]}
        self.assertEqual(set(rows), {"RJ", "Phodeo", "OHP", "Gone"})                     # not Idle
        self.assertEqual((rows["Phodeo"]["leads"], rows["Phodeo"]["converted"], rows["Phodeo"]["is_active"]),
                         (1, 1, True))
        self.assertEqual((rows["RJ"]["leads"], rows["RJ"]["converted_pct"], rows["RJ"]["average_days_to_update"],
                          rows["RJ"]["is_active"]), (0, None, None, True))
        self.assertEqual((rows["Gone"]["leads"], rows["Gone"]["pending"], rows["Gone"]["is_active"]), (1, 1, False))
        self.assertEqual([v["vendor_name"] for v in o["per_vendor"]][-2:], ["OHP", "RJ"])  # zeros last, by name
        self.assertEqual(o["dhruva"], {"received": 3, "interested": 1, "converted": 1, "pending": 1})
        self.assertEqual(self.get("/analytics/vendor-to-koc", "viewer", date_from="2026-11-01",
                                  date_to="2026-11-21")["dhruva"]["received"], 3)
        everything = self.get("/analytics/vendor-to-koc", period="all")
        self.assertEqual(everything["dhruva"]["received"], 4)                            # + September's
        vendor = self.get("/analytics/vendor-to-koc", "phodeo", date_from="2026-11-01", date_to="2026-11-21")
        self.assertNotIn("per_vendor", vendor)
        self.assertNotIn("dhruva", vendor)
        self.assertEqual(vendor["received"], 1)


if __name__ == "__main__":
    unittest.main()
