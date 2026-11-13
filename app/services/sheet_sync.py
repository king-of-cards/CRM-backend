"""Copy Google Sheets into MySQL.

Each sheet source has its own sync function (its columns are different). A sync:
  1. reads all rows from the sheet,
  2. inserts new rows, updates edited rows, leaves unchanged rows alone,
  3. marks rows that disappeared from the sheet as removed (is_deleted),
  4. writes a SheetSyncRun log entry.
Only one sync runs at a time.
"""
import hashlib
import logging
import re
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, select

from app.models.sheet_sync_run import SheetSyncRun
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.services import google_sheets

logger = logging.getLogger(__name__)

_sync_lock = threading.Lock()


class SyncAlreadyRunning(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _find_column(columns: list[str], *needles: str) -> str | None:
    """First header containing any of the needles (case and punctuation ignored)."""
    for needle in needles:
        for col in columns:
            if needle in _norm(col):
                return col
    return None


def _sha1(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


# ── Parsing helpers for the WhatsApp sheet ──────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))
_SUBMITTED_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?")


def parse_submitted(text: str) -> datetime | None:
    """'26/07/2026, 13:46:32 IST' (day/month/year, India time) -> UTC datetime."""
    m = _SUBMITTED_RE.match(text or "")
    if not m:
        return None
    day, month, year, hour, minute, second = (int(g) if g else 0 for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(timezone.utc)
    except ValueError:
        return None


_EVENT_FORMATS = (
    "%d/%m/%Y",   # 13/12/2026 (day first, as used in India)
    "%m/%d/%Y",   # 12/31/2026 (only when day-first is impossible)
    "%d/%m/%y",
    "%m/%d/%y",   # 08/15/27
    "%d-%m-%Y",
    "%Y-%m-%d",
    "%B %d, %Y",  # October 30, 2026
    "%B %d %Y",
    "%d %B %Y",   # 18 December 2026
    "%b %d, %Y",
    "%d %b %Y",
)


# Google Forms in a US-locale sheet write month/day/year
_EVENT_FORMATS_MONTH_FIRST = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y") + tuple(
    f for f in _EVENT_FORMATS if f not in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")
)


def parse_event_date(text: str, *, month_first: bool = False) -> date | None:
    """Best effort. Returns None when the text has no year or can't be read with confidence
    (e.g. 'November 16', 'April 2027'); the original text is always kept as well."""
    if not text or not text.strip():
        return None
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    for fmt in _EVENT_FORMATS_MONTH_FIRST if month_first else _EVENT_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


# ── WhatsApp sheet sync ─────────────────────────────────────────────────────
def sync_whatsapp(session: Session, source: str, run: SheetSyncRun) -> None:
    data = google_sheets.read_sheet(source, refresh=True)
    cols = data["columns"]

    country_col = _find_column(cols, "country")
    phone_col = _find_column(cols, "phone", "whatsapp", "mobile", "number")
    event_col = _find_column(cols, "eventdate", "event", "weddingdate")
    submitted_col = _find_column(cols, "submission", "submitted", "timestamp")
    if not phone_col:
        raise ValueError(f"No phone column found in the sheet. Columns: {', '.join(cols)}")

    existing = {lead.sheet_key: lead for lead in session.exec(select(WhatsAppLead))}
    seen: set[str] = set()
    key_counts: dict[str, int] = {}
    now = _now()

    for row in data["rows"]:
        phone = _digits(row.get(phone_col, ""))
        if not phone:
            continue  # rows without a phone number are skipped
        run.rows_read += 1

        country = (row.get(country_col, "") if country_col else "").strip() or None
        event_text = (row.get(event_col, "") if event_col else "").strip() or None
        submitted_text = (row.get(submitted_col, "") if submitted_col else "").strip()

        # Stable key: phone + submission time; repeats get #2, #3 in sheet order
        base = _sha1(phone, submitted_text)
        key_counts[base] = key_counts.get(base, 0) + 1
        key = base if key_counts[base] == 1 else _sha1(base, str(key_counts[base]))
        seen.add(key)

        row_hash = _sha1(country or "", phone, event_text or "", submitted_text)
        lead = existing.get(key)

        if lead is None:
            session.add(
                WhatsAppLead(
                    sheet_key=key,
                    country=country,
                    phone=phone,
                    event_date_text=event_text,
                    event_date=parse_event_date(event_text or ""),
                    submitted_at=parse_submitted(submitted_text),
                    sheet_row=row["_row"],
                    row_hash=row_hash,
                    first_seen_at=now,
                    updated_at=now,
                )
            )
            run.inserted += 1
        elif lead.row_hash != row_hash or lead.is_deleted:
            lead.country = country
            lead.phone = phone
            lead.event_date_text = event_text
            lead.event_date = parse_event_date(event_text or "")
            lead.submitted_at = parse_submitted(submitted_text)
            lead.sheet_row = row["_row"]
            lead.row_hash = row_hash
            lead.is_deleted = False
            lead.updated_at = now
            session.add(lead)
            run.updated += 1
        else:
            if lead.sheet_row != row["_row"]:
                lead.sheet_row = row["_row"]  # row moved (sorting); not counted as an edit
                session.add(lead)
            run.unchanged += 1

    # Rows that are no longer in the sheet
    for key, lead in existing.items():
        if key not in seen and not lead.is_deleted:
            lead.is_deleted = True
            lead.updated_at = now
            session.add(lead)
            run.removed += 1


# ── Wedding & Event Photography sheet sync ─────────────────────────────────
_FORM_TIMESTAMP_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?")


def parse_form_timestamp(text: str) -> datetime | None:
    """Google Form timestamp '10/11/2026 0:37:00' (month/day/year, India time) -> UTC."""
    m = _FORM_TIMESTAMP_RE.match(text or "")
    if not m:
        return None
    month, day, year, hour, minute, second = (int(g) if g else 0 for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(timezone.utc)
    except ValueError:
        return None


def normalize_indian_phone(text: str) -> str | None:
    """Digits with country code. 10-digit numbers are Indian (+91); others are kept as given."""
    digits = _digits(text)
    if not digits:
        return None
    digits = digits.lstrip("0") or digits
    if len(digits) == 10:
        return "91" + digits
    return digits


_BUDGET_RE = re.compile(r"^(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*(k|thousand|l|lac|lakh|lakhs)?\s*(?:rs\.?|inr|/-)?$", re.I)


def parse_budget(text: str) -> int | None:
    """'20k' -> 20000, '1.5 lakh' -> 150000, '15,000' -> 15000. Ranges and words -> None."""
    m = _BUDGET_RE.match((text or "").strip())
    if not m:
        return None
    number = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit in ("k", "thousand"):
        number *= 1_000
    elif unit in ("l", "lac", "lakh", "lakhs"):
        number *= 100_000
    return int(number)


# Each field -> header names it can come from (compared without case, spaces or punctuation).
# The sheet has two sets of columns; for each row the first non-empty one wins.
_WEDDING_FIELDS = {
    "timestamp": ["timestamp"],
    "email": ["emailaddress", "email"],
    "name": ["fullname", "name"],
    "phone": ["contactnumber", "phonenumber", "phone", "mobile"],
    "city": ["cityeventlocation", "city", "eventlocation", "location"],
    "occasion": ["whatistheoccasion", "occasion"],
    "event_date": ["eventdate"],
    "budget": ["whatisyourapproximatephotographybudget", "budget"],
    "oseid": ["oseid"],
    "shared_with": ["leadsshared", "sharedwith", "leadshared"],
}


def _columns_for(columns: list[str], names: list[str]) -> list[str]:
    """All headers matching any of the names, in the order of `names`."""
    found: list[str] = []
    for name in names:
        for col in columns:
            if _norm(col) == name and col not in found:
                found.append(col)
    return found


def _first_value(row: dict[str, Any], cols: list[str]) -> str:
    for col in cols:
        value = str(row.get(col, "")).strip()
        if value:
            return value
    return ""


def sync_wedding(session: Session, source: str, run: SheetSyncRun) -> None:
    data = google_sheets.read_sheet(source, refresh=True)
    cols = {field: _columns_for(data["columns"], names) for field, names in _WEDDING_FIELDS.items()}
    if not cols["phone"] and not cols["name"]:
        raise ValueError(f"No name or phone column found. Columns: {', '.join(data['columns'])}")

    # 1. Read and clean every row
    parsed: list[dict[str, Any]] = []
    for row in data["rows"]:
        v = {field: _first_value(row, c) for field, c in cols.items()}
        if not (v["name"] or v["phone"] or v["email"]):
            continue  # nothing to identify the lead
        parsed.append(
            {
                "row": row["_row"],
                "timestamp_text": v["timestamp"],
                "submitted_at": parse_form_timestamp(v["timestamp"]),
                "name": v["name"] or None,
                "email": v["email"].lower() or None,
                "phone_raw": v["phone"] or None,
                "phone": normalize_indian_phone(v["phone"]),
                "city": v["city"] or None,
                "occasion": v["occasion"] or None,
                "event_date_text": v["event_date"] or None,
                "event_date": parse_event_date(v["event_date"], month_first=True),
                "budget_text": v["budget"] or None,
                "budget_amount": parse_budget(v["budget"]),
                "oseid": v["oseid"] or None,
                "shared_with": v["shared_with"] or None,
            }
        )

    # 2. Mark repeats: the same phone (or email) submitted again later
    first_by_contact: dict[str, tuple] = {}
    for p in parsed:
        order = (p["submitted_at"] or datetime.max.replace(tzinfo=timezone.utc), p["row"])
        contact = p["phone"] or p["email"] or ""
        if contact and (contact not in first_by_contact or order < first_by_contact[contact]):
            first_by_contact[contact] = order
    for p in parsed:
        contact = p["phone"] or p["email"] or ""
        order = (p["submitted_at"] or datetime.max.replace(tzinfo=timezone.utc), p["row"])
        p["is_repeat"] = bool(contact) and order != first_by_contact[contact]

    # 3. Save
    existing = {lead.sheet_key: lead for lead in session.exec(select(WeddingLead))}
    seen: set[str] = set()
    key_counts: dict[str, int] = {}
    now = _now()
    fields = (
        "submitted_at", "name", "email", "phone_raw", "phone", "city", "occasion",
        "event_date_text", "event_date", "budget_text", "budget_amount", "oseid", "shared_with", "is_repeat",
    )

    for p in parsed:
        run.rows_read += 1
        base = _sha1(p["timestamp_text"], p["phone"] or p["email"] or p["name"] or "")
        key_counts[base] = key_counts.get(base, 0) + 1
        key = base if key_counts[base] == 1 else _sha1(base, str(key_counts[base]))
        seen.add(key)

        row_hash = _sha1(*(str(p[f]) for f in fields))
        lead = existing.get(key)
        if lead is None:
            session.add(
                WeddingLead(
                    sheet_key=key,
                    sheet_row=p["row"],
                    row_hash=row_hash,
                    first_seen_at=now,
                    updated_at=now,
                    **{f: p[f] for f in fields},
                )
            )
            run.inserted += 1
        elif lead.row_hash != row_hash or lead.is_deleted:
            for f in fields:
                setattr(lead, f, p[f])
            lead.sheet_row = p["row"]
            lead.row_hash = row_hash
            lead.is_deleted = False
            lead.updated_at = now
            session.add(lead)
            run.updated += 1
        else:
            if lead.sheet_row != p["row"]:
                lead.sheet_row = p["row"]
                session.add(lead)
            run.unchanged += 1

    for key, lead in existing.items():
        if key not in seen and not lead.is_deleted:
            lead.is_deleted = True
            lead.updated_at = now
            session.add(lead)
            run.removed += 1


# ── Wedding planner sheet sync ─────────────────────────────────────────────
_PLANNER_FIELDS = {
    "session": ["session"],
    "plan_id": ["planid"],
    "last_step": ["laststepreached", "laststep"],
    "updated": ["lastupdated", "timestamp"],
    "wedding_type": ["weddingtype"],
    "city": ["weddingcitydestination", "weddingcity", "city"],
    "home_city": ["familyhomecity", "homecity"],
    "place_kind": ["kindofplaceifnotsure", "kindofplace"],
    "wedding_month": ["weddingmonth"],
    "first_day": ["firstdayofcelebrations", "firstday"],
    "total_guests": ["totalguests"],
    "guests_staying": ["guestsstayingneedingrooms", "guestsstaying"],
    "traditions": ["traditions"],
    "food": ["food"],
    "alcohol": ["alcohol"],
    "budget_range": ["budgetrange"],
    "total_budget": ["totalbudget"],
    "budget_includes_jewellery": ["budgetincludesjewelleryandoutfits"],
    "jewellery_outfits": ["jewelleryandoutfits"],
    "priority1": ["priority1"],
    "priority2": ["priority2"],
    "priority3": ["priority3"],
    "name": ["name", "fullname"],
    "phone": ["whatsappnumber", "phonenumber", "contactnumber", "phone"],
    "email": ["email", "emailaddress"],
    "role": ["youarethe", "role"],
    "consent_share": ["consentshareplanwithkingofcards"],
    "consent_offers": ["consentoffersonwhatsapp"],
    "plan_estimate": ["planestimate"],
    "fits_budget": ["fitsbudget"],
    "shortlisted_vendors": ["shortlistedvendors"],
    "shared_with": ["leadsshared", "sharedwith", "leadshared"],
}


def _to_int(text: str) -> int | None:
    digits = _digits(text)
    return int(digits) if digits else None


def _to_bool(text: str) -> bool | None:
    t = (text or "").strip().lower()
    if t in ("yes", "y", "true", "1"):
        return True
    if t in ("no", "n", "false", "0"):
        return False
    return None


def _month_to_date(text: str) -> date | None:
    """'December 2027' -> 2027-12-01."""
    for fmt in ("%B %Y", "%b %Y"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_events(text: str) -> list[dict[str, Any]]:
    """One event per line: 'Day 1 | Evening | Mehendi | Lunch | 60% = 24 guests | Elegant décor | indoor'."""
    events = []
    keys = ("day", "time", "event", "meal", "guests", "decor", "setting")
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        item = dict(zip(keys, parts + [""] * (len(keys) - len(parts))))
        m = re.search(r"=\s*([\d,]+)", item["guests"])
        item["guest_count"] = int(m.group(1).replace(",", "")) if m else None
        events.append(item)
    return events


def sync_wedding_planner(session: Session, source: str, run: SheetSyncRun) -> None:
    data = google_sheets.read_sheet(source, refresh=True)
    columns = data["columns"]
    cols = {field: _columns_for(columns, names) for field, names in _PLANNER_FIELDS.items()}
    # The events header is long ("Events (day | time | event | ...)"): match it by its start
    cols["events"] = [c for c in columns if _norm(c).startswith("events")][:1]
    if not cols["plan_id"] and not cols["phone"]:
        raise ValueError(f"No Plan ID or WhatsApp number column found. Columns: {', '.join(columns)}")

    # 1. Read every row; keep only the most recent row per plan
    latest: dict[str, dict[str, Any]] = {}
    versions: dict[str, int] = {}
    for row in data["rows"]:
        v = {field: _first_value(row, c) for field, c in cols.items()}
        updated = parse_form_timestamp(v["updated"])
        phone = normalize_indian_phone(v["phone"])
        plan_key = v["plan_id"] or v["session"] or (f"no-plan-{_sha1(v['updated'], phone or '')[:16]}")
        if not (v["plan_id"] or phone or v["email"] or v["name"]):
            continue
        run.rows_read += 1
        versions[plan_key] = versions.get(plan_key, 0) + 1

        order = (updated or datetime.min.replace(tzinfo=timezone.utc), row["_row"])
        current = latest.get(plan_key)
        if current is not None and current["_order"] > order:
            continue

        first_day = parse_event_date(v["first_day"], month_first=True)
        month_date = _month_to_date(v["wedding_month"])
        latest[plan_key] = {
            "_order": order,
            "_row": row["_row"],
            "plan_id": plan_key,
            "session": v["session"] or None,
            "last_step": v["last_step"].lower() or None,
            "updated_in_sheet_at": updated,
            "name": v["name"] or None,
            "phone": phone,
            "email": v["email"].lower() or None,
            "role": v["role"] or None,
            "consent_share": _to_bool(v["consent_share"]),
            "consent_offers": _to_bool(v["consent_offers"]),
            "wedding_type": v["wedding_type"] or None,
            "city": v["city"] or None,
            "home_city": v["home_city"] or None,
            "place_kind": v["place_kind"] or None,
            "wedding_month": v["wedding_month"] or None,
            "first_day": first_day,
            "wedding_date": first_day or month_date,
            "total_guests": _to_int(v["total_guests"]),
            "guests_staying": _to_int(v["guests_staying"]),
            "traditions": v["traditions"] or None,
            "events": parse_events(v["events"]),
            "food": v["food"] or None,
            "alcohol": v["alcohol"] or None,
            "budget_range": v["budget_range"] or None,
            "total_budget": _to_int(v["total_budget"]),
            "budget_includes_jewellery": _to_bool(v["budget_includes_jewellery"]),
            "jewellery_outfits": _to_int(v["jewellery_outfits"]),
            "plan_estimate": _to_int(v["plan_estimate"]),
            "fits_budget": _to_bool(v["fits_budget"]),
            "priorities": [p for p in (v["priority1"], v["priority2"], v["priority3"]) if p],
            "shortlisted_vendors": v["shortlisted_vendors"] or None,
            "shared_with": v["shared_with"] or None,
        }

    # 2. Save
    existing = {plan.plan_id: plan for plan in session.exec(select(WeddingPlan))}
    now = _now()
    for plan_key, p in latest.items():
        p["versions"] = versions[plan_key]
        values = {k: val for k, val in p.items() if not k.startswith("_")}
        row_hash = _sha1(*(f"{k}={values[k]}" for k in sorted(values)))
        plan = existing.get(plan_key)
        if plan is None:
            session.add(WeddingPlan(**values, sheet_row=p["_row"], row_hash=row_hash, first_seen_at=now, updated_at=now))
            run.inserted += 1
        elif plan.row_hash != row_hash or plan.is_deleted:
            for k, val in values.items():
                setattr(plan, k, val)
            plan.sheet_row = p["_row"]
            plan.row_hash = row_hash
            plan.is_deleted = False
            plan.updated_at = now
            session.add(plan)
            run.updated += 1
        else:
            run.unchanged += 1

    for plan_key, plan in existing.items():
        if plan_key not in latest and not plan.is_deleted:
            plan.is_deleted = True
            plan.updated_at = now
            session.add(plan)
            run.removed += 1


# Source name (as in GOOGLE_SHEETS) -> its sync function. Add new sheets here.
SYNCERS: dict[str, Callable[[Session, str, SheetSyncRun], None]] = {
    "whatsapp": sync_whatsapp,
    "wedding": sync_wedding,
    "wedding_planner": sync_wedding_planner,
}


def syncable_sources() -> list[str]:
    """Sources that are both configured in .env and have a sync function."""
    return [s for s in google_sheets.sources() if s in SYNCERS]


def run_sync(session: Session, source: str, trigger: str = "manual") -> SheetSyncRun:
    """Sync one source. Raises SyncAlreadyRunning if another sync is in progress."""
    if source not in SYNCERS:
        raise google_sheets.UnknownSource(f"No sync is set up for sheet '{source}'.")
    if not _sync_lock.acquire(blocking=False):
        raise SyncAlreadyRunning("A sync is already running. Try again in a moment.")
    try:
        run = SheetSyncRun(source=source, trigger=trigger, started_at=_now())
        try:
            SYNCERS[source](session, source, run)
            run.status = "success"
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            run = SheetSyncRun(
                source=source,
                trigger=trigger,
                started_at=run.started_at,
                status="failed",
                error=str(exc)[:1000],
            )
            logger.warning("Sheet sync '%s' failed: %s", source, exc)
        run.finished_at = _now()
        session.add(run)
        session.commit()
        session.refresh(run)
        return run
    finally:
        _sync_lock.release()


def last_runs(session: Session) -> dict[str, Any]:
    """Latest run and latest successful run per configured source."""
    result: dict[str, Any] = {}
    for source in syncable_sources():
        latest = session.exec(
            select(SheetSyncRun).where(SheetSyncRun.source == source).order_by(SheetSyncRun.id.desc()).limit(1)
        ).first()
        last_ok = session.exec(
            select(SheetSyncRun)
            .where(SheetSyncRun.source == source, SheetSyncRun.status == "success")
            .order_by(SheetSyncRun.id.desc())
            .limit(1)
        ).first()
        result[source] = {"latest": latest, "last_success": last_ok}
    return result