"""The 4 sources' "Update" (replacing Interest / Converted) and conversion amount:
editing (PUT /leads/{source}/{key}/crm) and bulk uploads of new leads.

In-memory SQLite and a fake workflow database (see test_crm_leads_api.ApiTestBase). Run:

    python -m unittest discover -s tests -t . -v
"""
import io
import unittest

from openpyxl import load_workbook
from sqlmodel import Session, select

from app.models.lead_meta import LeadMeta
from tests.test_crm_leads_api import API, ApiTestBase


class UpdateEditTest(ApiTestBase, unittest.TestCase):
    def meta(self, source, key) -> LeadMeta | None:
        with Session(self.engine) as s:
            return s.exec(select(LeadMeta).where(LeadMeta.source == source, LeadMeta.lead_key == key)).first()

    def test_every_update_value_and_pending(self):
        key = self.add_ok("wedding")["lead_key"]
        for value in ("converted", "not_converted", "purchased_elsewhere", "purchased_with_us",
                      "will_call_back", "interested", "not_interested", None):
            resp = self.put_crm("admin", "wedding", key, {"update_status": value})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["update_status"], value)
            self.assertEqual(self.meta("wedding", key).update_status, value)

    def test_validation(self):
        key = self.add_ok("wedding")["lead_key"]
        for body in ({"update_status": "other"}, {"update_status": "follow_up"},
                     {"conversion_amount": -1}, {"conversion_amount": 100_000_001}, {"quoted_amount": 100_000_001},
                     {"interest": "interested"}, {"converted": True}, {"update_status": "converted", "converted": True}):
            resp = self.put_crm("admin", "wedding", key, body)
            self.assertEqual(resp.status_code, 422, body)
        resp = self.put_crm("admin", "wedding", key, {"interest": "interested"})
        self.assertIn("replaced by update_status", resp.text)
        self.assertIsNone(self.meta("wedding", key))                       # nothing saved
        resp = self.put_crm("admin", "wedding", key, {"conversion_amount": 100_000_000, "quoted_amount": 0})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((resp.json()["conversion_amount"], resp.json()["quoted_amount"]), (100_000_000, 0))

    def test_only_fields_sent_change(self):
        key = self.add_ok("wedding")["lead_key"]
        self.put_crm("admin", "wedding", key, {"update_status": "converted", "conversion_amount": 50000, "notes": "n"})
        self.put_crm("phodeo", "wedding", key, {"quoted_amount": 60000})
        meta = self.meta("wedding", key)
        self.assertEqual((meta.update_status, meta.conversion_amount, meta.quoted_amount, meta.notes),
                         ("converted", 50000, 60000, "n"))
        self.put_crm("admin", "wedding", key, {"conversion_amount": None})
        self.assertIsNone(self.meta("wedding", key).conversion_amount)


class UploadCrmColumnsTest(ApiTestBase, unittest.TestCase):
    URL = f"{API}/crm-leads/wedding"

    def preview(self, csv_text: str) -> dict:
        resp = self.client.post(f"{self.URL}/import/preview",
                                files={"file": ("w.csv", csv_text.encode("utf-8-sig"), "text/csv")})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def imported(self, body: dict) -> dict:
        resp = self.client.post(f"{self.URL}/import", json={"rows": [{"row": r["row"], "values": r["values"]}
                                                                    for r in body["rows"]]})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def metas(self) -> dict[str, LeadMeta]:
        with Session(self.engine) as s:
            return {m.lead_key: m for m in s.exec(select(LeadMeta).where(LeadMeta.source == "wedding"))}

    def test_new_leads_get_update_and_amounts(self):
        body = self.preview(
            "Contact,Lead,Assigned to,Update,Quoted amount,Conversion amount,Quoted date\r\n"
            "9845030001,One,Phodeo,Will call back,\"₹1,50,000\",,20 Dec 2026\r\n"
            "9845030002,Two,,converted,90000,85000,\r\n"
            "9845030003,Three,Automatic,Pending,,,\r\n"
            "9845030004,Four,Automatic,,,,\r\n")
        self.assertEqual([r["status"] for r in body["rows"]], ["ok"] * 4)
        self.assertEqual(body["read_only_columns"], ["Quoted date"])          # never read back
        self.assertEqual({k: body["rows"][0]["values"][k] for k in ("update_status", "quoted_amount", "conversion_amount")},
                         {"update_status": "will_call_back", "quoted_amount": 150000, "conversion_amount": None})
        result = self.imported(body)
        self.assertEqual(result["counts"]["imported"], 4)
        keys = [r["lead_key"] for r in result["rows"]]
        metas = self.metas()
        one, two = metas[keys[0]], metas[keys[1]]
        self.assertEqual((one.update_status, one.quoted_amount, one.conversion_amount, one.quoted_date, one.vendor_id),
                         ("will_call_back", 150000, None, None, self.vendor_ids["phodeo"]))
        self.assertEqual((two.update_status, two.quoted_amount, two.conversion_amount, two.vendor_id, two.auto_assign),
                         ("converted", 90000, 85000, None, False))           # empty Assigned to: unassigned
        self.assertNotIn(keys[2], metas)                                      # automatic, Pending, no amounts: no row
        self.assertNotIn(keys[3], metas)
        self.assertEqual(one.updated_by, self.users["admin"].id)
        # The list shows them
        items = {i["crm"]["lead_key"]: i["crm"] for i in self.list_items("wedding")}
        self.assertEqual((items[keys[1]]["update_status"], items[keys[1]]["conversion_amount"]), ("converted", 85000))

    def test_invalid_values_are_row_errors(self):
        body = self.preview("Contact,Update,Quoted amount,Conversion amount\r\n"
                            "9845030011,Maybe,,\r\n"
                            "9845030012,,lots,\r\n"
                            "9845030013,,,200000000\r\n")
        self.assertEqual([r["status"] for r in body["rows"]], ["error"] * 3)
        self.assertEqual([[e["field"] for e in r["errors"]] for r in body["rows"]],
                         [["update_status"], ["quoted_amount"], ["conversion_amount"]])
        self.assertIn("Unknown update", body["rows"][0]["errors"][0]["message"])
        result = self.imported(body)                                          # re-checked: nothing saved
        self.assertEqual(result["counts"]["imported"], 0)
        self.assertEqual(self.metas(), {})

    def test_duplicates_are_unchanged(self):
        key = self.add_ok("wedding")["lead_key"]                              # 9845010003
        self.put_crm("admin", "wedding", key, {"update_status": "interested", "quoted_amount": 1000})
        body = self.preview("Contact,Lead,City,Budget,Update,Quoted amount,Conversion amount\r\n"
                            "+91 98450 10003,Ravi,Bangalore,1.5 lakh,Converted,99999,88888\r\n")   # the same lead
        self.assertEqual(body["rows"][0]["status"], "duplicate")
        self.assertEqual(self.imported(body)["counts"]["imported"], 0)
        meta = self.metas()[key]
        self.assertEqual((meta.update_status, meta.quoted_amount, meta.conversion_amount), ("interested", 1000, None))

    def test_export_uploads_back_and_template_offers_update(self):
        key = self.add_ok("wedding")["lead_key"]
        self.put_crm("admin", "wedding", key, {"update_status": "purchased_with_us", "conversion_amount": 70000})
        out = self.export_rows("wedding", "csv")
        header = out[0]
        row = next(r for r in out[1:] if r[header.index("Contact")].endswith("9845010003"))
        self.assertEqual((row[header.index("Update")], row[header.index("Conversion amount")]),
                         ("Already purchased with us", "70000"))
        csv_text = "\r\n".join(",".join(f'"{v}"' for v in r) for r in out) + "\r\n"
        body = self.preview(csv_text)
        self.assertEqual(body["rows"][0]["status"], "duplicate")             # same lead: unchanged
        self.assertEqual(body["rows"][0]["values"]["update_status"], "purchased_with_us")   # the label reads back
        wb = load_workbook(io.BytesIO(self.client.get(f"{self.URL}/template").content))
        headers = [c.value for c in wb["Leads"][1]]
        self.assertIn("Update", headers)
        lists = wb["Lists"]
        update_col = next(c.column for c in lists[1] if c.value == "Update")
        values = [lists.cell(row=r, column=update_col).value for r in range(2, 10)]
        self.assertEqual(values[:2], ["Pending", "Converted"])
        self.assertEqual(len(values), 8)


if __name__ == "__main__":
    unittest.main()
