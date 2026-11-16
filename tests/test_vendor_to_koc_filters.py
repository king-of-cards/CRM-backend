"""Vendor -> KOC: the consistent filters on GET /leads, GET /leads/stats and GET /leads/export.

"Today" is fixed at 16 Nov 2026 (India time). In-memory SQLite (see test_vendor_to_koc). Run:

    python -m unittest discover -s tests -t . -v
"""
import io
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from tests.test_vendor_to_koc import BASE, VendorToKocTestCase

import openpyxl  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.models.vendor_to_koc import VendorToKocLead  # noqa: E402
from app.services import periods  # noqa: E402

TODAY = date(2026, 11, 16)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class VendorLeadFilterTests(VendorToKocTestCase):
    """L1 Phodeo, 23:30 IST on 15 Nov, Bengaluru, bulk, pending, follow-up 10 Nov (due)
    L2 Phodeo, 00:30 IST on 16 Nov, Mysuru, manual, converted, follow-up 1 Nov (closed: not due)
    L3 Other, 1 Oct, Pune, bulk, interested, follow-up 1 Dec (not due yet)
    L4 Other, 10 Nov, "Bengaluru South", manual, will call back, follow-up today (due)"""

    def setUp(self):
        super().setUp()
        self.today = mock.patch.object(periods, "today_ist", lambda: TODAY)
        self.today.start()
        self.L = {}
        rows = {
            "L1": dict(vendor_id=self.phodeo_id, phone="9000000011", city="Bengaluru", source="bulk_upload",
                       created_at=utc(2026, 11, 15, 18, 0), follow_up_date=date(2026, 11, 10), lead_date=date(2026, 11, 1)),
            "L2": dict(vendor_id=self.phodeo_id, phone="9000000012", city="Mysuru", source="manual",
                       created_at=utc(2026, 11, 15, 19, 0), update_status="converted", follow_up_date=date(2026, 11, 1)),
            "L3": dict(vendor_id=self.other_id, phone="9000000013", city="Pune", source="bulk_upload",
                       created_at=utc(2026, 10, 1, 5, 0), update_status="interested", follow_up_date=date(2026, 12, 1)),
            "L4": dict(vendor_id=self.other_id, phone="9000000014", city="Bengaluru South", source="manual",
                       created_at=utc(2026, 11, 10, 5, 0), update_status="will_call_back", follow_up_date=TODAY),
        }
        with Session(self.engine) as s:
            for key, values in rows.items():
                lead = VendorToKocLead(created_by=self.ids["admin"], updated_at=values["created_at"], **values)
                s.add(lead)
                s.commit()
                self.L[key] = lead.id
        self.name_of = {v: k for k, v in self.L.items()}

    def tearDown(self):
        self.today.stop()
        super().tearDown()

    def got(self, headers, query="") -> set[str]:
        resp = self.client.get(f"{BASE}/leads?page_size=100&{query}", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        return {self.name_of[i["id"]] for i in resp.json()["items"]}

    def test_filters(self):
        a = self.admin
        self.assertEqual(self.got(a, "date_from=2026-11-15&date_to=2026-11-15"), {"L1"})      # 23:30 IST
        self.assertEqual(self.got(a, "date_from=2026-11-16"), {"L2"})                         # 00:30 IST
        self.assertEqual(self.got(a, "date_from=2026-11-01&date_to=2026-11-16"), {"L1", "L2", "L4"})
        self.assertEqual(self.got(a, "city=bengaluru"), {"L1", "L4"})                         # contains, any case
        self.assertEqual(self.got(a, "source=manual"), {"L2", "L4"})
        self.assertEqual(self.got(a, "source=manual&source=bulk_upload"), {"L1", "L2", "L3", "L4"})
        self.assertEqual(self.got(a, "follow_up_due=true"), {"L1", "L4"})                     # open, date <= today
        self.assertEqual(self.got(a, "lead_date_from=2026-11-01&lead_date_to=2026-11-01"), {"L1"})
        self.assertEqual(self.got(a, "update_status=pending&city=bengaluru"), {"L1"})
        self.assertEqual(self.got(a, f"vendor_id={self.other_id}&follow_up_due=true"), {"L4"})

    def test_vendor_scope(self):
        p = self.phodeo
        self.assertEqual(self.got(p), {"L1", "L2"})
        self.assertEqual(self.got(p, f"vendor_id={self.other_id}"), {"L1", "L2"})            # ignored
        self.assertEqual(self.got(p, "city=bengaluru"), {"L1"})
        self.assertEqual(self.got(p, "date_from=2026-11-16"), {"L2"})
        resp = self.client.get(f"{BASE}/leads?follow_up_due=true", headers=p)
        self.assertEqual(resp.status_code, 422)                                              # admin field
        stats = self.client.get(f"{BASE}/leads/stats", headers=p).json()
        self.assertEqual(stats, {"total": 2, "by_source": {"manual": 1, "bulk_upload": 1}})    # no admin numbers

    def test_validation(self):
        for query, code, text in (
            ("date_from=2026-11-17", 400, "can't be in the future"),
            ("date_from=2026-11-10&date_to=2026-11-01", 400, "date_from must be on or before date_to"),
            ("lead_date_from=2026-11-10&lead_date_to=2026-11-01", 400, "lead_date_from must be on or before lead_date_to"),
            ("follow_up_from=2026-11-10&follow_up_to=2026-11-01", 400, "follow_up_from must be on or before follow_up_to"),
            ("source=email", 422, None),
        ):
            for path in ("leads", "leads/stats", "leads/export"):
                resp = self.client.get(f"{BASE}/{path}?{query}", headers=self.admin)
                self.assertEqual(resp.status_code, code, (path, query, resp.text))
                if text:
                    self.assertIn(text, resp.json()["detail"])

    def test_stats_and_export_match_the_list(self):
        combos = ["", "date_from=2026-11-01", "city=bengaluru", "source=manual", "follow_up_due=true",
                  "update_status=pending&update_status=converted", f"vendor_id={self.phodeo_id}&search=9000"]
        vendor_combos = ["", "date_from=2026-11-16", "city=mys", "source=bulk_upload"]
        for headers, cases in ((self.admin, combos), (self.phodeo, vendor_combos)):
            for query in cases:
                listed = self.client.get(f"{BASE}/leads?page_size=100&{query}", headers=headers).json()
                stats = self.client.get(f"{BASE}/leads/stats?{query}", headers=headers).json()
                resp = self.client.get(f"{BASE}/leads/export?{query}", headers=headers)
                self.assertEqual(resp.status_code, 200, (query, resp.text))
                rows = list(openpyxl.load_workbook(io.BytesIO(resp.content)).active.iter_rows(min_row=2, values_only=True))
                self.assertEqual(stats["total"], listed["total"], query)
                self.assertEqual(len(rows), listed["total"], query)
                phone_col = 3 if headers is self.phodeo else 4
                self.assertEqual([r[phone_col] for r in rows], [f"+91{i['phone']}" for i in listed["items"]], query)
        admin = self.client.get(f"{BASE}/leads/stats", headers=self.admin).json()
        self.assertEqual(admin["by_status"]["pending"], 1)
        self.assertEqual(admin["by_status"]["converted"], 1)
        self.assertEqual(admin["follow_ups_due"], 2)
        self.assertEqual({v["vendor_name"]: v["count"] for v in admin["by_vendor"]}, {"Phodeo": 2, "Other Vendor": 2})


if __name__ == "__main__":
    unittest.main()
