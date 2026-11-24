"""Vendor to KOC tests: scoping, admin-only fields, file preview, bulk save, manual add,
list filters, vendors and vendor logins. In-memory SQLite and fake data only."""
import csv
import io
import unittest
from unittest import mock
from datetime import date, datetime, timedelta

from tests.base import ORIGIN, PASSWORD, AuthTestCase

import openpyxl  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.crud.vendor_to_koc import utcnow  # noqa: E402
from app.models.refresh_token import RefreshToken  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.vendor import Vendor  # noqa: E402
from app.models.vendor_to_koc import ADMIN_ONLY_FIELDS, VendorToKocLead  # noqa: E402
from app.models.vendor_user import VendorUser  # noqa: E402
from app.services import notifications as notification_service  # noqa: E402
from app.api.v1.endpoints.vendor_to_koc import ONLY_VENDORS_ADD  # noqa: E402
from app.services.vendor_lead_import import clean_phone, parse_file_date  # noqa: E402

BASE = "/api/v1/vendor-to-koc"
PASSWORD_HASH = hash_password(PASSWORD)   # hash once; Argon2 is slow on purpose
PHODEO_HEADINGS = ["Sl. No", "Date", "Name", "Event date", "Phone", "City", "Status", "Comment",
                   "Quick reply", "campaign sent", "2nd campaign sent", "3rd campaign sent"]
ADMIN_FIELDS = {"status_updated_by"}                       # a user id: admins only
STATUS_FIELDS = {"update_status", "additional_comments", "follow_up_date", "quoted_amount", "conversion_amount",
                 "status_updated_at", "status_updated_by_name"}   # everyone (vendors: read-only)


def phodeo_rows():
    """Rows shaped like the real sheet "Phodeo Leads to KOC" (fake people and numbers)."""
    tail = ["Interested", "call in the evening", "Yes", "Yes", None, None]
    return [
        [1, datetime(2026, 1, 5), "Asha Rao ", datetime(2026, 2, 14), 8095852272.0, " Bengaluru ", *tail],
        [2, "13-01-2026", "Ravi Kumar", None, "+91 98450-12345", "Mysuru", *tail],
        [3, None, "Meena", None, "0 98450 54321", None, *tail],
        [4, "05-01-2026", "Short Phone", None, 984501234, "Pune", *tail],          # 9 digits
        [5, "05-01-2026", "asha  RAO", None, 8095852272, "bengaluru", *tail],      # repeats row 2 (any case/spacing)
        [6, "07-01-2026", "Already Saved", None, "9123456789", "Chennai", *tail],  # already saved
        [7, "03-04-2026", "Day First", None, "919876543210", "Goa", *tail],        # 3 April
        [None] * 12,                                                                # empty row
        [9, "2026/01/05", "Bad Date", None, "7012345678", "Kochi", *tail],
    ]


SAVED = {"name": "Already Saved", "lead_date": date(2026, 1, 7), "city": "Chennai"}

EXPECTED_PREVIEW = [
    {"row": 2, "name": "Asha Rao", "date": "2026-01-05", "phone": "8095852272", "city": "Bengaluru",
     "errors": [], "duplicate": False},
    {"row": 3, "name": "Ravi Kumar", "date": "2026-01-13", "phone": "9845012345", "city": "Mysuru",
     "errors": [], "duplicate": False},
    {"row": 4, "name": "Meena", "date": None, "phone": "9845054321", "city": None,
     "errors": [], "duplicate": False},
    {"row": 5, "name": "Short Phone", "date": "2026-01-05", "phone": "984501234", "city": "Pune",
     "errors": [{"field": "phone", "message": "Phone must be a 10-digit Indian mobile number "
                                              "starting with 6, 7, 8 or 9"}], "duplicate": False},
    {"row": 6, "name": "asha  RAO", "date": "2026-01-05", "phone": "8095852272", "city": "bengaluru",
     "errors": [], "duplicate": True},
    {"row": 7, "name": "Already Saved", "date": "2026-01-07", "phone": "9123456789", "city": "Chennai",
     "errors": [], "duplicate": True},
    {"row": 8, "name": "Day First", "date": "2026-04-03", "phone": "9876543210", "city": "Goa",
     "errors": [], "duplicate": False},
    {"row": 10, "name": "Bad Date", "date": "2026/01/05", "phone": "7012345678", "city": "Kochi",
     "errors": [{"field": "date", "message": "Date must be a date, or text like 13-12-2026 or 13 Dec 2026"}],
     "duplicate": False},
]


def make_xlsx(rows, headings=PHODEO_HEADINGS, title="Phodeo Leads to KOC", extra_sheets=()) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    ws.append(headings)
    for row in rows:
        ws.append(row)
    for name, sheet_rows in extra_sheets:
        extra = wb.create_sheet(name)
        for row in sheet_rows:
            extra.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_csv(rows, headings=PHODEO_HEADINGS) -> bytes:
    def cell(v):
        if isinstance(v, datetime):
            return v.strftime("%d-%m-%Y")
        return "" if v is None else str(v)       # 8095852272.0 stays "8095852272.0"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headings)
    for row in rows:
        writer.writerow([cell(v) for v in row])
    return buf.getvalue().encode("utf-8-sig")


class VendorToKocTestCase(AuthTestCase):
    def setUp(self):
        super().setUp()
        now = utcnow()
        with Session(self.engine) as s:
            phodeo = Vendor(name="Phodeo", is_active=True, created_at=now)
            other = Vendor(name="Other Vendor", is_active=True, created_at=now)
            s.add_all([phodeo, other])
            s.commit()
            self.phodeo_id, self.other_id = phodeo.id, other.id
            users = {
                "admin": User(name="Admin", email="admin@example.com",
                              hashed_password=PASSWORD_HASH, role="admin"),
                "phodeo": User(name="Phodeo User", email="phodeo@example.com",
                               hashed_password=PASSWORD_HASH, role="vendor"),
                "other": User(name="Other User", email="other@example.com",
                              hashed_password=PASSWORD_HASH, role="vendor"),
                "unlinked": User(name="Unlinked", email="unlinked@example.com",
                                 hashed_password=PASSWORD_HASH, role="vendor"),
            }
            s.add_all(users.values())
            s.commit()
            self.ids = {k: u.id for k, u in users.items()}
            s.add(VendorUser(user_id=self.ids["phodeo"], vendor_id=self.phodeo_id))
            s.add(VendorUser(user_id=self.ids["other"], vendor_id=self.other_id))
            s.commit()
        self.admin = self.headers("admin@example.com")
        self.phodeo = self.headers("phodeo@example.com")
        self.other = self.headers("other@example.com")

    # ── helpers ──
    def headers(self, email: str) -> dict:
        resp = self.login(email, PASSWORD)
        self.assertEqual(resp.status_code, 200, email)
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def add_lead(self, vendor_id: int, phone: str, **values) -> int:
        now = utcnow()
        with Session(self.engine) as s:
            lead = VendorToKocLead(vendor_id=vendor_id, phone=phone, source="manual",
                                   created_by=self.ids["admin"], created_at=now, updated_at=now,
                                   **values)
            s.add(lead)
            s.commit()
            return lead.id

    def leads(self, vendor_id: int | None = None) -> list[VendorToKocLead]:
        with Session(self.engine) as s:
            stmt = select(VendorToKocLead).order_by(VendorToKocLead.id)
            if vendor_id is not None:
                stmt = stmt.where(VendorToKocLead.vendor_id == vendor_id)
            return list(s.exec(stmt).all())

    def preview(self, headers, content: bytes, filename="Phodeo Leads to KOC.xlsx", **form):
        return self.client.post(f"{BASE}/upload/preview", headers=headers,
                                files={"file": (filename, content)},
                                data={k: str(v) for k, v in form.items()})

    def assert_no_admin_fields(self, obj: dict):
        self.assertEqual(set(obj) & ADMIN_FIELDS, set(), obj)


class NotificationsAndHotTests(VendorToKocTestCase):
    def test_single_and_bulk_add_create_admin_notifications_once_per_admin(self):
        single = self.client.post(f"{BASE}/leads", headers=self.phodeo,
                                  json={"name": "Single", "date": "2026-12-20",
                                        "phone": "9876543210", "city": "Mysuru"})
        self.assertEqual(single.status_code, 201, single.text)
        rows = self.client.get(f"/api/v1/notifications", headers=self.admin).json()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "vendor_lead_added")
        self.assertIn("Single", rows[0]["body"])
        self.assertEqual(self.client.get("/api/v1/notifications", headers=self.phodeo).json()["items"], [])
        bulk = self.client.post(f"{BASE}/leads/bulk", headers=self.phodeo, json={"leads": [
            {"name": "Bulk One", "date": "2026-12-21", "phone": "9876543211", "city": "Mysuru"},
            {"name": "Bulk Two", "date": "2026-12-22", "phone": "9876543212", "city": "Mysuru"},
        ]})
        self.assertEqual((bulk.status_code, bulk.json()["added"]), (200, 2), bulk.text)
        rows = self.client.get("/api/v1/notifications", headers=self.admin).json()["items"]
        self.assertEqual(len(rows), 2)   # one single notification and one bulk summary
        self.assertIn("2 leads", rows[0]["body"])
        self.assertEqual(self.client.post(f"{BASE}/leads/bulk", headers=self.phodeo,
                                          json={"leads": [
                                              {"name": "Bulk One", "date": "2026-12-21", "phone": "9876543211", "city": "Mysuru"},
                                              {"name": "Bulk Two", "date": "2026-12-22", "phone": "9876543212", "city": "Mysuru"},
                                          ]}).json()["added"], 0)
        self.assertEqual(len(self.client.get("/api/v1/notifications", headers=self.admin).json()["items"]), 2)

    def test_notification_failure_does_not_fail_vendor_add(self):
        with mock.patch.object(notification_service, "notify_admins_vendor_added", side_effect=RuntimeError("offline")):
            response = self.client.post(f"{BASE}/leads", headers=self.phodeo,
                                        json={"name": "Saved", "date": "2026-12-20",
                                              "phone": "9876543213", "city": "Mysuru"})
        self.assertEqual(response.status_code, 201, response.text)

    def test_hot_filter_export_column_and_vendor_permissions(self):
        lead_id = self.add_lead(self.phodeo_id, "9876543214", name="Hot", city="Mysuru")
        url = f"{BASE}/leads/{lead_id}/hot"
        self.assertEqual(self.client.post(url, headers=self.phodeo).status_code, 200)
        listed = self.client.get(f"{BASE}/leads", headers=self.phodeo, params={"hot": "yes"}).json()
        self.assertTrue(any(item["id"] == lead_id and item["is_hot"] for item in listed["items"]))
        cold = self.client.get(f"{BASE}/leads", headers=self.phodeo, params={"hot": "no"}).json()
        self.assertFalse(any(item["id"] == lead_id for item in cold["items"]))
        denied = self.client.delete(url, headers=self.other)
        self.assertEqual(denied.status_code, 404)
        exported = self.client.get(f"{BASE}/leads/export", headers=self.phodeo, params={"format": "csv", "hot": "yes"})
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertIn("Hot", exported.text.splitlines()[0])


# ── Preview ─────────────────────────────────────────────────────────────
class PreviewTests(VendorToKocTestCase):
    def setUp(self):
        super().setUp()
        self.add_lead(self.phodeo_id, "9123456789", **SAVED)  # makes row 7 a duplicate (every field)
        self.add_lead(self.other_id, "9845012345", name="Ravi Kumar", lead_date=date(2026, 1, 13),
                      city="Mysuru")                        # other vendor: not a duplicate

    def test_phodeo_sheet_preview(self):
        before = len(self.leads())
        resp = self.preview(self.phodeo, make_xlsx(phodeo_rows()))
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["rows"], EXPECTED_PREVIEW)
        self.assertEqual((body["valid_count"], body["invalid_count"], body["duplicate_count"]),
                         (4, 2, 2))
        self.assertEqual(len(self.leads()), before)          # preview saves nothing

    def test_same_rows_as_csv_give_the_same_preview(self):
        resp = self.preview(self.phodeo, make_csv(phodeo_rows()), filename="leads.csv")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["rows"], EXPECTED_PREVIEW)

    def test_headings_match_ignoring_case_and_spaces_other_columns_ignored(self):
        rows = [["x", " 8095852272 ", "Asha", "05-01-2026", "Bengaluru"]]
        content = make_xlsx(rows, headings=["Status", " PHONE ", "na me", "DATE", "City "])
        body = self.preview(self.phodeo, content).json()
        self.assertEqual(body["rows"][0]["phone"], "8095852272")
        self.assertEqual(body["rows"][0]["name"], "Asha")

    def test_vendor_id_from_a_vendor_is_ignored(self):
        resp = self.preview(self.phodeo, make_xlsx(phodeo_rows()), vendor_id=self.other_id)
        rows = {r["row"]: r for r in resp.json()["rows"]}
        self.assertTrue(rows[7]["duplicate"])     # checked against Phodeo, not Other Vendor
        self.assertFalse(rows[3]["duplicate"])

    def test_admins_cannot_preview(self):
        for form in ({}, {"vendor_id": self.other_id}):
            resp = self.preview(self.admin, make_xlsx(phodeo_rows()), **form)
            self.assertEqual((resp.status_code, resp.json()["detail"]), (403, ONLY_VENDORS_ADD), form)
        self.assertEqual(self.preview(self.admin, b"not a file", filename="x.txt").status_code, 403)

    def test_sheet_selection(self):
        content = make_xlsx([], title="Summary",
                            extra_sheets=[("Phodeo Leads to KOC", [PHODEO_HEADINGS, *phodeo_rows()])])
        first = self.preview(self.phodeo, content).json()
        self.assertEqual(first["rows"], [])                  # first sheet: headings only
        chosen = self.preview(self.phodeo, content, sheet_name="Phodeo Leads to KOC").json()
        self.assertEqual(chosen["rows"], EXPECTED_PREVIEW)
        missing = self.preview(self.phodeo, content, sheet_name="Nope")
        self.assertEqual(missing.status_code, 400)
        self.assertIn("Summary, Phodeo Leads to KOC", missing.json()["detail"])

    def test_file_checks(self):
        cases = [
            ("leads.xls", b"\xd0\xcf\x11\xe0 old excel", 400, "Only .xlsx and .csv"),
            ("leads.xlsx", b"not a zip file", 400, "not a valid .xlsx"),
            ("leads.csv", b"", 400, "empty"),
            ("leads.csv", make_csv([["1", "Asha"]], headings=["Sl. No", "Name"]), 400,
             "Missing column: Phone"),
        ]
        for filename, content, code, text in cases:
            resp = self.preview(self.phodeo, content, filename=filename)
            self.assertEqual(resp.status_code, code, filename)
            self.assertIn(text, resp.json()["detail"])

    def test_row_and_size_limits(self):
        rows = [["", f"{i:02d}-01-2026", "N", "", f"9{i:09d}", ""] for i in range(1, 5002)]
        too_many = make_csv(rows, headings=PHODEO_HEADINGS[:6])
        resp = self.preview(self.phodeo, too_many, filename="big.csv")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("more than 5,000 rows", resp.json()["detail"])
        exactly = make_csv(rows[:5000], headings=PHODEO_HEADINGS[:6])
        self.assertEqual(self.preview(self.phodeo, exactly, filename="ok.csv").status_code, 200)
        huge = b"Phone\n" + b"9123456789\n" * (5 * 1024 * 1024 // 11 + 10)
        self.assertEqual(self.preview(self.phodeo, huge, filename="huge.csv").status_code, 413)


# ── Bulk save ───────────────────────────────────────────────────────────
class BulkTests(VendorToKocTestCase):
    def test_bulk_revalidates_edited_rows(self):
        self.add_lead(self.phodeo_id, "9123456789", **SAVED)
        preview = self.preview(self.phodeo, make_xlsx(phodeo_rows())).json()
        leads = [{k: r[k] for k in ("name", "date", "phone", "city")}
                 for r in preview["rows"] if not r["errors"] and not r["duplicate"]]
        # The user edits rows in the UI before saving:
        leads[0]["phone"] = "98450123"          # valid in the preview, now too short
        leads[1] = dict(leads[2])               # now repeats another row (every field)
        leads.append({"name": "Edited Date", "date": "13-01-2026", "phone": "7012345678"})
        leads.append({"name": "already saved", "date": "2026-01-07", "phone": "9123456789",
                      "city": "Chennai"})          # saved before (any case)
        resp = self.client.post(f"{BASE}/leads/bulk", headers=self.phodeo, json={"leads": leads})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["added"], 2)                  # leads[2] and leads[3]
        self.assertEqual(body["duplicates_skipped"], 2)     # repeat + already saved
        self.assertEqual(body["failed"], 2)
        self.assertEqual({(e["index"], e["field"]) for e in body["errors"]},
                         {(0, "phone"), (4, "date")})
        saved = self.leads(self.phodeo_id)
        self.assertEqual(sorted(l.phone for l in saved),
                         sorted(["9123456789", leads[2]["phone"], leads[3]["phone"]]))
        new = [l for l in saved if l.phone == leads[3]["phone"]][0]
        self.assertEqual((new.source, new.created_by), ("bulk_upload", self.ids["phodeo"]))
        self.assertEqual(set(body), {"added", "duplicates_skipped", "failed", "errors"})

    def test_vendor_id_from_a_vendor_is_ignored(self):
        body = {"vendor_id": self.other_id, "leads": [{"phone": "9000000001"}]}
        self.client.post(f"{BASE}/leads/bulk", headers=self.phodeo, json=body)
        self.assertEqual([l.vendor_id for l in self.leads()], [self.phodeo_id])

    def test_admins_cannot_bulk_add(self):
        for body in ({"leads": [{"phone": "9000000001"}]},
                     {"vendor_id": self.other_id, "leads": [{"phone": "9000000001"}]}):
            resp = self.client.post(f"{BASE}/leads/bulk", headers=self.admin, json=body)
            self.assertEqual((resp.status_code, resp.json()["detail"]), (403, ONLY_VENDORS_ADD))
        self.assertEqual(self.leads(), [])

    def test_more_than_5000_leads_is_rejected(self):
        body = {"leads": [{"phone": "9000000001"}] * 5001}
        self.assertEqual(self.client.post(f"{BASE}/leads/bulk", headers=self.phodeo, json=body).status_code, 422)


# ── Manual add ──────────────────────────────────────────────────────────
class ManualAddTests(VendorToKocTestCase):
    def test_vendor_adds_a_lead(self):
        resp = self.client.post(f"{BASE}/leads", headers=self.phodeo, json={
            "vendor_id": self.other_id, "name": " Asha ", "date": "2026-01-05",
            "phone": "+91 80958 52272", "city": "Bengaluru"})
        self.assertEqual(resp.status_code, 201, resp.text)
        lead = resp.json()
        self.assert_no_admin_fields(lead)
        self.assertEqual((lead["vendor_id"], lead["vendor_name"]), (self.phodeo_id, "Phodeo"))
        self.assertEqual((lead["name"], lead["phone"], lead["lead_date"], lead["source"]),
                         ("Asha", "8095852272", "2026-01-05", "manual"))

    def test_duplicate_and_invalid(self):
        self.client.post(f"{BASE}/leads", headers=self.phodeo, json={"phone": "8095852272"})
        dup = self.client.post(f"{BASE}/leads", headers=self.phodeo, json={"phone": "08095852272"})
        self.assertEqual(dup.status_code, 409)
        bad = self.client.post(f"{BASE}/leads", headers=self.phodeo,
                               json={"phone": "123", "date": "05-01-2026"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual({e["field"] for e in bad.json()["detail"]}, {"phone", "date"})

    def test_admins_cannot_add(self):
        for body in ({"vendor_id": self.other_id, "phone": "9000000001"}, {"phone": "9000000001"}, {}):
            resp = self.client.post(f"{BASE}/leads", headers=self.admin, json=body)
            self.assertEqual((resp.status_code, resp.json()["detail"]), (403, ONLY_VENDORS_ADD), body)
        self.assertEqual(self.leads(), [])
        unlinked = self.headers("unlinked@example.com")                  # vendor login without a vendor
        self.assertEqual(self.client.post(f"{BASE}/leads", headers=unlinked,
                                          json={"phone": "9000000001"}).status_code, 403)

    def test_inactive_vendor(self):
        with Session(self.engine) as s:
            vendor = s.get(Vendor, self.phodeo_id)
            vendor.is_active = False
            s.add(vendor)
            s.commit()
        self.add_lead(self.phodeo_id, "9000000009")
        self.assertEqual(self.client.post(f"{BASE}/leads", headers=self.phodeo,
                                          json={"phone": "9000000001"}).status_code, 403)
        self.assertEqual(self.client.get(f"{BASE}/leads", headers=self.phodeo).json()["total"], 1)


# ── Vendor scope and hidden admin fields ────────────────────────────────
class ScopeTests(VendorToKocTestCase):
    def setUp(self):
        super().setUp()
        self.own = self.add_lead(self.phodeo_id, "9000000001", update_status="interested",
                                 additional_comments="admin note", follow_up_date=date(2026, 2, 1))
        self.foreign = self.add_lead(self.other_id, "9000000002")

    def test_vendor_cannot_see_another_vendors_lead(self):
        listed = self.client.get(f"{BASE}/leads", headers=self.phodeo).json()
        self.assertEqual([i["id"] for i in listed["items"]], [self.own])
        ignored = self.client.get(f"{BASE}/leads?vendor_id={self.other_id}", headers=self.phodeo).json()
        self.assertEqual([i["id"] for i in ignored["items"]], [self.own])
        self.assertEqual(self.client.get(f"{BASE}/leads/{self.foreign}", headers=self.phodeo).status_code, 404)

    def test_vendor_cannot_edit_any_lead_or_change_update_status(self):
        for lead_id in (self.own, self.foreign, 999):
            resp = self.client.patch(f"{BASE}/leads/{lead_id}", headers=self.phodeo,
                                     json={"update_status": "converted"})
            self.assertEqual(resp.status_code, 403, lead_id)
        self.assertEqual(self.leads(self.phodeo_id)[0].update_status, "interested")
        self.assertIsNone(self.leads(self.other_id)[0].update_status)

    def test_vendor_sees_the_update_read_only_but_no_user_id(self):
        self.assertEqual(ADMIN_ONLY_FIELDS, ADMIN_FIELDS)
        self.client.patch(f"{BASE}/leads/{self.own}", headers=self.admin, json={"update_status": "will_call_back"})
        items = self.client.get(f"{BASE}/leads", headers=self.phodeo).json()["items"]
        detail = self.client.get(f"{BASE}/leads/{self.own}", headers=self.phodeo).json()
        created = self.client.post(f"{BASE}/leads", headers=self.phodeo,
                                   json={"phone": "9000000003"}).json()
        for obj in (*items, detail, created):
            self.assert_no_admin_fields(obj)
            self.assertTrue(STATUS_FIELDS <= set(obj), obj)
        self.assertEqual((detail["update_status"], detail["additional_comments"], detail["follow_up_date"],
                          detail["status_updated_by_name"]), ("will_call_back", "admin note", "2026-02-01", "Admin"))
        self.assertIsNotNone(detail["status_updated_at"])
        self.assertEqual((created["update_status"], created["status_updated_by_name"]), (None, None))
        admin_detail = self.client.get(f"{BASE}/leads/{self.own}", headers=self.admin).json()
        self.assertTrue(ADMIN_FIELDS | STATUS_FIELDS <= set(admin_detail))
        self.assertEqual((admin_detail["status_updated_by"], admin_detail["status_updated_by_name"]),
                         (self.ids["admin"], "Admin"))

    def test_vendor_filters_and_sorts_by_update_and_follow_up_on_own_leads_only(self):
        self.add_lead(self.other_id, "9000000004", update_status="interested", follow_up_date=date(2026, 2, 1))
        for query, expected in (("update_status=interested", [self.own]), ("update_status=pending", []),
                                ("update_status=converted", []), ("follow_up_from=2026-01-01", [self.own]),
                                ("follow_up_to=2026-01-31", []), ("follow_up_due=true", [self.own]),
                                ("sort=update_status", [self.own]), ("sort=follow_up_date&order=asc", [self.own]),
                                (f"vendor_id={self.other_id}&update_status=interested", [self.own])):   # vendor_id ignored
            resp = self.client.get(f"{BASE}/leads?{query}", headers=self.phodeo)
            self.assertEqual(resp.status_code, 200, query)
            self.assertEqual([i["id"] for i in resp.json()["items"]], expected, query)
        stats = self.client.get(f"{BASE}/leads/stats?update_status=interested", headers=self.phodeo).json()
        self.assertEqual((stats["total"], stats["by_status"]["interested"]), (1, 1))      # Other's lead not counted
        self.assertNotIn("by_vendor", stats)

    def test_unlinked_vendor_login_gets_403(self):
        unlinked = self.headers("unlinked@example.com")
        resp = self.client.get(f"{BASE}/leads", headers=unlinked)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"detail": "This login isn't linked to a vendor."})


# ── PATCH (admin) ───────────────────────────────────────────────────────
class PatchTests(VendorToKocTestCase):
    def test_admin_updates_status_fields(self):
        lead_id = self.add_lead(self.phodeo_id, "9000000001")
        resp = self.client.patch(f"{BASE}/leads/{lead_id}", headers=self.admin, json={
            "update_status": "will_call_back", "additional_comments": "Call after 6",
            "follow_up_date": "2026-02-01"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual((body["update_status"], body["follow_up_date"], body["status_updated_by"]),
                         ("will_call_back", "2026-02-01", self.ids["admin"]))
        self.assertIsNotNone(body["status_updated_at"])
        # Only sent fields change; null sets the lead back to pending
        resp = self.client.patch(f"{BASE}/leads/{lead_id}", headers=self.admin,
                                 json={"update_status": None})
        self.assertIsNone(resp.json()["update_status"])
        self.assertEqual(resp.json()["additional_comments"], "Call after 6")

    def test_admin_sets_amounts_vendors_see_them_read_only(self):
        lead_id = self.add_lead(self.phodeo_id, "9000000001")
        url = f"{BASE}/leads/{lead_id}"
        resp = self.client.patch(url, headers=self.admin, json={"quoted_amount": 150000, "conversion_amount": 0})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((resp.json()["quoted_amount"], resp.json()["conversion_amount"]), (150000, 0))
        self.assertEqual(resp.json()["status_updated_by"], self.ids["admin"])
        resp = self.client.patch(url, headers=self.admin, json={"conversion_amount": 140000, "update_status": "converted"})
        self.assertEqual((resp.json()["quoted_amount"], resp.json()["conversion_amount"]), (150000, 140000))
        for path in (f"{BASE}/leads", url):                                   # vendor: list and details
            body = self.client.get(path, headers=self.phodeo).json()
            item = body["items"][0] if "items" in body else body
            self.assertEqual((item["quoted_amount"], item["conversion_amount"]), (150000, 140000), path)
        for body in ({"quoted_amount": 1}, {"conversion_amount": 1}, {"update_status": "converted"}):
            self.assertEqual(self.client.patch(url, headers=self.phodeo, json=body).status_code, 403, body)
        resp = self.client.patch(url, headers=self.admin, json={"quoted_amount": None})
        self.assertEqual((resp.json()["quoted_amount"], resp.json()["conversion_amount"]), (None, 140000))
        for body in ({"quoted_amount": -1}, {"conversion_amount": 100_000_001}, {"conversion_amount": "lots"}):
            self.assertEqual(self.client.patch(url, headers=self.admin, json=body).status_code, 422, body)

    def test_patch_errors(self):
        lead_id = self.add_lead(self.phodeo_id, "9000000001")
        url = f"{BASE}/leads/{lead_id}"
        self.assertEqual(self.client.patch(f"{BASE}/leads/999", headers=self.admin,
                                           json={"update_status": "converted"}).status_code, 404)
        for body in ({}, {"update_status": "maybe"}, {"phone": "9000000002"},
                     {"additional_comments": "x" * 5001}):
            self.assertEqual(self.client.patch(url, headers=self.admin, json=body).status_code, 422, body)


# ── List (filters, sorting, paging) ─────────────────────────────────────
class ListTests(VendorToKocTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.add_lead(self.phodeo_id, "9000000001", name="Asha Rao", city="Bengaluru",
                               lead_date=date(2026, 1, 5), update_status="converted",
                               follow_up_date=date(2026, 3, 1))
        self.b = self.add_lead(self.phodeo_id, "9845012345", name="Ravi", city="Mysuru",
                               lead_date=None)
        self.c = self.add_lead(self.other_id, "9000000003", name="Meena", city="Pune",
                               lead_date=date(2026, 2, 10))

    def ids_for(self, query: str, headers=None) -> list[int]:
        resp = self.client.get(f"{BASE}/leads?{query}", headers=headers or self.admin)
        self.assertEqual(resp.status_code, 200, resp.text)
        return [i["id"] for i in resp.json()["items"]]

    def test_filters(self):
        self.assertEqual(sorted(self.ids_for(f"vendor_id={self.phodeo_id}&vendor_id={self.other_id}")),
                         [self.a, self.b, self.c])
        self.assertEqual(self.ids_for(f"vendor_id={self.other_id}"), [self.c])
        self.assertEqual(sorted(self.ids_for("update_status=pending")), [self.b, self.c])
        self.assertEqual(sorted(self.ids_for("update_status=pending&update_status=converted")),
                         [self.a, self.b, self.c])
        # This backend: the lead's own date is lead_date_from / lead_date_to (date_from = arrival)
        self.assertEqual(self.ids_for("lead_date_from=2026-01-01&lead_date_to=2026-01-31"), [self.a])
        self.assertEqual(self.ids_for("follow_up_from=2026-02-15&follow_up_to=2026-03-15"), [self.a])
        self.assertEqual(self.ids_for("search=asha"), [self.a])
        self.assertEqual(self.ids_for("search=pune"), [self.c])
        self.assertEqual(self.ids_for("search=98450 12345"), [self.b])
        self.assertEqual(self.ids_for("search=50%25_x"), [])     # % and _ are literal

    def test_sorting_puts_empty_values_last(self):
        self.assertEqual(self.ids_for("sort=lead_date&order=asc"), [self.a, self.c, self.b])
        self.assertEqual(self.ids_for("sort=lead_date&order=desc"), [self.c, self.a, self.b])
        # "Other Vendor" before "Phodeo"; within a vendor, id breaks the tie
        self.assertEqual(self.ids_for("sort=vendor_name&order=asc"), [self.c, self.a, self.b])
        self.assertEqual(self.ids_for("search=flat 50"), [])    # digits alone don't match phones

    def test_paging_and_sl_no(self):
        for i in range(9):                                   # 12 leads: two pages of 10
            self.add_lead(self.other_id, f"910000000{i}")
        body = self.client.get(f"{BASE}/leads?page=2&page_size=10&sort=lead_date&order=asc",
                               headers=self.admin).json()
        self.assertEqual((body["total"], body["pages"], body["page"], body["page_size"]), (12, 2, 2, 10))
        self.assertEqual([i["sl_no"] for i in body["items"]], [11, 12])
        default = self.client.get(f"{BASE}/leads", headers=self.admin).json()
        self.assertEqual((default["page_size"], len(default["items"]), default["total"]), (10, 10, 12))
        for size in (2, 20, 101, 0):
            self.assertEqual(self.client.get(f"{BASE}/leads?page_size={size}", headers=self.admin).status_code, 422)


# ── Vendors (admin) ─────────────────────────────────────────────────────
class VendorAdminTests(VendorToKocTestCase):
    def test_list_with_lead_counts_admin_only(self):
        self.add_lead(self.phodeo_id, "9000000001")
        self.add_lead(self.phodeo_id, "9000000002")
        body = self.client.get(f"{BASE}/vendors", headers=self.admin).json()
        self.assertEqual(body, [
            {"id": self.other_id, "name": "Other Vendor", "is_active": True, "lead_count": 0},
            {"id": self.phodeo_id, "name": "Phodeo", "is_active": True, "lead_count": 2},
        ])
        self.assertEqual(self.client.get(f"{BASE}/vendors", headers=self.phodeo).status_code, 403)

    def test_create_rename_deactivate(self):
        resp = self.client.post(f"{BASE}/vendors", headers=self.admin, json={"name": "  WedPix  "})
        self.assertEqual(resp.status_code, 201)
        vendor = resp.json()
        self.assertEqual((vendor["name"], vendor["is_active"], vendor["lead_count"]), ("WedPix", True, 0))
        self.assertEqual(self.client.post(f"{BASE}/vendors", headers=self.admin,
                                          json={"name": "phodeo"}).status_code, 409)
        url = f"{BASE}/vendors/{vendor['id']}"
        renamed = self.client.patch(url, headers=self.admin, json={"name": "WedPix Studio"}).json()
        self.assertEqual(renamed["name"], "WedPix Studio")
        self.assertFalse(self.client.patch(url, headers=self.admin, json={"is_active": False}).json()["is_active"])
        self.assertEqual(self.client.patch(url, headers=self.admin, json={"name": "PHODEO"}).status_code, 409)
        self.assertEqual(self.client.patch(f"{BASE}/vendors/999", headers=self.admin,
                                           json={"name": "X"}).status_code, 404)
        self.assertEqual(self.client.post(f"{BASE}/vendors", headers=self.phodeo,
                                          json={"name": "Nope"}).status_code, 403)


# ── Vendor logins ───────────────────────────────────────────────────────
class VendorLoginTests(VendorToKocTestCase):
    def test_creating_a_vendor_user_needs_a_vendor_id(self):
        body = {"name": "New", "email": "new@example.com", "password": PASSWORD, "role": "vendor"}
        resp = self.client.post("/api/v1/users", headers=self.admin, json={**body, "email": "plain@example.com"})
        self.assertEqual(resp.status_code, 422, resp.text)                      # a vendor login needs its vendor
        self.assertEqual(self.client.post("/api/v1/users", headers=self.admin,
                                          json={**body, "vendor_id": 999}).status_code, 404)
        resp = self.client.post("/api/v1/users", headers=self.admin, json={**body, "vendor_id": self.phodeo_id})
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual((resp.json()["vendor_id"], resp.json()["vendor_name"]), (self.phodeo_id, "Phodeo"))
        me = self.client.get("/api/v1/auth/me", headers=self.headers("new@example.com")).json()
        self.assertEqual((me["vendor_id"], me["vendor_name"]), (self.phodeo_id, "Phodeo"))
        admin_body = {**body, "email": "a2@example.com", "role": "admin", "vendor_id": self.phodeo_id}
        self.assertEqual(self.client.post("/api/v1/users", headers=self.admin, json=admin_body).status_code, 422)

    def test_users_list_includes_vendor(self):
        users = {u["email"]: u for u in self.client.get("/api/v1/users", headers=self.admin).json()}
        self.assertEqual(users["phodeo@example.com"]["vendor_name"], "Phodeo")
        self.assertIsNone(users["admin@example.com"]["vendor_id"])

    def test_link_an_existing_vendor_login(self):
        url = f"{BASE}/vendors/{self.other_id}/users/{self.ids['unlinked']}"
        resp = self.client.put(url, headers=self.admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"user_id": self.ids["unlinked"], "vendor_id": self.other_id,
                                       "vendor_name": "Other Vendor"})
        self.assertEqual(self.client.put(url, headers=self.admin).status_code, 200)    # idempotent
        unlinked = self.headers("unlinked@example.com")
        self.assertEqual(self.client.get(f"{BASE}/leads", headers=unlinked).status_code, 200)
        admin_user = f"{BASE}/vendors/{self.other_id}/users/{self.ids['admin']}"
        self.assertEqual(self.client.put(admin_user, headers=self.admin).status_code, 400)
        self.assertEqual(self.client.put(f"{BASE}/vendors/999/users/{self.ids['unlinked']}",
                                         headers=self.admin).status_code, 404)
        self.assertEqual(self.client.put(url, headers=self.phodeo).status_code, 403)


# ── Team page: block / unblock, unlink ──────────────────────────────────
class TeamTests(VendorToKocTestCase):
    def users_url(self, key: str) -> str:
        return f"/api/v1/users/{self.ids[key]}"

    def refresh(self, raw: str):
        self.client.cookies.clear()
        return self.client.post("/api/v1/auth/refresh",
                                headers={"Origin": ORIGIN, "Cookie": f"{settings.REFRESH_COOKIE_NAME}={raw}"})

    def test_block_revokes_sessions_and_stops_login_then_unblock(self):
        login = self.login("phodeo@example.com", PASSWORD)
        raw = login.cookies[settings.REFRESH_COOKIE_NAME]
        access = {"Authorization": f"Bearer {login.json()['access_token']}"}
        self.assertEqual(self.client.get(f"{BASE}/leads", headers=access).status_code, 200)

        resp = self.client.patch(self.users_url("phodeo"), headers=self.admin, json={"is_active": False})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((resp.json()["is_active"], resp.json()["vendor_name"]), (False, "Phodeo"))
        with Session(self.engine) as s:
            open_tokens = s.exec(select(RefreshToken).where(RefreshToken.user_id == self.ids["phodeo"],
                                                            RefreshToken.revoked_at.is_(None))).all()
        self.assertEqual(open_tokens, [])                                        # every session revoked
        self.assertEqual(self.refresh(raw).status_code, 401)
        self.assertEqual(self.client.get(f"{BASE}/leads", headers=access).status_code, 401)   # access token too
        self.assertEqual(self.login("phodeo@example.com", PASSWORD).status_code, 401)
        users = {u["email"]: u for u in self.client.get("/api/v1/users", headers=self.admin).json()}
        self.assertFalse(users["phodeo@example.com"]["is_active"])
        self.assertTrue(users["other@example.com"]["is_active"])                 # others untouched

        resp = self.client.patch(self.users_url("phodeo"), headers=self.admin, json={"is_active": True})
        self.assertEqual((resp.status_code, resp.json()["is_active"]), (200, True))
        self.assertEqual(self.login("phodeo@example.com", PASSWORD).status_code, 200)
        self.assertEqual(self.refresh(raw).status_code, 401)                     # old sessions stay revoked

    def test_block_rules(self):
        resp = self.client.patch(self.users_url("admin"), headers=self.admin, json={"is_active": False})
        self.assertEqual((resp.status_code, resp.json()["detail"]), (400, "You can't block your own login"))
        self.assertEqual(self.client.patch(self.users_url("admin"), headers=self.admin,
                                           json={"is_active": True}).status_code, 200)   # no-op is fine
        self.assertEqual(self.client.patch(self.users_url("other"), headers=self.phodeo,
                                           json={"is_active": False}).status_code, 403)  # admins only
        self.assertEqual(self.client.patch("/api/v1/users/999", headers=self.admin,
                                           json={"is_active": False}).status_code, 404)
        for body in ({}, {"is_active": None}, {"is_active": False, "role": "boss"}):
            self.assertEqual(self.client.patch(self.users_url("other"), headers=self.admin,
                                               json=body).status_code, 422, body)
        second_admin = self.client.post("/api/v1/users", headers=self.admin, json={
            "name": "Second", "email": "second@example.com", "password": "password123", "role": "admin"}).json()
        self.assertEqual(self.client.patch(f"/api/v1/users/{second_admin['id']}", headers=self.admin,
                                           json={"is_active": False}).status_code, 200)  # another admin: ok

    def test_unlink(self):
        url = f"{BASE}/vendors/{self.phodeo_id}/users/{self.ids['phodeo']}"
        self.assertEqual(self.client.delete(url, headers=self.phodeo).status_code, 403)   # admins only
        wrong = f"{BASE}/vendors/{self.other_id}/users/{self.ids['phodeo']}"
        self.assertEqual(self.client.delete(wrong, headers=self.admin).status_code, 404)  # other vendor
        resp = self.client.delete(url, headers=self.admin)
        self.assertEqual((resp.status_code, resp.content), (204, b""))
        me = self.client.get(self.users_url("phodeo"), headers=self.admin).json()
        self.assertEqual((me["vendor_id"], me["vendor_name"], me["is_active"]), (None, None, True))
        self.assertEqual(self.client.get(f"{BASE}/leads", headers=self.phodeo).status_code, 403)
        self.assertEqual(self.client.delete(url, headers=self.admin).status_code, 404)    # already unlinked
        self.assertEqual(self.client.put(url, headers=self.admin).status_code, 200)       # link again
        self.assertEqual(self.client.get(f"{BASE}/leads", headers=self.phodeo).status_code, 200)


# ── Parsing rules ───────────────────────────────────────────────────────
class ParsingTests(unittest.TestCase):
    def test_text_dates_are_day_first(self):
        self.assertEqual(parse_file_date("13-01-2026")[0], date(2026, 1, 13))
        self.assertEqual(parse_file_date("03-04-2026")[0], date(2026, 4, 3))      # never March 4
        self.assertEqual(parse_file_date("3-4-2026")[0], date(2026, 4, 3))
        self.assertIsNotNone(parse_file_date("31-02-2026")[2])                    # impossible date
        self.assertIsNotNone(parse_file_date("2026-01-05")[2])                    # not DD-MM-YYYY
        self.assertEqual(parse_file_date("13 Dec 2026")[0], date(2026, 12, 13))   # the export's format
        self.assertEqual(parse_file_date("5 January 2026")[0], date(2026, 1, 5))
        self.assertIsNotNone(parse_file_date("Dec 13 2026")[2])                   # month first: no
        self.assertIsNotNone(parse_file_date(45678)[2])                           # bare number
        self.assertEqual(parse_file_date(datetime(2026, 1, 5, 18, 30))[0], date(2026, 1, 5))

    def test_phone_cleaning(self):
        cases = {
            8095852272.0: "8095852272", 8095852272: "8095852272", "8095852272.0": "8095852272",
            "+91 98450-12345": "9845012345", "919876543210": "9876543210",
            "09845054321": "9845054321", " 98450 54321 ": "9845054321",
            "9123456789": "9123456789", None: None, "  ": None,
        }
        for raw, expected in cases.items():
            self.assertEqual(clean_phone(raw), expected, raw)


if __name__ == "__main__":
    unittest.main()
