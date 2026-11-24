"""Activity, follow-up and city behavior using the in-memory SQLite/mock workflow test harness."""
import unittest
from datetime import date
from unittest import mock

from sqlmodel import Session, select

from tests.test_crm_leads_api import API, ApiTestBase, SAMPLE
from app.api.v1.endpoints import analytics
from app.api.v1.endpoints import reminders, spin_leads as spin_ep
from app.models.user import User
from app.models.lead_activity import LeadActivity
from app.services import lead_activity
from app.services import periods


class LeadActivityReminderTests(ApiTestBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.today_patch = mock.patch.object(periods, "today_ist", lambda: date(2026, 11, 19))
        self.today_patch.start()

    def tearDown(self):
        self.today_patch.stop()
        super().tearDown()

    def test_crm_follow_up_field_filter_and_vendor_activity_hiding(self):
        lead = self.add_ok("wedding", {**SAMPLE["wedding"], "phone": "09845020003"})
        key = lead["lead_key"]
        self.assertEqual(self.put_crm("admin", "wedding", key,
            {"vendor_id": self.vendor_ids["phodeo"], "notes": "Call soon", "follow_up_date": "2026-11-19"}).status_code, 200)
        listed = self.list_items("wedding", follow_up="today")
        row = next(item for item in listed if item["crm"]["lead_key"] == key)
        self.assertEqual(row["follow_up_date"], "2026-11-19")
        self.assertEqual(row["crm"]["follow_up_date"], "2026-11-19")
        overdue = self.list_items("wedding", follow_up="overdue")
        self.assertFalse(any(item["crm"]["lead_key"] == key for item in overdue))
        with self.logged_in("phodeo"):
            response = self.client.get(f"{API}/leads/wedding/{key}/activity")
        self.assertEqual(response.status_code, 200, response.text)
        fields = {item["field"] for item in response.json()["items"]}
        self.assertTrue({"notes", "follow_up_date"}.issubset(fields))
        self.assertNotIn("vendor_id", fields)
        with self.logged_in("admin"):
            admin_log = self.client.get(f"{API}/leads/wedding/{key}/activity").json()
        self.assertIn("vendor_id", {item["field"] for item in admin_log["items"]})

    def test_follow_up_filter_is_available_on_all_four_crm_lists(self):
        for source in ("spin", "whatsapp", "wedding", "wedding_planner"):
            values = dict(SAMPLE[source])
            values["phone"] = f"+9198450{len(source):05d}"
            lead = self.add_ok(source, values)
            response = self.put_crm("admin", source, lead["lead_key"], {"follow_up_date": "2026-11-19"})
            self.assertEqual(response.status_code, 200, response.text)
            rows = self.list_items(source, follow_up="today")
            self.assertTrue(any(row.get("crm", {}).get("lead_key") == lead["lead_key"] for row in rows), source)

    def test_activity_records_crm_edit_archive_and_hot_actions(self):
        lead = self.add_ok("wedding", {**SAMPLE["wedding"], "phone": "09845040003"})
        key = lead["lead_key"]
        with self.logged_in("admin"):
            changed = self.client.patch(f"{API}/crm-leads/wedding/{key}", json={"values": {"name": "Changed"}})
            self.assertEqual(changed.status_code, 200, changed.text)
            self.put_crm("admin", "wedding", key, {"update_status": "interested"})
            for method, path, prefix in (("post", "hot", "leads"), ("delete", "hot", "leads"),
                                         ("post", "archive", "crm-leads"), ("post", "unarchive", "crm-leads")):
                response = getattr(self.client, method)(f"{API}/{prefix}/wedding/{key}/{path}")
                self.assertEqual(response.status_code, 200, response.text)
            rows = self.client.get(f"{API}/leads/wedding/{key}/activity").json()["items"]
        actions = {item["action"] for item in rows}
        self.assertTrue({"created", "field_changed", "hot_on", "hot_off", "archived", "unarchived"}.issubset(actions))
        self.assertIn("update_status", {item["field"] for item in rows})

    def test_activity_storage_failure_is_best_effort(self):
        with Session(self.engine) as db:
            with mock.patch.object(db, "add_all", side_effect=RuntimeError("activity table unavailable")):
                lead_activity.log_action(db, source="wedding", lead_key="failure-case", action="field_changed",
                    field="name", old="A", new="B", actor_user_id=self.users["admin"].id,
                    actor_name="Admin", actor_role="admin")
        with Session(self.engine) as db:
            self.assertFalse(db.exec(select(LeadActivity).where(LeadActivity.lead_key == "failure-case")).all())

    def test_reminders_include_open_crm_follow_up_and_scope_vendors(self):
        dates = (("09845030003", "2026-11-18", None), ("09845030004", "2026-11-19", None),
                 ("09845030005", "2026-11-22", None), ("09845030006", "2026-11-18", "converted"),
                 ("09845030007", "2026-11-18", "not_interested"))
        leads = []
        for phone, follow_up, status in dates:
            lead = self.add_ok("wedding", {**SAMPLE["wedding"], "phone": phone})
            leads.append(lead)
            changes = {"vendor_id": self.vendor_ids["phodeo"], "follow_up_date": follow_up}
            if status:
                changes["update_status"] = status
            self.put_crm("admin", "wedding", lead["lead_key"], changes)
        archived = self.add_ok("wedding", {**SAMPLE["wedding"], "phone": "09845030008"})
        self.put_crm("admin", "wedding", archived["lead_key"], {"follow_up_date": "2026-11-18"})
        with self.logged_in("admin"):
            self.client.post(f"{API}/crm-leads/wedding/{archived['lead_key']}/archive")
        with self.logged_in("admin"):
            admin = self.client.get(f"{API}/reminders/follow-ups", params={"days": 7})
        self.assertEqual(admin.status_code, 200, admin.text)
        item = next(i for i in admin.json()["items"] if i["lead_key"] == leads[0]["lead_key"])
        self.assertEqual(item["days_overdue"], 1)
        self.assertEqual(item["vendor_name"], "Phodeo")
        self.assertEqual(admin.json()["counts"], {"overdue": 1, "today": 1, "upcoming": 1})
        self.assertEqual([i["lead_key"] for i in admin.json()["items"]],
                         [leads[0]["lead_key"], leads[1]["lead_key"], leads[2]["lead_key"]])
        with self.logged_in("admin"):
            filtered = self.client.get(f"{API}/reminders/follow-ups", params={"days": 1, "sources": "wedding"})
            empty = self.client.get(f"{API}/reminders/follow-ups", params={"days": 7, "sources": "spin"})
            invalid = self.client.get(f"{API}/reminders/follow-ups", params={"sources": "not_a_source"})
        self.assertEqual(len(filtered.json()["items"]), 2)
        self.assertEqual(filtered.json()["counts"], {"overdue": 1, "today": 1, "upcoming": 0})
        self.assertEqual(empty.json()["counts"], {"overdue": 0, "today": 0, "upcoming": 0})
        self.assertEqual(invalid.status_code, 400)
        with self.logged_in("phodeo"):
            vendor = self.client.get(f"{API}/reminders/follow-ups", params={"sources": "dhruva,wedding"})
        self.assertEqual(vendor.status_code, 200, vendor.text)
        self.assertEqual({item["source"] for item in vendor.json()["items"]}, {"wedding"})
        self.assertEqual(len(vendor.json()["items"]), 3)

    def test_spin_reminder_details_are_batched_with_a_200_row_cap(self):
        calls = []
        def batch(*args, **kwargs):
            calls.append((args, kwargs))
            keys = kwargs["filters"]["ids_in"]
            return {"columns": [{"name": "id"}, {"name": "name"}, {"name": "phone"}],
                    "items": [{"id": int(key), "name": "Lead", "phone": "9999999999"} for key in keys]}
        with mock.patch.object(spin_ep, "list_spin_leads", side_effect=batch):
            out = reminders._workflow_spin_details([str(i) for i in range(401)])
        self.assertEqual(len(out), 401)
        self.assertEqual([call[0][2] for call in calls], [200, 200, 1])
        self.assertTrue(all(call[1]["limit"] <= 200 for call in calls))

    def test_city_alias_merging_and_per_source_counts(self):
        result = analytics._top_cities([
            ("wedding", "Bangalore"), ("wedding_planner", " Bengaluru "), ("spin", "Banglore"),
            ("vendor_to_koc", "Shamirpet, HYDERBAD 500001"),
            ("vendor_to_koc", "Hyderabad"), ("wedding", ""),
        ])
        self.assertEqual(result[0], {"city": "Bengaluru", "count": 3,
                                     "by_source": {"wedding": 1, "wedding_planner": 1, "spin": 1}})
        hyd = next(city for city in result if city["city"] == "Hyderabad")
        self.assertEqual(hyd["count"], 2)
        self.assertEqual(hyd["by_source"]["vendor_to_koc"], 2)

    def test_dhruva_activity_and_read_only_reminders_route(self):
        with Session(self.engine) as db:
            dhruva = User(name="Dhruva", email="dhruva@example.com", hashed_password="x", role="dhruva")
            db.add(dhruva)
            db.commit()
            db.refresh(dhruva)
            self.users["dhruva"] = dhruva
        with self.logged_in("admin"):
            created = self.client.post(f"{API}/dhruva/leads", json={
                "name": "Sample", "venue": "Hall", "event_dates": "2026-12-20", "phones": ["9845022222"],
                "event_type": "Wedding"})
        self.assertEqual(created.status_code, 201, created.text)
        lead_id = created.json()["id"]
        with self.logged_in("admin"):
            self.client.patch(f"{API}/dhruva/leads/{lead_id}", json={"follow_up_date": "2026-11-19"})
        with self.logged_in("dhruva"):
            changed = self.client.patch(f"{API}/dhruva/leads/{lead_id}", json={"venue": "New Hall"})
            self.assertEqual(changed.status_code, 200, changed.text)
            log = self.client.get(f"{API}/dhruva/leads/{lead_id}/activity")
            self.assertEqual(log.status_code, 200, log.text)
            self.assertEqual(log.json()["items"][0]["actor_role"], "dhruva")
            self.assertEqual(self.client.get(f"{API}/reminders/follow-ups").status_code, 403)
            reminders_response = self.client.get(f"{API}/dhruva/reminders/follow-ups")
            self.assertEqual(reminders_response.status_code, 200, reminders_response.text)
            reminder = reminders_response.json()["items"][0]
            self.assertEqual(reminder["source"], "dhruva")
            self.assertNotIn("update_status", reminder)

    def test_vendor_to_koc_activity_permissions_and_visible_fields(self):
        with self.logged_in("phodeo"):
            created = self.client.post(f"{API}/vendor-to-koc/leads", json={
                "name": "VTK Sample", "date": "2026-11-18", "phone": "9988776655", "city": "Pune"})
        self.assertEqual(created.status_code, 201, created.text)
        lead_id = created.json()["id"]
        with self.logged_in("admin"):
            updated = self.client.patch(f"{API}/vendor-to-koc/leads/{lead_id}", json={
                "update_status": "interested", "additional_comments": "Call back"})
        self.assertEqual(updated.status_code, 200, updated.text)
        with self.logged_in("phodeo"):
            own = self.client.get(f"{API}/vendor-to-koc/leads/{lead_id}/activity")
            other = self.client.get(f"{API}/vendor-to-koc/leads/{lead_id + 1000}/activity")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(other.status_code, 404)
        visible_fields = {item["field"] for item in own.json()["items"]}
        self.assertTrue({"name", "update_status", "additional_comments"}.issubset(visible_fields))


if __name__ == "__main__":
    unittest.main()
