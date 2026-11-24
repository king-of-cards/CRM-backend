"""Several vendors per KOC -> Vendors lead: each vendor's own copy (Update, amounts, follow-up, notes,
hot flag, activity), the overall view, adding / removing vendors, the upload's "Assigned to" with
several vendors, exports, and the one-time migration (scripts/migrate_lead_vendors.py) with the
Kraftyz sheet. In-memory SQLite and the fake workflow database only. Run:

    python -m unittest tests.test_multi_vendor -v
"""
import csv
import io
import unittest

from openpyxl import Workbook
from sqlmodel import Session, select

from tests.test_crm_leads_api import API, NOW
from tests.test_spin_history_upload import SpinTestBase

from app.core import response_cache
from app.models.hot_lead import CrmHotLead
from app.models.lead_assignment import LeadAssignment
from app.models.lead_meta import LeadMeta
from app.models.notification import CrmNotification
from app.models.vendor import Vendor
from app.services import access, lead_crm
from app.services import lead_vendor_migration as migration

# Spin fixture (tests/test_spin_history_upload.ROWS): 403 = Phodeo (area rules); 404 = a gift lead
# received after WEDDING_GIFTS_FROM: Phodeo + Kraftyz (partner)


class MultiVendorBase(SpinTestBase):
    def put(self, who, key, body, vendor=None, status=200, source="spin"):
        with self.logged_in(who):
            resp = self.client.put(f"{API}/leads/{source}/{key}/crm", json=body,
                                   params={"vendor": vendor} if vendor is not None else {})
        self.assertEqual(resp.status_code, status, resp.text)
        return resp.json()

    def call(self, method, path, who="admin", status=200, **params):
        with self.logged_in(who):
            resp = self.client.request(method, f"{API}{path}", params=params)
        self.assertEqual(resp.status_code, status, f"{path}: {resp.text}")
        return resp.json()

    def names(self, crm) -> list[str]:
        return [e["vendor"]["name"] for e in crm["vendors"]]


class CopiesTests(MultiVendorBase):
    def test_each_vendor_works_its_own_copy(self):
        k, p = self.vendor_ids["kraftyz"], self.vendor_ids["phodeo"]
        self.put("phodeo", "404", {"update_status": "not_interested", "quoted_amount": 1000, "notes": "Phodeo notes"})
        self.put("kraftyz", "404", {"update_status": "interested", "quoted_amount": 500,
                                    "follow_up_date": "2026-12-01", "notes": "Kraftyz notes"})
        # A vendor login: only its own copy, no other vendor
        mine = self.get("/leads/spin/404/crm", "kraftyz")
        self.assertEqual((mine["update_status"], mine["quoted_amount"], mine["notes"]), ("interested", 500, "Kraftyz notes"))
        self.assertEqual(self.names(mine), ["Kraftyz"])
        listed = self.spin_items("phodeo")["404"]["crm"]
        self.assertEqual((listed["update_status"], listed["quoted_amount"], listed["follow_up_date"]),
                         ("not_interested", 1000, None))
        self.assertEqual(self.names(listed), ["Phodeo"])
        # Admins: overall (best Update, amounts summed) and every copy
        crm = self.get("/leads/spin/404/crm")
        self.assertEqual((crm["update_status"], crm["quoted_amount"], crm["follow_up_date"]), ("interested", 1500, "2026-12-01"))
        by_vendor = {e["vendor"]["id"]: e for e in crm["vendors"]}
        self.assertEqual(by_vendor[p]["update_status"], "not_interested")
        self.assertEqual(by_vendor[k]["notes"], "Kraftyz notes")
        # A vendor can't touch a lead it doesn't have
        self.put("kraftyz", "403", {"update_status": "converted"}, status=404)
        # Admins must say whose copy on a lead with several vendors
        self.put("admin", "404", {"update_status": "converted"}, status=422)
        self.put("admin", "404", {"update_status": "converted"}, vendor=k)
        self.put("admin", "404", {"update_status": "converted"}, vendor=self.vendor_ids["rj"], status=422)
        self.assertEqual(self.get("/leads/spin/404/crm", "phodeo")["update_status"], "not_interested")
        self.assertEqual(self.get("/leads/spin/404/crm", "kraftyz")["update_status"], "converted")
        # Lead with one vendor: no ?vendor= needed
        self.put("admin", "403", {"update_status": "will_call_back"})
        self.assertEqual(self.get("/leads/spin/403/crm", "phodeo")["update_status"], "will_call_back")

    def test_best_status_order(self):
        order = ["converted", "purchased_with_us", "interested", "will_call_back", "not_converted",
                 "purchased_elsewhere", "not_interested"]
        for i, better in enumerate(order):
            for worse in order[i + 1:] + [None]:
                self.assertEqual(lead_crm.best_status([worse, better]), better)
        self.assertIsNone(lead_crm.best_status([None, None]))

    def test_kpis_count_a_lead_once_and_vendors_their_copy(self):
        k = self.vendor_ids["kraftyz"]
        self.put("phodeo", "404", {"update_status": "converted", "conversion_amount": 100})
        self.put("kraftyz", "404", {"update_status": "converted", "conversion_amount": 50})
        overall = self.get("/spin-leads/stats")["kpis"]
        self.assertEqual((overall["converted"], overall["conversion_total"]), (1, 150))
        mine = self.get("/spin-leads/stats", "kraftyz")["kpis"]
        self.assertEqual((mine["converted"], mine["conversion_total"]), (1, 50))
        # "Assigned to" filter: any of the lead's vendors
        self.assertEqual(set(self.spin_items(vendor_id=k)), {"404"})
        self.assertEqual(set(self.spin_items(vendor_id=self.vendor_ids["phodeo"])), {"403", "404"})
        # Update filter for a vendor login: its own copy
        self.put("kraftyz", "404", {"update_status": "not_interested"})
        self.assertEqual(set(self.spin_items("kraftyz", update_status="converted")), set())
        self.assertEqual(set(self.spin_items(update_status="converted")), {"404"})       # overall: Phodeo's

    def test_hot_flag_and_activity_per_copy(self):
        k = self.vendor_ids["kraftyz"]
        self.call("POST", "/leads/spin/404/hot", "kraftyz")
        self.assertTrue(self.spin_items("kraftyz")["404"]["is_hot"])
        self.assertFalse(self.spin_items("phodeo")["404"]["is_hot"])
        self.assertTrue(self.spin_items()["404"]["is_hot"])                              # admins: any copy
        with Session(self.engine) as s:
            self.assertEqual([h.vendor_id for h in s.exec(select(CrmHotLead))], [k])
        self.call("POST", "/leads/spin/404/hot", status=422)                             # admins: whose copy?
        self.call("POST", "/leads/spin/404/hot", vendor=self.vendor_ids["phodeo"])
        self.assertTrue(self.spin_items("phodeo")["404"]["is_hot"])
        # Activity: vendors see their own copy's entries (and the lead's), admins all
        self.put("phodeo", "404", {"notes": "P"})
        self.put("kraftyz", "404", {"notes": "K"})
        mine = self.get("/leads/spin/404/activity", "kraftyz")["items"]
        self.assertEqual({i["vendor_id"] for i in mine}, {k})
        everything = self.get("/leads/spin/404/activity")["items"]
        self.assertEqual({i["vendor_id"] for i in everything}, {k, self.vendor_ids["phodeo"]})

    def test_reminders_one_per_copy(self):
        self.put("phodeo", "404", {"follow_up_date": "2026-11-01"})          # overdue: always listed
        self.put("kraftyz", "404", {"follow_up_date": "2026-11-02"})
        def rows(who):
            body = self.get("/reminders/follow-ups", who, sources="spin")
            return sorted((i.get("vendor_id"), i["follow_up_date"][:10]) for i in body["items"])
        self.assertEqual(rows("kraftyz"), [(self.vendor_ids["kraftyz"], "2026-11-02")])
        self.assertEqual(len(rows("admin")), 2)


class AssignTests(MultiVendorBase):
    def test_add_and_remove_vendors(self):
        rj, p, k = self.vendor_ids["rj"], self.vendor_ids["phodeo"], self.vendor_ids["kraftyz"]
        crm = self.call("POST", f"/leads/spin/403/vendors/{rj}")
        self.assertEqual([(e["vendor"]["name"], e["vendor_source"]) for e in crm["vendors"]],
                         [("Phodeo", "manual"), ("RJ", "manual")])                       # the area vendor kept
        self.assertEqual(set(self.spin_items("rj")), {"403"})
        with Session(self.engine) as s:
            note = s.exec(select(CrmNotification).where(CrmNotification.recipient_vendor_id == rj)).all()
        self.assertEqual(len(note), 1)
        self.call("DELETE", f"/leads/spin/403/vendors/{p}")
        self.assertEqual(self.names(self.get("/leads/spin/403/crm")), ["RJ"])
        self.assertEqual(set(self.spin_items("phodeo")), {"404"})
        # Removing the partner sticks: no rule gives it back
        self.call("DELETE", f"/leads/spin/404/vendors/{k}")
        response_cache.clear()
        self.assertEqual(self.names(self.get("/leads/spin/404/crm")), ["Phodeo"])
        self.assertEqual(set(self.spin_items("kraftyz")), set())
        self.call("DELETE", f"/leads/spin/404/vendors/{k}", status=404)                 # not its vendor any more
        # Only admins; only active vendors
        self.call("POST", f"/leads/spin/404/vendors/{rj}", "phodeo", status=403)
        with Session(self.engine) as s:
            ohp = s.get(Vendor, self.vendor_ids["ohp"])
            ohp.is_active = False
            s.add(ohp)
            s.commit()
        self.call("POST", f"/leads/spin/404/vendors/{self.vendor_ids['ohp']}", status=422)

    def test_manual_replaces_automatic_partner_still_adds(self):
        rj, k = self.vendor_ids["rj"], self.vendor_ids["kraftyz"]
        crm = self.put("admin", "404", {"vendor_ids": [rj, self.vendor_ids["ohp"]]})
        self.assertEqual([(e["vendor"]["name"], e["vendor_source"]) for e in crm["vendors"]],
                         [("RJ", "manual"), ("OHP", "manual"), ("Kraftyz", "partner")])
        crm = self.put("admin", "404", {"vendor_ids": []})                                # back to automatic
        self.assertEqual(self.names(crm), ["Phodeo", "Kraftyz"])
        self.put("admin", "404", {"vendor_ids": [rj], "vendor_id": rj}, status=422)
        self.put("admin", "404", {"vendor_ids": [rj]}, status=200)
        self.put("phodeo", "403", {"vendor_ids": [rj]}, status=403)
        self.assertIn(k, [e["vendor"]["id"] for e in self.get("/leads/spin/404/crm")["vendors"]])

    def test_adding_a_vendor_ends_unassigned(self):
        rj = self.vendor_ids["rj"]
        self.put("admin", "403", {"assigned_to": "unassigned"})
        self.assertEqual(self.get("/leads/spin/403/crm")["vendors"], [])
        crm = self.call("POST", f"/leads/spin/403/vendors/{rj}")
        self.assertEqual(self.names(crm), ["RJ"])
        self.assertTrue(crm["auto_assign"])

    def test_upload_with_several_vendors(self):
        rows = self.import_rows([
            {"name": "Two vendors", "phone": "9845090001", "assigned_to": "RJ, Kraftyz", "update_status": "Interested",
             "pincode": "560011"},
            {"name": "Auto", "phone": "9845090002", "assigned_to": "Phodeo · auto, Kraftyz · auto",
             "update_status": "Interested", "pincode": "560011"},
            {"name": "Unknown", "phone": "9845090003", "assigned_to": "RJ, Nobody"},
        ])
        self.assertEqual([r["status"] for r in rows], ["imported", "imported", "error"])
        crm = self.get(f"/leads/spin/{rows[0]['lead_key']}/crm")
        self.assertEqual([(e["vendor"]["name"], e["vendor_source"], e["update_status"]) for e in crm["vendors"]],
                         [("RJ", "manual", "interested"), ("Kraftyz", "manual", None)])
        # "· auto" only: the automatic rules; the Update lands on the automatic vendor's copy
        crm = self.get(f"/leads/spin/{rows[1]['lead_key']}/crm", "phodeo")
        self.assertEqual((self.names(crm), crm["update_status"]), (["Phodeo"], "interested"))

    def test_gift_upload_before_the_start_gets_no_kraftyz(self):
        gift = "Photography & Videography, Wedding Gifts & Return Gifts"
        rows = self.import_rows([
            {"name": "Old gift", "phone": "9845090011", "assigned_to": "Automatic", "otherWeddingServices": gift,
             "pincode": "560011", "created_at": "20 Nov 2026, 11:00 pm"},
            {"name": "New gift", "phone": "9845090012", "assigned_to": "Automatic", "otherWeddingServices": gift,
             "pincode": "560011", "created_at": "21 Nov 2026, 12:00 am"},
        ])
        self.assertEqual([r["status"] for r in rows], ["imported", "imported"], rows)
        self.assertEqual(self.names(self.get(f"/leads/spin/{rows[0]['lead_key']}/crm")), ["Phodeo"])
        self.assertEqual(self.names(self.get(f"/leads/spin/{rows[1]['lead_key']}/crm")), ["Phodeo", "Kraftyz"])

    def test_export_admins_every_vendor_vendors_their_copy(self):
        self.put("phodeo", "404", {"update_status": "not_interested"})
        self.put("kraftyz", "404", {"update_status": "interested"})

        def export(who):
            with self.logged_in(who):
                resp = self.client.get(f"{API}/crm-leads/spin/export", params={"format": "csv"})
            self.assertEqual(resp.status_code, 200, resp.text)
            out = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
            return out[0], {r[out[0].index("Lead")]: r for r in out[1:]}
        head, rows = export("admin")
        row = rows["Lead 404"]
        self.assertEqual(row[head.index("Assigned to")], "Phodeo · auto, Kraftyz · auto")
        self.assertEqual(row[head.index("Update")], "Interested")
        self.assertEqual(row[head.index("Vendor updates")], "Phodeo: Not interested; Kraftyz: Interested")
        head, rows = export("phodeo")
        self.assertNotIn("Vendor updates", head)
        self.assertEqual(rows["Lead 404"][head.index("Update")], "Not interested")


# ── The one-time migration ─────────────────────────────────────────────
def sheet(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Relation", "Timestamp", "Name", "Phone", "Branch", "occasion_date", "occasion", "Updates",
               "Additional comments"])
    for name, phone, update, comments in rows:
        ws.append(["Self", "2026-11-01", name, phone, "Jayanagar", "2026-12-20", "Wedding", update, comments])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class MigrationTests(MultiVendorBase):
    def test_copies_and_kraftyz_sheet(self):
        rj, p, k = self.vendor_ids["rj"], self.vendor_ids["phodeo"], self.vendor_ids["kraftyz"]
        with Session(self.engine) as s:
            # Before the migration: one row per lead (copy 0); 403 manual RJ, 404 automatic (Phodeo)
            s.add(LeadMeta(source="spin", lead_key="403", vendor_id=rj, update_status="interested", updated_at=NOW))
            s.add(LeadMeta(source="spin", lead_key="404", update_status="will_call_back", updated_at=NOW))
            s.add(CrmHotLead(source="spin", lead_key="404", hot_marked_by=self.admin.id, hot_marked_at=NOW))
            s.commit()
        rows = migration.read_sheet(sheet([
            ("A", "+91 98450 80003", "Catalogue shared", "Sent on WhatsApp"),     # 403: gains Kraftyz
            ("B", "9845080004", "Not interested", None),                          # 404: already Kraftyz
            ("Testing", "9999999990", "Will get back", None),                      # skipped
            ("C", "9000000001", "", None),                                         # unmatched
        ]))
        self.assertEqual(migration.map_update("Didn't pickup call"), ("will_call_back", True))
        self.assertEqual(migration.map_update("  catalogue TO be shared "), ("interested", True))
        self.assertEqual(migration.map_update(None), (None, True))
        self.assertEqual(migration.map_update("Call in Jan"), (None, False))
        with Session(self.engine) as s:
            copies = migration.plan_copies(s)
            kraftyz = migration.plan_kraftyz(s, rows)
            self.assertEqual(sorted((m["table"], m["lead_key"], m["to"]) for m in copies.moves),
                             [("crm_hot_leads", "404", p), ("crm_lead_meta", "403", rj), ("crm_lead_meta", "404", p)])
            self.assertEqual([(a["key"], a["current"], a["update_status"]) for a in kraftyz.adds],
                             [("403", ["RJ"], "interested")])
            self.assertEqual([a["key"] for a in kraftyz.already], ["404"])
            self.assertEqual([r.phone for r in kraftyz.unmatched], ["9000000001"])
            self.assertEqual(len(kraftyz.skipped), 1)
            undo = migration.apply(s, copies, kraftyz)
            s.commit()
        access.forget()
        self.assertTrue(any(u.startswith("DELETE FROM crm_lead_assignments") for u in undo))
        self.assertTrue(any(u.startswith("UPDATE crm_lead_meta") for u in undo))
        crm = self.get("/leads/spin/403/crm")
        by_name = {e["vendor"]["name"]: e for e in crm["vendors"]}
        self.assertEqual(list(by_name), ["RJ", "Kraftyz"])
        self.assertEqual(by_name["RJ"]["update_status"], "interested")
        self.assertEqual((by_name["Kraftyz"]["update_status"], by_name["Kraftyz"]["notes"]),
                         ("interested", "Sent on WhatsApp"))
        self.assertEqual(self.get("/leads/spin/404/crm", "phodeo")["update_status"], "will_call_back")
        self.assertTrue(self.spin_items("phodeo")["404"]["is_hot"])
        self.assertFalse(self.spin_items("kraftyz")["404"]["is_hot"])
        with Session(self.engine) as s:
            self.assertIsNone(s.exec(select(LeadMeta).where(LeadMeta.lead_key == "403", LeadMeta.copy_vendor_id == rj))
                              .one().vendor_id)                                       # legacy column cleared
            self.assertEqual(sorted((a.vendor_id, a.state) for a in s.exec(select(LeadAssignment)
                                                                          .where(LeadAssignment.lead_key == "403"))),
                             sorted([(rj, "added"), (k, "added")]))
            # Run again: nothing left to move, Kraftyz already there
            again = migration.plan_copies(s)
            self.assertEqual(again.moves, [])
            self.assertEqual(migration.plan_kraftyz(s, rows).adds, [])

    def test_unknown_update_is_pending_with_original_text(self):
        rows = migration.read_sheet(sheet([("A", "9845080003", "Call in Jan", "Busy")]))
        with Session(self.engine) as s:
            plan = migration.plan_kraftyz(s, rows)
            self.assertEqual(plan.unknown_updates, {"Call in Jan": 1})
            migration.apply(s, migration.plan_copies(s), plan)
            s.commit()
        access.forget()
        entry = next(e for e in self.get("/leads/spin/403/crm")["vendors"] if e["vendor"]["name"] == "Kraftyz")
        self.assertEqual((entry["update_status"], entry["notes"]), (None, "Busy\nOriginal update: Call in Jan"))


if __name__ == "__main__":
    unittest.main()
