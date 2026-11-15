"""Spin leads added or imported in the CRM (MySQL crm_spin_leads), merged into GET /spin-leads.

The workflow leads are filtered, sorted and paged in PostgreSQL. The CRM's own spin leads are
few, so they are loaded from MySQL and filtered and sorted here with the same rules, then
listed before the workflow leads:

    [ CRM spin leads, sorted ] + [ workflow spin leads, sorted ]

Paging runs over that combined list, so every lead appears exactly once across the pages
(and in an export that walks all pages), and "total" counts both.
"""
import json
import re
from datetime import date, datetime
from typing import Any

from sqlmodel import Session, select

from app.models.crm_spin_lead import CrmSpinLead

# Same names and rules as app/api/v1/endpoints/spin_leads.py
PINCODE_FIELD_NAMES = ("pincode", "pin", "zipcode", "postalcode")
BUDGET_FIELD_NAMES = ("estimatedbudgetpercard", "servicebudget", "budget")
RANGE_SORT_KEYS = {"estimatedBudgetPerCard", "serviceBudget"}
_COLUMNS = ("name", "email", "phone", "occasion", "source", "campaign_id", "customer_id", "branch_id", "consent_at", "created_at")
_SEARCH_COLUMNS = ("name", "email", "phone", "occasion", "source")   # text columns, as PostgreSQL searches
_RANGE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)")
JSON_PREFIX = "additional_details."


def _norm_name(name: str) -> str:
    return "".join(ch for ch in name.lower().split(".")[-1] if ch.isalnum())


def _details(lead: CrmSpinLead) -> dict[str, Any]:
    return lead.additional_details if isinstance(lead.additional_details, dict) else {}


def _text(value: Any) -> str:
    """A value as PostgreSQL's ->> / CAST(... AS TEXT) would give it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _named_value(lead: CrmSpinLead, wanted_names: tuple[str, ...]) -> str | None:
    """First column / additional_details key with one of these names (column wins), as text."""
    details = _details(lead)
    for wanted in wanted_names:
        for col in _COLUMNS:
            if _norm_name(col) == wanted:
                value = getattr(lead, col)
                return None if value is None else _text(value)
        for key, value in details.items():
            if _norm_name(key) == wanted:
                return None if value is None else _text(value)
    return None


def _like(term: str, text: str) -> bool:
    return term.casefold() in text.casefold()


def _matches_vendor(lead: CrmSpinLead, v: dict) -> bool:
    """Python version of the vendor filter in spin_leads.list_spin_leads."""
    key = lead.lead_key
    if key in v["manual_for"]:
        return True
    if key in v["manual_other"]:
        return False
    branch = "" if lead.branch_id is None else str(lead.branch_id)
    if branch and branch in {str(b) for b in v["branch_ids"]}:
        return True
    if branch:
        return False                     # area rules only apply to leads without a branch
    pin = _named_value(lead, PINCODE_FIELD_NAMES) or ""
    if not pin:
        return False
    if v["fallback"]:
        return pin not in v["other_pins"] and not any(pin.startswith(p) for p in v["other_prefixes"])
    if not (pin in v["pins"] or any(pin.startswith(p) for p in v["prefixes"])):
        return False
    return not (pin in v.get("before_pins", []) or any(pin.startswith(p) for p in v.get("before_prefixes", [])))


def _matches(lead: CrmSpinLead, search: str, branch: str | None, flt: dict) -> bool:
    if search:
        texts = [_text(getattr(lead, c)) for c in _SEARCH_COLUMNS] + [_text(v) for v in _details(lead).values()]
        if not any(_like(search, t) for t in texts):
            return False
    if branch not in (None, "") and str(lead.branch_id) != str(branch):
        return False
    if flt.get("pincode"):
        pin = _named_value(lead, PINCODE_FIELD_NAMES) or ""
        if not pin.casefold().startswith(str(flt["pincode"]).casefold()):
            return False
    if flt.get("budget") and _named_value(lead, BUDGET_FIELD_NAMES) != str(flt["budget"]):
        return False
    if flt.get("ids_in") is not None and lead.lead_key not in {str(i) for i in flt["ids_in"]}:
        return False
    if flt.get("ids_not_in") and lead.lead_key in {str(i) for i in flt["ids_not_in"]}:
        return False
    if flt.get("created_from") is not None:          # arrival in a period (naive UTC bounds)
        created = lead.created_at.replace(tzinfo=None) if lead.created_at else None
        if created is None or not (flt["created_from"] <= created < flt["created_to"]):
            return False
    if flt.get("vendor") and not _matches_vendor(lead, flt["vendor"]):
        return False
    return True


def _sort_value(lead: CrmSpinLead, sort: str) -> Any:
    """Comparable value, or None (always listed last, like NULLS LAST)."""
    if sort.startswith(JSON_PREFIX):
        key = sort[len(JSON_PREFIX):]
        value = _details(lead).get(key)
        if key in RANGE_SORT_KEYS:
            m = _RANGE_RE.match(_text(value))
            return (0, float(m.group(1))) if m else None
    elif sort == "id":
        return (0, lead.id or 0)
    else:
        value = getattr(lead, sort, None)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    if isinstance(value, (datetime, date)):
        return (1, value.isoformat())
    return (2, _text(value).casefold())


def matching(
    session: Session,
    search: str | None,
    sort: str | None,
    order: str,
    branch: str | None,
    flt: dict | None,
) -> list[CrmSpinLead]:
    """CRM spin leads that pass the list's filters, in the list's sort order."""
    term = (search or "").strip()
    leads = [
        lead
        for lead in session.exec(select(CrmSpinLead).where(CrmSpinLead.is_deleted == False))  # noqa: E712
        if _matches(lead, term, branch, flt or {})
    ]
    desc = order == "desc"
    leads.sort(key=lambda lead: lead.id or 0, reverse=desc)         # tie-break, as the primary key
    if sort:
        with_value = [lead for lead in leads if _sort_value(lead, sort) is not None]
        without = [lead for lead in leads if _sort_value(lead, sort) is None]
        with_value.sort(key=lambda lead: _sort_value(lead, sort), reverse=desc)   # stable: keeps id order
        leads = with_value + without
    return leads


def active(session: Session) -> list[CrmSpinLead]:
    """CRM spin leads that count (archived ones are hidden from lists, counts and exports)."""
    return list(session.exec(select(CrmSpinLead).where(CrmSpinLead.is_deleted == False)))  # noqa: E712


def pincode_of(lead: CrmSpinLead) -> str | None:
    return _named_value(lead, PINCODE_FIELD_NAMES)


def _out_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def to_item(lead: CrmSpinLead, columns: list[dict]) -> dict:
    """The lead in the same shape as a workflow row: one key per entry in `columns`."""
    details = _details(lead)
    item: dict[str, Any] = {}
    for meta in columns:
        name = meta["name"]
        if meta.get("json_field"):
            value = details.get(meta["key"])
            item[name] = json.dumps(value, separators=(",", ":"), ensure_ascii=False) if isinstance(value, (dict, list)) else value
        elif name == "id":
            item[name] = lead.lead_key
        else:
            item[name] = _out_value(getattr(lead, name, None)) if name in _COLUMNS else None
    item["origin"] = lead.origin
    return item
