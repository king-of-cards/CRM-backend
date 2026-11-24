"""Vendor access: who sees and edits which lead, per-role exports, and uploads' "Assigned to".

Runs against an in-memory SQLite database with a fake workflow database (see
test_crm_leads_api.ApiTestBase). Run from the project root:

    python -m unittest discover -s tests -t . -v

Assignments in the fixtures (vendors RJ, Phodeo, OHP; one login each):
  whatsapp         WA_OHP (area rules, fallback)  WA_RJ (manual)        WA_NONE (no country)
  wedding          WD_PHODEO (city Bangalore)     WD_RJ (leads shared)  WD_NONE (no city)
  wedding_planner  WP_OHP (city Udaipur)          WP_PHODEO (leads shared)  WP_NONE (no city)
  spin             101 (Chamrajpet branch: RJ)    102 (pincode 560034: Phodeo)  103 (nothing)
                   + one added spin lead at the Jayanagar branch (Phodeo)
"""
import io
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from openpyxl import load_workbook
from sqlmodel import Session, select

import tests.test_crm_leads_api as base
from app.models.lead_meta import LeadMeta
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.services.lead_crm import assigned_label
from tests.test_crm_leads_api import API, NOW, XLSX, ApiTestBase, xlsx_file

T = datetime(2026, 11, 14, 5, 0, tzinfo=timezone.utc)      # 14 Nov 2026, 10:30 am IST
EVENT = date(2026, 12, 20)

WORKFLOW_ROWS = [
    {"id": 101, "name": "Spin RJ", "phone": "9845400101", "created_at": "2026-11-01T04:30:00", "branch_id": 1,
     "additional_details.pincode": "560018", "additional_details.occasionDate": "2026-12-20",
     "additional_details.estimatedBudgetPerCard": "10-20"},
    {"id": 102, "name": "Spin Phodeo", "phone": "9845400102", "created_at": "2026-11-02T04:30:00", "branch_id": None,
     "additional_details.pincode": "560034", "additional_details.occasionDate": None,
     "additional_details.estimatedBudgetPerCard": "20_30"},
    {"id": 103, "name": "Spin nobody", "phone": "9845400103", "created_at": "2026-11-03T04:30:00", "branch_id": None,
     "additional_details.pincode": None, "additional_details.occasionDate": None,
     "additional_details.estimatedBudgetPerCard": None},
]

HEADERS = {
    "spin": ["Lead", "Email", "Contact", "Branch", "Branch code", "Location", "Occasion", "Event date", "Budget",
             "Other wedding services", "Quantity required", "Assigned to", "Vendor updates", "Update", "Quoted amount", "Quoted date", "Conversion amount", "Created", "Hot"],
    "whatsapp": ["Lead", "Country", "Event date", "Assigned to", "Vendor updates", "Update", "Quoted amount", "Quoted date",
                 "Conversion amount", "Submitted", "Hot"],
    "wedding": ["Lead", "Email", "Contact", "Occasion", "Event date", "City", "Budget", "Assigned to", "Vendor updates", "Update",
                "Quoted amount", "Quoted date", "Conversion amount", "Submitted", "Hot"],
    "wedding_planner": ["Lead", "Role", "Email", "Contact", "Wedding date", "Wedding city", "Guests",
                        "Guests staying", "Budget", "Budget range", "Estimate", "Fits budget", "Assigned to",
                        "Vendor updates", "Update", "Quoted amount", "Quoted date", "Conversion amount", "Updated", "Last step", "Hot"],
}


class VendorAccessTest(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.workflow = mock.patch.object(base, "WORKFLOW_ROWS", WORKFLOW_ROWS)
        self.workflow.start()
        sheet = {"origin": "sheet", "row_hash": "", "first_seen_at": NOW, "updated_at": NOW}
        with Session(self.engine) as s:
            s.add_all([
                WhatsAppLead(sheet_key="WA_OHP", country="UAE", phone="971500000001", phone_e164="+971500000001",
                             event_date=EVENT, event_date_text="20/12/2026", submitted_at=T, **sheet),
                WhatsAppLead(sheet_key="WA_RJ", country="UAE", phone="971500000002", phone_e164="+971500000002",
                             submitted_at=T, **sheet),
                WhatsAppLead(sheet_key="WA_NONE", country=None, phone="971500000003", phone_e164="+971500000003",
                             submitted_at=T, **sheet),
                WeddingLead(sheet_key="WD_PHODEO", name="Wed Phodeo", city="Bangalore", phone="919845100001",
                            phone_e164="+919845100001", event_date=EVENT, event_date_text="12/20/2026",
                            budget_amount=150000, budget_text="1.5 lakh", submitted_at=T, **sheet),
                WeddingLead(sheet_key="WD_RJ", name="Wed RJ", city="Mysore", shared_with="RJ", phone="919845100002",
                            phone_e164="+919845100002", submitted_at=T, **sheet),
                WeddingLead(sheet_key="WD_NONE", name="Wed nobody", phone="919845100003", phone_e164="+919845100003",
                            submitted_at=T, **sheet),
                WeddingPlan(plan_id="WP_OHP", name="Plan OHP", city="Udaipur", phone="919845200001",
                            phone_e164="+919845200001", wedding_month="December 2027", total_budget=2500000,
                            plan_estimate=2310000, fits_budget=True, last_step="pdf", updated_in_sheet_at=T, **sheet),
                WeddingPlan(plan_id="WP_PHODEO", name="Plan Phodeo", shared_with="Phodeo", phone="919845200002",
                            phone_e164="+919845200002", first_day=date(2027, 1, 15), wedding_date=date(2027, 1, 15),
                            updated_in_sheet_at=T, **sheet),
                WeddingPlan(plan_id="WP_NONE", name="Plan nobody", phone="919845200003", phone_e164="+919845200003",
                            updated_in_sheet_at=T, **sheet),
                LeadMeta(source="whatsapp", lead_key="WA_RJ", vendor_id=self.vendor_ids["rj"], updated_at=NOW),
            ])
            s.commit()
        self.added_spin = self.add_ok("spin", {"phone": "9845400199", "name": "Added Phodeo", "branch": "Jayanagar"})["lead_key"]
        self.expected = {
            "phodeo": {"whatsapp": set(), "wedding": {"WD_PHODEO"}, "wedding_planner": {"WP_PHODEO"},
                       "spin": {"102", self.added_spin}},
            "rj": {"whatsapp": {"WA_RJ"}, "wedding": {"WD_RJ"}, "wedding_planner": set(), "spin": {"101"}},
            "ohp": {"whatsapp": {"WA_OHP"}, "wedding": set(), "wedding_planner": {"WP_OHP"}, "spin": set()},
        }
        self.unassigned = {"whatsapp": "WA_NONE", "wedding": "WD_NONE", "wedding_planner": "WP_NONE", "spin": "103"}

    def tearDown(self):
        self.workflow.stop()
        super().tearDown()

    def keys(self, items: list[dict]) -> set[str]:
        return {str(i["crm"]["lead_key"]) for i in items}

    # ── Vendor A can't see vendor B's leads ──
    def test_vendor_sees_only_own_leads_everywhere(self):
        for who, per_source in self.expected.items():
            with self.logged_in(who):
                overview = self.client.get(f"{API}/analytics/overview").json()
                for source, expected in per_source.items():
                    with self.subTest(who=who, source=source):
                        resp = self.client.get(f"{API}{base.LIST_URL[source]}", params={"page_size": 100})
                        self.assertEqual(self.keys(resp.json()["items"]), expected)
                        self.assertEqual(resp.json()["total"], len(expected))
                        self.assertEqual(len(self.export_rows(source)) - 1, len(expected))       # minus the header
                        if source != "spin":
                            self.assertEqual(self.client.get(f"{API}{base.STATS_URL[source]}").json()["total"], len(expected))
                            if expected:
                                self.assertEqual(overview["totals"][source], len(expected))
                            else:                       # no lead in that source: left out of the overview
                                self.assertNotIn(source, overview["totals"])
                                self.assertNotIn(source, overview["sources"])
                # Asking for another vendor's leads doesn't help
                resp = self.client.get(f"{API}/spin-leads", params={"vendor_id": self.vendor_ids["rj"]})
                self.assertEqual(self.keys(resp.json()["items"]), per_source["spin"])

        # Direct requests for another vendor's lead, or an unassigned one: 404
        for source, key in (("wedding", "WD_RJ"), ("whatsapp", "WA_RJ"), ("spin", "101")):
            with self.logged_in("phodeo"):
                self.assertEqual(self.client.get(f"{API}/leads/{source}/{key}/crm").status_code, 404)
            self.assertEqual(self.put_crm("phodeo", source, key, {"notes": "x"}).status_code, 404)
            self.assertEqual(self.put_crm("phodeo", source, key, {"update_status": "interested"}).status_code, 404)
        for source, key in self.unassigned.items():
            for who in ("phodeo", "rj", "ohp"):
                with self.logged_in(who):
                    self.assertEqual(self.client.get(f"{API}/leads/{source}/{key}/crm").status_code, 404, (who, source))
            # …but admins see them
            self.assertIn(key, self.keys(self.list_items(source)))
            self.assertEqual(self.client.get(f"{API}/leads/{source}/{key}/crm").status_code, 200)

    def test_listed_leads_match_the_assigned_chip(self):
        """What a vendor sees is exactly what the admin list shows as assigned to that vendor."""
        for source in base.LIST_URL:
            admin_items = self.list_items(source)
            for who in ("phodeo", "rj", "ohp"):
                shown = {str(i["crm"]["lead_key"]) for i in admin_items
                         if (i["crm"]["vendor"] or {}).get("id") == self.vendor_ids[who]}
                with self.logged_in(who):
                    self.assertEqual(self.keys(self.list_items(source)), shown, (source, who))

    # ── Editing ──
    def test_assigned_vendor_edits_update_quote_conversion_and_notes(self):
        body = {"update_status": "will_call_back", "quoted_amount": 45000, "quoted_date": "2026-11-20",
                "conversion_amount": 42000}
        resp = self.put_crm("phodeo", "wedding", "WD_PHODEO", body)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.put_crm("phodeo", "wedding", "WD_PHODEO", {"notes": "Called"}).status_code, 200)
        with Session(self.engine) as s:
            meta = s.exec(select(LeadMeta).where(LeadMeta.lead_key == "WD_PHODEO")).one()
            self.assertEqual((meta.update_status, meta.quoted_amount, meta.quoted_date, meta.conversion_amount, meta.notes),
                             ("will_call_back", 45000, date(2026, 11, 20), 42000, "Called"))
            self.assertEqual(meta.updated_by, self.users["phodeo"].id)
        # Another vendor: the lead doesn't exist for them
        self.assertEqual(self.put_crm("rj", "wedding", "WD_PHODEO", {"update_status": "interested"}).status_code, 404)
        self.assertEqual(self.put_crm("rj", "wedding", "WD_PHODEO", {"notes": "x"}).status_code, 404)
        # Admins may edit notes
        self.assertEqual(self.put_crm("admin", "wedding", "WD_PHODEO", {"notes": "Admin note"}).status_code, 200)
        with Session(self.engine) as s:
            self.assertEqual(s.exec(select(LeadMeta).where(LeadMeta.lead_key == "WD_PHODEO")).one().updated_by,
                             self.users["admin"].id)

    def test_admin_can_edit_update_quote_and_conversion(self):
        leads = {"wedding": "WD_PHODEO", "whatsapp": "WA_OHP", "wedding_planner": "WP_OHP", "spin": "102"}
        body = {"update_status": "converted", "quoted_amount": 1000, "quoted_date": "2026-11-20", "conversion_amount": 900}
        for source, key in leads.items():
            resp = self.put_crm("admin", source, key, body)
            self.assertEqual(resp.status_code, 200, (source, resp.text))
            with Session(self.engine) as s:
                meta = s.exec(select(LeadMeta).where(LeadMeta.source == source, LeadMeta.lead_key == key)).one()
                self.assertEqual((meta.update_status, meta.quoted_amount, meta.quoted_date, meta.conversion_amount),
                                 ("converted", 1000, date(2026, 11, 20), 900), source)
                self.assertEqual(meta.updated_by, self.users["admin"].id, source)       # who changed it
        # The assigned vendor can still change them; updated_by follows the last change
        resp = self.put_crm("phodeo", "wedding", "WD_PHODEO", {"update_status": None, "conversion_amount": None})
        self.assertEqual(resp.status_code, 200, resp.text)
        with Session(self.engine) as s:
            meta = s.exec(select(LeadMeta).where(LeadMeta.lead_key == "WD_PHODEO")).one()
            self.assertEqual((meta.update_status, meta.conversion_amount, meta.updated_by), (None, None, self.users["phodeo"].id))

    def test_vendor_cannot_assign(self):
        for body in ({"vendor_id": self.vendor_ids["phodeo"]}, {"vendor_id": None}):
            resp = self.put_crm("phodeo", "wedding", "WD_PHODEO", body)
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json()["detail"], "Only an admin can assign leads.")
        # Admin reassigns: visibility follows immediately
        self.assertEqual(self.put_crm("admin", "wedding", "WD_PHODEO", {"vendor_id": self.vendor_ids["rj"]}).status_code, 200)
        with self.logged_in("phodeo"):
            self.assertNotIn("WD_PHODEO", self.keys(self.list_items("wedding")))
        with self.logged_in("rj"):
            self.assertIn("WD_PHODEO", self.keys(self.list_items("wedding")))
        # Back to automatic (null): Phodeo again
        self.assertEqual(self.put_crm("admin", "wedding", "WD_PHODEO", {"vendor_id": None}).status_code, 200)
        with self.logged_in("phodeo"):
            self.assertIn("WD_PHODEO", self.keys(self.list_items("wedding")))

    def test_vendor_cannot_use_admin_endpoints(self):
        added = self.add_ok("wedding", {"phone": "9845500001", "city": "Bangalore"})["lead_key"]
        upload = xlsx_file([["Contact"], ["9845500002"]])
        with self.logged_in("phodeo"):
            calls = [
                self.client.post(f"{API}/crm-leads/wedding", json={"values": {"phone": "9845500003"}}),
                self.client.get(f"{API}/crm-leads/wedding/template"),
                self.client.get(f"{API}/crm-leads/wedding/fields"),
                self.client.post(f"{API}/crm-leads/wedding/import/preview", files={"file": ("u.xlsx", upload, XLSX)}),
                self.client.post(f"{API}/crm-leads/wedding/import", json={"rows": [{"row": 2, "values": {"phone": "9845500004"}}]}),
                self.client.get(f"{API}/crm-leads/wedding/{added}"),
                self.client.patch(f"{API}/crm-leads/wedding/{added}", json={"values": {"name": "x"}}),
                self.client.post(f"{API}/crm-leads/wedding/{added}/archive"),
                self.client.post(f"{API}/crm-leads/wedding/{added}/unarchive"),
                self.client.post(f"{API}/vendors", json={"name": "New"}),
                self.client.patch(f"{API}/vendors/{self.vendor_ids['phodeo']}", json={"name": "x"}),
                self.client.get(f"{API}/vendors/stats"),
                self.client.patch(f"{API}/branches/1", json={"name": "x"}),
                self.client.get(f"{API}/users"),
            ]
            self.assertEqual([r.status_code for r in calls], [403] * len(calls))
            # Adding / uploading on all 4 KOC -> Vendors sources stays admin-only (spin single add included)
            for source in ("spin", "whatsapp", "wedding", "wedding_planner"):
                adds = [
                    self.client.post(f"{API}/crm-leads/{source}", json={"values": {"phone": "9845500005"}}),
                    self.client.get(f"{API}/crm-leads/{source}/template"),
                    self.client.post(f"{API}/crm-leads/{source}/import/preview", files={"file": ("u.xlsx", upload, XLSX)}),
                    self.client.post(f"{API}/crm-leads/{source}/import", json={"rows": [{"row": 2, "values": {"phone": "9845500006"}}]}),
                ]
                self.assertEqual([r.status_code for r in adds], [403] * len(adds), source)
            # What a vendor login may read about itself
            me = self.client.get(f"{API}/auth/me").json()
            self.assertEqual(me["vendor"], {"id": self.vendor_ids["phodeo"], "name": "Phodeo"})
            self.assertEqual([v["name"] for v in self.client.get(f"{API}/vendors").json()], ["Phodeo"])
            branches = self.client.get(f"{API}/branches").json()["branches"]
            self.assertEqual(len(branches), 3)
            self.assertTrue(all("spin_leads" not in b and "vendor" not in b for b in branches))
        self.assertIsNone(self.client.get(f"{API}/auth/me").json()["vendor"])          # admin
        with self.logged_in("unlinked"):
            resp = self.client.get(f"{API}/wedding-leads")
            self.assertEqual((resp.status_code, resp.json()["detail"]), (403, "This login isn't linked to a vendor."))
            self.assertEqual(self.client.get(f"{API}/crm-leads/wedding/export").status_code, 403)

    # ── Export per role ──
    def test_export_matches_each_users_list(self):
        for who in ("admin", "phodeo", "rj", "ohp"):
            with self.logged_in(who):
                for source in base.LIST_URL:
                    with self.subTest(who=who, source=source):
                        items = self.list_items(source)
                        ws = self.export_sheet(source)
                        rows = list(ws.iter_rows(values_only=True))
                        # Vendor logins: no Assigned to / Vendor updates columns
                        h = HEADERS[source] if who == "admin" else [c for c in HEADERS[source]
                                                                   if c not in ("Assigned to", "Vendor updates")]
                        self.assertEqual(list(rows[0]), h)
                        self.assertEqual(len(rows) - 1, len(items))
                        phone_col = h.index("Lead") if source == "whatsapp" else h.index("Contact")
                        for item, row in zip(items, rows[1:]):           # same order as the list
                            phone = item.get("phone_display") or ("+91" + str(item.get("phone")))
                            self.assertEqual(row[phone_col], phone)
                            if who == "admin":
                                self.assertEqual(row[h.index("Assigned to")], assigned_label(item["crm"]))
        # Formats, as on the screen
        ws = self.export_sheet("wedding")
        h = HEADERS["wedding"]
        row = next(r for r in ws.iter_rows(min_row=2) if r[h.index("Lead")].value == "Wed Phodeo")
        self.assertEqual(row[h.index("Contact")].value, "+919845100001")
        self.assertEqual(row[h.index("Contact")].data_type, "s")
        self.assertEqual((row[h.index("Event date")].value.date(), row[h.index("Event date")].number_format),
                         (EVENT, "dd mmm yyyy"))
        self.assertEqual((row[h.index("Submitted")].value, row[h.index("Submitted")].number_format),
                         (datetime(2026, 11, 14, 10, 30), "d mmm yyyy, hh:mm am/pm"))
        self.assertEqual(row[h.index("Budget")].value, 150000)
        self.assertIn('"₹"', row[h.index("Budget")].number_format)
        self.assertEqual(row[h.index("Assigned to")].value, "Phodeo · auto")       # area rules
        out = self.export_rows("wedding", "csv")
        csv_row = next(r for r in out[1:] if r[0] == "Wed Phodeo")
        self.assertEqual(csv_row[h.index("Event date")], "20 Dec 2026")
        self.assertEqual(csv_row[h.index("Submitted")], "14 Nov 2026, 10:30 am")
        self.assertEqual(csv_row[h.index("Contact")], "'+919845100001")
        spin = {r[2]: r for r in self.export_rows("spin")[1:]}
        self.assertEqual(spin["+919845400102"][HEADERS["spin"].index("Budget")], "₹20–30")
        self.assertEqual(spin["+919845400102"][HEADERS["spin"].index("Location")], "Bangalore · 560034")
        self.assertEqual(spin["+919845400102"][HEADERS["spin"].index("Assigned to")], "Phodeo · auto")
        self.assertEqual(spin["+919845400101"][HEADERS["spin"].index("Assigned to")], "RJ")
        self.assertEqual(spin["+919845400103"][HEADERS["spin"].index("Assigned to")], "Unassigned")
        planner = {r[3]: r for r in self.export_rows("wedding_planner")[1:]}
        self.assertEqual(planner["+919845200001"][HEADERS["wedding_planner"].index("Wedding date")], "December 2027")
        self.assertEqual(planner["+919845200002"][HEADERS["wedding_planner"].index("Wedding date")], datetime(2027, 1, 15))

    def test_round_trip_export_upload_shows_already_exists(self):
        for source in base.LIST_URL:
            with self.subTest(source=source):
                resp = self.client.get(f"{API}/crm-leads/{source}/export")
                preview = self.client.post(f"{API}/crm-leads/{source}/import/preview",
                                           files={"file": ("export.xlsx", resp.content, XLSX)})
                self.assertEqual(preview.status_code, 200, preview.text)
                body = preview.json()
                counts = body["counts"]
                self.assertEqual((counts["errors"], counts["ok"]), (0, 0), [r["errors"] for r in body["rows"] if r["errors"]])
                self.assertEqual(counts["duplicates"], counts["total"])
                self.assertEqual(body["ignored_columns"], [])
                self.assertIn("Quoted date", body["read_only_columns"])
                self.assertNotIn("Update", body["read_only_columns"])               # read for new leads
        spin = self.client.post(f"{API}/crm-leads/spin/import/preview", files={
            "file": ("e.xlsx", self.client.get(f"{API}/crm-leads/spin/export").content, XLSX)}).json()
        self.assertEqual(spin["counts"]["duplicates_by_reason"],
                         {"in_file": 0, "existing_sheet": 0, "existing_workflow": 3, "existing_added": 1})

    # ── Uploads ──
    def test_unassigned_upload_stays_unassigned(self):
        upload = xlsx_file([
            ["Lead", "Contact", "Branch", "Assigned to"],
            ["Up none", "9845300001", "Jayanagar", None],            # empty: unassigned (no automatic rule)
            ["Up RJ", "9845300002", "Jayanagar", "RJ"],              # manual
            ["Up auto", "9845300003", "Jayanagar", "Phodeo · auto"],  # automatic: the Jayanagar branch's vendor
            ["Up unknown", "9845300004", "Jayanagar", "Nobody"],     # error
            ["Up explicit", "9845300005", "Mysore", "Unassigned"],
        ])
        preview = self.client.post(f"{API}/crm-leads/spin/import/preview", files={"file": ("u.xlsx", upload, XLSX)}).json()
        self.assertEqual([r["status"] for r in preview["rows"]], ["ok", "ok", "ok", "error", "ok"])
        self.assertEqual(preview["rows"][3]["errors"], [{"field": "assigned_to", "message": "Unknown vendor 'Nobody'"}])
        result = self.client.post(f"{API}/crm-leads/spin/import", json={
            "rows": [{"row": r["row"], "values": r["values"]} for r in preview["rows"]]}).json()
        self.assertEqual(result["counts"]["imported"], 4)
        key = {r["values"]["name"]: r.get("lead_key") for r in result["rows"]}

        crm = {i["id"]: i["crm"] for i in self.list_items("spin")}
        self.assertEqual(assigned_label(crm[key["Up none"]]), "Unassigned")
        self.assertEqual(assigned_label(crm[key["Up explicit"]]), "Unassigned")
        self.assertEqual((crm[key["Up RJ"]]["vendor"]["name"], crm[key["Up RJ"]]["vendor_source"]), ("RJ", "manual"))
        self.assertEqual((crm[key["Up auto"]]["vendor"]["name"], crm[key["Up auto"]]["vendor_source"]), ("Phodeo", "branch"))
        with self.logged_in("phodeo"):
            phodeo = self.keys(self.list_items("spin"))
        self.assertIn(key["Up auto"], phodeo)
        self.assertNotIn(key["Up none"], phodeo)               # a Phodeo branch, but uploaded unassigned
        with self.logged_in("rj"):
            self.assertIn(key["Up RJ"], self.keys(self.list_items("spin")))
        # The admin later switches it to automatic: Phodeo (branch) gets it
        self.assertEqual(self.put_crm("admin", "spin", key["Up none"], {"vendor_id": None}).status_code, 200)
        with self.logged_in("phodeo"):
            self.assertIn(key["Up none"], self.keys(self.list_items("spin")))

        # Same for a sheet source: Bangalore would go to Phodeo automatically, but not when uploaded empty
        upload = xlsx_file([["Lead", "Contact", "City", "Assigned to"], ["Wed up", "9845300010", "Bangalore", None]])
        rows = self.client.post(f"{API}/crm-leads/wedding/import/preview", files={"file": ("w.xlsx", upload, XLSX)}).json()["rows"]
        result = self.client.post(f"{API}/crm-leads/wedding/import", json={
            "rows": [{"row": r["row"], "values": r["values"]} for r in rows]}).json()
        wed_key = result["rows"][0]["lead_key"]
        self.assertEqual(assigned_label(next(i["crm"] for i in self.list_items("wedding") if i["lead_key"] == wed_key)), "Unassigned")
        with self.logged_in("phodeo"):
            self.assertNotIn(wed_key, self.keys(self.list_items("wedding")))

    def test_upload_automatic_assignment_words(self):
        """"Automatic" / "Auto" (any case) in Assigned to = automatic, like "<vendor> · auto"."""
        upload = xlsx_file([
            ["Lead", "Contact", "Branch", "Assigned to"],
            ["Auto 1", "9845310001", "Jayanagar", "Automatic"],
            ["Auto 2", "9845310002", "Jayanagar", "auto"],
            ["Auto 3", "9845310003", "Jayanagar", "AUTO"],
            ["Auto 4", "9845310004", "Jayanagar", "Phodeo · auto"],
        ])
        preview = self.client.post(f"{API}/crm-leads/spin/import/preview", files={"file": ("a.xlsx", upload, XLSX)}).json()
        self.assertEqual([r["status"] for r in preview["rows"]], ["ok"] * 4)
        self.assertEqual([r["values"]["assigned_to"] for r in preview["rows"]],
                         ["Automatic", "Automatic", "Automatic", "Phodeo · auto"])
        result = self.client.post(f"{API}/crm-leads/spin/import", json={
            "rows": [{"row": r["row"], "values": r["values"]} for r in preview["rows"]]}).json()
        keys = [r["lead_key"] for r in result["rows"]]
        crm = {i["id"]: i["crm"] for i in self.list_items("spin")}
        for key in keys:                                   # the Jayanagar branch's vendor, automatically
            self.assertEqual((crm[key]["vendor"]["name"], crm[key]["vendor_source"]), ("Phodeo", "branch"))
        with self.logged_in("phodeo"):
            self.assertTrue(set(keys) <= self.keys(self.list_items("spin")))
        with Session(self.engine) as s:                   # automatic: nothing stored in crm_lead_meta
            self.assertEqual(s.exec(select(LeadMeta).where(LeadMeta.lead_key.in_(keys))).all(), [])

        # A sheet source too: Bangalore -> Phodeo by the area rules
        upload = xlsx_file([["Lead", "Contact", "City", "Assigned to"], ["Wed auto", "9845310010", "Bangalore", "Auto"]])
        rows = self.client.post(f"{API}/crm-leads/wedding/import/preview", files={"file": ("w.xlsx", upload, XLSX)}).json()["rows"]
        key = self.client.post(f"{API}/crm-leads/wedding/import", json={
            "rows": [{"row": r["row"], "values": r["values"]} for r in rows]}).json()["rows"][0]["lead_key"]
        item = next(i for i in self.list_items("wedding") if i["lead_key"] == key)
        self.assertEqual(assigned_label(item["crm"]), "Phodeo · auto")

    def test_upload_date_formats(self):
        """Event dates behave like the sheet sync (full date -> date, anything else kept as text,
        no error); Submitted/Created stay strict (a date, optional time)."""
        upload = xlsx_file([
            ["Contact", "Event date", "Submitted"],
            ["9845600001", "13-12-2026", "14 Nov 2026, 10:30 am"],
            ["9845600002", "13 Dec 2026", "14-11-2026 10:30"],
            ["9845600003", "13 December 2026", "14 Nov 2026"],
            ["9845600004", "13-12-2026 18:00", None],
            ["9845600005", "20/12/2026", None],                  # the sync reads this too
            ["9845600006", "April 2027", None],                  # partial: kept as text
            ["9845600007", "November 16", None],
            ["9845600008", "16th Nov", None],
            ["9845600009", None, "14/11/2026 10:30"],            # Submitted: not an accepted format
            ["9845600010", "x" * 101, None],                     # longer than the column
        ])
        rows = self.client.post(f"{API}/crm-leads/wedding/import/preview", files={"file": ("d.xlsx", upload, XLSX)}).json()["rows"]
        self.assertEqual([r["status"] for r in rows], ["ok"] * 8 + ["error", "error"])
        self.assertEqual(rows[8]["errors"][0]["field"], "submitted_at")
        self.assertEqual(rows[9]["errors"][0], {"field": "event_date", "message": "Longer than 100 characters"})
        self.client.post(f"{API}/crm-leads/wedding/import", json={"rows": [{"row": r["row"], "values": r["values"]} for r in rows]})
        with Session(self.engine) as s:
            stored = {l.phone: (l.event_date, l.event_date_text) for l in s.exec(select(WeddingLead).where(WeddingLead.origin == "imported"))}
            lead = s.exec(select(WeddingLead).where(WeddingLead.phone == "919845600001")).one()
            self.assertEqual(lead.submitted_at.replace(tzinfo=None), datetime(2026, 11, 14, 5, 0))   # 10:30 IST = 05:00 UTC
        self.assertEqual(stored["919845600001"], (date(2026, 12, 13), "13-12-2026"))
        self.assertEqual(stored["919845600004"][0], date(2026, 12, 13))
        self.assertEqual(stored["919845600005"][0], date(2026, 12, 20))
        for phone, text in (("919845600006", "April 2027"), ("919845600007", "November 16"), ("919845600008", "16th Nov")):
            self.assertEqual(stored[phone], (None, text))

        # International: the same rule
        upload = xlsx_file([["Lead", "Event date"], ["+971500000091", "16th Nov"], ["+971500000092", "20 Dec 2026"]])
        rows = self.client.post(f"{API}/crm-leads/whatsapp/import/preview", files={"file": ("w.xlsx", upload, XLSX)}).json()["rows"]
        self.assertEqual([r["status"] for r in rows], ["ok", "ok"])

        # Planner: a full date is the first day; a month or partial date is the wedding month's text
        upload = xlsx_file([
            ["Contact", "Wedding date"],
            ["9845610001", "20 Dec 2027"],
            ["9845610002", "April 2027"],
            ["9845610003", "16th Nov"],
            ["9845610004", "y" * 51],
        ])
        rows = self.client.post(f"{API}/crm-leads/wedding_planner/import/preview", files={"file": ("p.xlsx", upload, XLSX)}).json()["rows"]
        self.assertEqual([r["status"] for r in rows], ["ok", "ok", "ok", "error"])
        self.client.post(f"{API}/crm-leads/wedding_planner/import", json={"rows": [{"row": r["row"], "values": r["values"]} for r in rows]})
        with Session(self.engine) as s:
            plans = {p.phone: (p.first_day, p.wedding_month, p.wedding_date) for p in s.exec(select(WeddingPlan).where(WeddingPlan.origin == "imported"))}
        self.assertEqual(plans["919845610001"], (date(2027, 12, 20), None, date(2027, 12, 20)))
        self.assertEqual(plans["919845610002"], (None, "April 2027", date(2027, 4, 1)))
        self.assertEqual(plans["919845610003"], (None, "16th Nov", None))

if __name__ == "__main__":
    unittest.main()
