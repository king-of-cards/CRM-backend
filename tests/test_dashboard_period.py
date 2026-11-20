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
from app.models.hot_lead import CrmHotLead
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
    {"key": "501", "created": datetime(2026, 11, 16, 2, 0), "occasion_type": "Wedding", "occasion_date": "2026-11-25",
     "city": "Bangalore · 560043"},
    {"key": "502", "created": datetime(2026, 11, 5, 4, 30), "occasion_type": "Birthday", "occasion_date": None,
     "pincode": "500084"},
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
                                           "all_time": False,
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
        self.assertEqual(default["not_filtered"], [])                    # every number follows the period

    def test_range_previous_period_and_change(self):
        o = self.overview(date_from="2026-11-10", date_to="2026-11-16")     # 7 days; previous 3-9 Nov
        self.assertEqual(o["period"]["previous"], {"date_from": "2026-11-03", "date_to": "2026-11-09"})
        self.assertEqual(o["new_in_period"], {"spin": 2, "wedding_gifts": 0, "whatsapp": 2, "wedding": 1, "wedding_planner": 1, "all": 6})
        self.assertEqual(o["previous_period"], {"spin": 1, "wedding_gifts": 0, "whatsapp": 0, "wedding": 1, "wedding_planner": 0, "all": 2})
        self.assertEqual(o["change_pct"]["all"], 200)                    # 6 vs 2
        self.assertEqual(o["change_pct"]["wedding"], 0)
        self.assertIsNone(o["change_pct"]["whatsapp"])                   # nothing to compare with
        self.assertEqual(sum(p[s] for p in o["trend"] for s in ("spin", "whatsapp", "wedding", "wedding_planner")), 6)
        mix = {m["source"]: (m["count"], m["pct"]) for m in o["source_mix"]}
        self.assertEqual(mix["spin"], (2, 33))
        # Period-filtered numbers
        self.assertEqual({k: v for k, v in o["sharing"].items() if k != "by_source"}, {"shareable": 2, "shared": 0, "not_shared": 2, "rate": 0})
        self.assertEqual(o["sharing"]["by_source"], {"wedding": {"shareable": 1, "not_shared": 1, "shared": 0},
                                                     "wedding_planner": {"shareable": 1, "not_shared": 1, "shared": 0}})
        self.assertEqual(o["photography_budget"]["count"], 1)
        self.assertEqual(o["budget_fit"], {"over": 1})
        self.assertEqual([c["city"] for c in o["top_cities"]], ["Bengaluru", "Udaipur"])
        self.assertEqual(o["top_cities_sources"], ["spin", "wedding_gifts", "wedding", "wedding_planner"])   # default
        bengaluru = next(city for city in o["top_cities"] if city["city"] == "Bengaluru")
        self.assertEqual(bengaluru["by_source"]["spin"], 1)
        hyderabad = next(city for city in self.overview()["top_cities"] if city["city"] == "Hyderabad")
        self.assertEqual(hyderabad["by_source"]["spin"], 1)
        self.assertEqual(o["top_cities_no_city_by_source"]["spin"], 1)

    def test_totals_are_the_leads_that_arrived_in_the_period(self):
        o = self.overview(date_from="2026-11-01", date_to="2026-11-10")
        self.assertEqual(o["totals"], {"spin": 1, "wedding_gifts": 0, "whatsapp": 0, "wedding": 2, "wedding_planner": 0, "all": 3})
        self.assertEqual(o["totals"], o["new_in_period"])
        # Default period (18 Oct - 16 Nov): October, September and the lead with no date are left out
        default = self.overview()
        self.assertEqual(default["totals"], {"spin": 3, "wedding_gifts": 0, "whatsapp": 2, "wedding": 2, "wedding_planner": 1, "all": 8})

    def test_removed_kpis_and_kept_ones(self):
        for params in ({"date_from": "2026-11-10", "date_to": "2026-11-16"}, {"period": "all"}):
            o = self.overview(**params)
            for removed in ("average_per_day", "upcoming_30_days", "wedding_budget"):
                self.assertNotIn(removed, o, (params, removed))
            for kept in ("sharing", "needs_attention", "outcomes", "kpis", "upcoming_by_month", "photography_budget"):
                self.assertIn(kept, o, (params, kept))

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

    def test_upcoming_events_and_needs_attention_follow_the_period(self):
        nov = self.overview(date_from="2026-11-01", date_to="2026-11-16")
        # Events within 30 days of today, of leads that arrived in the period: the 10 Nov wedding
        # (event 26 Nov) and spin 501 (16 Nov, event 25 Nov); the October lead's 1 Dec event is left out
        months = {m["month"]: m for m in nov["upcoming_by_month"]}
        self.assertEqual((months["2026-11"]["wedding"], months["2026-11"]["spin"]), (1, 1))
        self.assertEqual(months["2026-12"]["wedding"], 0)                  # October lead: outside the period
        self.assertEqual(months["2027-01"]["wedding_planner"], 1)          # plan of 14 Nov
        # Not shared and over budget among the period's leads: 10 Nov wedding and the 14 Nov plan;
        # the 14 Nov plan is over budget (the September one isn't in the period)
        self.assertEqual({k: v for k, v in nov["needs_attention"].items() if k != "by_source"}, {"not_shared": 2, "over_budget": 1, "total": 3})
        self.assertEqual(nov["needs_attention"]["by_source"],
                         {"wedding": {"not_shared": 1, "over_budget": 0, "total": 1},
                          "wedding_planner": {"not_shared": 1, "over_budget": 1, "total": 2}})
        # A period with no leads: everything is zero
        empty = self.overview(date_from="2026-01-01", date_to="2026-03-31")
        self.assertEqual({k: v for k, v in empty["needs_attention"].items() if k != "by_source"}, {"not_shared": 0, "over_budget": 0, "total": 0})
        self.assertTrue(all(m[s] == 0 for m in empty["upcoming_by_month"]
                            for s in ("spin", "whatsapp", "wedding", "wedding_planner")))
        # this_week / last_week keep their original meaning (last 7 days vs the 7 before)
        self.assertEqual(nov["this_week"], empty["this_week"])
        self.assertEqual(nov["this_week"]["whatsapp"], 2)

    def test_vendor_sees_only_own_leads_in_the_period(self):
        phodeo = self.overview("phodeo", date_from="2026-11-01", date_to="2026-11-16")
        rj = self.overview("rj", date_from="2026-11-01", date_to="2026-11-16")
        self.assertEqual(phodeo["new_in_period"]["wedding"], 1)          # Bangalore (area rules)
        self.assertEqual(rj["new_in_period"]["wedding"], 1)              # leads shared: RJ (5 Nov)
        self.assertEqual(rj["totals"]["wedding"], 1)                     # the period's only (October is outside)
        self.assertEqual(phodeo["needs_attention"]["not_shared"], 1)
        admin = self.overview(date_from="2026-11-01", date_to="2026-11-16")
        self.assertEqual(admin["new_in_period"]["wedding"], 2)

    # ── All time ──
    def test_all_time(self):
        o = self.overview(period="all")
        # From the earliest arrival (the September plan) to today, monthly points, no comparison
        self.assertEqual(o["period"], {"date_from": "2026-09-01", "date_to": "2026-11-16", "days": 77, "all_time": True,
                                       "previous": None, "granularity": "month"})
        self.assertEqual([(p["date"], p["end"]) for p in o["trend"]],
                         [("2026-09-01", "2026-09-30"), ("2026-10-01", "2026-10-31"), ("2026-11-01", "2026-11-16")])
        self.assertIsNone(o["previous_period"])
        self.assertIsNone(o["change_pct"])
        # Every lead the viewer can see, also the one without an arrival date
        self.assertEqual(o["totals"], {"spin": 3, "wedding_gifts": 0, "whatsapp": 3, "wedding": 3, "wedding_planner": 2, "all": 11})
        dated = sum(p[s] for p in o["trend"] for s in ("spin", "whatsapp", "wedding", "wedding_planner"))
        self.assertEqual(dated, 10)                                  # the undated lead has no month
        self.assertEqual(o["not_filtered"], [])
        # The same as the lists without dates (they mean all time)
        for source, url in (("whatsapp", "/whatsapp-leads"), ("wedding", "/wedding-leads"), ("wedding_planner", "/wedding-plans")):
            listed = self.client.get(f"{API}{url}", params={"page_size": 10}).json()["total"]
            self.assertEqual(o["totals"][source], listed, source)
        # Needs attention and upcoming events over all leads
        self.assertEqual({k: v for k, v in o["needs_attention"].items() if k != "by_source"}, {"not_shared": 3, "over_budget": 2, "total": 5})
        # The per-source parts add up to the totals (drill-down)
        parts = o["needs_attention"]["by_source"].values()
        for key in ("not_shared", "over_budget", "total"):
            self.assertEqual(sum(p[key] for p in parts), o["needs_attention"][key], key)
        self.assertEqual(sum(p["not_shared"] for p in o["sharing"]["by_source"].values()), o["sharing"]["not_shared"])

    def test_all_time_vendor_scope_and_errors(self):
        rj = self.overview("rj", period="all")
        self.assertEqual(rj["totals"]["wedding"], 2)                  # 5 Nov + October
        self.assertEqual(rj["period"]["date_from"], "2026-10-01")     # RJ's earliest lead
        self.assertEqual([p["date"] for p in rj["trend"]], ["2026-10-01", "2026-11-01"])
        phodeo = self.overview("phodeo", period="all")
        self.assertEqual(phodeo["totals"]["wedding"], 1)
        self.assertEqual(self.overview(period="ALL")["period"]["all_time"], True)    # any case
        for params, message in (({"period": "all", "date_from": "2026-11-01"}, "not both"),
                                ({"period": "all", "date_to": "2026-11-16"}, "not both"),
                                ({"period": "last_week"}, 'period must be "all"')):
            resp = self.client.get(f"{API}/analytics/overview", params=params)
            self.assertEqual(resp.status_code, 400, params)
            self.assertIn(message, resp.json()["detail"])

    # ── CRM outcomes ──
    def add_outcomes(self):
        from sqlmodel import select
        from app.models.lead_meta import LeadMeta
        with Session(self.engine) as s:
            crm_key = s.exec(select(CrmSpinLead)).one().lead_key
            s.add_all([
                LeadMeta(source="whatsapp", lead_key="WA_1600", update_status="converted", quoted_amount=40000,
                         updated_at=NOW),
                LeadMeta(source="wedding", lead_key="WD_10", update_status="converted", quoted_amount=50000,
                         conversion_amount=45000, updated_at=NOW),
                LeadMeta(source="wedding", lead_key="WD_OCT", update_status="interested", conversion_amount=12000,
                         updated_at=NOW),   # October
                LeadMeta(source="wedding_planner", lead_key="WP_14", update_status="not_interested",
                         quoted_amount=1_000_000, updated_at=NOW),
                LeadMeta(source="spin", lead_key="501", update_status="interested", updated_at=NOW),
                LeadMeta(source="spin", lead_key=crm_key, update_status="converted", updated_at=NOW),
            ])
            s.commit()

    def test_outcomes_per_source(self):
        self.add_outcomes()
        with Session(self.engine) as s:
            s.add(CrmHotLead(source="wedding", lead_key="WD_10", hot_marked_by=self.users["admin"].id,
                             hot_marked_at=NOW))
            s.add(CrmHotLead(source="wedding", lead_key="WD_OCT", hot_marked_by=self.users["admin"].id,
                             hot_marked_at=NOW))
            s.commit()
        def k(total, interested=0, not_interested=0, quoted=(0, 0), converted=0, conversion=(0, 0),
              converted_amount=(0, 0), hot_count=0):
            return {"leads": total, "total": total, "interested": interested, "not_interested": not_interested,
                    "quoted_count": quoted[0], "quoted_total": quoted[1], "converted": converted,
                    "converted_pct": round(converted / total * 100, 1) if total else None,
                    "conversion_count": conversion[0], "conversion_total": conversion[1],
                    "conversion_amount_count": converted_amount[0], "conversion_amount_total": converted_amount[1],
                    "quotes_count": quoted[0], "hot_count": hot_count}

        overview = self.overview(date_from="2026-11-10", date_to="2026-11-16")
        o = overview["outcomes"]
        self.assertEqual(o["whatsapp"], k(2, quoted=(1, 40000), converted=1))
        self.assertEqual(o["wedding"], k(1, quoted=(1, 50000), converted=1, conversion=(1, 45000),
                                             converted_amount=(1, 45000), hot_count=1))
        self.assertEqual(o["wedding_planner"], k(1, not_interested=1, quoted=(1, 1_000_000)))
        self.assertEqual(o["spin"], k(2, interested=1, converted=1))
        self.assertEqual(o["all"], k(6, interested=1, not_interested=1, quoted=(3, 1_090_000), converted=3,
                                     conversion=(1, 45000), converted_amount=(1, 45000), hot_count=1))
        # The top-level KPIs: every source combined (= outcomes.all without "leads")
        self.assertEqual(overview["kpis"], {key: v for key, v in o["all"].items() if key != "leads"})
        # A period with no leads
        empty = self.overview(date_from="2026-01-01", date_to="2026-01-31")
        self.assertEqual(empty["outcomes"]["all"], k(0))
        self.assertEqual(empty["kpis"]["converted_pct"], None)
        # All time: the October wedding lead's interest counts too
        everything = self.overview(period="all")["outcomes"]
        self.assertEqual(everything["wedding"]["interested"], 1)
        self.assertEqual(everything["wedding"]["hot_count"], 2)
        self.assertEqual((everything["wedding"]["conversion_count"], everything["wedding"]["conversion_total"]),
                         (2, 57000))
        self.assertEqual((everything["wedding"]["conversion_amount_count"],
                          everything["wedding"]["conversion_amount_total"]), (1, 45000))
        self.assertEqual(everything["all"]["leads"], 11)
        self.assertEqual(everything["all"]["converted_pct"], round(3 / 11 * 100, 1))

    def test_outcomes_equal_the_filtered_list_totals(self):
        self.add_outcomes()
        lists = {"whatsapp": "/whatsapp-leads", "wedding": "/wedding-leads", "wedding_planner": "/wedding-plans"}
        filters = {"interested": {"update_status": "interested"}, "not_interested": {"update_status": "not_interested"},
                   "converted": {"update_status": "converted"}, "quoted_count": {"quote": "has"},
                   "conversion_count": {"conversion": "has"}}
        # Spin is left out here: this test fakes the dashboard's spin rows separately from the spin
        # list's (test_list_filters checks /spin-leads/stats against the spin list)
        stats_url = {"whatsapp": "/whatsapp-leads/stats", "wedding": "/wedding-leads/stats",
                     "wedding_planner": "/wedding-plans/stats"}
        for who in ("admin", "phodeo", "rj"):
            for dates in ({"date_from": "2026-11-01", "date_to": "2026-11-16"}, {}):
                params = dates or {"period": "all"}
                outcomes = self.overview(who, **params)["outcomes"]
                with self.logged_in(who):
                    for source, url in lists.items():
                        for field, flt in filters.items():
                            listed = self.client.get(f"{API}{url}", params={"page_size": 10, **dates, **flt}).json()["total"]
                            self.assertEqual(outcomes[source][field], listed, (who, dates, source, field))
                    # Each list's stats KPIs (same dates) equal the dashboard's outcomes for that source
                    for source, url in stats_url.items():
                        stats = self.client.get(f"{API}{url}", params=dates)
                        self.assertEqual(stats.status_code, 200, stats.text)
                        expected = {k: v for k, v in outcomes[source].items()
                                    if k not in ("leads", "quotes_count", "hot_count", "conversion_amount_count",
                                                 "conversion_amount_total")}
                        self.assertEqual(stats.json()["kpis"], expected, (who, dates, source))

    def test_outcomes_vendor_scope(self):
        self.add_outcomes()
        phodeo = self.overview("phodeo", date_from="2026-11-01", date_to="2026-11-16")["outcomes"]
        self.assertEqual((phodeo["wedding"]["converted"], phodeo["wedding"]["quoted_total"]), (1, 50000))   # Bangalore lead
        self.assertEqual(phodeo["whatsapp"]["leads"], 0)
        rj = self.overview("rj", period="all")["outcomes"]
        self.assertEqual((rj["wedding"]["leads"], rj["wedding"]["interested"], rj["wedding"]["converted"]), (2, 1, 0))

    # ── The lists the Dashboard also calls: optional period, no filter by default ──
    def list_keys(self, url, key="lead_key", **params) -> set[str]:
        resp = self.client.get(f"{API}{url}", params={"page_size": 100, **params})
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
