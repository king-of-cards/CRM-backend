"""Payout: the commission on a converted lead, as a % of its conversion amount (admins set it).

KOC -> Vendors leads (PUT /leads/{source}/{key}/crm) and Vendors -> KOC leads (PATCH
/vendor-to-koc/leads/{id}): payout_percent is saved, payout_amount is always worked out by the server
(conversion amount x % / 100, rounded), vendors can't set it, and the dashboards add it up
(kpis payout_count / payout_total). Run:

    python -m unittest tests.test_payout -v
"""
import unittest
from decimal import Decimal

from sqlmodel import Session, select

from app.core import response_cache
from app.models.lead_meta import LeadMeta
from app.models.vendor_to_koc import VendorToKocLead
from app.services import kpis
from app.services.lead_crm import Record, payout_of
from tests.test_crm_leads_api import ApiTestBase
from tests.test_vendor_to_koc import BASE, VendorToKocTestCase


class PayoutMathTest(unittest.TestCase):
    def test_amount_from_conversion_and_percent(self):
        self.assertEqual(payout_of(40000, Decimal("10")), 4000)
        self.assertEqual(payout_of(50000, Decimal("7.5")), 3750)
        self.assertEqual(payout_of(1005, Decimal("50")), 503)          # half rounds up
        self.assertIsNone(payout_of(None, Decimal("10")))
        self.assertIsNone(payout_of(40000, None))
        self.assertIsNone(payout_of(40000, Decimal("0")))

    def test_kpis_add_up_payouts(self):
        rows = [Record("converted", None, 40000, payout_amount=4000), Record("converted", None, 10000),
                Record("converted", None, 20000, payout_amount=1500), None]
        numbers = kpis.compute(rows)
        self.assertEqual((numbers["payout_count"], numbers["payout_total"]), (2, 5500))
        self.assertEqual(set(kpis.KEYS), set(numbers))


class LeadCrmPayoutTest(ApiTestBase, unittest.TestCase):
    def meta(self, key) -> LeadMeta:
        with Session(self.engine) as s:
            return s.exec(select(LeadMeta).where(LeadMeta.source == "wedding", LeadMeta.lead_key == key)).first()

    def test_admin_sets_the_percent_and_the_server_works_out_the_amount(self):
        key = self.add_ok("wedding")["lead_key"]
        resp = self.put_crm("admin", "wedding", key, {"update_status": "converted", "conversion_amount": 40000,
                                                       "payout_percent": 10, "payout_amount": 999999})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((resp.json()["payout_percent"], resp.json()["payout_amount"]), (10.0, 4000))
        meta = self.meta(key)
        self.assertEqual((meta.payout_percent, meta.payout_amount), (Decimal("10.00"), 4000))
        # The % with decimals; clearing it clears the amount
        self.assertEqual(self.put_crm("admin", "wedding", key, {"payout_percent": 7.5}).json()["payout_amount"], 3000)
        cleared = self.put_crm("admin", "wedding", key, {"payout_percent": None}).json()
        self.assertEqual((cleared["payout_percent"], cleared["payout_amount"]), (None, None))

    def test_vendors_cannot_set_it_but_their_conversion_amount_updates_it(self):
        key = self.add_ok("wedding")["lead_key"]
        self.put_crm("admin", "wedding", key, {"conversion_amount": 40000, "payout_percent": 10})
        for body in ({"payout_percent": 20}, {"payout_amount": 1}):
            resp = self.put_crm("phodeo", "wedding", key, body)
            self.assertEqual(resp.status_code, 403, body)
            self.assertIn("payout", resp.text)
        resp = self.put_crm("phodeo", "wedding", key, {"conversion_amount": 60000})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["payout_amount"], 6000)               # vendors see it
        self.assertEqual(self.meta(key).payout_amount, 6000)

    def test_dashboard_adds_up_the_payouts(self):
        first = self.add_ok("wedding")["lead_key"]
        self.put_crm("admin", "wedding", first, {"update_status": "converted", "conversion_amount": 40000,
                                                  "payout_percent": 10})
        response_cache.clear()
        resp = self.client.get("/api/v1/analytics/overview")
        self.assertEqual(resp.status_code, 200, resp.text)
        numbers = resp.json()["kpis"]
        self.assertEqual((numbers["payout_count"], numbers["payout_total"]), (1, 4000))

    def test_validation(self):
        key = self.add_ok("wedding")["lead_key"]
        for body in ({"payout_percent": -1}, {"payout_percent": 100.01}, {"payout_percent": 12.345}):
            self.assertEqual(self.put_crm("admin", "wedding", key, body).status_code, 422, body)
        self.assertEqual(self.put_crm("admin", "wedding", key, {"payout_percent": 100}).status_code, 200)


class VendorToKocPayoutTest(VendorToKocTestCase):
    def test_admin_patch_works_out_the_amount_and_vendors_see_it(self):
        lead_id = self.add_lead(self.phodeo_id, "9845011111")
        resp = self.client.patch(f"{BASE}/leads/{lead_id}", headers=self.admin,
                                 json={"conversion_amount": 50000, "payout_percent": 7.5})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((resp.json()["payout_percent"], resp.json()["payout_amount"]), (7.5, 3750))
        # Changing the conversion amount alone updates the payout
        resp = self.client.patch(f"{BASE}/leads/{lead_id}", headers=self.admin, json={"conversion_amount": 80000})
        self.assertEqual(resp.json()["payout_amount"], 6000)
        with Session(self.engine) as s:
            self.assertEqual(s.get(VendorToKocLead, lead_id).payout_amount, 6000)
        response_cache.clear()
        stats = self.client.get(f"{BASE}/leads/stats", headers=self.admin)
        self.assertEqual(stats.status_code, 200, stats.text)
        self.assertEqual((stats.json()["kpis"]["payout_count"], stats.json()["kpis"]["payout_total"]), (1, 6000))

    def test_the_dashboard_shows_a_new_payout_at_once(self):
        lead_id = self.add_lead(self.phodeo_id, "9845044444", conversion_amount=40000)
        before = self.client.get("/api/v1/analytics/vendor-to-koc", headers=self.phodeo)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(before.json()["kpis"]["payout_total"], 0)
        self.client.patch(f"{BASE}/leads/{lead_id}", headers=self.phodeo, json={"payout_percent": 10})
        after = self.client.get("/api/v1/analytics/vendor-to-koc", headers=self.phodeo).json()   # no refresh
        self.assertEqual((after["kpis"]["payout_count"], after["kpis"]["payout_total"]), (1, 4000))

    def test_vendors_set_their_own_payout_percent_and_nothing_else(self):
        own = self.add_lead(self.phodeo_id, "9845022222", conversion_amount=40000, update_status="converted")
        foreign = self.add_lead(self.other_id, "9845033333", conversion_amount=40000)
        resp = self.client.patch(f"{BASE}/leads/{own}", headers=self.phodeo, json={"payout_percent": 12.5})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual((resp.json()["payout_percent"], resp.json()["payout_amount"]), (12.5, 5000))
        self.assertNotIn("status_updated_by", resp.json())                     # vendor view of the lead
        with Session(self.engine) as s:
            lead = s.get(VendorToKocLead, own)
            self.assertEqual(lead.payout_amount, 5000)
            self.assertIsNone(lead.status_updated_by)                         # not KOC's update
        # Anything else, together or alone: 403 (and nothing saved)
        for body in ({"payout_percent": 10, "update_status": "interested"}, {"conversion_amount": 1},
                     {"additional_comments": "x"}):
            self.assertEqual(self.client.patch(f"{BASE}/leads/{own}", headers=self.phodeo, json=body).status_code,
                             403, body)
        with Session(self.engine) as s:
            self.assertEqual(s.get(VendorToKocLead, own).payout_percent, Decimal("12.50"))
        # Another vendor's lead: not found
        self.assertEqual(self.client.patch(f"{BASE}/leads/{foreign}", headers=self.phodeo,
                                           json={"payout_percent": 10}).status_code, 404)
        # Activity: the vendor's change is logged under the vendor
        activity = self.client.get(f"{BASE}/leads/{own}/activity", headers=self.phodeo)
        self.assertEqual(activity.status_code, 200, activity.text)
        fields = {i.get("field") for i in activity.json()["items"]}
        self.assertTrue({"payout_percent", "payout_amount"} <= fields, fields)



if __name__ == "__main__":
    unittest.main()
