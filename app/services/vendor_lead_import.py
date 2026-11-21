"""Reading and validating Vendor to KOC leads. No database access.

Files (.xlsx / .csv): headings in row 1, matched ignoring case and spaces: Name, Date,
Phone, City. Every other column (Sl. No, Event date, Status, Comment, ...) is ignored.
Dates in files: real Excel dates, or text DD-MM-YYYY (always day first, never MM-DD) or
"13 Dec 2026" / "13 December 2026" (the export's format, so an exported file uploads back).
Dates in JSON (manual add, bulk save): YYYY-MM-DD.
Phones: numeric cells (8095852272.0) become text; a leading ' (the CSV export's formula guard),
spaces, dashes, +91 (or 91 when 12 digits remain) and a leading 0 are removed; the result must be
an Indian mobile number.
"""
import csv
import io
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePath
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_NAME = 100
MAX_CITY = 100
FIELDS = ("name", "date", "phone", "city")

PHONE_RE = re.compile(r"^[6-9]\d{9}$")
DAY_FIRST_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
MONTH_NAME_FORMATS = ("%d %b %Y", "%d %B %Y")          # 13 Dec 2026, 13 December 2026
ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
INTEGRAL_TEXT_RE = re.compile(r"^(\d+)\.0+$")

MSG_PHONE_REQUIRED = "Phone is required"
MSG_PHONE_INVALID = "Phone must be a 10-digit Indian mobile number starting with 6, 7, 8 or 9"
MSG_DATE_FILE = "Date must be a date, or text like 13-12-2026 or 13 Dec 2026"
MSG_DATE_JSON = "Date must be in YYYY-MM-DD format"
MSG_DATE_INVALID = "Date is not a valid calendar date"


# The example row of the downloadable template (fake data). Uploading it unchanged is an
# error, so a forgotten example row can't be saved as a real lead.
TEMPLATE_EXAMPLE = {"name": "Asha Example", "date": date(2026, 1, 15),
                    "phone": "9876543210", "city": "Bengaluru"}
EXAMPLE_ROW_ERROR = {"field": "name", "message": "This is the example row from the template; delete it"}


class ImportFileError(Exception):
    """The file as a whole can't be used. Mapped to an HTTP error by the endpoint."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class CleanLead:
    """Result of validating one lead. Values are cleaned even when there are errors,
    so the preview can show what will be saved (or what needs fixing)."""
    name: str | None
    date: date | None
    date_display: str | None      # YYYY-MM-DD when valid, otherwise the text as given
    phone: str | None
    city: str | None
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ── Value cleaning ─────────────────────────────────────────────────────
def _is_blank(value: Any) -> bool:
    """Empty or N/A-like ("N/A", "-", "nil", "none", "." …): never a value, never an error."""
    from app.services.lead_values import is_na

    return is_na(value)


def signature(lead: "CleanLead") -> tuple:
    """Every template field but the phone, as the duplicate check compares them (the phone is
    matched first): name, date, city (trimmed, spaces collapsed, any case; the date as ISO)."""
    from app.services.lead_values import dup_norm

    return dup_norm(lead.name), dup_norm(lead.date, "date"), dup_norm(lead.city)


def clean_text(value: Any) -> str | None:
    if _is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    if len(text) > 1 and text[0] == "'" and text[1] in "=+-@":     # the CSV export's formula guard
        text = text[1:]
    return text


def clean_phone(value: Any) -> str | None:
    """Normalise a phone value; returns None for blank. Doesn't validate."""
    if _is_blank(value) or isinstance(value, bool):
        return None
    if isinstance(value, (float, Decimal)):
        integral = math.isfinite(value) and value == int(value)
        text = str(int(value)) if integral else str(value)
    else:
        text = str(value).strip().lstrip("'")     # the CSV export's formula guard
        match = INTEGRAL_TEXT_RE.match(text)      # "8095852272.0" from a CSV export
        if match:
            text = match.group(1)
    text = re.sub(r"[\s\-]", "", text)
    if text.startswith("+91"):
        text = text[3:]
    elif len(text) == 12 and text.startswith("91"):
        text = text[2:]
    if text.startswith("0"):
        text = text[1:]
    return text


def _date_from(day: int, month: int, year: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_file_date(value: Any) -> tuple[date | None, str | None, str | None]:
    """(date, display text, error) for a value from a spreadsheet or CSV cell."""
    if _is_blank(value):
        return None, None, None
    if isinstance(value, datetime):
        return value.date(), value.date().isoformat(), None
    if isinstance(value, date):
        return value, value.isoformat(), None
    if isinstance(value, str):
        text = value.strip()
        match = DAY_FIRST_RE.match(text)
        if match:
            day, month, year = (int(g) for g in match.groups())
            parsed = _date_from(day, month, year)
            if parsed is None:
                return None, text, MSG_DATE_INVALID
            return parsed, parsed.isoformat(), None
        for fmt in MONTH_NAME_FORMATS:                  # the export's display format
            try:
                parsed = datetime.strptime(re.sub(r"\s+", " ", text), fmt).date()
            except ValueError:
                continue
            return parsed, parsed.isoformat(), None
        return None, text, MSG_DATE_FILE
    return None, clean_text(value), MSG_DATE_FILE   # numbers, times, booleans


def parse_json_date(value: Any) -> tuple[date | None, str | None, str | None]:
    """(date, display text, error) for a date sent as JSON (YYYY-MM-DD)."""
    if _is_blank(value):
        return None, None, None
    text = str(value).strip()
    match = ISO_RE.match(text)
    if not match:
        return None, text, MSG_DATE_JSON
    year, month, day = (int(g) for g in match.groups())
    parsed = _date_from(day, month, year)
    if parsed is None:
        return None, text, MSG_DATE_INVALID
    return parsed, parsed.isoformat(), None


def validate_lead(name: Any, date_value: Any, phone: Any, city: Any,
                  parse_date: Callable[[Any], tuple]) -> CleanLead:
    lead_date, date_display, date_error = parse_date(date_value)
    lead = CleanLead(
        name=clean_text(name), date=lead_date, date_display=date_display,
        phone=clean_phone(phone), city=clean_text(city),
    )
    if lead.name is not None and len(lead.name) > MAX_NAME:
        lead.errors.append({"field": "name", "message": f"Name must be at most {MAX_NAME} characters"})
    if date_error:
        lead.errors.append({"field": "date", "message": date_error})
    if lead.phone is None:
        lead.errors.append({"field": "phone", "message": MSG_PHONE_REQUIRED})
    elif not PHONE_RE.match(lead.phone):
        lead.errors.append({"field": "phone", "message": MSG_PHONE_INVALID})
    if lead.city is not None and len(lead.city) > MAX_CITY:
        lead.errors.append({"field": "city", "message": f"City must be at most {MAX_CITY} characters"})
    return lead


def is_template_example(lead: CleanLead) -> bool:
    """True if all four values match the template's example row (name/city ignoring case)."""
    example = TEMPLATE_EXAMPLE
    return ((lead.name or "").casefold() == example["name"].casefold()
            and lead.date == example["date"]
            and lead.phone == example["phone"]
            and (lead.city or "").casefold() == example["city"].casefold())


def flag_template_example(lead: CleanLead) -> CleanLead:
    """Adds EXAMPLE_ROW_ERROR to an untouched template example row (upload flow only)."""
    if is_template_example(lead):
        lead.errors.append(dict(EXAMPLE_ROW_ERROR))
    return lead


# ── Files ──────────────────────────────────────────────────────────────
def _heading_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value)).lower() if value is not None else ""


def _xlsx_rows(data: bytes, sheet_name: str | None):
    if not data.startswith(b"PK\x03\x04"):
        raise ImportFileError(400, "The file is not a valid .xlsx file")
    import openpyxl   # uses defusedxml automatically when installed

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        logger.warning("Could not open uploaded .xlsx: %s", type(exc).__name__)
        raise ImportFileError(400, "The .xlsx file could not be read") from None
    try:
        names = workbook.sheetnames
        if sheet_name:
            if sheet_name not in names:
                raise ImportFileError(400, f"Sheet '{sheet_name}' not found. Sheets: {', '.join(names)}")
            sheet = workbook[sheet_name]
        else:
            sheet = workbook[names[0]]
        for row in sheet.iter_rows(values_only=True):
            yield list(row)
    except ImportFileError:
        raise
    except Exception as exc:
        logger.warning("Could not read uploaded .xlsx: %s", type(exc).__name__)
        raise ImportFileError(400, "The .xlsx file could not be read") from None
    finally:
        workbook.close()


def _csv_rows(data: bytes):
    if b"\x00" in data:
        raise ImportFileError(400, "The file is not a valid .csv file")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    for row in csv.reader(io.StringIO(text, newline="")):
        yield row


def read_file(filename: str | None, data: bytes, sheet_name: str | None = None
              ) -> list[tuple[int, dict[str, Any]]]:
    """Returns [(row number, {name, date, phone, city: raw value})], skipping empty rows."""
    suffix = PurePath(filename or "").suffix.lower()
    if suffix == ".xlsx":
        rows = _xlsx_rows(data, sheet_name)
    elif suffix == ".csv":
        rows = _csv_rows(data)
    else:
        raise ImportFileError(400, "Only .xlsx and .csv files are supported")

    header = next(rows, None)
    if header is None:
        raise ImportFileError(400, "The file is empty")
    columns: dict[str, int] = {}
    for index, heading in enumerate(header):
        key = _heading_key(heading)
        if key in FIELDS and key not in columns:       # first matching column wins
            columns[key] = index
    if "phone" not in columns:
        raise ImportFileError(400, "Missing column: Phone")

    result = []
    for row_number, row in enumerate(rows, start=2):
        values = {f: (row[i] if i < len(row) else None) for f, i in columns.items()}
        values = {f: values.get(f) for f in FIELDS}
        if all(_is_blank(v) for v in values.values()):
            continue
        if len(result) >= MAX_ROWS:
            raise ImportFileError(400, f"The file has more than {MAX_ROWS:,} rows")
        result.append((row_number, values))
    return result
