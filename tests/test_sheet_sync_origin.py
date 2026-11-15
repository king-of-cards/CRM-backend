"""The sheet sync must never insert over, change or remove leads added/imported in the CRM.

Runs against an in-memory SQLite database with a fake Google Sheet reader. The .env file is
never read, Google is never called and no real database is contacted. Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import os

# Must be set before anything from app is imported
os.environ.update({
    "ENV_FILE": "",
    "DATABASE_URL": "sqlite://",
    "READ_DB_HOST": "",
    "READ_DATABASE_URL": "",
    "JWT_SECRET_KEY": "test-secret-key-for-unit-tests-only-0123456789",
})

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

import app.models  # noqa: E402,F401
from app.models.crm_spin_lead import CrmSpinLead  # noqa: E402
from app.models.lead_origin import ORIGIN_ADDED, ORIGIN_IMPORTED, ORIGIN_SHEET, new_crm_key  # noqa: E402
from app.models.wedding_lead import WeddingLead  # noqa: E402
from app.models.wedding_plan import WeddingPlan  # noqa: E402
from app.models.whatsapp_lead import WhatsAppLead  # noqa: E402
from app.services import crm_spin_leads, google_sheets, sheet_sync  # noqa: E402
from app.services.crm_leads import service  # noqa: E402

NOW = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)

COLUMNS = {
    "whatsapp": ["Country", "Phone", "Event Date", "Submitted"],
    "wedding": ["Timestamp", "Full Name", "Contact Number", "Email Address", "City"],
    "wedding_planner": ["Plan ID", "Last Updated", "Name", "WhatsApp Number", "Email"],
}

# Two versions of each sheet: rows edited, removed and added between them
SHEET_V1 = {
    "whatsapp": [
        ["India", "+91 98450 00001", "12/12/2026", "26/07/2026, 13:46:32 IST"],
        ["UAE", "+971 50 000 0002", "01/01/2027", "27/07/2026, 09:00:00 IST"],
    ],
    "wedding": [
        ["10/11/2026 0:37:00", "Asha", "9845000003", "asha@example.com", "Bangalore"],
        ["10/12/2026 8:00:00", "Ravi", "9845000004", "ravi@example.com", "Mysore"],
    ],
    "wedding_planner": [
        ["KOC-AAA111", "10/11/2026 0:37:00", "Meera", "9845000005", "meera@example.com"],
        ["KOC-BBB222", "10/12/2026 8:00:00", "Kiran", "9845000006", "kiran@example.com"],
    ],
}
SHEET_V2 = {
    "whatsapp": [
        ["India", "+91 98450 00001", "13/12/2026", "26/07/2026, 13:46:32 IST"],   # edited
        ["USA", "+1 555 000 0007", "02/02/2027", "28/07/2026, 10:00:00 IST"],    # new
    ],
    "wedding": [
        ["10/11/2026 0:37:00", "Asha", "9845000003", "asha@example.com", "Bengaluru"],  # edited
        ["10/13/2026 9:00:00", "Nisha", "9845000008", "nisha@example.com", "Chennai"],  # new
    ],
    "wedding_planner": [
        ["KOC-AAA111", "10/11/2026 0:37:00", "Meera R", "9845000005", "meera@example.com"],  # edited
        ["KOC-CCC333", "10/13/2026 9:00:00", "Dev", "9845000009", "dev@example.com"],         # new
    ],
}


def fake_reader(sheets: dict):
    def read_sheet(source: str, refresh: bool = False):
        cols = COLUMNS[source]
        rows = [dict(zip(cols, values), _row=i + 2) for i, values in enumerate(sheets.get(source, []))]
        return {"columns": cols, "rows": rows}
    return read_sheet


def crm_rows() -> list:
    """One added and one imported lead in every sheet table."""
    rows = []
    for origin in (ORIGIN_ADDED, ORIGIN_IMPORTED):
        common = {"origin": origin, "row_hash": "", "first_seen_at": NOW, "updated_at": NOW}
        rows.append(WhatsAppLead(sheet_key=new_crm_key(), country="India", phone="919845000101", **common))
        rows.append(WeddingLead(sheet_key=new_crm_key(), name=f"{origin} lead", phone="919845000102", **common))
        rows.append(WeddingPlan(plan_id=new_crm_key(), name=f"{origin} plan", phone="919845000103", **common))
    return rows


MODELS = (WhatsAppLead, WeddingLead, WeddingPlan)


class SheetSyncOriginTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            for row in crm_rows():
                s.add(row)
            s.commit()
        self.before = self.snapshot(origin_in=(ORIGIN_ADDED, ORIGIN_IMPORTED))
        self.assertEqual(sum(len(v) for v in self.before.values()), 6)

    def tearDown(self):
        self.engine.dispose()

    def snapshot(self, origin_in) -> dict:
        with Session(self.engine) as s:
            return {
                m.__tablename__: sorted(
                    (r.model_dump() for r in s.exec(select(m).where(m.origin.in_(origin_in)))),
                    key=lambda d: d["id"],
                )
                for m in MODELS
            }

    def sync_all(self, sheets: dict) -> dict:
        runs = {}
        with mock.patch.object(google_sheets, "read_sheet", fake_reader(sheets)):
            for source in ("whatsapp", "wedding", "wedding_planner"):
                with Session(self.engine) as s:
                    run = sheet_sync.run_sync(s, source)
                    self.assertEqual(run.status, "success", run.error)
                    runs[source] = run.model_dump()
        return runs

    def assert_crm_rows_untouched(self):
        self.assertEqual(self.snapshot(origin_in=(ORIGIN_ADDED, ORIGIN_IMPORTED)), self.before)

    def test_sync_never_changes_or_removes_added_or_imported_rows(self):
        # 1. First sync: sheet rows inserted next to the CRM rows
        runs = self.sync_all(SHEET_V1)
        self.assert_crm_rows_untouched()
        for source in runs:
            self.assertEqual(runs[source]["inserted"], 2)
            self.assertEqual(runs[source]["removed"], 0)

        # 2. Rows edited, removed and added in the sheet
        runs = self.sync_all(SHEET_V2)
        self.assert_crm_rows_untouched()
        for source in runs:
            self.assertEqual((runs[source]["inserted"], runs[source]["updated"], runs[source]["removed"]), (1, 1, 1))

        # 3. Sheet emptied: every sheet row is marked removed, the CRM rows stay as they were
        runs = self.sync_all({})
        self.assert_crm_rows_untouched()
        for source in runs:
            self.assertEqual(runs[source]["removed"], 2)          # only the sheet rows still active
        sheet_rows = self.snapshot(origin_in=(ORIGIN_SHEET,))
        for table, rows in sheet_rows.items():
            self.assertEqual(len(rows), 3, table)
            self.assertTrue(all(r["is_deleted"] for r in rows), table)

    def test_new_sheet_rows_get_origin_sheet(self):
        self.sync_all(SHEET_V1)
        with Session(self.engine) as s:
            for m in MODELS:
                origins = {r.origin for r in s.exec(select(m))}
                self.assertEqual(origins, {ORIGIN_SHEET, ORIGIN_ADDED, ORIGIN_IMPORTED}, m.__tablename__)

    def test_sync_fills_phone_e164_without_counting_it_as_an_edit(self):
        self.sync_all(SHEET_V1)
        with Session(self.engine) as s:
            wa = s.exec(select(WhatsAppLead).where(WhatsAppLead.origin == ORIGIN_SHEET)).all()
            self.assertEqual(sorted(r.phone_e164 for r in wa), ["+919845000001", "+971500000002"])
            wd = s.exec(select(WeddingLead).where(WeddingLead.origin == ORIGIN_SHEET)).all()
            self.assertEqual(sorted(r.phone_e164 for r in wd), ["+919845000003", "+919845000004"])
            wp = s.exec(select(WeddingPlan).where(WeddingPlan.origin == ORIGIN_SHEET)).all()
            self.assertEqual(sorted(r.phone_e164 for r in wp), ["+919845000005", "+919845000006"])
            for r in wa:                       # as if synced before the column existed
                r.phone_e164 = None
                s.add(r)
            s.commit()
        runs = self.sync_all(SHEET_V1)
        self.assertEqual(runs["whatsapp"]["updated"], 0)
        with Session(self.engine) as s:
            self.assertTrue(all(r.phone_e164 for r in s.exec(select(WhatsAppLead).where(WhatsAppLead.origin == ORIGIN_SHEET))))

    def test_number_with_country_code_but_no_plus_gets_the_right_e164(self):
        """A number typed with its country code but no "+" is stored with that country code
        (447911123456, 919845012345); its E.164 form must come from that, not from re-reading the
        typed digits as an Indian number (which gave +91 447911123456)."""
        sheets = {
            "wedding": [["10/11/2026 0:37:00", "Asha", "447911123456", "asha@example.com", "London"],
                        ["10/12/2026 0:37:00", "Ravi", "919845012345", "ravi@example.com", "Bangalore"]],
            "wedding_planner": [["KOC-UKNOPLUS", "10/11/2026 0:37:00", "Meera", "447911123499", "meera@example.com"]],
        }
        expected_wedding = {"447911123456": "+447911123456", "919845012345": "+919845012345"}
        self.sync_all(sheets)
        with Session(self.engine) as s:
            weddings = s.exec(select(WeddingLead).where(WeddingLead.origin == ORIGIN_SHEET)).all()
            self.assertEqual({w.phone: w.phone_e164 for w in weddings}, expected_wedding)
            plan = s.exec(select(WeddingPlan).where(WeddingPlan.origin == ORIGIN_SHEET)).one()
            self.assertEqual((plan.phone, plan.phone_e164), ("447911123499", "+447911123499"))
            uk_key = next(w.sheet_key for w in weddings if w.phone == "447911123456")
            # Rows synced by the old code have the wrong value: the next sync corrects it quietly
            for w in weddings:
                if w.phone == "447911123456":
                    w.phone_e164 = "+91447911123456"
                    s.add(w)
            plan.phone_e164 = "+91447911123499"
            s.add(plan)
            s.commit()
        runs = self.sync_all(sheets)
        self.assertEqual((runs["wedding"]["updated"], runs["wedding_planner"]["updated"]), (0, 0))
        with Session(self.engine) as s:
            self.assertEqual({w.phone: w.phone_e164 for w in s.exec(select(WeddingLead).where(WeddingLead.origin == ORIGIN_SHEET))},
                             expected_wedding)
            self.assertEqual(s.exec(select(WeddingPlan.phone_e164).where(WeddingPlan.origin == ORIGIN_SHEET)).one(),
                             "+447911123499")
            # An upload of the same number (as exported, with "+") is recognised as the existing sheet lead
            for source, phone, key in (("wedding", "+447911123456", uk_key),
                                       ("wedding_planner", "+447911123499", "KOC-UKNOPLUS")):
                results, _, _ = service.check_rows(s, source, [(2, {"phone": phone})])
                self.assertEqual((results[0]["status"], results[0]["duplicate"]),
                                 ("duplicate", {"reason": "existing_sheet", "lead_key": key}))

    def test_sheet_row_with_a_key_taken_by_an_imported_row_is_skipped(self):
        # An import that kept the sheet's Plan ID: the sync must not overwrite or remove it
        with Session(self.engine) as s:
            s.add(WeddingPlan(plan_id="KOC-AAA111", name="imported copy", origin=ORIGIN_IMPORTED,
                              row_hash="", first_seen_at=NOW, updated_at=NOW))
            s.commit()
        before = self.snapshot(origin_in=(ORIGIN_ADDED, ORIGIN_IMPORTED))

        with self.assertLogs("app.services.sheet_sync", level="WARNING"):
            runs = self.sync_all(SHEET_V1)
        self.assertEqual(runs["wedding_planner"]["inserted"], 1)      # only KOC-BBB222
        self.sync_all({})
        self.assertEqual(self.snapshot(origin_in=(ORIGIN_ADDED, ORIGIN_IMPORTED)), before)


class CrmSpinLeadsTest(unittest.TestCase):
    """The CRM's own spin leads, as merged into GET /spin-leads."""

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as s:
            s.add_all([
                CrmSpinLead(origin=ORIGIN_ADDED, name="Asha", phone="9845000001", phone_e164="+919845000001", branch_id=3, created_at=NOW,
                            updated_at=NOW, additional_details={"pincode": "560018", "estimatedBudgetPerCard": "20-30"}),
                CrmSpinLead(origin=ORIGIN_IMPORTED, name="Ravi", phone="9845000002", phone_e164="+919845000002", created_at=NOW, updated_at=NOW,
                            additional_details={"pincode": "600001", "estimatedBudgetPerCard": "5-10"}),
                CrmSpinLead(origin=ORIGIN_ADDED, name="Gone", phone="9845000003", phone_e164="+919845000003", created_at=NOW, updated_at=NOW,
                            is_deleted=True),
            ])
            s.commit()

    def tearDown(self):
        self.engine.dispose()

    def names(self, **kw) -> list[str]:
        args = {"search": None, "sort": None, "order": "asc", "branch": None, "flt": None, **kw}
        with Session(self.engine) as s:
            return [lead.name for lead in crm_spin_leads.matching(s, **args)]

    def test_filters_and_sort(self):
        self.assertEqual(self.names(), ["Asha", "Ravi"])                       # deleted one hidden
        self.assertEqual(self.names(search="rav"), ["Ravi"])
        self.assertEqual(self.names(search="560018"), ["Asha"])               # additional_details searched
        self.assertEqual(self.names(branch="3"), ["Asha"])
        self.assertEqual(self.names(flt={"pincode": "600"}), ["Ravi"])
        self.assertEqual(self.names(flt={"ids_in": []}), [])
        self.assertEqual(self.names(sort="additional_details.estimatedBudgetPerCard"), ["Ravi", "Asha"])
        self.assertEqual(self.names(sort="name", order="desc"), ["Ravi", "Asha"])

    def test_item_has_the_list_columns_and_a_crm_key(self):
        columns = [
            {"name": "id"}, {"name": "name"}, {"name": "created_at"}, {"name": "campaign_id"},
            {"name": "additional_details.pincode", "json_field": True, "key": "pincode"},
        ]
        with Session(self.engine) as s:
            lead = s.exec(select(CrmSpinLead).where(CrmSpinLead.name == "Asha")).one()
            item = crm_spin_leads.to_item(lead, columns)
        self.assertEqual(item["id"], f"crm-{lead.id}")
        self.assertEqual(item["additional_details.pincode"], "560018")
        self.assertIsNone(item["campaign_id"])
        self.assertEqual(item["origin"], ORIGIN_ADDED)
        self.assertEqual(set(item), {c["name"] for c in columns} | {"origin"})


if __name__ == "__main__":
    unittest.main()
