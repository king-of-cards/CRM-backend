"""The duplicate rule: the same phone and no conflicting filled field. A field empty (or N/A) on
either side is ignored; a filled field that differs from a filled existing value makes it another
lead. Every section; live workflow rows (Spin, KOC -> Dhruva) included. In-memory SQLite and the
fake workflow database only. Run:

    python -m unittest discover -s tests -t . -v
"""
import unittest
from datetime import date
from unittest import mock

from sqlmodel import Session, select

import tests.test_crm_leads_api as base
from tests.test_crm_leads_api import API, NOW, ApiTestBase
from tests.test_koc_to_dhruva import KocTestBase

from app.core import response_cache
from app.core.config import settings
from app.models.crm_spin_lead import CrmSpinLead
from app.services.crm_leads import service

CUTOFF = date(2026, 11, 21)
# Today's live workflow lead, with more fields filled than the upload will have
RAMESH = {"id": 901, "name": "Ramesh Kumar", "phone": "7702762184", "email": "ramesh@example.com",
          "created_at": "2026-11-22T05:00:00", "branch_id": None,
          "additional_details.occasionDate": "2026-12-20", "additional_details.pincode": "560011",
          "additional_details.estimatedBudgetPerCard": "20-30",
          "additional_details.otherWeddingServices": "Photography & Videography"}
HISTORY = {**RAMESH, "id": 902, "phone": "7702762185", "created_at": "2026-11-10T05:00:00"}   # before the cut-off


class Base(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.extra = [mock.patch.object(base, "WORKFLOW_ROWS", [RAMESH, HISTORY]),
                      mock.patch.object(settings, "SPIN_LIVE_FROM", CUTOFF)]
        for p in self.extra:
            p.start()
        response_cache.clear()

    def tearDown(self):
        for p in self.extra:
            p.stop()
        super().tearDown()

    def preview(self, rows: list[dict]) -> list[dict]:
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}/crm-leads/spin/import",
                                    json={"rows": [{"row": n + 2, "values": v} for n, v in enumerate(rows)]})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["rows"]


class SpinWorkflowTests(Base):
    def test_todays_lead_uploaded_again_is_a_duplicate(self):
        """The reported case: phone + name + event date vs the live row with email, pincode, budget too."""
        row = self.preview([{"phone": "7702762184", "name": "Ramesh Kumar", "occasionDate": "20-12-2026"}])[0]
        self.assertEqual((row["status"], row["duplicate"]), ("duplicate", {"reason": "existing_workflow", "lead_key": "901"}))
        self.assertEqual(self.count(CrmSpinLead), 0)

    def test_case_spaces_and_na_still_match(self):
        row = self.preview([{"phone": "+91 77027 62184", "name": "  ramesh   KUMAR ", "email": "N/A",
                             "occasionDate": "2026-12-20", "estimatedBudgetPerCard": "20_30"}])[0]
        self.assertEqual(row["status"], "duplicate")

    def test_a_different_filled_field_is_another_lead(self):
        for values in ({"name": "Suresh Kumar"}, {"occasionDate": "21-12-2026"}, {"email": "other@example.com"},
                       {"pincode": "560012"}):
            with self.subTest(values=values), Session(self.engine) as s:        # checked only: nothing saved
                results, _, _ = service.check_rows(s, "spin", [(2, {"phone": "7702762184", "name": "Ramesh Kumar",
                                                                    **values})])
                self.assertEqual((results[0]["status"], results[0]["duplicate"]), ("ok", None), values)

    def test_a_field_empty_on_the_existing_lead_is_no_conflict(self):
        # The live row has no Quantity required or Bride/Groom name: filled in the upload, still the same lead
        row = self.preview([{"phone": "7702762184", "name": "Ramesh Kumar", "quantityRequired": "300",
                             "brideGroomName": "Asha"}])[0]
        self.assertEqual(row["status"], "duplicate")
        # A saved upload with only a phone: a later row with more fields is the same lead
        first = self.preview([{"phone": "9845070001", "assigned_to": "Unassigned"}])[0]
        self.assertEqual(first["status"], "imported")
        again = self.preview([{"phone": "9845070001", "name": "Filled later", "occasionDate": "20-12-2026"}])[0]
        self.assertEqual((again["status"], again["duplicate"]["reason"]), ("duplicate", "existing_added"))

    def test_rows_before_the_cutoff_still_do_not_count(self):
        row = self.preview([{"phone": "7702762185", "name": "Ramesh Kumar"}])[0]
        self.assertEqual(row["status"], "imported")

    def test_in_file_duplicates_follow_the_same_rule(self):
        rows = self.preview([
            {"phone": "9845070011", "name": "Asha", "email": "asha@example.com"},         # 2 imported
            {"phone": "9845070011", "name": "asha"},                                      # 3 = row 2 (fewer fields)
            {"phone": "9845070011", "name": "Asha", "pincode": "560011"},                 # 4 = row 2 (row 2 has none)
            {"phone": "9845070011", "name": "Meena"},                                     # 5 another name: imported
            {"phone": "9845070011", "name": "Meena", "email": "meena@example.com"},       # 6 = row 5
        ])
        self.assertEqual([r["status"] for r in rows], ["imported", "duplicate", "duplicate", "imported", "duplicate"])
        self.assertEqual([r["duplicate"]["duplicate_of_row"] for r in rows if r["duplicate"]], [2, 2, 5])

    def test_single_add_follows_the_rule(self):
        self.assertEqual(self.add("wedding", {"phone": "9845070021", "name": "Ravi", "city": "Bangalore"}).status_code, 201)
        self.assertEqual(self.add("wedding", {"phone": "9845070021", "name": "ravi"}).status_code, 409)
        self.assertEqual(self.add("wedding", {"phone": "9845070021", "city": "Mysore"}).status_code, 201)

    def test_read_only_check_lists_uploaded_copies_of_live_leads(self):
        # An upload saved before the fix (directly: the import would now refuse it), and an unrelated one
        with Session(self.engine) as s:
            s.add(CrmSpinLead(origin="imported", name="Ramesh Kumar", phone="7702762184", phone_e164="+917702762184",
                              created_at=NOW, updated_at=NOW, additional_details={"occasionDate": "2026-12-20"}))
            s.add(CrmSpinLead(origin="imported", name="Someone else", phone="7702762184", phone_e164="+917702762184",
                              created_at=NOW, updated_at=NOW, additional_details={}))
            s.add(CrmSpinLead(origin="imported", name="Old one", phone="7702762185", phone_e164="+917702762185",
                              created_at=NOW, updated_at=NOW, additional_details={}))   # its workflow row is history
            s.commit()
            found = service.uploaded_spin_duplicates(s)
        self.assertEqual([(f["name"], f["phone"], f["workflow_ids"]) for f in found],
                         [("Ramesh Kumar", "7702762184", ["901"])])


class OtherSectionsTests(KocTestBase):
    def test_vendor_to_koc_and_dhruva(self):
        def vtk(**body):
            with self.logged_in("phodeo"):
                return self.client.post(f"{API}/vendor-to-koc/leads", json=body)
        self.assertEqual(vtk(name="Asha", phone="9845070031", city="Pune").status_code, 201)
        self.assertEqual(vtk(name="asha", phone="9845070031").status_code, 409)              # fewer fields
        self.assertEqual(vtk(phone="9845070031", city="Pune", date="2026-12-01").status_code, 409)  # saved has no date
        self.assertEqual(vtk(name="Meena", phone="9845070031").status_code, 201)            # another name
        # Dhruva -> KOC: a duplicate is the same phone (last 10 digits), whatever the other fields
        self.assertEqual(self.add(name="Ravi", venue="Hall", event_dates=["2026-12-04"], phones=["9845070041"]).status_code, 201)
        self.assertEqual(self.add(name="ravi", phones=["9845070041"]).status_code, 409)
        self.assertEqual(self.add(name="Other", phones=["+91 98450 70041"], venue="Other Hall").status_code, 409)
        self.assertEqual(self.add(name="Other", phones=["9845000000", "9845070041"]).status_code, 409)   # any of them
        self.assertEqual(self.add(name="Ravi", phones=["9845070042"]).status_code, 201)


class KocWorkflowTests(KocTestBase):
    def setUp(self):
        super().setUp()
        live = {**RAMESH, "id": 911, "additional_details.estimatedBudgetPerCard": "70-100",
                "additional_details.quantityRequired": 500, "additional_details.occasionType": "Wedding"}
        self.extra = [mock.patch.object(base, "WORKFLOW_ROWS", [live]),
                      mock.patch.object(settings, "SPIN_LIVE_FROM", CUTOFF)]
        for p in self.extra:
            p.start()
        response_cache.clear()

    def tearDown(self):
        for p in self.extra:
            p.stop()
        super().tearDown()

    def test_live_koc_row_uploaded_again_is_a_duplicate(self):
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}/koc-to-dhruva/leads/import", json={"rows": [
                {"row": 2, "values": {"phone": "7702762184", "name": "Ramesh Kumar", "occasionDate": "20-12-2026"}},
                {"row": 3, "values": {"phone": "7702762184", "name": "Someone else"}}]})
        rows = resp.json()["rows"]
        self.assertEqual((rows[0]["status"], rows[0]["duplicate"]), ("duplicate", {"reason": "existing_workflow",
                                                                                   "lead_key": "911"}))
        self.assertEqual(rows[1]["status"], "imported")


if __name__ == "__main__":
    unittest.main()
