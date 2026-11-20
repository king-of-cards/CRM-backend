"""Wedding gifts: a KOC -> Vendors source split out of Spin (no new table).

Workflow spin_lead rows whose otherWeddingServices names "Wedding Gifts & Return Gifts" are the
wedding_gifts flow; every other listed row stays spin. CRM-added leads follow the same rule. All
wedding_gifts leads go to the vendor named Kraftzy (when it exists and is active). In-memory SQLite
and the fake workflow database of test_crm_leads_api only. Run:

    python -m unittest discover -s tests -t . -v
"""
import csv
import io
import unittest
from datetime import datetime
from unittest import mock

from tests.test_crm_leads_api import API, LIST_URL, ApiTestBase, workflow_flow, xlsx_file
import tests.test_crm_leads_api as base

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy import BigInteger, Column, MetaData, Table  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.api.v1.endpoints import analytics  # noqa: E402
from app.api.v1.endpoints import spin_leads as sl  # noqa: E402
from app.core import response_cache  # noqa: E402
from app.models.crm_spin_lead import CrmSpinLead  # noqa: E402
from app.models.notification import CrmNotification  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.vendor import Vendor  # noqa: E402
from app.models.vendor_user import VendorUser  # noqa: E402
from app.services import lead_sources  # noqa: E402

GIFTS = "/wedding-gifts-leads"


def row(lead_id, phone, services, pincode="560011", created="2026-11-10T05:00:00"):
    return {"id": lead_id, "name": f"Lead {lead_id}", "phone": phone, "created_at": created, "branch_id": None,
            "additional_details.pincode": pincode, "additional_details.occasionDate": "2026-12-20",
            "additional_details.estimatedBudgetPerCard": "10-20",
            "additional_details.otherWeddingServices": services}


ROWS = [
    row(201, "9845020001", "Photography & Videography"),                                   # spin
    row(202, "9845020002", "Wedding Gifts & Return Gifts"),                                # gifts
    row(203, "9845020003", "Photography & Videography, wedding gifts & return gifts, Wedding Decor"),  # gifts
    row(204, "9845020004", "Gift"),                                                        # spin: not the phrase
    row(205, "9845020002", "Makeup Artists"),                                              # spin, 202's phone
]
SPIN_IDS, GIFT_IDS = {"201", "204", "205"}, {"202", "203"}


def analytics_rows():
    """What analytics._workflow_spin_rows reads (both flows, tagged)."""
    return [{"key": str(r["id"]), "created": datetime.fromisoformat(r["created_at"]), "occasion_type": "Wedding",
             "occasion_date": r["additional_details.occasionDate"], "city": None,
             "pincode": r["additional_details.pincode"], "is_workflow": True, "flow": workflow_flow(r)}
            for r in ROWS]


class WeddingGiftsTestBase(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.rows = mock.patch.object(base, "WORKFLOW_ROWS", ROWS)
        self.analytics = mock.patch.object(analytics, "_workflow_spin_rows", analytics_rows)
        self.rows.start()
        self.analytics.start()

    def tearDown(self):
        self.analytics.stop()
        self.rows.stop()
        super().tearDown()

    # ── helpers ──
    def get(self, path, who="admin", **params):
        with self.logged_in(who):
            resp = self.client.get(f"{API}{path}", params=params)
        self.assertEqual(resp.status_code, 200, f"{path}: {resp.text}")
        return resp.json()

    def keys(self, path, who="admin", **params) -> set[str]:
        items = self.get(path, who, page_size=100, **params)["items"]
        return {str(i["id"]) for i in items}

    def item(self, path, key, who="admin"):
        return next(i for i in self.get(path, who, page_size=100)["items"] if str(i["id"]) == key)

    def create_kraftzy(self, active=True) -> int:
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}/vendors", json={"name": "kraftzy", "is_active": active})
        self.assertEqual(resp.status_code, 201, resp.text)
        vendor_id = resp.json()["id"]
        with Session(self.engine, expire_on_commit=False) as s:
            self.users["kraftzy"] = User(name="Kraftzy User", email="kraftzy@example.com", hashed_password="x",
                                         role="vendor")
            s.add(self.users["kraftzy"])
            s.commit()
            s.add(VendorUser(user_id=self.users["kraftzy"].id, vendor_id=vendor_id))
            s.commit()
        response_cache.clear()
        return vendor_id

    def put(self, who, key, body, source="wedding_gifts"):
        with self.logged_in(who):
            return self.client.put(f"{API}/leads/{source}/{key}/crm", json=body)

    def import_rows(self, source, rows):
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}/crm-leads/{source}/import",
                                    json={"rows": [{"row": i + 2, "values": v} for i, v in enumerate(rows)]})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["rows"]


# ── The rule and the split ─────────────────────────────────────────────
class FlowRuleTests(unittest.TestCase):
    def test_is_gift(self):
        for value in ("Wedding Gifts & Return Gifts", "wedding gifts & return gifts",
                      "Photography & Videography, Wedding Gifts & Return Gifts, Wedding Loans & Finance",
                      "Wedding Gifts & Return Gifts, Not Interested"):
            self.assertTrue(sl.is_gift(value), value)
        for value in ("Gift", "Photography & Videography", "", None, "Return Gifts"):
            self.assertFalse(sl.is_gift(value), value)
        self.assertEqual(sl.flow_of_details({"otherWeddingServices": "x, WEDDING GIFTS & RETURN GIFTS"}), "wedding_gifts")
        self.assertEqual(sl.flow_of_details({}), "spin")

    def test_sql_filters_are_complementary(self):
        table = Table("spin_lead", MetaData(), Column("id", BigInteger, primary_key=True),
                      Column("additional_details", JSONB))
        dialect = postgresql.dialect()

        def sql(flow):
            return " AND ".join(str(c.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
                                for c in sl.base_filter(table, flow)).replace("%%", "%")
        spin, gifts, both = sql("spin"), sql("wedding_gifts"), sql(None)
        like = ("lower(spin_lead.additional_details ->> 'otherWeddingServices') "
                "LIKE '%wedding gifts & return gifts%' ESCAPE '/'")
        self.assertIn(like, gifts)
        self.assertIn(f"coalesce({like}", spin)
        self.assertIn("= false", spin)
        self.assertNotIn("LIKE", both)                     # the pre-split listing: required keys only
        self.assertTrue(spin.startswith(both) and gifts.startswith(both))
        with self.assertRaises(ValueError):
            sl.flow_filter(table, "other")

    def test_lead_sources_accepts_wedding_gifts(self):
        self.assertEqual(lead_sources.parse_sources("wedding_gifts, spin"), {"wedding_gifts", "spin"})


class SplitTests(WeddingGiftsTestBase):
    def test_no_lead_in_both_and_totals_unchanged(self):
        crm_spin = self.import_rows("spin", [{"phone": "9845030001", "name": "CRM spin"}])[0]["lead_key"]
        crm_gift = self.import_rows("wedding_gifts", [{"phone": "9845030002", "name": "CRM gift"}])[0]["lead_key"]
        spin, gifts = self.keys(LIST_URL["spin"]), self.keys(GIFTS)
        self.assertEqual(spin, SPIN_IDS | {crm_spin})
        self.assertEqual(gifts, GIFT_IDS | {crm_gift})
        self.assertEqual(spin & gifts, set())
        # Spin before the split = every listed workflow row + every CRM spin lead
        with Session(self.engine) as s:
            crm_count = len(s.exec(select(CrmSpinLead).where(CrmSpinLead.is_deleted == False)).all())  # noqa: E712
        spin_before = len(ROWS) + crm_count
        spin_total = self.get(LIST_URL["spin"])["total"]
        gifts_total = self.get(GIFTS)["total"]
        self.assertEqual(spin_total + gifts_total, spin_before)
        self.assertEqual(self.get(f"{LIST_URL['spin']}/stats")["total"] + self.get(f"{GIFTS}/stats")["total"],
                         spin_before)
        overview = self.get("/analytics/overview", period="all")
        self.assertEqual(overview["totals"]["spin"] + overview["totals"]["wedding_gifts"], spin_before)
        self.assertEqual(overview["totals"]["wedding_gifts"], gifts_total)
        # The CRM gift lead carries the phrase, so the rule (not a marker column) places it
        with Session(self.engine) as s:
            lead = s.get(CrmSpinLead, int(crm_gift.split("-")[1]))
            self.assertIn("Wedding Gifts & Return Gifts", lead.additional_details["otherWeddingServices"])

    def test_stats_filter_options_and_export_match_the_list(self):
        listed = self.get(GIFTS)
        self.assertEqual(self.get(f"{GIFTS}/stats")["total"], listed["total"])
        self.assertEqual(self.get(f"{GIFTS}/stats", pincode="560")["total"], 2)
        self.assertEqual(self.get(f"{GIFTS}/stats", pincode="999")["total"], 0)
        options = self.get(f"{GIFTS}/filter-options")
        self.assertIn("origins", options)
        with self.logged_in("admin"):
            resp = self.client.get(f"{API}/crm-leads/wedding_gifts/export", params={"format": "csv"})
        self.assertEqual(resp.status_code, 200, resp.text)
        lines = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
        self.assertEqual(len(lines) - 1, listed["total"])
        self.assertIn("wedding_gifts-leads", resp.headers["content-disposition"])
        with self.logged_in("admin"):
            spin_csv = self.client.get(f"{API}/crm-leads/spin/export", params={"format": "csv"})
        self.assertEqual(len(list(csv.reader(io.StringIO(spin_csv.content.decode("utf-8-sig"))))) - 1,
                         len(SPIN_IDS))

    def test_shared_filters_work(self):
        self.put("admin", "202", {"update_status": "interested", "quoted_amount": 5000})
        with self.logged_in("admin"):
            self.assertEqual(self.client.post(f"{API}/leads/wedding_gifts/203/hot").status_code, 200)
        self.assertEqual(self.keys(GIFTS, update_status="interested"), {"202"})
        self.assertEqual(self.keys(GIFTS, quote="has"), {"202"})
        self.assertEqual(self.keys(GIFTS, hot="yes"), {"203"})
        self.assertEqual(self.keys(GIFTS, also_in="yes"), {"202"})          # 205 (spin) has its phone
        self.assertEqual(self.keys(GIFTS, date_from="2026-11-10", date_to="2026-11-10"), GIFT_IDS)
        self.assertEqual(self.keys(GIFTS, date_from="2026-11-11", date_to="2026-11-12"), set())
        self.assertEqual(self.keys(LIST_URL["spin"], hot="yes"), set())       # hot is per source


# ── Kraftzy and vendor scoping ─────────────────────────────────────────
class KraftzyTests(WeddingGiftsTestBase):
    def test_unassigned_until_kraftzy_exists_then_automatic(self):
        self.assertIsNone(self.item(GIFTS, "202")["crm"]["vendor"])
        self.assertIsNone(self.item(GIFTS, "202")["crm"]["suggested_vendor"])   # area rules don't apply
        self.assertEqual(self.item(LIST_URL["spin"], "201")["crm"]["vendor"]["name"], "Phodeo")  # same pincode
        kraftzy = self.create_kraftzy()
        crm = self.item(GIFTS, "202")["crm"]
        self.assertEqual((crm["vendor"], crm["vendor_source"]), ({"id": kraftzy, "name": "kraftzy"}, "source_default"))
        self.assertEqual(self.item(LIST_URL["spin"], "201")["crm"]["vendor"]["name"], "Phodeo")  # spin unchanged
        # Inactive: unassigned again
        with self.logged_in("admin"):
            self.assertEqual(self.client.patch(f"{API}/vendors/{kraftzy}", json={"is_active": False}).status_code, 200)
        response_cache.clear()
        self.assertIsNone(self.item(GIFTS, "202")["crm"]["vendor"])

    def test_vendor_scoping_and_manual_reassignment(self):
        self.create_kraftzy()
        self.assertEqual(self.keys(GIFTS, "kraftzy"), GIFT_IDS)
        self.assertEqual(self.keys(LIST_URL["spin"], "kraftzy"), set())
        for vendor in ("phodeo", "rj", "ohp"):
            self.assertEqual(self.keys(GIFTS, vendor), set(), vendor)
        self.assertIn("201", self.keys(LIST_URL["spin"], "phodeo"))
        # Kraftzy's users may update their leads; other vendors get 404
        self.assertEqual(self.put("kraftzy", "202", {"update_status": "interested"}).status_code, 200)
        self.assertEqual(self.put("phodeo", "202", {"update_status": "converted"}).status_code, 404)
        self.assertEqual(self.put("kraftzy", "202", {"vendor_id": self.vendor_ids["phodeo"]}).status_code, 403)
        # An admin can still move a lead by hand
        moved = self.put("admin", "203", {"vendor_id": self.vendor_ids["phodeo"]})
        self.assertEqual((moved.status_code, moved.json()["vendor_source"]), (200, "manual"))
        self.assertEqual(self.keys(GIFTS, "phodeo"), {"203"})
        self.assertEqual(self.keys(GIFTS, "kraftzy"), {"202"})
        with Session(self.engine) as s:
            note = s.exec(select(CrmNotification).where(CrmNotification.lead_key == "203")).one()
            self.assertEqual(note.source, "wedding_gifts")
        # Back to automatic: Kraftzy again
        self.assertEqual(self.put("admin", "203", {"vendor_id": None}).json()["vendor"]["name"], "kraftzy")

    def test_bulk_upload_automatic_and_empty_go_to_kraftzy(self):
        self.create_kraftzy()
        rows = self.import_rows("wedding_gifts", [
            {"phone": "9845040001", "name": "Auto", "assigned_to": "Automatic"},
            {"phone": "9845040002", "name": "Empty", "assigned_to": ""},
            {"phone": "9845040003", "name": "None"},
            {"phone": "9845040004", "name": "Unassigned", "assigned_to": "Unassigned"},
            {"phone": "9845040005", "name": "Manual", "assigned_to": "Phodeo"},
        ])
        self.assertTrue(all(r["status"] == "imported" for r in rows), rows)
        vendor = {r["values"]["name"]: (self.item(GIFTS, r["lead_key"])["crm"]["vendor"] or {}).get("name")
                  for r in rows}
        self.assertEqual(vendor, {"Auto": "kraftzy", "Empty": "kraftzy", "None": "kraftzy", "Unassigned": None,
                                  "Manual": "Phodeo"})
        # Spin uploads keep their rule: empty = unassigned
        spin = self.import_rows("spin", [{"phone": "9845040006", "name": "Spin empty"}])[0]
        self.assertIsNone(self.item(LIST_URL["spin"], spin["lead_key"])["crm"]["vendor"])


# ── Features ───────────────────────────────────────────────────────────
class FeatureTests(WeddingGiftsTestBase):
    def test_crm_hot_activity_follow_up_and_reminders(self):
        self.create_kraftzy()
        saved = self.put("admin", "202", {"update_status": "will_call_back", "follow_up_date": "2026-11-20",
                                          "notes": "Call"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.get("/leads/wedding_gifts/202/crm")["update_status"], "will_call_back")
        with self.logged_in("kraftzy"):
            self.assertEqual(self.client.post(f"{API}/leads/wedding_gifts/202/hot").json()["is_hot"], True)
            self.assertEqual(self.client.post(f"{API}/leads/spin/201/hot").status_code, 404)
        activity = self.get("/leads/wedding_gifts/202/activity")["items"]
        self.assertTrue({"field_changed", "hot_on"} <= {a["action"] for a in activity})
        with self.logged_in("admin"):
            self.assertEqual(self.client.get(f"{API}/leads/spin/202/activity").status_code, 404)   # not a spin lead
        item = self.item(GIFTS, "202")
        self.assertEqual((item["follow_up_date"], item["is_hot"]), ("2026-11-20", True))
        with mock.patch("app.services.periods.today_ist", lambda: datetime(2026, 11, 20).date()):
            reminders = self.get("/reminders/follow-ups", sources="wedding_gifts")
            mine = self.get("/reminders/follow-ups", who="kraftzy")
            spin_only = self.get("/reminders/follow-ups", sources="spin")
        self.assertEqual([(i["source"], i["lead_key"]) for i in reminders["items"]], [("wedding_gifts", "202")])
        self.assertEqual(reminders["items"][0]["name"], "Lead 202")
        self.assertEqual([i["lead_key"] for i in mine["items"]], ["202"])
        self.assertEqual(spin_only["items"], [])

    def test_also_in_follows_the_flow(self):
        spin_item = self.item(LIST_URL["spin"], "205")
        self.assertEqual({a["source"] for a in spin_item["also_in"]}, {"wedding_gifts"})
        gift_item = self.item(GIFTS, "202")
        self.assertEqual({a["source"] for a in gift_item["also_in"]}, {"spin"})
        added = ApiTestBase.add(self, "wedding", {"phone": "9845020003", "name": "Wedding twin", "city": "Mysore"})
        self.assertEqual(added.status_code, 201, added.text)
        key = added.json()["lead"]["lead_key"]
        wedding_item = next(i for i in self.get(LIST_URL["wedding"], page_size=100)["items"]
                            if i["crm"]["lead_key"] == key)
        self.assertEqual({a["source"] for a in wedding_item["also_in"]}, {"wedding_gifts"})

    def test_upload_preview_template_fields_and_cross_flow_duplicates(self):
        fields = self.get("/crm-leads/wedding_gifts/fields")
        self.assertEqual(fields["label"], "Wedding gifts")
        self.assertIn("branches", fields)
        with self.logged_in("admin"):
            template = self.client.get(f"{API}/crm-leads/wedding_gifts/template")
            self.assertEqual(template.status_code, 200)
            self.assertEqual(self.client.post(f"{API}/crm-leads/wedding_gifts", json={"values": {}}).status_code, 405)
            preview = self.client.post(f"{API}/crm-leads/wedding_gifts/import/preview", files={"file": (
                "gifts.xlsx", xlsx_file([["Lead", "Contact", "Assigned to"],
                                         ["New", "9845050001", "Automatic"],
                                         ["Spin twin", "9845020001", ""],
                                         ["Gift twin", "9845020003", ""]]))})
        self.assertEqual(preview.status_code, 200, preview.text)
        by_name = {r["values"]["name"]: r for r in preview.json()["rows"]}
        self.assertEqual(by_name["New"]["status"], "ok")
        # The duplicate check covers every workflow row, in both flows
        self.assertEqual(by_name["Spin twin"]["duplicate"], {"reason": "existing_workflow", "lead_key": "201"})
        self.assertEqual(by_name["Gift twin"]["duplicate"], {"reason": "existing_workflow", "lead_key": "203"})
        spin = self.import_rows("spin", [{"phone": "9845020002", "name": "Into spin"}])[0]
        self.assertEqual(spin["status"], "duplicate")
        self.assertIn(spin["duplicate"]["lead_key"], {"202", "205"})
        gift = self.import_rows("wedding_gifts", [{"phone": "9845060001", "name": "CRM gift"}])[0]
        again = self.import_rows("spin", [{"phone": "9845060001", "name": "Again"}])[0]
        self.assertEqual((again["status"], again["duplicate"]["reason"], again["duplicate"]["lead_key"]),
                         ("duplicate", "existing_added", gift["lead_key"]))
        # A gift CRM lead is a wedding_gifts lead, not a spin one
        with self.logged_in("admin"):
            self.assertEqual(self.client.get(f"{API}/crm-leads/wedding_gifts/{gift['lead_key']}").status_code, 200)
            self.assertEqual(self.client.get(f"{API}/crm-leads/spin/{gift['lead_key']}").status_code, 404)

    def test_dashboard_includes_wedding_gifts(self):
        self.put("admin", "202", {"update_status": "converted", "conversion_amount": 9000})
        o = self.get("/analytics/overview", period="all")
        self.assertEqual(o["new_in_period"]["wedding_gifts"], 2)
        self.assertEqual(o["new_in_period"]["spin"], 3)
        self.assertIn("wedding_gifts", {m["source"] for m in o["source_mix"]})
        self.assertEqual(o["outcomes"]["wedding_gifts"]["converted"], 1)
        self.assertEqual(o["outcomes"]["spin"]["converted"], 0)
        self.assertEqual(o["kpis"]["converted"], 1)
        self.assertEqual(sum(p["wedding_gifts"] for p in o["trend"]), 2)
        self.assertIn("wedding_gifts", o["top_cities_sources"])
        self.assertIn("wedding_gifts", o["upcoming_by_month"][0])
        # Vendor logins: Kraftzy's overview counts its gift leads only
        self.create_kraftzy()
        mine = self.get("/analytics/overview", who="kraftzy", period="all")
        self.assertEqual((mine["new_in_period"]["wedding_gifts"], mine["new_in_period"]["spin"]), (2, 0))
        phodeo = self.get("/analytics/overview", who="phodeo", period="all")
        self.assertEqual(phodeo["new_in_period"]["wedding_gifts"], 0)

    def test_vendor_stats_count_wedding_gifts_for_kraftzy(self):
        self.create_kraftzy()
        from app.api.v1.endpoints import vendors as vendors_ep
        with mock.patch.object(vendors_ep, "engine", self.engine):
            stats = vendors_ep._build_stats()
        kraftzy = next(v for v in stats["vendors"] if v["vendor"]["name"] == "kraftzy")
        self.assertEqual((kraftzy["leads"], kraftzy["by_source"]), (2, {"wedding_gifts": 2}))
        phodeo = next(v for v in stats["vendors"] if v["vendor"]["name"] == "Phodeo")
        self.assertNotIn("wedding_gifts", phodeo["by_source"])


if __name__ == "__main__":
    unittest.main()
