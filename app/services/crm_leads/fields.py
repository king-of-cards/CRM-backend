"""Fields of an added/imported lead, per source, and how each value is checked and cleaned.

The same definitions drive the add/edit forms (GET /crm-leads/{source}/fields), the Excel/CSV
template, the upload preview, the bulk import and the single add, so the rules can't drift.

Only the lead's own details are here. CRM fields (assigned vendor, interest, quote, converted,
notes) live in crm_lead_meta and are edited with PUT /leads/{source}/{lead_key}/crm.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.services import phones
from app.services.sheet_sync import parse_budget, parse_event_date

IST = timezone(timedelta(hours=5, minutes=30))
SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
SOURCE_LABELS = {
    "spin": "Spin leads",
    "whatsapp": "International (WhatsApp)",
    "wedding": "Wedding & event photography",
    "wedding_planner": "Wedding planner",
}


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    type: str = "text"     # text | email | phone | country | date | date_text | datetime | month
    #                        int | bool | range | budget | pincode | branch
    required: bool = False
    max_length: int | None = None
    aliases: tuple[str, ...] = ()        # other header names accepted in uploads
    detail: bool = False                 # spin: stored in additional_details under `key`
    help: str = ""
    example: Any = None

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "type": self.type, "required": self.required,
            "max_length": self.max_length, "help": self.help or None,
        }


PHONE_HELP_IN = "Indian numbers can be written without +91; other countries need their country code."

FIELDS: dict[str, list[FieldDef]] = {
    "spin": [
        FieldDef("phone", "Phone", "phone", True, 20, ("mobile", "phonenumber", "contactnumber", "whatsappnumber"),
                 help=PHONE_HELP_IN, example="9845012345"),
        FieldDef("name", "Name", max_length=150, aliases=("fullname",), example="Asha Rao"),
        FieldDef("email", "Email", "email", max_length=150, aliases=("emailaddress",), example="asha@example.com"),
        FieldDef("occasion", "Occasion", max_length=100, example="Wedding"),
        FieldDef("branch", "Branch", "branch", aliases=("store", "branchname"),
                 help="Branch name exactly as on the Branches page.", example="Jayanagar"),
        FieldDef("pincode", "Pincode", "pincode", detail=True, aliases=("pin", "zipcode", "postalcode"), example="560011"),
        FieldDef("occasionType", "Occasion type", max_length=100, detail=True, example="Wedding"),
        FieldDef("occasionDate", "Occasion date", "date", detail=True, aliases=("eventdate",), example="20-12-2026"),
        FieldDef("brideGroomName", "Bride/Groom name", max_length=150, detail=True),
        FieldDef("brideGroomPhone", "Bride/Groom phone", max_length=30, detail=True),
        FieldDef("eventVenue", "Event venue", max_length=300, detail=True),
        FieldDef("coverageDays", "Coverage days", max_length=50, detail=True),
        FieldDef("estimatedBudgetPerCard", "Budget per card", "range", max_length=100, detail=True,
                 help="A range such as 10-20.", example="10-20"),
        FieldDef("quantityRequired", "Quantity required", "int", detail=True),
        FieldDef("service", "Service", max_length=200, detail=True),
        FieldDef("serviceTypes", "Service types", max_length=300, detail=True),
        FieldDef("serviceBudget", "Service budget", "range", max_length=100, detail=True),
        FieldDef("serviceQuantity", "Service quantity", "int", detail=True),
        FieldDef("otherWeddingServices", "Other wedding services", max_length=500, detail=True, example="Photography"),
    ],
    "whatsapp": [
        FieldDef("phone", "Phone", "phone", True, 32, ("whatsapp", "whatsappnumber", "mobile", "phonenumber", "number"),
                 help="With the country code (e.g. +971 50 123 4567), or fill in Country.", example="+971501234567"),
        FieldDef("country", "Country", "country", max_length=100, example="United Arab Emirates"),
        FieldDef("event_date", "Event date", "date_text", max_length=100, aliases=("weddingdate",),
                 help="Kept as written; read as a date when it is clear.", example="20-12-2026"),
        FieldDef("submitted_at", "Submitted", "datetime", aliases=("submittedat", "submissiontime", "timestamp"),
                 help="Date and time in India time. Empty = now."),
    ],
    "wedding": [
        FieldDef("phone", "Phone", "phone", True, 50, ("contactnumber", "mobile", "phonenumber"),
                 help=PHONE_HELP_IN, example="9845012345"),
        FieldDef("name", "Name", max_length=200, aliases=("fullname",), example="Ravi Kumar"),
        FieldDef("email", "Email", "email", max_length=254, aliases=("emailaddress",), example="ravi@example.com"),
        FieldDef("city", "City", max_length=200, aliases=("cityeventlocation", "eventlocation", "location"), example="Bangalore"),
        FieldDef("occasion", "Occasion", max_length=100, aliases=("whatistheoccasion",), example="Wedding"),
        FieldDef("event_date", "Event date", "date_text", max_length=100, example="20-12-2026"),
        FieldDef("budget", "Budget", "budget", max_length=100,
                 aliases=("photographybudget", "whatisyourapproximatephotographybudget"),
                 help="E.g. 50000, 50k or 1.5 lakh.", example="1.5 lakh"),
        FieldDef("submitted_at", "Submitted", "datetime", aliases=("timestamp", "submittedat"),
                 help="Date and time in India time. Empty = now."),
    ],
    "wedding_planner": [
        FieldDef("phone", "Phone", "phone", True, 32, ("whatsappnumber", "contactnumber", "mobile", "phonenumber"),
                 help=PHONE_HELP_IN, example="9845012345"),
        FieldDef("name", "Name", max_length=200, aliases=("fullname",), example="Meera"),
        FieldDef("email", "Email", "email", max_length=254, aliases=("emailaddress",)),
        FieldDef("role", "You are the", max_length=100, aliases=("role",), example="Bride"),
        FieldDef("wedding_type", "Wedding type", max_length=100),
        FieldDef("city", "Wedding city", max_length=200, aliases=("weddingcitydestination", "city")),
        FieldDef("home_city", "Family home city", max_length=200, aliases=("homecity",)),
        FieldDef("place_kind", "Kind of place", max_length=200, aliases=("kindofplaceifnotsure",)),
        FieldDef("wedding_month", "Wedding month", "month", max_length=50, help="E.g. December 2027.", example="December 2027"),
        FieldDef("first_day", "First day of celebrations", "date", aliases=("firstday",), example="20-12-2027"),
        FieldDef("total_guests", "Total guests", "int", example=300),
        FieldDef("guests_staying", "Guests staying", "int", aliases=("guestsstayingneedingrooms",)),
        FieldDef("traditions", "Traditions", max_length=300),
        FieldDef("food", "Food", max_length=100),
        FieldDef("alcohol", "Alcohol", max_length=50),
        FieldDef("budget_range", "Budget range", max_length=100),
        FieldDef("total_budget", "Total budget", "int", example=2500000),
        FieldDef("budget_includes_jewellery", "Budget includes jewellery and outfits", "bool",
                 aliases=("budgetincludesjewelleryandoutfits",), help="Yes or No."),
        FieldDef("jewellery_outfits", "Jewellery and outfits", "int", aliases=("jewelleryandoutfits",)),
        FieldDef("priority1", "Priority 1", max_length=100),
        FieldDef("priority2", "Priority 2", max_length=100),
        FieldDef("priority3", "Priority 3", max_length=100),
        FieldDef("shortlisted_vendors", "Shortlisted vendors", max_length=5000),
        FieldDef("consent_share", "Consent: share plan", "bool", aliases=("consentshareplanwithkingofcards",), help="Yes or No."),
        FieldDef("consent_offers", "Consent: offers on WhatsApp", "bool", aliases=("consentoffersonwhatsapp",), help="Yes or No."),
    ],
}


def norm_header(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def header_map(source: str) -> dict[str, str]:
    """Normalised header -> field key (key, label and aliases all accepted)."""
    out: dict[str, str] = {}
    for f in FIELDS[source]:
        for name in (f.label, f.key, *f.aliases):
            out.setdefault(norm_header(name), f.key)
    return out


# ── Value parsing ───────────────────────────────────────────────────────
class Invalid(ValueError):
    pass


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MONTHS = ("%B %Y", "%b %Y", "%m/%Y", "%m-%Y", "%Y-%m")
_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y",
                 "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y")
_DATETIME_FORMATS = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                     "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def as_text(value: Any) -> str:
    """Upload/JSON value -> trimmed text. Excel numbers like 9845012345.0 lose the .0."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y %H:%M") if value.time() != time() else value.strftime("%d-%m-%Y")
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    return str(value).strip()


def parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", as_text(value), flags=re.I)
    text = re.sub(r"\s+", " ", text)
    if re.match(r"^\d{4}-\d{2}-\d{2}T", text):          # ISO datetime from JSON
        text = text[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise Invalid("Not a date (use DD-MM-YYYY)")


def parse_datetime(value: Any) -> datetime:
    """-> aware UTC. Values without a time zone (typed or from Excel) are India time."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time())
    else:
        text = as_text(value)
        dt = None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in _DATETIME_FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            dt = datetime.combine(parse_date(text), time())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def parse_month(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%B %Y")
    text = as_text(value)
    for fmt in _MONTHS:
        try:
            return datetime.strptime(text, fmt).strftime("%B %Y")
        except ValueError:
            continue
    raise Invalid("Not a month (e.g. December 2027)")


def parse_int(value: Any) -> int:
    if isinstance(value, bool):
        raise Invalid("Not a whole number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise Invalid("Not a whole number")
        number = int(value)
    else:
        text = as_text(value).replace(",", "").replace(" ", "")
        if not re.fullmatch(r"\d+", text):
            raise Invalid("Not a whole number")
        number = int(text)
    if number < 0 or number > 10**12:
        raise Invalid("Out of range")
    return number


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = as_text(value).lower()
    if text in ("yes", "y", "true", "1"):
        return True
    if text in ("no", "n", "false", "0"):
        return False
    raise Invalid("Use Yes or No")


@dataclass
class RowContext:
    """What validation needs from the database (branch names) and from the row (country)."""
    branches_by_name: dict[str, int] = field(default_factory=dict)   # norm_header(name) -> id
    branch_ids: set[int] = field(default_factory=set)


@dataclass
class Cleaned:
    values: dict[str, Any]                 # field key -> clean Python value (None = empty)
    errors: list[dict]                     # [{"field": key, "message": ...}]
    phone: phones.Phone | None = None
    phone_input: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


def clean_row(source: str, raw: dict[str, Any], ctx: RowContext, *, partial: bool = False) -> Cleaned:
    """Check and clean one lead. partial=True (edit): only the fields present in `raw`."""
    values: dict[str, Any] = {}
    errors: list[dict] = []
    phone: phones.Phone | None = None
    phone_input = ""
    defs = FIELDS[source]
    country = as_text(raw.get("country")) if source == "whatsapp" else None

    for f in defs:
        if partial and f.key not in raw:
            continue
        value = raw.get(f.key)
        text = as_text(value)
        if f.type == "branch" and isinstance(value, int) and not isinstance(value, bool):
            text = str(value)
        if not text:
            if f.required:
                errors.append({"field": f.key, "message": f"{f.label} is required"})
            values[f.key] = None
            continue
        try:
            if f.max_length and f.type in ("text", "email", "country", "date_text", "range", "budget", "phone") \
                    and len(text) > f.max_length:
                raise Invalid(f"Longer than {f.max_length} characters")
            if f.type in ("text", "country", "range"):
                values[f.key] = text
            elif f.type == "email":
                if not _EMAIL_RE.match(text):
                    raise Invalid("Not a valid email address")
                values[f.key] = text.lower()
            elif f.type == "phone":
                phone_input = text
                phone = phones.normalize(text, international=source == "whatsapp", country=country)
                if phone.error:
                    raise Invalid(phone.error)
                values[f.key] = text
            elif f.type == "date":
                values[f.key] = parse_date(value)
            elif f.type == "date_text":
                values[f.key] = text                       # parsed date added in to_model_kwargs
            elif f.type == "datetime":
                values[f.key] = parse_datetime(value)
            elif f.type == "month":
                values[f.key] = parse_month(value)
            elif f.type == "int":
                values[f.key] = parse_int(value)
            elif f.type == "bool":
                values[f.key] = parse_bool(value)
            elif f.type == "budget":
                values[f.key] = text
            elif f.type == "pincode":
                digits = re.sub(r"\D", "", text)
                if len(digits) != 6:
                    raise Invalid("Pincode must have 6 digits")
                values[f.key] = digits
            elif f.type == "branch":
                if isinstance(value, int) and not isinstance(value, bool):
                    if value not in ctx.branch_ids:
                        raise Invalid("Unknown branch")
                    values[f.key] = value
                else:
                    branch_id = ctx.branches_by_name.get(norm_header(text))
                    if branch_id is None:
                        raise Invalid(f"Unknown branch '{text}' (use the name shown on the Branches page)")
                    values[f.key] = branch_id
            else:  # pragma: no cover
                values[f.key] = text
        except Invalid as exc:
            errors.append({"field": f.key, "message": str(exc)})
    return Cleaned(values, errors, phone, phone_input)


def field_defs(source: str) -> dict[str, FieldDef]:
    return {f.key: f for f in FIELDS[source]}


# ── Clean values <-> model columns ─────────────────────────────────────
def to_model_kwargs(source: str, values: dict[str, Any], phone: phones.Phone | None) -> dict[str, Any]:
    """Clean form values -> column values for the source's table (only the keys present)."""
    out: dict[str, Any] = {}
    if source == "spin":
        details = {}
        for f in FIELDS["spin"]:
            if f.key not in values:
                continue
            v = values[f.key]
            if f.key == "branch":
                out["branch_id"] = v
            elif f.detail:
                details[f.key] = v.isoformat() if isinstance(v, date) else v
            elif f.key == "phone":
                out["phone"] = phone.national_in
                out["phone_e164"] = phone.e164
            else:
                out[f.key] = v
        out["_details"] = details
        return out

    for key, v in values.items():
        if key == "phone":
            out["phone"] = phone.digits                        # same display form as the sync
            out["phone_e164"] = phone.e164
            if source == "wedding":
                out["phone_raw"] = v
        elif key == "event_date":
            out["event_date_text"] = v
            out["event_date"] = parse_event_date(v or "") if v else None
            if v and out["event_date"] is None:
                try:
                    out["event_date"] = parse_date(v)
                except Invalid:
                    pass
        elif key == "budget":
            out["budget_text"] = v
            out["budget_amount"] = parse_budget(v or "") if v else None
        elif key in ("priority1", "priority2", "priority3"):
            out.setdefault("_priorities", {})[key] = v
        else:
            out[key] = v
    if source == "wedding_planner" and ("first_day" in values or "wedding_month" in values):
        out["_wedding_date"] = True
    return out


def from_model(source: str, lead) -> dict[str, Any]:
    """Model -> form values (JSON-friendly), for the edit form and API answers."""
    def j(v):
        return v.isoformat() if isinstance(v, (date, datetime)) else v

    if source == "spin":
        details = lead.additional_details or {}
        out = {}
        for f in FIELDS["spin"]:
            if f.key == "branch":
                out["branch"] = lead.branch_id
            elif f.detail:
                out[f.key] = details.get(f.key)
            else:
                out[f.key] = j(getattr(lead, f.key))
        return out
    out = {}
    for f in FIELDS[source]:
        if f.key == "phone":
            out["phone"] = lead.phone_raw if source == "wedding" and lead.phone_raw else (f"+{lead.phone}" if lead.phone else None)
        elif f.key == "event_date":
            out["event_date"] = lead.event_date_text
        elif f.key == "budget":
            out["budget"] = lead.budget_text
        elif f.key in ("priority1", "priority2", "priority3"):
            prios = list(lead.priorities or [])
            idx = int(f.key[-1]) - 1
            out[f.key] = prios[idx] if idx < len(prios) else None
        else:
            out[f.key] = j(getattr(lead, f.key))
    return out


def json_values(values: dict[str, Any]) -> dict[str, Any]:
    """Clean values -> what the preview sends back (bulk save re-checks them)."""
    out = {}
    for k, v in values.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, date):
            out[k] = v.strftime("%d-%m-%Y")
        else:
            out[k] = v
    return out
