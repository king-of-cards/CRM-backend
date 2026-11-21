"""KOC -> Dhruva (koc_to_dhruva): spin leads KOC shares with Dhruva by rule.

Rule: estimatedBudgetPerCard's first number >= 70, or quantityRequired >= 1000, over every workflow
row and every CRM-added spin lead. The leads stay in spin / wedding_gifts too, with separate CRM
details. In-memory SQLite and the fake workflow database only. Run:

    python -m unittest discover -s tests -t . -v
"""
import csv
import io
import unittest
from unittest import mock

from tests.dhruva_fixtures import DhruvaTestBase
from tests.test_crm_leads_api import API, ApiTestBase
import tests.test_crm_leads_api as base

from sqlalchemy import BigInteger, Column, MetaData, Table  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.api.deps import get_current_user  # noqa: E402
from app.api.v1.endpoints import spin_leads as sl  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.main import app  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import lead_sources  # noqa: E402

K2D = "/koc-to-dhruva"
GIFT = "Wedding Gifts & Return Gifts"


def row(lead_id, phone, budget, quantity, services="Photography & Videography", occasion="Wedding",
        created="2026-11-10T05:00:00", pincode="560011"):
    return {"id": lead_id, "name": f"Lead {lead_id}", "phone": phone, "created_at": created, "branch_id": None,
            "additional_details.pincode": pincode, "additional_details.occasionDate": "2026-12-20",
            "additional_details.occasionType": occasion,
            "additional_details.estimatedBudgetPerCard": budget,
            "additional_details.quantityRequired": quantity,
            "additional_details.otherWeddingServices": services}


ROWS = [
    row(301, "9845070001", "70-100", 200),                                    # budget       spin
    row(302, "9845070002", "10-20", 1000, services=GIFT),                     # quantity     wedding_gifts
    row(303, "9845070003", "UNDER_50", 500),                                  # 50: no       spin
    row(304, "9845070004", "50_100", None),                                   # 50: no       spin
    row(305, "9845070005", "500_PLUS", None, occasion="Reception"),           # budget       spin
    row(306, "9845070006", None, 1500, occasion="Birthday", created="2026-10-01T05:00:00"),   # quantity
    row(307, "9845070007", "40-70", 999),                                     # no
]
KOC_IDS = {"301", "302", "305", "306"}


class KocTestBase(DhruvaTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.rows = mock.patch.object(base, "WORKFLOW_ROWS", ROWS)
        self.rows.start()
        with Session(self.engine, expire_on_commit=False) as s:
            self.users["viewer"] = User(name="Viewer", email="viewer@example.com", hashed_password="x", role="viewer")
            s.add(self.users["viewer"])
            s.commit()

    def tearDown(self):
        self.rows.stop()
        super().tearDown()

    def get(self, path, who="admin", status=200, **params):
        with self.logged_in(who):
            resp = self.client.get(f"{API}{path}", params=params)
        self.assertEqual(resp.status_code, status, f"{path}: {resp.text}")
        return resp.json() if status == 200 else resp

    def keys(self, who="admin", **params) -> set[str]:
        return {i["lead_key"] for i in self.get(f"{K2D}/leads", who, page_size=100, **params)["items"]}

    def put(self, key, body, who="admin"):
        with self.logged_in(who):
            return self.client.put(f"{API}{K2D}/leads/{key}/crm", json=body)

    def export_count(self, **params) -> int:
        with self.logged_in("admin"):
            resp = self.client.get(f"{API}{K2D}/leads/export", params={"format": "csv", **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        return len(list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))) - 1

    def item(self, path, key, who="admin"):
        items = self.get(path, who, page_size=100)["items"]
        return next(i for i in items if str(i.get("lead_key") or i["id"]) == key)


# ── The rule ───────────────────────────────────────────────────────────
class RuleTests(unittest.TestCase):
    def test_budget_formats(self):
        expected = {"70-100": 70, "70_100": 70, "100-200": 100, "500+": 500, "500_PLUS": 500, "400": 400,
                    "500_": 500, "500-2000": 500, "50_100": 50, "UNDER_50": 50, "40-70": 40, "20 to 25": 20,
                    "1,000": 1000, None: None, "": None, "n/a": None}
        for value, number in expected.items():
            self.assertEqual(sl.first_number(value), number, value)
        for value in ("70-100", "70_100", "100-200", "500+", "500_PLUS", "400"):
            self.assertEqual(sl.koc_reason({"estimatedBudgetPerCard": value}), "budget", value)
        for value in ("50_100", "UNDER_50", "40-70", "10-20", None, ""):
            self.assertIsNone(sl.koc_reason({"estimatedBudgetPerCard": value}), value)

    def test_quantity_rule(self):
        for budget in (None, "", "10-20", "UNDER_50"):
            self.assertEqual(sl.koc_reason({"estimatedBudgetPerCard": budget, "quantityRequired": 1000}), "quantity")
        self.assertEqual(sl.koc_reason({"quantityRequired": "1500"}), "quantity")
        self.assertEqual(sl.koc_reason({"quantityRequired": 2500.0}), "quantity")
        self.assertIsNone(sl.koc_reason({"quantityRequired": 999}))
        self.assertIsNone(sl.koc_reason({"quantityRequired": None}))
        self.assertEqual(sl.koc_reason({"estimatedBudgetPerCard": "100-200", "quantityRequired": 5000}), "budget")

    def test_sql_rule_covers_every_row(self):
        table = Table("spin_lead", MetaData(), Column("id", BigInteger, primary_key=True),
                      Column("additional_details", JSONB))
        sql = " AND ".join(str(c.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
                           for c in sl.base_filter(table, sl.KOC_TO_DHRUVA))
        self.assertIn("'estimatedBudgetPerCard'", sql)
        self.assertIn("'quantityRequired'", sql)
        self.assertIn(">= 70", sql)
        self.assertIn(">= 1000", sql)
        self.assertNotIn("otherWeddingServices", sql)          # not limited to the listed rows
        self.assertEqual(lead_sources.parse_sources("koc_to_dhruva, dhruva"), {"koc_to_dhruva", "dhruva"})


# ── In both places, separate CRM data ──────────────────────────────────
class InBothTests(KocTestBase):
    def test_rows_and_both_places(self):
        crm = base.ApiTestBase.import_spin(self, {"phone": "9845070099", "name": "CRM big",
                                                  "estimatedBudgetPerCard": "100-200", "assigned_to": "Automatic"})
        self.assertEqual(crm["status"], "imported", crm)
        self.assertEqual(self.keys(), KOC_IDS | {crm["lead_key"]})
        spin = {str(i["id"]) for i in self.get("/spin-leads", page_size=100)["items"]}
        gifts = {str(i["id"]) for i in self.get("/wedding-gifts-leads", page_size=100)["items"]}
        self.assertTrue({"301", "305", crm["lead_key"]} <= spin)           # still in spin
        self.assertIn("302", gifts)                                         # still in wedding_gifts
        reasons = {i["lead_key"]: (i["reason"], i["reason_label"]) for i in
                   self.get(f"{K2D}/leads", page_size=100)["items"]}
        self.assertEqual(reasons["301"], ("budget", "Budget ≥ ₹70"))
        self.assertEqual(reasons["302"], ("quantity", "Quantity ≥ 1000"))
        lead = self.get(f"{K2D}/leads/306")
        self.assertEqual((lead["quantity_required"], lead["occasion"], lead["estimated_budget_per_card"]),
                         (1500, "Birthday", None))
        self.get(f"{K2D}/leads/307", status=404)                            # not in the rule
        self.get(f"{K2D}/leads/303", status=404)

    def test_crm_data_is_separate(self):
        saved = self.put("301", {"update_status": "converted", "quoted_amount": 40000, "quoted_date": "2026-11-12",
                                 "conversion_amount": 38000, "notes": "Dhruva deal", "follow_up_date": "2026-11-20"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["update_status"], saved.json()["vendor"]), ("converted", None))
        spin_301 = self.item("/spin-leads", "301")
        self.assertIsNone(spin_301["crm"]["update_status"])                 # spin record untouched
        self.assertEqual(self.put_spin("301", {"update_status": "interested"}).status_code, 200)
        koc_301 = self.item(f"{K2D}/leads", "301")
        self.assertEqual((koc_301["crm"]["update_status"], koc_301["crm"]["notes"]), ("converted", "Dhruva deal"))
        with self.logged_in("admin"):
            self.assertTrue(self.client.post(f"{API}{K2D}/leads/301/hot").json()["is_hot"])
        self.assertFalse(self.item("/spin-leads", "301")["is_hot"])
        self.assertTrue(self.item(f"{K2D}/leads", "301")["is_hot"])
        activity = self.get(f"{K2D}/leads/301/activity")["items"]
        self.assertTrue({"field_changed", "hot_on"} <= {a["action"] for a in activity})
        self.assertNotIn("hot_on", {a["action"] for a in self.get("/leads/spin/301/activity")["items"]})
        self.assertEqual(self.get(f"{K2D}/leads/301/crm")["quoted_date"], "2026-11-12")

    def put_spin(self, key, body):
        return ApiTestBase.put_crm(self, "admin", "spin", key, body)


# ── Access per role ────────────────────────────────────────────────────
class AccessTests(KocTestBase):
    def test_dhruva_logins_and_admins_read_and_edit(self):
        for who in ("admin", "dhruva"):
            self.assertEqual(self.keys(who), KOC_IDS)
            self.assertEqual(self.get(f"{K2D}/leads/stats", who)["total"], 4)
            self.get("/analytics/koc-to-dhruva", who)
            self.get(f"{K2D}/leads/filter-options", who)
            self.assertEqual(self.put("305", {"update_status": "interested"}, who).status_code, 200, who)
            with self.logged_in(who):
                self.assertEqual(self.client.post(f"{API}{K2D}/leads/305/hot").status_code, 200)
        self.assertEqual(self.put("305", {"vendor_id": 1}).status_code, 422)             # never assigned
        self.assertEqual(self.put("999", {"update_status": "converted"}).status_code, 404)
        with self.logged_in("admin"):                                                     # no add / upload
            self.assertEqual(self.client.post(f"{API}{K2D}/leads", json={}).status_code, 405)

    def test_vendors_get_403(self):
        for path in (f"{K2D}/leads", f"{K2D}/leads/stats", f"{K2D}/leads/filter-options", f"{K2D}/leads/export",
                     f"{K2D}/leads/301", f"{K2D}/leads/301/activity", "/analytics/koc-to-dhruva"):
            self.get(path, "phodeo", status=403)
        self.assertEqual(self.put("301", {"update_status": "converted"}, "phodeo").status_code, 403)

    def test_view_only_reads_but_cannot_write(self):
        app.dependency_overrides.pop(get_current_user, None)                   # real tokens: the central block
        token = create_access_token(self.users["viewer"].id, "viewer")
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get(f"{API}{K2D}/leads", headers=headers).json()["total"], 4)
        self.assertEqual(self.client.get(f"{API}/analytics/koc-to-dhruva", headers=headers).status_code, 200)
        put = self.client.put(f"{API}{K2D}/leads/301/crm", json={"update_status": "converted"}, headers=headers)
        hot = self.client.post(f"{API}{K2D}/leads/301/hot", headers=headers)
        for resp in (put, hot):
            self.assertEqual((resp.status_code, resp.json()["detail"]), (403, "View-only logins cannot change data."))


# ── Filters = stats = export ───────────────────────────────────────────
class FilterTests(KocTestBase):
    def test_filters_stats_and_export_agree(self):
        self.put("301", {"update_status": "converted", "quoted_amount": 5000, "follow_up_date": "2026-11-15"})
        self.put("306", {"update_status": "interested", "follow_up_date": "2026-11-30"})
        with self.logged_in("dhruva"):
            self.client.post(f"{API}{K2D}/leads/302/hot")
        cases = [
            ({}, KOC_IDS),
            ({"reason": "budget"}, {"301", "305"}),
            ({"reason": "quantity"}, {"302", "306"}),
            ({"quantity_min": 1000}, {"302", "306"}),
            ({"quantity_min": 100, "quantity_max": 1000}, {"301", "302"}),
            ({"occasion": "birth"}, {"306"}),
            ({"budget": "70-100"}, {"301"}),
            ({"pincode": "5600"}, KOC_IDS),
            ({"update_status": "converted"}, {"301"}),
            ({"update_status": "pending"}, {"302", "305"}),
            ({"quote": "has"}, {"301"}),
            ({"hot": "yes"}, {"302"}),
            ({"follow_up": "due"}, set()),                     # 301 is converted: closed
            ({"follow_up": "upcoming"}, {"306"}),
            ({"date_from": "2026-11-01", "date_to": "2026-11-16"}, {"301", "302", "305"}),
            ({"event_from": "2026-12-01", "event_to": "2026-12-31"}, KOC_IDS),
            ({"search": "Lead 305"}, {"305"}),
        ]
        for params, expected in cases:
            self.assertEqual(self.keys(**params), expected, params)
            self.assertEqual(self.get(f"{K2D}/leads/stats", **params)["total"], len(expected), params)
            self.assertEqual(self.export_count(**params), len(expected), params)
        stats = self.get(f"{K2D}/leads/stats")
        self.assertEqual((stats["kpis"]["converted"], stats["by_reason"]), (1, {"budget": 2, "quantity": 2}))
        self.assertEqual(self.get(f"{K2D}/leads", page_size=10, page=1, sort="additional_details.quantityRequired",
                                  order="desc")["total"], 4)
        for bad in ({"vendor_id": 1}, {"unassigned": "true"}, {"quantity_min": 10, "quantity_max": 5},
                    {"reason": "other"}, {"page_size": 7}):
            resp = self.get(f"{K2D}/leads", status=422 if bad.keys() & {"reason", "page_size"} else 400, **bad)
            self.assertIn(resp.status_code, (400, 422))

    def test_export_columns(self):
        self.put("302", {"update_status": "will_call_back", "notes": "Call Sunday", "quoted_amount": 9000})
        with self.logged_in("admin"):
            resp = self.client.get(f"{API}{K2D}/leads/export", params={"format": "csv", "reason": "quantity"})
        lines = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
        self.assertEqual(lines[0], ["Sl. No", "Name", "Phone", "Email", "Occasion", "Event date", "Branch", "Pincode",
                                    "Estimated budget per card", "Quantity required", "Reason", "Received", "Update",
                                    "Additional comments", "Quoted amount", "Quoted date", "Conversion amount",
                                    "Follow-up date", "Hot"])
        by_name = {r[1]: r for r in lines[1:]}
        self.assertEqual(by_name["Lead 302"][9:11], ["1000", "Quantity ≥ 1000"])
        self.assertEqual(by_name["Lead 302"][12:15], ["Will call back", "Call Sunday", "9000"])


# ── Dashboard ──────────────────────────────────────────────────────────
class DashboardTests(KocTestBase):
    def test_numbers(self):
        self.put("301", {"update_status": "converted", "quoted_amount": 40000, "conversion_amount": 38000})
        self.put("302", {"update_status": "not_interested"})
        self.put("305", {"quoted_amount": 12000})
        with self.logged_in("dhruva"):
            self.client.post(f"{API}{K2D}/leads/306/hot")
        o = self.get("/analytics/koc-to-dhruva")                          # all time by default
        self.assertTrue(o["period"]["all_time"])
        self.assertEqual(o["received"], 4)
        k = o["kpis"]
        self.assertEqual((k["total"], k["converted"], k["not_interested"], k["quoted_count"], k["quoted_total"],
                          k["conversion_amount_count"], k["conversion_amount_total"], k["hot_count"], k["pending"]),
                         (4, 1, 1, 2, 52000, 1, 38000, 1, 2))
        self.assertEqual(k["converted_pct"], 25.0)
        self.assertEqual({r["reason"]: r["count"] for r in o["by_reason"]}, {"budget": 2, "quantity": 2})
        bands = {b["band"]: b["count"] for b in o["by_budget_band"]}
        self.assertEqual((bands["70-100"], bands["500+"], bands["under_70"], bands["not_given"]), (1, 1, 1, 1))
        self.assertEqual({x["value"]: x["count"] for x in o["by_occasion"]}, {"Wedding": 2, "Reception": 1, "Birthday": 1})
        self.assertEqual(sum(p["total"] for p in o["inflow"]), 4)
        self.assertEqual(sum(p["quantity"] for p in o["inflow"]), 2)
        self.assertEqual((o["by_status"]["converted"], o["by_status"]["pending"]), (1, 2))
        self.assertEqual(o["top_cities"][0]["count"], 4)                 # pincode 560011 -> Bengaluru
        self.assertEqual(o["latest"][0]["lead_key"], "305")
        # A period: only November's leads (306 arrived 1 Oct)
        nov = self.get("/analytics/koc-to-dhruva", date_from="2026-11-01", date_to="2026-11-16")
        self.assertEqual((nov["received"], nov["kpis"]["hot_count"]), (3, 0))


# ── Reminders and also_in ──────────────────────────────────────────────
class RemindersAndAlsoInTests(KocTestBase):
    def test_reminders_cover_both_dhruva_directions(self):
        self.put("301", {"follow_up_date": "2026-11-18"})
        lead = self.add(name="Dhruva lead", venue="Hall", date="Dec 4th", phone="9800000555", event_type="wedding")
        with self.logged_in("admin"):
            self.client.patch(f"{API}/dhruva/leads/{lead.json()['id']}", json={"follow_up_date": "2026-11-17"})
        both = self.get("/dhruva/reminders/follow-ups", "dhruva")
        self.assertEqual(sorted(i["source"] for i in both["items"]), ["dhruva", "koc_to_dhruva"])
        self.assertEqual(next(i for i in both["items"] if i["source"] == "koc_to_dhruva")["name"], "Lead 301")
        only = self.get("/dhruva/reminders/follow-ups", "dhruva", sources="koc_to_dhruva")
        self.assertEqual([i["source"] for i in only["items"]], ["koc_to_dhruva"])
        self.get("/dhruva/reminders/follow-ups", "dhruva", status=400, sources="spin")
        admin = self.get("/reminders/follow-ups", sources="koc_to_dhruva")
        self.assertEqual([i["lead_key"] for i in admin["items"]], ["301"])
        self.put_spin_follow_up()
        vendor = self.get("/reminders/follow-ups", "phodeo")
        self.assertEqual({i["source"] for i in vendor["items"]}, {"spin"})          # never koc_to_dhruva

    def put_spin_follow_up(self):
        resp = ApiTestBase.put_crm(self, "admin", "spin", "301", {"follow_up_date": "2026-11-18"})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_also_in(self):
        spin_admin = self.item("/spin-leads", "301")
        self.assertIn("koc_to_dhruva", {a["source"] for a in spin_admin["also_in"]})
        spin_vendor = self.item("/spin-leads", "301", "phodeo")
        self.assertNotIn("koc_to_dhruva", {a["source"] for a in spin_vendor["also_in"]})
        koc_admin = self.item(f"{K2D}/leads", "301")
        self.assertEqual({a["source"] for a in koc_admin["also_in"]}, {"spin"})
        self.assertEqual({a["source"] for a in self.item(f"{K2D}/leads", "302")["also_in"]}, {"wedding_gifts"})
        self.assertEqual(self.item(f"{K2D}/leads", "301", "dhruva")["also_in"], [])   # Dhruva: no other section
        self.assertEqual(self.keys(also_in="yes"), KOC_IDS)
        self.assertNotIn("301", {str(i["id"]) for i in
                                 self.get("/spin-leads", "phodeo", page_size=100, also_in="yes")["items"]})


if __name__ == "__main__":
    unittest.main()
