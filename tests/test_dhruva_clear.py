"""scripts/clear_dhruva_to_koc.py (app/services/dhruva_clear.py): every Dhruva -> KOC lead goes, with its
event dates, activity, hot stars, notifications (and reads) and CRM details; nothing of other sections.
The undo SQL restores every row. In-memory SQLite only. Run:

    python -m unittest tests.test_dhruva_clear -v
"""
import unittest
from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel import Session, select

from app.models.dhruva import DhruvaEventDate, DhruvaLead, DhruvaPhone
from app.models.hot_lead import CrmHotLead
from app.models.lead_activity import LeadActivity
from app.models.lead_meta import LeadMeta
from app.models.notification import CrmNotification, CrmNotificationRead
from app.services import dhruva_clear
from tests.dhruva_fixtures import DhruvaTestBase

T = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)


class ClearTests(DhruvaTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.upload_all()["added"], 6)                    # by the admin
        self.assertEqual(self.add(name="By Dhruva", phones=["9845012399"], event_dates=["2026-12-04"]).status_code, 201)
        lead_id = self.add(name="Archived", phones=["9845012398"]).json()["id"]
        self.call("admin", "POST", f"/leads/{lead_id}/archive")
        with Session(self.engine) as s:
            first = s.exec(select(DhruvaLead).where(DhruvaLead.name == "Asha Fake")).one()
            s.add(CrmHotLead(source="dhruva", lead_key=str(first.id), vendor_id=0, hot_marked_by=1, hot_marked_at=T))
            note = CrmNotification(recipient_user_id=self.users["admin"].id, type="lead", title="t", body="b",
                                   source="dhruva", lead_key=str(first.id), actor_user_id=1, actor_name="A",
                                   created_at=T)
            s.add(note)
            s.add(LeadMeta(source="dhruva", lead_key=str(first.id), notes="x", updated_at=T))
            # Other sections: never touched
            s.add(LeadActivity(source="spin", lead_key="1", action="created", actor_name="A", actor_role="admin",
                               created_at=T))
            s.add(CrmHotLead(source="spin", lead_key="1", vendor_id=0, hot_marked_by=1, hot_marked_at=T))
            s.commit()
            s.add(CrmNotificationRead(notification_id=note.id, user_id=self.users["admin"].id, read_at=T))
            s.commit()

    def counts(self, s) -> dict:
        return {
            "leads": len(s.exec(select(DhruvaLead)).all()),
            "dates": len(s.exec(select(DhruvaEventDate)).all()),
            "phones": len(s.exec(select(DhruvaPhone)).all()),
            "activity": len(s.exec(select(LeadActivity).where(LeadActivity.source == "dhruva")).all()),
            "hot": len(s.exec(select(CrmHotLead).where(CrmHotLead.source == "dhruva")).all()),
            "notes": len(s.exec(select(CrmNotification).where(CrmNotification.source == "dhruva")).all()),
            "reads": len(s.exec(select(CrmNotificationRead)).all()),
            "meta": len(s.exec(select(LeadMeta).where(LeadMeta.source == "dhruva")).all()),
            "other": len(s.exec(select(LeadActivity).where(LeadActivity.source == "spin")).all())
                     + len(s.exec(select(CrmHotLead).where(CrmHotLead.source == "spin")).all()),
        }

    def test_preview_apply_and_undo(self):
        with Session(self.engine) as s:
            plan = dhruva_clear.plan(s)
            before = self.counts(s)
        self.assertEqual(plan.leads, 8)
        self.assertEqual([(g["name"], g["leads"], g["archived"]) for g in plan.by_creator],
                         [("Admin", 6, 0), ("Dhruva", 2, 1)])
        self.assertTrue(all(g["earliest"] and g["latest"] for g in plan.by_creator))
        self.assertEqual(plan.related, {"crm_notification_reads": 1, "crm_notifications": 1,
                                        "crm_lead_activity": before["activity"], "crm_hot_leads": 1,
                                        "crm_lead_meta": 1, "crm_dhruva_event_dates": before["dates"],
                                        "crm_dhruva_phones": before["phones"]})
        self.assertEqual(before["phones"], 10)
        self.assertGreater(before["activity"], 8)                          # created + archived entries
        with Session(self.engine) as s:
            undo, removed = dhruva_clear.apply(s)
            s.commit()
        self.assertEqual(removed["crm_dhruva_leads"], 8)
        with Session(self.engine) as s:
            after = self.counts(s)
        self.assertEqual(after, {**{k: 0 for k in before}, "other": 2})
        self.assertEqual(self.call("dhruva", "GET", "/leads").json()["total"], 0)
        with Session(self.engine) as s:                                    # the undo SQL restores everything
            for statement in undo:
                s.exec(text(statement))
            s.commit()
            self.assertEqual(self.counts(s), before)
        self.assertEqual(self.call("dhruva", "GET", "/leads", params={"page_size": 100}).json()["total"], 7)


if __name__ == "__main__":
    unittest.main()
