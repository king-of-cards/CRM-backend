"""Remove the Spin leads a KOC -> Dhruva upload created (scripts/remove_koc_created_spin_leads.py).

For a while a KOC -> Dhruva upload also created an uploaded Spin lead (crm_spin_leads) for each row
whose phone wasn't in Spin. That rule is gone (a KOC -> Dhruva lead never creates a Spin lead).

Which leads: the upload logged, on each Spin lead it created, one activity entry
    crm_lead_activity: source = 'spin', action = 'created', new_value = 'from KOC → Dhruva kd-<id>'
(a normal Spin upload logs new_value 'created'). Those lead keys (crm-<id>) are the candidates.
Imported Spin leads WITHOUT that entry that look like a copy of a KOC -> Dhruva upload row (same
phone and name, created by the same login within 2 minutes) are only listed ("unlinked"), never removed.

Removed per lead: the crm_spin_leads row, its crm_lead_assignments, crm_lead_meta (every vendor's
copy), crm_hot_leads, crm_lead_activity, crm_notifications (and their reads). The KOC -> Dhruva rows
(crm_koc_dhruva_leads) and their CRM details are kept.

Skipped (listed, nothing removed): a lead someone already worked on: any copy with an Update other
than pending, notes, a quoted amount or date, a conversion amount or a follow-up date; a hot star;
or any activity entry besides the upload's "created" one.
"""
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from app.models.crm_spin_lead import CrmSpinLead
from app.models.hot_lead import CrmHotLead
from app.models.koc_dhruva_lead import KocDhruvaLead
from app.models.lead_activity import LeadActivity
from app.models.lead_assignment import LeadAssignment
from app.models.lead_meta import LeadMeta
from app.models.lead_origin import CRM_KEY_PREFIX
from app.models.notification import CrmNotification, CrmNotificationRead
from app.services.crm_leads.service import KOC_SPIN_CREATED_PREFIX

SOURCE = "spin"
UNLINKED_WINDOW = timedelta(minutes=2)
# Child tables keyed by (source, lead_key), in delete order (restore runs the other way)
LEAD_TABLES = (LeadActivity, CrmHotLead, LeadMeta, LeadAssignment)
WORK_FIELDS = ("notes", "quoted_amount", "quoted_date", "conversion_amount", "follow_up_date")


@dataclass
class Plan:
    remove: list[dict] = field(default_factory=list)     # {lead_key, id, name, phone, koc_key, vendors}
    skipped: list[dict] = field(default_factory=list)    # the same + reasons
    unlinked: list[dict] = field(default_factory=list)   # {lead_key, name, phone, koc_key}
    missing: list[str] = field(default_factory=list)     # logged keys whose Spin lead is already gone


def _naive(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=None) if value is not None and value.tzinfo else value


def _rows(db: Session, model, key: str) -> list:
    return list(db.exec(select(model).where(model.source == SOURCE, model.lead_key == key)))


def _vendors(db: Session, keys: list[str]) -> dict[str, list[str]]:
    """The leads' vendor names now (assignments and rules), or ["?"] when they can't be worked out."""
    from app.services.crm_leads.service import spin_vendors

    try:
        found = spin_vendors(db, keys)
    except Exception:  # noqa: BLE001  (workflow database unreachable: the rules can't be evaluated)
        return {k: ["?"] for k in keys}
    return {k: [v["name"] or f"vendor {v['id']}" for v in found.get(k, [])] for k in keys}


def _work(db: Session, key: str, created_ids: set[int]) -> list[str]:
    """Why the lead counts as worked on ([] = not worked on)."""
    from app.services.crm_leads.service import _vendor_names

    names = _vendor_names(db)                      # inactive vendors too
    reasons = []
    for meta in _rows(db, LeadMeta, key):
        who = names.get(meta.copy_vendor_id, "the lead's own row") if meta.copy_vendor_id else "the lead's own row"
        if meta.update_status not in (None, "", "pending"):
            reasons.append(f"{who}: Update {meta.update_status}")
        reasons += [f"{who}: {name}" for name in WORK_FIELDS if getattr(meta, name) not in (None, "")]
    if _rows(db, CrmHotLead, key):
        reasons.append("hot star")
    other = [a for a in _rows(db, LeadActivity, key) if a.id not in created_ids]
    if other:
        reasons.append("activity: " + ", ".join(sorted({a.action for a in other})))
    return reasons


def plan(db: Session) -> Plan:
    out = Plan()
    logged = list(db.exec(select(LeadActivity).where(
        LeadActivity.source == SOURCE, LeadActivity.action == "created",
        LeadActivity.new_value.like(KOC_SPIN_CREATED_PREFIX + "%"))))
    created: dict[str, dict] = {}
    for act in logged:
        entry = created.setdefault(act.lead_key, {"ids": set(), "koc_key": act.new_value[len(KOC_SPIN_CREATED_PREFIX):]})
        entry["ids"].add(act.id)
    keys = sorted(created, key=lambda k: (len(k), k))
    vendors = _vendors(db, keys)
    for key in keys:
        lead = db.get(CrmSpinLead, int(key[len(CRM_KEY_PREFIX):])) if key.startswith(CRM_KEY_PREFIX) else None
        if lead is None:
            out.missing.append(key)
            continue
        item = {"lead_key": key, "id": lead.id, "name": lead.name, "phone": lead.phone,
                "koc_key": created[key]["koc_key"], "vendors": vendors.get(key, []), "archived": bool(lead.is_deleted)}
        reasons = _work(db, key, created[key]["ids"])
        if reasons:
            out.skipped.append({**item, "reasons": reasons})
        else:
            out.remove.append(item)

    # Unlinked look-alikes: listed only
    linked = set(created)
    koc = list(db.exec(select(KocDhruvaLead)))
    by_phone: dict[str, list[KocDhruvaLead]] = {}
    for row in koc:
        by_phone.setdefault(row.phone_e164, []).append(row)
    for lead in db.exec(select(CrmSpinLead).where(CrmSpinLead.origin == "imported")):
        if lead.lead_key in linked:
            continue
        for row in by_phone.get(lead.phone_e164, []):
            if (row.name or "") == (lead.name or "") and row.created_by == lead.created_by \
                    and abs(_naive(row.created_at) - _naive(lead.created_at)) <= UNLINKED_WINDOW:
                out.unlinked.append({"lead_key": lead.lead_key, "name": lead.name, "phone": lead.phone,
                                     "koc_key": row.lead_key})
                break
    return out


# ── Apply (one transaction) with undo SQL ─────────────────────────────
def sql_value(value: Any) -> str:
    """A MySQL literal for the undo SQL (JSON columns as JSON text)."""
    from app.services.spin_history_rekey import sql_value as literal

    if isinstance(value, (dict, list)):
        return literal(json.dumps(value, ensure_ascii=False))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return literal(_naive(value))
    if value is None or isinstance(value, (bool, int, float, str, date)):
        return literal(value)
    return literal(str(value))


def insert_sql(row) -> str:
    table = row.__table__
    columns = [c.name for c in table.columns]
    values = ", ".join(sql_value(getattr(row, c)) for c in columns)
    return f"INSERT INTO {table.name} ({', '.join(columns)}) VALUES ({values});"


def apply(db: Session, the_plan: Plan) -> list[str]:
    """Remove the planned leads (not committed). Returns the undo SQL (INSERTs of every removed
    row, parents first). Raises ValueError when a lead changed since the plan (caller rolls back)."""
    undo_parents: list[str] = []
    undo_children: list[str] = []
    for item in the_plan.remove:
        key = item["lead_key"]
        lead = db.get(CrmSpinLead, item["id"])
        if lead is None:
            raise ValueError(f"{key} is gone; preview again")
        created_ids = {a.id for a in _rows(db, LeadActivity, key)
                       if a.action == "created" and (a.new_value or "").startswith(KOC_SPIN_CREATED_PREFIX)}
        if not created_ids or _work(db, key, created_ids):
            raise ValueError(f"{key} changed since the preview; preview again")
        notes = _rows(db, CrmNotification, key)
        reads = list(db.exec(select(CrmNotificationRead).where(
            CrmNotificationRead.notification_id.in_([n.id for n in notes])))) if notes else []
        undo_parents.append(insert_sql(lead))
        for model in reversed(LEAD_TABLES):
            undo_children += [insert_sql(r) for r in _rows(db, model, key)]
        undo_children += [insert_sql(n) for n in notes] + [insert_sql(r) for r in reads]
        for r in reads:
            db.delete(r)
        db.flush()
        for n in notes:
            db.delete(n)
        for model in LEAD_TABLES:
            for r in _rows(db, model, key):
                db.delete(r)
        db.delete(lead)
        db.flush()
    return undo_parents + undo_children
