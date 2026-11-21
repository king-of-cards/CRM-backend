"""Reading Dhruva lead files and checking leads (upload preview, bulk save, add, edit).

Files: .xlsx (any sheet: sheet_name, default the first) or .csv. Title rows above the headings
("Updated on 12th Nov") are skipped: the heading row is the first one where at least two cells are
known headings. Headings are matched ignoring case, spaces and punctuation:

    SL NO / SLNO / Sl. No              ignored
    Name                               name
    Venue                              venue
    Date / Event date                  date (as typed)
    Contact No / MOBILE NO / Phone     phone (the cell may hold two numbers)
    Alt phone / Alternate phone        alt_phone
    Event Type                         event_type

Other columns are ignored (and reported). Values:

  name, venue   whitespace (line breaks too) collapsed to single spaces
  event_type    trimmed; line breaks become ", " ("reception\nmuhurtham" -> "reception, muhurtham")
  phone         split on line breaks, "," "/" ";": the first number is phone, the second alt_phone;
                Excel numbers (9845012345.0) become text; "nil", "na", "-" = no phone. Indian
                numbers may skip +91; others need their country code. More than two: an error.
  date          a real Excel date, DD-MM-YYYY, YYYY-MM-DD, or text with one day and one month
                ("Dec 4th", "12th Dec", "Feb 28th, Tentative date", "April 23rd 2027"). Without a
                year: the next time that day comes (today included), from the day it's entered.
                Ranges and choices ("Jan 22nd -24th 2027", "Dec 5th or 6th"), a month alone
                ("March") and "Nil" stay text only (event_date empty).

A lead needs a name or a phone. Rows without a phone are allowed and flagged "no_phone".
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.models.dhruva import DATE_TEXT_MAX, EVENT_TYPE_MAX, NAME_MAX, PHONE_MAX, VENUE_MAX
from app.services import phones

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
HEADING_SCAN_ROWS = 15

HEADINGS: dict[str, tuple[str, ...]] = {
    "sl_no": ("slno", "sl", "srno", "serialno", "no"),
    "name": ("name", "customername", "clientname", "fullname"),
    "venue": ("venue", "eventvenue", "location"),
    "date": ("date", "eventdate", "dateofevent"),
    "phone": ("contactno", "contactnumber", "contact", "mobileno", "mobilenumber", "mobile", "phone",
              "phoneno", "phonenumber"),
    "alt_phone": ("altphone", "alternatephone", "alternatenumber", "altno", "alternateno"),
    "event_type": ("eventtype", "event", "typeofevent", "occasion"),
}
FIELDS = ("name", "venue", "date", "phone", "alt_phone", "event_type")
NO_VALUE = {"", "nil", "na", "n/a", "none", "-", "--", "nill", "null", "."}   # = lead_values.NA_VALUES

MSG_TOO_MANY_PHONES = "At most two numbers (phone and alternate phone)"
MSG_NAME_OR_PHONE = "A name or a phone is required"
TEMPLATE_EXAMPLE = {"name": "Example Name", "venue": "Example Venue", "date": "4 Dec 2026",
                    "phone": "9876543210", "alt_phone": "", "event_type": "wedding"}
EXAMPLE_ROW_ERROR = {"field": "name", "message": "This is the example row from the template; delete it"}


class FileError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message, self.status_code = message, status_code


def norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


_HEADING_KEY = {alias: key for key, aliases in HEADINGS.items() for alias in aliases}


# ── Files ──────────────────────────────────────────────────────────────
def _xlsx_rows(content: bytes, sheet_name: str | None) -> tuple[list[list[Any]], list[str]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        raise FileError("The file isn't a readable Excel (.xlsx) file.")
    names = wb.sheetnames
    if sheet_name:
        match = next((n for n in names if n == sheet_name), None) or \
            next((n for n in names if n.strip().lower() == sheet_name.strip().lower()), None)
        if match is None:
            raise FileError(f"No sheet named '{sheet_name}'. Sheets: {', '.join(names)}")
        ws = wb[match]
    else:
        ws = wb[names[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows, names


def _csv_rows(content: bytes) -> list[list[Any]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise FileError("The CSV file must be UTF-8 or Windows-1252 text.")
    first = text.split("\n", 1)[0]
    delimiter = max((",", ";", "\t"), key=first.count)
    return [list(r) for r in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


@dataclass
class FileRows:
    sheet: str | None
    sheets: list[str]
    heading_row: int                       # 1-based, as in Excel
    columns: dict[str, str]                # heading as written -> field
    ignored_columns: list[str]
    rows: list[tuple[int, dict[str, Any]]]  # (Excel row number, {field: raw value})


def read_file(filename: str, content: bytes, sheet_name: str | None = None) -> FileRows:
    if len(content) > MAX_FILE_BYTES:
        raise FileError("The file is larger than 5 MB.", 413)
    if not content:
        raise FileError("The file is empty.")
    name = (filename or "").lower()
    sheets: list[str] = []
    if content[:2] == b"PK" or name.endswith(".xlsx"):
        rows, sheets = _xlsx_rows(content, sheet_name)
    elif name.endswith((".csv", ".txt")) or not name:
        rows = _csv_rows(content)
    else:
        raise FileError("Upload an Excel (.xlsx) or CSV file.")

    heading_index = None
    for i, row in enumerate(rows[:HEADING_SCAN_ROWS]):
        keys = {_HEADING_KEY.get(norm(v)) for v in row if not _blank(v)} - {None}
        if len(keys - {"sl_no"}) >= 2:
            heading_index = i
            break
    if heading_index is None:
        raise FileError("No heading row found. Use headings like Name, Venue, Date, Contact No, Event Type.")
    mapping: dict[int, str] = {}
    columns: dict[str, str] = {}
    ignored: list[str] = []
    for i, heading in enumerate(rows[heading_index]):
        if _blank(heading):
            continue
        key = _HEADING_KEY.get(norm(heading))
        written = str(heading).strip()
        if key == "sl_no":
            continue
        if key and key not in mapping.values():
            mapping[i] = key
            columns[written] = key
        else:
            ignored.append(written)
    if not any(k in mapping.values() for k in ("phone", "name")):
        raise FileError("The file needs a Name or a phone (Contact No / MOBILE NO / Phone) column.")
    data = []
    for offset, row in enumerate(rows[heading_index + 1:], start=heading_index + 2):
        values = {key: (row[i] if i < len(row) else None) for i, key in mapping.items()}
        if all(_blank(v) for v in values.values()):
            continue                                       # empty row (or only SL NO)
        data.append((offset, values))
        if len(data) > MAX_ROWS:
            raise FileError(f"The file has more than {MAX_ROWS:,} rows. Split it into smaller files.")
    sheet = (sheet_name or (sheets[0] if sheets else None)) if sheets else None
    return FileRows(sheet, sheets, heading_index + 1, columns, ignored, data)


# ── Values ─────────────────────────────────────────────────────────────
def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return f"{value.day} {value:%b %Y}"
    return str(value)


def one_line(value: Any) -> str | None:
    """One line of text; N/A-like values ("N/A", "-", "nil", …) are empty."""
    from app.services.lead_values import is_na

    text = re.sub(r"\s+", " ", as_text(value)).strip()
    return None if is_na(text) else text


def clean_event_type(value: Any) -> str | None:
    from app.services.lead_values import is_na

    if is_na(as_text(value)):
        return None
    parts = [re.sub(r"\s+", " ", p).strip(" ,") for p in re.split(r"[\r\n]+", as_text(value))]
    text = ", ".join(p for p in parts if p)
    return text or None


def split_phones(value: Any) -> list[str]:
    """The numbers written in one cell (Excel numbers become text)."""
    text = as_text(value).strip()
    if norm(text) in {norm(v) for v in NO_VALUE} or text.lower() in NO_VALUE:
        return []
    return [p.strip() for p in re.split(r"[\r\n,/;]+", text) if p.strip() and p.strip().lower() not in NO_VALUE]


_MONTHS = {m: i for i, names in enumerate(
    (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
     ("nov", "november"), ("dec", "december")), start=1) for m in names}
_MONTH_RE = re.compile(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b", re.I)
_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?(?!\d)", re.I)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_NUMERIC_DATES = ((re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$"), "dmy"),
                  (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "ymd"))


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next_occurrence(month: int, day: int, today: date) -> date | None:
    for year in range(today.year, today.year + 9):          # Feb 29th: the next leap year
        found = _safe_date(year, month, day)
        if found and found >= today:
            return found
    return None


def parse_event_date(value: Any, today: date) -> tuple[date | None, str | None, str | None]:
    """(event_date, event_date_text, error). Text that isn't one clear date stays text only."""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value, as_text(value), None
    text = re.sub(r"\s+", " ", as_text(value)).strip()
    if not text:
        return None, None, None
    if len(text) > DATE_TEXT_MAX:
        return None, text, f"Longer than {DATE_TEXT_MAX} characters"
    if text.lower() in NO_VALUE:
        return None, text, None
    for pattern, order in _NUMERIC_DATES:
        m = pattern.match(text)
        if m:
            a, b, c = (int(g) for g in m.groups())
            parsed = _safe_date(c, b, a) if order == "dmy" else _safe_date(a, b, c)
            if parsed is None:
                return None, text, "Not a real date"
            return parsed, text, None
    months = _MONTH_RE.findall(text)
    years = _YEAR_RE.findall(text)
    rest = _YEAR_RE.sub(" ", text)
    days = [int(d) for d in _DAY_RE.findall(rest)]
    if len(months) != 1 or len(days) != 1 or len(years) > 1:
        return None, text, None                            # a month alone, a range, a choice: text only
    month, day = _MONTHS[months[0].lower()], days[0]
    if years:
        return _safe_date(int(years[0]), month, day), text, None
    return _next_occurrence(month, day, today), text, None


# ── One lead ───────────────────────────────────────────────────────────
@dataclass
class CleanLead:
    name: str | None = None
    venue: str | None = None
    event_date: date | None = None
    event_date_text: str | None = None
    phone: str | None = None
    alt_phone: str | None = None
    phone_e164: str | None = None
    alt_phone_e164: str | None = None
    event_type: str | None = None
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def no_phone(self) -> bool:
        return self.phone_e164 is None and self.alt_phone_e164 is None

    @property
    def e164s(self) -> set[str]:
        return {e for e in (self.phone_e164, self.alt_phone_e164) if e}

    def values(self) -> dict:
        """What the preview sends back (the bulk save checks it again)."""
        return {"name": self.name, "venue": self.venue, "date": self.event_date_text, "phone": self.phone,
                "alt_phone": self.alt_phone, "event_type": self.event_type}


def _limited(lead: CleanLead, key: str, value: str | None, limit: int) -> str | None:
    if value is not None and len(value) > limit:
        lead.errors.append({"field": key, "message": f"Longer than {limit} characters"})
    return value


def clean_phones(lead: CleanLead, phone_value: Any, alt_value: Any) -> None:
    numbers = split_phones(phone_value) + split_phones(alt_value)
    if len(numbers) > 2:
        lead.errors.append({"field": "phone", "message": MSG_TOO_MANY_PHONES})
        numbers = numbers[:2]
    for key, number in zip(("phone", "alt_phone"), numbers):
        setattr(lead, key, _limited(lead, key, number, PHONE_MAX))
        parsed = phones.normalize(number)
        if parsed.error:
            lead.errors.append({"field": key, "message": parsed.error})
        else:
            setattr(lead, f"{key}_e164", parsed.e164)
    if lead.phone_e164 and lead.phone_e164 == lead.alt_phone_e164:
        lead.alt_phone, lead.alt_phone_e164 = None, None          # the same number twice


def clean_lead(raw: dict[str, Any], today: date) -> CleanLead:
    lead = CleanLead()
    lead.name = _limited(lead, "name", one_line(raw.get("name")), NAME_MAX)
    lead.venue = _limited(lead, "venue", one_line(raw.get("venue")), VENUE_MAX)
    lead.event_type = _limited(lead, "event_type", clean_event_type(raw.get("event_type")), EVENT_TYPE_MAX)
    lead.event_date, lead.event_date_text, error = parse_event_date(raw.get("date"), today)
    if error:
        lead.errors.append({"field": "date", "message": error})
    clean_phones(lead, raw.get("phone"), raw.get("alt_phone"))
    if not lead.name and not split_phones(raw.get("phone")) and not split_phones(raw.get("alt_phone")):
        lead.errors.append({"field": "name", "message": MSG_NAME_OR_PHONE})
    return lead


def is_template_example(lead: CleanLead) -> bool:
    e = TEMPLATE_EXAMPLE
    return (lead.name == e["name"] and lead.venue == e["venue"] and (lead.phone or "") == e["phone"])


def name_date_key(name: str | None, event_date: date | None, event_date_text: str | None) -> tuple | None:
    """Duplicate key for leads without a phone: the name (any case/spacing) and the event date
    (the date when known, else the date text)."""
    if not name:
        return None
    when = event_date.isoformat() if event_date else norm(event_date_text)
    return norm(name), when
