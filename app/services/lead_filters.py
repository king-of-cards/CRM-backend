"""Filters shared by the four lead lists, their exports and their stats.

Common parameters (every source):
  search                      name, phone (digits), email where the source has them
  date_from / date_to         arrival date (IST days; see app/services/periods.py)
  event_from / event_to       event date, inclusive (YYYY-MM-DD)
  upcoming_days               event from today to today + N (not with event_from / event_to)
  vendor_id (repeatable)      effective assignment (the "Assigned to" chip); admins only
  unassigned=true             no vendor; admins only (with vendor_id: either)
  update_status (repeatable)  converted | not_converted | purchased_elsewhere | purchased_with_us |
                              will_call_back | interested | not_interested | pending (no update yet)
  quote                       has | none | gt:<amount> | lt:<amount>      (quoted amount)
  conversion                  has | none | gt:<amount> | lt:<amount>      (conversion amount)
  origin (repeatable)         spin: workflow | added | imported; others: sheet | added | imported
  also_in                     yes | no: the phone exists in another source or Vendor -> KOC (that the
                              viewer may see; see app/services/also_in.py)
  hot                         yes | no: whether this lead is marked hot

Different parameters combine with AND, repeated values of one parameter with OR. Vendor users
only ever get their own leads (vendor_id / unassigned are ignored for them). Errors -> 400.

The CRM filters (vendor, unassigned, update_status, quote, conversion) become a set of lead keys: from
the same assignment map vendor visibility uses (access.assignments) and crm_lead_meta.
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_
from sqlmodel import Session, col, select

from app.models.lead_meta import LeadMeta
from app.services import access, periods

UPDATE_VALUES = ("converted", "not_converted", "purchased_elsewhere", "purchased_with_us",
                 "will_call_back", "interested", "not_interested")
UPDATE_FILTER_VALUES = (*UPDATE_VALUES, "pending")
SHEET_ORIGINS = ("sheet", "added", "imported")
SPIN_ORIGINS = ("workflow", "added", "imported")
MAX_UPCOMING_DAYS = 366
_QUOTE_RE = re.compile(r"^(gt|lt):(\d{1,12})$")


def bad_request(message: str) -> HTTPException:
    return HTTPException(400, message)


@dataclass
class CommonFilters:
    search: str | None = None
    period: periods.Period | None = None                 # arrival date
    event_from: date | None = None
    event_to: date | None = None
    vendor_ids: list[int] = field(default_factory=list)
    unassigned: bool = False
    updates: list[str] = field(default_factory=list)       # may contain "pending" (= no update)
    quote: tuple[str, int | None] | None = None            # (op, amount): has / none / gt / lt
    conversion: tuple[str, int | None] | None = None       # same, on the conversion amount
    also_in: str | None = None                             # yes | no
    hot: bool | None = None
    follow_up: str | None = None
    origins: list[str] = field(default_factory=list)

    @property
    def has_crm_filters(self) -> bool:
        return bool(self.updates or self.quote or self.conversion or self.also_in or self.hot is not None or self.follow_up)

    @property
    def has_event_filter(self) -> bool:
        return self.event_from is not None or self.event_to is not None


def parse_quote(text: str | None, name: str = "quote") -> tuple[str, int | None] | None:
    value = (text or "").strip().lower()
    if not value:
        return None
    if value in ("has", "none"):
        return value, None
    m = _QUOTE_RE.match(value)
    if not m:
        raise bad_request(f"{name} must be has, none, gt:<amount> or lt:<amount> (e.g. gt:50000)")
    return m.group(1), int(m.group(2))


def _event_day(text: str | None, name: str) -> date | None:
    value = (text or "").strip()
    if not value:
        return None
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise bad_request(f"{name} must be a date like 2026-12-20 (YYYY-MM-DD)")


def common_filters(
    period: periods.Period | None,
    search: str | None,
    event_from: str | None,
    event_to: str | None,
    upcoming_days: int | None,
    vendor_id: list[int] | None,
    unassigned: bool,
    update_status: list[str] | None,
    quote: str | None,
    conversion: str | None,
    origin: list[str] | None,
    allowed_origins: tuple[str, ...],
    source_label: str,
    also_in: str | None = None,
    hot: str | None = None,
    follow_up: str | None = None,
) -> CommonFilters:
    """Validate and build the common filters (the endpoints declare the query parameters)."""
    start, end = _event_day(event_from, "event_from"), _event_day(event_to, "event_to")
    if upcoming_days is not None:
        if start or end:
            raise bad_request("Use either upcoming_days or event_from / event_to, not both")
        if not 1 <= upcoming_days <= MAX_UPCOMING_DAYS:
            raise bad_request(f"upcoming_days must be between 1 and {MAX_UPCOMING_DAYS}")
        start = periods.today_ist()
        end = start + timedelta(days=upcoming_days)
    if start and end and start > end:
        raise bad_request("event_from must be on or before event_to")

    updates = []
    for value in update_status or []:
        if value not in UPDATE_FILTER_VALUES:
            raise bad_request(f"update_status must be one of: {', '.join(UPDATE_FILTER_VALUES)}")
        updates.append(value)

    origins = [o.strip().lower() for o in origin or [] if o.strip()]
    for value in origins:
        if value not in allowed_origins:
            raise bad_request(f"origin '{value}' doesn't exist for {source_label}; use {', '.join(allowed_origins)}")

    if hot not in (None, "yes", "no"):
        raise bad_request("hot must be yes or no")
    if follow_up not in (None, "due", "overdue", "today", "upcoming"):
        raise bad_request("follow_up must be due, overdue, today or upcoming")

    return CommonFilters(
        search=(search or "").strip() or None,
        period=period,
        event_from=start,
        event_to=end,
        vendor_ids=list(vendor_id or []),
        unassigned=bool(unassigned),
        updates=updates,
        quote=parse_quote(quote),
        conversion=parse_quote(conversion, "conversion"),
        also_in=also_in,
        hot=None if hot is None else hot == "yes",
        follow_up=follow_up,
        origins=origins,
    )


def check_range(low: int | None, high: int | None, name: str) -> None:
    if low is not None and high is not None and low > high:
        raise bad_request(f"{name}_min must not be more than {name}_max")


# ── Lead keys from the CRM filters ─────────────────────────────────────
def _quote_matches(amount: int | None, quote: tuple[str, int | None]) -> bool:
    op, value = quote
    if op == "has":
        return amount is not None
    if op == "none":
        return amount is None
    if amount is None:
        return False
    return amount > value if op == "gt" else amount < value


def allowed_keys(db: Session, source: str, viewer, f: CommonFilters) -> set[str] | None:
    """Lead keys the list may show: the viewer's visible leads, narrowed by the CRM filters.
    None = no restriction (an admin without CRM filters)."""
    sets: list[set[str]] = []
    visible = access.visible_keys(db, viewer, source)
    if visible is not None:
        sets.append(visible)
    if viewer.sees_all and (f.vendor_ids or f.unassigned):
        # "Assigned to": any of the lead's vendors matches; unassigned = no vendor at all
        assignment = access.assignments(db, source)
        wanted = set(f.vendor_ids)
        sets.append({k for k, ids in assignment.items() if (wanted & set(ids)) or (f.unassigned and not ids)})
    if f.has_crm_filters:
        from app.services import lead_crm

        universe = set(access.assignments(db, source))          # every listed lead of the source
        own = lead_crm.viewer_copy(viewer)                      # vendor logins: their own copy
        metas = lead_crm.records(db, source, own)               # admins: every copy merged

        def meta_of(key):
            return metas.get(key)

        if f.updates:
            sets.append({k for k in universe
                         if ((meta_of(k).update_status if meta_of(k) else None) or "pending") in f.updates})
        if f.quote:
            sets.append({k for k in universe if _quote_matches(meta_of(k).quoted_amount if meta_of(k) else None, f.quote)})
        if f.conversion:
            sets.append({k for k in universe
                         if _quote_matches(meta_of(k).conversion_amount if meta_of(k) else None, f.conversion)})
        if f.also_in:
            from app.services import also_in as also_in_service

            found = also_in_service.matching_keys(db, viewer, source)
            sets.append(universe & found if f.also_in == "yes" else universe - found)
        if f.hot is not None:
            from app.services import hot_leads

            marked = hot_leads.hot_keys(db, source, lead_crm.viewer_copy(viewer))
            sets.append(universe & marked if f.hot else universe - marked)
        if f.follow_up:
            from app.models.vendor_to_koc import CLOSED_STATUSES
            today = periods.today_ist()
            dated = {m.lead_key: m for m in metas.values() if m.follow_up_date is not None}
            def matches_follow_up(key: str) -> bool:
                meta = dated.get(key)
                if meta is None:
                    return False
                value = meta.follow_up_date
                if meta.update_status in CLOSED_STATUSES:
                    return False
                return ((f.follow_up == "due" and value <= today) or
                        (f.follow_up == "overdue" and value < today) or
                        (f.follow_up == "today" and value == today) or
                        (f.follow_up == "upcoming" and value > today))
            sets.append({k for k in universe if matches_follow_up(k)})
    if not sets:
        return None
    keys = sets[0]
    for s in sets[1:]:
        keys = keys & s
    return keys


# ── SQL helpers ────────────────────────────────────────────────────────
def contains(column, text: str):
    """Case-insensitive "contains", with % and _ taken literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return col(column).ilike(f"%{escaped}%", escape="\\")


def search_condition(text: str, text_columns: list, phone_column) -> Any:
    """Search: any text column contains the text, or the phone contains its digits."""
    parts = [contains(c, text) for c in text_columns]
    digits = re.sub(r"\D", "", text)
    if phone_column is not None and digits:
        parts.append(col(phone_column).like(f"%{digits}%"))
    return or_(*parts)


# ── Filter options ─────────────────────────────────────────────────────
def update_options(db: Session, source: str, keys: set[str], viewer=None) -> list[dict]:
    """[{value, count}] over these leads: pending (no update yet), then the 7 update values (each lead
    once: its overall Update; vendor logins: their own copy's)."""
    from app.services import lead_crm

    metas = {k: (r.update_status if r else None)
             for k, r in lead_crm.records(db, source, lead_crm.viewer_copy(viewer)).items()}
    counts = dict.fromkeys(("pending", *UPDATE_VALUES), 0)
    for k in keys:
        value = metas.get(k) or "pending"
        counts[value] = counts.get(value, 0) + 1
    return [{"value": v, "count": n} for v, n in counts.items()]


def vendor_options(db: Session, viewer) -> list[dict]:
    """Active vendors for admins; none for vendor users (vendor filtering is admin-only)."""
    if not viewer.sees_all:
        return []
    from app.services.lead_crm import active_vendors

    return [{"id": v.id, "name": v.name} for v in active_vendors(db)]


def counted(values) -> list[dict]:
    """[{value, count}] most common first, then by name."""
    counts: dict[Any, int] = {}
    for v in values:
        if v not in (None, ""):
            counts[v] = counts.get(v, 0) + 1
    return [{"value": v, "count": n} for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0]).lower()))]

