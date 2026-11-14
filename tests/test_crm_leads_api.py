"""Add / import / export / edit / archive leads through the API, for all four sources.

Runs against an in-memory SQLite database. The workflow (PostgreSQL) database is replaced by
a small fake, the .env file is never read and no real database is contacted. Run from the
project root:

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
    "CORS_ORIGINS": '["https://app.example.test"]',
})

import csv  # noqa: E402
import io  # noqa: E402
import unittest  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402
from sqlalchemy import BigInteger, Column, MetaData, Table, inspect  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.api.deps import require_admin  # noqa: E402
from app.api.v1.endpoints import spin_leads as spin_ep  # noqa: E402
from app.core import response_cache  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.branch import Branch  # noqa: E402
from app.models.crm_spin_lead import CrmSpinLead  # noqa: E402
from app.models.lead_meta import LeadMeta  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.wedding_lead import WeddingLead  # noqa: E402
from app.models.wedding_plan import WeddingPlan  # noqa: E402
from app.models.whatsapp_lead import WhatsAppLead  # noqa: E402
from app.services import branches as branch_service  # noqa: E402
from app.services.crm_leads import service, tabular  # noqa: E402
from app.services.lead_crm import seed_vendors  # noqa: E402

API = "/api/v1"
ORIGIN = "https://app.example.test"
NOW = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Minimal valid lead per source (phone written differently each time on purpose)
SAMPLE = {
    "spin": {"phone": "98450 10001", "name": "Asha", "branch": "Jayanagar", "pincode": "560011",
             "occasionDate": "20-12-2026", "estimatedBudgetPerCard": "10-20"},
    "whatsapp": {"phone": "+971 50 123 4567", "country": "UAE", "event_date": "20-12-2026"},
    "wedding": {"phone": "09845010003", "name": "Ravi", "city": "Bangalore", "budget": "1.5 lakh",
                "submitted_at": "14-11-2026 10:30"},
    "wedding_planner": {"phone": "+91 98450 10004", "name": "Meera", "wedding_month": "December 2027",
                        "total_guests": "300", "priority1": "Food"},
}
SAME_PHONE_OTHER_FORMAT = {
    "spin": "+919845010001", "whatsapp": "00971501234567", "wedding": "+91 98450 10003", "wedding_planner": "9845010004",
}
LIST_URL = {
    "spin": "/spin-leads", "whatsapp": "/whatsapp-leads", "wedding": "/wedding-leads", "wedding_planner": "/wedding-plans",
}

# ── Fake workflow database (spin_lead) ─────────────────────────────────
WORKFLOW_COLUMNS = [
    {"name": "id", "type": "BIGINT"},
    {"name": "name", "type": "VARCHAR(150)"},
    {"name": "phone", "type": "VARCHAR(20)"},
    {"name": "created_at", "type": "TIMESTAMP(6) WITHOUT TIME ZONE"},
    {"name": "branch_id", "type": "BIGINT"},
    {"name": "additional_details.pincode", "type": "string", "json_field": True, "source": "additional_details", "key": "pincode"},
    {"name": "additional_details.occasionDate", "type": "string", "json_field": True, "source": "additional_details",
     "key": "occasionDate"},
]
WORKFLOW_ROWS = [
    {"id": 101, "name": "Workflow One", "phone": "9845099999", "created_at": "2026-11-01T04:30:00", "branch_id": 1,
     "additional_details.pincode": "560018", "additional_details.occasionDate": "2026-12-20"},
]
WORKFLOW_PHONE = "+919845099999"


def fake_list_spin_leads(_admin=None, page=1, page_size=50, search=None, sort=None, order="asc", branch=None,
                         filters=None, offset=None, limit=None):
    start = (page - 1) * page_size if offset is None else offset
    count = page_size if limit is None else limit
    rows = WORKFLOW_ROWS
    if filters and filters.get("ids_in") is not None:
        rows = [r for r in rows if str(r["id"]) in {str(i) for i in filters["ids_in"]}]
    return {"columns": WORKFLOW_COLUMNS, "items": [dict(r) for r in rows[start:start + count]], "total": len(rows),
            "page": page, "page_size": page_size, "branch_column": "branch_id"}


FAKE_TABLE = Table("spin_lead", MetaData(), Column("id", BigInteger, primary_key=True))


def fake_workflow_phones(e164s):
    return {WORKFLOW_PHONE: ["101"]} if WORKFLOW_PHONE in e164s else {}


def xlsx_file(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    for r in rows:
        ws.append(r)
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"        # text that looks like a formula (what a user can type)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class CrmLeadsApiTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine, expire_on_commit=False) as s:
            admin = User(name="Admin", email="admin@example.com", hashed_password="x", role="admin", is_active=True)
            s.add(admin)
            for bid, name, city in ((1, "Chamrajpet", "Bangalore"), (4, "Jayanagar", "Bangalore"), (10, "Mysore", "Mysore")):
                s.add(Branch(id=bid, name=name, city=city, source="workflow", created_at=NOW, updated_at=NOW))
            s.commit()
            s.refresh(admin)
            self.admin = admin
            seed_vendors(s)

        def override_get_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[require_admin] = lambda: self.admin
        self.patches = [
            mock.patch.object(spin_ep, "list_spin_leads", fake_list_spin_leads),
            mock.patch.object(spin_ep, "_get_table", lambda: FAKE_TABLE),
            mock.patch.object(service, "workflow_phones", fake_workflow_phones),
        ]
        for p in self.patches:
            p.start()
        response_cache.clear()
        branch_service._cache["at"] = None
        self.client = TestClient(app)          # no lifespan: nothing touches a real database

    def tearDown(self):
        for p in self.patches:
            p.stop()
        app.dependency_overrides.clear()
        response_cache.clear()
        branch_service._cache["at"] = None
        self.client.close()
        self.engine.dispose()

    # ── helpers ──
    def add(self, source, values, allow_duplicate=False):
        return self.client.post(f"{API}/crm-leads/{source}", json={"values": values, "allow_duplicate": allow_duplicate})

    def add_ok(self, source, values=None) -> dict:
        resp = self.add(source, values or SAMPLE[source])
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["lead"]

    def listed_keys(self, source) -> list[str]:
        resp = self.client.get(f"{API}{LIST_URL[source]}", params={"page_size": 200})
        self.assertEqual(resp.status_code, 200, resp.text)
        return [i["crm"]["lead_key"] for i in resp.json()["items"]]

    def export_rows(self, source, fmt="xlsx", **params) -> list[list]:
        resp = self.client.get(f"{API}/crm-leads/{source}/export", params={"format": fmt, **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        if fmt == "csv":
            return list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
        ws = load_workbook(io.BytesIO(resp.content)).active
        return [[c.value for c in row] for row in ws.iter_rows()]

    def count(self, model) -> int:
        with Session(self.engine) as s:
            return len(s.exec(select(model)).all())

    # ── 1. crm_lead_meta for an added spin lead (steps 1-5) ──
    def test_crm_fields_of_an_added_spin_lead_go_to_crm_lead_meta(self):
        # 1. Add a spin lead through the API
        lead = self.add_ok("spin")
        key = lead["lead_key"]
        self.assertRegex(key, r"^crm-\d+$")
        with Session(self.engine) as s:
            before = s.exec(select(CrmSpinLead)).one().model_dump()

        # 2. Set Interest and Quoted amount from the API
        resp = self.client.put(f"{API}/leads/spin/{key}/crm", json={"interest": "interested", "quoted_amount": 45000})
        self.assertEqual(resp.status_code, 200, resp.text)

        with Session(self.engine) as s:
            # 3. Exactly one crm_lead_meta row, keyed like workflow leads, with the values
            metas = s.exec(select(LeadMeta)).all()
            self.assertEqual(len(metas), 1)
            self.assertEqual((metas[0].source, metas[0].lead_key), ("spin", key))
            self.assertEqual((metas[0].interest, metas[0].quoted_amount), ("interested", 45000))
            # 4. The crm_spin_leads row is unchanged, and that table has no CRM columns at all
            self.assertEqual(s.exec(select(CrmSpinLead)).one().model_dump(), before)
        columns = {c["name"] for c in inspect(self.engine).get_columns("crm_spin_leads")}
        crm_columns = {c["name"] for c in inspect(self.engine).get_columns("crm_lead_meta")} - {"id", "source", "lead_key", "updated_at"}
        self.assertEqual(columns & crm_columns, set(), "CRM fields must only live in crm_lead_meta")

        # 5. GET /spin-leads shows the values in the lead's crm object (next to the workflow lead)
        resp = self.client.get(f"{API}/spin-leads")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["total"], 2)
        added = next(i for i in data["items"] if i["id"] == key)
        self.assertEqual(added["origin"], "added")
        self.assertEqual((added["crm"]["interest"], added["crm"]["quoted_amount"]), ("interested", 45000))
        self.assertEqual(added["branch"]["name"], "Jayanagar")
        self.assertEqual(added["additional_details.pincode"], "560011")
        workflow = next(i for i in data["items"] if i["id"] == 101)
        self.assertEqual(workflow["origin"], "workflow")
        self.assertIsNone(workflow["crm"]["interest"])

    # ── 2. PUT /leads/{source}/{key}/crm and crm- keys ──
    def test_crm_put_404_for_unknown_crm_key_in_every_source(self):
        for source in SAMPLE:
            resp = self.client.put(f"{API}/leads/{source}/crm-doesnotexist/crm", json={"interest": "interested"})
            self.assertEqual(resp.status_code, 404, source)
        self.assertEqual(self.client.put(f"{API}/leads/spin/crm-999/crm", json={"notes": "x"}).status_code, 404)
        # Workflow ids and sheet keys work as before (no lookup)
        self.assertEqual(self.client.put(f"{API}/leads/spin/101/crm", json={"interest": "follow_up"}).status_code, 200)
        self.assertEqual(self.client.put(f"{API}/leads/wedding/abc123/crm", json={"notes": "n"}).status_code, 200)
        # A real added lead works in every source, archived or not
        for source in SAMPLE:
            key = self.add_ok(source)["lead_key"]
            self.assertEqual(self.client.put(f"{API}/leads/{source}/{key}/crm", json={"quoted_amount": 1}).status_code, 200)
            self.client.post(f"{API}/crm-leads/{source}/{key}/archive")
            self.assertEqual(self.client.put(f"{API}/leads/{source}/{key}/crm", json={"quoted_amount": 2}).status_code, 200)

    # ── 3. Add, duplicates, edit, archive for every source ──
    def test_add_duplicate_edit_archive_in_every_source(self):
        for source in SAMPLE:
            with self.subTest(source=source):
                lead = self.add_ok(source)
                key = lead["lead_key"]
                self.assertEqual(lead["origin"], "added")
                self.assertIn(key, self.listed_keys(source))

                # Same number written differently -> 409 existing_added, with created_at
                resp = self.add(source, {**SAMPLE[source], "phone": SAME_PHONE_OTHER_FORMAT[source]})
                self.assertEqual(resp.status_code, 409, resp.text)
                dup = resp.json()["detail"]["duplicate"]
                self.assertEqual((dup["reason"], dup["lead_key"]), ("existing_added", key))
                self.assertTrue(dup["created_at"])

                # allow_duplicate saves it anyway, with a warning
                resp = self.add(source, {**SAMPLE[source], "phone": SAME_PHONE_OTHER_FORMAT[source]}, allow_duplicate=True)
                self.assertEqual(resp.status_code, 201, resp.text)
                second = resp.json()["lead"]["lead_key"]
                self.assertEqual(resp.json()["warnings"][-1]["type"], "duplicate_allowed")

                # Edit: only the fields sent change
                field = "country" if source == "whatsapp" else "name"
                resp = self.client.patch(f"{API}/crm-leads/{source}/{key}", json={"values": {field: "Edited"}})
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(resp.json()["lead"]["values"][field], "Edited")
                self.assertEqual(self.client.get(f"{API}/crm-leads/{source}/{key}").json()["values"][field], "Edited")
                # Changing the phone to another lead's number is refused
                self.client.patch(f"{API}/crm-leads/{source}/{second}", json={"values": {"phone": "+447911123456"}})
                resp = self.client.patch(f"{API}/crm-leads/{source}/{key}", json={"values": {"phone": "+44 7911 123456"}})
                self.assertEqual(resp.status_code, 409, resp.text)

                # CRM details, then archive: hidden from list and export, phone still a duplicate, meta kept
                self.client.put(f"{API}/leads/{source}/{key}/crm", json={"interest": "interested"})
                resp = self.client.post(f"{API}/crm-leads/{source}/{key}/archive")
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertTrue(resp.json()["lead"]["archived"])
                self.assertNotIn(key, self.listed_keys(source))
                exported = self.export_rows(source)
                self.assertNotIn(key, {str(c) for row in exported for c in row})
                resp = self.add(source, SAMPLE[source])
                self.assertEqual(resp.status_code, 409)
                self.assertTrue(resp.json()["detail"]["duplicate"]["archived"])
                with Session(self.engine) as s:
                    meta = s.exec(select(LeadMeta).where(LeadMeta.source == source, LeadMeta.lead_key == key)).one()
                    self.assertEqual(meta.interest, "interested")

                # Unarchive: back in the list
                self.assertEqual(self.client.post(f"{API}/crm-leads/{source}/{key}/unarchive").status_code, 200)
                self.assertIn(key, self.listed_keys(source))

    def test_phone_is_required_and_normalised_to_e164(self):
        for source in SAMPLE:
            values = {k: v for k, v in SAMPLE[source].items() if k != "phone"}
            resp = self.add(source, values)
            self.assertEqual(resp.status_code, 422, source)
            self.assertEqual(resp.json()["detail"]["errors"][0]["field"], "phone")
        self.add_ok("wedding")
        with Session(self.engine) as s:
            lead = s.exec(select(WeddingLead)).one()
            self.assertEqual((lead.phone_e164, lead.phone, lead.phone_raw), ("+919845010003", "919845010003", "09845010003"))
        # International: country code or a Country is needed
        self.assertEqual(self.add("whatsapp", {"phone": "501234568"}).status_code, 422)
        lead = self.add_ok("whatsapp", {"phone": "50 123 4568", "country": "United Arab Emirates"})
        self.assertEqual(lead["values"]["phone"], "+971501234568")
        # Spin keeps the workflow's 10-digit display form for Indian numbers
        self.add_ok("spin", {"phone": "+91 98450 10009"})
        with Session(self.engine) as s:
            spin = s.exec(select(CrmSpinLead)).one()
            self.assertEqual((spin.phone, spin.phone_e164), ("9845010009", "+919845010009"))
        self.assertEqual(self.add("wedding", {"phone": "12345"}).status_code, 422)

    def test_duplicates_with_sheet_workflow_and_other_sources(self):
        with Session(self.engine) as s:
            s.add(WeddingLead(sheet_key="a" * 40, origin="sheet", phone="919845010003", phone_e164="+919845010003",
                              row_hash="", first_seen_at=NOW, updated_at=NOW))
            s.add(WeddingPlan(plan_id="KOC-GONE", origin="sheet", phone="919845010004", phone_e164="+919845010004",
                              row_hash="", is_deleted=True, first_seen_at=NOW, updated_at=NOW))   # removed from sheet
            s.commit()
        resp = self.add("wedding", SAMPLE["wedding"])
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"]["duplicate"], {"reason": "existing_sheet", "lead_key": "a" * 40})
        resp = self.add("wedding_planner", SAMPLE["wedding_planner"])
        self.assertEqual(resp.json()["detail"]["duplicate"]["reason"], "existing_sheet")   # removed rows count too
        resp = self.add("spin", {"phone": "9845099999"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"]["duplicate"], {"reason": "existing_workflow", "lead_key": "101"})
        # Not blocking: the phone exists in other sources
        resp = self.add("whatsapp", {"phone": "+91 98450 10003"})
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["warnings"], [{"type": "other_source", "sources": ["wedding"]}])
        resp = self.add("wedding", {"phone": "9845099999"})
        self.assertEqual(resp.json()["warnings"], [{"type": "other_source", "sources": ["spin"]}])

    def test_sheet_and_workflow_leads_cannot_be_edited_or_archived(self):
        with Session(self.engine) as s:
            s.add(WhatsAppLead(sheet_key="b" * 40, origin="sheet", phone="971501234567", row_hash="",
                               first_seen_at=NOW, updated_at=NOW))
            s.commit()
        self.assertEqual(self.client.patch(f"{API}/crm-leads/whatsapp/{'b' * 40}", json={"values": {"country": "X"}}).status_code, 409)
        self.assertEqual(self.client.post(f"{API}/crm-leads/whatsapp/{'b' * 40}/archive").status_code, 409)
        self.assertEqual(self.client.post(f"{API}/crm-leads/spin/101/archive").status_code, 409)
        self.assertEqual(self.client.get(f"{API}/crm-leads/wedding/crm-nothing").status_code, 404)

    # ── 4. Template, preview, import, export ──
    def test_template_preview_import_and_xlsx_export(self):
        resp = self.client.get(f"{API}/crm-leads/spin/template")
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, ["Leads", "Instructions", "Branches"])
        headers = [c.value for c in wb["Leads"][1]]
        self.assertEqual(headers[:5], ["Phone", "Name", "Email", "Occasion", "Branch"])
        self.assertEqual([r[0].value for r in wb["Branches"].iter_rows(min_row=2)], ["Chamrajpet", "Jayanagar", "Mysore"])

        existing = self.add_ok("spin", {"phone": "9845020000"})
        upload = xlsx_file([
            ["Phone", "Name", "Email", "Branch", "Pincode", "Occasion date", "Unknown column"],
            [9845020001, "=HYPERLINK(\"http://x\")", "a@example.com", "Jayanagar", 560011, date(2026, 12, 20), "x"],  # ok
            ["9845020002", "Bad email", "not-an-email", "Mysore", None, None, None],                               # error
            ["9845020003", "Bad branch", None, "Nowhere", None, None, None],                                       # error
            ["+91 98450 20001", "Same as row 2", None, None, None, None, None],                                    # in_file
            ["9845020000", "Already added", None, None, None, None, None],                                         # existing_added
            ["9845099999", "Workflow lead", None, None, None, None, None],                                         # existing_workflow
            [None, "No phone", None, None, None, None, None],                                                      # error
            ["9845020004", "Fine too", None, "chamrajpet", None, "20/12/2026", None],                              # ok
        ])
        resp = self.client.post(f"{API}/crm-leads/spin/import/preview", files={"file": ("leads.xlsx", upload, XLSX)})
        self.assertEqual(resp.status_code, 200, resp.text)
        preview = resp.json()
        self.assertEqual(preview["ignored_columns"], ["Unknown column"])
        status = {r["row"]: r["status"] for r in preview["rows"]}
        self.assertEqual(status, {2: "ok", 3: "error", 4: "error", 5: "duplicate", 6: "duplicate", 7: "duplicate",
                                  8: "error", 9: "ok"})
        by_row = {r["row"]: r for r in preview["rows"]}
        self.assertEqual(by_row[5]["duplicate"], {"reason": "in_file", "duplicate_of_row": 2})
        self.assertEqual(by_row[6]["duplicate"]["reason"], "existing_added")
        self.assertEqual(by_row[6]["duplicate"]["lead_key"], existing["lead_key"])
        self.assertTrue(by_row[6]["duplicate"]["created_at"])
        self.assertEqual(by_row[7]["duplicate"]["reason"], "existing_workflow")
        self.assertIn("Unknown branch", by_row[4]["errors"][0]["message"])
        self.assertEqual(preview["counts"]["duplicates_by_reason"],
                         {"in_file": 1, "existing_sheet": 0, "existing_workflow": 1, "existing_added": 1})
        self.assertEqual((preview["counts"]["ok"], preview["counts"]["errors"], preview["counts"]["duplicates"]), (2, 3, 3))
        self.assertEqual(self.count(CrmSpinLead), 1, "preview must not save anything")

        # Bulk save: the valid rows only, re-checked on the server
        resp = self.client.post(f"{API}/crm-leads/spin/import",
                                json={"rows": [{"row": r["row"], "values": r["values"]} for r in preview["rows"]]})
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertEqual((result["counts"]["imported"], result["counts"]["skipped"]), (2, 6))
        imported = [r for r in result["rows"] if r["status"] == "imported"]
        self.assertEqual({r["row"] for r in imported}, {2, 9})
        with Session(self.engine) as s:
            lead = s.exec(select(CrmSpinLead).where(CrmSpinLead.origin == "imported", CrmSpinLead.phone == "9845020001")).one()
            self.assertEqual(lead.branch_id, 4)
            self.assertEqual(lead.additional_details, {"pincode": "560011", "occasionDate": "2026-12-20"})
        # Importing the same file again: everything is now a duplicate or an error
        resp = self.client.post(f"{API}/crm-leads/spin/import",
                                json={"rows": [{"row": r["row"], "values": r["values"]} for r in preview["rows"]]})
        self.assertEqual(resp.json()["counts"]["imported"], 0)

        # Export (xlsx): formula-safe text, phone as text, dates DD-MM-YYYY, file name exposed to the browser
        resp = self.client.get(f"{API}/crm-leads/spin/export", headers={"Origin": ORIGIN})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertRegex(resp.headers["content-disposition"], r'^attachment; filename="spin-leads-\d{4}-\d{2}-\d{2}\.xlsx"$')
        self.assertIn("content-disposition", resp.headers["access-control-expose-headers"].lower())
        ws = load_workbook(io.BytesIO(resp.content)).active
        header = [c.value for c in ws[1]]
        rows = {str(r[header.index("Lead key")].value): r for r in ws.iter_rows(min_row=2)}
        self.assertEqual(len(rows), 4)                      # 1 workflow + 3 CRM
        row = next(r for r in rows.values() if r[header.index("Phone")].value == "9845020001")
        name_cell = row[header.index("Name")]
        self.assertEqual((name_cell.value, name_cell.data_type), ('=HYPERLINK("http://x")', "s"))
        self.assertEqual(row[header.index("Phone")].data_type, "s")
        date_cell = row[header.index("Occasion date")]
        self.assertEqual((date_cell.value.date(), date_cell.number_format), (date(2026, 12, 20), "DD-MM-YYYY"))
        self.assertEqual(row[header.index("Branch")].value, "Jayanagar")
        self.assertEqual(row[header.index("Origin")].value, "imported")
        workflow = rows["101"]
        self.assertEqual(workflow[header.index("Created")].value, datetime(2026, 11, 1, 10, 0))   # 04:30 UTC = 10:00 IST

    def test_csv_upload_and_csv_export(self):
        content = ("Contact Number;Full Name;Email Address;City;Budget;Timestamp\r\n"
                   "09845030001;=cmd|' /C calc'!A0;x@example.com;Bengaluru;50k;14-11-2026 10:30\r\n"
                   "9845030002;-Minus;;;;\r\n").encode("utf-8-sig")
        resp = self.client.post(f"{API}/crm-leads/wedding/import/preview", files={"file": ("w.csv", content, "text/csv")})
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = resp.json()["rows"]
        self.assertEqual([r["status"] for r in rows], ["ok", "ok"])
        resp = self.client.post(f"{API}/crm-leads/wedding/import", json={"rows": [{"row": r["row"], "values": r["values"]} for r in rows]})
        self.assertEqual(resp.json()["counts"]["imported"], 2)

        out = self.export_rows("wedding", "csv")
        header = out[0]
        first = next(r for r in out[1:] if r[header.index("Email")] == "x@example.com")
        self.assertEqual(first[header.index("Name")], "'=cmd|' /C calc'!A0")      # apostrophe: not a formula
        self.assertEqual(first[header.index("Phone")], "'+919845030001")           # text, formula-safe
        self.assertEqual(first[header.index("Submitted")], "14-11-2026 10:30")     # IST, DD-MM-YYYY
        self.assertEqual(first[header.index("Budget amount")], "50000")
        second = next(r for r in out[1:] if r[header.index("Name")] == "'-Minus")
        self.assertTrue(second)
        resp = self.client.get(f"{API}/crm-leads/wedding/export", params={"format": "csv"})
        self.assertRegex(resp.headers["content-disposition"], r'filename="wedding-leads-\d{4}-\d{2}-\d{2}\.csv"')

    def test_export_cap_and_upload_limits(self):
        self.add_ok("wedding")
        self.add_ok("wedding", {"phone": "9845040002"})
        with mock.patch.object(tabular, "EXPORT_MAX_ROWS", 1):
            resp = self.client.get(f"{API}/crm-leads/wedding/export")
            self.assertEqual(resp.status_code, 400)
            self.assertIn("limit is 1", resp.json()["detail"])
            self.assertEqual(self.client.get(f"{API}/crm-leads/spin/export").status_code, 200)   # 1 workflow lead
        big = b"Phone\n" + b"9845000000\n" * 600_000
        resp = self.client.post(f"{API}/crm-leads/wedding/import/preview", files={"file": ("big.csv", big, "text/csv")})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("5 MB", resp.json()["detail"])
        many = xlsx_file([["Phone"]] + [[f"98450{i:05d}"] for i in range(5_001)])
        resp = self.client.post(f"{API}/crm-leads/wedding/import/preview", files={"file": ("many.xlsx", many, XLSX)})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("5,000 rows", resp.json()["detail"])
        no_phone = xlsx_file([["Name"], ["x"]])
        resp = self.client.post(f"{API}/crm-leads/wedding/import/preview", files={"file": ("n.xlsx", no_phone, XLSX)})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(f"{API}/crm-leads/wedding/import/preview", files={"file": ("x.xlsx", b"PK\x03\x04junk", XLSX)})
        self.assertEqual(resp.status_code, 400)

    def test_preview_sheet_name(self):
        def workbook(sheets: dict[str, list[list]]) -> bytes:
            wb = Workbook()
            wb.remove(wb.active)
            for title, rows in sheets.items():
                ws = wb.create_sheet(title)
                for r in rows:
                    ws.append(r)
            buf = io.BytesIO()
            wb.save(buf)
            return buf.getvalue()

        def preview(content: bytes, sheet_name=None, filename="leads.xlsx", media=XLSX):
            data = {"sheet_name": sheet_name} if sheet_name is not None else {}
            return self.client.post(f"{API}/crm-leads/wedding/import/preview",
                                    files={"file": (filename, content, media)}, data=data)

        def phones(resp) -> list[str]:
            self.assertEqual(resp.status_code, 200, resp.text)
            return [r["values"]["phone"] for r in resp.json()["rows"]]

        content = workbook({
            "Instructions": [["Read me first"]],
            "Leads": [["Phone"], ["9845050001"]],
            "March batch": [["Phone"], ["9845050002"], ["9845050003"]],
        })
        self.assertEqual(phones(preview(content)), ["9845050001"])                     # default: "Leads"
        self.assertEqual(phones(preview(content, "")), ["9845050001"])                 # empty = default
        self.assertEqual(phones(preview(content, "March batch")), ["9845050002", "9845050003"])
        self.assertEqual(phones(preview(content, "  march BATCH ")), ["9845050002", "9845050003"])  # case/spaces

        resp = preview(content, "April")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], 'There is no sheet named "April" in this file. '
                                                'Sheets in the file: "Instructions", "Leads", "March batch".')
        self.assertEqual(preview(content, "x" * 32).status_code, 422)                 # Excel names are <= 31 chars

        # No "Leads" sheet: the first sheet is read
        no_leads = workbook({"Batch 1": [["Phone"], ["9845050004"]], "Batch 2": [["Phone"], ["9845050005"]]})
        self.assertEqual(phones(preview(no_leads)), ["9845050004"])
        # CSV has no sheets: sheet_name is ignored
        self.assertEqual(phones(preview(b"Phone\n9845050006\n", "Anything", "w.csv", "text/csv")), ["9845050006"])
        self.assertEqual(self.count(WeddingLead), 0, "preview must not save anything")

    def test_fields_and_backfill(self):
        resp = self.client.get(f"{API}/crm-leads/spin/fields")
        self.assertEqual(resp.json()["fields"][0], {"key": "phone", "label": "Phone", "type": "phone", "required": True,
                                                    "max_length": 20, "help": resp.json()["fields"][0]["help"]})
        self.assertEqual([b["name"] for b in resp.json()["branches"]], ["Chamrajpet", "Jayanagar", "Mysore"])
        for source in ("whatsapp", "wedding", "wedding_planner"):
            phone = next(f for f in self.client.get(f"{API}/crm-leads/{source}/fields").json()["fields"] if f["key"] == "phone")
            self.assertTrue(phone["required"])
        # Sheet rows synced before phone_e164 existed get it filled in
        with Session(self.engine) as s:
            s.add(WhatsAppLead(sheet_key="c" * 40, origin="sheet", phone="971501234567", row_hash="", first_seen_at=NOW, updated_at=NOW))
            s.add(WeddingLead(sheet_key="d" * 40, origin="sheet", phone="12", row_hash="", first_seen_at=NOW, updated_at=NOW))
            s.commit()
            self.assertEqual(service.backfill_phone_e164(s), 2)
            self.assertEqual(s.exec(select(WhatsAppLead)).one().phone_e164, "+971501234567")
            self.assertEqual(s.exec(select(WeddingLead)).one().phone_e164, "")       # unreadable: checked, none
            self.assertEqual(service.backfill_phone_e164(s), 0)


if __name__ == "__main__":
    unittest.main()
