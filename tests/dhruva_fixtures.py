"""Fake Dhruva data copying the real sheet's patterns (no real names or phones).

Two sheets: "Oct - Dec " (title row "Updated on 12th Nov ", headings on row 2 with trailing
spaces, "Contact No ") and "Dec -March 2027" (blank row 1, "MOBILE NO"). Phones: numeric Excel
cells, empty, "nil", two numbers split by a line break or by a comma + line break. Dates mostly
text ("Dec 4th", "Feb 24&25th 2027", "Nil"), a few real date cells. Venues and event types with
odd spacing and line breaks; a name with a line break.
"""
import io
from contextlib import contextmanager
from datetime import date, datetime
from unittest import mock

from openpyxl import Workbook
from sqlmodel import Session

from app.models.user import User
from app.services import periods
from tests.test_crm_leads_api import ApiTestBase

TODAY = date(2026, 11, 16)

# (SL NO, Name, Venue, Date, Contact, Event Type)
SHEET1 = [
    (1, "Asha Fake", "Main Palace", "Dec 4th", 9800000001, "wedding"),
    (2, "Ravi \nKumarfake", "Main palace", "dec 25th", "9800000002\n9000000002", "engagement \nwedding"),
    (3, "Meena Fake", "Main Palace \n", "12th Dec", "9700000003,\n8200000003", "reception \nmuhurtham"),
    (4, "No Phone One", None, "Nov 22nd", None, "beegar oota"),
    (5, "Nil Phone", "Need to finalise the venue", "Feb 28th, Tentative date", "nil", None),
    (6, "Month Only", None, "March", 9800000006, "naming ceremony"),
    (7, "Either Day", "Shangrila or other \nOne", "Dec 5th or 6th", 9800000007, "nikah"),
    (8, "Range Fake", None, "Jan 22nd -24th 2027", 9800000008, "wedding \nsangeeth"),
    (9, "Excel Date", "Hall Fake", datetime(2026, 12, 20), 9800000009, "after muhurtham lunch"),
    (10, "Nil Date", None, "Nil", 9800000010, None),
    (11, "Repeat In File", None, "Dec 4th", 9800000001, "wedding"),        # same phone as row 3 (Asha)
    (12, "No Phone One", None, "Nov 22nd", None, None),                     # same name + date as SL 4
    (13, None, None, None, None, None),                                    # empty row: skipped
    (14, "Bad Phone", None, "Dec 1st", "12345", None),                      # needs fixing
]
SHEET2 = [
    ("1", "Dec Fake", "Garden Fake", "Feb 24&25th 2027", 9800000101, "reception"),
    ("2", "Two Days", None, "10th & 11th Feb 2027", "9800000102\n9000000102", "engagement"),
    ("3", "June Fake", None, "June 22nd ,23rd \n2027", 9800000103, "wedding"),
    ("4", "April Fake", "Main Palace", "April 23rd 2027", None, "wedding"),
    ("5", "Feb Only", None, "Feb", 9800000105, None),
    ("6", "Excel Date Two", None, datetime(2027, 1, 10), 9800000106, "nikah"),
]


def sample_workbook() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Oct - Dec "
    ws.append(["Updated on 12th Nov "])
    ws.append(["SL NO", "Name ", "Venue ", "Date ", "Contact No ", "Event Type "])
    for row in SHEET1:
        ws.append(list(row))
    ws2 = wb.create_sheet("Dec -March 2027")
    ws2.append([])
    ws2.append(["SLNO ", "Name ", "Venue ", "Date ", "MOBILE NO", "Event Type "])
    for row in SHEET2:
        ws2.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class DhruvaTestBase(ApiTestBase):
    """ApiTestBase (admin, vendor logins RJ / Phodeo / OHP) plus two Dhruva logins; today fixed."""

    def setUp(self):
        super().setUp()
        self.today_patch = mock.patch.object(periods, "today_ist", lambda: TODAY)
        self.today_patch.start()
        with Session(self.engine, expire_on_commit=False) as s:
            for who in ("dhruva", "dhruva2"):
                self.users[who] = User(name=who.title(), email=f"{who}@example.com", hashed_password="x", role="dhruva")
                s.add(self.users[who])
            s.commit()
            for who in ("dhruva", "dhruva2"):
                s.refresh(self.users[who])

    def tearDown(self):
        self.today_patch.stop()
        super().tearDown()

    def call(self, who, method, url, **kw):
        with self.logged_in(who):
            return self.client.request(method, f"/api/v1/dhruva{url}", **kw)

    def preview(self, who="dhruva", content: bytes | None = None, filename="sample.xlsx", **form):
        return self.call(who, "POST", "/upload/preview", files={"file": (filename, content or sample_workbook())},
                         data=form)

    def upload_all(self, who="dhruva", **form) -> dict:
        body = self.preview(who, **form).json()
        leads = [r["values"] for r in body["rows"]]
        return self.call(who, "POST", "/leads/bulk", json={"leads": leads}).json()

    def add(self, who="dhruva", **values):
        return self.call(who, "POST", "/leads", json=values)

    @contextmanager
    def as_user(self, who):
        with self.logged_in(who):
            yield
