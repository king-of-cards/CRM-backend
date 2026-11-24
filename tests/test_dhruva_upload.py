"""Dhruva -> KOC leads: the upload (template, preview, bulk save; admins only), the single add, edits,
activity and archive. "Today" is 16 Nov 2026 (India time); see tests/dhruva_fixtures.py. Run:

    python -m unittest tests.test_dhruva_upload -v
"""
import csv
import io
import unittest
from datetime import date, datetime, timezone

from openpyxl import load_workbook
from sqlmodel import Session, select

from app.models.dhruva import DhruvaEventDate, DhruvaLead, DhruvaPhone
from app.models.lead_activity import LeadActivity
from app.services import dhruva_import as parser
from tests.dhruva_fixtures import HEADERS, DhruvaTestBase, lead_row, sample_workbook

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)          # crud.utcnow in the fixtures


class ParserTests(unittest.TestCase):
    def test_phones(self):
        for text, expected in (("9845012345", "9845012345"), ("+91 98450 12345", "9845012345"),
                               ("919845012345", "9845012345"), ("09845012345", "9845012345"),
                               (9845012345.0, "9845012345"), ("", None), ("N/A", None)):
            self.assertEqual(parser.parse_phone(text), (expected, None), text)
        for text in ("12345", "98450123456789", "+1 415 555 0100"):
            self.assertEqual(parser.parse_phone(text)[1], parser.MSG_PHONE, text)
        self.assertEqual(parser.parse_phones("9845012345, +91 98450 12346; 9845012345 / 09845012347\n9845012348"),
                         (["9845012345", "9845012346", "9845012347", "9845012348"], None))   # order kept, repeats dropped
        self.assertEqual(parser.parse_phones(["9845012345", 9845012346]), (["9845012345", "9845012346"], None))
        self.assertEqual(parser.parse_phones("N/A"), ([], None))
        self.assertEqual(parser.parse_phones("9845012345, 123")[1], parser.MSG_PHONE)

    def test_event_dates(self):
        self.assertEqual(parser.parse_event_dates("2026-12-05, 2026-12-04;2026-12-05"),
                         ([date(2026, 12, 4), date(2026, 12, 5)], None))          # sorted, repeats dropped
        self.assertEqual(parser.parse_event_dates(["2026-12-04", date(2026, 12, 6)]),
                         ([date(2026, 12, 4), date(2026, 12, 6)], None))
        self.assertEqual(parser.parse_event_dates(datetime(2026, 12, 20)), ([date(2026, 12, 20)], None))  # Excel cell
        self.assertEqual(parser.parse_event_dates("N/A"), ([], None))
        for bad in ("Dec 4th", "04-12-2026", "2026-02-30", "2026-12-04 or 05"):
            self.assertEqual(parser.parse_event_dates(bad)[1], parser.MSG_DATES, bad)

    def test_values_and_errors(self):
        lead = parser.clean_lead({"name": "  Asha  Fake ", "phones": "9845012345, 9845012345",
                                  "update_status": "Will call back", "quoted_amount": "₹1,50,000",
                                  "needs_intervention": "yes", "follow_up_date": "25-11-2026",
                                  "follow_up_notes": "line one\nline two", "event_dates": "2026-12-04"})
        self.assertTrue(lead.ok, lead.errors)
        self.assertEqual((lead.name, lead.phones, lead.update_status, lead.quoted_amount, lead.needs_intervention,
                          lead.follow_up_date, lead.follow_up_notes),
                         ("Asha Fake", ["9845012345"], "will_call_back", 150000, True, date(2026, 11, 25), "line one\nline two"))
        self.assertEqual(parser.clean_lead({"name": "X", "update_status": "converted"}).update_status, "converted")
        bad = parser.clean_lead({"phones": "9845012345", "update_status": "maybe", "needs_intervention": "perhaps",
                                 "quoted_amount": "lots"})
        self.assertEqual({e["field"] for e in bad.errors},
                         {"name", "update_status", "needs_intervention", "quoted_amount"})


class UploadTests(DhruvaTestBase, unittest.TestCase):
    def test_preview(self):
        resp = self.preview()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual((body["sheet"], body["heading_row"], body["ignored_columns"]), ("Leads", 1, []))
        self.assertEqual(list(body["columns"]), HEADERS)
        status = {r["row"]: r["status"] for r in body["rows"]}
        self.assertEqual(status, {2: "ok", 3: "ok", 4: "ok", 5: "ok", 6: "ok", 7: "ok", 8: "duplicate",
                                  9: "duplicate", 10: "error", 11: "error", 12: "error"})       # 13: empty
        rows = {r["row"]: r for r in body["rows"]}
        self.assertEqual(rows[8]["duplicate"], {"reason": "in_file", "duplicate_of_row": 2})       # one same number
        self.assertEqual(rows[9]["duplicate"], {"reason": "in_file", "duplicate_of_row": 5})       # name + first date
        self.assertEqual([e["field"] for e in rows[10]["errors"]], ["name"])
        self.assertEqual([e["message"] for e in rows[11]["errors"]], [parser.MSG_PHONE])
        self.assertEqual([e["message"] for e in rows[12]["errors"]], [parser.MSG_DATES])
        self.assertEqual(rows[4]["values"]["phones"], "9700000003, 8200000003, 7000000033")
        self.assertEqual(rows[6]["values"]["event_dates"], "2027-01-22, 2027-01-24")
        self.assertEqual(rows[2]["values"]["update_status"], "interested")
        self.assertEqual(rows[5]["flags"], ["no_phone"])
        c = body["counts"]
        self.assertEqual((c["total"], c["valid"], c["needs_fixing"], c["duplicates"], c["no_phone"]), (11, 6, 3, 2, 2))
        self.assertEqual((rows[11]["values"]["phones"], rows[12]["values"]["event_dates"]),
                         ("9800000013, 12345", "Dec 4th"))

    def test_only_admins_upload(self):
        for who in ("dhruva", "phodeo"):
            self.assertEqual(self.preview(who).status_code, 403, who)
            self.assertEqual(self.call(who, "POST", "/leads/bulk", json={"leads": []}).status_code, 403, who)

    def test_bulk_save_sent_at_dates_and_activity(self):
        out = self.upload_all()
        self.assertEqual((out["added"], out["duplicates_skipped"], out["failed"]), (6, 2, 3))
        with Session(self.engine) as s:
            leads = {l.name: l for l in s.exec(select(DhruvaLead))}
            dates, numbers = {}, {}
            for d in s.exec(select(DhruvaEventDate)):
                dates.setdefault(d.lead_id, []).append(d.event_date)
            for p in s.exec(select(DhruvaPhone).order_by(DhruvaPhone.id)):
                numbers.setdefault(p.lead_id, []).append((p.phone, p.phone_e164))
            acts = s.exec(select(LeadActivity).where(LeadActivity.source == "dhruva")).all()
        asha, ravi, two = leads["Asha Fake"], leads["Ravi Kumarfake"], leads["Two Dates"]
        self.assertEqual((asha.source, asha.created_by), ("bulk_upload", self.users["admin"].id))
        self.assertEqual(asha.sent_at.replace(tzinfo=timezone.utc), NOW)
        self.assertEqual((asha.whatsapp_status, asha.update_status, asha.quoted_amount, asha.conversion_amount,
                          asha.follow_up_date), ("Delivered", "interested", 45000, 120000, date(2026, 11, 20)))
        self.assertEqual((asha.status_updated_by, asha.phone_e164), (self.users["admin"].id, "+919800000001"))
        self.assertTrue(ravi.needs_intervention)
        self.assertEqual(numbers[ravi.id], [("9800000002", "+919800000002"), ("9000000002", "+919000000002")])
        self.assertEqual((ravi.phone, ravi.phone_e164), ("9800000002", "+919800000002"))   # the first
        self.assertEqual(len(numbers[leads["Meena Fake"].id]), 3)
        self.assertNotIn(leads["No Phone One"].id, numbers)
        self.assertEqual(sorted(dates[ravi.id]), [date(2026, 12, 25), date(2026, 12, 26)])
        self.assertEqual(two.event_date, date(2027, 1, 22))                                 # the earliest
        self.assertIsNone(leads["No Date"].event_date)
        self.assertEqual(sorted(a.action for a in acts), ["created"] * 6)
        # The same file again: every valid row is a duplicate of a saved lead
        again = self.preview().json()["counts"]
        self.assertEqual((again["valid"], again["duplicates_by_reason"]["existing"]), (0, 8))

    def test_headers_must_be_the_templates(self):
        other = sample_workbook([["A", "9845012345"]], headers=["Customer", "Mobile"])
        resp = self.preview(content=other)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Name", resp.json()["detail"])
        extra = sample_workbook([["Sl", "A", "9845012345", "x", "y"]],
                                headers=["Sl. No", "Name", "Phone numbers", "Hot", "Alternate phone"])
        body = self.preview(content=extra).json()
        self.assertEqual((body["columns"], body["ignored_columns"]),
                         ({"Name": "name", "Phone numbers": "phones"}, ["Hot", "Alternate phone"]))
        self.assertEqual(body["rows"][0]["status"], "ok")

    def test_csv_upload(self):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(HEADERS)
        writer.writerow(lead_row("Csv Lead", "9845012399, 9845012398", "2026-12-04, 2026-12-05"))
        body = self.preview(content=buf.getvalue().encode("utf-8"), filename="leads.csv").json()
        self.assertEqual(body["rows"][0]["status"], "ok")
        self.assertEqual(body["rows"][0]["values"]["event_dates"], "2026-12-04, 2026-12-05")
        self.assertEqual(body["rows"][0]["values"]["phones"], "9845012399, 9845012398")

    def test_template_and_example_row(self):
        resp = self.call("admin", "GET", "/leads/template")
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))
        ws = wb["Leads"]
        self.assertEqual(wb.sheetnames[0], "Leads")
        self.assertEqual([c.value for c in ws[1]], HEADERS)
        self.assertTrue(all(ws.cell(row=r, column=c).number_format == "@" for r in (2, 3, 2001)
                            for c in range(1, len(HEADERS) + 1)))
        self.assertTrue(all(isinstance(c.value, (str, type(None))) for c in ws[2]))
        self.assertEqual((ws.cell(row=2, column=2).value, ws.cell(row=2, column=3).value),
                         ("9876543210, 9876543211", "2026-12-04, 2026-12-05"))
        body = self.preview(content=resp.content).json()                                  # the example row
        self.assertEqual(body["rows"][0]["status"], "error")
        self.assertIn(parser.EXAMPLE_ROW_ERROR, body["rows"][0]["errors"])
        self.assertEqual(self.call("dhruva", "GET", "/leads/template").status_code, 403)
        csv_template = self.call("admin", "GET", "/leads/template", params={"format": "csv"})
        self.assertEqual(next(csv.reader(io.StringIO(csv_template.content.decode("utf-8-sig")))), HEADERS)


class AddAndEditTests(DhruvaTestBase, unittest.TestCase):
    def test_single_add(self):
        resp = self.add(name="Single", phones=["9845012301", "9845012311"], event_dates=["2026-12-20", "2026-12-21"],
                        venue="Hall")
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual((body["source"], body["event_dates"], body["event_date"], body["sent_at"][:19]),
                         ("manual", ["2026-12-20", "2026-12-21"], "2026-12-20", "2026-11-16T12:00:00"))
        self.assertEqual((body["phones"], body["phones_e164"]),
                         (["9845012301", "9845012311"], ["+919845012301", "+919845012311"]))
        self.assertEqual(self.add(name="Second number", phones="9845012311").status_code, 409)   # any number
        self.assertEqual(self.add(name="Again", phones=["919845012301"]).status_code, 409)          # same phone
        self.assertEqual(self.add(name="Again", phones=["9845012301"], allow_duplicate=True).status_code, 201)
        self.assertEqual(self.add(name="KOC field", update_status="converted").status_code, 403)  # Dhruva: lead fields
        self.assertEqual(self.add("admin", name="By admin", update_status="Converted",
                                  needs_intervention=True).json()["update_status"], "converted")
        self.assertEqual(self.add(name="Sent", sent_at="2026-01-01").status_code, 422)            # never editable
        self.assertEqual(self.add(name="Bad", event_dates=["2026-13-01"]).status_code, 422)

    def test_edits_and_activity(self):
        lead_id = self.add(name="Edit Me", phones=["9845012302"], event_dates=["2026-12-04"]).json()["id"]
        # Dhruva logins: lead fields (add / remove event dates by sending the list)
        resp = self.call("dhruva", "PATCH", f"/leads/{lead_id}",
                         json={"event_dates": ["2026-12-06", "2026-12-04", "2026-12-05"], "venue": "New Hall",
                               "phones": ["9845012302", "9845012312"]})                  # add a number
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["event_dates"], ["2026-12-04", "2026-12-05", "2026-12-06"])
        self.assertEqual(resp.json()["phones"], ["9845012302", "9845012312"])
        resp = self.call("dhruva", "PATCH", f"/leads/{lead_id}", json={"phones": ["9845012312"]})   # remove one
        self.assertEqual(resp.json()["phones"], ["9845012312"])
        self.assertEqual(self.call("dhruva", "GET", "/leads", params={"search": "98450 12312"}).json()["total"], 1)
        self.assertEqual(self.call("dhruva", "GET", "/leads", params={"search": "9845012302"}).json()["total"], 0)
        resp = self.call("dhruva", "PATCH", f"/leads/{lead_id}", json={"event_dates": ["2026-12-05"]})
        self.assertEqual((resp.json()["event_dates"], resp.json()["event_date"]), (["2026-12-05"], "2026-12-05"))
        self.assertEqual(self.call("dhruva", "PATCH", f"/leads/{lead_id}", json={"comments": "x"}).status_code, 403)
        self.assertEqual(self.call("dhruva", "PATCH", f"/leads/{lead_id}", json={"name": None}).status_code, 422)
        self.assertEqual(self.call("admin", "PATCH", f"/leads/{lead_id}", json={"sent_at": None}).status_code, 422)
        # Admins: every KOC field
        koc = {"whatsapp_status": "Read", "response": "Will decide", "message_sent": "Brochure", "follow_up_notes": "Call",
               "comments": "VIP", "needs_intervention": True, "update_status": "will_call_back",
               "quoted_amount": "50000", "conversion_amount": 40000, "follow_up_date": "2026-11-25"}
        resp = self.call("admin", "PATCH", f"/leads/{lead_id}", json=koc)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual({k: body[k] for k in koc}, {**koc, "quoted_amount": 50000, "follow_up_date": "2026-11-25"})
        self.assertEqual(body["status_updated_by_name"], "Admin")
        # Every change logged with who, field, old -> new; shown on the lead
        entries = [(a["actor_name"], a["field"], a["old_value"], a["new_value"]) for a in body["activity"]]
        self.assertIn(("Dhruva", "event_dates", "2026-12-04", "2026-12-04, 2026-12-05, 2026-12-06"), entries)
        self.assertIn(("Dhruva", "event_dates", "2026-12-04, 2026-12-05, 2026-12-06", "2026-12-05"), entries)
        self.assertIn(("Dhruva", "venue", None, "New Hall"), entries)
        self.assertIn(("Dhruva", "phones", "9845012302", "9845012302, 9845012312"), entries)
        self.assertIn(("Dhruva", "phones", "9845012302, 9845012312", "9845012312"), entries)
        self.assertIn(("Admin", "update_status", None, "will_call_back"), entries)
        self.assertIn(("Admin", "quoted_amount", None, "50000"), entries)
        self.assertIn(("Admin", "conversion_amount", None, "40000"), entries)
        self.assertIn(("Admin", "needs_intervention", "False", "True"), entries)
        self.assertIn(("Admin", "follow_up_date", None, "2026-11-25"), entries)
        self.assertIn(("Dhruva", "lead", None, "Edit Me"), entries)                          # the create
        self.assertTrue(all(a["created_at"] for a in body["activity"]))

    def test_edit_checks_duplicates(self):
        first = self.add(name="First", phones=["9845012303"]).json()["id"]
        second = self.add(name="Second", phones=["9845012304"]).json()["id"]
        self.assertEqual(self.call("dhruva", "PATCH", f"/leads/{first}", json={"name": "First renamed"}).status_code, 200)
        resp = self.call("dhruva", "PATCH", f"/leads/{second}", json={"phones": ["9845012304", "9845012303"]})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"]["duplicate"]["lead_id"], first)
        self.assertEqual(self.call("dhruva", "PATCH", f"/leads/{second}",
                                   json={"phones": ["9845012304", "9845012303"], "allow_duplicate": True}).status_code, 200)

    def test_archive_is_admin_only(self):
        lead_id = self.add(name="Archive Me", phones=["9845012305"]).json()["id"]
        self.assertEqual(self.call("dhruva", "POST", f"/leads/{lead_id}/archive").status_code, 403)
        self.assertTrue(self.call("admin", "POST", f"/leads/{lead_id}/archive").json()["archived"])
        self.assertEqual(self.add(name="Archive Me 2", phones=["9845012305"]).status_code, 409)     # still a duplicate
        self.assertFalse(self.call("admin", "POST", f"/leads/{lead_id}/unarchive").json()["archived"])


if __name__ == "__main__":
    unittest.main()
