"""Cross-source duplicates ("also_in") on the 5 lead lists, the also_in=yes|no filter on lists,
stats and exports, and vendor scope.

In-memory SQLite and a fake workflow database (see test_crm_leads_api.ApiTestBase). Run:

    python -m unittest discover -s tests -t . -v

Phone +91 98451 00001 is in:
  wedding   WD1   Bangalore -> Phodeo (area rules)
  whatsapp  WA1   country UAE -> OHP (fallback)
  spin      a CRM spin lead at Jayanagar (Phodeo's branch)
  Vendor -> KOC: one lead from Phodeo, one from OHP
  wedding   WDX   an archived added lead (same source as WD1, so never "also in" for WD1)
The workflow lead 101 (+91 98450 99999) is also sent by RJ to KOC.
"""
import io
import unittest
from datetime import datetime, timezone

from openpyxl import load_workbook
from sqlmodel import Session

from app.models.crm_spin_lead import CrmSpinLead
from app.models.vendor_to_koc import VendorToKocLead
from app.models.wedding_lead import WeddingLead
from app.models.whatsapp_lead import WhatsAppLead
from tests.test_crm_leads_api import API, NOW, ApiTestBase

PHONE = "+919845100001"
LISTS = {"spin": "/spin-leads", "whatsapp": "/whatsapp-leads", "wedding": "/wedding-leads",
         "wedding_planner": "/wedding-plans", "vendor_to_koc": "/vendor-to-koc/leads"}
STATS = {"spin": "/spin-leads/stats", "whatsapp": "/whatsapp-leads/stats", "wedding": "/wedding-leads/stats",
         "wedding_planner": "/wedding-plans/stats", "vendor_to_koc": "/vendor-to-koc/leads/stats"}
EXPORT = {"spin": "/crm-leads/spin/export", "whatsapp": "/crm-leads/whatsapp/export",
          "wedding": "/crm-leads/wedding/export", "wedding_planner": "/crm-leads/wedding_planner/export",
          "vendor_to_koc": "/vendor-to-koc/leads/export"}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class AlsoInTest(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        base = {"row_hash": "", "first_seen_at": NOW, "updated_at": NOW, "origin": "sheet"}
        v = self.vendor_ids
        with Session(self.engine) as s:
            s.add_all([
                WeddingLead(sheet_key="WD1", name="Asha", city="Bangalore", phone="919845100001", phone_e164=PHONE,
                            submitted_at=utc(2026, 11, 10, 5), **base),
                WeddingLead(sheet_key="WD2", name="Other", city="Bangalore", phone="919845100002",
                            phone_e164="+919845100002", submitted_at=utc(2026, 11, 11, 5), **base),
                WeddingLead(sheet_key="crm-WDX", name="Archived", city="Bangalore", phone="919845100001",
                            phone_e164=PHONE, submitted_at=utc(2026, 11, 9, 5), is_deleted=True,
                            **{**base, "origin": "added"}),
                WhatsAppLead(sheet_key="WA1", country="UAE", phone="919845100001", phone_e164=PHONE,
                             submitted_at=utc(2026, 11, 10, 5), **base),
                WhatsAppLead(sheet_key="WA2", country="UAE", phone="971500000002", phone_e164="+971500000002",
                             submitted_at=utc(2026, 11, 10, 5), **base),
            ])
            spin = CrmSpinLead(origin="added", name="Spin dup", phone="9845100001", phone_e164=PHONE, branch_id=4,
                               created_at=utc(2026, 11, 12, 5), updated_at=NOW, additional_details={})
            s.add(spin)
            admin = self.users["admin"].id
            vtk = {"phodeo": VendorToKocLead(vendor_id=v["phodeo"], phone="9845100001", source="manual", created_by=admin,
                                             created_at=NOW, updated_at=NOW),
                   "ohp": VendorToKocLead(vendor_id=v["ohp"], phone="9845100001", source="manual", created_by=admin,
                                          created_at=NOW, updated_at=NOW),
                   "rj": VendorToKocLead(vendor_id=v["rj"], phone="9845099999", source="manual", created_by=admin,
                                         created_at=NOW, updated_at=NOW),
                   "alone": VendorToKocLead(vendor_id=v["phodeo"], phone="9000000009", source="manual", created_by=admin,
                                            created_at=NOW, updated_at=NOW)}
            s.add_all(vtk.values())
            s.commit()
            self.spin_key = spin.lead_key
            self.vtk = {k: lead.id for k, lead in vtk.items()}

    def items(self, place, who="admin", **params) -> list[dict]:
        with self.logged_in(who):
            resp = self.client.get(f"{API}{LISTS[place]}", params={"page_size": 100, **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["items"]

    @staticmethod
    def key(place, item) -> str:
        if place in ("whatsapp", "wedding"):
            return item["lead_key"]
        if place == "wedding_planner":
            return item["plan_id"]
        return str(item["id"])

    def also(self, place, who="admin", **params) -> dict[str, list]:
        return {self.key(place, i): i["also_in"] for i in self.items(place, who, **params)}

    # ── Admin ──
    def test_admin_sees_every_other_place(self):
        wedding = self.also("wedding")
        self.assertEqual(wedding["WD1"], [
            {"source": "spin", "count": 1}, {"source": "whatsapp", "count": 1},
            {"source": "vendor_to_koc", "count": 2, "vendors": ["OHP", "Phodeo"]},
        ])
        self.assertEqual(wedding["WD2"], [])
        self.assertEqual(self.also("whatsapp")["WA1"], [
            {"source": "spin", "count": 1}, {"source": "wedding", "count": 2},              # WD1 + archived WDX
            {"source": "vendor_to_koc", "count": 2, "vendors": ["OHP", "Phodeo"]},
        ])
        spin = self.also("spin")
        self.assertEqual(spin[self.spin_key][0], {"source": "whatsapp", "count": 1})
        self.assertEqual(spin["101"], [{"source": "vendor_to_koc", "count": 1, "vendors": ["RJ"]}])   # workflow lead
        vtk = self.also("vendor_to_koc")
        self.assertEqual(vtk[str(self.vtk["phodeo"])], [
            {"source": "spin", "count": 1}, {"source": "whatsapp", "count": 1}, {"source": "wedding", "count": 2}])
        self.assertEqual(vtk[str(self.vtk["rj"])], [{"source": "spin", "count": 1}])
        self.assertEqual(vtk[str(self.vtk["alone"])], [])                                  # other VTK leads don't count

    # ── Vendor scope ──
    def test_vendor_sees_only_its_own_places(self):
        # Phodeo: WD1 and the spin lead are Phodeo's; WA1 is OHP's; only Phodeo's own Vendor -> KOC lead
        self.assertEqual(self.also("wedding", "phodeo")["WD1"], [
            {"source": "spin", "count": 1}, {"source": "vendor_to_koc", "count": 1}])       # no vendor names
        self.assertEqual(self.also("vendor_to_koc", "phodeo")[str(self.vtk["phodeo"])], [
            {"source": "spin", "count": 1}, {"source": "wedding", "count": 1}])             # not WA1, not archived WDX
        # OHP: sees WA1 only; the wedding and spin copies are Phodeo's
        self.assertEqual(self.also("whatsapp", "ohp")["WA1"], [{"source": "vendor_to_koc", "count": 1}])
        self.assertEqual(self.also("vendor_to_koc", "ohp")[str(self.vtk["ohp"])], [{"source": "whatsapp", "count": 1}])
        # RJ: its Vendor -> KOC lead's phone is workflow lead 101 (Chamrajpet branch: RJ's)
        self.assertEqual(self.also("vendor_to_koc", "rj")[str(self.vtk["rj"])], [{"source": "spin", "count": 1}])
        self.assertEqual(self.also("spin", "rj")["101"], [{"source": "vendor_to_koc", "count": 1}])
        # Nothing about other vendors leaks anywhere in their answers
        for who, place in (("phodeo", "wedding"), ("ohp", "whatsapp"), ("phodeo", "vendor_to_koc")):
            text = str(self.items(place, who))
            self.assertNotIn("vendors", text)

    # ── also_in filter: lists, stats, exports ──
    def test_filter_yes_no(self):
        cases = [
            ("admin", "wedding", {"WD1"}, {"WD2"}),
            ("admin", "whatsapp", {"WA1"}, {"WA2"}),
            ("admin", "spin", {self.spin_key, "101"}, set()),
            ("admin", "vendor_to_koc", {str(self.vtk[k]) for k in ("phodeo", "ohp", "rj")}, {str(self.vtk["alone"])}),
            ("phodeo", "wedding", {"WD1"}, {"WD2"}),
            ("phodeo", "vendor_to_koc", {str(self.vtk["phodeo"])}, {str(self.vtk["alone"])}),
            ("ohp", "whatsapp", {"WA1"}, {"WA2"}),
            ("ohp", "vendor_to_koc", {str(self.vtk["ohp"])}, set()),
        ]
        for who, place, yes, no in cases:
            with self.subTest(who=who, place=place):
                got_yes = {self.key(place, i) for i in self.items(place, who, also_in="yes")}
                got_no = {self.key(place, i) for i in self.items(place, who, also_in="no")}
                self.assertEqual(got_yes, yes)
                self.assertEqual(got_no, no)
                # The filter and the per-lead also_in always agree
                self.assertTrue(all(i["also_in"] for i in self.items(place, who, also_in="yes")))
                self.assertTrue(all(not i["also_in"] for i in self.items(place, who, also_in="no")))

    def test_stats_and_export_follow_the_filter(self):
        for who in ("admin", "phodeo", "ohp"):
            for place in LISTS:
                if who != "admin" and place == "wedding_planner":
                    continue
                for value in ("yes", "no"):
                    with self.subTest(who=who, place=place, also_in=value), self.logged_in(who):
                        listed = self.client.get(f"{API}{LISTS[place]}", params={"page_size": 100, "also_in": value}).json()
                        stats = self.client.get(f"{API}{STATS[place]}", params={"also_in": value})
                        self.assertEqual(stats.status_code, 200, stats.text)
                        self.assertEqual(stats.json()["total"], listed["total"])
                        resp = self.client.get(f"{API}{EXPORT[place]}", params={"also_in": value})
                        self.assertEqual(resp.status_code, 200, resp.text)
                        rows = list(load_workbook(io.BytesIO(resp.content)).active.iter_rows(min_row=2, values_only=True))
                        self.assertEqual(len(rows), listed["total"])

    def test_bad_value(self):
        for place in LISTS:
            self.assertEqual(self.client.get(f"{API}{LISTS[place]}", params={"also_in": "maybe"}).status_code, 422, place)


if __name__ == "__main__":
    unittest.main()
