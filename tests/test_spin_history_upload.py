"""Spin after the history change: gift leads back in spin (auto vendor Kraftyz), the SPIN_LIVE_FROM
cut-off, the history upload (duplicate check, Assigned to, new columns), KOC -> Dhruva unchanged,
totals, and the re-key of hidden leads' CRM data. In-memory SQLite and the fake workflow database
only. Run:

    python -m unittest discover -s tests -t . -v
"""
import csv
import io
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from sqlmodel import Session, select

import tests.test_crm_leads_api as base
from tests.test_crm_leads_api import API, NOW, ApiTestBase

from app.api.v1.endpoints import analytics
from app.api.v1.endpoints import spin_leads as sl
from app.api.v1.endpoints import vendors as vendors_ep
from app.core import response_cache
from app.core.config import settings
from app.models.crm_spin_lead import CrmSpinLead
from app.models.hot_lead import CrmHotLead
from app.models.lead_activity import LeadActivity
from app.models.lead_meta import LeadMeta
from app.models.notification import CrmNotification
from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser
from app.services import spin_history_rekey as rekey

GIFT = "Wedding Gifts & Return Gifts"
CUTOFF = date(2026, 11, 21)                       # 00:00 IST = 2026-11-20 18:30 UTC
K2D = "/koc-to-dhruva"


def row(lead_id, phone, created, services="Photography & Videography", budget="10-20", quantity=None,
        pincode="560011", listed=True):
    r = {"id": lead_id, "name": f"Lead {lead_id}", "phone": phone, "created_at": created, "branch_id": None,
         "additional_details.pincode": pincode, "additional_details.occasionDate": "2026-12-20",
         "additional_details.estimatedBudgetPerCard": budget, "additional_details.quantityRequired": quantity,
         "additional_details.otherWeddingServices": services}
    if not listed:
        r["listed"] = False
    return r


ROWS = [
    row(401, "9845080001", "2026-11-10T05:00:00"),                              # history: hidden
    row(402, "9845080002", "2026-11-20T18:29:59", services=GIFT),               # 23:59:59 IST, 20 Nov: hidden
    row(403, "9845080003", "2026-11-20T18:30:00"),                              # 00:00 IST, 21 Nov: live
    row(404, "9845080004", "2026-11-21T06:00:00", services=f"Photography & Videography, {GIFT}"),  # live gift
    row(405, "9845080005", "2026-11-21T07:00:00", listed=False),                # live but not listed
    row(406, "9845080006", "2026-10-01T05:00:00", budget="100-200"),            # history, KOC (budget)
]
LIVE = {"403", "404"}
# Every upload field of the workflow rows 403 / 405 (but the name)
LIVE_403 = {"name": "Lead 403", "pincode": "560011", "occasionDate": "20-12-2026", "estimatedBudgetPerCard": "10-20",
            "otherWeddingServices": "Photography & Videography"}


def fake_analytics_rows():
    """What analytics._workflow_spin_rows reads (the rows in spin), from the fake rows."""
    return [{"key": str(r["id"]), "created": datetime.fromisoformat(r["created_at"]), "occasion_type": None,
             "occasion_date": r["additional_details.occasionDate"], "pincode": r["additional_details.pincode"],
             "is_workflow": True} for r in base.WORKFLOW_ROWS if base.in_spin(r)]


class SpinTestBase(ApiTestBase, unittest.TestCase):
    cutoff = CUTOFF

    def setUp(self):
        super().setUp()
        self.extra = [mock.patch.object(base, "WORKFLOW_ROWS", ROWS),
                      mock.patch.object(settings, "SPIN_LIVE_FROM", self.cutoff),
                      mock.patch.object(analytics, "_workflow_spin_rows", fake_analytics_rows)]
        for p in self.extra:
            p.start()
        with Session(self.engine, expire_on_commit=False) as s:
            kraftyz = Vendor(name="Kraftyz", coverage="Wedding gifts", priority=50, created_at=NOW, updated_at=NOW)
            s.add(kraftyz)
            self.users["kraftyz"] = User(name="kraftyz", email="kraftyz@example.com", hashed_password="x",
                                         role="vendor")
            s.add(self.users["kraftyz"])
            s.commit()
            s.add(VendorUser(user_id=self.users["kraftyz"].id, vendor_id=kraftyz.id))
            s.commit()
            self.vendor_ids["kraftyz"] = kraftyz.id
        response_cache.clear()

    def tearDown(self):
        for p in self.extra:
            p.stop()
        super().tearDown()

    def get(self, path, who="admin", status=200, **params):
        with self.logged_in(who):
            resp = self.client.get(f"{API}{path}", params=params)
        self.assertEqual(resp.status_code, status, f"{path}: {resp.text}")
        return resp.json() if status == 200 else resp

    def spin_items(self, who="admin", **params) -> dict[str, dict]:
        items = self.get("/spin-leads", who, page_size=100, **params)["items"]
        return {str(i["id"]): i for i in items}

    def import_rows(self, rows: list[dict]) -> list[dict]:
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}/crm-leads/spin/import",
                                    json={"rows": [{"row": n + 2, "values": v} for n, v in enumerate(rows)]})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["rows"]

    def export(self, **params) -> list[list[str]]:
        with self.logged_in("admin"):
            resp = self.client.get(f"{API}/crm-leads/spin/export", params={"format": "csv", **params})
        self.assertEqual(resp.status_code, 200, resp.text)
        return list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))

    @staticmethod
    def vendor_of(item) -> tuple[str | None, str | None]:
        crm = item["crm"]
        return (crm["vendor"] or {}).get("name"), crm["vendor_source"]

    @staticmethod
    def vendors_of(item) -> list[tuple[str, str]]:
        return [(e["vendor"]["name"], e["vendor_source"]) for e in item["crm"]["vendors"]]


# ── A: gift leads in spin -> Kraftyz ───────────────────────────────────
class GiftVendorTests(SpinTestBase):
    def test_gift_leads_are_spin_leads_assigned_to_kraftyz(self):
        items = self.spin_items()
        self.assertEqual(set(items), LIVE)                                  # gifts listed in spin itself
        # A gift lead received after WEDDING_GIFTS_FROM: its area vendor AND Kraftyz (a partner)
        self.assertEqual(self.vendors_of(items["404"]), [("Phodeo", "suggested"), ("Kraftyz", "partner")])
        self.assertEqual(self.vendors_of(items["403"]), [("Phodeo", "suggested")])   # area rules (560011)
        self.assertEqual([e["vendor"]["name"] for e in self.get("/leads/spin/404/crm")["vendors"]], ["Phodeo", "Kraftyz"])
        # Vendor visibility follows the same assignment
        self.assertEqual(set(self.spin_items("kraftyz")), {"404"})
        self.assertEqual(set(self.spin_items("phodeo")), {"403", "404"})
        # The old source is gone everywhere
        self.get("/wedding-gifts-leads", status=404)
        self.get("/crm-leads/wedding_gifts/export", status=422)
        self.get("/reminders/follow-ups", status=400, sources="wedding_gifts")
        self.get("/analytics/overview", status=400, sources="wedding_gifts")
        overview = self.get("/analytics/overview", period="all")
        self.assertNotIn("wedding_gifts", overview["totals"])

    def test_manual_wins_and_missing_vendor_leaves_it_unassigned(self):
        resp = self.put_crm("admin", "spin", "404", {"vendor_id": self.vendor_ids["rj"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.vendors_of(self.spin_items()["404"]), [("RJ", "manual"), ("Kraftyz", "partner")])
        self.put_crm("admin", "spin", "404", {"vendor_id": None})                  # back to automatic
        self.assertEqual(self.vendors_of(self.spin_items()["404"]), [("Phodeo", "suggested"), ("Kraftyz", "partner")])
        with mock.patch.object(settings, "WEDDING_GIFTS_VENDOR_NAME", "Someone else"):
            response_cache.clear()
            self.assertEqual(self.vendors_of(self.spin_items()["404"]), [("Phodeo", "suggested")])   # no partner
        with mock.patch.object(settings, "WEDDING_GIFTS_FROM", datetime(2026, 11, 22, 0, 0)):
            response_cache.clear()                                   # received before the Kraftyz start
            self.assertEqual(self.vendors_of(self.spin_items()["404"]), [("Phodeo", "suggested")])


# ── B: the cut-off ─────────────────────────────────────────────────────
class CutoffSqlTests(unittest.TestCase):
    def test_spin_sql_is_listed_and_on_or_after_the_cutoff(self):
        from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.dialects.postgresql import JSONB

        table = Table("spin_lead", MetaData(), Column("id", BigInteger, primary_key=True),
                      Column("created_at", DateTime), Column("additional_details", JSONB))

        def sql(conditions):
            return " AND ".join(str(c.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
                                for c in conditions)
        with mock.patch.object(settings, "SPIN_LIVE_FROM", CUTOFF):
            spin = sql(sl.base_filter(table, sl.SPIN))
            koc = sql(sl.base_filter(table, sl.KOC_TO_DHRUVA))
        self.assertIn("spin_lead.created_at >= '2026-11-20 18:30:00'", spin)
        self.assertIn("'otherWeddingServices'", spin)                    # listed rows only
        self.assertNotIn("LIKE", spin)                                    # no gift split any more
        self.assertIn("spin_lead.created_at >= '2026-11-20 18:30:00'", koc)   # KOC -> Dhruva: the same cut-off
        self.assertEqual(sql(sl.live_filter(Table("t", MetaData(), Column("id", BigInteger)))), "false")


class CutoffTests(SpinTestBase):
    def test_rows_before_the_cutoff_are_hidden_everywhere(self):
        self.assertTrue(sl.is_live("2026-11-20T18:30:00") and not sl.is_live("2026-11-20T18:29:59"))
        self.assertEqual(sl.spin_live_from_utc(), datetime(2026, 11, 20, 18, 30))
        self.assertEqual(set(self.spin_items()), LIVE)
        self.assertEqual(self.get("/spin-leads/stats")["total"], 2)
        self.assertEqual(len(self.export()) - 1, 2)
        self.assertEqual(self.get("/analytics/overview", period="all")["totals"]["spin"], 2)
        for key in ("401", "402", "405"):                                   # hidden or never listed
            self.get(f"/leads/spin/{key}/crm", status=404)
            self.assertEqual(self.put_crm("admin", "spin", key, {"update_status": "converted"}).status_code, 404)
            with self.logged_in("admin"):
                self.assertEqual(self.client.post(f"{API}/leads/spin/{key}/hot").status_code, 404)
        self.assertEqual(set(self.spin_items("phodeo")), {"403", "404"})   # 401 (Phodeo's area) hidden

    def test_reminders_and_also_in(self):
        with mock.patch.object(settings, "SPIN_LIVE_FROM", date(2000, 1, 1)):    # before: everything live
            response_cache.clear()
            for key in ("401", "403"):
                self.assertEqual(self.put_crm("admin", "spin", key, {"follow_up_date": "2026-11-22"}).status_code, 200)
        response_cache.clear()
        reminders = self.get("/reminders/follow-ups", days=30, sources="spin")["items"]
        self.assertEqual([i["lead_key"] for i in reminders], ["403"])      # 401's row is kept, but hidden
        with Session(self.engine) as s:
            self.assertIsNotNone(s.exec(select(LeadMeta).where(LeadMeta.lead_key == "401")).first())
        # International leads with the phones of a hidden and a live spin row
        self.add_ok("whatsapp", {"phone": "+919845080001", "country": "India"})
        self.add_ok("whatsapp", {"phone": "+919845080003", "country": "India"})
        also = {i["phone"]: {a["source"] for a in i["also_in"]} for i in self.list_items("whatsapp")}
        self.assertEqual(also["919845080001"], set())
        self.assertEqual(also["919845080003"], {"spin"})

    def test_upload_duplicate_check_ignores_rows_before_the_cutoff(self):
        rows = self.import_rows([
            {"phone": "9845080001", "name": "History 401", "created_at": "10-11-2026 10:30"},   # hidden row: ok
            {"phone": "9845080003", **LIVE_403},                                                 # live row
            {"phone": "9845080005", **LIVE_403, "name": "Lead 405"},                             # live row too
            {"phone": "9845080003", **LIVE_403, "name": "Someone else"},                         # same phone only
        ])
        self.assertEqual([r["status"] for r in rows], ["imported", "duplicate", "duplicate", "imported"])
        self.assertEqual({r["duplicate"]["reason"] for r in rows[1:3]}, {"existing_workflow"})
        self.assertEqual(rows[1]["duplicate"]["lead_key"], "403")
        # Other sources no longer warn about the hidden row
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}/crm-leads/whatsapp/import", json={"rows": [
                {"row": 2, "values": {"phone": "+919845080002", "country": "India"}},
                {"row": 3, "values": {"phone": "+919845080004", "country": "India"}}]})
        warnings = [r["warnings"] for r in resp.json()["rows"]]
        self.assertEqual(warnings[0], [])
        self.assertEqual(warnings[1], [{"type": "other_source", "sources": ["spin"]}])


# ── B: the history upload ──────────────────────────────────────────────
class HistoryUploadTests(SpinTestBase):
    def test_assigned_to_automatic_gift_and_new_columns(self):
        rows = self.import_rows([
            {"phone": "9845081001", "name": "Manual", "pincode": "560011", "assigned_to": "OHP",
             "created_at": "01-06-2026 10:00"},
            {"phone": "9845081002", "name": "Auto gift", "pincode": "560011", "assigned_to": "Automatic",
             "otherWeddingServices": f"Wedding Decor, {GIFT}", "quantityRequired": "1500",
             "estimatedBudgetPerCard": "100-200"},
            {"phone": "9845081003", "name": "Auto plain", "pincode": "560011", "assigned_to": "Automatic",
             "otherWeddingServices": "Wedding Decor"},
            {"phone": "9845081004", "name": "Empty", "pincode": "560011", "otherWeddingServices": GIFT},
            {"phone": "9845081005", "name": "Gift manual", "assigned_to": "RJ", "otherWeddingServices": GIFT},
        ])
        self.assertEqual([r["status"] for r in rows], ["imported"] * 5, rows)
        key = {r["values"]["name"]: r["lead_key"] for r in rows}
        items = self.spin_items()
        self.assertEqual(self.vendor_of(items[key["Manual"]]), ("OHP", "manual"))
        self.assertEqual(self.vendors_of(items[key["Auto gift"]]), [("Phodeo", "suggested"), ("Kraftyz", "partner")])
        self.assertEqual(self.vendor_of(items[key["Auto plain"]]), ("Phodeo", "suggested"))
        self.assertEqual(self.vendor_of(items[key["Empty"]]), (None, None))          # empty = unassigned
        self.assertEqual(self.vendors_of(items[key["Gift manual"]]), [("RJ", "manual"), ("Kraftyz", "partner")])
        self.assertEqual(items[key["Manual"]]["created_at"][:10], "2026-06-01")      # history date kept, listed
        with Session(self.engine) as s:
            lead = s.get(CrmSpinLead, int(key["Auto gift"].split("-")[1]))
            self.assertEqual((lead.additional_details["otherWeddingServices"],
                              lead.additional_details["quantityRequired"]), (f"Wedding Decor, {GIFT}", 1500))
        # Not in KOC -> Dhruva although budget 100-200 and quantity 1500
        self.assertNotIn(key["Auto gift"], {i["lead_key"] for i in self.get(f"{K2D}/leads", page_size=100)["items"]})
        # Export / template: the two new columns, filled
        export = self.export()
        header = export[0]
        self.assertEqual(header[8:11], ["Budget", "Other wedding services", "Quantity required"])
        by_name = {r[0]: r for r in export[1:]}
        self.assertEqual(by_name["Auto gift"][9:11], [f"Wedding Decor, {GIFT}", "1500"])
        with self.logged_in("admin"):
            template = self.client.get(f"{API}/crm-leads/spin/template", params={"format": "csv"})
        self.assertEqual(next(csv.reader(io.StringIO(template.content.decode("utf-8-sig"))))[8:11], header[8:11])

    def test_other_services_filter_and_options(self):
        self.import_rows([{"phone": "9845082001", "name": "Decor", "otherWeddingServices": "Wedding Decor"}])
        cases = [({"other_services": "gifts & return"}, {"404"}), ({"other_services": "photography"}, {"403", "404"}),
                 ({"other_services": "decor"}, {"crm-1"})]
        for params, expected in cases:
            self.assertEqual(set(self.spin_items(**params)), expected, params)
            self.assertEqual(self.get("/spin-leads/stats", **params)["total"], len(expected), params)
            self.assertEqual(len(self.export(**params)) - 1, len(expected), params)
        options = self.get("/spin-leads/filter-options")["other_services"]
        self.assertIn({"value": "Wedding Decor", "count": 1}, options)               # CRM part (no workflow DB)
        self.assertNotIn("other_services", self.get(f"{K2D}/leads/filter-options"))   # KOC unchanged


# ── KOC -> Dhruva unchanged, totals ────────────────────────────────────
class KocAndTotalsTests(SpinTestBase):
    def test_koc_hides_workflow_rows_before_the_cutoff_too(self):
        self.assertEqual(self.get(f"{K2D}/leads", page_size=100)["items"], [])   # 406 (history): hidden
        self.get(f"{K2D}/leads/406", status=404)
        self.assertNotIn("406", self.spin_items())

    def test_list_stats_export_and_dashboard_agree(self):
        self.import_rows([{"phone": f"984508300{n}", "name": f"History {n}", "created_at": f"0{n}-09-2026 10:00"}
                          for n in range(1, 4)])
        total = 2 + 3                                                        # live workflow + uploaded history
        self.assertEqual(len(self.spin_items()), total)
        self.assertEqual(self.get("/spin-leads/stats")["total"], total)
        self.assertEqual(len(self.export()) - 1, total)
        self.assertEqual(self.get("/analytics/overview", period="all")["totals"]["spin"], total)
        with mock.patch.object(vendors_ep, "engine", self.engine):         # vendor statistics: own session
            stats = self.get("/vendors/stats")
        # Per vendor, a lead counts once for each of its vendors: 404 (a gift lead) for Phodeo and Kraftyz
        self.assertEqual(sum(v["by_source"].get("spin", 0) for v in stats["vendors"]) + stats["unassigned"], total + 1)


# ── Re-key after the history upload ────────────────────────────────────
class RekeyTests(SpinTestBase):
    def seed(self):
        """Hidden leads 401 (meta + hot + activity + notification) and 402 (meta); an upload of 401's
        phone that already has an Update; 406 has data but no uploaded copy."""
        rows = self.import_rows([{"phone": "+91 98450 80001", "name": "Copy of 401", "assigned_to": "Automatic"},
                                 {"phone": "9845080002", "name": "Copy of 402", "assigned_to": "Unassigned"}])
        self.copy401, self.copy402 = rows[0]["lead_key"], rows[1]["lead_key"]
        admin = self.users["admin"].id
        when = datetime(2026, 11, 15, tzinfo=timezone.utc)
        with Session(self.engine) as s:
            s.add(LeadMeta(source="spin", lead_key="401", vendor_id=self.vendor_ids["phodeo"],
                           update_status="converted", quoted_amount=50000, notes="old", updated_at=when))
            s.add(LeadMeta(source="spin", lead_key="402", vendor_id=self.vendor_ids["rj"], quoted_amount=9000,
                           updated_at=when))
            s.add(LeadMeta(source="spin", lead_key=self.copy401, update_status="interested", updated_at=when))
            s.add(LeadMeta(source="spin", lead_key="406", update_status="interested", updated_at=when))
            s.add(CrmHotLead(source="spin", lead_key="401", hot_marked_by=admin, hot_marked_at=when))
            s.add(LeadActivity(source="spin", lead_key="401", action="field_changed", actor_name="A",
                               actor_role="admin", created_at=when))
            s.add(CrmNotification(recipient_user_id=admin, type="x", title="t", body="b", source="spin",
                                  lead_key="401", actor_user_id=admin, actor_name="A", created_at=when))
            s.add(LeadMeta(source="spin", lead_key="403", update_status="interested", updated_at=when))   # live
            s.commit()

    def workflow(self):
        return {str(r["id"]): (r["phone"], datetime.fromisoformat(r["created_at"])) for r in ROWS}

    def test_preview_then_apply_and_undo_sql(self):
        self.seed()
        with Session(self.engine) as s:
            plan = rekey.plan(s, self.workflow(), sl.spin_live_from_utc())
            moves = {m.old_key: m for m in plan.moves}
            self.assertEqual(set(moves), {"401", "402"})
            m401 = moves["401"]
            self.assertEqual((m401.new_key, m401.meta, m401.hot, m401.activity, m401.notifications),
                             (self.copy401, "merge", "move", 1, 1))
            self.assertEqual(m401.merged, {"vendor_id": self.vendor_ids["phodeo"], "quoted_amount": 50000,
                                           "notes": "old"})
            self.assertEqual(m401.kept, {"update_status": ("interested", "converted")})    # the upload wins
            self.assertEqual(moves["402"].merged, {"quoted_amount": 9000})                  # Unassigned kept
            self.assertEqual(moves["402"].kept, {"vendor_id": (None, self.vendor_ids["rj"])})
            self.assertEqual(plan.no_match, [("406", "9845080006")])
            self.assertEqual(plan.live, ["403"])
            self.assertTrue(any("keep upload update_status=interested (old converted)" in line
                                for line in rekey.report(plan)))
            self.assertEqual(s.exec(select(LeadMeta).where(LeadMeta.lead_key == "401")).first().lead_key, "401")
            undo = rekey.apply(s, plan)
            s.commit()
        with Session(self.engine) as s:
            new = s.exec(select(LeadMeta).where(LeadMeta.lead_key == self.copy401)).one()
            self.assertEqual((new.vendor_id, new.update_status, new.quoted_amount, new.notes),
                             (self.vendor_ids["phodeo"], "interested", 50000, "old"))
            self.assertIsNone(s.exec(select(LeadMeta).where(LeadMeta.lead_key == "401")).first())
            self.assertEqual(s.exec(select(CrmHotLead.lead_key)).all(), [self.copy401])
            self.assertEqual(s.exec(select(LeadActivity.lead_key).where(LeadActivity.action == "field_changed")).all(),
                             [self.copy401])
            self.assertEqual(s.exec(select(CrmNotification.lead_key)).all(), [self.copy401])
            self.assertIsNotNone(s.exec(select(LeadMeta).where(LeadMeta.lead_key == "406")).first())   # untouched
        self.assertTrue(any(u.startswith("INSERT INTO crm_lead_meta") and "'401'" in u for u in undo))
        self.assertTrue(any(u.startswith("UPDATE crm_hot_leads SET lead_key = '401'") for u in undo))
        # The moved data shows on the uploaded lead
        item = self.spin_items()[self.copy401]
        self.assertEqual((self.vendor_of(item), item["is_hot"]), (("Phodeo", "manual"), True))

    def test_ambiguous_and_changed_data_are_refused(self):
        self.seed()
        self.import_rows([{"phone": "9845080002", "name": "Second copy of 402"}])   # duplicate: refused
        with Session(self.engine) as s:                                            # so add one directly
            s.add(CrmSpinLead(origin="imported", phone="9845080002", phone_e164="+919845080002",
                              created_at=NOW, updated_at=NOW, additional_details={}))
            s.commit()
        with Session(self.engine) as s:
            plan = rekey.plan(s, self.workflow(), sl.spin_live_from_utc())
            self.assertEqual([m.old_key for m in plan.moves], ["401"])
            self.assertEqual(plan.ambiguous[0][0], "402")
            hot = s.exec(select(CrmHotLead).where(CrmHotLead.lead_key == "401")).one()
            s.delete(hot)                                                           # changed since the preview
            s.flush()
            with self.assertRaises(ValueError):
                rekey.apply(s, plan)
            s.rollback()
        with Session(self.engine) as s:                                             # nothing moved
            self.assertIsNotNone(s.exec(select(LeadMeta).where(LeadMeta.lead_key == "401")).first())
            self.assertIsNotNone(s.exec(select(CrmHotLead).where(CrmHotLead.lead_key == "401")).first())


if __name__ == "__main__":
    unittest.main()
