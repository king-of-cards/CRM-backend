"""Dhruva leads: file parsing (the sample sheet's patterns, fake data), preview, bulk save,
duplicates, rows without a phone, single add, edit rules and archive.

"Today" is 16 Nov 2026 (India time). In-memory SQLite (see tests/dhruva_fixtures.py). Run:

    python -m unittest discover -s tests -t . -v
"""
import csv
import io
import unittest
from datetime import date

from openpyxl import load_workbook
from sqlmodel import Session, select

from app.models.dhruva import DhruvaLead
from app.services.dhruva_import import parse_event_date, split_phones
from tests.dhruva_fixtures import TODAY, DhruvaTestBase, sample_workbook


class ParserTest(unittest.TestCase):
    def test_dates(self):
        cases = {
            "Dec 4th": date(2026, 12, 4), "dec 25th": date(2026, 12, 25), "12th Dec": date(2026, 12, 12),
            "Nov 22nd": date(2026, 11, 22), "Nov 10th": date(2027, 11, 10),          # passed: next year
            "Nov 16th": TODAY,                                                         # today counts
            "Feb 28th, Tentative date": date(2027, 2, 28), "March 1st": date(2027, 3, 1),
            "April 23rd 2027": date(2027, 4, 23), "13-12-2026": date(2026, 12, 13), "2026-12-13": date(2026, 12, 13),
            "Feb 29th": date(2028, 2, 29),
            # text only
            "March": None, "Feb": None, "Dec 5th or 6th": None, "Jan 22nd -24th 2027": None,
            "Feb 24&25th 2027": None, "10th & 11th Feb 2027": None, "June 22nd ,23rd \n2027": None, "Nil": None,
        }
        for text, expected in cases.items():
            parsed, kept, error = parse_event_date(text, TODAY)
            self.assertEqual((parsed, error), (expected, None), text)
            self.assertEqual(kept, " ".join(text.split()), text)               # as typed (spacing collapsed)
        self.assertEqual(parse_event_date("", TODAY), (None, None, None))
        self.assertEqual(parse_event_date("31-02-2026", TODAY)[2], "Not a real date")

    def test_phone_cells(self):
        self.assertEqual(split_phones("9800000002\n9000000002"), ["9800000002", "9000000002"])
        self.assertEqual(split_phones("9700000003,\n8200000003"), ["9700000003", "8200000003"])
        self.assertEqual(split_phones("9700000003 / 8200000003"), ["9700000003", "8200000003"])
        self.assertEqual(split_phones(9800000001.0), ["9800000001"])                 # Excel number
        for empty in (None, "", "nil", "NIL", " Nil "):
            self.assertEqual(split_phones(empty), [], empty)


class UploadTest(DhruvaTestBase, unittest.TestCase):
    def test_preview_first_sheet(self):
        resp = self.preview()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual((body["sheet"], body["sheets"], body["heading_row"]), ("Oct - Dec ", ["Oct - Dec ", "Dec -March 2027"], 2))
        self.assertEqual(body["columns"], {"Name": "name", "Venue": "venue", "Date": "date", "Contact No": "phone",
                                           "Event Type": "event_type"})                 # SL NO ignored
        rows = {r["row"]: r for r in body["rows"]}
        self.assertNotIn(15, rows)                                                      # the empty row
        self.assertEqual(body["counts"], {"total": 13, "valid": 11, "needs_fixing": 1, "duplicates": 1, "no_phone": 3,
                                          "duplicates_by_reason": {"in_file": 1, "existing": 0}})
        ravi = rows[4]
        self.assertEqual(ravi["values"], {"name": "Ravi Kumarfake", "venue": "Main palace", "date": "dec 25th",
                                          "phone": "9800000002", "alt_phone": "9000000002",
                                          "event_type": "engagement, wedding"})
        self.assertEqual(ravi["event_date"], "2026-12-25")
        self.assertEqual(rows[5]["values"]["venue"], "Main Palace")                     # trailing line break gone
        self.assertEqual((rows[5]["values"]["phone"], rows[5]["values"]["alt_phone"]), ("9700000003", "8200000003"))
        self.assertEqual(rows[5]["values"]["event_type"], "reception, muhurtham")
        self.assertEqual((rows[6]["status"], rows[6]["flags"]), ("ok", ["no_phone"]))   # no phone: allowed
        self.assertEqual((rows[7]["status"], rows[7]["flags"], rows[7]["event_date"]), ("ok", ["no_phone"], "2027-02-28"))
        self.assertEqual((rows[8]["event_date"], rows[8]["values"]["date"]), (None, "March"))
        self.assertEqual(rows[9]["values"]["venue"], "Shangrila or other One")
        self.assertEqual((rows[11]["event_date"], rows[11]["values"]["date"]), ("2026-12-20", "20 Dec 2026"))  # date cell
        self.assertEqual(rows[12]["event_date"], None)                                  # "Nil"
        self.assertEqual(rows[13]["duplicate"], {"reason": "in_file", "duplicate_of_row": 3})
        self.assertIsNone(rows[14]["duplicate"])                                    # no-phone rows aren't duplicates
        self.assertEqual((rows[16]["status"], [e["field"] for e in rows[16]["errors"]]), ("error", ["phone"]))

    def test_second_sheet_by_name(self):
        for name in ("Dec -March 2027", " dec -march 2027 "):                        # exact, or any case/spaces
            body = self.preview(sheet_name=name).json()
            self.assertEqual(body["sheet"], name)
            self.assertEqual(body["columns"]["MOBILE NO"], "phone")
            rows = {r["values"]["name"]: r for r in body["rows"]}
            self.assertEqual({r["status"] for r in rows.values()}, {"ok"})
            self.assertEqual(rows["Two Days"]["values"]["alt_phone"], "9000000102")
            self.assertEqual([r["event_date"] for r in body["rows"]],
                             [None, None, None, "2027-04-23", None, "2027-01-10"])
            self.assertEqual(rows["April Fake"]["flags"], ["no_phone"])
        resp = self.preview(sheet_name="Jan")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Oct - Dec", resp.json()["detail"])

    def test_bulk_save_then_everything_is_a_duplicate(self):
        result = self.upload_all()
        self.assertEqual({k: result[k] for k in ("added", "duplicates_skipped", "failed")}, {"added": 11, "duplicates_skipped": 1, "failed": 1})
        self.assertEqual(result["errors"][0]["field"], "phone")
        with Session(self.engine) as s:
            leads = {l.name: l for l in s.exec(select(DhruvaLead))}
        self.assertEqual(len(leads), 10)
        ravi = leads["Ravi Kumarfake"]
        self.assertEqual((ravi.phone_e164, ravi.alt_phone_e164, ravi.source, ravi.created_by),
                         ("+919800000002", "+919000000002", "bulk_upload", self.users["dhruva"].id))
        self.assertEqual((leads["Month Only"].event_date, leads["Month Only"].event_date_text), (None, "March"))
        self.assertIsNone(leads["No Phone One"].phone_e164)
        # The same file again: phone-bearing rows match; rows without a phone remain valid.
        counts = self.preview().json()["counts"]
        self.assertEqual((counts["valid"], counts["duplicates_by_reason"]["existing"]), (3, 9))
        self.assertEqual(self.upload_all()["added"], 3)

    def test_duplicates_by_alt_phone_and_archived(self):
        base = {"name": "A", "venue": "Hall", "date": "Dec 4th", "event_type": "wedding"}
        self.assertEqual(self.add(**{**base, "phone": "9800000301\n9800000302"}).status_code, 201)
        resp = self.add(**{**base, "phone": "9800000302"})                            # = A's alt phone
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"]["duplicate"]["reason"], "existing")
        resp = self.add(**{**base, "phone": "9800000399", "alt_phone": "+91 98000 00301"})  # alt = A's phone
        self.assertEqual(resp.status_code, 409)
        archived = {"name": "D", "venue": "Hall", "date": "Dec 5th", "event_type": "wedding", "phone": "9800000401"}
        lead_id = self.add(**archived).json()["id"]
        self.assertEqual(self.call("admin", "POST", f"/leads/{lead_id}/archive").status_code, 200)
        self.assertEqual(self.add(**archived).status_code, 409)                         # archived still counts
        self.assertEqual(self.add(**{**archived, "allow_duplicate": True}).status_code, 201)

    def test_duplicate_requires_same_normalized_identifying_fields(self):
        base = {"name": "Asha   Fake", "venue": "Main Palace", "date": "Dec 4th", "event_type": "Wedding"}
        self.assertEqual(self.add(**{**base, "phone": "9800000501"}).status_code, 201)
        for field, value in (("name", "Different Name"), ("venue", "Different Hall"),
                             ("date", "Dec 5th"), ("event_type", "Reception")):
            with self.subTest(field=field):
                resp = self.add(**{**base, field: value, "phone": "9800000501"})
                self.assertEqual(resp.status_code, 201, resp.text)
        equivalent = {"name": "  asha fake ", "venue": "main   palace", "date": " DEC   4TH ",
                      "event_type": " wedding ", "phone": "9800000501"}
        self.assertEqual(self.add(**equivalent).status_code, 409)

    def test_bulk_rechecks_the_new_duplicate_rule(self):
        same = {"name": "Same", "venue": "Hall", "date": "Dec 4th", "event_type": "Wedding", "phone": "9800000551"}
        other = {**same, "name": "Different"}
        resp = self.call("dhruva", "POST", "/leads/bulk", json={"leads": [same, same, other]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((resp.json()["added"], resp.json()["duplicates_skipped"]), (2, 1))

    def test_single_add_rules(self):
        resp = self.add(name="Solo", date="Dec 4th", phone="98000 00501", event_type="wedding \nreception")
        self.assertEqual(resp.status_code, 201, resp.text)
        lead = resp.json()
        self.assertEqual((lead["phone_e164"], lead["event_date"], lead["event_type"], lead["source"]),
                         ("+919800000501", "2026-12-04", "wedding, reception", "manual"))
        self.assertEqual(lead["update_status"], None)                                 # admin fields: read-only
        self.assertIn("status_updated_by", lead)                                      # status updater id is read-only
        for body, field in (({}, "name"), ({"venue": "x"}, "name"), ({"phone": "1\n2\n3"}, "phone"),
                            ({"name": "x", "phone": "12"}, "phone"), ({"name": "x" * 201}, "name")):
            resp = self.add(**body)
            self.assertEqual(resp.status_code, 422, body)
            self.assertIn(field, [e["field"] for e in resp.json()["detail"]], body)
        self.assertEqual(self.add(name="No Phone", date="Dec 9th").status_code, 201)
        self.assertEqual(self.add(name="no  phone", date="9-12-2026").status_code, 201)  # no phone means no duplicate
        self.assertEqual(self.add(name="No Phone", date="Dec 10th").status_code, 201)

    def test_csv_upload(self):
        wb = load_workbook(io.BytesIO(sample_workbook()))
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in wb["Oct - Dec "].iter_rows(values_only=True):
            writer.writerow(["" if v is None else (v.strftime("%d-%m-%Y") if hasattr(v, "strftime") else v) for v in row])
        body = self.preview(content=buf.getvalue().encode("utf-8"), filename="sample.csv").json()
        self.assertEqual(body["heading_row"], 2)
        self.assertEqual((body["counts"]["valid"], body["counts"]["duplicates"], body["counts"]["needs_fixing"]), (11, 1, 1))

    def test_template_and_example_row(self):
        for fmt in ("xlsx", "csv"):
            resp = self.call("dhruva", "GET", f"/leads/template?format={fmt}")
            self.assertEqual(resp.status_code, 200, fmt)
            body = self.preview(content=resp.content, filename=f"t.{fmt}").json()
            self.assertEqual(body["rows"][0]["status"], "error", fmt)
            self.assertIn("example row", body["rows"][0]["errors"][-1]["message"])
        headings = [c.value for c in load_workbook(io.BytesIO(self.call("admin", "GET", "/leads/template").content)).active[1]]
        self.assertEqual(headings, ["Name", "Venue", "Date", "Phone", "Alt phone", "Event type"])


class EditAccessTest(DhruvaTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.lead_id = self.add("dhruva2", name="Edit Me", phone="9800000601", date="Dec 4th").json()["id"]

    def test_dhruva_edits_basic_fields_of_any_lead(self):
        resp = self.call("dhruva", "PATCH", f"/leads/{self.lead_id}",
                         json={"venue": "New Venue", "date": "Jan 5th", "alt_phone": "9800000602"})
        self.assertEqual(resp.status_code, 200, resp.text)
        lead = resp.json()
        self.assertEqual((lead["venue"], lead["event_date"], lead["alt_phone_e164"], lead["name"]),
                         ("New Venue", "2027-01-05", "+919800000602", "Edit Me"))
        self.assertEqual(self.call("dhruva", "PATCH", f"/leads/{self.lead_id}", json={"name": None}).status_code, 200)
        self.assertEqual(self.call("dhruva", "PATCH", f"/leads/{self.lead_id}",
                                   json={"phone": None, "alt_phone": None}).status_code, 422)   # neither name nor phone

    def test_admin_fields_are_admin_only(self):
        for body in ({"update_status": "converted"}, {"quoted_amount": 1}, {"follow_up_date": "2026-12-01"},
                     {"additional_comments": "x"}, {"conversion_amount": 5}, {"venue": "x", "quoted_amount": 1}):
            resp = self.call("dhruva", "PATCH", f"/leads/{self.lead_id}", json=body)
            self.assertEqual(resp.status_code, 403, body)
        with Session(self.engine) as s:
            self.assertIsNone(s.get(DhruvaLead, self.lead_id).venue)                  # nothing saved
        resp = self.call("admin", "PATCH", f"/leads/{self.lead_id}", json={
            "update_status": "will_call_back", "quoted_amount": 150000, "conversion_amount": 0,
            "follow_up_date": "2026-12-01", "additional_comments": "Call after 6", "venue": "Admin Venue"})
        self.assertEqual(resp.status_code, 200, resp.text)
        lead = resp.json()
        self.assertEqual((lead["update_status"], lead["status_updated_by"], lead["status_updated_by_name"], lead["venue"]),
                         ("will_call_back", self.users["admin"].id, "Admin", "Admin Venue"))
        seen = self.call("dhruva", "GET", f"/leads/{self.lead_id}").json()             # read-only for Dhruva
        self.assertEqual((seen["update_status"], seen["quoted_amount"], seen["status_updated_by_name"]),
                         ("will_call_back", 150000, "Admin"))
        self.assertEqual((seen["additional_comments"], seen["conversion_amount"], seen["follow_up_date"],
                          seen["status_updated_by"], seen["status_updated_at"]),
                         ("Call after 6", 0, "2026-12-01", self.users["admin"].id, lead["status_updated_at"]))
        listed = self.call("dhruva", "GET", "/leads").json()["items"]
        listed_lead = next(item for item in listed if item["id"] == self.lead_id)
        self.assertEqual(listed_lead["status_updated_by"], self.users["admin"].id)
        csv_export = self.call("dhruva", "GET", "/leads/export", params={"format": "csv"}).text
        self.assertIn("Updated by ID", csv_export)
        self.assertIn(str(self.users["admin"].id), csv_export)
        for body in ({"update_status": "maybe"}, {"quoted_amount": -1}, {"unknown": 1}):
            self.assertEqual(self.call("admin", "PATCH", f"/leads/{self.lead_id}", json=body).status_code, 422, body)

    def test_phone_change_checks_duplicates(self):
        fields = {"name": "Other", "venue": "Hall", "date": "Dec 4th", "event_type": "wedding", "phone": "9800000699"}
        self.add("dhruva", name="Edit Me", date="Dec 4th", phone="9800000699")
        resp = self.call("dhruva", "PATCH", f"/leads/{self.lead_id}", json={"phone": "9800000699"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.call("dhruva", "PATCH", f"/leads/{self.lead_id}",
                                   json={"phone": "9800000699", "allow_duplicate": True}).status_code, 200)
        self.assertEqual(self.call("dhruva", "PATCH", f"/leads/{self.lead_id}", json={"name": "Renamed"}).status_code, 200)

    def test_basic_field_edit_checks_duplicate_even_when_phone_is_unchanged(self):
        first = {"name": "First", "venue": "Hall", "date": "Dec 4th", "event_type": "Wedding", "phone": "9800000688"}
        self.add("dhruva", **first)
        target = self.add("dhruva", **{**first, "name": "Second"}).json()["id"]
        resp = self.call("dhruva", "PATCH", f"/leads/{target}", json={"name": " First "})
        self.assertEqual(resp.status_code, 409, resp.text)
        resp = self.call("dhruva", "PATCH", f"/leads/{target}", json={"name": " First ", "allow_duplicate": True})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_archive_is_admin_only_and_roles(self):
        self.assertEqual(self.call("dhruva", "POST", f"/leads/{self.lead_id}/archive").status_code, 403)
        resp = self.call("admin", "POST", f"/leads/{self.lead_id}/archive")
        self.assertEqual((resp.status_code, resp.json()["archived"]), (200, True))
        self.assertEqual(self.call("admin", "POST", f"/leads/{self.lead_id}/unarchive").json()["archived"], False)
        self.assertEqual(self.call("admin", "GET", "/leads/999").status_code, 404)
        # Vendor logins: 403 on every Dhruva endpoint
        for method, url in (("GET", f"/leads/{self.lead_id}"), ("PATCH", f"/leads/{self.lead_id}"), ("POST", "/leads"),
                            ("POST", "/leads/bulk"), ("POST", "/upload/preview"), ("GET", "/leads/template")):
            self.assertEqual(self.call("phodeo", method, url, json={}).status_code, 403, (method, url))
        # Admins add and upload too
        self.assertEqual(self.add("admin", name="By Admin", phone="9800000700").status_code, 201)
        self.assertEqual(self.upload_all("admin")["added"], 11)


if __name__ == "__main__":
    unittest.main()
