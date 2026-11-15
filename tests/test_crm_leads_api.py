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
import re  # noqa: E402
import unittest  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402
from sqlalchemy import BigInteger, Column, MetaData, Table, inspect  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app.api.deps import get_current_user  # noqa: E402
from app.api.v1.endpoints import spin_leads as spin_ep  # noqa: E402
from app.api.v1.endpoints import vendors as vendors_ep  # noqa: E402
from app.core import response_cache  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.branch import Branch  # noqa: E402
from app.models.crm_spin_lead import CrmSpinLead  # noqa: E402
from app.models.lead_meta import LeadMeta  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.vendor import Vendor  # noqa: E402
from app.models.vendor_user import VendorUser  # noqa: E402
from app.models.wedding_lead import WeddingLead  # noqa: E402
from app.models.wedding_plan import WeddingPlan  # noqa: E402
from app.models.whatsapp_lead import WhatsAppLead  # noqa: E402
from app.services import branches as branch_service  # noqa: E402
from app.services import phones  # noqa: E402
from app.services.crm_leads import service, tabular  # noqa: E402
from app.services.lead_crm import seed_vendors  # noqa: E402
from app.services.stores import STORES  # noqa: E402

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
    "wedding_planner": {"phone": "+91 98450 10004", "name": "Meera", "city": "Udaipur",
                        "wedding_month": "December 2027", "total_guests": "300", "priority1": "Food"},
}
# Who SAMPLE leads are assigned to automatically (branch / area rules)
SAMPLE_VENDOR = {"spin": "phodeo", "whatsapp": "ohp", "wedding": "phodeo", "wedding_planner": "ohp"}
SAME_PHONE_OTHER_FORMAT = {
    "spin": "+919845010001", "whatsapp": "00971501234567", "wedding": "+91 98450 10003", "wedding_planner": "9845010004",
}
LIST_URL = {
    "spin": "/spin-leads", "whatsapp": "/whatsapp-leads", "wedding": "/wedding-leads", "wedding_planner": "/wedding-plans",
}
STATS_URL = {s: f"{u}/stats" for s, u in LIST_URL.items() if s != "spin"}

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
    {"name": "additional_details.estimatedBudgetPerCard", "type": "range", "json_field": True,
     "source": "additional_details", "key": "estimatedBudgetPerCard"},
]
WORKFLOW_ROWS = [
    {"id": 101, "name": "Workflow One", "phone": "9845099999", "created_at": "2026-11-01T04:30:00", "branch_id": 1,
     "additional_details.pincode": "560018", "additional_details.occasionDate": "2026-12-20",
     "additional_details.estimatedBudgetPerCard": "10-20"},
]
WORKFLOW_PHONE = "+919845099999"


def fake_list_spin_leads(_admin=None, page=1, page_size=50, search=None, sort=None, order="asc", branch=None,
                         filters=None, offset=None, limit=None):
    start = (page - 1) * page_size if offset is None else offset
    count = page_size if limit is None else limit
    rows = WORKFLOW_ROWS
    if filters and filters.get("ids_in") is not None:
        rows = [r for r in rows if str(r["id"]) in {str(i) for i in filters["ids_in"]}]
    if filters and filters.get("created_from") is not None:      # created_at in [from, to), naive UTC
        rows = [r for r in rows if filters["created_from"] <= datetime.fromisoformat(r["created_at"]) < filters["created_to"]]
    return {"columns": WORKFLOW_COLUMNS, "items": [dict(r) for r in rows[start:start + count]], "total": len(rows),
            "page": page, "page_size": page_size, "branch_column": "branch_id"}


FAKE_TABLE = Table("spin_lead", MetaData(), Column("id", BigInteger, primary_key=True))


def fake_workflow_phones(e164s):
    found = {}
    for r in WORKFLOW_ROWS:
        e164 = phones.normalize(r["phone"], lenient=True).e164
        if e164 in e164s:
            found.setdefault(e164, []).append(str(r["id"]))
    return found


def fake_workflow_spin_rows(db):
    """What vendors._workflow_spin_rows reads from PostgreSQL, from the fake rows."""
    branches = branch_service.branches_by_id(db)
    out = []
    for r in WORKFLOW_ROWS:
        branch = branches.get(str(r["branch_id"])) if r.get("branch_id") is not None else None
        out.append((str(r["id"]), (branch or {}).get("city"), r.get("additional_details.pincode"), None,
                    (branch or {}).get("vendor_id")))
    return out


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


class ApiTestBase:
    """In-memory database, the three default vendors (RJ, Phodeo, OHP) with one login each, an
    admin, a vendor login without a vendor, and a fake workflow database."""

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine, expire_on_commit=False) as s:
            for bid, name, code, city in ((1, "Chamrajpet", "CHA-001", "Bangalore"), (4, "Jayanagar", "JNG-004", "Bangalore"),
                                          (10, "Mysore", "MYS-010", "Mysore")):
                s.add(Branch(id=bid, name=name, code=code, city=city, source="workflow", created_at=NOW, updated_at=NOW))
            s.commit()
            seed_vendors(s)
            self.vendor_ids = {v.name.lower(): v.id for v in s.exec(select(Vendor))}
            self.users = {"admin": User(name="Admin", email="admin@example.com", hashed_password="x", role="admin")}
            for who in ("phodeo", "rj", "ohp", "unlinked"):
                self.users[who] = User(name=who, email=f"{who}@example.com", hashed_password="x", role="vendor")
            s.add_all(self.users.values())
            s.commit()
            for who in ("phodeo", "rj", "ohp"):
                s.add(VendorUser(user_id=self.users[who].id, vendor_id=self.vendor_ids[who]))
            s.commit()
            for u in self.users.values():
                s.refresh(u)
        self.admin = self.users["admin"]
        self.current = self.admin

        def override_get_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: self.current
        self.patches = [
            mock.patch.object(spin_ep, "list_spin_leads", fake_list_spin_leads),
            mock.patch.object(spin_ep, "_get_table", lambda: FAKE_TABLE),
            mock.patch.object(service, "workflow_phones", fake_workflow_phones),
            mock.patch.object(vendors_ep, "_workflow_spin_rows", fake_workflow_spin_rows),
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
    @contextmanager
    def logged_in(self, who: str):
        previous, self.current = self.current, self.users[who]
        try:
            yield
        finally:
            self.current = previous

    def add(self, source, values, allow_duplicate=False):
        with self.logged_in("admin"):
            return self.client.post(f"{API}/crm-leads/{source}", json={"values": values, "allow_duplicate": allow_duplicate})

    def add_ok(self, source, values=None) -> dict:
        """Add one lead. Spin has no single add (405): it goes through the bulk import, with
        automatic assignment unless "assigned_to" is given."""
        values = values or SAMPLE[source]
        if source == "spin":
            row = self.import_spin({"assigned_to": "Phodeo · auto", **values})
            self.assertEqual(row["status"], "imported", row)
            return {"lead_key": row["lead_key"], "origin": "imported"}
        resp = self.add(source, values)
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["lead"]

    def import_spin(self, values: dict) -> dict:
        """POST /crm-leads/spin/import with one row; returns that row's result."""
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}/crm-leads/spin/import", json={"rows": [{"row": 2, "values": values}]})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["rows"][0]

    def put_crm(self, who, source, key, body):
        with self.logged_in(who):
            return self.client.put(f"{API}/leads/{source}/{key}/crm", json=body)

    def list_items(self, source, **params) -> list[dict]:
        resp = self.client.get(f"{API}{LIST_URL[source]}", params={"page_size": 200, **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["items"]

    def listed_keys(self, source) -> list[str]:
        return [i["crm"]["lead_key"] for i in self.list_items(source)]

    def export_rows(self, source, fmt="xlsx", **params) -> list[list]:
        resp = self.client.get(f"{API}/crm-leads/{source}/export", params={"format": fmt, **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        if fmt == "csv":
            return list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
        ws = load_workbook(io.BytesIO(resp.content)).active
        return [[c.value for c in row] for row in ws.iter_rows()]

    def export_sheet(self, source, **params):
        resp = self.client.get(f"{API}/crm-leads/{source}/export", params={"format": "xlsx", **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        return load_workbook(io.BytesIO(resp.content)).active

    def count(self, model) -> int:
        with Session(self.engine) as s:
            return len(s.exec(select(model)).all())


class CrmLeadsApiTest(ApiTestBase, unittest.TestCase):
    # ── 1. crm_lead_meta for an added spin lead (steps 1-5) ──
    def test_crm_fields_of_an_added_spin_lead_go_to_crm_lead_meta(self):
        # 1. Add a spin lead through the API
        lead = self.add_ok("spin")
        key = lead["lead_key"]
        self.assertRegex(key, r"^crm-\d+$")
        with Session(self.engine) as s:
            before = s.exec(select(CrmSpinLead)).one().model_dump()

        # 2. Set Interest and Quoted amount from the API, as a user of the assigned vendor
        #    (Jayanagar is a Phodeo branch); an admin may not set them
        resp = self.put_crm("admin", "spin", key, {"interest": "interested", "quoted_amount": 45000})
        self.assertEqual(resp.status_code, 403, resp.text)
        resp = self.put_crm("phodeo", "spin", key, {"interest": "interested", "quoted_amount": 45000})
        self.assertEqual(resp.status_code, 200, resp.text)

        with Session(self.engine) as s:
            # 3. Exactly one crm_lead_meta row, keyed like workflow leads, with the values
            metas = s.exec(select(LeadMeta)).all()
            self.assertEqual(len(metas), 1)
            self.assertEqual((metas[0].source, metas[0].lead_key), ("spin", key))
            self.assertEqual((metas[0].interest, metas[0].quoted_amount), ("interested", 45000))
            self.assertEqual(metas[0].updated_by, self.users["phodeo"].id)
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
        self.assertEqual(added["origin"], "imported")         # spin leads come in by bulk upload
        self.assertEqual((added["crm"]["interest"], added["crm"]["quoted_amount"]), ("interested", 45000))
        self.assertEqual(added["branch"]["name"], "Jayanagar")
        self.assertEqual(added["additional_details.pincode"], "560011")
        workflow = next(i for i in data["items"] if i["id"] == 101)
        self.assertEqual(workflow["origin"], "workflow")
        self.assertIsNone(workflow["crm"]["interest"])

    # ── 2. PUT /leads/{source}/{key}/crm and crm- keys ──
    def test_crm_put_404_for_unknown_crm_key_in_every_source(self):
        for source in SAMPLE:
            resp = self.client.put(f"{API}/leads/{source}/crm-doesnotexist/crm", json={"notes": "x"})
            self.assertEqual(resp.status_code, 404, source)
        self.assertEqual(self.client.put(f"{API}/leads/spin/crm-999/crm", json={"notes": "x"}).status_code, 404)
        # Workflow ids and sheet keys work as before (no lookup)
        self.assertEqual(self.client.put(f"{API}/leads/spin/101/crm", json={"notes": "n"}).status_code, 200)
        self.assertEqual(self.client.put(f"{API}/leads/wedding/abc123/crm", json={"notes": "n"}).status_code, 200)
        # A real added lead works in every source, for the admin archived or not; the assigned
        # vendor can't see archived leads (hidden everywhere)
        for source in SAMPLE:
            key = self.add_ok(source)["lead_key"]
            vendor = SAMPLE_VENDOR[source]
            self.assertEqual(self.put_crm(vendor, source, key, {"quoted_amount": 1}).status_code, 200, source)
            self.client.post(f"{API}/crm-leads/{source}/{key}/archive")
            self.assertEqual(self.put_crm("admin", source, key, {"notes": "archived"}).status_code, 200)
            self.assertEqual(self.put_crm(vendor, source, key, {"quoted_amount": 2}).status_code, 404)

    # ── 3. Add, duplicates, edit, archive (sources with a single add) ──
    def test_add_duplicate_edit_archive_in_every_source(self):
        for source in ("whatsapp", "wedding", "wedding_planner"):
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

                # CRM details (by the assigned vendor), then archive: hidden from list and export,
                # phone still a duplicate, meta kept
                resp = self.put_crm(SAMPLE_VENDOR[source], source, key, {"interest": "interested"})
                self.assertEqual(resp.status_code, 200, resp.text)
                exported_before = len(self.export_rows(source))
                resp = self.client.post(f"{API}/crm-leads/{source}/{key}/archive")
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertTrue(resp.json()["lead"]["archived"])
                self.assertNotIn(key, self.listed_keys(source))
                self.assertEqual(len(self.export_rows(source)), exported_before - 1)
                resp = self.add(source, SAMPLE[source])
                self.assertEqual(resp.status_code, 409)
                self.assertTrue(resp.json()["detail"]["duplicate"]["archived"])
                with Session(self.engine) as s:
                    meta = s.exec(select(LeadMeta).where(LeadMeta.source == source, LeadMeta.lead_key == key)).one()
                    self.assertEqual(meta.interest, "interested")

                # Unarchive: back in the list
                self.assertEqual(self.client.post(f"{API}/crm-leads/{source}/{key}/unarchive").status_code, 200)
                self.assertIn(key, self.listed_keys(source))

    def test_spin_has_no_single_add(self):
        resp = self.add("spin", SAMPLE["spin"])
        self.assertEqual((resp.status_code, resp.json()["detail"]), (405, "Spin leads can only be added by bulk upload"))
        self.assertEqual(self.count(CrmSpinLead), 0)
        # Imported spin leads can still be edited and archived by an admin
        key = self.add_ok("spin")["lead_key"]
        resp = self.client.patch(f"{API}/crm-leads/spin/{key}", json={"values": {"name": "Edited"}})
        self.assertEqual((resp.status_code, resp.json()["lead"]["values"]["name"]), (200, "Edited"))
        self.assertEqual(self.client.post(f"{API}/crm-leads/spin/{key}/archive").status_code, 200)
        self.assertNotIn(key, self.listed_keys("spin"))

    def test_phone_is_required_and_normalised_to_e164(self):
        for source in ("whatsapp", "wedding", "wedding_planner"):
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
        row = self.import_spin({"phone": "9845099999"})
        self.assertEqual((row["status"], row["duplicate"]), ("duplicate", {"reason": "existing_workflow", "lead_key": "101"}))
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
        self.assertEqual(wb.sheetnames, ["Leads", "Instructions", "Lists"])
        headers = [c.value for c in wb["Leads"][1]]
        self.assertEqual(headers, ["Lead", "Email", "Contact", "Branch", "Branch code", "Location", "Occasion",
                                   "Event date", "Budget", "Assigned to", "Interest", "Quoted amount", "Quoted date",
                                   "Converted", "Created"])
        lists = wb["Lists"]
        self.assertEqual([r[0].value for r in lists.iter_rows(min_row=2) if r[0].value],    # Branches page + built-in stores
                         sorted((s["name"] for s in STORES.values()), key=str.lower))
        # Assigned to choices: Automatic, Unassigned, then each active vendor (priority order)
        self.assertEqual([r[1].value for r in lists.iter_rows(min_row=2) if r[1].value],
                         ["Automatic", "Unassigned", "RJ", "Phodeo", "OHP"])
        # The example rows use dropdown values only
        for source in ("spin", "whatsapp", "wedding", "wedding_planner"):
            book = load_workbook(io.BytesIO(self.client.get(f"{API}/crm-leads/{source}/template").content))
            head = [c.value for c in book["Leads"][1]]
            example = book["Leads"].cell(row=2, column=head.index("Assigned to") + 1).value
            self.assertIn(example, (None, "Automatic", "Unassigned", "RJ", "Phodeo", "OHP"), source)

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
            ["9845020004", "Fine too", None, "chamrajpet", None, "20 Dec 2026", None],                             # ok
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

        # Export (xlsx): the list screen's columns and formats, formula-safe, file name exposed to the browser
        resp = self.client.get(f"{API}/crm-leads/spin/export", headers={"Origin": ORIGIN})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertRegex(resp.headers["content-disposition"], r'^attachment; filename="spin-leads-\d{4}-\d{2}-\d{2}\.xlsx"$')
        self.assertIn("content-disposition", resp.headers["access-control-expose-headers"].lower())
        ws = load_workbook(io.BytesIO(resp.content)).active
        header = [c.value for c in ws[1]]
        self.assertEqual(header, headers)                   # same columns as the template
        rows = {r[header.index("Contact")].value: r for r in ws.iter_rows(min_row=2)}
        self.assertEqual(len(rows), 4)                      # 1 workflow + 3 CRM
        row = rows["+919845020001"]
        name_cell = row[header.index("Lead")]
        self.assertEqual((name_cell.value, name_cell.data_type), ('=HYPERLINK("http://x")', "s"))
        self.assertEqual(row[header.index("Contact")].data_type, "s")
        date_cell = row[header.index("Event date")]
        self.assertEqual((date_cell.value.date(), date_cell.number_format), (date(2026, 12, 20), "dd mmm yyyy"))
        self.assertEqual(row[header.index("Branch")].value, "Jayanagar")
        self.assertEqual(row[header.index("Branch code")].value, "JNG-004 · Bangalore")       # code · city, as on screen
        self.assertEqual(row[header.index("Location")].value, "Bangalore · 560011")
        self.assertEqual(row[header.index("Assigned to")].value, "Unassigned")      # uploaded with it empty
        workflow = rows["+919845099999"]
        created = workflow[header.index("Created")]
        self.assertEqual((created.value, created.number_format),
                         (datetime(2026, 11, 1, 10, 0), "d mmm yyyy, hh:mm am/pm"))   # 04:30 UTC = 10:00 IST
        self.assertEqual(workflow[header.index("Budget")].value, "₹10–20")
        self.assertEqual(workflow[header.index("Location")].value, "Bangalore · 560018")
        self.assertEqual(workflow[header.index("Assigned to")].value, "RJ")         # Chamrajpet branch
        self.assertEqual(workflow[header.index("Converted")].value, "No")

    def test_spin_branches_not_in_crm_branches(self):
        """The real database's crm_branches can be empty: exports must still show the screen's
        branch (built-in stores), never "Branch <id>", and upload back as already existing."""
        with Session(self.engine) as s:
            for b in s.exec(select(Branch)).all():
                s.delete(b)
            s.commit()
        branch_service._cache["at"] = None
        rows = [
            {"id": 201, "name": "Kondhapur lead", "phone": "9845700001", "created_at": "2026-11-01T04:30:00",
             "branch_id": 7, "additional_details.pincode": "500084", "additional_details.occasionDate": "2026-12-20",
             "additional_details.estimatedBudgetPerCard": "10-20"},
            {"id": 202, "name": "Banjara lead", "phone": "9845700002", "created_at": "2026-11-02T04:30:00",
             "branch_id": 12, "additional_details.pincode": None, "additional_details.occasionDate": None,
             "additional_details.estimatedBudgetPerCard": None},
            {"id": 203, "name": "Unknown branch lead", "phone": "9845700003", "created_at": "2026-11-03T04:30:00",
             "branch_id": 99, "additional_details.pincode": None, "additional_details.occasionDate": None,
             "additional_details.estimatedBudgetPerCard": None},
        ]
        with mock.patch(f"{__name__}.WORKFLOW_ROWS", rows):
            added = self.import_spin({"phone": "9845700009", "name": "Uploaded", "branch": "Hyderabad - Kondhapur"})
            self.assertEqual(added["status"], "imported", added)
            with Session(self.engine) as s:
                self.assertEqual(s.exec(select(CrmSpinLead)).one().branch_id, 7)

            content = self.client.get(f"{API}/crm-leads/spin/export").content
            ws = load_workbook(io.BytesIO(content)).active
            header = [c.value for c in ws[1]]
            by_lead = {r[0]: r for r in ws.iter_rows(min_row=2, values_only=True)}
            col = header.index
            self.assertEqual(by_lead["Kondhapur lead"][col("Branch")], "Hyderabad - Kondhapur")
            self.assertEqual(by_lead["Kondhapur lead"][col("Branch code")], "HYB-007 · Hyderabad")
            self.assertEqual(by_lead["Kondhapur lead"][col("Location")], "Hyderabad · 500084")
            self.assertEqual(by_lead["Banjara lead"][col("Branch")], "Banjara Hills Hyderabad")
            self.assertEqual(by_lead["Banjara lead"][col("Branch code")], "BANJARA-009 · Hyderabad")
            self.assertEqual(by_lead["Banjara lead"][col("Location")], "Hyderabad")          # no pincode: branch city
            self.assertEqual(by_lead["Uploaded"][col("Branch")], "Hyderabad - Kondhapur")
            unknown = by_lead["Unknown branch lead"]
            self.assertEqual((unknown[col("Branch")], unknown[col("Branch code")], unknown[col("Location")]),
                             (None, None, None))
            cells = [str(v) for r in ws.iter_rows(min_row=2, values_only=True) for v in r if v is not None]
            self.assertFalse([v for v in cells if re.match(r"^Branch #?\d", v)], "never a made-up branch name")

            # Uploading the export back: every row already exists, nothing to fix
            preview = self.client.post(f"{API}/crm-leads/spin/import/preview",
                                       files={"file": ("spin.xlsx", content, XLSX)}).json()
            counts = preview["counts"]
            self.assertEqual((counts["total"], counts["ok"], counts["errors"], counts["duplicates"]), (4, 0, 0, 4))
            self.assertEqual(counts["duplicates_by_reason"],
                             {"in_file": 0, "existing_sheet": 0, "existing_workflow": 3, "existing_added": 1})
            self.assertEqual([r["errors"] for r in preview["rows"]], [[], [], [], []])

    def test_duplicate_wins_over_field_errors_in_every_source(self):
        """existing lead > repeat in the file > field errors."""
        with Session(self.engine) as s:
            sheet = {"origin": "sheet", "row_hash": "", "first_seen_at": NOW, "updated_at": NOW}
            s.add(WhatsAppLead(sheet_key="w" * 40, phone="971500000001", phone_e164="+971500000001", **sheet))
            s.add(WeddingLead(sheet_key="d" * 40, phone="919845800001", phone_e164="+919845800001", **sheet))
            s.add(WeddingPlan(plan_id="KOC-DUP", phone="919845800001", phone_e164="+919845800001", **sheet))
            s.commit()
        existing = {"spin": ("9845099999", "existing_workflow"), "whatsapp": ("+971500000001", "existing_sheet"),
                    "wedding": ("9845800001", "existing_sheet"), "wedding_planner": ("9845800001", "existing_sheet")}
        bad = {"spin": ("Email", "not-an-email"), "whatsapp": ("Submitted", "not a date"),
               "wedding": ("Email", "not-an-email"), "wedding_planner": ("Email", "not-an-email")}
        for source, (phone, reason) in existing.items():
            with self.subTest(source=source):
                column, value = bad[source]
                new_a, new_b = ("+971500000077", "+971500000088") if source == "whatsapp" else ("9845800077", "9845800088")
                phone_col = "Lead" if source == "whatsapp" else "Contact"
                upload = xlsx_file([
                    [phone_col, column],
                    [phone, value],          # 2: existing lead, with a field error -> existing duplicate
                    [new_a, value],          # 3: new phone, field error -> error
                    [new_a, None],           # 4: repeats row 3 -> in_file
                    [new_b, value],          # 5: new phone, field error -> error
                    [new_b, value],          # 6: repeats row 5, also with an error -> in_file
                ])
                body = self.client.post(f"{API}/crm-leads/{source}/import/preview",
                                        files={"file": ("d.xlsx", upload, XLSX)}).json()
                rows = {r["row"]: r for r in body["rows"]}
                self.assertEqual([rows[n]["status"] for n in range(2, 7)],
                                 ["duplicate", "error", "duplicate", "error", "duplicate"])
                self.assertEqual(rows[2]["duplicate"]["reason"], reason)
                self.assertTrue(rows[2]["errors"])                       # still reported, for information
                self.assertEqual(rows[4]["duplicate"], {"reason": "in_file", "duplicate_of_row": 3})
                self.assertEqual(rows[6]["duplicate"], {"reason": "in_file", "duplicate_of_row": 5})
                self.assertTrue(rows[6]["errors"])
                counts = body["counts"]
                self.assertEqual((counts["ok"], counts["errors"], counts["duplicates"]), (0, 2, 3))
                self.assertEqual(counts["duplicates_by_reason"][reason], 1)
                self.assertEqual(counts["duplicates_by_reason"]["in_file"], 2)

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
        self.assertEqual(header, ["Lead", "Email", "Contact", "Occasion", "Event date", "City", "Budget", "Assigned to",
                                  "Interest", "Quoted amount", "Quoted date", "Converted", "Submitted"])
        first = next(r for r in out[1:] if r[header.index("Email")] == "x@example.com")
        self.assertEqual(first[header.index("Lead")], "'=cmd|' /C calc'!A0")       # apostrophe: not a formula
        self.assertEqual(first[header.index("Contact")], "'+919845030001")          # text, formula-safe
        self.assertEqual(first[header.index("Submitted")], "14 Nov 2026, 10:30 am")  # IST, as on screen
        self.assertEqual(first[header.index("Budget")], "50000")
        self.assertEqual(first[header.index("Assigned to")], "Unassigned")
        second = next(r for r in out[1:] if r[header.index("Lead")] == "'-Minus")
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
        fields = resp.json()["fields"]
        phone = next(f for f in fields if f["key"] == "phone")
        self.assertEqual(phone, {"key": "phone", "label": "Contact", "type": "phone", "required": True,
                                 "max_length": 20, "help": phone["help"]})
        self.assertEqual([f["key"] for f in fields if f["required"]], ["phone"])     # only the phone is required
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
