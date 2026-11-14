"""Export columns per source: the list rows (as the list endpoints return them, with their "crm"
details) turned into a table. Dates become real dates, ISO times become India time when written."""
import re
from datetime import date, datetime, timezone
from typing import Any, Callable

from app.services.crm_leads.fields import FIELDS

Col = tuple[str, Callable[[dict], Any], str]          # (header, getter, kind: text | phone | date | dt | value)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)      # stored times are UTC


def _date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _crm(item: dict, key: str) -> Any:
    return (item.get("crm") or {}).get(key)


CRM_COLUMNS: list[Col] = [
    ("Assigned to", lambda i: (_crm(i, "vendor") or {}).get("name"), "text"),
    ("Assigned how", lambda i: _crm(i, "vendor_source"), "text"),
    ("Suggested vendor", lambda i: (_crm(i, "suggested_vendor") or {}).get("name"), "text"),
    ("Interest", lambda i: _crm(i, "interest"), "text"),
    ("Converted", lambda i: bool(_crm(i, "converted")), "value"),
    ("Converted at", lambda i: _crm(i, "converted_at"), "dt"),
    ("Quoted date", lambda i: _crm(i, "quoted_date"), "date"),
    ("Quoted amount", lambda i: _crm(i, "quoted_amount"), "value"),
    ("Notes", lambda i: _crm(i, "notes"), "text"),
    ("CRM updated", lambda i: _crm(i, "updated_at"), "dt"),
]


def _g(key: str) -> Callable[[dict], Any]:
    return lambda i: i.get(key)


SHEET_COLUMNS: dict[str, list[Col]] = {
    "whatsapp": [
        ("Lead key", _g("lead_key"), "text"),
        ("Origin", _g("origin"), "text"),
        ("Phone", _g("phone_display"), "phone"),
        ("Country", _g("country"), "text"),
        ("Event date (as written)", _g("event_date_text"), "text"),
        ("Event date", _g("event_date"), "date"),
        ("Submitted", _g("submitted_at"), "dt"),
        ("First seen", _g("first_seen_at"), "dt"),
    ],
    "wedding": [
        ("Lead key", _g("lead_key"), "text"),
        ("Origin", _g("origin"), "text"),
        ("Name", _g("name"), "text"),
        ("Phone", _g("phone_display"), "phone"),
        ("Email", _g("email"), "text"),
        ("City", _g("city"), "text"),
        ("Occasion", _g("occasion"), "text"),
        ("Event date (as written)", _g("event_date_text"), "text"),
        ("Event date", _g("event_date"), "date"),
        ("Budget (as written)", _g("budget_text"), "text"),
        ("Budget amount", _g("budget_amount"), "value"),
        ("Leads shared", _g("shared_with"), "text"),
        ("Repeat", _g("is_repeat"), "value"),
        ("Submitted", _g("submitted_at"), "dt"),
    ],
    "wedding_planner": [
        ("Plan ID", _g("plan_id"), "text"),
        ("Origin", _g("origin"), "text"),
        ("Last step", _g("last_step"), "text"),
        ("Updated", _g("updated_at"), "dt"),
        ("Name", _g("name"), "text"),
        ("Phone", _g("phone_display"), "phone"),
        ("Email", _g("email"), "text"),
        ("You are the", _g("role"), "text"),
        ("Wedding type", _g("wedding_type"), "text"),
        ("Wedding city", _g("city"), "text"),
        ("Family home city", _g("home_city"), "text"),
        ("Kind of place", _g("place_kind"), "text"),
        ("Wedding month", _g("wedding_month"), "text"),
        ("First day", _g("first_day"), "date"),
        ("Wedding date", _g("wedding_date"), "date"),
        ("Total guests", _g("total_guests"), "value"),
        ("Guests staying", _g("guests_staying"), "value"),
        ("Traditions", _g("traditions"), "text"),
        ("Food", _g("food"), "text"),
        ("Alcohol", _g("alcohol"), "text"),
        ("Budget range", _g("budget_range"), "text"),
        ("Total budget", _g("total_budget"), "value"),
        ("Budget includes jewellery and outfits", _g("budget_includes_jewellery"), "value"),
        ("Jewellery and outfits", _g("jewellery_outfits"), "value"),
        ("Plan estimate", _g("plan_estimate"), "value"),
        ("Fits budget", _g("fits_budget"), "value"),
        ("Priorities", lambda i: ", ".join(i.get("priorities") or []) or None, "text"),
        ("Shortlisted vendors", _g("shortlisted_vendors"), "text"),
        ("Leads shared", _g("shared_with"), "text"),
    ],
}

_SPIN_LABELS = {f.key: f.label for f in FIELDS["spin"]}
_SPIN_LABELS.update({
    "id": "Lead key", "created_at": "Created", "consent_at": "Consent at", "campaign_id": "Campaign id",
    "customer_id": "Customer id", "branch_id": "Branch id", "source": "Lead source",
})
_SPIN_TEXT = {"phone", "brideGroomPhone", "pincode", "id"}


def spin_columns(columns_meta: list[dict]) -> list[Col]:
    cols: list[Col] = [("Origin", _g("origin"), "text"), ("Branch", lambda i: (i.get("branch") or {}).get("name"), "text")]
    for meta in columns_meta:
        name = meta["name"]
        short = meta.get("key") or name
        label = _SPIN_LABELS.get(short) or short.replace("_", " ").capitalize()
        type_name = str(meta.get("type", "")).upper()
        if short in _SPIN_TEXT:
            kind = "phone" if "phone" in short.lower() else "text"
        elif "TIMESTAMP" in type_name:
            kind = "dt"
        elif type_name == "DATE":
            kind = "date"
        elif meta.get("json_field"):
            kind = "iso_date_or_value"
        else:
            kind = "value"
        cols.append((label, _g(name), kind))
    return cols


def build(columns: list[Col], items: list[dict]) -> tuple[list[str], list[list[Any]], set[int]]:
    """-> (headers, rows, indexes of text-only columns)."""
    cols = columns + CRM_COLUMNS
    headers = [c[0] for c in cols]
    text_cols = {i for i, c in enumerate(cols) if c[2] in ("phone", "text")}
    rows = []
    for item in items:
        row = []
        for _, get, kind in cols:
            value = get(item)
            if kind == "dt":
                value = _dt(value)
            elif kind == "date":
                value = _date(value)
            elif kind == "iso_date_or_value" and isinstance(value, str) and _ISO_DATE.match(value):
                value = _date(value)
            elif kind in ("text", "phone") and value is not None:
                value = str(value)
            row.append(value)
        rows.append(row)
    return headers, rows, text_cols
