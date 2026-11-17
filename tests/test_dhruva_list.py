"""Dhruva leads: list, filters, stats, export and filter options (list = stats = export).

"Today" is 16 Nov 2026 (India time). Both fake sample sheets are uploaded by a Dhruva login (see
tests/dhruva_fixtures.py), then an admin sets some admin fields. Run:

    python -m unittest discover -s tests -t . -v
"""
import io
import unittest
from datetime import datetime, timezone

from openpyxl import load_workbook
from sqlmodel import Session, select

from app.models.dhruva import DhruvaLead
from tests.dhruva_fixtures import DhruvaTestBase


class DhruvaListTest(DhruvaTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload_all()["added"], 11)                                 # sheet 1
        self.assertEqual(self.upload_all(sheet_name="Dec -March 2027")["added"], 6)      # sheet 2
        with Session(self.engine) as s:
            self.ids = {l.name: l.id for l in s.exec(select(DhruvaLead))}
            # Received dates: everything today except one lead received on 1 Nov
            old = s.get(DhruvaLead, self.ids["Month Only"])
            old.created_at = datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)
            s.add(old)
            s.commit()
        admin = {
            "Asha Fake": {"update_status": "converted", "quoted_amount": 200000, "conversion_amount": 180000},
            "Ravi Kumarfake": {"update_status": "interested", "quoted_amount": 50000, "follow_up_date": "2026-11-10"},
            "Meena Fake": {"update_status": "not_interested"},
            "Dec Fake": {"follow_up_date": "2026-11-20", "update_status": "will_call_back"},
        }
        for name, body in admin.items():
            self.assertEqual(self.call("admin", "PATCH", f"/leads/{self.ids[name]}", json=body).status_code, 200)

    def names(self, who="dhruva", **params) -> set[str]:
        resp = self.call(who, "GET", "/leads", params={"page_size": 100, **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["total"], len(body["items"]))
        return {i["name"] for i in body["items"]}

    def test_filters(self):
        c = lambda expected, **p: self.assertEqual(self.names(**p), expected, p)  # noqa: E731
        self.assertEqual(len(self.names()), 16)
        c({"Asha Fake"}, search="asha")
        c({"Ravi Kumarfake"}, search="98000 00002")                       # phone digits
        c({"Ravi Kumarfake"}, search="9000000002")                        # alt phone
        c({"Asha Fake", "Ravi Kumarfake", "Meena Fake", "April Fake"}, venue="main palace")   # any spelling
        c({"Meena Fake", "Excel Date"}, event_type="muhurtham")
        c({"Ravi Kumarfake", "Two Days"}, event_type="engagement")
        c({"Ravi Kumarfake", "Two Days", "Meena Fake", "Excel Date"}, event_type=["engagement", "muhurtham"])
        c({"Asha Fake"}, update_status="converted")
        c({"Asha Fake", "Ravi Kumarfake"}, update_status=["converted", "interested"])
        self.assertEqual(len(self.names(update_status="pending")), 12)
        c({"Asha Fake", "Ravi Kumarfake"}, quote="has")
        c({"Asha Fake"}, quote="gt:100000")
        c({"Asha Fake"}, conversion="has")
        c({"Ravi Kumarfake"}, follow_up_due="true")                       # 10 Nov; Dec Fake's is 20 Nov
        c({"Ravi Kumarfake", "Dec Fake"}, follow_up_from="2026-11-01", follow_up_to="2026-11-30")
        c({"No Phone One", "Nil Phone", "April Fake"}, has_phone="no")
        self.assertEqual(len(self.names(has_phone="yes")), 13)
        c({"Asha Fake", "Ravi Kumarfake", "Meena Fake", "Excel Date"}, event_from="2026-12-01", event_to="2026-12-31")
        c({"No Phone One", "Asha Fake", "Meena Fake"}, upcoming_days=30)    # 16 Nov - 16 Dec (not 20 Dec)
        c({"Month Only"}, date_from="2026-11-01", date_to="2026-11-01")   # received
        self.assertEqual(len(self.names(date_from="2026-11-16")), 15)  # name set collapses the repeated Asha row
        c({"Asha Fake"}, venue="palace", update_status="converted")        # combined: AND

    def test_sort_and_paging(self):
        resp = self.call("dhruva", "GET", "/leads", params={"sort": "event_date", "order": "asc"}).json()
        self.assertEqual((resp["total"], resp["pages"], resp["page_size"], len(resp["items"])), (17, 2, 10, 10))
        dates = [i["event_date"] for i in resp["items"]]
        self.assertEqual(dates[0], "2026-11-22")
        page2 = self.call("dhruva", "GET", "/leads", params={"sort": "event_date", "order": "asc", "page": 2}).json()
        self.assertEqual([i["sl_no"] for i in page2["items"]], list(range(11, 18)))
        self.assertIsNone(page2["items"][-1]["event_date"])                  # text-only dates last
        self.assertEqual(self.call("dhruva", "GET", "/leads", params={"page_size": 20}).status_code, 422)

    def test_list_stats_export_agree(self):
        combos = [{}, {"venue": "palace"}, {"update_status": "pending"}, {"has_phone": "no"}, {"upcoming_days": 30},
                  {"search": "fake", "sort": "name", "order": "asc"}, {"quote": "has"}, {"follow_up_due": "true"},
                  {"event_type": "wedding"}, {"date_from": "2026-11-16"}]
        for who in ("admin", "dhruva"):
            for params in combos:
                with self.subTest(who=who, params=params):
                    listed = self.call(who, "GET", "/leads", params={"page_size": 100, **params}).json()
                    stats = self.call(who, "GET", "/leads/stats", params=params).json()
                    resp = self.call(who, "GET", "/leads/export", params=params)
                    self.assertEqual(resp.status_code, 200, resp.text)
                    rows = list(load_workbook(io.BytesIO(resp.content)).active.iter_rows(min_row=2, values_only=True))
                    self.assertEqual(stats["total"], listed["total"])
                    self.assertEqual(stats["kpis"]["total"], listed["total"])
                    self.assertEqual(len(rows), listed["total"])
                    self.assertEqual([r[1] for r in rows], [i["name"] for i in listed["items"]])   # same order

    def test_stats(self):
        stats = self.call("dhruva", "GET", "/leads/stats").json()
        self.assertEqual(stats["kpis"], {"total": 17, "interested": 1, "not_interested": 1, "quoted_count": 2,
                                         "quoted_total": 250000, "converted": 1, "converted_pct": 5.9,
                                         "conversion_count": 1, "conversion_total": 180000})
        self.assertEqual(stats["by_status"]["pending"], 13)
        self.assertEqual((stats["no_phone"], stats["follow_ups_due"], stats["upcoming_30_days"]), (4, 1, 4))

    def test_export_columns_formats_and_round_trip(self):
        resp = self.call("dhruva", "GET", "/leads/export", params={"sort": "name", "order": "asc"})
        self.assertTrue(resp.headers["content-disposition"].startswith('attachment; filename="dhruva_leads_2026-11-16'))
        ws = load_workbook(io.BytesIO(resp.content)).active
        header = [c.value for c in ws[1]]
        self.assertEqual(header, ["Sl. No", "Name", "Venue", "Date", "Phone", "Alt phone", "Event type", "Update",
                                  "Additional comments", "Follow-up date", "Quoted amount", "Conversion amount",
                                  "Received", "Updated by", "Updated by ID", "Updated at"])
        rows = {r[1]: r for r in ws.iter_rows(min_row=2, values_only=True)}
        asha = rows["Asha Fake"]
        self.assertEqual((asha[3], asha[4], asha[7], asha[10], asha[13]),
                         ("Dec 4th", "+919800000001", "Converted", 200000, "Admin"))
        self.assertEqual(rows["Month Only"][3], "March")                   # text-only date stays text
        self.assertEqual(rows["Ravi Kumarfake"][5], "+919000000002")
        self.assertEqual(rows["Nil Phone"][7], "Pending")
        # CSV, then the export uploads back: rows with phones duplicate; no-phone rows don't.
        csv_resp = self.call("dhruva", "GET", "/leads/export", params={"format": "csv"})
        body = self.preview(content=csv_resp.content, filename="export.csv").json()
        self.assertEqual((body["counts"]["total"], body["counts"]["duplicates"], body["counts"]["valid"]), (17, 13, 4))
        body = self.preview(content=resp.content, filename="export.xlsx").json()
        self.assertEqual((body["counts"]["duplicates"], body["counts"]["valid"]), (13, 4))

    def test_filter_options(self):
        o = self.call("dhruva", "GET", "/leads/filter-options").json()
        venues = {v["value"]: v["count"] for v in o["venues"]}
        self.assertEqual(venues["Main Palace"], 4)                         # "Main palace" counted with it
        types = {v["value"]: v["count"] for v in o["event_types"]}
        self.assertEqual((types["wedding"], types["engagement"], types["muhurtham"]), (5, 2, 1))
        self.assertEqual(o["updates"][0], {"value": "pending", "count": 13})
        self.assertEqual(o["has_phone"], [{"value": "yes", "count": 13}, {"value": "no", "count": 4}])
        self.assertEqual(o["sources"], [{"value": "manual", "count": 0}, {"value": "bulk_upload", "count": 17}])

    def test_archived_and_errors(self):
        self.call("admin", "POST", f"/leads/{self.ids['Asha Fake']}/archive")
        self.assertNotIn("Asha Fake", self.names())
        self.assertEqual(self.call("dhruva", "GET", "/leads/stats").json()["total"], 16)
        for params, text in (({"event_from": "2026-12-31", "event_to": "2026-12-01"}, "event_from must be on or before"),
                             ({"upcoming_days": 0}, "between 1 and 366"), ({"quote": "lots"}, "quote must be"),
                             ({"upcoming_days": 5, "event_from": "2026-12-01"}, "either upcoming_days"),
                             ({"date_from": "2026-11-17"}, "can't be in the future"),
                             ({"follow_up_from": "2026-12-02", "follow_up_to": "2026-12-01"}, "follow_up_from")):
            for url in ("/leads", "/leads/stats", "/leads/export"):
                resp = self.call("dhruva", "GET", url, params=params)
                self.assertEqual(resp.status_code, 400, (url, params))
                self.assertIn(text, resp.json()["detail"])
        self.assertEqual(self.call("dhruva", "GET", "/leads", params={"update_status": "maybe"}).status_code, 422)
        for url in ("/leads", "/leads/stats", "/leads/export", "/leads/filter-options"):
            self.assertEqual(self.call("phodeo", "GET", url).status_code, 403, url)


if __name__ == "__main__":
    unittest.main()
