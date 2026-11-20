"""Top cities per flow: the `sources` parameter of GET /analytics/overview and GET /analytics/vendor-to-koc.

"Today" is fixed at 16 Nov 2026 (India time). In-memory SQLite (see test_vendor_to_koc), Spin mocked. Run:

    python -m unittest discover -s tests -t . -v
"""
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from fastapi import HTTPException
from sqlmodel import Session

from tests.test_vendor_to_koc import VendorToKocTestCase

from app.api.v1.endpoints import analytics  # noqa: E402
from app.core import response_cache  # noqa: E402
from app.models.vendor_to_koc import VendorToKocLead  # noqa: E402
from app.models.wedding_lead import WeddingLead  # noqa: E402
from app.models.wedding_plan import WeddingPlan  # noqa: E402
from app.models.whatsapp_lead import WhatsAppLead  # noqa: E402
from app.services import lead_sources, periods  # noqa: E402

TODAY = date(2026, 11, 16)
OVERVIEW = "/api/v1/analytics/overview"
VTK = "/api/v1/analytics/vendor-to-koc"
NOW = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)
SPIN_ROWS = [
    {"key": "701", "created": datetime(2026, 11, 14, 5, 0), "city": "Chennai"},
    {"key": "702", "created": datetime(2026, 11, 15, 5, 0), "city": "Chennai"},
]


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TopCitiesSourcesTests(VendorToKocTestCase):
    """Spin: Chennai x2. Wedding: Mysuru. Planner: Udaipur. International: UAE (no city).
    Vendor -> KOC: Phodeo Bengaluru x2, Other vendor Pune x3 (all in the default period)."""

    def setUp(self):
        super().setUp()
        self.today = mock.patch.object(periods, "today_ist", lambda: TODAY)
        self.spin = mock.patch.object(analytics, "_workflow_spin_rows", lambda: [dict(r) for r in SPIN_ROWS])
        self.today.start()
        self.spin.start()
        response_cache.clear()
        sheet = {"origin": "sheet", "row_hash": "", "first_seen_at": NOW, "updated_at": NOW}
        with Session(self.engine) as s:
            s.add_all([
                WeddingLead(sheet_key="WD_1", name="W", city="Mysore", phone="919845100001",
                            phone_e164="+919845100001", submitted_at=utc(2026, 11, 10, 5, 0), **sheet),
                WeddingPlan(plan_id="WP_1", name="P", city="Udaipur", phone="919845200001",
                            phone_e164="+919845200001", updated_in_sheet_at=utc(2026, 11, 11, 5, 0), **sheet),
                WhatsAppLead(sheet_key="WA_1", country="UAE", phone="971500000001", phone_e164="+971500000001",
                             submitted_at=utc(2026, 11, 12, 5, 0), **sheet),
            ])
            leads = [(self.phodeo_id, "Bengaluru")] * 2 + [(self.other_id, "Pune")] * 3
            for i, (vendor, city) in enumerate(leads, start=1):
                s.add(VendorToKocLead(vendor_id=vendor, phone=f"90000002{i:02d}", name=f"V{i}", city=city,
                                      source="manual", created_at=utc(2026, 11, 13, 5, 0),
                                      updated_at=utc(2026, 11, 13, 5, 0), created_by=self.ids["admin"]))
            s.commit()

    def tearDown(self):
        self.spin.stop()
        self.today.stop()
        response_cache.clear()
        super().tearDown()

    def get(self, url, headers=None, **params):
        resp = self.client.get(url, headers=headers or self.admin, params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    @staticmethod
    def cities(o) -> dict:
        return {c["city"]: c["count"] for c in o["top_cities"]}

    # ── /analytics/overview ──
    def test_overview_default_is_spin_wedding_planner(self):
        o = self.get(OVERVIEW)
        self.assertEqual(o["top_cities_sources"], ["spin", "wedding_gifts", "wedding", "wedding_planner"])
        self.assertEqual(self.cities(o), {"Chennai": 2, "Mysuru": 1, "Udaipur": 1})
        self.assertEqual(set(o["top_cities_no_city_by_source"]), {"spin", "wedding_gifts", "wedding", "wedding_planner"})

    def test_overview_custom_subset(self):
        o = self.get(OVERVIEW, sources="wedding,vendor_to_koc")
        self.assertEqual(o["top_cities_sources"], ["wedding", "vendor_to_koc"])
        self.assertEqual(self.cities(o), {"Pune": 3, "Bengaluru": 2, "Mysuru": 1})
        self.assertEqual(o["top_cities"][0], {"city": "Pune", "count": 3, "by_source": {"vendor_to_koc": 3}})
        # Only Top cities follows the selection: the rest of the overview is unchanged
        self.assertEqual(o["new_in_period"], self.get(OVERVIEW)["new_in_period"])

    def test_sources_without_a_city_give_an_empty_aggregate(self):
        for value in ("whatsapp", "dhruva", "whatsapp,dhruva"):
            o = self.get(OVERVIEW, sources=value)
            self.assertEqual((o["top_cities"], o["top_cities_sources"], o["top_cities_no_city"],
                              o["top_cities_no_city_by_source"]), ([], [], 0, {}), value)

    def test_spaces_and_case_are_accepted(self):
        o = self.get(OVERVIEW, sources=" Spin , WEDDING_PLANNER ,")
        self.assertEqual(o["top_cities_sources"], ["spin", "wedding_planner"])
        self.assertEqual(self.cities(o), {"Chennai": 2, "Udaipur": 1})

    def test_invalid_sources_are_rejected_on_both_endpoints(self):
        for url in (OVERVIEW, VTK):
            for headers in (self.admin, self.phodeo):
                bad = self.client.get(url, headers=headers, params={"sources": "wedding,instagram"})
                self.assertEqual(bad.status_code, 400, bad.text)
                self.assertIn("Unknown source(s): instagram. Allowed: spin, wedding_gifts, whatsapp", bad.json()["detail"])
                empty = self.client.get(url, headers=headers, params={"sources": " , "})
                self.assertEqual(empty.status_code, 400, empty.text)

    def test_spin_unavailable_drops_out_of_the_sources(self):
        with mock.patch.object(analytics, "_workflow_spin_rows", lambda: None), \
                mock.patch.object(analytics, "_spin_rows", lambda _db: None):
            o = self.get(OVERVIEW, sources="spin,wedding")
        self.assertEqual(o["top_cities_sources"], ["wedding"])
        self.assertEqual(self.cities(o), {"Mysuru": 1})

    def test_vendor_overview_is_scoped_to_its_own_leads(self):
        o = self.get(OVERVIEW, headers=self.phodeo, sources="vendor_to_koc")
        self.assertEqual(o["top_cities_sources"], ["vendor_to_koc"])
        self.assertEqual(self.cities(o), {"Bengaluru": 2})                  # never the other vendor's Pune
        default = self.get(OVERVIEW, headers=self.phodeo)
        self.assertNotIn("Pune", self.cities(default))
        self.assertNotIn("Bengaluru", self.cities(default))

    # ── /analytics/vendor-to-koc ──
    def test_vtk_default_is_vendor_to_koc_only(self):
        o = self.get(VTK)
        self.assertEqual(o["top_cities_sources"], ["vendor_to_koc"])
        self.assertEqual(self.cities(o), {"Pune": 3, "Bengaluru": 2})
        self.assertEqual(o["top_cities_no_city_by_source"], {"vendor_to_koc": 0})

    def test_vtk_custom_subset(self):
        o = self.get(VTK, sources="spin,wedding_planner,whatsapp")
        self.assertEqual(o["top_cities_sources"], ["spin", "wedding_planner"])
        self.assertEqual(self.cities(o), {"Chennai": 2, "Udaipur": 1})

    def test_vtk_vendor_login_still_gets_no_top_cities(self):
        o = self.get(VTK, headers=self.phodeo, sources="spin,wedding,vendor_to_koc")
        self.assert_no_admin_fields(o)
        for key in ("top_cities", "top_cities_sources", "top_cities_no_city", "top_cities_no_city_by_source"):
            self.assertNotIn(key, o)
        self.assertEqual(o["received"], 2)                                  # its own leads only

    # ── Cache keys ──
    def test_each_selection_has_its_own_cache_entry(self):
        first = self.get(OVERVIEW, sources="wedding")
        second = self.get(OVERVIEW, sources="wedding_planner")
        self.assertNotEqual(first["top_cities"], second["top_cities"])
        vtk_first = self.get(VTK)
        vtk_second = self.get(VTK, sources="wedding")
        self.assertNotEqual(vtk_first["top_cities"], vtk_second["top_cities"])
        period = periods.default_period()
        self.assertNotEqual(analytics._cache_key("admin", period, ("wedding",)),
                            analytics._cache_key("admin", period, ("wedding_planner",)))
        # Order in the request doesn't matter: the same selection shares one entry
        self.assertEqual(analytics._cache_key("admin", period, analytics._parse_city_sources("wedding,spin", ())),
                         analytics._cache_key("admin", period, analytics._parse_city_sources("spin,wedding", ())))
        # warm_overview fills the entry a default request reads
        self.assertEqual(analytics._cache_key("admin", period),
                         analytics._cache_key("admin", period, analytics.OVERVIEW_CITY_DEFAULTS))


class ParseSourcesTests(unittest.TestCase):
    def test_default_and_explicit(self):
        self.assertEqual(lead_sources.parse_sources(None), set(lead_sources.LEAD_SOURCES))
        self.assertEqual(lead_sources.parse_sources(None, default=("spin",)), {"spin"})
        self.assertEqual(lead_sources.parse_sources("Dhruva, whatsapp"), {"dhruva", "whatsapp"})

    def test_errors(self):
        with self.assertRaises(HTTPException) as empty:
            lead_sources.parse_sources(",")
        self.assertEqual(empty.exception.status_code, 400)
        with self.assertRaises(HTTPException) as unknown:
            lead_sources.parse_sources("spin,foo,bar")
        self.assertEqual(unknown.exception.detail,
                         "Unknown source(s): bar, foo. Allowed: spin, wedding_gifts, whatsapp, wedding, wedding_planner, "
                         "vendor_to_koc, dhruva")


if __name__ == "__main__":
    unittest.main()
