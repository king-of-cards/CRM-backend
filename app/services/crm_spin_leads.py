"""Spin leads added or imported in the CRM (MySQL crm_spin_leads), merged into GET /spin-leads.

The workflow leads are filtered, sorted and paged in PostgreSQL. The CRM's own spin leads are
few, so they are loaded from MySQL and filtered and sorted here with the same rules, then
listed before the workflow leads:

    [ CRM spin leads, sorted ] + [ workflow spin leads, sorted ]

Paging runs over that combined list, so every lead appears exactly once across the pages
(and in an export that walks all pages), and "total" counts both.

Flows: a CRM lead whose additional_details.otherWeddingServices names "Wedding Gifts & Return
Gifts" belongs to wedding_gifts, every other one to spin (the same rule as the workflow rows), so
no marker column is needed. koc_to_dhruva (KOC -> Dhruva) is a rule on top: a CRM lead can be in
it AND in spin / wedding_gifts (see spin_leads.koc_reason).
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


def flow_of(lead: CrmSpinLead) -> str:
    """spin or wedding_gifts (see the module docstring)."""
    from app.api.v1.endpoints.spin_leads import flow_of_details

    return flow_of_details(_details(lead))


def koc_reason(lead: CrmSpinLead) -> str | None:
    """"budget", "quantity" or None: why the lead is (or isn't) a KOC -> Dhruva lead."""
    from app.api.v1.endpoints.spin_leads import koc_reason as reason_of

    return reason_of(_details(lead))


def _in_flow(lead: CrmSpinLead, flow: str | None) -> bool:
    if flow == "koc_to_dhruva":
        return koc_reason(lead) is not None
    return flow is None or flow_of(lead) == flow


def _like(term: str, text: str) -> bool:
    return term.casefold() in text.casefold()


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
    if flt.get("origins") and lead.origin not in flt["origins"]:
        return False
    if flt.get("occasion") and not _like(flt["occasion"], _text(_details(lead).get("occasionType"))):
        return False
    if flt.get("quantity_min") is not None or flt.get("quantity_max") is not None:
        from app.api.v1.endpoints.spin_leads import first_number
        quantity = first_number(_details(lead).get("quantityRequired"))
        if quantity is None:
            return False
        if flt.get("quantity_min") is not None and quantity < flt["quantity_min"]:
            return False
        if flt.get("quantity_max") is not None and quantity > flt["quantity_max"]:
            return False
    if flt.get("reason") and koc_reason(lead) != flt["reason"]:
        return False
    if flt.get("event_from") is not None or flt.get("event_to") is not None:
        event = str(_details(lead).get("occasionDate") or "")[:10]       # YYYY-MM-DD
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", event):
            return False
        if flt.get("event_from") is not None and event < flt["event_from"]:
            return False
        if flt.get("event_to") is not None and event > flt["event_to"]:
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
    flow: str | None = "spin",
) -> list[CrmSpinLead]:
    """CRM leads of the flow that pass the list's filters, in the list's sort order."""
    term = (search or "").strip()
    leads = [
        lead
        for lead in session.exec(select(CrmSpinLead).where(CrmSpinLead.is_deleted == False))  # noqa: E712
        if _in_flow(lead, flow) and _matches(lead, term, branch, flt or {})
    ]
    desc = order == "desc"
    leads.sort(key=lambda lead: lead.id or 0, reverse=desc)         # tie-break, as the primary key
    if sort:
        with_value = [lead for lead in leads if _sort_value(lead, sort) is not None]
        without = [lead for lead in leads if _sort_value(lead, sort) is None]
        with_value.sort(key=lambda lead: _sort_value(lead, sort), reverse=desc)   # stable: keeps id order
        leads = with_value + without
    return leads


def active(session: Session, flow: str | None = "spin") -> list[CrmSpinLead]:
    """CRM leads of the flow that count (archived ones are hidden from lists, counts and exports).
    flow None: both flows."""
    return [lead for lead in session.exec(select(CrmSpinLead).where(CrmSpinLead.is_deleted == False))  # noqa: E712
            if _in_flow(lead, flow)]


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
