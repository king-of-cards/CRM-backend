"""KOC -> Dhruva (koc_to_dhruva): spin leads KOC shares with Dhruva by rule.

Rule: estimatedBudgetPerCard's first number >= 70, or quantityRequired >= 1000, over every workflow
row (any date; CRM-uploaded spin leads never). Live leads stay in spin too, with separate CRM
details. In-memory SQLite and the fake workflow database only. Run:

    python -m unittest discover -s tests -t . -v
"""
import csv
import io
import json
import unittest
from datetime import date, datetime, timezone
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
from app.models.lead_meta import LeadMeta  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import koc_dhruva, lead_sources, periods  # noqa: E402

K2D = "/koc-to-dhruva"
SRC = "koc_to_dhruva"
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
    row(302, "9845070002", "10-20", 1000, services=GIFT),                     # quantity     spin (gift)
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
        self.assertEqual(self.keys(), KOC_IDS)                              # CRM-uploaded spin leads: never
        self.get(f"{K2D}/leads/{crm['lead_key']}", status=404)
        spin = {str(i["id"]) for i in self.get("/spin-leads", page_size=100)["items"]}
        self.assertTrue({"301", "302", "305", crm["lead_key"]} <= spin)    # still in spin (302: a gift lead)
        items = {i["lead_key"]: i for i in self.get(f"{K2D}/leads", page_size=100)["items"]}
        self.assertEqual({k: i["potential_value"] for k, i in items.items()},
                         {"301": 14000, "302": 10000, "305": None, "306": None})
        self.assertFalse({"reason", "reason_label"} & set(items["301"]))
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
        self.assertEqual(stats["kpis"]["converted"], 1)
        self.assertNotIn("by_reason", stats)
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
        self.assertEqual(lines[0], ["Sl. No", "Name", "Phone", "Email", "Occasion", "Event date", "Branch",
                                    "Branch code", "Estimated budget per card", "Quantity required", "Received",
                                    "Update", "Additional comments", "Quoted amount", "Quoted date",
                                    "Conversion amount", "Follow-up date", "Hot"])
        self.assertEqual(len(lines), 3)                       # the reason filter still works (302, 306)
        by_name = {r[1]: r for r in lines[1:]}
        self.assertEqual(by_name["Lead 302"][9], "1000")
        self.assertEqual(by_name["Lead 302"][11:14], ["Will call back", "Call Sunday", "9000"])


# ── Branch: as the spin list and the spin export ───────────────────────
BRANCH_ROWS = [dict(r, branch_id=b) for r, b in zip(ROWS, (1, 7, None, None, 999, None, None))]
#   301: branch 1 (Branches page)   302: 7 (built-in store only)   305: 999 (unknown)   306: none


class BranchTests(KocTestBase):
    def setUp(self):
        super().setUp()
        self.branch_rows = mock.patch.object(base, "WORKFLOW_ROWS", BRANCH_ROWS)
        self.branch_rows.start()

    def tearDown(self):
        self.branch_rows.stop()
        super().tearDown()

    def test_items_carry_the_spin_branch(self):
        koc = {i["lead_key"]: i for i in self.get(f"{K2D}/leads", page_size=100)["items"]}
        spin = {str(i["id"]): i for i in self.get("/spin-leads", page_size=100)["items"]}
        for key in ("301", "302", "305", "306"):
            self.assertEqual((koc[key]["branch_id"], koc[key]["branch"]),
                             (spin[key]["branch_id"], spin[key]["branch"]), key)
            self.assertEqual(self.get(f"{K2D}/leads/{key}")["branch"], spin[key]["branch"], key)
        self.assertEqual(koc["301"]["branch"], {"id": 1, "name": "Chamrajpet", "code": "CHA-001", "city": "Bangalore"})
        self.assertEqual(koc["305"]["branch"], {"id": 999, "name": "Branch 999", "code": None, "city": None})
        self.assertIsNone(koc["306"]["branch"])                         # no pincode-area fallback, as spin

    def test_export_branch_and_code_as_the_spin_export(self):
        with self.logged_in("admin"):
            koc = self.client.get(f"{API}{K2D}/leads/export", params={"format": "csv"})
            spin = self.client.get(f"{API}/crm-leads/spin/export", params={"format": "csv"})
        koc_rows = list(csv.reader(io.StringIO(koc.content.decode("utf-8-sig"))))
        spin_rows = list(csv.reader(io.StringIO(spin.content.decode("utf-8-sig"))))
        k, s = koc_rows[0], spin_rows[0]
        koc_branch = {r[k.index("Name")]: (r[k.index("Branch")], r[k.index("Branch code")]) for r in koc_rows[1:]}
        spin_branch = {r[s.index("Lead")]: (r[s.index("Branch")], r[s.index("Branch code")]) for r in spin_rows[1:]}
        self.assertEqual(koc_branch["Lead 301"], ("Chamrajpet", "CHA-001 · Bangalore"))
        self.assertEqual(koc_branch["Lead 302"], ("Hyderabad - Kondhapur", "HYB-007 · Hyderabad"))   # store fallback
        self.assertEqual(koc_branch["Lead 305"], ("", ""))                                          # unknown id
        self.assertEqual(koc_branch["Lead 306"], ("", ""))
        for name in ("Lead 301", "Lead 302", "Lead 305", "Lead 306"):
            self.assertEqual(koc_branch[name], spin_branch[name], name)
        self.assertNotIn("Reason", k)
        self.assertNotIn("Pincode", k)


# ── Dashboard ──────────────────────────────────────────────────────────
TODAY = date(2026, 11, 21)


def dash_row(lead_id, budget, quantity, event, created, branch):
    return dict(row(lead_id, f"98450900{lead_id - 500:02d}", budget, quantity, created=created),
                branch_id=branch, **{"additional_details.occasionDate": event})


DASH_ROWS = [
    #        budget       qty   event         created (UTC)          branch   potential
    dash_row(501, "70-100", 1000, "2026-12-01", "2026-11-01T05:00:00", 1),     # 70000  interested, quoted
    dash_row(502, "100-200", 500, "2027-03-01", "2026-11-05T05:00:00", 4),     # 50000  converted
    dash_row(503, "500_PLUS", 200, "2027-01-10", "2026-10-15T05:00:00", 7),    # 100000 pending (stale)
    dash_row(504, "UNDER_50", 1500, "2027-02-15", "2026-11-20T05:00:00", None),  # 0    pending
    dash_row(505, None, 2000, "Dec 4th", "2026-09-10T05:00:00", 1),            # None   will call back (stale)
    dash_row(506, "200-500", 100, "2026-11-10", "2026-11-18T05:00:00", 999),   # 20000  not interested
    dash_row(507, "70-100", 300, "2026-12-20", "2026-11-21T03:00:00", 4),      # 21000  purchased with us
    dash_row(508, "10-20", 100, "2026-12-20", "2026-11-21T03:00:00", 4),       # not a KOC -> Dhruva lead
]


class DashboardTests(KocTestBase):
    def setUp(self):
        super().setUp()
        self.extra = [mock.patch.object(base, "WORKFLOW_ROWS", DASH_ROWS),
                      mock.patch.object(periods, "today_ist", lambda: TODAY)]
        for p in self.extra:
            p.start()
        recent, old = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc), datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
        metas = [
            LeadMeta(source=SRC, lead_key="501", update_status="interested", quoted_amount=10000,
                     follow_up_date=TODAY, updated_at=recent),
            LeadMeta(source=SRC, lead_key="502", update_status="converted", quoted_amount=40000,
                     conversion_amount=38000, updated_at=recent),
            LeadMeta(source=SRC, lead_key="505", update_status="will_call_back",
                     follow_up_date=date(2026, 11, 15), updated_at=old),
            LeadMeta(source=SRC, lead_key="506", update_status="not_interested", updated_at=recent),
            LeadMeta(source=SRC, lead_key="507", update_status="purchased_with_us",
                     follow_up_date=date(2026, 11, 1), updated_at=recent),
        ]
        with Session(self.engine) as session:
            session.add_all(metas)
            session.commit()

    def tearDown(self):
        for p in self.extra:
            p.stop()
        super().tearDown()

    def test_kpis_and_funnel(self):
        o = self.get("/analytics/koc-to-dhruva")                          # all time by default
        self.assertTrue(o["period"]["all_time"])
        k = o["kpis"]
        self.assertEqual((k["shared"], k["pending"], k["interested"], k["quotes_count"], k["quoted_total"]),
                         (7, 2, 1, 2, 50000))
        self.assertEqual((k["converted"], k["converted_pct"], k["conversion_total"]), (1, 14.3, 38000))
        self.assertEqual((k["conversion_amount_count"], k["conversion_amount_total"]), (1, 38000))
        self.assertEqual(k["follow_ups_due"], 2)                    # 501 today, 505 overdue; 507 is closed
        self.assertEqual(k["potential_value"], 170000)              # open: 501 + 503 + 504 (0); 505 has no budget
        self.assertEqual([(f["stage"], f["count"]) for f in o["funnel"]],
                         [("shared", 7), ("contacted", 5), ("interested", 4), ("quoted", 2), ("converted", 1)])

    def test_upcoming_top_value_and_stale(self):
        o = self.get("/analytics/koc-to-dhruva")
        u = o["upcoming_events"]
        self.assertEqual((u["next_30_days"], u["next_60_days"], u["next_90_days"]), (1, 2, 3))
        self.assertEqual([e["lead_key"] for e in u["next"]], ["501", "503", "504"])   # open only, by date
        self.assertEqual(u["next"][0], {"lead_key": "501", "name": "Lead 501", "event_date": "2026-12-01",
                                        "budget": "70-100", "quantity": 1000, "potential_value": 70000,
                                        "update_status": "interested", "status_label": "Interested"})
        self.assertEqual([(e["lead_key"], e["potential_value"]) for e in o["top_value_leads"]],
                         [("503", 100000), ("501", 70000), ("504", 0)])
        self.assertEqual(o["stale"]["count"], 2)                    # 503 never touched, 505 untouched 20 days
        self.assertEqual([e["lead_key"] for e in o["stale"]["oldest"]], ["503", "505"])
        self.assertEqual((o["stale"]["oldest"][0]["last_update"], o["stale"]["oldest"][1]["last_update"][:10]),
                         (None, "2026-11-01"))
        self.assertEqual(o["stale"]["oldest"][0]["received"][:10], "2026-10-15")

    def test_monthly_branches_bands_and_latest(self):
        o = self.get("/analytics/koc-to-dhruva")
        self.assertEqual(o["monthly"], [{"month": "2026-09", "shared": 1, "converted": 0},
                                        {"month": "2026-10", "shared": 1, "converted": 0},
                                        {"month": "2026-11", "shared": 5, "converted": 1}])
        self.assertEqual(o["by_branch"], [
            {"branch_id": 1, "name": "Chamrajpet", "code": "CHA-001 · Bangalore", "leads": 2, "converted": 0,
             "conversion_amount": 0},
            {"branch_id": 4, "name": "Jayanagar", "code": "JNG-004 · Bangalore", "leads": 2, "converted": 1,
             "conversion_amount": 38000},
            {"branch_id": None, "name": None, "code": None, "leads": 2, "converted": 0, "conversion_amount": 0},
            {"branch_id": 7, "name": "Hyderabad - Kondhapur", "code": "HYB-007 · Hyderabad", "leads": 1,
             "converted": 0, "conversion_amount": 0}])
        bands = {b["band"]: (b["leads"], b["converted"], b["converted_pct"]) for b in o["by_budget_band"]}
        self.assertEqual(bands, {"70-100": (2, 0, 0.0), "100-200": (1, 1, 100.0), "200-500": (1, 0, 0.0),
                                 "500+": (1, 0, 0.0), "under_70": (1, 0, 0.0), "not_given": (1, 0, 0.0)})
        self.assertEqual([r["lead_key"] for r in o["latest"]], ["507", "504", "506", "502", "501", "503"])
        self.assertEqual(o["latest"][0], {"lead_key": "507", "name": "Lead 507", "occasion": "Wedding",
                                          "event_date": "2026-12-20", "budget": "70-100", "quantity": 300,
                                          "update_status": "purchased_with_us",
                                          "status_label": "Already purchased with us",
                                          "received": "2026-11-21T03:00:00"})
        self.assertEqual(sum(p["total"] for p in o["inflow"]), 7)
        self.assertEqual((o["by_status"]["pending"], o["by_status"]["converted"]), (2, 1))
        self.assertNotIn("reason", json.dumps(o))                   # nowhere, also not in by_reason / inflow

    def test_period_limits_everything_but_follow_ups(self):
        o = self.get("/analytics/koc-to-dhruva", date_from="2026-11-01", date_to="2026-11-21")
        self.assertEqual((o["kpis"]["shared"], o["kpis"]["follow_ups_due"], o["kpis"]["potential_value"]), (5, 2, 70000))
        self.assertEqual([m["month"] for m in o["monthly"]], ["2026-11"])
        self.assertEqual([e["lead_key"] for e in o["top_value_leads"]], ["501", "504"])
        self.assertEqual(o["stale"]["count"], 0)

    def test_list_items_and_potential_value_sort(self):
        items = self.get(f"{K2D}/leads", page_size=100, sort="potential_value", order="desc")["items"]
        self.assertEqual([(i["lead_key"], i["potential_value"]) for i in items],
                         [("503", 100000), ("501", 70000), ("502", 50000), ("507", 21000), ("506", 20000),
                          ("504", 0), ("505", None)])                # empty last
        self.assertNotIn("reason", json.dumps(items))
        self.assertEqual(self.get(f"{K2D}/leads/503")["potential_value"], 100000)
        self.assertNotIn("reasons", self.get(f"{K2D}/leads/filter-options"))
        self.assertNotIn("by_reason", self.get(f"{K2D}/leads/stats"))
        self.assertEqual(self.get(f"{K2D}/leads/stats", reason="quantity")["total"], 2)   # filter still works


class PotentialValueRuleTests(unittest.TestCase):
    def test_python_rule(self):
        cases = [("70-100", 1000, 70000), ("500_PLUS", 200, 100000), ("UNDER_50", 1500, 0), ("under 50", 10, 0),
                 ("12.5-20", 3, 37.5), (None, 1000, None), ("70-100", None, None), ("n/a", 10, None)]
        for budget, qty, value in cases:
            self.assertEqual(koc_dhruva.potential_value(budget, qty), value, (budget, qty))
        self.assertTrue(koc_dhruva.is_open(None) and koc_dhruva.is_open("interested")
                        and koc_dhruva.is_open("will_call_back"))
        for closed in ("converted", "purchased_with_us", "not_converted", "not_interested", "purchased_elsewhere"):
            self.assertFalse(koc_dhruva.is_open(closed), closed)

    def test_sql_rule(self):
        table = Table("spin_lead", MetaData(), Column("id", BigInteger, primary_key=True),
                      Column("additional_details", JSONB))
        sql = str(sl.koc_potential_expr(table).compile(dialect=postgresql.dialect(),
                                                        compile_kwargs={"literal_binds": True}))
        self.assertIn("LIKE 'under%", sql)
        self.assertIn("'estimatedBudgetPerCard'", sql)
        self.assertIn("'quantityRequired'", sql)


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
        self.assertEqual({a["source"] for a in self.item(f"{K2D}/leads", "302")["also_in"]}, {"spin"})
        self.assertEqual(self.item(f"{K2D}/leads", "301", "dhruva")["also_in"], [])   # Dhruva: no other section
        self.assertEqual(self.keys(also_in="yes"), KOC_IDS)
        self.assertNotIn("301", {str(i["id"]) for i in
                                 self.get("/spin-leads", "phodeo", page_size=100, also_in="yes")["items"]})


if __name__ == "__main__":
    unittest.main()
