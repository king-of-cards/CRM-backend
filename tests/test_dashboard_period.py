"""Dashboard date filter: GET /analytics/overview?date_from=&date_to= (IST days, inclusive).

"Today" is fixed at 16 Nov 2026 (India time). Runs against an in-memory SQLite database and a fake
workflow database (see test_crm_leads_api.ApiTestBase). Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from sqlmodel import Session

from app.api.v1.endpoints import analytics
from app.models.crm_spin_lead import CrmSpinLead
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.services import periods
from tests.test_crm_leads_api import API, NOW, XLSX, ApiTestBase, xlsx_file

TODAY = date(2026, 11, 16)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# Workflow spin leads (created_at stored in UTC, without a time zone)
SPIN_ROWS = [
    {"key": "501", "created": datetime(2026, 11, 16, 2, 0), "occasion_type": "Wedding", "occasion_date": "2026-11-25"},
    {"key": "502", "created": datetime(2026, 11, 5, 4, 30), "occasion_type": "Birthday", "occasion_date": None},
]


class DashboardPeriodTest(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.today = mock.patch.object(periods, "today_ist", lambda: TODAY)
        self.spin = mock.patch.object(analytics, "_workflow_spin_rows", lambda: [dict(r) for r in SPIN_ROWS])
        self.today.start()
        self.spin.start()
        sheet = {"origin": "sheet", "row_hash": "", "first_seen_at": NOW, "updated_at": NOW}
        with Session(self.engine) as s:
            s.add_all([
                # 23:30 IST on 15 Nov / 00:30 IST on 16 Nov (19:00 UTC on the 15th)
                WhatsAppLead(sheet_key="WA_1530", country="UAE", phone="971500000001", phone_e164="+971500000001",
                             submitted_at=utc(2026, 11, 15, 18, 0), **sheet),
                WhatsAppLead(sheet_key="WA_1600", country="UAE", phone="971500000002", phone_e164="+971500000002",
                             submitted_at=utc(2026, 11, 15, 19, 0), **sheet),
                WhatsAppLead(sheet_key="WA_NODATE", country="UAE", phone="971500000003", phone_e164="+971500000003",
                             submitted_at=None, **sheet),
                WeddingLead(sheet_key="WD_10", name="Ten", city="Bangalore", phone="919845100010", phone_e164="+919845100010",
                            submitted_at=utc(2026, 11, 10, 5, 0), budget_amount=50000, event_date=date(2026, 11, 26),
                            **sheet),
                WeddingLead(sheet_key="WD_05", name="Five", city="Mysore", shared_with="RJ", phone="919845100005",
                            phone_e164="+919845100005", submitted_at=utc(2026, 11, 5, 5, 0), **sheet),
                WeddingLead(sheet_key="WD_OCT", name="October", city="Mysore", shared_with="RJ", phone="919845100099",
                            phone_e164="+919845100099", submitted_at=utc(2026, 10, 1, 5, 0), event_date=date(2026, 12, 1),
                            **sheet),
                # Planner: arrival = updated_in_sheet_at, not first_seen_at
                WeddingPlan(plan_id="WP_14", name="Plan", city="Udaipur", phone="919845200014", phone_e164="+919845200014",
                            updated_in_sheet_at=utc(2026, 11, 14, 5, 0), fits_budget=False, total_budget=2_000_000,
                            total_guests=300, wedding_date=date(2027, 1, 10), origin="sheet", row_hash="",
                            first_seen_at=utc(2026, 1, 1, 5, 0), updated_at=NOW),
                WeddingPlan(plan_id="WP_SEP", name="Old plan", phone="919845200009", phone_e164="+919845200009",
                            updated_in_sheet_at=utc(2026, 9, 1, 5, 0), fits_budget=False, **sheet),
                # A spin lead added in the CRM
                CrmSpinLead(origin="added", name="Added", phone="9845300001", phone_e164="+919845300001",
                            created_at=utc(2026, 11, 12, 5, 0), updated_at=NOW, additional_details={}),
            ])
            s.commit()

    def tearDown(self):
        self.spin.stop()
        self.today.stop()
        super().tearDown()

    def overview(self, who="admin", **params):
        with self.logged_in(who):
            resp = self.client.get(f"{API}/analytics/overview", params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    # ── Periods ──
    def test_ist_day_boundaries(self):
        day15 = self.overview(date_from="2026-11-15", date_to="2026-11-15")
        day16 = self.overview(date_from="2026-11-16", date_to="2026-11-16")
        self.assertEqual(day15["new_in_period"]["whatsapp"], 1)          # 23:30 IST on the 15th
        self.assertEqual(day16["new_in_period"]["whatsapp"], 1)          # 00:30 IST on the 16th
        self.assertEqual(day16["new_in_period"]["spin"], 1)              # 02:00 UTC = 07:30 IST on the 16th
        self.assertEqual(day15["period"], {"date_from": "2026-11-15", "date_to": "2026-11-15", "days": 1,
                                           "previous": {"date_from": "2026-11-14", "date_to": "2026-11-14"},
                                           "granularity": "day"})

    def test_single_date_and_default_period(self):
        only_from = self.overview(date_from="2026-11-15")
        only_to = self.overview(date_to="2026-11-15")
        self.assertEqual(only_from["period"]["days"], 1)
        self.assertEqual(only_from["new_in_period"], only_to["new_in_period"])
        default = self.overview()
        self.assertEqual((default["period"]["date_from"], default["period"]["date_to"], default["period"]["days"]),
                         ("2026-10-18", "2026-11-16", 30))
        self.assertEqual(len(default["trend"]), 30)
        self.assertEqual(default["trend"][0]["date"], "2026-10-18")
        self.assertEqual(default["not_filtered"], ["upcoming_30_days", "upcoming_by_month", "needs_attention"])

    def test_range_previous_period_and_change(self):
        o = self.overview(date_from="2026-11-10", date_to="2026-11-16")     # 7 days; previous 3-9 Nov
        self.assertEqual(o["period"]["previous"], {"date_from": "2026-11-03", "date_to": "2026-11-09"})
        self.assertEqual(o["new_in_period"], {"spin": 2, "whatsapp": 2, "wedding": 1, "wedding_planner": 1, "all": 6})
        self.assertEqual(o["previous_period"], {"spin": 1, "whatsapp": 0, "wedding": 1, "wedding_planner": 0, "all": 2})
        self.assertEqual(o["change_pct"]["all"], 200)                    # 6 vs 2
        self.assertEqual(o["change_pct"]["wedding"], 0)
        self.assertIsNone(o["change_pct"]["whatsapp"])                   # nothing to compare with
        self.assertEqual(sum(p[s] for p in o["trend"] for s in ("spin", "whatsapp", "wedding", "wedding_planner")), 6)
        mix = {m["source"]: (m["count"], m["pct"]) for m in o["source_mix"]}
        self.assertEqual(mix["spin"], (2, 33))
        # Period-filtered numbers
        self.assertEqual(o["sharing"], {"shareable": 2, "shared": 0, "not_shared": 2, "rate": 0})
        self.assertEqual(o["wedding_budget"]["total"], 2_000_000)
        self.assertEqual(o["photography_budget"]["count"], 1)
        self.assertEqual(o["budget_fit"], {"over": 1})
        self.assertEqual([c["value"] for c in o["top_cities"]], ["Bangalore", "Udaipur"])

    def test_totals_are_every_lead_up_to_date_to(self):
        o = self.overview(date_from="2026-11-01", date_to="2026-11-10")
        # International: the lead with no arrival date counts; the 15/16 Nov ones don't
        self.assertEqual(o["totals"]["whatsapp"], 1)
        self.assertEqual(o["totals"]["wedding"], 3)                      # Oct, 5 Nov, 10 Nov
        self.assertEqual(o["totals"]["wedding_planner"], 1)              # Sep (14 Nov is after date_to)
        self.assertEqual(o["totals"]["spin"], 1)                         # 5 Nov (12 and 16 Nov are after date_to)
        self.assertEqual(self.overview()["totals"], {"spin": 3, "whatsapp": 3, "wedding": 3, "wedding_planner": 2, "all": 11})

    def test_per_source_arrival_dates(self):
        # Planner: updated_in_sheet_at (14 Nov), not first_seen_at (1 Jan)
        self.assertEqual(self.overview(date_from="2026-11-14", date_to="2026-11-14")["new_in_period"]["wedding_planner"], 1)
        self.assertEqual(self.overview(date_from="2026-01-01", date_to="2026-01-01")["new_in_period"]["wedding_planner"], 0)
        # Spin: workflow created_at and the CRM-added lead's created_at (12 Nov)
        self.assertEqual(self.overview(date_from="2026-11-12", date_to="2026-11-12")["new_in_period"]["spin"], 1)
        # Imported lead with an uploaded Submitted date: counted on that day
        upload = xlsx_file([["Lead", "Contact", "Submitted"], ["Upl", "9845400001", "12 Nov 2026, 10:30 am"]])
        rows = self.client.post(f"{API}/crm-leads/wedding/import/preview", files={"file": ("u.xlsx", upload, XLSX)}).json()["rows"]
        self.client.post(f"{API}/crm-leads/wedding/import", json={"rows": [{"row": r["row"], "values": r["values"]} for r in rows]})
        self.assertEqual(self.overview(date_from="2026-11-12", date_to="2026-11-12", refresh=True)["new_in_period"]["wedding"], 1)

    def test_long_period_has_weekly_points(self):
        o = self.overview(date_from="2026-01-01", date_to="2026-11-16")
        self.assertEqual(o["period"]["granularity"], "week")
        self.assertEqual((o["trend"][0]["date"], o["trend"][0]["end"]), ("2026-01-01", "2026-01-07"))
        self.assertEqual(o["trend"][-1]["end"], "2026-11-16")
        self.assertEqual(sum(p[s] for p in o["trend"] for s in ("spin", "whatsapp", "wedding", "wedding_planner")),
                         o["new_in_period"]["all"])
        self.assertEqual(self.overview(date_from="2026-08-19", date_to="2026-11-16")["period"]["granularity"], "day")   # 90 days

    def test_unfiltered_numbers_ignore_the_period(self):
        a = self.overview(date_from="2026-11-15", date_to="2026-11-15")
        b = self.overview(date_from="2026-01-01", date_to="2026-03-31")
        for key in ("upcoming_30_days", "upcoming_by_month", "needs_attention", "this_week", "last_week"):
            self.assertEqual(a[key], b[key], key)
        self.assertEqual(a["needs_attention"], {"not_shared": 3, "over_budget": 2, "total": 5})
        self.assertEqual(a["upcoming_30_days"]["wedding"], 2)            # 26 Nov and 1 Dec: within 30 days of today
        self.assertEqual(a["this_week"]["whatsapp"], 2)                  # 10-16 Nov, the original meaning

    def test_vendor_sees_only_own_leads_in_the_period(self):
        phodeo = self.overview("phodeo", date_from="2026-11-01", date_to="2026-11-16")
        rj = self.overview("rj", date_from="2026-11-01", date_to="2026-11-16")
        self.assertEqual(phodeo["new_in_period"]["wedding"], 1)          # Bangalore (area rules)
        self.assertEqual(rj["new_in_period"]["wedding"], 1)              # leads shared: RJ (5 Nov)
        self.assertEqual(rj["totals"]["wedding"], 2)                     # + October
        self.assertEqual(phodeo["needs_attention"]["not_shared"], 1)
        admin = self.overview(date_from="2026-11-01", date_to="2026-11-16")
        self.assertEqual(admin["new_in_period"]["wedding"], 2)

    # ── The lists the Dashboard also calls: optional period, no filter by default ──
    def list_keys(self, url, key="lead_key", **params) -> set[str]:
        resp = self.client.get(f"{API}{url}", params={"page_size": 200, **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        return {str(i[key]) for i in resp.json()["items"]}

    def test_list_endpoints_take_an_optional_period(self):
        # No dates: no filter (the list pages are unchanged)
        self.assertEqual(self.list_keys("/whatsapp-leads"), {"WA_1530", "WA_1600", "WA_NODATE"})
        # IST day boundaries on the arrival column
        self.assertEqual(self.list_keys("/whatsapp-leads", date_from="2026-11-15", date_to="2026-11-15"), {"WA_1530"})
        self.assertEqual(self.list_keys("/whatsapp-leads", date_from="2026-11-16"), {"WA_1600"})
        self.assertEqual(self.list_keys("/wedding-leads", date_from="2026-11-01", date_to="2026-11-16"), {"WD_10", "WD_05"})
        self.assertEqual(self.list_keys("/wedding-plans", key="plan_id", date_from="2026-11-14", date_to="2026-11-14"),
                         {"WP_14"})                                       # updated_in_sheet_at
        self.assertEqual(self.list_keys("/wedding-plans", key="plan_id", date_from="2026-01-01", date_to="2026-01-01"),
                         set())                                           # not first_seen_at
        # Spin: workflow created_at (PostgreSQL) and CRM-added leads, together
        everything = self.list_keys("/spin-leads", key="id")
        self.assertEqual(len(everything), 2)                             # workflow 101 + the added lead
        added = next(k for k in everything if k.startswith("crm-"))
        self.assertEqual(self.list_keys("/spin-leads", key="id", date_from="2026-11-01", date_to="2026-11-01"), {"101"})
        self.assertEqual(self.list_keys("/spin-leads", key="id", date_from="2026-11-12", date_to="2026-11-12"), {added})
        # The same validation as the dashboard
        resp = self.client.get(f"{API}/wedding-leads", params={"date_from": "2026-11-17"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("can't be in the future", resp.json()["detail"])

    def test_validation_errors(self):
        cases = {
            ("2026-11-16", "2026-11-01"): "date_from must be on or before date_to",
            ("2025-11-01", "2026-11-16"): "at most 366 days",
            ("2026-11-10", "2026-11-17"): "can't be in the future",
            ("16-11-2026", None): "YYYY-MM-DD",
            ("2026-02-30", None): "YYYY-MM-DD",
        }
        for (start, end), message in cases.items():
            params = {"date_from": start, **({"date_to": end} if end else {})}
            resp = self.client.get(f"{API}/analytics/overview", params=params)
            self.assertEqual(resp.status_code, 400, params)
            self.assertIn(message, resp.json()["detail"])
        self.assertEqual(self.client.get(f"{API}/analytics/overview",
                                         params={"date_from": "2025-11-16", "date_to": "2026-11-16"}).status_code, 200)   # 366


if __name__ == "__main__":
    unittest.main()
