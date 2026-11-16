"""GET /analytics/vendor-to-koc: the Vendor -> KOC dashboard.

"Today" is fixed at 16 Nov 2026 (India time). In-memory SQLite (see test_vendor_to_koc). Run:

    python -m unittest discover -s tests -t . -v
"""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from tests.test_vendor_to_koc import VendorToKocTestCase

from sqlmodel import Session  # noqa: E402

from app.core import response_cache  # noqa: E402
from app.models.vendor_to_koc import VendorToKocLead  # noqa: E402
from app.services import periods  # noqa: E402

TODAY = date(2026, 11, 16)
URL = "/api/v1/analytics/vendor-to-koc"
VENDOR_KEYS = {"generated_at", "today", "period", "received", "change_pct", "average_per_day", "inflow", "inflow_vendors",
               "not_filtered", "pending", "converted", "converted_pct", "purchased_with_us", "by_status", "funnel",
               "follow_ups_due", "next_follow_ups"}
ADMIN_ONLY_KEYS = {"previous_received", "per_vendor", "top_cities", "by_source", "latest"}


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class VendorDashboardTests(VendorToKocTestCase):
    """D1 Phodeo  23:30 IST 15 Nov  Bengaluru  bulk    pending          follow-up 10 Nov (due)
    D2 Phodeo  00:30 IST 16 Nov  Mysuru     manual  converted (+1 day) follow-up 1 Nov (closed)
    D3 Other   1 Oct             Pune       bulk    interested       follow-up 1 Dec
    D4 Other   10 Nov            Bengaluru  manual  will call back (+3 days) follow-up today (due)
    D5 Phodeo  5 Nov             Bengaluru  bulk    purchased with us (+2 days)"""

    def setUp(self):
        super().setUp()
        self.today = mock.patch.object(periods, "today_ist", lambda: TODAY)
        self.today.start()
        response_cache.clear()
        spec = {
            "D1": (self.phodeo_id, utc(2026, 11, 15, 18, 0), "Bengaluru", "bulk_upload", None, None, date(2026, 11, 10)),
            "D2": (self.phodeo_id, utc(2026, 11, 15, 19, 0), "Mysuru", "manual", "converted", 1, date(2026, 11, 1)),
            "D3": (self.other_id, utc(2026, 10, 1, 5, 0), "Pune", "bulk_upload", "interested", None, date(2026, 12, 1)),
            "D4": (self.other_id, utc(2026, 11, 10, 5, 0), "Bengaluru", "manual", "will_call_back", 3, TODAY),
            "D5": (self.phodeo_id, utc(2026, 11, 5, 5, 0), "Bengaluru", "bulk_upload", "purchased_with_us", 2, None),
        }
        self.ids_by_name = {}
        with Session(self.engine) as s:
            for i, (key, (vendor, created, city, source, status, days, follow)) in enumerate(spec.items(), start=1):
                lead = VendorToKocLead(
                    vendor_id=vendor, phone=f"90000001{i:02d}", name=key, city=city, source=source, created_at=created,
                    updated_at=created, created_by=self.ids["admin"], update_status=status, follow_up_date=follow,
                    status_updated_at=created + timedelta(days=days) if days else None,
                    status_updated_by=self.ids["admin"] if status else None,
                )
                s.add(lead)
                s.commit()
                self.ids_by_name[key] = lead.id

    def tearDown(self):
        self.today.stop()
        response_cache.clear()
        super().tearDown()

    def dash(self, headers=None, query=""):
        resp = self.client.get(f"{URL}?{query}", headers=headers or self.admin)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_admin_numbers_for_the_default_period(self):
        o = self.dash()                                         # 18 Oct - 16 Nov; previous 18 Sep - 17 Oct
        self.assertEqual((o["period"]["date_from"], o["period"]["date_to"]), ("2026-10-18", "2026-11-16"))
        self.assertEqual((o["received"], o["previous_received"], o["change_pct"]), (4, 1, 300))
        self.assertEqual(o["average_per_day"], {"period": 0.1, "previous": 0.0})
        self.assertEqual((o["pending"], o["converted"], o["converted_pct"], o["purchased_with_us"]), (1, 1, 25.0, 1))
        self.assertEqual({k: v for k, v in o["by_status"].items() if v},
                         {"pending": 1, "converted": 1, "will_call_back": 1, "purchased_with_us": 1})
        self.assertEqual(o["funnel"], {"received": 4, "updated": 3, "engaged": 2, "converted": 1})
        self.assertEqual(o["by_source"], {"manual": 2, "bulk_upload": 2})
        self.assertEqual(o["top_cities"], [{"value": "Bengaluru", "count": 3}, {"value": "Mysuru", "count": 1}])
        vendors = {v["vendor_name"]: v for v in o["per_vendor"]}
        self.assertEqual(vendors["Phodeo"], {"vendor_id": self.phodeo_id, "vendor_name": "Phodeo", "leads": 3,
                                             "interested": 0, "converted": 1, "pending": 1, "converted_pct": 33.3,
                                             "average_days_to_update": 1.5})
        self.assertEqual(vendors["Other Vendor"]["average_days_to_update"], 3.0)
        self.assertEqual([l["name"] for l in o["latest"]], ["D2", "D1", "D4", "D5"])          # newest first
        self.assertIn("update_status", o["latest"][0])                                       # admin fields
        # Follow-ups: open leads only (D2 converted, D5 purchased are closed); not limited to the period
        self.assertEqual(o["follow_ups_due"], 2)                                             # D1 (10 Nov), D4 (today)
        self.assertEqual([l["name"] for l in o["next_follow_ups"]], ["D1", "D4", "D3"])      # earliest first
        self.assertEqual(o["not_filtered"], ["follow_ups_due", "next_follow_ups"])
        other = self.dash(query="date_from=2026-01-01&date_to=2026-01-31")
        self.assertEqual((other["received"], other["follow_ups_due"], len(other["next_follow_ups"])), (0, 2, 3))
        for point in o["inflow"]:
            self.assertEqual(point["total"], sum(point["vendors"].values()))

    def test_ist_boundaries_range_and_previous_period(self):
        self.assertEqual(self.dash(query="date_from=2026-11-15&date_to=2026-11-15")["received"], 1)   # 23:30 IST
        self.assertEqual(self.dash(query="date_from=2026-11-16")["received"], 1)                      # 00:30 IST
        week = self.dash(query="date_from=2026-11-10&date_to=2026-11-16")                           # previous 3-9 Nov
        self.assertEqual((week["received"], week["previous_received"], week["change_pct"]), (3, 1, 200))
        self.assertEqual(week["period"]["granularity"], "day")
        self.assertEqual(len(week["inflow"]), 7)

    def test_all_time_and_weekly_inflow(self):
        everything = self.dash(query="period=all")
        self.assertEqual((everything["received"], everything["previous_received"], everything["change_pct"]), (5, None, None))
        self.assertEqual(everything["average_per_day"]["previous"], None)
        self.assertEqual(everything["period"]["granularity"], "month")
        self.assertEqual([p["date"] for p in everything["inflow"]], ["2026-10-01", "2026-11-01"])
        long = self.dash(query="date_from=2026-08-01&date_to=2026-11-16")
        self.assertEqual(long["period"]["granularity"], "week")
        self.assertEqual(sum(p["total"] for p in long["inflow"]), 5)

    def test_vendor_login_gets_only_its_own_numbers(self):
        o = self.dash(self.phodeo)
        self.assertEqual(set(o), VENDOR_KEYS)
        self.assertEqual(set(self.dash()), VENDOR_KEYS | ADMIN_ONLY_KEYS)
        self.assertEqual(o["received"], 3)                                                   # D1, D2, D5
        # Status numbers over their own leads only (D3, D4 are Other Vendor's)
        self.assertEqual((o["pending"], o["converted"], o["converted_pct"], o["purchased_with_us"]), (1, 1, 33.3, 1))
        self.assertEqual({k: v for k, v in o["by_status"].items() if v},
                         {"pending": 1, "converted": 1, "purchased_with_us": 1})
        self.assertEqual(o["funnel"], {"received": 3, "updated": 2, "engaged": 1, "converted": 1})
        self.assertEqual(o["follow_ups_due"], 1)                                             # D1; not D4 (Other's)
        self.assertEqual([l["name"] for l in o["next_follow_ups"]], ["D1"])
        self.assertNotIn("status_updated_by", o["next_follow_ups"][0])                      # no user ids
        self.assertIn("update_status", o["next_follow_ups"][0])
        self.assertEqual([v["name"] for v in o["inflow_vendors"]], ["Phodeo"])
        week = self.dash(self.phodeo, "date_from=2026-11-10&date_to=2026-11-16")
        self.assertEqual((week["received"], week["change_pct"]), (2, 100))                  # D1, D2 vs D5
        self.assertEqual(self.dash(self.phodeo, "period=all")["received"], 3)
        unlinked = self.headers("unlinked@example.com")
        self.assertEqual(self.client.get(URL, headers=unlinked).status_code, 403)

    def test_validation(self):
        for query, text in (("period=all&date_from=2026-11-01", "not both"),
                            ("date_from=2026-11-17", "can't be in the future"),
                            ("date_from=2025-01-01&date_to=2026-11-16", "at most 366 days")):
            resp = self.client.get(f"{URL}?{query}", headers=self.admin)
            self.assertEqual(resp.status_code, 400, query)
            self.assertIn(text, resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
