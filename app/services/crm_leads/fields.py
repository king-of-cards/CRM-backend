"""Fields of an added/imported lead, per source, and how each value is checked and cleaned.

The same definitions drive the add/edit forms (GET /crm-leads/{source}/fields), the upload
preview, the bulk import and the single add. Labels are the list screens' column headers (the
export and template use the same headers, see layout.py), so an exported file uploads back.
Older labels are still accepted as aliases, and columns that aren't on the screens (extra spin
details, planner details) are accepted as optional extras.

Only the lead's own details are stored on the lead. "Assigned to" is read here but saved in
crm_lead_meta (a vendor name = manual, "<vendor> · auto" = automatic, empty = unassigned for
uploads). Uploads (not the add/edit forms) also read Update, Quoted amount and Conversion amount
for new leads (UPLOAD_CRM_FIELDS); they are saved in crm_lead_meta too. Quoted date is never read.

Only the phone is required. N/A-like values ("N/A", "-", "nil", "none", ".", …: lead_values.is_na)
are empty, never invalid; an empty Created / Submitted / Updated means the upload time. Other
unreadable values (an email without a domain, a date that isn't one) stay row errors.

KOC -> Dhruva (koc_to_dhruva) uploads use the same machinery (FIELDS / KOC_CRM_FIELDS); they are
stored in crm_koc_dhruva_leads with the same columns as crm_spin_leads (DETAIL_SOURCES).
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Any

from app.services import lead_values, phones
from app.services.sheet_sync import parse_budget, parse_event_date

IST = timezone(timedelta(hours=5, minutes=30))
SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
# Sources stored in crm_spin_leads (the workflow spin_lead table's leads)
SPIN_SOURCES = ("spin",)
KOC = "koc_to_dhruva"
# Sources whose CRM rows keep the lead's details in additional_details (crm_spin_leads, crm_koc_dhruva_leads)
DETAIL_SOURCES = ("spin", KOC)
SOURCE_LABELS = {
    "spin": "Spin leads",
    KOC: "KOC → Dhruva",
    "whatsapp": "International (WhatsApp)",
    "wedding": "Wedding & event photography",
    "wedding_planner": "Wedding planner",
}


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    type: str = "text"     # text | email | phone | country | date | event_date | datetime | month | int
    #                        money | bool | range | budget | pincode | branch | assigned | location | wedding_when
    required: bool = False
    max_length: int | None = None
    aliases: tuple[str, ...] = ()        # other header names accepted in uploads
    detail: bool = False                 # spin: stored in additional_details under `key`
    help: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "type": self.type, "required": self.required,
            "max_length": self.max_length, "help": self.help or None,
        }


PHONE_HELP_IN = "Indian numbers can be written without +91; other countries need their country code."
ASSIGNED_HELP = ('A vendor name (e.g. Phodeo) assigns the lead to that vendor; several vendors separated by '
                 'commas (e.g. "Phodeo, Kraftyz") give it to each of them. "Automatic" (or "Auto", or '
                 '"Phodeo · auto") leaves it to the automatic rules. Empty or Unassigned: the lead stays '
                 "unassigned until an admin assigns it.")
DATE_HELP = "13 Dec 2026 or 13-12-2026."
DATETIME_HELP = "14 Nov 2026, 10:30 am or 14-11-2026 10:30 (India time); the time is optional. Empty = now."
ASSIGNED = FieldDef("assigned_to", "Assigned to", "assigned", aliases=("assignedto", "vendor"), help=ASSIGNED_HELP)

FIELDS: dict[str, list[FieldDef]] = {
    "spin": [
        FieldDef("name", "Lead", max_length=150, aliases=("name", "fullname")),
        FieldDef("email", "Email", "email", max_length=150, aliases=("emailaddress",)),
        FieldDef("phone", "Contact", "phone", True, 20,
                 ("phone", "mobile", "phonenumber", "contactnumber", "whatsappnumber"), help=PHONE_HELP_IN),
        FieldDef("branch", "Branch", "branch", aliases=("store", "branchname"),
                 help="Branch name exactly as on the Branches page."),
        FieldDef("location", "Location", "location", help="The 6-digit pincode in it is saved (e.g. Bangalore · 560011)."),
        FieldDef("occasionType", "Occasion", max_length=100, detail=True, aliases=("occasiontype",)),
        FieldDef("occasionDate", "Event date", "date", detail=True, aliases=("occasiondate",), help=DATE_HELP),
        FieldDef("estimatedBudgetPerCard", "Budget", "range", max_length=100, detail=True,
                 aliases=("budgetpercard", "estimatedbudgetpercard"), help="A range such as ₹10–20 or 10-20."),
        ASSIGNED,
        FieldDef("created_at", "Created", "datetime", aliases=("createdat",), help=DATETIME_HELP),
        # Extra columns (not on the list screen; optional)
        FieldDef("pincode", "Pincode", "pincode", detail=True, aliases=("pin", "zipcode", "postalcode")),
        FieldDef("occasion", "Lead occasion", max_length=100),
        FieldDef("brideGroomName", "Bride/Groom name", max_length=150, detail=True),
        FieldDef("brideGroomPhone", "Bride/Groom phone", max_length=30, detail=True),
        FieldDef("eventVenue", "Event venue", max_length=300, detail=True),
        FieldDef("coverageDays", "Coverage days", max_length=50, detail=True),
        FieldDef("quantityRequired", "Quantity required", "int", detail=True),
        FieldDef("service", "Service", max_length=200, detail=True),
        FieldDef("serviceTypes", "Service types", max_length=300, detail=True),
        FieldDef("serviceBudget", "Service budget", "range", max_length=100, detail=True),
        FieldDef("serviceQuantity", "Service quantity", "int", detail=True),
        FieldDef("otherWeddingServices", "Other wedding services", max_length=500, detail=True,
                 help='E.g. "Photography & Videography, Wedding Gifts & Return Gifts". Leads naming '
                      '"Wedding Gifts & Return Gifts" go to the gifts vendor (Kraftyz) when Assigned to is Automatic.'),
    ],
    "whatsapp": [
        FieldDef("phone", "Lead", "phone", True, 32,
                 ("phone", "contact", "whatsapp", "whatsappnumber", "mobile", "phonenumber", "number"),
                 help="With the country code (e.g. +971501234567), or fill in Country."),
        FieldDef("country", "Country", "country", max_length=100),
        FieldDef("event_date", "Event date", "event_date", max_length=100, aliases=("weddingdate",), help=DATE_HELP),
        ASSIGNED,
        FieldDef("submitted_at", "Submitted", "datetime", aliases=("submittedat", "submissiontime", "timestamp"),
                 help=DATETIME_HELP),
    ],
    "wedding": [
        FieldDef("name", "Lead", max_length=200, aliases=("name", "fullname")),
        FieldDef("email", "Email", "email", max_length=254, aliases=("emailaddress",)),
        FieldDef("phone", "Contact", "phone", True, 50, ("phone", "contactnumber", "mobile", "phonenumber"),
                 help=PHONE_HELP_IN),
        FieldDef("occasion", "Occasion", max_length=100, aliases=("whatistheoccasion",)),
        FieldDef("event_date", "Event date", "event_date", max_length=100, help=DATE_HELP),
        FieldDef("city", "City", max_length=200, aliases=("cityeventlocation", "eventlocation", "location")),
        FieldDef("budget", "Budget", "budget", max_length=100,
                 aliases=("photographybudget", "whatisyourapproximatephotographybudget"),
                 help="E.g. 150000, ₹1,50,000, 50k or 1.5 lakh."),
        ASSIGNED,
        FieldDef("submitted_at", "Submitted", "datetime", aliases=("timestamp", "submittedat"), help=DATETIME_HELP),
    ],
    "wedding_planner": [
        FieldDef("name", "Lead", max_length=200, aliases=("name", "fullname")),
        FieldDef("role", "Role", max_length=100, aliases=("youarethe",)),
        FieldDef("email", "Email", "email", max_length=254, aliases=("emailaddress",)),
        FieldDef("phone", "Contact", "phone", True, 32, ("phone", "whatsappnumber", "contactnumber", "mobile", "phonenumber"),
                 help=PHONE_HELP_IN),
        FieldDef("wedding_when", "Wedding date", "wedding_when",
                 help="A date (20 Dec 2027) or a month (December 2027)."),
        FieldDef("city", "Wedding city", max_length=200, aliases=("weddingcitydestination", "city")),
        FieldDef("total_guests", "Guests", "int", aliases=("totalguests",)),
        FieldDef("guests_staying", "Guests staying", "int", aliases=("guestsstayingneedingrooms",)),
        FieldDef("total_budget", "Budget", "money", aliases=("totalbudget",)),
        FieldDef("budget_range", "Budget range", max_length=100),
        ASSIGNED,
        FieldDef("updated", "Updated", "datetime", aliases=("lastupdated",), help=DATETIME_HELP),
        # Extra columns (not on the list screen; optional)
        FieldDef("wedding_month", "Wedding month", "month", max_length=50),
        FieldDef("first_day", "First day of celebrations", "date", aliases=("firstday",)),
        FieldDef("wedding_type", "Wedding type", max_length=100),
        FieldDef("home_city", "Family home city", max_length=200, aliases=("homecity",)),
        FieldDef("place_kind", "Kind of place", max_length=200, aliases=("kindofplaceifnotsure",)),
        FieldDef("traditions", "Traditions", max_length=300),
        FieldDef("food", "Food", max_length=100),
        FieldDef("alcohol", "Alcohol", max_length=50),
        FieldDef("budget_includes_jewellery", "Budget includes jewellery and outfits", "bool",
                 aliases=("budgetincludesjewelleryandoutfits",)),
        FieldDef("jewellery_outfits", "Jewellery and outfits", "money", aliases=("jewelleryandoutfits",)),
        FieldDef("priority1", "Priority 1", max_length=100),
        FieldDef("priority2", "Priority 2", max_length=100),
        FieldDef("priority3", "Priority 3", max_length=100),
        FieldDef("shortlisted_vendors", "Shortlisted vendors", max_length=5000),
        FieldDef("consent_share", "Consent: share plan", "bool", aliases=("consentshareplanwithkingofcards",)),
        FieldDef("consent_offers", "Consent: offers on WhatsApp", "bool", aliases=("consentoffersonwhatsapp",)),
    ],
}

# KOC -> Dhruva upload: the list's / export's columns (keys as in spin_lead.additional_details)
FIELDS[KOC] = [
    # Aliases: the KOC -> Dhruva and Spin export headers and the workflow names (header_map also
    # accepts the key and "additional_details.<key>"); any case, spaces and punctuation ignored
    FieldDef("name", "Name", max_length=150, aliases=("lead", "fullname", "leadname", "customername")),
    FieldDef("phone", "Phone", "phone", True, 20, ("contact", "mobile", "phonenumber", "contactnumber",
                                                  "whatsappnumber", "mobilenumber"), help=PHONE_HELP_IN),
    FieldDef("email", "Email", "email", max_length=150, aliases=("emailaddress", "emailid", "mail")),
    FieldDef("occasionType", "Occasion", max_length=100, detail=True,
             aliases=("occasiontype", "eventtype", "typeofoccasion")),
    FieldDef("occasionDate", "Event date", "date", detail=True,
             aliases=("occasiondate", "eventdate", "weddingdate", "dateofevent"), help=DATE_HELP),
    FieldDef("branch", "Branch", "branch", aliases=("store", "branchname", "storename", "branchid"),
             help="Branch name exactly as on the Branches page."),
    FieldDef("estimatedBudgetPerCard", "Estimated budget per card", "range", max_length=100, detail=True,
             aliases=("budget", "budgetpercard", "estimatedbudget", "budgetpercardrs", "pricepercard"),
             help="A range such as 70-100, or 500+. Format the column as Text in Excel (Excel turns 1-10 "
                  "into a date; such dates are read back as the range)."),
    FieldDef("quantityRequired", "Quantity required", "int", detail=True,
             aliases=("quantity", "qty", "noofcards", "numberofcards", "cards")),
    FieldDef("created_at", "Received", "datetime",
             aliases=("createdat", "created", "timestamp", "receivedat", "receivedon", "datereceived",
                      "submittedat", "submitted"),
             help=DATETIME_HELP + " Empty: the Spin game lead's received date, else now."),
    # Optional (template only, not in the export)
    FieldDef("source", "Source", max_length=50, aliases=("leadsource", "utmsource"),
             help="Where the lead came from, e.g. Instagram. Empty: none (stored as crm)."),
    FieldDef("occasion", "Relation", max_length=100, aliases=("leadoccasion", "relationship", "relationtocouple"),
             help="E.g. Sister or Self."),
    # Extra columns (not in the export; optional)
    FieldDef("pincode", "Pincode", "pincode", detail=True, aliases=("pin", "zipcode", "postalcode")),
    FieldDef("otherWeddingServices", "Other wedding services", max_length=500, detail=True,
             aliases=("otherservices", "weddingservices", "services")),
]

# Read-only columns present in exports and/or templates; never read back from an upload
READ_ONLY_COLUMNS: dict[str, tuple[str, ...]] = {
    KOC: ("Sl. No", "Branch code", "Quoted date", "Hot"),
    "spin": ("Branch code", "Vendor updates", "Quoted date", "Hot"),
    "whatsapp": ("Vendor updates", "Quoted date", "Hot"),
    "wedding": ("Vendor updates", "Quoted date", "Hot"),
    "wedding_planner": ("Estimate", "Fits budget", "Vendor updates", "Quoted date", "Last step", "Hot"),
}
UPDATE_HELP = ("Empty or Pending, Converted, Not converted, Already purchased somewhere else, Already purchased "
               "with us, Will call back, Interested or Not interested.")
AMOUNT_HELP = "Rupees, e.g. 150000 or ₹1,50,000 (at most 10,00,00,000)."
# CRM details an upload may set on NEW leads (saved in crm_lead_meta, like Assigned to). Not in
# FIELDS: the add/edit forms don't have them (set them with PUT /leads/{source}/{key}/crm).
UPLOAD_CRM_FIELDS: tuple[FieldDef, ...] = (
    FieldDef("update_status", "Update", "update", aliases=("updatestatus", "status"), help=UPDATE_HELP),
    FieldDef("quoted_amount", "Quoted amount", "amount", aliases=("quotedamount", "quote"), help=AMOUNT_HELP),
    FieldDef("conversion_amount", "Conversion amount", "amount", aliases=("conversionamount",), help=AMOUNT_HELP),
)
# KOC -> Dhruva uploads: the section's CRM details for new rows (crm_lead_meta, source koc_to_dhruva)
KOC_CRM_FIELDS: tuple[FieldDef, ...] = (
    UPLOAD_CRM_FIELDS[0],
    FieldDef("notes", "Additional comments", max_length=5000, aliases=("notes", "comments")),
    UPLOAD_CRM_FIELDS[1],
    UPLOAD_CRM_FIELDS[2],
    FieldDef("follow_up_date", "Follow-up date", "date", aliases=("followupdate", "followup"), help=DATE_HELP),
)
UPLOAD_CRM_KEYS = tuple(f.key for f in UPLOAD_CRM_FIELDS)
CRM_KEYS = frozenset(f.key for f in (*UPLOAD_CRM_FIELDS, *KOC_CRM_FIELDS))    # never lead columns


def upload_crm_fields(source: str) -> tuple[FieldDef, ...]:
    """The CRM columns an upload of the source reads for new leads."""
    return KOC_CRM_FIELDS if source == KOC else UPLOAD_CRM_FIELDS
MAX_AMOUNT = 100_000_000
VIRTUAL_KEYS = {"assigned_to", "location", "wedding_when"}     # read from uploads, not lead columns


def norm_header(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def header_map(source: str, *, upload: bool = False) -> dict[str, str]:
    """Normalised header -> field key (label, key and aliases all accepted). upload=True adds
    Update, Quoted amount and Conversion amount."""
    out: dict[str, str] = {}
    for f in (*FIELDS[source], *(upload_crm_fields(source) if upload else ())):
        for name in (f.label, f.key, *f.aliases, f"additional_details.{f.key}"):
            out.setdefault(norm_header(name), f.key)
    return out


def read_only_headers(source: str) -> set[str]:
    return {norm_header(h) for h in READ_ONLY_COLUMNS[source]}


# ── Value parsing ───────────────────────────────────────────────────────
class Invalid(ValueError):
    pass


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATE_TEXT_RE = re.compile(r"^\s*\d{1,2} [A-Za-z]{3} \d{4}\s*$")     # how a date cell reads ("01 Oct 2026")


def excel_range(value: Any, year: int | None = None) -> str | None:
    """A range Excel turned into a date, back to the range; None when the value isn't such a date.
    A date cell (or its text, "01 Oct 2026") of the 1st in another year than `year` (default: this
    year, India time) was month-year: "10-20" -> 1 Oct 2020 -> "10-20". Otherwise day and month,
    lower first: "1-10" -> 1 Oct 2026 -> "1-10" (also when Excel read it month-first)."""
    if isinstance(value, datetime):
        when = value.date()
    elif isinstance(value, date):
        when = value
    elif isinstance(value, str) and _DATE_TEXT_RE.match(value):
        try:
            when = datetime.strptime(value.strip(), "%d %b %Y").date()
        except ValueError:
            return None
    else:
        return None
    if year is None:
        year = datetime.now(timezone(timedelta(hours=5, minutes=30))).year
    if when.day == 1 and when.year != year:
        return f"{when.month}-{when.year % 100}"
    low, high = sorted((when.day, when.month))
    return f"{low}-{high}"
_MONTHS = ("%B %Y", "%b %Y", "%m/%Y", "%m-%Y", "%Y-%m")
# Uploads: the screen's formats (13 Dec 2026 / 14 Nov 2026, 10:30 am) and DD-MM-YYYY; time optional
_UPLOAD_DATES = ("%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d")     # ISO too (exports re-uploaded)
_UPLOAD_TIMES = ("", " %H:%M", " %H:%M:%S", ", %H:%M", ", %I:%M %p", " %I:%M %p", ", %I:%M:%S %p", " %I:%M:%S %p")
DATE_ERROR = "Use a date like 13 Dec 2026 or 13-12-2026"


def as_text(value: Any) -> str:
    """Upload/JSON value -> trimmed text. Excel numbers like 9845012345.0 lose the .0."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, datetime):
        return display_datetime(value.replace(tzinfo=IST) if value.tzinfo is None else value) \
            if value.time() != time() else display_date(value.date())
    if isinstance(value, date):
        return display_date(value)
    return str(value).strip()


def display_date(d: date) -> str:
    """13 Dec 2026"""
    return d.strftime("%d %b %Y")


def display_datetime(dt: datetime) -> str:
    """14 Nov 2026, 10:30 am (India time), as on the list screens."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(IST)
    return f"{local.day} {local:%b %Y}, {local:%I:%M} {'am' if local.hour < 12 else 'pm'}"


def _clean_text(value: Any) -> str:
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", as_text(value), flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=8192)                # uploads repeat the same dates; pure, immutable result
def _upload_datetime(text: str) -> datetime | None:
    """Naive datetime (India time) from the accepted upload formats, or None."""
    for d in _UPLOAD_DATES:
        for t in _UPLOAD_TIMES:
            try:
                return datetime.strptime(text, d + t)
            except ValueError:
                continue
    return None


def parse_date(value: Any, *, upload: bool = False) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean_text(value)
    dt = _upload_datetime(text)
    if dt is not None:
        return dt.date()
    if not upload:                                       # API: ISO dates as well
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    raise Invalid(DATE_ERROR)


def parse_datetime(value: Any, *, upload: bool = False) -> datetime:
    """-> aware UTC. Values without a time zone (typed or from Excel) are India time."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time())
    else:
        text = _clean_text(value)
        dt = _upload_datetime(text)
        if dt is None and not upload:                    # API: ISO 8601 as well
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        if dt is None:
            raise Invalid("Use a date like 14 Nov 2026, 10:30 am or 14-11-2026 10:30")
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


def parse_money(value: Any) -> int:
    """150000, 1,50,000, ₹1,50,000, 25L, 1.5 lakh -> rupees."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return parse_int(value)
    amount = parse_budget(as_text(value).replace(" ", "").replace("L", "l"))
    if amount is None:
        raise Invalid("Not an amount (e.g. 150000 or ₹1,50,000)")
    return parse_int(amount)


def parse_update(value: Any) -> str | None:
    """Update: a code (will_call_back) or its label (Will call back); empty / Pending -> None."""
    from app.models.vendor_to_koc import STATUS_LABELS

    text = norm_header(as_text(value))
    if text in ("", "pending"):
        return None
    for code, label in STATUS_LABELS.items():
        if code and text in (norm_header(code), norm_header(label)):
            return code
    raise Invalid("Unknown update; use " + ", ".join(l for l in STATUS_LABELS.values()))


def parse_amount(value: Any) -> int:
    amount = parse_money(value)
    if amount > MAX_AMOUNT:
        raise Invalid("At most 10,00,00,000")
    return amount


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = as_text(value).lower()
    if text in ("yes", "y", "true", "1"):
        return True
    if text in ("no", "n", "false", "0"):
        return False
    raise Invalid("Use Yes or No")


_AUTO_RE = re.compile(r"^(.*?)\s*[·•\-–(]?\s*auto\)?$", re.I)


@dataclass
class RowContext:
    """What validation needs from the database (branch and vendor names)."""
    branches_by_name: dict[str, int] = field(default_factory=dict)   # norm_header(name) -> id
    branch_ids: set[int] = field(default_factory=set)
    vendors_by_name: dict[str, tuple[int, str]] = field(default_factory=dict)   # norm name -> (id, name)


@dataclass
class Cleaned:
    values: dict[str, Any]                 # field key -> clean Python value (None = empty)
    errors: list[dict]                     # [{"field": key, "message": ...}]
    phone: phones.Phone | None = None
    phone_input: str = ""
    # "Assigned to": None = column empty/absent; ("none", []) | ("auto", []) | ("manual", [vendor ids])
    assignment: tuple[str, list[int]] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


_ASSIGNED_SPLIT = re.compile(r"[,;\n]+")


def parse_assigned(text: str, ctx: RowContext) -> tuple[tuple[str, list[int]], str]:
    """-> ((mode, vendor ids), canonical text). Several vendors are separated by commas (or ";"),
    as the export writes them ("Phodeo, Kraftyz · auto"). The vendors named without "· auto" are
    the lead's manual vendors (they replace the automatic one; partner rules such as Kraftyz still
    add theirs); only "· auto" names (or "Automatic") = the automatic rules decide."""
    if norm_header(text) in ("", "unassigned", "none"):
        return ("none", []), "Unassigned"
    if norm_header(text) in ("automatic", "auto"):
        return ("auto", []), "Automatic"          # same as "<vendor> · auto": the automatic rules decide
    manual: list[tuple[int, str]] = []
    autos: list[str] = []
    for part in (p.strip() for p in _ASSIGNED_SPLIT.split(text)):
        if not part:
            continue
        auto = _AUTO_RE.match(part)
        name, is_auto = (auto.group(1), True) if auto and auto.group(1).strip() else (part, False)
        if norm_header(name) in ("automatic", "auto"):
            continue
        vendor = ctx.vendors_by_name.get(norm_header(name))
        if vendor is None:
            raise Invalid(f"Unknown vendor '{name.strip()}'")
        if is_auto:
            autos.append(f"{vendor[1]} · auto")
        elif vendor[0] not in [vid for vid, _ in manual]:
            manual.append(vendor)
    if not manual:
        return ("auto", []), ", ".join(autos) or "Automatic"
    return ("manual", [vid for vid, _ in manual]), ", ".join(name for _, name in manual)


WEDDING_MONTH_MAX = 50       # crm_wedding_plans.wedding_month


def _planner_when(value: Any, text: str, values: dict[str, Any], upload: bool) -> None:
    """A planner date cell: a full date -> first_day; "December 2027" -> wedding_month as
    "December 2027"; any other text -> wedding_month as written (the sync keeps it as text)."""
    try:
        values["first_day"] = parse_date(value, upload=upload)
        return
    except Invalid:
        pass
    try:
        values["wedding_month"] = parse_month(value)
    except Invalid:
        if len(text) > WEDDING_MONTH_MAX:
            raise Invalid(f"Longer than {WEDDING_MONTH_MAX} characters")
        values["wedding_month"] = text


def clean_row(source: str, raw: dict[str, Any], ctx: RowContext, *, partial: bool = False, upload: bool = False) -> Cleaned:
    """Check and clean one lead. partial=True (edit): only the fields present in `raw`.
    upload=True: dates must be in the upload formats (13 Dec 2026 / 13-12-2026, time optional)."""
    values: dict[str, Any] = {}
    errors: list[dict] = []
    phone: phones.Phone | None = None
    phone_input = ""
    assignment = None
    country = as_text(raw.get("country")) if source == "whatsapp" else None

    for f in FIELDS[source]:
        if partial and f.key not in raw:
            continue
        value = raw.get(f.key)
        text = as_text(value)
        if f.type == "branch" and isinstance(value, int) and not isinstance(value, bool):
            text = str(value)
        if f.type != "assigned" and lead_values.is_na(text):      # N/A-like = empty (not for Assigned to)
            text, value = "", None
        if not text:
            if f.required:
                errors.append({"field": f.key, "message": f"{f.label} is required"})
            values.setdefault(f.key, None)      # keep a value filled from Location / Wedding date
            continue
        try:
            if f.max_length and f.type in ("text", "email", "country", "event_date", "range", "budget", "phone") \
                    and len(text) > f.max_length:
                raise Invalid(f"Longer than {f.max_length} characters")
            if upload and source == "wedding_planner" and f.key in ("first_day", "wedding_month"):
                # Planner dates, like the sheet sync: a full date is the first day; a month or a
                # partial date ("16th Nov") is kept as the wedding month's text
                _planner_when(value, text, values, upload)
            elif f.type in ("text", "country"):
                values[f.key] = text
            elif f.type == "range":
                values[f.key] = excel_range(value) or re.sub(r"\s*[–—]\s*", "-", text.replace("₹", "")).strip()
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
                values[f.key] = parse_date(value, upload=upload)
            elif f.type == "event_date":
                # Like the sheet sync: kept as written ("April 2027", "16th Nov" too) and read as a
                # date when it is one (to_model_kwargs); the sync rejects no text, so neither do we
                if isinstance(value, (date, datetime)):     # Excel date cell
                    values[f.key] = display_date(parse_date(value))
                else:
                    values[f.key] = text
            elif f.type == "datetime":
                values[f.key] = parse_datetime(value, upload=upload)
            elif f.type == "month":
                values[f.key] = parse_month(value)
            elif f.type == "int":
                values[f.key] = parse_int(value)
            elif f.type == "money":
                values[f.key] = parse_money(value)
            elif f.type == "bool":
                values[f.key] = parse_bool(value)
            elif f.type == "budget":
                values[f.key] = text
            elif f.type == "pincode":
                digits = re.sub(r"\D", "", text)
                if len(digits) != 6:
                    raise Invalid("Pincode must have 6 digits")
                values[f.key] = digits
            elif f.type == "location":
                pin = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
                values[f.key] = text
                if pin and not as_text(raw.get("pincode")):
                    values["pincode"] = pin.group(1)
            elif f.type == "wedding_when":
                values[f.key] = text
                _planner_when(value, text, values, upload)
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
            elif f.type == "assigned":
                assignment, values[f.key] = parse_assigned(text, ctx)
            else:  # pragma: no cover
                values[f.key] = text
        except Invalid as exc:
            errors.append({"field": f.key, "message": str(exc)})
    if upload:
        for f in upload_crm_fields(source):         # CRM details for new leads (crm_lead_meta)
            text = as_text(raw.get(f.key))
            if lead_values.is_na(text):
                values[f.key] = None
                continue
            try:
                if f.type == "update":
                    values[f.key] = parse_update(raw[f.key])
                elif f.type == "amount":
                    values[f.key] = parse_amount(raw[f.key])
                elif f.type == "date":
                    values[f.key] = parse_date(raw[f.key], upload=True)
                else:
                    if f.max_length and len(text) > f.max_length:
                        raise Invalid(f"Longer than {f.max_length} characters")
                    values[f.key] = text
            except Invalid as exc:
                errors.append({"field": f.key, "message": str(exc)})
    return Cleaned(values, errors, phone, phone_input, assignment)


def field_defs(source: str) -> dict[str, FieldDef]:
    return {f.key: f for f in FIELDS[source]}


# ── Clean values <-> model columns ─────────────────────────────────────
def to_model_kwargs(source: str, values: dict[str, Any], phone: phones.Phone | None) -> dict[str, Any]:
    """Clean form values -> column values for the source's table (only the keys present)."""
    out: dict[str, Any] = {}
    if source in DETAIL_SOURCES:
        details = {}
        for f in FIELDS[source]:
            if f.key not in values or f.key in VIRTUAL_KEYS:
                continue
            v = values[f.key]
            if f.key == "branch":
                out["branch_id"] = v
            elif f.detail:
                details[f.key] = v.isoformat() if isinstance(v, date) else v
            elif f.key == "phone":
                out["phone"] = phone.national_in
                out["phone_e164"] = phone.e164
            elif f.key == "created_at":
                if v is not None:
                    out["created_at"] = v
            else:
                out[f.key] = v
        out["_details"] = details
        return out

    for key, v in values.items():
        if key in VIRTUAL_KEYS or key in CRM_KEYS:            # not lead columns (crm_lead_meta)
            continue
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
        elif key == "updated":
            if v is not None:
                out["updated_in_sheet_at"] = v
        elif key in ("submitted_at",) and v is None:
            continue                                           # empty = now (set on create)
        else:
            out[key] = v
    if source == "wedding_planner" and ("first_day" in values or "wedding_month" in values):
        out["_wedding_date"] = True
    return out


def from_model(source: str, lead) -> dict[str, Any]:
    """Model -> form values (JSON-friendly), for the edit form and API answers.
    "Assigned to" isn't a lead field: see the lead's crm details."""
    def j(v):
        return v.isoformat() if isinstance(v, (date, datetime)) else v

    out: dict[str, Any] = {}
    if source in DETAIL_SOURCES:
        details = lead.additional_details or {}
        for f in FIELDS[source]:
            if f.key == "branch":
                out["branch"] = lead.branch_id
            elif f.key in VIRTUAL_KEYS:
                out[f.key] = None
            elif f.detail:
                out[f.key] = details.get(f.key)
            else:
                out[f.key] = j(getattr(lead, f.key))
        return out
    for f in FIELDS[source]:
        if f.key == "phone":
            out["phone"] = lead.phone_raw if source == "wedding" and lead.phone_raw else (f"+{lead.phone}" if lead.phone else None)
        elif f.key == "event_date":
            out["event_date"] = lead.event_date_text
        elif f.key == "budget":
            out["budget"] = lead.budget_text
        elif f.key == "wedding_when":
            out["wedding_when"] = j(lead.first_day) if lead.first_day else lead.wedding_month
        elif f.key == "updated":
            out["updated"] = j(lead.updated_in_sheet_at)
        elif f.key in VIRTUAL_KEYS:
            out[f.key] = None
        elif f.key in ("priority1", "priority2", "priority3"):
            prios = list(lead.priorities or [])
            idx = int(f.key[-1]) - 1
            out[f.key] = prios[idx] if idx < len(prios) else None
        else:
            out[f.key] = j(getattr(lead, f.key))
    return out


def json_values(values: dict[str, Any]) -> dict[str, Any]:
    """Clean values -> what the preview sends back, in upload formats (bulk save re-checks them)."""
    out = {}
    for k, v in values.items():
        if isinstance(v, datetime):
            out[k] = display_datetime(v)
        elif isinstance(v, date):
            out[k] = display_date(v)
        else:
            out[k] = v
    return out
