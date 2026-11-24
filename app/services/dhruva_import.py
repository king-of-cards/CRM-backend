"""Reading Dhruva -> KOC lead files and checking leads (upload preview, bulk save, add, edit).

Files: .xlsx (the sheet "Leads", else the first sheet) or .csv. The first row holds the headings,
exactly the template's (any case, spaces and punctuation ignored):

    Name, Phone numbers, Event date, Event type, Venue, WhatsApp status, Response,
    Message sent, Follow-up notes, Comments, Needs Dhruva intervention, Update, Quoted amount,
    Conversion amount, Follow-up date

Other columns (e.g. an export's Sl. No, Sent at, Hot) are ignored and reported. Values:

  name                required; whitespace collapsed
  phone numbers       optional; any number of them, comma-separated (also ";", "/" or line
                      breaks), 10 digits each (+91 / 91 / 0 in front and spaces are dropped);
                      repeats dropped, order kept
  event date          one or more real dates as YYYY-MM-DD, comma-separated ("2026-12-04, 2026-12-05");
                      an Excel date cell also works; sorted, repeats dropped
  update              a key (will_call_back) or its label (Will call back); empty / Pending = none
  amounts             rupees: 150000, 1,50,000, ₹1,50,000, 1.5 lakh
  needs intervention  Yes / No (empty = No)
  follow-up date      2026-12-04, 04-12-2026 or 4 Dec 2026
  other text          one line; notes, comments and response keep their line breaks
N/A-like values ("N/A", "-", "nil", …) are empty everywhere.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.models.dhruva import (
    EVENT_TYPE_MAX, MAX_EVENT_DATES, MAX_PHONES, MESSAGE_SENT_MAX, NAME_MAX, NOTES_MAX, VENUE_MAX,
    WHATSAPP_STATUS_MAX,
)

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
SHEET_NAME = "Leads"

# (field key, template heading), in template order
HEADINGS: tuple[tuple[str, str], ...] = (
    ("name", "Name"), ("phones", "Phone numbers"), ("event_dates", "Event date"),
    ("event_type", "Event type"), ("venue", "Venue"), ("whatsapp_status", "WhatsApp status"),
    ("response", "Response"), ("message_sent", "Message sent"), ("follow_up_notes", "Follow-up notes"),
    ("comments", "Comments"), ("needs_intervention", "Needs Dhruva intervention"), ("update_status", "Update"),
    ("quoted_amount", "Quoted amount"), ("conversion_amount", "Conversion amount"),
    ("follow_up_date", "Follow-up date"),
)
FIELDS = tuple(key for key, _ in HEADINGS)
LABELS = dict(HEADINGS)
TEXT_LIMITS = {"name": NAME_MAX, "venue": VENUE_MAX, "event_type": EVENT_TYPE_MAX,
               "whatsapp_status": WHATSAPP_STATUS_MAX, "message_sent": MESSAGE_SENT_MAX,
               "response": NOTES_MAX, "follow_up_notes": NOTES_MAX, "comments": NOTES_MAX}
MULTILINE = {"response", "follow_up_notes", "comments"}

MSG_NAME = "Name is required"
MSG_PHONE = "Each phone number must have 10 digits (comma-separated)"
MSG_DATES = "Use real dates as YYYY-MM-DD, comma-separated (e.g. 2026-12-04, 2026-12-05)"
TEMPLATE_EXAMPLE = {
    "name": "Example Name", "phones": "9876543210, 9876543211", "event_dates": "2026-12-04, 2026-12-05",
    "event_type": "Wedding", "venue": "Example Venue", "whatsapp_status": "Delivered", "response": "Interested",
    "message_sent": "Yes", "follow_up_notes": "Call after 6 pm", "comments": "", "needs_intervention": "No",
    "update_status": "Interested", "quoted_amount": "45000", "conversion_amount": "", "follow_up_date": "2026-11-25",
}
EXAMPLE_ROW_ERROR = {"field": "name", "message": "This is the example row from the template; delete it"}


class FileError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message, self.status_code = message, status_code


def norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


_HEADING_KEY = {norm(label): key for key, label in HEADINGS}
_IGNORED_SILENTLY = {"slno", "sl", "srno"}


# ── Files ──────────────────────────────────────────────────────────────
def _xlsx_rows(content: bytes, sheet_name: str | None) -> tuple[list[list[Any]], list[str], str]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        raise FileError("The file isn't a readable Excel (.xlsx) file.")
    names = wb.sheetnames
    wanted = sheet_name or SHEET_NAME
    match = next((n for n in names if n.strip().lower() == wanted.strip().lower()), None)
    if match is None:
        if sheet_name:
            raise FileError(f"No sheet named '{sheet_name}'. Sheets: {', '.join(names)}")
        match = names[0]
    ws = wb[match]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows, names, match


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
    sheet = None
    if content[:2] == b"PK" or name.endswith(".xlsx"):
        rows, sheets, sheet = _xlsx_rows(content, sheet_name)
    elif name.endswith((".csv", ".txt")) or not name:
        rows = _csv_rows(content)
    else:
        raise FileError("Upload an Excel (.xlsx) or CSV file.")
    if not rows or all(_blank(v) for v in rows[0]):
        raise FileError("The first row must hold the headings: " + ", ".join(LABELS.values()) + ".")
    mapping: dict[int, str] = {}
    columns: dict[str, str] = {}
    ignored: list[str] = []
    for i, heading in enumerate(rows[0]):
        if _blank(heading):
            continue
        key = _HEADING_KEY.get(norm(heading))
        written = str(heading).strip()
        if key and key not in mapping.values():
            mapping[i] = key
            columns[written] = key
        elif norm(heading) not in _IGNORED_SILENTLY:
            ignored.append(written)
    if "name" not in mapping.values():
        raise FileError("The file has no Name column. Use the template's headings: " + ", ".join(LABELS.values()) + ".")
    data = []
    for offset, row in enumerate(rows[1:], start=2):
        values = {key: (row[i] if i < len(row) else None) for i, key in mapping.items()}
        if all(_blank(v) for v in values.values()):
            continue
        data.append((offset, values))
        if len(data) > MAX_ROWS:
            raise FileError(f"The file has more than {MAX_ROWS:,} rows. Split it into smaller files.")
    return FileRows(sheet, sheets, 1, columns, ignored, data)


# ── Values ─────────────────────────────────────────────────────────────
def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _text(value: Any, multiline: bool) -> str | None:
    from app.services.lead_values import is_na

    raw = as_text(value)
    if multiline:
        text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in raw.strip().splitlines()).strip()
    else:
        text = re.sub(r"\s+", " ", raw).strip()
    return None if is_na(text) else text


def parse_phone(value: Any) -> tuple[str | None, str | None]:
    """-> (10 digits or None, error or None)."""
    from app.services.lead_values import is_na

    text = as_text(value).strip()
    if is_na(text):
        return None, None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10:
        return None, MSG_PHONE
    return digits, None


def parse_phones(value: Any) -> tuple[list[str], str | None]:
    """-> (10-digit numbers, repeats dropped, order kept; error or None). A list, one number, or
    text with the numbers separated by commas, ";", "/" or line breaks."""
    from app.services.lead_values import is_na

    items = value if isinstance(value, (list, tuple)) else [value]
    found: list[str] = []
    for item in items:
        text = as_text(item).strip()
        if is_na(text):
            continue
        for part in re.split(r"[,;/\n]+", text):
            if is_na(part.strip()):
                continue
            digits, problem = parse_phone(part)
            if problem:
                return [], problem
            if digits and digits not in found:
                found.append(digits)
    if len(found) > MAX_PHONES:
        return [], f"At most {MAX_PHONES} phone numbers"
    return found, None


_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_event_dates(value: Any) -> tuple[list[date], str | None]:
    """-> (sorted distinct dates, error or None). A list, one date, or comma-separated text."""
    from app.services.lead_values import is_na

    items = value if isinstance(value, (list, tuple)) else [value]
    found: set[date] = set()
    for item in items:
        if isinstance(item, datetime):
            found.add(item.date())
            continue
        if isinstance(item, date):
            found.add(item)
            continue
        text = as_text(item).strip()
        if is_na(text):
            continue
        for part in re.split(r"[,;\n]+", text):
            part = part.strip()
            if not part:
                continue
            m = _ISO_DATE.match(part)
            if not m:
                return [], MSG_DATES
            try:
                found.add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                return [], MSG_DATES
    if len(found) > MAX_EVENT_DATES:
        return [], f"At most {MAX_EVENT_DATES} event dates"
    return sorted(found), None


def phones_text(phones: list[str]) -> str | None:
    return ", ".join(phones) or None


def dates_text(dates: list[date]) -> str | None:
    return ", ".join(d.isoformat() for d in dates) or None


# ── One lead ───────────────────────────────────────────────────────────
@dataclass
class CleanLead:
    """A checked lead. present: the fields given (all of them for a new lead)."""
    name: str | None = None
    phones: list[str] = field(default_factory=list)
    event_dates: list[date] = field(default_factory=list)
    event_type: str | None = None
    venue: str | None = None
    whatsapp_status: str | None = None
    response: str | None = None
    message_sent: str | None = None
    follow_up_notes: str | None = None
    comments: str | None = None
    needs_intervention: bool = False
    update_status: str | None = None
    quoted_amount: int | None = None
    conversion_amount: int | None = None
    follow_up_date: date | None = None
    present: set[str] = field(default_factory=set)
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def no_phone(self) -> bool:
        return not self.phones

    @property
    def tails(self) -> set[str]:
        """The phones' 10 digits (the duplicate check)."""
        return set(self.phones)

    @property
    def first_date(self) -> date | None:
        return self.event_dates[0] if self.event_dates else None

    def values(self) -> dict:
        """What the preview sends back (the bulk save checks it again), in upload formats."""
        return {
            "name": self.name, "phones": phones_text(self.phones),
            "event_dates": dates_text(self.event_dates), "event_type": self.event_type, "venue": self.venue,
            "whatsapp_status": self.whatsapp_status, "response": self.response, "message_sent": self.message_sent,
            "follow_up_notes": self.follow_up_notes, "comments": self.comments,
            "needs_intervention": "Yes" if self.needs_intervention else "No", "update_status": self.update_status,
            "quoted_amount": self.quoted_amount, "conversion_amount": self.conversion_amount,
            "follow_up_date": self.follow_up_date.isoformat() if self.follow_up_date else None,
        }


def clean_lead(raw: dict[str, Any], keys: set[str] | None = None) -> CleanLead:
    """Check a lead. keys: only these fields (an edit); None = every field (a new lead: Name required)."""
    from app.services.crm_leads.fields import Invalid, parse_amount, parse_bool, parse_date, parse_update
    from app.services.lead_values import is_na

    wanted = set(FIELDS) if keys is None else set(keys) & set(FIELDS)
    lead = CleanLead(present=wanted)

    def error(key: str, message: str) -> None:
        lead.errors.append({"field": key, "message": message})

    for key in wanted:
        value = raw.get(key)
        try:
            if key in TEXT_LIMITS:
                text = _text(value, key in MULTILINE)
                if text is not None and len(text) > TEXT_LIMITS[key]:
                    raise Invalid(f"Longer than {TEXT_LIMITS[key]} characters")
                setattr(lead, key, text)
            elif key == "phones":
                numbers, problem = parse_phones(value)
                if problem:
                    raise Invalid(problem)
                lead.phones = numbers
            elif key == "event_dates":
                dates, problem = parse_event_dates(value)
                if problem:
                    raise Invalid(problem)
                lead.event_dates = dates
            elif key == "needs_intervention":
                lead.needs_intervention = False if is_na(as_text(value)) else parse_bool(value)
            elif key == "update_status":
                lead.update_status = parse_update(value)
            elif key in ("quoted_amount", "conversion_amount"):
                setattr(lead, key, None if is_na(as_text(value)) else parse_amount(value))
            elif key == "follow_up_date":
                lead.follow_up_date = None if is_na(as_text(value)) else parse_date(value)
        except Invalid as exc:
            error(key, str(exc))
    if "name" in wanted and not lead.name:
        error("name", MSG_NAME)
    return lead


def is_template_example(lead: CleanLead) -> bool:
    e = TEMPLATE_EXAMPLE
    return lead.name == e["name"] and lead.venue == e["venue"] and phones_text(lead.phones) == e["phones"]
