"""Consistent filters on the four lead lists, their exports and their stats.

"Today" is fixed at 16 Nov 2026 (India time). In-memory SQLite and a fake workflow database
(see test_crm_leads_api.ApiTestBase). Run from the project root:

    python -m unittest discover -s tests -t . -v

Fixtures (vendors RJ, Phodeo, OHP):
  International  WA1 UAE, 10 Nov, event 25 Nov (area rules: OHP)
                 WA2 United Kingdom, 1 Oct, event 1 Mar 2027; manual RJ, interested, quote 60000, converted
                 WA3 added, UAE, 15 Nov, uploaded unassigned
  Wedding        WD1 Asha Rao, "Shamirpet, Hyderabad", Wedding, 50000, 10 Nov, event 30 Nov (OHP)
                 WD2 Ravi, Bangalore, Birthday, 150000, shared RJ, repeat, 1 Nov, event 10 Jan; not interested, quote 20000
                 WD3 imported, Mysore, 14 Nov (OHP)
  Planner        WP1 Meera, Udaipur, 300 guests, 2,500,000, fits, pdf, shared Phodeo, 12 Nov, wedding 5 Dec
                 WP2 Dev, Jaipur, 80 guests, 600,000, over budget, whatsapp, 20 Oct, wedding 1 Feb (OHP)
                 WP3 added, Hyderabad, 16 Nov; manual Phodeo, converted
"""
import io
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from openpyxl import load_workbook
from sqlmodel import Session

from app.models.lead_meta import LeadMeta
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.services import periods
from tests.test_crm_leads_api import API, NOW, ApiTestBase

TODAY = date(2026, 11, 16)
LIST = {"whatsapp": "/whatsapp-leads", "wedding": "/wedding-leads", "wedding_planner": "/wedding-plans"}
KEY = {"whatsapp": "lead_key", "wedding": "lead_key", "wedding_planner": "plan_id"}


def utc(*args) -> datetime:
    return datetime(*args, 5, 0, tzinfo=timezone.utc)


class SheetListFiltersTest(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.today = mock.patch.object(periods, "today_ist", lambda: TODAY)
        self.today.start()
        base = {"row_hash": "", "first_seen_at": NOW, "updated_at": NOW}
        v = self.vendor_ids
        with Session(self.engine) as s:
            s.add_all([
                WhatsAppLead(sheet_key="WA1", origin="sheet", country="UAE", phone="971500000001", phone_e164="+971500000001",
                             submitted_at=utc(2026, 11, 10), event_date=date(2026, 11, 25), **base),
                WhatsAppLead(sheet_key="WA2", origin="sheet", country="United Kingdom", phone="447911000002",
                             phone_e164="+447911000002", submitted_at=utc(2026, 10, 1), event_date=date(2027, 3, 1), **base),
                WhatsAppLead(sheet_key="WA3", origin="added", country="UAE", phone="971500000003", phone_e164="+971500000003",
                             submitted_at=utc(2026, 11, 15), **base),
                WeddingLead(sheet_key="WD1", origin="sheet", name="Asha Rao", email="asha@x.com", city="Shamirpet, Hyderabad",
                            occasion="Wedding", budget_amount=50000, phone="919845100001", phone_e164="+919845100001",
                            submitted_at=utc(2026, 11, 10), event_date=date(2026, 11, 30), **base),
                WeddingLead(sheet_key="WD2", origin="sheet", name="Ravi", email="ravi@y.com", city="Bangalore", occasion="Birthday",
                            budget_amount=150000, shared_with="RJ", is_repeat=True, phone="919845100002",
                            phone_e164="+919845100002", submitted_at=utc(2026, 11, 1), event_date=date(2027, 1, 10), **base),
                WeddingLead(sheet_key="WD3", origin="imported", name="Imported One", city="Mysore", phone="919845100003",
                            phone_e164="+919845100003", submitted_at=utc(2026, 11, 14), **base),
                WeddingPlan(plan_id="WP1", origin="sheet", name="Meera", city="Udaipur", total_guests=300,
                            total_budget=2_500_000, fits_budget=True, last_step="pdf", shared_with="Phodeo",
                            phone="919845200001", phone_e164="+919845200001", updated_in_sheet_at=utc(2026, 11, 12),
                            wedding_date=date(2026, 12, 5), **base),
                WeddingPlan(plan_id="WP2", origin="sheet", name="Dev", city="Jaipur", total_guests=80, total_budget=600_000,
                            fits_budget=False, last_step="whatsapp", phone="919845200002", phone_e164="+919845200002",
                            updated_in_sheet_at=utc(2026, 10, 20), wedding_date=date(2027, 2, 1), **base),
                WeddingPlan(plan_id="WP3", origin="added", name="Added Plan", city="Hyderabad", phone="919845200003",
                            phone_e164="+919845200003", updated_in_sheet_at=utc(2026, 11, 16), **base),
                LeadMeta(source="whatsapp", lead_key="WA2", vendor_id=v["rj"], interest="interested", quoted_amount=60000,
                         converted=True, updated_at=NOW),
                LeadMeta(source="whatsapp", lead_key="WA3", auto_assign=False, updated_at=NOW),
                LeadMeta(source="wedding", lead_key="WD2", interest="not_interested", quoted_amount=20000, updated_at=NOW),
                LeadMeta(source="wedding_planner", lead_key="WP3", vendor_id=v["phodeo"], converted=True, updated_at=NOW),
            ])
            s.commit()

    def tearDown(self):
        self.today.stop()
        super().tearDown()

    def keys(self, source, who="admin", **params) -> set[str]:
        with self.logged_in(who):
            resp = self.client.get(f"{API}{LIST[source]}", params={"page_size": 200, **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["total"], len(body["items"]))
        return {i[KEY[source]] for i in body["items"]}

    def check(self, source, expected: set[str], who="admin", **params):
        self.assertEqual(self.keys(source, who, **params), expected, (source, who, params))

    # ── International ──
    def test_international_filters(self):
        c = lambda expected, **p: self.check("whatsapp", expected, **p)  # noqa: E731
        c({"WA1", "WA2", "WA3"})
        c({"WA2"}, search="+44 7911")                       # phone digits
        c({"WA1", "WA3"}, search="uae")                     # country text
        c({"WA1", "WA3"}, country="UAE")
        c({"WA1", "WA3"}, date_from="2026-11-01", date_to="2026-11-16")
        c({"WA2"}, event_from="2027-01-01", event_to="2027-12-31")
        c({"WA1"}, upcoming_days=30)                        # 16 Nov - 16 Dec
        c({"WA1"}, vendor_id=self.vendor_ids["ohp"])
        c({"WA1", "WA2"}, vendor_id=[self.vendor_ids["ohp"], self.vendor_ids["rj"]])
        c({"WA3"}, unassigned="true")
        c({"WA2", "WA3"}, vendor_id=self.vendor_ids["rj"], unassigned="true")
        c({"WA2"}, interest="interested")
        c({"WA1", "WA3"}, interest="not_set")
        c({"WA1", "WA2", "WA3"}, interest=["interested", "not_set"])
        c({"WA2"}, converted="yes")
        c({"WA1", "WA3"}, converted="no")
        c({"WA2"}, quote="has")
        c({"WA1", "WA3"}, quote="none")
        c({"WA2"}, quote="gt:50000")
        c(set(), quote="lt:50000")
        c({"WA1", "WA2"}, origin="sheet")
        c({"WA3"}, origin="added")
        c({"WA1"}, country="UAE", origin="sheet")           # combination
        c({"WA1"}, interest="not_set", date_from="2026-11-01", date_to="2026-11-12")

    # ── Wedding ──
    def test_wedding_filters(self):
        c = lambda expected, **p: self.check("wedding", expected, **p)  # noqa: E731
        c({"WD1"}, search="asha")
        c({"WD2"}, search="y.com")                          # email
        c({"WD2"}, search="98451 00002")
        c({"WD1"}, city="hyderabad")                        # contains, any case
        c({"WD2"}, budget_min=100000)
        c({"WD1"}, budget_max=60000)
        c({"WD1", "WD2"}, budget_min=50000, budget_max=150000)
        c({"WD2"}, occasion="Birthday")
        c({"WD1", "WD3"}, shared_with="__none__")
        c({"WD1", "WD3"}, hide_repeats="true")
        c({"WD1"}, event_from="2026-11-01", event_to="2026-12-31")
        c({"WD2"}, interest="not_interested")
        c({"WD2"}, quote="lt:30000")
        c({"WD3"}, origin="imported")
        c({"WD2"}, vendor_id=self.vendor_ids["rj"])         # leads shared: RJ
        c({"WD1", "WD3"}, vendor_id=self.vendor_ids["ohp"]) # area rules
        c({"WD1"}, vendor_id=self.vendor_ids["ohp"], city="hyderabad", origin="sheet")

    # ── Wedding planner ──
    def test_planner_filters(self):
        c = lambda expected, **p: self.check("wedding_planner", expected, **p)  # noqa: E731
        c({"WP1"}, search="meera")
        c({"WP3"}, city="hyder")
        c({"WP1"}, guests_min=100)
        c({"WP2"}, guests_max=100)
        c({"WP2"}, budget_min=500_000, budget_max=1_000_000)
        c({"WP2"}, fits_budget="false")
        c({"WP2"}, last_step="WhatsApp")
        c({"WP1"}, shared_with="Phodeo")
        c({"WP1", "WP3"}, vendor_id=self.vendor_ids["phodeo"])     # sheet + manual
        c({"WP3"}, converted="yes")
        c({"WP1"}, event_from="2026-12-01", event_to="2026-12-31")  # wedding date
        c({"WP3"}, origin="added")
        c({"WP1", "WP3"}, date_from="2026-11-10", date_to="2026-11-16")
        c({"WP1"}, date_from="2026-11-10", date_to="2026-11-16", guests_min=1)

    # ── Vendor scope ──
    def test_vendor_filters_are_ignored_for_vendors(self):
        rj = self.vendor_ids["rj"]
        self.check("wedding_planner", {"WP1", "WP3"}, who="phodeo")
        self.check("wedding_planner", {"WP1", "WP3"}, who="phodeo", vendor_id=rj)     # ignored
        self.check("wedding_planner", {"WP1", "WP3"}, who="phodeo", unassigned="true")
        self.check("wedding", {"WD2"}, who="rj")
        self.check("wedding", {"WD2"}, who="rj", vendor_id=self.vendor_ids["ohp"])
        self.check("wedding", set(), who="rj", city="hyderabad")                      # own leads only
        self.check("whatsapp", {"WA1"}, who="ohp", unassigned="true")                 # WA3 stays hidden

    # ── list = export = stats ──
    def test_export_and_stats_match_the_list(self):
        combos = {
            "whatsapp": [{}, {"country": "UAE"}, {"interest": "not_set", "origin": "sheet"}, {"quote": "has"},
                         {"upcoming_days": 30}, {"vendor_id": self.vendor_ids["ohp"], "date_from": "2026-11-01"}],
            "wedding": [{}, {"city": "hyderabad"}, {"budget_min": 10000, "hide_repeats": "true"},
                        {"interest": ["not_interested", "not_set"]}, {"search": "ravi"}],
            "wedding_planner": [{}, {"guests_min": 50}, {"fits_budget": "true"}, {"converted": "yes"},
                                {"event_from": "2026-12-01", "event_to": "2027-12-31", "sort": "name", "order": "asc"}],
        }
        stats_url = {"whatsapp": "/whatsapp-leads/stats", "wedding": "/wedding-leads/stats",
                     "wedding_planner": "/wedding-plans/stats"}
        identity = {"whatsapp": "Lead", "wedding": "Contact", "wedding_planner": "Contact"}
        for who in ("admin", "phodeo", "ohp"):
            with self.logged_in(who):
                for source, cases in combos.items():
                    for params in cases:
                        with self.subTest(who=who, source=source, params=params):
                            listed = self.client.get(f"{API}{LIST[source]}", params={"page_size": 200, **params}).json()
                            stats = self.client.get(f"{API}{stats_url[source]}", params=params).json()
                            resp = self.client.get(f"{API}/crm-leads/{source}/export", params=params)
                            self.assertEqual(resp.status_code, 200, resp.text)
                            ws = load_workbook(io.BytesIO(resp.content)).active
                            header = [c.value for c in ws[1]]
                            exported = [r[header.index(identity[source])] for r in ws.iter_rows(min_row=2, values_only=True)]
                            self.assertEqual(exported, [i["phone_display"] for i in listed["items"]])   # same rows, same order
                            self.assertEqual(stats["total"], listed["total"])

    # ── Validation ──
    def test_validation_errors(self):
        cases = [
            ("whatsapp", {"quote": "gt:abc"}, "quote must be"),
            ("whatsapp", {"quote": "eq:5"}, "quote must be"),
            ("whatsapp", {"event_from": "2026-12-31", "event_to": "2026-12-01"}, "event_from must be on or before"),
            ("whatsapp", {"upcoming_days": 30, "event_from": "2026-12-01"}, "either upcoming_days"),
            ("whatsapp", {"upcoming_days": 0}, "between 1 and 366"),
            ("whatsapp", {"event_from": "20-12-2026"}, "YYYY-MM-DD"),
            ("whatsapp", {"origin": "workflow"}, "doesn't exist for International leads"),
            ("wedding", {"interest": "maybe"}, "interest must be one of"),
            ("wedding", {"budget_min": 10, "budget_max": 5}, "budget_min must not be more than budget_max"),
            ("wedding_planner", {"guests_min": 10, "guests_max": 5}, "guests_min must not be more than guests_max"),
            ("wedding_planner", {"date_from": "2026-11-17"}, "can't be in the future"),
        ]
        for source, params, message in cases:
            resp = self.client.get(f"{API}{LIST[source]}", params=params)
            self.assertEqual(resp.status_code, 400, (source, params, resp.text))
            self.assertIn(message, resp.json()["detail"])
            export = self.client.get(f"{API}/crm-leads/{source}/export", params=params)
            self.assertEqual(export.status_code, 400, (source, params))               # same rules on the export


if __name__ == "__main__":
    unittest.main()
