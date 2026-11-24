"""Dhruva -> KOC leads: list, filters (any event date), sort (earliest event date), stats, export and
filter options (list = stats = export). "Today" is 16 Nov 2026 (India time). The fake sheet
(tests/dhruva_fixtures.py) is uploaded by an admin, then an admin sets some KOC fields. Run:

    python -m unittest tests.test_dhruva_list -v
"""
import io
import unittest
from datetime import date, datetime, timezone

from openpyxl import load_workbook
from sqlmodel import Session, select

from app.models.dhruva import DhruvaLead
from tests.dhruva_fixtures import HEADERS, DhruvaTestBase

EXPORT_HEADERS = ["Sl. No", *HEADERS, "Sent at", "Added by", "Updated by", "Updated at", "Hot"]


class DhruvaListTest(DhruvaTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload_all()["added"], 6)
        with Session(self.engine) as s:
            self.ids = {lead.name: lead.id for lead in s.exec(select(DhruvaLead))}
            old = s.get(DhruvaLead, self.ids["No Date"])            # received on 1 Nov
            old.created_at = datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)
            s.add(old)
            s.commit()
        koc = {
            "Asha Fake": {"update_status": "converted", "quoted_amount": 200000, "conversion_amount": 180000},
            "Ravi Kumarfake": {"update_status": "interested", "quoted_amount": 50000, "follow_up_date": "2026-11-10"},
            "Meena Fake": {"update_status": "not_interested"},
        }
        for name, body in koc.items():
            self.assertEqual(self.call("admin", "PATCH", f"/leads/{self.ids[name]}", json=body).status_code, 200)

    def names(self, who="dhruva", **params) -> set[str]:
        resp = self.call(who, "GET", "/leads", params={"page_size": 100, **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["total"], len(body["items"]))
        return {i["name"] for i in body["items"]}

    def test_filters(self):
        c = lambda expected, **p: self.assertEqual(self.names(**p), expected, p)  # noqa: E731
        self.assertEqual(len(self.names()), 6)
        c({"Asha Fake"}, search="asha")
        c({"Ravi Kumarfake"}, search="98000 00002")                       # phone digits
        c({"Ravi Kumarfake"}, search="9000000002")                        # its second number
        c({"Meena Fake"}, search="+91 70000 00033")                       # its third number
        c({"Asha Fake"}, search="delivered")                              # WhatsApp status
        c({"Asha Fake", "Ravi Kumarfake", "Meena Fake"}, venue="main palace")
        c({"Asha Fake", "Ravi Kumarfake", "Two Dates"}, event_type="wedding")
        # Event dates: any of the lead's dates
        c({"Ravi Kumarfake"}, event_from="2026-12-26", event_to="2026-12-31")          # its second date
        c({"Two Dates"}, event_from="2027-01-23", event_to="2027-01-31")               # not its earliest
        c({"No Phone One", "Asha Fake", "Meena Fake"}, upcoming_days=30)                # 16 Nov - 16 Dec
        c({"Ravi Kumarfake"}, needs_intervention="yes")
        self.assertEqual(len(self.names(needs_intervention="no")), 5)
        c({"No Phone One"}, has_phone="no")
        c({"Asha Fake", "Ravi Kumarfake"}, quote="has")
        c({"Asha Fake"}, conversion="has")
        c({"Ravi Kumarfake"}, follow_up_due="true")                       # 10 Nov; Asha's is 20 Nov
        c({"No Phone One", "Two Dates", "No Date"}, update_status="pending")
        c({"No Date"}, date_from="2026-11-01", date_to="2026-11-01")     # received
        c({"Asha Fake"}, venue="palace", update_status="converted")       # combined: AND

    def test_sort_by_earliest_event_date_and_paging(self):
        def order(direction):
            items = self.call("dhruva", "GET", "/leads", params={"sort": "event_date", "order": direction}).json()["items"]
            return [i["name"] for i in items]
        self.assertEqual(order("asc"), ["No Phone One", "Asha Fake", "Meena Fake", "Ravi Kumarfake", "Two Dates",
                                        "No Date"])
        self.assertEqual(order("desc"), ["Two Dates", "Ravi Kumarfake", "Meena Fake", "Asha Fake", "No Phone One",
                                         "No Date"])                      # no date: always last
        page = self.call("dhruva", "GET", "/leads", params={"sort": "name", "order": "asc", "page_size": 10}).json()
        self.assertEqual((page["total"], page["pages"], [i["sl_no"] for i in page["items"]]), (6, 1, list(range(1, 7))))
        two = next(i for i in page["items"] if i["name"] == "Two Dates")
        self.assertEqual((two["event_dates"], two["event_date"]), (["2027-01-22", "2027-01-24"], "2027-01-22"))
        self.assertEqual(self.call("dhruva", "GET", "/leads", params={"page_size": 20}).status_code, 422)
        sent = self.call("dhruva", "GET", "/leads", params={"sort": "sent_at"})
        self.assertEqual(sent.status_code, 200)

    def test_list_stats_export_agree(self):
        combos = [{}, {"venue": "palace"}, {"update_status": "pending"}, {"has_phone": "no"}, {"upcoming_days": 30},
                  {"search": "fake", "sort": "name", "order": "asc"}, {"quote": "has"}, {"follow_up_due": "true"},
                  {"event_type": "wedding"}, {"needs_intervention": "yes"},
                  {"event_from": "2027-01-23", "event_to": "2027-01-31"}, {"sort": "event_date", "order": "asc"}]
        for who in ("admin", "dhruva"):
            for params in combos:
                with self.subTest(who=who, params=params):
                    listed = self.call(who, "GET", "/leads", params={"page_size": 100, **params}).json()
                    stats = self.call(who, "GET", "/leads/stats", params=params).json()
                    resp = self.call(who, "GET", "/leads/export", params=params)
                    self.assertEqual(resp.status_code, 200, resp.text)
                    rows = list(load_workbook(io.BytesIO(resp.content)).active.iter_rows(min_row=2, values_only=True))
                    self.assertEqual(stats["total"], listed["total"])
                    self.assertEqual(len(rows), listed["total"])
                    self.assertEqual([r[1] for r in rows], [i["name"] for i in listed["items"]])   # same order

    def test_stats(self):
        stats = self.call("dhruva", "GET", "/leads/stats").json()
        self.assertEqual((stats["total"], stats["by_status"]["pending"], stats["kpis"]["converted"]), (6, 3, 1))
        self.assertEqual((stats["no_phone"], stats["no_event_date"], stats["needs_intervention"],
                          stats["follow_ups_due"], stats["upcoming_30_days"]), (1, 1, 1, 1, 3))

    def test_export_columns_and_round_trip(self):
        resp = self.call("dhruva", "GET", "/leads/export", params={"sort": "name", "order": "asc"})
        self.assertTrue(resp.headers["content-disposition"].startswith('attachment; filename="dhruva_leads_2026-11-16'))
        ws = load_workbook(io.BytesIO(resp.content)).active
        self.assertEqual([c.value for c in ws[1]], EXPORT_HEADERS)
        rows = {r[1]: dict(zip(EXPORT_HEADERS, r)) for r in ws.iter_rows(min_row=2, values_only=True)}
        ravi, asha = rows["Ravi Kumarfake"], rows["Asha Fake"]
        self.assertEqual((ravi["Phone numbers"], ravi["Event date"], ravi["Needs Dhruva intervention"]),
                         ("9800000002, 9000000002", "2026-12-25, 2026-12-26", True))
        self.assertEqual((asha["Update"], asha["Quoted amount"], asha["WhatsApp status"], asha["Added by"]),
                         ("Converted", 200000, "Delivered", "Admin"))
        self.assertEqual(asha["Follow-up date"].date() if isinstance(asha["Follow-up date"], datetime)
                         else asha["Follow-up date"], date(2026, 11, 20))
        self.assertIsNotNone(asha["Sent at"])
        # The export uploads back (xlsx and csv): every row a duplicate; the extra columns are ignored
        body = self.preview(content=resp.content, filename="export.xlsx").json()
        self.assertEqual((body["counts"]["duplicates"], body["counts"]["valid"], body["counts"]["needs_fixing"]),
                         (6, 0, 0))
        self.assertEqual(body["ignored_columns"], ["Sent at", "Added by", "Updated by", "Updated at", "Hot"])
        csv_resp = self.call("dhruva", "GET", "/leads/export", params={"format": "csv"})
        body = self.preview(content=csv_resp.content, filename="export.csv").json()
        self.assertEqual((body["counts"]["total"], body["counts"]["duplicates"]), (6, 6))

    def test_filter_options(self):
        o = self.call("dhruva", "GET", "/leads/filter-options").json()
        self.assertEqual({v["value"]: v["count"] for v in o["venues"]}["Main Palace"], 3)
        self.assertEqual({v["value"]: v["count"] for v in o["event_types"]}["wedding"], 3)
        self.assertEqual(o["whatsapp_statuses"], [{"value": "Delivered", "count": 1}])
        self.assertEqual(o["updates"][0], {"value": "pending", "count": 3})
        self.assertEqual(o["needs_intervention"], [{"value": "yes", "count": 1}, {"value": "no", "count": 5}])
        self.assertEqual(o["sources"], [{"value": "manual", "count": 0}, {"value": "bulk_upload", "count": 6}])

    def test_hot(self):
        lead_id = self.ids["Asha Fake"]
        for method in ("POST", "DELETE"):
            self.assertEqual(self.call("phodeo", method, f"/leads/{lead_id}/hot").status_code, 403)
        self.assertEqual(self.call("dhruva", "POST", f"/leads/{lead_id}/hot").status_code, 200)
        self.assertTrue(self.call("admin", "GET", f"/leads/{lead_id}").json()["is_hot"])
        self.assertEqual(self.names(hot="yes"), {"Asha Fake"})
        exported = self.call("admin", "GET", "/leads/export", params={"hot": "yes"})
        ws = load_workbook(io.BytesIO(exported.content)).active
        self.assertTrue(ws.cell(2, len(EXPORT_HEADERS)).value)
        self.assertEqual(self.call("admin", "DELETE", f"/leads/{lead_id}/hot").status_code, 200)
        actions = {a["action"] for a in self.call("admin", "GET", f"/leads/{lead_id}").json()["activity"]}
        self.assertTrue({"hot_on", "hot_off", "created", "field_changed"} <= actions)

    def test_archived_and_errors(self):
        self.call("admin", "POST", f"/leads/{self.ids['Asha Fake']}/archive")
        self.assertNotIn("Asha Fake", self.names())
        self.assertEqual(self.call("dhruva", "GET", "/leads/stats").json()["total"], 5)
        for params, text in (({"event_from": "2026-12-31", "event_to": "2026-12-01"}, "event_from must be on or before"),
                             ({"upcoming_days": 0}, "between 1 and 366"), ({"quote": "lots"}, "quote must be"),
                             ({"upcoming_days": 5, "event_from": "2026-12-01"}, "either upcoming_days"),
                             ({"date_from": "2026-11-17"}, "can't be in the future")):
            for url in ("/leads", "/leads/stats", "/leads/export"):
                resp = self.call("dhruva", "GET", url, params=params)
                self.assertEqual(resp.status_code, 400, (url, params))
                self.assertIn(text, resp.json()["detail"])
        for url in ("/leads", "/leads/stats", "/leads/export", "/leads/filter-options"):
            self.assertEqual(self.call("phodeo", "GET", url).status_code, 403, url)


if __name__ == "__main__":
    unittest.main()
