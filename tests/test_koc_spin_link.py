"""KOC -> Dhruva and Spin: an upload row is matched with the Spin leads (uploaded and workflow rows in
spin) by the last 10 digits of the phone, only to show the link. A KOC -> Dhruva lead never creates a
Spin lead or runs vendor rules. Plus the cleanup of the Spin leads an older upload created
(scripts/remove_koc_created_spin_leads.py). In-memory SQLite and the fake workflow database only. Run:

    python -m unittest tests.test_koc_spin_link -v
"""
import io
import unittest
from datetime import datetime, timezone

from openpyxl import Workbook
from sqlalchemy import text
from sqlmodel import Session, select

from tests.test_crm_leads_api import API
from tests.test_spin_history_upload import GIFT, SpinTestBase

from app.models.crm_spin_lead import CrmSpinLead
from app.models.hot_lead import CrmHotLead
from app.models.koc_dhruva_lead import KocDhruvaLead
from app.models.lead_activity import LeadActivity
from app.models.lead_assignment import LeadAssignment
from app.models.lead_meta import LeadMeta
from app.models.lead_origin import ORIGIN_IMPORTED
from app.models.notification import CrmNotification
from app.api.v1.endpoints import spin_leads as sl
from app.services import koc_spin_cleanup as cleanup
from app.services import lead_activity
from app.services.crm_leads import service
from app.services.crm_leads.fields import clean_row

K2D = "/koc-to-dhruva"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Spin fixture (tests/test_spin_history_upload.ROWS): 401 history (not in spin); 403 live = Phodeo;
# 404 live gift = Phodeo + Kraftyz; 405 live but not listed (not in spin)


def line(name, phone, **extra):
    return {"name": name, "phone": phone, "occasionType": "Wedding", "occasionDate": "20-12-2026",
            "estimatedBudgetPerCard": "100-200", "quantityRequired": "300", "pincode": "560011",
            "otherWeddingServices": "Photography & Videography", **extra}


class KocSpinBase(SpinTestBase):
    def preview(self, rows: list[dict]) -> dict:
        """The preview's checks, through the bulk save's own check (no file needed)."""
        with Session(self.engine) as s:
            results, counts, _ = service.check_rows(s, "koc_to_dhruva", [(n + 2, v) for n, v in enumerate(rows)])
        return {"rows": {r["row"]: r for r in results}, "counts": counts}

    def save(self, rows: list[dict]) -> dict:
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}{K2D}/leads/import",
                                    json={"rows": [{"row": n + 2, "values": v} for n, v in enumerate(rows)]})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        return {"rows": {r["row"]: r for r in body["rows"]}, "counts": body["counts"]}

    def spin_count(self) -> int:
        with Session(self.engine) as s:
            return len(s.exec(select(CrmSpinLead)).all())

    def koc_keys(self) -> dict[str, str]:
        with Session(self.engine) as s:
            return {lead.name: lead.lead_key for lead in s.exec(select(KocDhruvaLead))}


class KocSpinLinkTests(KocSpinBase):
    def test_matched_rows_show_the_spin_vendors_and_leave_spin_alone(self):
        p, k = self.vendor_ids["phodeo"], self.vendor_ids["kraftyz"]
        with self.logged_in("phodeo"):              # Phodeo's own copy of 404, before the upload
            self.client.put(f"{API}/leads/spin/404/crm", json={"update_status": "interested", "notes": "mine"})
        rows = [line("Gift lead", "+91 98450 80004", estimatedBudgetPerCard="70-100"),   # 404: other format
                line("Area lead", "09845080003"),                                       # 403 (leading 0)
                line("Again", "9845080003", quantityRequired="1200")]                   # 403 again
        preview = self.preview(rows)
        r = preview["rows"]
        self.assertEqual([r[n]["spin"]["action"] for n in (2, 3, 4)], ["matched"] * 3)
        self.assertEqual([m["lead_key"] for m in r[2]["spin"]["leads"]], ["404"])
        self.assertEqual(sorted(v["id"] for v in r[2]["spin"]["leads"][0]["vendors"]), sorted([p, k]))
        self.assertEqual(r[2]["also_in"], [{"source": "spin", "count": 1, "vendors": ["Kraftyz", "Phodeo"]}])
        self.assertEqual(r[3]["also_in"], [{"source": "spin", "count": 1, "vendors": ["Phodeo"]}])
        self.assertEqual((preview["counts"]["spin_matched"], preview["counts"]["spin_not_in_spin"]), (3, 0))
        self.assertFalse({"spin_create", "spin_duplicates"} & set(preview["counts"]))

        saved = self.save(rows)
        self.assertEqual(saved["counts"]["imported"], 3)
        self.assertNotIn("spin_created", saved["counts"])
        self.assertEqual(saved["rows"][2]["spin"]["action"], "matched")
        self.assertEqual(self.spin_count(), 0)                                   # nothing new in Spin
        with Session(self.engine) as s:
            spin_meta = s.exec(select(LeadMeta).where(LeadMeta.source == "spin")).all()
            self.assertEqual(s.exec(select(LeadAssignment)).all(), [])
        self.assertEqual([(m.lead_key, m.copy_vendor_id, m.update_status, m.notes) for m in spin_meta],
                         [("404", p, "interested", "mine")])                     # Phodeo's copy untouched
        crm = self.get("/leads/spin/404/crm")
        self.assertEqual(sorted(e["vendor"]["name"] for e in crm["vendors"]), ["Kraftyz", "Phodeo"])
        # also_in both ways
        koc_item = {i["lead_key"]: i for i in self.get(f"{K2D}/leads", page_size=100)["items"]}
        self.assertIn("spin", {a["source"] for a in koc_item[self.koc_keys()["Gift lead"]]["also_in"]})
        self.assertIn("koc_to_dhruva", {a["source"] for a in self.spin_items()["404"]["also_in"]})

    def test_matched_with_an_uploaded_spin_lead(self):
        self.import_rows([{"name": "Uploaded", "phone": "9845081111", "assigned_to": "RJ"}])
        r = self.preview([line("KOC", "9845081111")])["rows"][2]
        self.assertEqual(r["spin"]["action"], "matched")
        lead = r["spin"]["leads"][0]
        self.assertTrue(lead["lead_key"].startswith("crm-"))
        self.assertEqual((lead["origin"], [v["name"] for v in lead["vendors"]]), ("imported", ["RJ"]))

    def test_not_in_spin_saves_only_the_koc_row(self):
        rows = [
            line("New gift", "9845082001", otherWeddingServices=GIFT, created_at="22-11-2026 10:00"),
            line("History", "9845080001", created_at="01-10-2026 10:00"),    # 401: before the cut-off, not Spin
            line("Unlisted", "9845080005"),                                   # 405: live but not in spin
            line("Unread", "12345"),                                          # bad phone: an error row
        ]
        preview = self.preview(rows)
        self.assertEqual({n: r["spin"]["action"] for n, r in preview["rows"].items()},
                         {2: "not_in_spin", 3: "not_in_spin", 4: "not_in_spin", 5: "not_in_spin"})
        self.assertEqual(preview["rows"][2]["also_in"], [])
        self.assertEqual((preview["counts"]["spin_matched"], preview["counts"]["spin_not_in_spin"]), (0, 4))
        saved = self.save(rows)
        self.assertEqual(saved["counts"]["imported"], 3)
        self.assertEqual(set(self.koc_keys()), {"New gift", "History", "Unlisted"})
        self.assertEqual(self.spin_count(), 0)                                   # never a Spin lead
        with Session(self.engine) as s:
            self.assertEqual(s.exec(select(LeadMeta).where(LeadMeta.source == "spin")).all(), [])
            self.assertEqual(s.exec(select(LeadActivity).where(LeadActivity.source == "spin")).all(), [])
            self.assertEqual(s.exec(select(CrmNotification)).all(), [])
        self.assertEqual({str(i["id"]) for i in self.get("/spin-leads", "kraftyz", page_size=100)["items"]}, {"404"})
        # The same file again: the KOC -> Dhruva duplicate rule still applies
        again = self.preview(rows)["counts"]
        self.assertEqual((again["ok"], again["duplicates"]), (0, 3))

    def test_no_single_add(self):
        with self.logged_in("admin"):
            add = self.client.post(f"{API}/crm-leads/koc_to_dhruva", json={"values": {"phone": "9845086001"}})
            direct = self.client.post(f"{API}{K2D}/leads", json={"phone": "9845086001"})
        self.assertEqual(add.status_code, 422)
        self.assertEqual(direct.status_code, 405)
        self.assertEqual(self.spin_count(), 0)

    def test_empty_and_na_budget(self):
        rows = [line("No budget", "9845084001", estimatedBudgetPerCard="", quantityRequired=""),
                line("NA budget", "9845084002", estimatedBudgetPerCard="N/A", quantityRequired="-",
                     pincode="N/A", occasionDate="NA")]
        self.assertEqual(self.save(rows)["counts"]["imported"], 2)
        keys = self.koc_keys()
        items = {i["lead_key"]: i for i in self.get(f"{K2D}/leads", page_size=100)["items"]}
        for name in ("No budget", "NA budget"):                 # listed whatever the 70 / 1000 rule says
            item = items[keys[name]]
            self.assertEqual((item["estimated_budget_per_card"], item["quantity_required"]), (None, None), name)

    def test_uploaded_rows_are_never_filtered_by_the_rule(self):
        import csv
        below = [("Range below", "40-70", "999"), ("Under", "UNDER_70", "999"), ("Below", "below 70", None),
                 ("Empty", "", ""), ("NA", "N/A", "-"), ("Small", "10-20", "100")]
        rows = [line(n, f"98450880{i:02d}", estimatedBudgetPerCard=b, quantityRequired=q)
                for i, (n, b, q) in enumerate(below)]
        rows.append(line("History", "9845088099", created_at="01-10-2026 10:00", estimatedBudgetPerCard="10-20"))
        self.assertEqual(self.save(rows)["counts"]["imported"], 7)
        keys = set(self.koc_keys().values())
        with Session(self.engine) as s:
            for lead in s.exec(select(KocDhruvaLead)):                   # none of them matches the rule
                self.assertIsNone(sl.koc_reason(lead.additional_details), lead.name)
        listed = {i["lead_key"] for i in self.get(f"{K2D}/leads", page_size=100)["items"]}
        self.assertTrue(keys <= listed)                                   # list
        self.assertEqual(self.get(f"{K2D}/leads/stats")["total"], len(listed))
        with self.logged_in("admin"):
            export = self.client.get(f"{API}{K2D}/leads/export", params={"format": "csv"})
        self.assertEqual(len(export.content.decode("utf-8-sig").strip().splitlines()) - 1, len(listed))
        for key in keys:                                                  # details, CRM, hot
            self.assertEqual(self.get(f"{K2D}/leads/{key}")["lead_key"], key)
            self.assertEqual(self.get(f"{K2D}/leads/{key}/crm")["lead_key"], key)
        self.assertGreaterEqual(self.get("/analytics/koc-to-dhruva", period="all")["kpis"]["shared"], len(keys))
        # Still there after the rule's caches are cleared and the list is read again
        from app.services import access
        access.forget()
        self.assertTrue(keys <= {i["lead_key"] for i in self.get(f"{K2D}/leads", page_size=100)["items"]})

    def test_source_and_relation(self):
        # Through the file preview: optional columns, matched by their names
        wb = Workbook()
        ws = wb.active
        ws.title = "Leads"
        ws.append(["Phone", "Name", "Source", "Relation"])
        ws.append(["9845085001", "With both", "Instagram", "Sister"])
        ws.append(["9845085002", "Without", None, None])
        ws.append(["9845085003", "NA ones", "N/A", "-"])
        buf = io.BytesIO()
        wb.save(buf)
        with self.logged_in("admin"):
            resp = self.client.post(f"{API}{K2D}/leads/import/preview", files={"file": ("k.xlsx", buf.getvalue(), XLSX)})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual((body["columns"]["Source"], body["columns"]["Relation"]), ("source", "occasion"))
        self.assertEqual([r["status"] for r in body["rows"]], ["ok"] * 3)              # never required
        self.assertEqual((body["rows"][0]["values"]["source"], body["rows"][0]["values"]["occasion"]),
                         ("Instagram", "Sister"))
        with self.logged_in("admin"):
            saved = self.client.post(f"{API}{K2D}/leads/import", json={"rows": [
                {"row": r["row"], "values": r["values"]} for r in body["rows"]]})
        self.assertEqual(saved.json()["counts"]["imported"], 3, saved.text)
        with Session(self.engine) as s:
            koc = {lead.name: (lead.source, lead.occasion) for lead in s.exec(select(KocDhruvaLead))}
        self.assertEqual(koc, {"With both": ("Instagram", "Sister"), "Without": ("crm", None),
                               "NA ones": ("crm", None)})
        self.assertEqual(self.spin_count(), 0)
        # Re-check: other Source / Relation = another lead; "crm" (no Source) never conflicts
        again = self.preview([
            line("With both", "9845085001", source="Facebook", occasion="Sister"),
            line("With both", "9845085001", source="Instagram", occasion="Brother"),
            {"name": "Without", "phone": "9845085002", "source": "Instagram", "occasion": "Self"},
        ])["rows"]
        self.assertEqual([again[n]["status"] for n in (2, 3)], ["ok", "ok"])
        self.assertEqual(again[4]["status"], "duplicate")
        too_long = self.preview([{"phone": "9845085009", "source": "x" * 51}])["rows"][2]
        self.assertEqual((too_long["status"], too_long["errors"][0]["field"]), ("error", "source"))


# ── Cleanup of the Spin leads an older upload created ──────────────────
class CleanupTests(KocSpinBase):
    def spin_lead(self, s, name: str, phone: str) -> CrmSpinLead:
        c = clean_row("spin", {"name": name, "phone": phone, "pincode": "560011"}, service.context(s), upload=True)
        lead = service.create(s, "spin", c, ORIGIN_IMPORTED, self.users["admin"].id)
        s.commit()
        return lead

    def setUp(self):
        super().setUp()
        people = [("Clean", "9845087001"), ("Worked", "9845087002"), ("Talked", "9845087003"),
                  ("Starred", "9845087004"), ("Assigned", "9845087005"), ("Lookalike", "9845087006")]
        self.save([line(n, ph) for n, ph in people])
        koc = self.koc_keys()
        p = self.vendor_ids["phodeo"]
        admin = self.users["admin"].id
        now = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
        with Session(self.engine, expire_on_commit=False) as s:
            # What the older upload did: the KOC -> Dhruva row also as a Spin lead, with its activity entry
            self.leads = {}
            for name, phone in people[:5]:
                lead = self.spin_lead(s, name, phone)
                lead_activity.log_action(s, source="spin", lead_key=lead.lead_key, action="created", field="lead",
                                         old=None, new=f"from KOC → Dhruva {koc[name]}", actor_user_id=admin,
                                         actor_name="Admin", actor_role="admin")
                self.leads[name] = lead
            # An unlinked look-alike (no entry): listed, never removed
            self.lookalike = self.spin_lead(s, "Lookalike", "9845087006").lead_key
            k = {n: lead.lead_key for n, lead in self.leads.items()}
            # Clean: a pending Phodeo copy is not "work"; plus a notification
            s.add(LeadMeta(source="spin", lead_key=k["Clean"], copy_vendor_id=p, updated_at=now))
            s.add(CrmNotification(recipient_vendor_id=p, type="vendor_assigned", title="New lead", body="x",
                                  source="spin", lead_key=k["Clean"], actor_user_id=admin,
                                  actor_name="Admin", vendor_id=p, created_at=now))
            s.add(LeadMeta(source="spin", lead_key=k["Worked"], copy_vendor_id=p, update_status="interested",
                           updated_at=now))
            s.add(CrmHotLead(source="spin", lead_key=k["Starred"], vendor_id=p, hot_marked_by=admin, hot_marked_at=now))
            s.add(LeadAssignment(source="spin", lead_key=k["Assigned"], vendor_id=p, state="added", updated_at=now))
            s.commit()
            lead_activity.log_action(s, source="spin", lead_key=k["Talked"], action="field_changed", field="notes",
                                     old=None, new="x", actor_user_id=admin, actor_name="phodeo", actor_role="vendor")
        # A normal Spin upload (activity "created", not from KOC -> Dhruva): never removed
        self.import_rows([{"name": "Normal", "phone": "9845087009"}])
        self.k = {n: lead.lead_key for n, lead in self.leads.items()}

    def snapshot(self, s) -> dict:
        keys = [self.k["Clean"], self.k["Assigned"]]
        out = {"spin": sorted((x.id, x.name, x.phone_e164) for x in s.exec(select(CrmSpinLead)))}
        for model in (LeadMeta, LeadAssignment, CrmHotLead, LeadActivity, CrmNotification):
            out[model.__name__] = sorted((x.id, x.lead_key) for x in s.exec(select(model).where(
                model.source == "spin", model.lead_key.in_(keys))))
        return out

    def test_preview_apply_and_undo(self):
        k = self.k
        with Session(self.engine) as s:
            the_plan = cleanup.plan(s)
        removed = {r["lead_key"]: r for r in the_plan.remove}
        skipped = {r["lead_key"]: r for r in the_plan.skipped}
        self.assertEqual(set(removed), {k["Clean"], k["Assigned"]})
        self.assertEqual(set(skipped), {k["Worked"], k["Talked"], k["Starred"]})
        self.assertIn("Phodeo: Update interested", skipped[k["Worked"]]["reasons"])
        self.assertEqual(skipped[k["Talked"]]["reasons"], ["activity: field_changed"])
        self.assertEqual(skipped[k["Starred"]]["reasons"], ["hot star"])
        self.assertEqual(removed[k["Clean"]]["vendors"], ["Phodeo"])               # the area rule
        self.assertEqual(removed[k["Assigned"]]["vendors"], ["Phodeo"])            # manual
        self.assertEqual(removed[k["Clean"]]["phone"], "9845087001")
        self.assertTrue(removed[k["Clean"]]["koc_key"].startswith("kd-"))
        self.assertEqual([u["lead_key"] for u in the_plan.unlinked], [self.lookalike])

        with Session(self.engine) as s:
            before = self.snapshot(s)
            koc_before = len(s.exec(select(KocDhruvaLead)).all())
            undo = cleanup.apply(s, cleanup.plan(s))
            s.commit()
        with Session(self.engine) as s:
            after = self.snapshot(s)
            names = {x.name for x in s.exec(select(CrmSpinLead))}
            self.assertEqual(len(s.exec(select(KocDhruvaLead)).all()), koc_before)        # KOC rows kept
        self.assertEqual(names, {"Worked", "Talked", "Starred", "Normal", "Lookalike"})
        for table in ("LeadMeta", "LeadAssignment", "LeadActivity", "CrmNotification"):
            self.assertEqual(after[table], [], table)
        self.assertTrue(before["LeadMeta"] and before["LeadAssignment"] and before["CrmNotification"]
                        and before["LeadActivity"])
        # The undo SQL restores every removed row
        with Session(self.engine) as s:
            for statement in undo:
                s.exec(text(statement))
            s.commit()
            self.assertEqual(self.snapshot(s), before)
            restored = s.get(CrmSpinLead, removed[k["Clean"]]["id"])
            self.assertEqual(restored.additional_details, self.leads["Clean"].additional_details)
        with Session(self.engine) as s:
            self.assertEqual({r["lead_key"] for r in cleanup.plan(s).remove}, {k["Clean"], k["Assigned"]})

    def test_apply_refuses_a_lead_worked_on_since_the_preview(self):
        with Session(self.engine) as s:
            the_plan = cleanup.plan(s)
            key = the_plan.remove[0]["lead_key"]
            s.add(LeadMeta(source="spin", lead_key=key, copy_vendor_id=0, notes="new",
                           updated_at=datetime(2026, 11, 23, tzinfo=timezone.utc)))
            s.flush()
            with self.assertRaises(ValueError):
                cleanup.apply(s, the_plan)
            s.rollback()


if __name__ == "__main__":
    unittest.main()
