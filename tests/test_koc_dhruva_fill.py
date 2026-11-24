"""KOC -> Dhruva uploads: export / workflow header names, budgets Excel turned into dates, empty fields
filled from the workflow row of the phone (stored, never overwriting), Received from the sheet, else
the workflow row, else the upload time; and the backfill of the existing rows
(scripts/backfill_koc_dhruva_fields.py). In-memory SQLite and the fake workflow database only. Run:

    python -m unittest tests.test_koc_dhruva_fill -v
"""
import io
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from openpyxl import Workbook
from sqlalchemy import text
from sqlmodel import Session, select

import tests.test_crm_leads_api as base
from tests.test_crm_leads_api import API
from tests.test_spin_history_upload import SpinTestBase

from app.models.koc_dhruva_lead import KocDhruvaLead
from app.models.lead_activity import LeadActivity
from app.services import koc_dhruva_backfill as backfill
from app.services.crm_leads.fields import excel_range

K2D = "/koc-to-dhruva"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def wf(lead_id, phone, created, name=None, **details):
    row = {"id": lead_id, "name": name or f"Lead {lead_id}", "phone": phone, "created_at": created, "branch_id": 1,
           "email": f"w{lead_id}@example.com", "occasion": "Bride",
           "additional_details.pincode": "560011", "additional_details.occasionDate": "2026-12-20",
           "additional_details.occasionType": "Wedding", "additional_details.estimatedBudgetPerCard": "70-100",
           "additional_details.quantityRequired": 500,
           "additional_details.otherWeddingServices": "Photography & Videography"}
    row.update({f"additional_details.{k}": v for k, v in details.items()})
    return row


ROWS = [
    wf(901, "9845091001", "2026-11-05T05:00:00"),
    wf(902, "9845091002", "2026-10-01T05:00:00", estimatedBudgetPerCard="10-20"),      # before the cut-off
    wf(903, "9845091003", "2026-11-22T05:00:00"),
    wf(904, "9845091004", "2026-11-01T05:00:00"),                                    # two rows, one phone
    wf(905, "9845091004", "2026-11-02T05:00:00"),
]


class FillBase(SpinTestBase):
    def setUp(self):
        super().setUp()
        self.wf_rows = mock.patch.object(base, "WORKFLOW_ROWS", ROWS)
        self.wf_rows.start()

    def tearDown(self):
        self.wf_rows.stop()
        super().tearDown()

    def save(self, rows: list[dict]) -> dict:
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}{K2D}/leads/import",
                                    json={"rows": [{"row": n + 2, "values": v} for n, v in enumerate(rows)]})
        self.assertEqual(resp.status_code, 200, resp.text)
        return {r["row"]: r for r in resp.json()["rows"]}

    def lead(self, phone: str) -> KocDhruvaLead:
        with Session(self.engine) as s:
            return s.exec(select(KocDhruvaLead).where(KocDhruvaLead.phone == phone)).one()


class HeaderAndExcelTests(FillBase):
    def test_excel_range(self):
        self.assertEqual(excel_range(datetime(2020, 10, 1), 2026), "10-20")       # "10-20" -> Oct 2020
        self.assertEqual(excel_range(date(2026, 10, 1), 2026), "1-10")            # "1-10" -> 1 Oct
        self.assertEqual(excel_range(date(2026, 10, 12), 2026), "10-12")          # month-first reading
        self.assertEqual(excel_range("01 Oct 2020", 2026), "10-20")               # as a stored date reads
        self.assertEqual(excel_range("01 Oct 2026", 2026), "1-10")
        for value in ("70-100", "500+", None, 70, "1 October 2026"):
            self.assertIsNone(excel_range(value, 2026), value)

    def test_template_budget_and_quantity_are_text(self):
        from openpyxl import load_workbook

        with self.logged_in("admin"):
            resp = self.client.get(f"{API}{K2D}/leads/template")
        ws = load_workbook(io.BytesIO(resp.content))["Leads"]
        headers = [c.value for c in ws[1]]
        for name in ("Estimated budget per card", "Quantity required"):
            col = headers.index(name) + 1
            self.assertEqual({ws.cell(row=r, column=col).number_format for r in (2, 3, 500, 2001)}, {"@"}, name)
            self.assertIsInstance(ws.cell(row=2, column=col).value, str, name)        # the example row too
        self.assertNotEqual(ws.cell(row=3, column=headers.index("Name") + 1).number_format, "@")

    def test_workflow_and_export_header_names(self):
        year = datetime.now(timezone(timedelta(hours=5, minutes=30))).year
        wb = Workbook()
        ws = wb.active
        ws.title = "Leads"
        ws.append(["Lead", "Contact", "Email ID", "occasionType", "Budget per card", "quantityRequired",
                   "Timestamp", "additional_details.pincode", "Lead occasion", "Other services"])
        ws.append(["A", "9845092001", "a@example.com", "Engagement", datetime(2020, 10, 1), 1200,
                   "02-11-2026 10:00", "560034", "Groom", "Photography"])
        ws.append(["B", "9845092002", None, "Wedding", datetime(year, 10, 1), None, None, None, None, None])
        buf = io.BytesIO()
        wb.save(buf)
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}{K2D}/leads/import/preview", files={"file": ("k.xlsx", buf.getvalue(), XLSX)})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["ignored_columns"], [])
        self.assertEqual(body["columns"], {
            "Lead": "name", "Contact": "phone", "Email ID": "email", "occasionType": "occasionType",
            "Budget per card": "estimatedBudgetPerCard", "quantityRequired": "quantityRequired",
            "Timestamp": "created_at", "additional_details.pincode": "pincode", "Lead occasion": "occasion",
            "Other services": "otherWeddingServices"})
        a, b = (r["values"] for r in body["rows"])
        self.assertEqual((a["estimatedBudgetPerCard"], b["estimatedBudgetPerCard"]), ("10-20", "1-10"))
        self.assertEqual((a["quantityRequired"], a["pincode"], a["occasion"], a["email"]),
                         (1200, "560034", "Groom", "a@example.com"))
        with self.logged_in("admin"):
            self.client.post(f"{API}{K2D}/leads/import", json={"rows": [
                {"row": r["row"], "values": r["values"]} for r in body["rows"]]})
        lead = self.lead("9845092001")
        self.assertEqual(lead.additional_details["estimatedBudgetPerCard"], "10-20")
        self.assertEqual(lead.created_at.replace(tzinfo=None), datetime(2026, 11, 2, 4, 30))   # 10:00 IST


class UploadFillTests(FillBase):
    def test_empty_fields_are_filled_and_stored(self):
        before = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
        out = self.save([
            {"phone": "9845091001", "name": "Lead 901", "quantityRequired": "800"},          # quantity kept
            {"phone": "9845091002", "created_at": "03-10-2026 09:00"},                        # Received kept
            {"phone": "9845091003", "name": "Someone else"},                                  # conflicting name
            {"phone": "9845091004"},                                                          # two rows, no date
            {"phone": "9845091009", "name": "No match"},
        ])
        self.assertEqual({n: r["status"] for n, r in out.items()}, {n: "imported" for n in range(2, 7)})
        self.assertEqual(out[2]["fill"]["workflow_id"], "901")
        self.assertEqual(out[2]["fill"]["received"][:19], "2026-11-05T05:00:00")
        self.assertNotIn("quantityRequired", out[2]["fill"]["fields"])
        self.assertIn("fill_skipped", out[4])
        self.assertIn("fill_skipped", out[5])
        self.assertNotIn("fill", out[6])

        filled = self.lead("9845091001")
        self.assertEqual((filled.name, filled.email, filled.occasion, filled.branch_id),
                         ("Lead 901", "w901@example.com", "Bride", 1))
        self.assertEqual(filled.additional_details, {
            "occasionType": "Wedding", "occasionDate": "2026-12-20", "estimatedBudgetPerCard": "70-100",
            "quantityRequired": 800, "pincode": "560011", "otherWeddingServices": "Photography & Videography"})
        self.assertEqual(filled.created_at.replace(tzinfo=None), datetime(2026, 11, 5, 5, 0))   # the workflow's
        kept = self.lead("9845091002")
        self.assertEqual(kept.created_at.replace(tzinfo=None), datetime(2026, 10, 3, 3, 30))   # the sheet's
        self.assertEqual(kept.additional_details["estimatedBudgetPerCard"], "10-20")         # filled (any date)
        self.assertEqual(self.lead("9845091003").email, None)                                 # not filled
        self.assertGreaterEqual(self.lead("9845091009").created_at.replace(tzinfo=None), before)   # upload time
        with Session(self.engine) as s:
            acts = s.exec(select(LeadActivity).where(LeadActivity.source == "koc_to_dhruva",
                                                     LeadActivity.action == "filled_from_workflow")).all()
        self.assertEqual({a.lead_key for a in acts}, {filled.lead_key, kept.lead_key})
        # Listed with the filled values
        items = {i["lead_key"]: i for i in self.get(f"{K2D}/leads", page_size=100)["items"]}
        item = items[filled.lead_key]
        self.assertEqual((item["estimated_budget_per_card"], item["quantity_required"], item["received"][:16]),
                         ("70-100", 800, "2026-11-05T05:00"))


class BackfillTests(FillBase):
    UPLOAD = datetime(2026, 11, 23, 8, 8, tzinfo=timezone.utc)

    def old_row(self, s, phone, created, **details) -> KocDhruvaLead:
        """What the earlier upload stored at UPLOAD: empty fields; Received = upload time when the
        sheet had none."""
        lead = KocDhruvaLead(origin="imported", name=details.pop("name", None), phone=phone,
                             phone_e164="+91" + phone, source="crm", created_at=created, updated_at=self.UPLOAD,
                             additional_details={"occasionType": "Wedding", **details}, created_by=1)
        s.add(lead)
        s.commit()
        s.add(LeadActivity(source="koc_to_dhruva", lead_key=lead.lead_key, action="created", field="lead",
                           new_value="created", actor_user_id=1, actor_name="Admin", actor_role="admin",
                           created_at=self.UPLOAD + timedelta(seconds=3)))
        s.commit()
        return lead

    def test_preview_apply_and_undo(self):
        upload = self.UPLOAD
        with Session(self.engine, expire_on_commit=False) as s:
            a = self.old_row(s, "9845091001", upload, name="Lead 901", estimatedBudgetPerCard="01 Oct 2020")
            b = self.old_row(s, "9845091003", datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc), quantityRequired=300)
            c = self.old_row(s, "9845091009", upload, estimatedBudgetPerCard="01 Oct 2026")       # no workflow row
            d = self.old_row(s, "9845091004", upload)                                             # two rows
        with Session(self.engine) as s:
            the_plan = backfill.plan(s)
        items = {i["lead_key"]: i for i in the_plan.items}
        self.assertEqual(set(items), {a.lead_key, b.lead_key, c.lead_key})
        self.assertEqual(items[a.lead_key]["budget"], "10-20")                     # read back, not the workflow's
        self.assertNotIn("estimatedBudgetPerCard", items[a.lead_key]["fields"])
        self.assertEqual(items[a.lead_key]["received"].replace(tzinfo=None), datetime(2026, 11, 5, 5, 0))
        self.assertIsNone(items[b.lead_key]["received"])                           # a real Received: kept
        self.assertNotIn("quantityRequired", items[b.lead_key]["fields"])         # uploaded: never overwritten
        self.assertEqual((items[c.lead_key]["budget"], items[c.lead_key]["fields"]), ("1-10", {}))
        self.assertEqual([r["lead_key"] for r in the_plan.no_match], [c.lead_key])
        self.assertEqual([r["lead_key"] for r in the_plan.skipped], [d.lead_key])
        self.assertEqual(the_plan.per_field["email"], 2)
        self.assertEqual(the_plan.per_field["Received"], 1)
        self.assertEqual(the_plan.per_field["Budget (Excel date read back)"], 2)

        def state(s):
            return {x.id: (x.name, x.email, x.occasion, x.branch_id, x.created_at.replace(tzinfo=None),
                           dict(x.additional_details)) for x in s.exec(select(KocDhruvaLead))}

        with Session(self.engine) as s:
            before = state(s)
            undo = backfill.apply(s, backfill.plan(s))
            s.commit()
        with Session(self.engine) as s:
            after = state(s)
            acts = s.exec(select(LeadActivity).where(LeadActivity.action == "filled_from_workflow")).all()
        self.assertEqual(after[a.id][1:4], ("w901@example.com", "Bride", 1))
        self.assertEqual(after[a.id][4], datetime(2026, 11, 5, 5, 0))
        self.assertEqual(after[a.id][5]["estimatedBudgetPerCard"], "10-20")
        self.assertEqual(after[a.id][5]["pincode"], "560011")
        self.assertEqual(after[b.id][5]["quantityRequired"], 300)
        self.assertEqual(after[b.id][4], datetime(2026, 11, 2, 5, 0))
        self.assertEqual(after[c.id][5]["estimatedBudgetPerCard"], "1-10")
        self.assertEqual(after[d.id], before[d.id])
        self.assertEqual(len(acts), 3)
        with Session(self.engine) as s:                                            # the undo SQL restores
            for statement in undo:
                s.exec(text(statement))
            s.commit()
            self.assertEqual(state(s), before)
            self.assertEqual(s.exec(select(LeadActivity).where(LeadActivity.action == "filled_from_workflow")).all(), [])
        with Session(self.engine) as s:                                            # filled once: nothing left
            backfill.apply(s, backfill.plan(s))
            s.commit()
        with Session(self.engine) as s:
            self.assertEqual({i["lead_key"] for i in backfill.plan(s).items}, set())


if __name__ == "__main__":
    unittest.main()
