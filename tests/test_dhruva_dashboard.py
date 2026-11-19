"""GET /analytics/dhruva: the Dhruva leads dashboard (period=all by default).

"Today" is 16 Nov 2026 (India time). In-memory SQLite (see tests/dhruva_fixtures.py). Run:

    python -m unittest discover -s tests -t . -v
"""
import unittest
from datetime import date, datetime, timezone

from sqlmodel import Session

from app.models.dhruva import DhruvaLead
from tests.dhruva_fixtures import DhruvaTestBase

URL = "/api/v1/analytics/dhruva"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class DhruvaDashboardTest(DhruvaTestBase, unittest.TestCase):
    """L1 received 00:00 IST 16 Nov, Main Palace, wedding, 4 Dec, converted (quote 2,00,000 / conversion 1,80,000)
    L2 received 10 Nov, Main palace, engagement + wedding, 25 Dec, interested (quote 50,000)
    L3 received 1 Oct, Hall, reception, "March" (no date), not interested
    L4 received 5 Nov, no venue / type, 22 Nov, pending
    L5 received 20 Sep, archived (never counted)"""

    def setUp(self):
        super().setUp()
        admin = self.users["admin"].id
        spec = [
            ("L1", utc(2026, 11, 15, 18, 30), "Main Palace", "wedding", date(2026, 12, 4), "converted", 200000, 180000, False),
            ("L2", utc(2026, 11, 10, 5), "Main palace", "engagement, wedding", date(2026, 12, 25), "interested", 50000, 10000, False),
            ("L3", utc(2026, 10, 1, 5), "Hall", "reception", None, "not_interested", None, None, False),
            ("L4", utc(2026, 11, 5, 5), None, None, date(2026, 11, 22), None, None, None, False),
            ("L5", utc(2026, 9, 20, 5), "Hall", "wedding", date(2026, 12, 1), "converted", 1, 1, True),
        ]
        with Session(self.engine) as s:
            for name, created, venue, etype, edate, status, quoted, conversion, archived in spec:
                s.add(DhruvaLead(name=name, created_at=created, updated_at=created, venue=venue, event_type=etype,
                                 event_date=edate, event_date_text="March" if name == "L3" else None,
                                 update_status=status, quoted_amount=quoted, conversion_amount=conversion,
                                 status_updated_by=admin if status else None, source="manual",
                                 created_by=self.users["dhruva"].id, is_deleted=archived))
            s.commit()

    def dash(self, who="dhruva", **params):
        with self.logged_in(who):
            resp = self.client.get(URL, params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_all_time_by_default(self):
        o = self.dash()
        self.assertEqual((o["period"]["date_from"], o["period"]["all_time"], o["period"]["granularity"]),
                         ("2026-10-01", True, "month"))
        self.assertEqual((o["received"], o["previous_period"], o["change_pct"]), (4, None, None))
        self.assertEqual(o["kpis"], {"total": 4, "interested": 1, "not_interested": 1, "quoted_count": 2,
                                     "quoted_total": 250000, "converted": 1, "converted_pct": 25.0,
                                     "conversion_count": 2, "conversion_total": 190000,
                                     "conversion_amount_count": 1, "conversion_amount_total": 180000,
                                     "hot_count": 0, "pending": 1,
                                 "upcoming_30_days": 2, "quotes_count": 2})
        self.assertEqual(o["by_event_type"], [{"value": "wedding", "count": 2}, {"value": "engagement", "count": 1},
                                              {"value": "reception", "count": 1}])
        self.assertEqual(o["top_venues"], [{"value": "Main Palace", "count": 2}, {"value": "Hall", "count": 1}])
        self.assertEqual(o["events_by_month"], [{"month": "2026-11", "count": 1}, {"month": "2026-12", "count": 2}])
        self.assertEqual(o["no_event_date"], 1)
        self.assertEqual([(p["date"], p["total"]) for p in o["inflow"]], [("2026-10-01", 1), ("2026-11-01", 3)])
        self.assertEqual({k: v for k, v in o["by_status"].items() if v},
                         {"pending": 1, "converted": 1, "interested": 1, "not_interested": 1})
        self.assertEqual([l["name"] for l in o["latest"]], ["L1", "L2", "L4", "L3"])
        self.assertEqual(self.dash(period="all")["kpis"], o["kpis"])

    def test_hot_count_is_date_filtered(self):
        lead_id = self.dash()["latest"][0]["id"]
        self.call("admin", "POST", f"/leads/{lead_id}/hot")
        self.assertEqual(self.dash(date_from="2026-11-16", date_to="2026-11-16")["kpis"]["hot_count"], 1)
        self.assertEqual(self.dash(date_from="2026-11-10", date_to="2026-11-10")["kpis"]["hot_count"], 0)

    def test_period_previous_and_change(self):
        week = self.dash(date_from="2026-11-10", date_to="2026-11-16")          # previous 3-9 Nov: L4
        self.assertEqual((week["received"], week["previous_period"], week["change_pct"]), (2, 1, 100))
        self.assertEqual(week["period"]["granularity"], "day")
        self.assertEqual(sum(p["total"] for p in week["inflow"]), 2)
        self.assertEqual(week["kpis"]["converted"], 1)
        day = self.dash(date_from="2026-11-16")                                 # 00:00 IST: L1 only
        self.assertEqual(day["received"], 1)
        empty = self.dash(date_from="2026-01-01", date_to="2026-01-31")
        self.assertEqual((empty["received"], empty["kpis"]["converted_pct"], empty["latest"]), (0, None, []))

    def test_roles(self):
        admin = self.dash("admin")
        self.assertEqual(admin["kpis"], self.dash()["kpis"])                    # Dhruva logins: everything
        self.assertIn("status_updated_by", admin["latest"][0])                  # user ids: admins only
        self.assertNotIn("status_updated_by", self.dash()["latest"][0])
        with self.logged_in("phodeo"):
            self.assertEqual(self.client.get(URL).status_code, 403)
        for params, text in (({"period": "all", "date_from": "2026-11-01"}, "not both"),
                             ({"date_from": "2026-11-17"}, "can't be in the future"),
                             ({"period": "week"}, 'period must be "all"')):
            with self.logged_in("dhruva"):
                resp = self.client.get(URL, params=params)
            self.assertEqual(resp.status_code, 400, params)
            self.assertIn(text, resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
