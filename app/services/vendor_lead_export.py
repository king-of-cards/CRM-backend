"""Vendor to KOC export and template files (.xlsx and .csv). No database access.

Formula safety:
- XLSX: every text value is written as an explicit string cell (data_type "s"), so a value
  such as "=HYPERLINK(...)" is stored as text, never as a formula. Values are not altered.
- CSV: a text value starting with = + - @ tab or carriage return gets a leading apostrophe,
  because a CSV can't mark a cell as text.
"""
import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from app.models.vendor_to_koc import STATUS_LABELS
from app.services.vendor_lead_import import TEMPLATE_EXAMPLE

EXPORT_MAX_ROWS = 50_000
IST = timezone(timedelta(hours=5, minutes=30), "IST")   # India has no daylight saving

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
# Exports look like this CRM's list screens: 13 Dec 2026, 14 Nov 2026, 10:30 am (IST), +91 phones
DATE_FORMAT = "dd mmm yyyy"
DATETIME_FORMAT = "d mmm yyyy, hh:mm am/pm"
# ₹1,50,000 (Indian grouping), as the other exports' money cells
MONEY_FORMAT = '[>=10000000]"₹"##\\,##\\,##\\,##0;[>=100000]"₹"##\\,##\\,##0;"₹"##,##0'
# The upload template keeps the upload format (dates DD-MM-YYYY), which the import reads
TEMPLATE_DATE_FORMAT = "DD-MM-YYYY"
CSV_DANGEROUS_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class Column:
    heading: str
    kind: str          # "int" | "text" | "phone" | "date" | "datetime" | "money" (rupees)
    width: int


ADMIN_COLUMNS = [
    Column("Sl. No", "int", 8), Column("Vendor", "text", 24), Column("Date", "date", 12),
    Column("Name", "text", 30), Column("Phone", "phone", 14), Column("City", "text", 20),
    Column("Update", "text", 30), Column("Additional comments", "text", 50),
    Column("Follow-up date", "date", 14), Column("Quoted amount", "money", 16),
    Column("Conversion amount", "money", 18), Column("Updated by", "text", 24),
    Column("Updated at", "datetime", 18),
]
# Vendor logins: the same without the Vendor column (only their own leads)
VENDOR_COLUMNS = [c for c in ADMIN_COLUMNS if c.heading != "Vendor"]
TEMPLATE_COLUMNS = [
    Column("Name", "text", 30), Column("Date", "date", 14),
    Column("Phone", "phone", 14), Column("City", "text", 20),
]


def to_ist(value: datetime | None) -> datetime | None:
    """Aware UTC -> naive IST, to the minute (what the export shows)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).replace(tzinfo=None, second=0, microsecond=0)


def today_ist() -> date:
    return datetime.now(IST).date()


def filename_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "vendor"


# ── Building the table ─────────────────────────────────────────────────
def display_phone(phone: str | None) -> str | None:
    """+919845012345, as the list screens show phones (stored as the 10-digit mobile number)."""
    return f"+91{phone}" if phone else phone


def display_date(value: date) -> str:
    """13 Dec 2026"""
    return value.strftime("%d %b %Y")


def display_datetime(value: datetime) -> str:
    """14 Nov 2026, 10:30 am (the value is already India time)"""
    return f"{value.day} {value:%b %Y}, {value:%I:%M} {'am' if value.hour < 12 else 'pm'}"


def lead_table(rows: Iterable[tuple], admin: bool) -> tuple[list[Column], list[list[Any]]]:
    """Turn export_rows() tuples into (columns, values). Values are Python objects;
    the writers below format them."""
    table = []
    for i, (vendor, lead_date, name, phone, city, status, comments, follow_up, quoted, conversion,
            updater_name, updater_email, updated_at) in enumerate(rows, start=1):
        row = [
            i, vendor, lead_date, name, display_phone(phone), city,
            STATUS_LABELS.get(status, status), comments, follow_up, quoted, conversion,
            updater_name or updater_email, to_ist(updated_at),
        ]
        table.append(row if admin else row[:1] + row[2:])         # vendor logins: no Vendor
    return (ADMIN_COLUMNS if admin else VENDOR_COLUMNS), table


def template_table() -> tuple[list[Column], list[list[Any]]]:
    e = TEMPLATE_EXAMPLE
    return TEMPLATE_COLUMNS, [[e["name"], e["date"], e["phone"], e["city"]]]


# ── XLSX ───────────────────────────────────────────────────────────────
def _text_cell(ws, value: str, number_format: str | None = None, bold: bool = False) -> WriteOnlyCell:
    cell = WriteOnlyCell(ws)
    cell.value = ILLEGAL_CHARACTERS_RE.sub("", str(value))
    cell.data_type = "s"            # always a string, even if it starts with "="
    if number_format:
        cell.number_format = number_format
    if bold:
        cell.font = Font(bold=True)
    return cell


def _xlsx_cell(ws, value: Any, kind: str) -> WriteOnlyCell:
    if value is None:
        cell = WriteOnlyCell(ws)
        if kind == "phone":
            cell.number_format = "@"
        return cell
    if kind in ("text", "phone") or isinstance(value, str):     # a date column may hold text too
        return _text_cell(ws, value, "@" if kind == "phone" else None)
    cell = WriteOnlyCell(ws, value=value)
    if kind == "date":
        cell.number_format = DATE_FORMAT
    elif kind == "datetime":
        cell.number_format = DATETIME_FORMAT
    elif kind == "money":
        cell.number_format = MONEY_FORMAT
    return cell


def to_xlsx(columns: list[Column], rows: list[list[Any]], sheet_title: str = "Leads") -> bytes:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(sheet_title)
    for index, column in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(index)].width = column.width
    ws.freeze_panes = "A2"
    ws.append([_text_cell(ws, c.heading, bold=True) for c in columns])
    for row in rows:
        ws.append([_xlsx_cell(ws, value, c.kind) for value, c in zip(row, columns)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template_xlsx() -> bytes:
    """Template with Date formatted DD-MM-YYYY and Phone as text for the whole column,
    so values typed later keep the right format too."""
    columns, rows = template_table()
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    for index, column in enumerate(columns, start=1):
        dim = ws.column_dimensions[get_column_letter(index)]
        dim.width = column.width
        if column.kind == "date":
            dim.number_format = TEMPLATE_DATE_FORMAT
        elif column.kind == "phone":
            dim.number_format = "@"
    for index, column in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=index, value=column.heading)
        cell.font = Font(bold=True)
    for r, row in enumerate(rows, start=2):
        for index, (value, column) in enumerate(zip(row, columns), start=1):
            cell = ws.cell(row=r, column=index, value=value)
            if column.kind == "date":
                cell.number_format = TEMPLATE_DATE_FORMAT
            elif column.kind in ("text", "phone"):
                cell.data_type = "s"
                if column.kind == "phone":
                    cell.number_format = "@"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── CSV ────────────────────────────────────────────────────────────────
def _csv_value(value: Any, kind: str, upload_format: bool = False) -> str:
    if value is None:
        return ""
    if kind == "date" and not isinstance(value, str):
        return value.strftime("%d-%m-%Y") if upload_format else display_date(value)
    if kind == "datetime" and not isinstance(value, str):
        return display_datetime(value)
    if kind in ("int", "money"):
        return str(value)                # plain rupees: 150000
    text = str(value)
    if text.startswith(CSV_DANGEROUS_START):
        text = "'" + text           # stops spreadsheet programs from running it as a formula
    return text


def to_csv(columns: list[Column], rows: list[list[Any]], upload_format: bool = False) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c.heading for c in columns])
    for row in rows:
        writer.writerow([_csv_value(value, c.kind, upload_format) for value, c in zip(row, columns)])
    return buf.getvalue().encode("utf-8-sig")    # BOM: Excel then reads UTF-8 correctly


def template_csv() -> bytes:
    return to_csv(*template_table(), upload_format=True)
