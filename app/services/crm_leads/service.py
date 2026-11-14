"""Added/imported leads: create, edit, archive, look up, and duplicate checks.

Duplicates are found on the E.164 phone (phone_e164), within the lead's source and across the
other sources:
  existing_sheet     same source, a row copied from the Google Sheet (also rows since removed from it)
  existing_workflow  spin only: a lead in the workflow database (PostgreSQL spin_lead)
  existing_added     same source, a lead added or imported in the CRM (archived ones too)
  in_file            the same phone earlier in the same upload (duplicate_of_row)
  other_source       not blocking: the phone exists in other sources (listed)
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Text, cast, func
from sqlmodel import Session, select

from app.models.branch import Branch
from app.models.crm_spin_lead import CrmSpinLead
from app.models.lead_origin import CRM_KEY_PREFIX, CRM_ORIGINS, ORIGIN_SHEET, new_crm_key
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.services import phones
from app.services.crm_leads.fields import (
    Cleaned, RowContext, as_text, clean_row, from_model, json_values, norm_header, to_model_kwargs,
)
from app.services.sheet_sync import _month_to_date

logger = logging.getLogger(__name__)

# source -> (model, key column name, "created" column name)
TABLES = {
    "spin": (CrmSpinLead, None, "created_at"),
    "whatsapp": (WhatsAppLead, "sheet_key", "first_seen_at"),
    "wedding": (WeddingLead, "sheet_key", "first_seen_at"),
    "wedding_planner": (WeddingPlan, "plan_id", "first_seen_at"),
}
DUPLICATE_REASONS = ("in_file", "existing_sheet", "existing_workflow", "existing_added")


class WorkflowUnavailable(Exception):
    """The workflow database can't be checked for duplicates right now."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)      # MySQL gives naive UTC
    return value.isoformat()


def lead_key_of(source: str, lead) -> str:
    if source == "spin":
        return lead.lead_key
    return getattr(lead, TABLES[source][1])


def created_of(source: str, lead) -> datetime | None:
    return getattr(lead, TABLES[source][2])


# ── Looking leads up ───────────────────────────────────────────────────
def find_crm_lead(db: Session, source: str, lead_key: str):
    """The added/imported lead with this crm-… key (archived included), or None."""
    if not lead_key.startswith(CRM_KEY_PREFIX):
        return None
    model, key_col, _ = TABLES[source]
    if source == "spin":
        number = lead_key[len(CRM_KEY_PREFIX):]
        return db.get(CrmSpinLead, int(number)) if number.isdigit() else None
    return db.exec(
        select(model).where(getattr(model, key_col) == lead_key, model.origin.in_(CRM_ORIGINS))
    ).first()


def is_sheet_lead(db: Session, source: str, lead_key: str) -> bool:
    if source == "spin":
        return lead_key.isdigit()          # a workflow lead id
    model, key_col, _ = TABLES[source]
    return db.exec(
        select(model.id).where(getattr(model, key_col) == lead_key, model.origin == ORIGIN_SHEET)
    ).first() is not None


def context(db: Session) -> RowContext:
    branches = list(db.exec(select(Branch)))
    return RowContext(
        branches_by_name={norm_header(b.name): b.id for b in branches},
        branch_ids={b.id for b in branches},
    )


def out(source: str, lead) -> dict:
    return {
        "source": source,
        "lead_key": lead_key_of(source, lead),
        "origin": lead.origin,
        "archived": bool(lead.is_deleted),
        "created_at": _iso(created_of(source, lead)),
        "updated_at": _iso(lead.updated_at),
        "values": from_model(source, lead),
    }


# ── Duplicates ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Match:
    source: str
    kind: str                     # sheet | workflow | added
    lead_key: str
    created_at: datetime | None
    archived: bool


def _chunks(items: list[str], size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def workflow_phones(e164s: set[str]) -> dict[str, list[str]]:
    """{e164: [spin_lead ids]} for workflow leads with these phones. Read-only.
    No workflow database configured = no workflow leads. Raises WorkflowUnavailable on errors."""
    from app.db.session import read_engine

    if read_engine is None or not e164s:
        return {}
    try:
        from sqlalchemy import select as sa_select

        from app.api.v1.endpoints import spin_leads as sl

        table = sl._get_table()
        pk = list(table.primary_key.columns)[0]
        tail = func.right(func.regexp_replace(cast(table.c.phone, Text), r"\D", "", "g"), 10)
        found: dict[str, list[str]] = {}
        with read_engine.connect() as conn:
            for chunk in _chunks(sorted({phones.last10(e) for e in e164s})):
                for lead_id, phone in conn.execute(sa_select(pk, table.c.phone).where(tail.in_(chunk))):
                    e164 = phones.normalize(phone, lenient=True).e164
                    if e164 in e164s:
                        found.setdefault(e164, []).append(str(lead_id))
        return found
    except Exception as exc:  # noqa: BLE001
        logger.warning("Duplicate check: workflow database unavailable (%s)", type(exc).__name__)
        raise WorkflowUnavailable() from exc


def lookup(db: Session, e164s: set[str]) -> dict[str, list[Match]]:
    """Every lead in every source with one of these phones (archived and removed rows included)."""
    found: dict[str, list[Match]] = {}
    if not e164s:
        return found
    for source, (model, _, created_col) in TABLES.items():
        for chunk in _chunks(sorted(e164s)):
            for lead in db.exec(select(model).where(model.phone_e164.in_(chunk))):
                kind = "sheet" if lead.origin == ORIGIN_SHEET else "added"
                found.setdefault(lead.phone_e164, []).append(
                    Match(source, kind, lead_key_of(source, lead), getattr(lead, created_col), bool(lead.is_deleted))
                )
    for e164, ids in workflow_phones(e164s).items():
        for lead_id in ids:
            found.setdefault(e164, []).append(Match("spin", "workflow", lead_id, None, False))
    return found


def classify(source: str, e164: str, matches: dict[str, list[Match]], exclude_key: str | None = None):
    """(duplicate dict or None, [other sources where the phone exists])."""
    rows = [m for m in matches.get(e164, []) if m.lead_key != exclude_key]
    same = [m for m in rows if m.source == source]
    others = sorted({m.source for m in rows if m.source != source})
    duplicate = None
    existing = next((m for m in same if m.kind in ("sheet", "workflow")), None)
    if existing:
        duplicate = {"reason": f"existing_{existing.kind}", "lead_key": existing.lead_key}
    else:
        added = next((m for m in same if m.kind == "added"), None)
        if added:
            duplicate = {
                "reason": "existing_added", "lead_key": added.lead_key,
                "created_at": _iso(added.created_at), "archived": added.archived,
            }
    return duplicate, others


def check_rows(db: Session, source: str, rows: list[tuple[int, dict[str, Any]]]) -> tuple[list[dict], dict, list[Cleaned]]:
    """Validate rows and find duplicates. Returns (row results, counts, cleaned rows).
    A row is "ok" only if it has no errors and is not a duplicate."""
    ctx = context(db)
    cleaned = [clean_row(source, raw, ctx) for _, raw in rows]
    matches = lookup(db, {c.phone.e164 for c in cleaned if c.ok and c.phone and c.phone.e164})

    results: list[dict] = []
    first_row_for: dict[str, int] = {}
    counts = {"total": len(rows), "ok": 0, "errors": 0, "duplicates": 0,
              "duplicates_by_reason": {r: 0 for r in DUPLICATE_REASONS}, "other_source_warnings": 0}
    for (row_number, raw), c in zip(rows, cleaned):
        values = json_values(c.values)
        for error in c.errors:      # send back what was written, so the bulk save rejects it again
            values[error["field"]] = as_text(raw.get(error["field"])) or None
        result: dict[str, Any] = {
            "row": row_number, "values": values, "errors": c.errors, "duplicate": None, "warnings": [],
        }
        if not c.ok:
            result["status"] = "error"
            counts["errors"] += 1
            results.append(result)
            continue
        e164 = c.phone.e164
        duplicate, others = classify(source, e164, matches)
        if duplicate is None and e164 in first_row_for:
            duplicate = {"reason": "in_file", "duplicate_of_row": first_row_for[e164]}
        first_row_for.setdefault(e164, row_number)
        if others:
            result["warnings"].append({"type": "other_source", "sources": others})
            counts["other_source_warnings"] += 1
        result["phone_e164"] = e164
        if duplicate:
            result["duplicate"] = duplicate
            result["status"] = "duplicate"
            counts["duplicates"] += 1
            counts["duplicates_by_reason"][duplicate["reason"]] += 1
        else:
            result["status"] = "ok"
            counts["ok"] += 1
        results.append(result)
    return results, counts, cleaned


# ── Create / edit / archive ────────────────────────────────────────────
def _apply(source: str, lead, kw: dict[str, Any]) -> None:
    details = kw.pop("_details", None)
    prios = kw.pop("_priorities", None)
    recompute_date = kw.pop("_wedding_date", False)
    for key, value in kw.items():
        setattr(lead, key, value)
    if details is not None:
        merged = dict(lead.additional_details or {})
        for key, value in details.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        lead.additional_details = merged                 # new dict so the JSON change is saved
    if prios is not None:
        current = list(lead.priorities or []) + [None, None, None]
        slots = {f"priority{i + 1}": current[i] for i in range(3)}
        slots.update(prios)
        lead.priorities = [slots[f"priority{i}"] for i in (1, 2, 3) if slots[f"priority{i}"]]
    if recompute_date:
        lead.wedding_date = lead.first_day or (_month_to_date(lead.wedding_month) if lead.wedding_month else None)


def create(db: Session, source: str, c: Cleaned, origin: str, user_id: int | None):
    """Add one lead (not committed). The caller checked duplicates."""
    now = _now()
    kw = to_model_kwargs(source, c.values, c.phone)
    if source == "spin":
        lead = CrmSpinLead(origin=origin, phone=kw["phone"], phone_e164=kw["phone_e164"], source="crm",
                           created_at=now, created_by=user_id, updated_at=now, additional_details={})
    elif source == "whatsapp":
        lead = WhatsAppLead(sheet_key=new_crm_key(), origin=origin, phone=kw["phone"], row_hash="",
                            first_seen_at=now, updated_at=now)
    elif source == "wedding":
        repeat = db.exec(select(WeddingLead.id).where(WeddingLead.phone_e164 == kw["phone_e164"])).first() is not None
        lead = WeddingLead(sheet_key=new_crm_key(), origin=origin, row_hash="", is_repeat=repeat,
                           first_seen_at=now, updated_at=now)
    else:
        lead = WeddingPlan(plan_id=new_crm_key(), origin=origin, versions=1, row_hash="", events=[], priorities=[],
                           updated_in_sheet_at=now, first_seen_at=now, updated_at=now)
    _apply(source, lead, kw)
    if source in ("whatsapp", "wedding") and lead.submitted_at is None:
        lead.submitted_at = now
    db.add(lead)
    db.flush()                                  # gives spin leads their id (crm-<id>)
    return lead


def update(db: Session, source: str, lead, c: Cleaned) -> None:
    kw = to_model_kwargs(source, c.values, c.phone)
    _apply(source, lead, kw)
    lead.updated_at = _now()
    db.add(lead)


def set_archived(db: Session, lead, archived: bool) -> None:
    """Archived = hidden from lists, counts and exports. The phone still counts in duplicate
    checks and the lead's crm_lead_meta row is kept."""
    lead.is_deleted = archived
    lead.updated_at = _now()
    db.add(lead)


def clear_caches() -> None:
    from app.core import response_cache

    for prefix in ("vendors", "analytics", "branches"):
        response_cache.clear(prefix)


# ── Filling phone_e164 on existing sheet rows ──────────────────────────
def backfill_phone_e164(db: Session) -> int:
    """Sheet rows synced before phone_e164 existed: fill it from the stored phone (digits with
    country code). "" marks numbers that can't be read, so they aren't retried every time."""
    filled = 0
    for model in (WhatsAppLead, WeddingLead, WeddingPlan):
        for lead in db.exec(select(model).where(model.phone_e164.is_(None))):
            e164 = None
            if lead.phone:
                e164 = phones.normalize("+" + lead.phone, lenient=True).e164
            lead.phone_e164 = e164 or ""
            db.add(lead)
            filled += 1
    if filled:
        db.commit()
    return filled
