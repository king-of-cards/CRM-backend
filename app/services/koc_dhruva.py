"""KOC -> Dhruva (source key koc_to_dhruva): spin leads KOC shares with Dhruva by rule.

Rule (spin_leads.koc_filter / koc_reason): estimatedBudgetPerCard's first number >= 70, or
quantityRequired >= 1000, over EVERY workflow spin_lead row (listed as spin or not) and every
CRM-added spin lead. The leads stay in spin / wedding_gifts too; this section keeps its own CRM
details, hot flags and activity under source "koc_to_dhruva", and no vendor is ever assigned.

Who: admins, view-only and Dhruva logins read; admins and Dhruva logins edit (view-only: the
central 403); vendor logins never see these leads.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session

from app.core import response_cache

logger = logging.getLogger(__name__)

SOURCE = "koc_to_dhruva"
LABEL = "KOC → Dhruva"
WORKFLOW_LIMIT = 100_000          # every rule row in one query (869 today)
CACHE_SECONDS = 30                # cleared with the access cache (access.forget)


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower().split(".")[-1] if ch.isalnum())


def pick(item: dict, *names: str) -> Any:
    """The first non-empty value among fields whose last name part matches (any case / punctuation)."""
    wanted = [_norm(n) for n in names]
    by_norm: dict[str, Any] = {}
    for key, value in item.items():
        by_norm.setdefault(_norm(key), value)
    for w in wanted:
        value = by_norm.get(w)
        if value not in (None, ""):
            return value
    return None


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


def row_of(item: dict) -> dict:
    """A list item (workflow or CRM) -> the fields the section works with."""
    from app.api.v1.endpoints.spin_leads import KOC_REASON_LABELS, first_number, koc_reason

    budget = pick(item, "estimatedBudgetPerCard")
    quantity = pick(item, "quantityRequired")
    reason = koc_reason({"estimatedBudgetPerCard": budget, "quantityRequired": quantity})
    return {
        "key": str(item.get("id")),
        "name": pick(item, "name"),
        "phone": pick(item, "phone"),
        "email": pick(item, "email"),
        "occasion": pick(item, "occasionType"),
        "event_date": pick(item, "occasionDate"),
        "pincode": pick(item, "pincode"),
        "city": pick(item, "city", "location"),
        "branch_id": pick(item, "branch_id", "branchId"),
        "budget": budget,
        "quantity": first_number(quantity),
        "reason": reason,
        "reason_label": KOC_REASON_LABELS.get(reason),
        "created": _datetime(pick(item, "created_at", "createdAt", "created")),
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
    items = [{**i, "origin": "workflow"} for i in data["items"]]
    items += [crm_spin_leads.to_item(lead, data["columns"]) for lead in crm_spin_leads.active(db, SOURCE)]
    return [row_of(i) for i in items]


def rows(db: Session) -> list[dict] | None:
    """Every KOC -> Dhruva lead (workflow rows, then CRM-added ones), or None when the workflow
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
