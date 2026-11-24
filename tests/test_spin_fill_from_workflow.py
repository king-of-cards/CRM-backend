"""Filling empty fields of uploaded Spin leads from the workflow ("Spin game"): the bulk import and the
one-time backfill (scripts/fill_spin_from_workflow.py). In-memory SQLite and the fake workflow
database only. Run:

    python -m unittest discover -s tests -t . -v
"""
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from sqlmodel import Session, select

import tests.test_crm_leads_api as base
from tests.test_crm_leads_api import NOW, ApiTestBase

from app.core import response_cache
from app.core.config import settings
from app.models.crm_spin_lead import CrmSpinLead
from app.models.lead_activity import LeadActivity
from app.services.crm_leads import service


def wf(lead_id, phone, name, created, **details):
    row = {"id": lead_id, "name": name, "phone": phone, "email": details.pop("email", None), "created_at": created,
           "branch_id": details.pop("branch_id", None)}
    row.update({f"additional_details.{k}": v for k, v in details.items()})
    return row


# All before the cut-off (history): the duplicate check ignores them, the fill uses them
ROWS = [
    wf(951, "9845091001", "Asha Rao", "2026-11-01T05:00:00", email="asha@example.com", branch_id=4,
       pincode="560011", occasionDate="2026-12-20", occasionType="Wedding", estimatedBudgetPerCard="20-30",
       otherWeddingServices="Photography & Videography", quantityRequired=300),
    wf(952, "9845091002", "Ravi", "2026-10-01T05:00:00", pincode="560001"),
    wf(953, "9845091002", "Ravi", "2026-11-10T05:00:00", pincode="560002"),
    wf(954, "9845091003", "Meena", "2026-10-01T05:00:00", pincode="560003"),
    wf(955, "9845091003", "Kiran", "2026-10-05T05:00:00", pincode="560004"),
    wf(956, "9845091004", "Same", "2026-10-01T05:00:00", pincode="560005"),
    wf(957, "9845091004", "Same", "2026-10-03T05:00:00", pincode="560006"),
    wf(958, "9845091005", "Unknown Branch", "2026-10-01T05:00:00", branch_id=99, pincode="560007"),
]


class Base(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.extra = [mock.patch.object(base, "WORKFLOW_ROWS", ROWS),
                      mock.patch.object(settings, "SPIN_LIVE_FROM", date(2026, 11, 21))]
        for p in self.extra:
            p.start()
        response_cache.clear()

    def tearDown(self):
        for p in self.extra:
            p.stop()
        super().tearDown()

    def lead(self, key) -> CrmSpinLead:
        with Session(self.engine) as s:
            return s.get(CrmSpinLead, int(key.split("-")[1]))

    def check(self, values) -> dict:
        with Session(self.engine) as s:
            results, _, _ = service.check_rows(s, "spin", [(2, values)])
        return results[0]


class ImportFillTests(Base):
    def test_only_empty_fields_are_filled_and_the_preview_says_so(self):
        values = {"phone": "9845091001", "name": "Asha Rao", "email": "mine@example.com",
                  "occasionDate": "20-12-2026", "assigned_to": "Unassigned"}
        preview = self.check(values)
        self.assertEqual(preview["status"], "ok")
        self.assertEqual(preview["fill"]["workflow_id"], "951")
        self.assertEqual(set(preview["fill"]["fields"]), {"branch", "pincode", "occasionType", "estimatedBudgetPerCard",
                                                          "otherWeddingServices", "quantityRequired"})
        row = self.import_spin(values)
        self.assertEqual(row["status"], "imported", row)
        lead = self.lead(row["lead_key"])
        self.assertEqual((lead.name, lead.email, lead.branch_id), ("Asha Rao", "mine@example.com", 4))   # email kept
        self.assertEqual(lead.additional_details, {
            "occasionDate": "2026-12-20", "pincode": "560011", "occasionType": "Wedding",
            "estimatedBudgetPerCard": "20-30", "otherWeddingServices": "Photography & Videography",
            "quantityRequired": 300})
        with Session(self.engine) as s:
            actions = {a.action: a for a in s.exec(select(LeadActivity).where(LeadActivity.lead_key == row["lead_key"]))}
        self.assertIn(service.FILLED_ACTION, actions)
        self.assertEqual(actions[service.FILLED_ACTION].new_value, "workflow lead 951")
        # The upload's created date is kept (here: empty = the upload time, not the workflow's)
        self.assertNotEqual(lead.created_at.date(), date(2026, 11, 1))

    def test_same_phone_picks_the_non_conflicting_row(self):
        preview = self.check({"phone": "9845091003", "name": "Kiran"})
        self.assertEqual((preview["fill"]["workflow_id"], preview["fill"]["fields"]["pincode"]), ("955", "560004"))

    def test_closest_created_date_wins(self):
        preview = self.check({"phone": "9845091002", "name": "Ravi", "created_at": "15-11-2026 10:00"})
        self.assertEqual((preview["fill"]["workflow_id"], preview["fill"]["fields"]["pincode"]), ("953", "560002"))

    def test_ambiguous_or_conflicting_rows_are_skipped_but_the_lead_is_imported(self):
        no_date = self.check({"phone": "9845091002", "name": "Ravi"})
        self.assertIn("no created date to choose by", no_date["fill_skipped"])
        tie = self.check({"phone": "9845091004", "created_at": "02-10-2026 10:30"})   # 05:00 UTC: 1 day from each
        self.assertIn("equally close", tie["fill_skipped"])
        conflict = self.check({"phone": "9845091001", "name": "Someone Else"})
        self.assertIn("another name", conflict["fill_skipped"])
        self.assertNotIn("fill", conflict)
        row = self.import_spin({"phone": "9845091001", "name": "Someone Else"})
        self.assertEqual(row["status"], "imported")
        self.assertEqual(self.lead(row["lead_key"]).additional_details, {})            # nothing filled
        self.assertNotIn("fill", self.check({"phone": "9845099988"}))                  # no workflow row at all

    def test_unknown_workflow_branch_is_not_filled(self):
        preview = self.check({"phone": "9845091005"})
        self.assertNotIn("branch", preview["fill"]["fields"])
        self.assertEqual(preview["fill"]["fields"]["pincode"], "560007")


class BackfillTests(Base):
    def add_upload(self, phone, name=None, created=NOW, **details) -> int:
        with Session(self.engine) as s:
            lead = CrmSpinLead(origin="imported", name=name, phone=phone, phone_e164=f"+91{phone}", created_at=created,
                               updated_at=created, additional_details=details)
            s.add(lead)
            s.commit()
            return lead.id

    def test_preview_apply_and_undo(self):
        asha = self.add_upload("9845091001", "Asha Rao", pincode="560099")              # pincode filled: kept
        ravi = self.add_upload("9845091002", "Ravi", created=datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc))
        other = self.add_upload("9845091001", "Someone Else")                           # conflicts: skipped
        none = self.add_upload("9845099977", "Nobody")                                  # no workflow row
        with Session(self.engine) as s:
            plan = {row["id"]: row for row in service.plan_backfill(s)}
            self.assertEqual(plan[asha]["workflow_id"], "951")
            self.assertNotIn("pincode", plan[asha]["fields"])
            self.assertEqual(plan[asha]["fields"]["branch"], 4)
            self.assertEqual((plan[ravi]["workflow_id"], plan[ravi]["fields"]), ("952", {"pincode": "560001"}))
            self.assertIn("another name", plan[other]["skipped"])
            self.assertEqual(plan[none]["skipped"], "No Spin game lead with this phone")
            self.assertEqual(self.lead(f"crm-{asha}").additional_details, {"pincode": "560099"})   # preview: no change
            undo = service.apply_backfill(s, list(plan.values()))
            s.commit()
        lead = self.lead(f"crm-{asha}")
        self.assertEqual((lead.email, lead.branch_id, lead.additional_details["pincode"],
                          lead.additional_details["occasionDate"]), ("asha@example.com", 4, "560099", "2026-12-20"))
        self.assertEqual(self.lead(f"crm-{ravi}").additional_details, {"pincode": "560001"})
        self.assertEqual(self.lead(f"crm-{other}").additional_details, {})
        self.assertTrue(any(u.startswith(f"UPDATE crm_spin_leads SET name = 'Asha Rao', email = NULL") and
                            f"WHERE id = {asha};" in u and '"pincode": "560099"' in u for u in undo))
        self.assertTrue(undo[-1].startswith("DELETE FROM crm_lead_activity"))
        with Session(self.engine) as s:
            filled = s.exec(select(LeadActivity).where(LeadActivity.action == service.FILLED_ACTION)).all()
        self.assertEqual(sorted(a.lead_key for a in filled), sorted([f"crm-{asha}", f"crm-{ravi}"]))


if __name__ == "__main__":
    unittest.main()
