"""Vendor to KOC export and template tests. In-memory SQLite and fake data only."""
import csv
import io
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from tests.base import ORIGIN
from tests.test_vendor_to_koc import BASE, VendorToKocTestCase

import openpyxl  # noqa: E402

from app.services import vendor_lead_export as lead_export  # noqa: E402
from app.services.vendor_lead_import import EXAMPLE_ROW_ERROR  # noqa: E402

EXPORT, TEMPLATE = f"{BASE}/leads/export", f"{BASE}/leads/template"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FORMULA_NAME = '=HYPERLINK("http://evil.example","click")'
VENDOR_HEADINGS = ["Sl. No", "Date", "Name", "Phone", "City"]
ADMIN_HEADINGS = ["Sl. No", "Vendor", "Date", "Name", "Phone", "City", "Update",
                  "Additional comments", "Follow-up date", "Updated by", "Updated at"]


def xlsx_sheet(resp):
    return openpyxl.load_workbook(io.BytesIO(resp.content)).active


def xlsx_rows(resp) -> list[list]:
    return [list(r) for r in xlsx_sheet(resp).iter_rows(values_only=True)]


def csv_rows(resp) -> list[list[str]]:
    text = resp.content.decode("utf-8")
    assert text.startswith("﻿"), "CSV must start with a UTF-8 BOM"
    return list(csv.reader(io.StringIO(text[1:])))


def as_text(value) -> str:
    """Format an XLSX value the way the CSV writes it."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return (value.strftime("%d-%m-%Y %H:%M") if (value.hour, value.minute) != (0, 0)
                else value.strftime("%d-%m-%Y"))
    return str(value)


class ExportTestCase(VendorToKocTestCase):
    def setUp(self):
        super().setUp()
        self.p1 = self.add_lead(
            self.phodeo_id, "9000000001", name="Asha Rao", city="Bengaluru",
            lead_date=date(2026, 1, 5), update_status="converted",
            additional_comments="- call later", follow_up_date=date(2026, 2, 1),
            status_updated_by=self.ids["admin"],
            status_updated_at=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc))
        self.p2 = self.add_lead(self.phodeo_id, "9000000002", name=FORMULA_NAME, city="@Pune")
        self.o1 = self.add_lead(self.other_id, "9000000003", name="Meena", city="Mysuru",
                                lead_date=date(2026, 2, 10), update_status="interested")

    def export(self, headers, query: str = ""):
        return self.client.get(f"{EXPORT}?{query}", headers=headers)


class VendorExportTests(ExportTestCase):
    def test_vendor_export_has_only_own_leads_and_no_admin_columns(self):
        resp = self.export(self.phodeo, "sort=lead_date&order=asc")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers["content-type"], XLSX_TYPE)
        today = lead_export.today_ist().isoformat()
        self.assertEqual(resp.headers["content-disposition"],
                         f'attachment; filename="phodeo_leads_{today}.xlsx"')
        self.assertEqual(resp.headers["cache-control"], "no-store")
        rows = xlsx_rows(resp)
        self.assertEqual(rows[0], VENDOR_HEADINGS)
        self.assertEqual([r[3] for r in rows[1:]], ["9000000001", "9000000002"])   # no Meena
        everything = " ".join(as_text(v) for row in rows for v in row)
        for admin_value in ("call later", "Converted", "Admin", "Phodeo"):
            self.assertNotIn(admin_value, everything)

    def test_vendor_cannot_use_admin_filters_or_sorts(self):
        for query in ("update_status=pending", "follow_up_from=2026-01-01", "sort=update_status",
                      "sort=follow_up_date"):
            self.assertEqual(self.export(self.phodeo, query).status_code, 422, query)

    def test_vendor_id_from_a_vendor_is_ignored(self):
        rows = xlsx_rows(self.export(self.phodeo, f"vendor_id={self.other_id}"))
        self.assertEqual(sorted(r[3] for r in rows[1:]), ["9000000001", "9000000002"])

    def test_unlinked_vendor_and_bad_format(self):
        unlinked = self.headers("unlinked@example.com")
        self.assertEqual(self.export(unlinked).status_code, 403)
        self.assertEqual(self.export(self.phodeo, "format=pdf").status_code, 422)


class AdminExportTests(ExportTestCase):
    def test_filters_and_readable_labels(self):
        resp = self.export(self.admin, f"vendor_id={self.phodeo_id}&update_status=converted")
        today = lead_export.today_ist().isoformat()
        self.assertEqual(resp.headers["content-disposition"],
                         f'attachment; filename="vendor_to_koc_leads_{today}.xlsx"')
        rows = xlsx_rows(resp)
        self.assertEqual(rows[0], ADMIN_HEADINGS)
        self.assertEqual(rows[1], [
            1, "Phodeo", datetime(2026, 1, 5), "Asha Rao", "9000000001", "Bengaluru",
            "Converted", "- call later", datetime(2026, 2, 1), "Admin",
            datetime(2026, 1, 15, 15, 0),            # 09:30 UTC -> 15:00 IST
        ])
        self.assertEqual(len(rows), 2)
        pending = xlsx_rows(self.export(self.admin, "update_status=pending"))
        self.assertEqual([(r[4], r[6]) for r in pending[1:]], [("9000000002", "Pending")])
        others = xlsx_rows(self.export(self.admin, f"vendor_id={self.other_id}"))
        self.assertEqual([(r[1], r[6]) for r in others[1:]], [("Other Vendor", "Interested")])

    def test_xlsx_text_cells_formats_and_formula_safety(self):
        ws = xlsx_sheet(self.export(self.admin, "sort=lead_date&order=asc"))
        by_phone = {ws.cell(row=r, column=5).value: r for r in range(2, ws.max_row + 1)}
        formula_row, asha_row = by_phone["9000000002"], by_phone["9000000001"]
        name = ws.cell(row=formula_row, column=4)
        self.assertEqual((name.value, name.data_type), (FORMULA_NAME, "s"))   # text, exact
        self.assertEqual(ws.cell(row=formula_row, column=6).value, "@Pune")
        comment = ws.cell(row=asha_row, column=8)
        self.assertEqual((comment.value, comment.data_type), ("- call later", "s"))
        phone = ws.cell(row=asha_row, column=5)
        self.assertEqual((phone.data_type, phone.number_format), ("s", "@"))
        lead_date = ws.cell(row=asha_row, column=3)
        self.assertEqual((lead_date.value, lead_date.number_format), (datetime(2026, 1, 5), "DD-MM-YYYY"))
        self.assertEqual(ws.cell(row=asha_row, column=11).number_format, "DD-MM-YYYY HH:MM")

    def test_csv_formats_and_apostrophe_escaping(self):
        resp = self.export(self.admin, "format=csv&sort=lead_date&order=asc")
        self.assertEqual(resp.headers["content-type"], "text/csv; charset=utf-8")
        self.assertTrue(resp.headers["content-disposition"].endswith('.csv"'))
        rows = {r[4]: r for r in csv_rows(resp)[1:]}
        asha, formula = rows["9000000001"], rows["9000000002"]
        self.assertEqual(asha[2], "05-01-2026")
        self.assertEqual(asha[7], "'- call later")
        self.assertEqual(asha[8], "01-02-2026")
        self.assertEqual(asha[10], "15-01-2026 15:00")
        self.assertEqual(formula[3], "'" + FORMULA_NAME)
        self.assertEqual(formula[5], "'@Pune")

    def test_csv_and_xlsx_give_the_same_rows(self):
        query = "sort=lead_date&order=asc"
        from_xlsx = [[as_text(v) for v in row] for row in xlsx_rows(self.export(self.admin, query))]
        from_csv = csv_rows(self.export(self.admin, f"{query}&format=csv"))
        unescaped = [[v[1:] if v[:2] in {"'" + c for c in "=+-@\t\r"} else v for v in row]
                     for row in from_csv]
        self.assertEqual(from_xlsx, unescaped)
        self.assertEqual(len(from_csv), 4)

    def test_over_the_row_cap_returns_400(self):
        with mock.patch.object(lead_export, "EXPORT_MAX_ROWS", 2):
            resp = self.export(self.admin)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()["detail"],
                             "The export has 3 leads; the limit is 2. Narrow the filters, "
                             "for example by date range or vendor.")
            self.assertEqual(self.export(self.admin, f"vendor_id={self.phodeo_id}").status_code, 200)

    def test_export_route_is_not_taken_for_a_lead_id_and_cors_exposes_filename(self):
        resp = self.client.get(EXPORT, headers={**self.admin, "Origin": ORIGIN})
        self.assertEqual(resp.status_code, 200)
        exposed = {h.strip().lower() for h in resp.headers["access-control-expose-headers"].split(",")}
        # Ported: this backend has no login rate limit, so only Content-Disposition (no Retry-After)
        self.assertIn("content-disposition", exposed)
        self.assertEqual(self.client.get(f"{BASE}/leads/999", headers=self.admin).status_code, 404)


class TemplateTests(VendorToKocTestCase):
    def template(self, headers, query: str = ""):
        resp = self.client.get(f"{TEMPLATE}?{query}", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp

    def test_template_content_and_formats(self):
        resp = self.template(self.phodeo)
        self.assertEqual(resp.headers["content-disposition"],
                         'attachment; filename="vendor_to_koc_template.xlsx"')
        ws = xlsx_sheet(resp)
        self.assertEqual([c.value for c in ws[1]], ["Name", "Date", "Phone", "City"])
        name, day, phone, city = ws[2]
        self.assertEqual((name.value, day.value, phone.value, city.value),
                         ("Asha Example", datetime(2026, 1, 15), "9876543210", "Bengaluru"))
        self.assertEqual(day.number_format, "DD-MM-YYYY")
        self.assertEqual((phone.data_type, phone.number_format), ("s", "@"))
        self.assertEqual(ws.column_dimensions["B"].number_format, "DD-MM-YYYY")
        self.assertEqual(ws.column_dimensions["C"].number_format, "@")
        self.assertEqual(csv_rows(self.template(self.admin, "format=csv")),
                         [["Name", "Date", "Phone", "City"],
                          ["Asha Example", "15-01-2026", "9876543210", "Bengaluru"]])

    def test_any_logged_in_user_can_download(self):
        self.template(self.headers("unlinked@example.com"))
        self.assertEqual(self.client.get(TEMPLATE).status_code, 401)

    def test_untouched_template_example_row_is_flagged_in_preview(self):
        for fmt, filename in (("xlsx", "template.xlsx"), ("csv", "template.csv")):
            content = self.template(self.phodeo, f"format={fmt}").content
            body = self.preview(self.phodeo, content, filename=filename).json()
            self.assertEqual((body["valid_count"], body["invalid_count"]), (0, 1), fmt)
            self.assertEqual(body["rows"][0]["row"], 2)
            self.assertEqual(body["rows"][0]["errors"], [EXAMPLE_ROW_ERROR], fmt)

    def test_edited_template_is_valid(self):
        wb = openpyxl.load_workbook(io.BytesIO(self.template(self.phodeo).content))
        ws = wb.active
        ws["A2"], ws["B2"], ws["C2"], ws["D2"] = "Ravi Sample", datetime(2026, 3, 2), "9123400000", "Mysuru"
        buf = io.BytesIO()
        wb.save(buf)
        body = self.preview(self.phodeo, buf.getvalue(), filename="filled.xlsx").json()
        self.assertEqual(body["valid_count"], 1)
        self.assertEqual(body["rows"][0]["errors"], [])
        self.assertEqual(body["rows"][0]["date"], "2026-03-02")
        csv_text = self.template(self.phodeo, "format=csv").content.decode("utf-8-sig")
        edited = csv_text.replace("Asha Example", "Ravi Sample").encode("utf-8")
        body = self.preview(self.phodeo, edited, filename="filled.csv").json()
        self.assertEqual(body["valid_count"], 1)

    def test_example_row_is_rejected_by_bulk_too(self):
        lead = {"name": "Asha Example", "date": "2026-01-15", "phone": "9876543210", "city": "Bengaluru"}
        body = self.client.post(f"{BASE}/leads/bulk", headers=self.phodeo, json={"leads": [lead]}).json()
        self.assertEqual((body["added"], body["failed"]), (0, 1))
        self.assertEqual(body["errors"], [{"index": 0, **EXAMPLE_ROW_ERROR}])


if __name__ == "__main__":
    unittest.main()
