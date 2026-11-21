"""Export (and template) columns per source: each list screen flattened into columns.

The identity cell becomes Lead (+ its subtitle), the contact cell Contact, then the screen's
columns in order (a cell with two lines becomes two columns), the CRM columns, and the trailing
column. Values are what the screen shows:
  dates        13 Dec 2026           (xlsx: date cells, "dd mmm yyyy")
  date-times   14 Nov 2026, 10:30 am (India time; xlsx: datetime cells)
  phones       +919845012345         (text)
  money        full rupees           (xlsx: number cells with a ₹ format)
  Assigned to  the chip text: "Phodeo", "Phodeo · auto" or "Unassigned" (admins only)
The template and the upload parser use the same headers (see fields.py), so an exported file
can be uploaded back.
"""
import re
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

from app.models.vendor_to_koc import STATUS_LABELS as UPDATE_LABELS   # None -> "Pending"
from app.services import phones
from app.services.lead_crm import assigned_label
from app.services.stores import pincode_area, store_for

Col = tuple[str, Callable[[dict], Any], str]   # (header, getter, kind: text | phone | date | datetime | money | int | yesno)


def to_datetime(value: Any) -> datetime | None:
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


def to_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def date_or_text(value: Any, fallback: Any = None) -> Any:
    """A date when the value is an ISO date, else the text as shown (e.g. "December 2027")."""
    return to_date(value) or (fallback if fallback not in ("", None) else None)


def e164(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    if text.startswith("+"):
        return text
    return phones.normalize(text, lenient=True).e164 or text


def _crm(item: dict, key: str) -> Any:
    return (item.get("crm") or {}).get(key)


def _g(key: str) -> Callable[[dict], Any]:
    return lambda i: i.get(key)


ASSIGNED_HEADER = "Assigned to"         # left out of vendor logins' exports
HOT_COLUMN: Col = ("Hot", lambda i: bool(i.get("is_hot")), "yesno")
CRM_COLUMNS: list[Col] = [
    (ASSIGNED_HEADER, lambda i: assigned_label(i.get("crm")), "text"),
    ("Update", lambda i: UPDATE_LABELS.get(_crm(i, "update_status"), _crm(i, "update_status")), "text"),
    ("Quoted amount", lambda i: _crm(i, "quoted_amount"), "money"),
    ("Quoted date", lambda i: to_date(_crm(i, "quoted_date")), "date"),
    ("Conversion amount", lambda i: _crm(i, "conversion_amount"), "money"),
]


def _event(i: dict) -> Any:
    return date_or_text(i.get("event_date"), i.get("event_date_text"))


SHEET_COLUMNS: dict[str, list[Col]] = {
    "whatsapp": [
        ("Lead", lambda i: e164(i.get("phone_display")), "phone"),
        ("Country", _g("country"), "text"),
        ("Event date", _event, "date"),
        *CRM_COLUMNS,
        ("Submitted", lambda i: to_datetime(i.get("submitted_at")), "datetime"),
    ],
    "wedding": [
        ("Lead", _g("name"), "text"),
        ("Email", _g("email"), "text"),
        ("Contact", _g("phone_display"), "phone"),           # +<digits with country code>
        ("Occasion", _g("occasion"), "text"),
        ("Event date", _event, "date"),
        ("City", _g("city"), "text"),
        ("Budget", lambda i: i.get("budget_amount") if i.get("budget_amount") is not None else i.get("budget_text"), "money"),
        *CRM_COLUMNS,
        ("Submitted", lambda i: to_datetime(i.get("submitted_at")), "datetime"),
    ],
    "wedding_planner": [
        ("Lead", _g("name"), "text"),
        ("Role", _g("role"), "text"),
        ("Email", _g("email"), "text"),
        ("Contact", _g("phone_display"), "phone"),
        ("Wedding date", lambda i: date_or_text(i.get("first_day"), i.get("wedding_month")), "date"),
        ("Wedding city", _g("city"), "text"),
        ("Guests", _g("total_guests"), "int"),
        ("Guests staying", _g("guests_staying"), "int"),
        ("Budget", _g("total_budget"), "money"),
        ("Budget range", _g("budget_range"), "text"),
        ("Estimate", _g("plan_estimate"), "money"),
        ("Fits budget", _g("fits_budget"), "yesno"),
        *CRM_COLUMNS,
        ("Updated", lambda i: to_datetime(i.get("updated_at")), "datetime"),
        ("Last step", _g("last_step"), "text"),
    ],
}

# ── Spin: which list column is which (same matching as the spin screen) ─
_SPIN_FIELDS = {
    "name": ["name", "fullname", "bridegroomname", "customername", "leadname", "clientname", "contactname"],
    "phone": ["phone", "phonenumber", "mobile", "mobilenumber", "bridegroomphone", "contactnumber", "whatsapp",
              "whatsappnumber"],
    "email": ["email", "emailaddress", "customeremail"],
    "occasion": ["occasiontype", "eventtype", "occasion"],
    "event_date": ["occasiondate", "eventdate", "weddingdate"],
    "pincode": ["pincode", "pin", "zipcode"],
    "city": ["city", "location", "eventcity"],
    "budget": ["estimatedbudgetpercard", "servicebudget", "budget"],
    "other_services": ["otherweddingservices"],
    "quantity": ["quantityrequired"],
    "created": ["createdat", "created", "createdon", "submittedat"],
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"^additional_details\.", "", str(name).lower()))


def spin_fields(columns: list[dict]) -> dict[str, dict | None]:
    """{"name": column meta, ...} like the spin screen's pick()."""
    by_norm: dict[str, dict] = {}
    for c in columns:
        by_norm.setdefault(_norm(c["name"]), c)
    out = {f: next((by_norm[c] for c in candidates if c in by_norm), None) for f, candidates in _SPIN_FIELDS.items()}
    if out["name"] is None:
        out["name"] = next((c for c in columns if "name" in _norm(c["name"])), None)
    if out["phone"] is None:
        out["phone"] = next((c for c in columns if re.search("phone|mobile", _norm(c["name"]))), None)
    return out


def _spin_budget(value: Any, meta: dict | None) -> Any:
    """₹10–20 for ranges that start with a number, as the screen shows them."""
    if value in (None, ""):
        return None
    text = str(value)
    if meta and meta.get("type") == "range" and re.match(r"^\d", text):
        return "₹" + re.sub(r"[-_]", "–", text)
    return text


def spin_columns(columns_meta: list[dict], branches: list[dict] | None = None) -> list[Col]:
    """branches: the Branches page's branches ({id, name, code, city}); the 12 built-in stores
    fill in the rest, exactly like the spin screen. Unknown branch ids give empty cells."""
    f = spin_fields(columns_meta)

    def val(item: dict, field: str) -> Any:
        meta = f[field]
        return item.get(meta["name"]) if meta else None

    def store(item: dict) -> dict:
        branch_id = (item.get("branch") or {}).get("id")
        return store_for(branch_id, branches) or {}

    def branch_code(item: dict) -> str | None:
        s = store(item)
        return " · ".join(p for p in (s.get("code"), s.get("city")) if p) or None      # HYB-007 · Hyderabad

    def location(item: dict) -> str | None:
        pin = str(val(item, "pincode")) if val(item, "pincode") else None
        city = str(val(item, "city")) if val(item, "city") else None
        area = city or pincode_area(pin) or (None if pin else store(item).get("city"))
        return " · ".join(p for p in (area, pin) if p) or None                        # Hyderabad · 500084

    return [
        ("Lead", lambda i: val(i, "name"), "text"),
        ("Email", lambda i: val(i, "email"), "text"),
        ("Contact", lambda i: e164(val(i, "phone")), "phone"),
        ("Branch", lambda i: store(i).get("name"), "text"),
        ("Branch code", branch_code, "text"),
        ("Location", location, "text"),
        ("Occasion", lambda i: val(i, "occasion"), "text"),
        ("Event date", lambda i: date_or_text(val(i, "event_date"), val(i, "event_date")), "date"),
        ("Budget", lambda i: _spin_budget(val(i, "budget"), f["budget"]), "text"),
        ("Other wedding services", lambda i: val(i, "other_services"), "text"),
        ("Quantity required", lambda i: val(i, "quantity"), "text"),
        *CRM_COLUMNS,
        ("Created", lambda i: to_datetime(val(i, "created")), "datetime"),
    ]


# KOC -> Dhruva upload template: its export's columns (koc_to_dhruva.EXPORT_COLUMNS) without "Sl. No"
KOC_TEMPLATE_COLUMNS: list[tuple[str, str]] = [
    ("Name", "text"), ("Phone", "phone"), ("Email", "text"), ("Occasion", "text"), ("Event date", "date"),
    ("Branch", "text"), ("Branch code", "text"), ("Estimated budget per card", "text"), ("Quantity required", "int"),
    ("Received", "datetime"), ("Update", "text"), ("Additional comments", "text"), ("Quoted amount", "money"),
    ("Quoted date", "date"), ("Conversion amount", "money"), ("Follow-up date", "date"), ("Hot", "yesno"),
]


def headers(source: str) -> list[str]:
    """The column headers of a source's export and template."""
    if source == "koc_to_dhruva":
        return [h for h, _ in KOC_TEMPLATE_COLUMNS]
    if source == "spin":
        return [c[0] for c in spin_columns([])]
    return [c[0] for c in SHEET_COLUMNS[source]]


def kinds(source: str) -> list[str]:
    if source == "koc_to_dhruva":
        return [k for _, k in KOC_TEMPLATE_COLUMNS]
    cols = spin_columns([]) if source == "spin" else SHEET_COLUMNS[source]
    return [c[2] for c in cols]


def build(columns: list[Col], items: list[dict]) -> tuple[list[str], list[list[Any]], list[str]]:
    """-> (headers, rows, kind per column)."""
    return [c[0] for c in columns], [[get(item) for _, get, _ in columns] for item in items], [c[2] for c in columns]
