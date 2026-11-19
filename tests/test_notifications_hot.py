"""Notification and hot lead API coverage using the in-memory SQLite/mock workflow harness."""
import unittest
from unittest import mock

from sqlmodel import Session, select

from app.models.hot_lead import CrmHotLead
from app.services import notifications
from tests.test_crm_leads_api import API, ApiTestBase, SAMPLE, SAMPLE_VENDOR


class NotificationHotApiTests(ApiTestBase, unittest.TestCase):
    def _items(self, who):
        with self.logged_in(who):
            response = self.client.get(f"{API}/notifications", params={"page_size": 100})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def test_assignment_notifications_only_for_new_vendor_and_per_login_read_state(self):
        key = "101"  # fake read-only workflow row
        vendor_id = self.vendor_ids["phodeo"]
        self.assertEqual(self.put_crm("admin", "spin", key, {"vendor_id": vendor_id}).status_code, 200)
        self.assertEqual(len(self._items("phodeo")), 1)
        for changes in ({"notes": "ordinary save"}, {"vendor_id": vendor_id}, {"vendor_id": None}):
            self.assertEqual(self.put_crm("admin", "spin", key, changes).status_code, 200)
        self.assertEqual(len(self._items("phodeo")), 1)
        self.assertEqual(self.put_crm("admin", "spin", key, {"vendor_id": self.vendor_ids["rj"]}).status_code, 200)
        self.assertEqual(len(self._items("rj")), 1)
        items = self._items("phodeo")
        notification_id = items[0]["id"]
        with self.logged_in("phodeo"):
            self.assertEqual(self.client.get(f"{API}/notifications/unread-count").json()["unread_count"], 1)
            self.assertEqual(self.client.post(f"{API}/notifications/{notification_id}/read").status_code, 200)
            self.assertEqual(self.client.get(f"{API}/notifications/unread-count").json()["unread_count"], 0)
        with Session(self.engine, expire_on_commit=False) as db:
            from app.models.user import User
            from app.models.vendor_user import VendorUser
            second = User(name="Phodeo second", email="phodeo-second@example.com", role="vendor")
            db.add(second)
            db.commit()
            db.refresh(second)
            db.add(VendorUser(user_id=second.id, vendor_id=vendor_id))
            db.commit()
            self.users["phodeo_second"] = second
        self.assertEqual(len(self._items("phodeo_second")), 1)
        with self.logged_in("phodeo_second"):
            self.assertEqual(self.client.get(f"{API}/notifications/unread-count").json()["unread_count"], 1)
            marked = self.client.post(f"{API}/notifications/read-all")
            self.assertEqual(marked.json()["marked_read"], 1)
            self.assertEqual(self.client.get(f"{API}/notifications/unread-count").json()["unread_count"], 0)

    def test_failed_notification_does_not_fail_assignment(self):
        with mock.patch.object(notifications, "notify_vendor_assigned", side_effect=RuntimeError("offline")):
            response = self.put_crm("admin", "spin", "101", {"vendor_id": self.vendor_ids["phodeo"]})
        self.assertEqual(response.status_code, 200, response.text)

    def test_hot_filter_toggle_and_export_for_each_source_including_workflow_spin(self):
        with self.logged_in("admin"):
            self.assertEqual(self.client.post(f"{API}/leads/spin/101/hot").status_code, 200)
        for source, values in SAMPLE.items():
            lead = self.add_ok(source, values)
            with self.logged_in("admin"):
                response = self.client.post(f"{API}/leads/{source}/{lead['lead_key']}/hot")
            self.assertEqual(response.status_code, 200, response.text)
            hot = self.list_items(source, hot="yes")
            self.assertTrue(hot and all(row["is_hot"] for row in hot))
            self.assertTrue(all(not row["is_hot"] for row in self.list_items(source, hot="no")))
            self.assertIn("Hot", self.export_rows(source)[0])
            with self.logged_in(SAMPLE_VENDOR[source]):
                response = self.client.delete(f"{API}/leads/{source}/{lead['lead_key']}/hot")
            self.assertEqual(response.status_code, 200, response.text)
        spin = self.list_items("spin", hot="yes")
        self.assertTrue(any(str(item["id"]) == "101" for item in spin))
        with Session(self.engine) as db:
            self.assertEqual(len(db.exec(select(CrmHotLead)).all()), 1)  # four source flags were cleared by vendors

    def test_hot_toggle_respects_vendor_scope(self):
        lead = self.add_ok("wedding", SAMPLE["wedding"])
        with self.logged_in("rj"):
            response = self.client.post(f"{API}/leads/wedding/{lead['lead_key']}/hot")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
