"""Fake Dhruva -> KOC data (no real names or phones) in the upload template's format: sheet "Leads",
the 16 template headings, every value as text. Event dates as YYYY-MM-DD, comma-separated.
"""
import io
from contextlib import contextmanager
from datetime import date, datetime, timezone
from unittest import mock

from openpyxl import Workbook
from sqlmodel import Session

from app.models.user import User
from app.services import periods
from app.services.dhruva_import import HEADINGS
from tests.test_crm_leads_api import ApiTestBase

TODAY = date(2026, 11, 16)
HEADERS = [label for _, label in HEADINGS]


def lead_row(name=None, phones=None, event_dates=None, event_type=None, venue=None, **koc) -> list:
    """One sheet row in template order; koc: the other fields by key (whatsapp_status, update_status, …)."""
    values = {"name": name, "phones": phones, "event_dates": event_dates, "event_type": event_type,
              "venue": venue, **koc}
    return [values.get(key) for key, _ in HEADINGS]


SHEET = [
    lead_row("Asha Fake", "9800000001", "2026-12-04", "wedding", "Main Palace", whatsapp_status="Delivered",
             update_status="Interested", quoted_amount="45000", conversion_amount="1,20,000",
             follow_up_date="2026-11-20"),
    lead_row("Ravi Kumarfake", "9800000002, 9000000002", "2026-12-25, 2026-12-26", "engagement, wedding",
             "Main palace", needs_intervention="Yes"),
    lead_row("Meena Fake", "+91 97000 00003, 8200000003; 7000000033", "2026-12-12", "reception", "Main Palace"),
    lead_row("No Phone One", None, "2026-11-22", "beegar oota"),
    lead_row("Two Dates", "9800000006", "2027-01-24, 2027-01-22", "wedding, sangeeth"),
    lead_row("No Date", "9800000010"),
    lead_row("Asha Again", "9100000099, 9800000001", "2026-12-05", "wedding"),     # a phone of row 2
    lead_row("No Phone One", None, "2026-11-22", "lunch"),                           # same name + first date
    lead_row(None, "9800000011"),                                                    # no name: needs fixing
    lead_row("Bad Phone", "9800000013, 12345", "2026-12-01"),                        # needs fixing
    lead_row("Bad Date", "9800000012", "Dec 4th"),                                   # needs fixing
    lead_row(None, None),                                                            # empty row: skipped
]


def sample_workbook(rows: list[list] | None = None, headers: list[str] | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(headers or HEADERS)
    for row in (SHEET if rows is None else rows):
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class DhruvaTestBase(ApiTestBase):
    """ApiTestBase (admin, vendor logins RJ / Phodeo / OHP) plus two Dhruva logins; today fixed."""

    def setUp(self):
        super().setUp()
        self.today_patch = mock.patch.object(periods, "today_ist", lambda: TODAY)
        self.today_patch.start()
        self.utcnow_patch = mock.patch("app.crud.dhruva.utcnow",
                                       lambda: datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc))
        self.utcnow_patch.start()
        with Session(self.engine, expire_on_commit=False) as s:
            for who in ("dhruva", "dhruva2"):
                self.users[who] = User(name=who.title(), email=f"{who}@example.com", hashed_password="x", role="dhruva")
                s.add(self.users[who])
            s.commit()
            for who in ("dhruva", "dhruva2"):
                s.refresh(self.users[who])

    def tearDown(self):
        self.utcnow_patch.stop()
        self.today_patch.stop()
        super().tearDown()

    def call(self, who, method, url, **kw):
        with self.logged_in(who):
            return self.client.request(method, f"/api/v1/dhruva{url}", **kw)

    def preview(self, who="admin", content: bytes | None = None, filename="sample.xlsx", **form):
        return self.call(who, "POST", "/upload/preview", files={"file": (filename, content or sample_workbook())},
                         data=form)

    def upload_all(self, who="admin", **form) -> dict:
        body = self.preview(who, **form).json()
        leads = [r["values"] for r in body["rows"]]
        return self.call(who, "POST", "/leads/bulk", json={"leads": leads}).json()

    def add(self, who="dhruva", **values):
        return self.call(who, "POST", "/leads", json=values)

    @contextmanager
    def as_user(self, who):
        with self.logged_in(who):
            yield
