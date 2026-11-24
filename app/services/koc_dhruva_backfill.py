"""Fill the empty fields of the uploaded KOC -> Dhruva rows (scripts/backfill_koc_dhruva_fields.py).

The same rule as new uploads (crm_leads.service.plan_koc_fill): every empty field of a
crm_koc_dhruva_leads row is filled from the workflow row (any date) with the same last 10 phone
digits, chosen as for Spin (no conflicting name, event date, occasion or branch; the closest
created date when several), and stored. Filled values are never overwritten. Also:
  Received   a Received that is only the upload time (within 2 minutes of the row's "created"
             activity entry, else of its first save) becomes the workflow row's created_at
  Budget     an estimated budget per card that Excel turned into a date ("01 Oct 2026",
             "01 Oct 2020") is read back as the range it was ("1-10", "10-20"; fields.excel_range)
Each changed row gets one activity entry (action filled_from_workflow, actor "Backfill").
"""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, select

from app.models.koc_dhruva_lead import KocDhruvaLead
from app.models.lead_activity import LeadActivity
from app.services import phones
from app.services.crm_leads import service
from app.services.crm_leads.fields import KOC, excel_range, from_model
from app.services.koc_spin_cleanup import sql_value

BUDGET_KEY = "estimatedBudgetPerCard"
DEFAULT_RECEIVED_WINDOW = timedelta(minutes=2)
ACTOR = "Backfill"
COLUMNS = ("name", "email", "occasion", "branch_id", "created_at", "additional_details", "updated_at")


@dataclass
class Plan:
    items: list[dict] = field(default_factory=list)      # {id, lead_key, phone, workflow_id, fields, received, budget}
    no_match: list[dict] = field(default_factory=list)   # {lead_key, phone, name}: no workflow row has the phone
    skipped: list[dict] = field(default_factory=list)    # {lead_key, phone, reason}: several / conflicting rows
    per_field: Counter = field(default_factory=Counter)


def _naive(value: datetime | None) -> datetime | None:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value is not None and value.tzinfo else value


def _created_entries(db: Session) -> dict[str, datetime]:
    rows = db.exec(select(LeadActivity.lead_key, LeadActivity.created_at).where(
        LeadActivity.source == KOC, LeadActivity.action == "created"))
    out: dict[str, datetime] = {}
    for key, when in rows:
        out.setdefault(key, when)
    return out


def received_is_default(lead: KocDhruvaLead, created_entry: datetime | None) -> bool:
    """The row's Received is only its upload time (the upload left Received empty)."""
    saved = created_entry or lead.updated_at
    return saved is not None and abs(_naive(lead.created_at) - _naive(saved)) <= DEFAULT_RECEIVED_WINDOW


def plan(db: Session) -> Plan:
    from app.services.branches import branches_by_id

    out = Plan()
    leads = list(db.exec(select(KocDhruvaLead).order_by(KocDhruvaLead.id)))
    hits_by_tail = service.workflow_phones({lead.phone_e164 for lead in leads if lead.phone_e164}, by_last10=True)
    known = list(branches_by_id(db).values())
    created = _created_entries(db)
    for lead in leads:
        values = from_model(KOC, lead)
        default_received = received_is_default(lead, created.get(lead.lead_key))
        values["created_at"] = None if default_received else lead.created_at
        budget = excel_range(values.get(BUDGET_KEY), _naive(lead.created_at).year)
        if budget is not None:
            values[BUDGET_KEY] = budget
        hits = hits_by_tail.get(phones.last10(lead.phone_e164 or ""), [])
        fill = service.plan_koc_fill(values, hits, known) if hits else None
        info = {"id": lead.id, "lead_key": lead.lead_key, "phone": lead.phone, "name": lead.name}
        if fill is None and not hits:
            out.no_match.append(info)
        elif fill is not None and "skipped" in fill:
            out.skipped.append({**info, "reason": fill["skipped"]})
        fields = (fill or {}).get("fields") or {}
        received = (fill or {}).get("received")
        if fields or received or budget is not None:
            out.items.append({**info, "workflow_id": (fill or {}).get("workflow_id"), "fields": fields,
                              "received": received, "budget": budget})
            out.per_field.update(list(fields))
            out.per_field.update(["Received"] if received else [])
            out.per_field.update(["Budget (Excel date read back)"] if budget is not None else [])
    return out


def _snapshot(lead: KocDhruvaLead) -> dict[str, Any]:
    return {c: (dict(lead.additional_details or {}) if c == "additional_details" else getattr(lead, c))
            for c in COLUMNS}


def apply(db: Session, the_plan: Plan) -> list[str]:
    """Fill the planned rows (not committed). Returns the undo SQL. Raises ValueError when a row
    changed since the plan (the caller rolls back)."""
    undo: list[str] = []
    activity_ids: list[int] = []
    now = datetime.now(timezone.utc)
    for item in the_plan.items:
        lead = db.get(KocDhruvaLead, item["id"])
        if lead is None:
            raise ValueError(f"{item['lead_key']} is gone; preview again")
        before = _snapshot(lead)
        details = dict(lead.additional_details or {})
        if item["budget"] is not None:
            if excel_range(details.get(BUDGET_KEY), _naive(lead.created_at).year) != item["budget"]:
                raise ValueError(f"{item['lead_key']}: the budget changed since the preview; preview again")
            details[BUDGET_KEY] = item["budget"]
            lead.additional_details = details
        if item["fields"]:
            service.fill_lead(lead, item["fields"])                 # never overwrites a filled field
        if item["received"] is not None:
            lead.created_at = item["received"]
        lead.updated_at = now
        db.add(lead)
        changed = sorted(item["fields"]) + (["Received"] if item["received"] else []) \
            + (["Budget"] if item["budget"] is not None else [])
        entry = LeadActivity(source=KOC, lead_key=lead.lead_key, action=service.FILLED_ACTION,
                             field=", ".join(changed)[:100], old_value=None,
                             new_value=f"workflow lead {item['workflow_id']}" if item["workflow_id"] else "budget read back",
                             actor_user_id=None, actor_name=ACTOR, actor_role="admin", created_at=now)
        db.add(entry)
        db.flush()
        activity_ids.append(entry.id)
        sets = ", ".join(f"{c} = {sql_value(before[c])}" for c in COLUMNS)
        undo.append(f"UPDATE crm_koc_dhruva_leads SET {sets} WHERE id = {lead.id};")
    if activity_ids:
        undo.append(f"DELETE FROM crm_lead_activity WHERE id IN ({', '.join(map(str, activity_ids))});")
    return undo
