"""Upload and single-add rules in every section: duplicates = every field the same, only the phone
required, N/A-like values empty; the KOC -> Dhruva cut-off and upload; only active vendors can be
chosen. In-memory SQLite and the fake workflow database only. Run:

    python -m unittest discover -s tests -t . -v
"""
import io
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from openpyxl import Workbook, load_workbook
from sqlmodel import Session, select

import tests.test_crm_leads_api as base
from tests.test_crm_leads_api import API, NOW, XLSX, ApiTestBase, SAMPLE
from tests.test_koc_to_dhruva import KocTestBase, row

from app.core import response_cache
from app.core.config import settings
from app.models.crm_spin_lead import CrmSpinLead
from app.models.koc_dhruva_lead import KocDhruvaLead
from app.models.lead_meta import LeadMeta
from app.models.vendor import Vendor
from app.models.vendor_to_koc import VendorToKocLead
from app.models.wedding_lead import WeddingLead
from app.services import lead_values

K2D = "/koc-to-dhruva"


def xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Shared value rules ─────────────────────────────────────────────────
class ValueRuleTests(unittest.TestCase):
    def test_na_like_values_are_empty(self):
        for value in (None, "", "  ", "N/A", "n/a", "NA", " na ", "-", "--", "nil", "NIL", "none", "None", "null",
                      ".", "n / a"):
            self.assertTrue(lead_values.is_na(value), repr(value))
        for value in ("Nalini", "0", 0, "-5", "N/A road", "Na Hall"):
            self.assertFalse(lead_values.is_na(value), repr(value))

    def test_duplicate_comparison_normalises(self):
        n = lead_values.dup_norm
        self.assertEqual(n("  Asha   RAO "), n("asha rao"))
        self.assertEqual(n("N/A"), n(None))
        self.assertEqual(n("20-12-2026", "date"), n(date(2026, 12, 20), "date"))
        self.assertEqual(n("20 Dec 2026", "date"), "2026-12-20")
        self.assertEqual(n("2026-12-20", "date"), n(datetime(2026, 12, 20, 10, 30), "date"))
        self.assertEqual(n("1,500", "int"), n(1500.0, "int"))
        self.assertEqual(n("10_20", "range"), n("₹10–20", "range"))
        self.assertNotEqual(n("Asha"), n("Asha R"))


# ── Required fields and N/A in the KOC -> Vendors uploads and adds ─────
class RequiredAndNaTests(ApiTestBase, unittest.TestCase):
    def test_only_the_phone_is_required_and_na_values_are_empty(self):
        resp = self.add("wedding", {"phone": "9845030001"})                       # phone only
        self.assertEqual(resp.status_code, 201, resp.text)
        resp = self.add("wedding", {"phone": "9845030002", "email": "N/A", "event_date": "-", "budget": "nil",
                                    "city": "none", "submitted_at": "."})
        self.assertEqual(resp.status_code, 201, resp.text)
        values = resp.json()["lead"]["values"]
        self.assertEqual((values["email"], values["event_date"], values["budget"], values["city"]),
                         (None, None, None, None))
        self.assertTrue(values["submitted_at"])                                    # empty = now
        resp = self.add("wedding", {"phone": "N/A", "name": "No phone"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual([e["field"] for e in resp.json()["detail"]["errors"]], ["phone"])
        resp = self.add("wedding", {"phone": "9845030003", "email": "abc@"})       # unreadable: still an error
        self.assertEqual([e["field"] for e in resp.json()["detail"]["errors"]], ["email"])
        row = self.import_spin({"phone": "9845030004", "email": "N/A", "occasionDate": "-", "quantityRequired": "nil",
                                "pincode": "NA", "created_at": "", "assigned_to": "Automatic"})
        self.assertEqual(row["status"], "imported", row)
        with Session(self.engine) as s:
            lead = s.exec(select(CrmSpinLead).where(CrmSpinLead.phone == "9845030004")).one()
            self.assertEqual((lead.email, lead.additional_details), (None, {}))
            self.assertIsNotNone(lead.created_at)                                 # empty Created = upload time
        bad = self.import_spin({"phone": "9845030005", "occasionDate": "32-13-2026"})
        self.assertEqual((bad["status"], [e["field"] for e in bad["errors"]]), ("error", ["occasionDate"]))

    def test_same_phone_other_field_is_not_a_duplicate_in_every_source(self):
        for source in ("whatsapp", "wedding", "wedding_planner"):
            with self.subTest(source=source):
                self.assertEqual(self.add(source, SAMPLE[source]).status_code, 201)
                field = "country" if source == "whatsapp" else "name"
                self.assertEqual(self.add(source, {**SAMPLE[source], field: "Other"}).status_code, 201)
                same = {**SAMPLE[source], field: f"  {str(SAMPLE[source][field]).upper()} "}  # case and spaces
                self.assertEqual(self.add(source, same).status_code, 409)


# ── Vendor -> KOC and Dhruva ───────────────────────────────────────────
class VendorToKocAndDhruvaTests(KocTestBase):
    def vtk_add(self, **body):
        with self.logged_in("phodeo"):
            return self.client.post(f"{API}/vendor-to-koc/leads", json=body)

    def test_vendor_to_koc_every_field_and_na(self):
        self.assertEqual(self.vtk_add(name="Asha", phone="9845040001", city="Pune").status_code, 201)
        self.assertEqual(self.vtk_add(name="Asha", phone="9845040001", city="Mumbai").status_code, 201)  # other city
        self.assertEqual(self.vtk_add(name=" asha ", phone="+91 98450 40001", city="PUNE").status_code, 409)
        self.assertEqual(self.vtk_add(name="N/A", phone="9845040002", city="-").status_code, 201)
        with Session(self.engine) as s:
            lead = s.exec(select(VendorToKocLead).where(VendorToKocLead.phone == "9845040002")).one()
            self.assertEqual((lead.name, lead.city), (None, None))
        self.assertEqual(self.vtk_add(name="x", phone="nil").status_code, 422)          # phone still required
        self.assertFalse([c for c in VendorToKocLead.__table__.constraints
                          if type(c).__name__ == "UniqueConstraint"], "no unique (vendor_id, phone)")

    def test_dhruva_keeps_name_or_phone_and_na_is_empty(self):
        resp = self.add(name="Only Name", venue="N/A", event_type="-")
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual((resp.json()["venue"], resp.json()["event_type"]), (None, None))
        self.assertEqual(self.add(venue="Hall").status_code, 422)                        # neither name nor phone
        self.assertEqual(self.add(name="only  name").status_code, 409)                   # every field the same
        self.assertEqual(self.add(name="Phone A", phone="9845060001").status_code, 201)
        self.assertEqual(self.add(name="Phone B", phone="9845060001").status_code, 201)  # same phone, other name
        self.assertEqual(self.add(name="phone b", phone="+91 98450 60001").status_code, 409)


# ── KOC -> Dhruva: cut-off and upload ──────────────────────────────────
KOC_ROWS = [
    row(701, "9845071001", "70-100", 500, created="2026-11-21T06:00:00"),              # live
    row(702, "9845071002", "100-200", None, created="2026-11-01T06:00:00"),            # before the cut-off
]
HEAD = ["Name", "Phone", "Email", "Occasion", "Event date", "Branch", "Estimated budget per card", "Quantity required",
        "Received", "Update", "Additional comments", "Quoted amount", "Conversion amount", "Follow-up date",
        "Pincode", "Other wedding services", "Sl. No", "Hot"]


def koc_line(name, phone, budget, qty, **extra):
    values = {"Name": name, "Phone": phone, "Occasion": "Wedding", "Event date": "20-12-2026",
              "Estimated budget per card": budget, "Quantity required": qty, "Pincode": "560011",
              "Other wedding services": "Photography & Videography", **extra}
    return [values.get(h) for h in HEAD]


class KocUploadTests(KocTestBase):
    def setUp(self):
        super().setUp()
        self.patches2 = [mock.patch.object(base, "WORKFLOW_ROWS", KOC_ROWS),
                         mock.patch.object(settings, "SPIN_LIVE_FROM", date(2026, 11, 21))]
        for p in self.patches2:
            p.start()
        response_cache.clear()

    def tearDown(self):
        for p in self.patches2:
            p.stop()
        super().tearDown()

    def upload(self, who="admin"):
        content = xlsx([
            HEAD,
            koc_line("History", "9845071002", "100-200", 300, **{"Received": "01-10-2026 10:00"}),  # 2 hidden 702's phone
            koc_line("Lead 701", "9845071001", "70-100", 500),                                     # 3 = live workflow 701
            koc_line("Small", "9845071003", "10-20", 100, **{"Update": "Interested", "Additional comments": "Call",
                     "Quoted amount": "5,000", "Follow-up date": "25-11-2026", "Sl. No": 4}),      # 4 ok (not the rule)
            koc_line(" small ", "+91 98450 71003", "10_20", "100"),                                 # 5 = row 4
            koc_line("No phone", None, "70-100", 10),                                              # 6 phone required
            koc_line("Blanks", "9845071004", "N/A", "-", **{"Email": "N/A", "Event date": "-"}),   # 7 ok, empty values
            koc_line("Other name", "9845071003", "10-20", 100),                                     # 8 same phone only
        ])
        with self.logged_in(who):
            return self.client.post(f"{API}{K2D}/leads/import/preview", files={"file": ("k.xlsx", content, XLSX)})

    def test_cutoff_preview_save_and_listing(self):
        self.assertEqual(self.keys(), {"701"})                                    # 702 (history) hidden
        self.get(f"{K2D}/leads/702", status=404)
        preview = self.upload()
        self.assertEqual(preview.status_code, 200, preview.text)
        body = preview.json()
        status = {r["row"]: r["status"] for r in body["rows"]}
        self.assertEqual(status, {2: "ok", 3: "duplicate", 4: "ok", 5: "duplicate", 6: "error", 7: "ok", 8: "ok"})
        rows = {r["row"]: r for r in body["rows"]}
        self.assertEqual(rows[3]["duplicate"], {"reason": "existing_workflow", "lead_key": "701"})
        self.assertEqual(rows[5]["duplicate"], {"reason": "in_file", "duplicate_of_row": 4})
        self.assertEqual(rows[6]["errors"][0]["field"], "phone")
        self.assertEqual(set(body["read_only_columns"]), {"Sl. No", "Hot"})
        with self.logged_in("admin"):
            saved = self.client.post(f"{API}{K2D}/leads/import", json={"rows": [
                {"row": r["row"], "values": r["values"]} for r in body["rows"]]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["counts"]["imported"], saved.json()["counts"]["skipped"]), (4, 3))
        with Session(self.engine) as s:
            uploaded = {u.name: u for u in s.exec(select(KocDhruvaLead))}
            meta = s.exec(select(LeadMeta).where(LeadMeta.source == "koc_to_dhruva")).one()
            self.assertEqual(s.exec(select(CrmSpinLead)).all(), [])               # never a spin lead
        self.assertEqual(set(uploaded), {"History", "Small", "Blanks", "Other name"})
        small = uploaded["Small"].lead_key
        self.assertTrue(small.startswith("kd-"))
        self.assertEqual((meta.lead_key, meta.update_status, meta.notes, meta.quoted_amount, meta.follow_up_date,
                          meta.vendor_id), (small, "interested", "Call", 5000, date(2026, 11, 25), None))
        self.assertEqual(uploaded["Blanks"].email, None)
        self.assertEqual(uploaded["History"].created_at.date(), date(2026, 10, 1))
        # Listed (every uploaded row, also below the rule), stats, export, details, dashboard
        items = {i["lead_key"]: i for i in self.get(f"{K2D}/leads", page_size=100)["items"]}
        self.assertEqual(set(items), {"701", *(u.lead_key for u in uploaded.values())})
        self.assertEqual(items[small]["potential_value"], 1000)                    # 10 x 100, only shown
        self.assertEqual(items[small]["crm"]["update_status"], "interested")
        self.assertFalse({"reason", "reason_label"} & set(items[small]))
        self.assertEqual(self.get(f"{K2D}/leads/stats")["total"], 5)
        self.assertEqual(self.export_count(), 5)
        self.assertEqual(self.get(f"{K2D}/leads/{small}")["lead_key"], small)
        self.assertEqual(self.get("/analytics/koc-to-dhruva")["kpis"]["shared"], 5)
        # A second upload of the same file: everything already there
        again = self.upload().json()["counts"]
        self.assertEqual((again["ok"], again["duplicates"]), (0, 6))

    def test_crm_hot_activity_and_reminders_on_uploaded_rows(self):
        body = self.upload().json()
        with self.logged_in("admin"):
            self.client.post(f"{API}{K2D}/leads/import", json={"rows": [
                {"row": r["row"], "values": r["values"]} for r in body["rows"]]})
        with Session(self.engine) as s:
            key = s.exec(select(KocDhruvaLead).where(KocDhruvaLead.name == "Blanks")).one().lead_key
        for who in ("admin", "dhruva"):
            resp = self.put(key, {"update_status": "will_call_back", "notes": who, "quoted_amount": 900,
                                  "quoted_date": "2026-11-20", "conversion_amount": None,
                                  "follow_up_date": "2026-11-20"}, who)
            self.assertEqual(resp.status_code, 200, resp.text)
            with self.logged_in(who):
                self.assertTrue(self.client.post(f"{API}{K2D}/leads/{key}/hot").json()["is_hot"])
                self.assertEqual(self.client.delete(f"{API}{K2D}/leads/{key}/hot").status_code, 200)
        self.assertEqual(self.get(f"{K2D}/leads/{key}/crm")["notes"], "dhruva")
        actions = {a["action"] for a in self.get(f"{K2D}/leads/{key}/activity")["items"]}
        self.assertTrue({"created", "field_changed", "hot_on", "hot_off"} <= actions)
        reminders = self.get("/dhruva/reminders/follow-ups", "dhruva", days=30)["items"]
        self.assertIn(key, [i["lead_key"] for i in reminders if i["source"] == "koc_to_dhruva"])
        # also_in: an International lead with the uploaded row's phone (admins see KOC -> Dhruva)
        with self.logged_in("admin"):
            added = self.client.post(f"{API}/crm-leads/whatsapp", json={"values": {"phone": "+919845071004"}})
        self.assertEqual(added.status_code, 201, added.text)
        item = next(i for i in self.list_items("whatsapp") if i["phone"] == "919845071004")
        self.assertIn("koc_to_dhruva", {a["source"] for a in item["also_in"]})

    def test_only_admins_upload_and_the_template_matches_the_export(self):
        for who in ("dhruva", "phodeo", "viewer"):
            self.assertEqual(self.upload(who).status_code, 403, who)
        with self.logged_in("viewer"):
            self.assertEqual(self.client.post(f"{API}{K2D}/leads/import", json={"rows": []}).status_code, 403)
        with self.logged_in("admin"):
            template = self.client.get(f"{API}{K2D}/leads/template")
            export = self.client.get(f"{API}{K2D}/leads/export")
        headers = [c.value for c in load_workbook(io.BytesIO(template.content))["Leads"][1]]
        export_headers = [c.value for c in load_workbook(io.BytesIO(export.content)).active[1]]
        self.assertEqual(headers, [h for h in export_headers if h != "Sl. No"])


# ── Only active vendors can be chosen ──────────────────────────────────
class ActiveVendorTests(ApiTestBase, unittest.TestCase):
    def test_inactive_vendor_is_refused_but_keeps_its_name(self):
        key = self.add_ok("wedding")["lead_key"]
        self.assertEqual(self.put_crm("admin", "wedding", key, {"vendor_id": self.vendor_ids["rj"]}).status_code, 200)
        with Session(self.engine) as s:
            rj = s.get(Vendor, self.vendor_ids["rj"])
            rj.is_active = False
            s.add(rj)
            s.commit()
        response_cache.clear()
        item = next(i for i in self.list_items("wedding") if i["crm"]["lead_key"] == key)
        self.assertEqual((item["crm"]["vendor"]["name"], item["crm"]["vendor_source"]), ("RJ", "manual"))
        resp = self.put_crm("admin", "wedding", key, {"vendor_id": self.vendor_ids["rj"]})
        self.assertEqual((resp.status_code, resp.json()["detail"]), (422, "This vendor is inactive; choose an active vendor."))
        self.assertEqual(self.put_crm("admin", "wedding", key, {"vendor_id": 999}).status_code, 404)
        vendors = self.client.get(f"{API}/vendors", params={"include_inactive": "true"}).json()
        self.assertEqual({v["name"]: v["is_active"] for v in vendors}["RJ"], False)
        self.assertNotIn("RJ", [v["name"] for v in self.client.get(f"{API}/vendors").json()])


if __name__ == "__main__":
    unittest.main()
