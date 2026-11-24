"""KOC -> Dhruva (source key koc_to_dhruva): spin leads KOC shares with Dhruva by rule.

Rows: the workflow spin_lead rows (listed as spin or not) created on or after the cut-off
(SPIN_LIVE_FROM) whose estimatedBudgetPerCard is >= 70 or else quantityRequired >= 1000, each read as
its starting number, "under…" / "below…" never (spin_leads.rule_number; koc_filter / koc_reason), plus every row uploaded to crm_koc_dhruva_leads (keys
"kd-<id>"; the rule is only shown there, never a filter). CRM spin leads are never included, and an
upload row never creates a Spin lead (the Spin link is only shown: crm_leads.service.plan_spin). A live
workflow lead stays in spin too; this section keeps its own CRM details, hot flags and activity
under source "koc_to_dhruva", and no vendor is ever assigned.

Who: admins, view-only and Dhruva logins read; admins and Dhruva logins edit (view-only: the
central 403); vendor logins never see these leads.
"""
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from sqlmodel import Session

from app.core import response_cache

logger = logging.getLogger(__name__)

SOURCE = "koc_to_dhruva"
LABEL = "KOC → Dhruva"
# Closed leads (won or lost); every other Update (none, interested, will call back) is open
CLOSED_STATUSES = frozenset({"converted", "purchased_with_us", "not_converted", "not_interested",
                             "purchased_elsewhere"})
WORKFLOW_LIMIT = 100_000          # every rule row in one query (869 today)
CACHE_SECONDS = 30                # cleared with the access cache (access.forget)


@lru_cache(maxsize=4096)
def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower().split(".")[-1] if ch.isalnum())


def _by_norm(item: dict) -> dict[str, Any]:
    """{normalised field name: value}, the first field of each name winning."""
    by_norm: dict[str, Any] = {}
    for key, value in item.items():
        by_norm.setdefault(_norm(key), value)
    return by_norm


def _pick_from(by_norm: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = by_norm.get(_norm(name))
        if value not in (None, ""):
            return value
    return None


def pick(item: dict, *names: str) -> Any:
    """The first non-empty value among fields whose last name part matches (any case / punctuation)."""
    return _pick_from(_by_norm(item), *names)


def _datetime(value: Any) -> datetime | None:
    """Naive UTC (as the workflow stores created_at), whatever the input."""
    if isinstance(value, str) and value:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def is_open(update_status: str | None) -> bool:
    return update_status not in CLOSED_STATUSES


def row_of(item: dict) -> dict:
    """A list item (workflow or CRM) -> the fields the section works with."""
    from app.api.v1.endpoints.spin_leads import KOC_REASON_LABELS, first_number, koc_reason

    fields = _by_norm(item)               # once per item (not once per field)
    budget = _pick_from(fields, "estimatedBudgetPerCard")
    quantity = _pick_from(fields, "quantityRequired")
    reason = koc_reason({"estimatedBudgetPerCard": budget, "quantityRequired": quantity})
    return {
        "key": str(item.get("id")),
        "name": _pick_from(fields, "name"),
        "phone": _pick_from(fields, "phone"),
        "email": _pick_from(fields, "email"),
        "occasion": _pick_from(fields, "occasionType"),
        "event_date": _pick_from(fields, "occasionDate"),
        "pincode": _pick_from(fields, "pincode"),
        "city": _pick_from(fields, "city", "location"),
        "branch_id": _pick_from(fields, "branch_id", "branchId"),
        "budget": budget,
        "quantity": first_number(quantity),
        "reason": reason,                 # internal (the reason filter); never returned
        "reason_label": KOC_REASON_LABELS.get(reason),
        "created": _datetime(_pick_from(fields, "created_at", "createdAt", "created")),
        "origin": item.get("origin") or "workflow",
    }


def _build(db: Session) -> list[dict] | None:
    from app.api.v1.endpoints import spin_leads as sl
    from app.services import crm_spin_leads

    try:
        data = sl.list_spin_leads(None, 1, WORKFLOW_LIMIT, offset=0, limit=WORKFLOW_LIMIT, flow=sl.KOC_TO_DHRUVA)
    except Exception:  # noqa: BLE001
        logger.warning("KOC -> Dhruva: workflow database unavailable", exc_info=True)
        return None
    uploaded = [crm_spin_leads.to_item(lead, data["columns"]) for lead in crm_spin_leads.active(db, SOURCE)]
    return [row_of(i) for i in uploaded] + [row_of({**i, "origin": "workflow"}) for i in data["items"]]


def rows(db: Session) -> list[dict] | None:
    """Every KOC -> Dhruva lead (uploaded rows, then live workflow rows), or None when the workflow
    database can't be read. Cached briefly; access.forget() clears it."""
    def build():
        built = _build(db)
        if built is None:
            raise _Unavailable()
        return built
    try:
        return response_cache.get(("access", "koc_rows"), build, fresh=CACHE_SECONDS, stale=CACHE_SECONDS)
    except _Unavailable:
        return None


class _Unavailable(Exception):
    """Not cached: the next call tries the workflow database again."""


def keys(db: Session) -> set[str]:
    return {r["key"] for r in rows(db) or []}
