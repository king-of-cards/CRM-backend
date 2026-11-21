"""Added/imported leads: create, edit, archive, look up, and duplicate checks.

A duplicate is a lead of the same source whose EVERY template field matches (duplicate_fields:
the lead's own fields, without read-only, CRM and arrival-date columns), compared after
lead_values.dup_norm (trim, spaces, any case, dates as ISO, N/A-like = empty) with the phone as
E.164. The same phone with any other field different is not a duplicate. Reasons:
  existing_sheet     same source, a row copied from the Google Sheet (also rows since removed from it)
  existing_workflow  spin: a workflow row (PostgreSQL spin_lead, listed or not) created on or after
                     the spin cut-off (SPIN_LIVE_FROM). Older rows are ignored, so the spin history
                     can be uploaded (see spin_leads.spin_filter).
                     koc_to_dhruva: a live workflow row in the KOC -> Dhruva rule.
  existing_added     same source, a lead added or imported in the CRM (archived ones too)
  in_file            the same lead earlier in the same upload (duplicate_of_row)
  other_source       not blocking: the phone exists in other sources (listed; phone only)
koc_to_dhruva (KOC -> Dhruva uploads, crm_koc_dhruva_leads) is looked up on its own: never an
other_source of the four KOC -> Vendors sources, nor they of it.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NamedTuple

from sqlalchemy import Text, cast, func
from sqlmodel import Session, select

from app.models.branch import Branch
from app.models.crm_spin_lead import CrmSpinLead
from app.models.koc_dhruva_lead import KOC_KEY_PREFIX, KocDhruvaLead
from app.models.lead_origin import CRM_KEY_PREFIX, CRM_ORIGINS, ORIGIN_SHEET, new_crm_key
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.services import lead_values, phones
from app.services.crm_leads.fields import (
    DETAIL_SOURCES, FIELDS, KOC, KOC_CRM_FIELDS, SPIN_SOURCES, VIRTUAL_KEYS, Cleaned, RowContext, as_text,
    clean_row, from_model, json_values, norm_header, to_model_kwargs,
)
from app.services.sheet_sync import _month_to_date
from app.services.stores import store_for

logger = logging.getLogger(__name__)

# source -> (model, key column name, "created" column name)
TABLES = {
    "spin": (CrmSpinLead, None, "created_at"),
    "whatsapp": (WhatsAppLead, "sheet_key", "first_seen_at"),
    "wedding": (WeddingLead, "sheet_key", "first_seen_at"),
    "wedding_planner": (WeddingPlan, "plan_id", "first_seen_at"),
    KOC: (KocDhruvaLead, None, "created_at"),
}
KOC_VENDOR_SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
# Not compared by the duplicate check: the phone (matched as E.164 first), the arrival dates
# (empty = upload time) and the virtual upload columns (Assigned to, Location, Wedding date)
DUPLICATE_SKIP = frozenset(VIRTUAL_KEYS | {"phone", "created_at", "submitted_at", "updated"})
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
    if source in DETAIL_SOURCES:
        return lead.lead_key
    return getattr(lead, TABLES[source][1])


def created_of(source: str, lead) -> datetime | None:
    return getattr(lead, TABLES[source][2])


# ── Looking leads up ───────────────────────────────────────────────────
def find_crm_lead(db: Session, source: str, lead_key: str):
    """The added/imported lead with this crm-… key (archived included), or None."""
    prefix = KOC_KEY_PREFIX if source == KOC else CRM_KEY_PREFIX
    if not lead_key.startswith(prefix):
        return None
    model, key_col, _ = TABLES[source]
    if source in DETAIL_SOURCES:
        number = lead_key[len(prefix):]
        return db.get(model, int(number)) if number.isdigit() else None
    return db.exec(
        select(model).where(getattr(model, key_col) == lead_key, model.origin.in_(CRM_ORIGINS))
    ).first()


def is_sheet_lead(db: Session, source: str, lead_key: str) -> bool:
    if source in SPIN_SOURCES:
        return lead_key.isdigit()          # a workflow lead id
    model, key_col, _ = TABLES[source]
    return db.exec(
        select(model.id).where(getattr(model, key_col) == lead_key, model.origin == ORIGIN_SHEET)
    ).first() is not None


def context(db: Session) -> RowContext:
    from app.services.lead_crm import active_vendors

    from app.services.stores import STORES, branch_ids_by_name

    branches = list(db.exec(select(Branch)))
    return RowContext(
        # The Branches page's names, plus the 12 built-in stores (crm_branches can be empty)
        branches_by_name=branch_ids_by_name([{"id": b.id, "name": b.name} for b in branches]),
        branch_ids={b.id for b in branches} | set(STORES),
        vendors_by_name={norm_header(v.name): (v.id, v.name) for v in active_vendors(db)},
    )


def save_assignment(db: Session, source: str, lead_key: str, assignment, user_id: int | None,
                    *, empty_means_unassigned: bool, crm: dict[str, Any] | None = None) -> None:
    """"Assigned to" (and, from uploads, Update / Quoted amount / Conversion amount) of a NEW
    lead -> crm_lead_meta (not committed).
      manual vendor   vendor_id = that vendor
      "<vendor> · auto" automatic (no vendor stored)
      Unassigned      auto_assign = False: stays unassigned until an admin assigns it
      empty           uploads: unassigned; single add: automatic
    No row is written when nothing needs storing (automatic, no CRM values)."""
    from app.models.lead_meta import LeadMeta

    if assignment is None:
        assignment = ("none", None) if empty_means_unassigned else ("auto", None)
    mode, vendor_id = assignment
    crm = {k: v for k, v in (crm or {}).items() if v is not None}
    if mode == "auto" and not crm:
        return
    db.add(LeadMeta(
        source=source, lead_key=lead_key, updated_at=_now(), updated_by=user_id,
        vendor_id=vendor_id if mode == "manual" else None, auto_assign=mode != "none", **crm,
    ))


def save_koc_crm(db: Session, lead_key: str, values: dict[str, Any], user_id: int | None) -> None:
    """KOC -> Dhruva upload: the row's Update, comments, amounts and follow-up -> the section's CRM
    details (crm_lead_meta, source koc_to_dhruva; never a vendor). Nothing stored when all are empty."""
    from app.models.lead_meta import LeadMeta

    crm = {f.key: values.get(f.key) for f in KOC_CRM_FIELDS if values.get(f.key) is not None}
    if crm:
        db.add(LeadMeta(source=KOC, lead_key=lead_key, updated_at=_now(), updated_by=user_id, **crm))


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
def duplicate_fields(source: str):
    """The fields the duplicate check compares (see the module docstring)."""
    return [f for f in FIELDS[source] if f.key not in DUPLICATE_SKIP]


def _comparable(value: Any, kind: str) -> str:
    if kind == "budget" and not lead_values.is_na(value):
        # Wedding budget: "1.5 lakh", "₹1,50,000" and an export's 150000 are the same amount
        from app.services.sheet_sync import parse_budget

        amount = parse_budget(as_text(value))
        if amount is not None:
            return str(amount)
    if kind == "event_date" and isinstance(value, str) and not lead_values.is_na(value):
        # Event dates as the sheet sync reads them ("12/20/2026", "20 Dec 2026", "2026-12-20" …)
        from app.services.sheet_sync import parse_event_date

        when = parse_event_date(value)
        if when is not None:
            return when.isoformat()
    return lead_values.dup_norm(value, kind)


def signature(source: str, values: dict[str, Any] | None) -> tuple:
    """The lead's comparable values (lead_values.dup_norm), one per duplicate field."""
    values = values or {}
    return tuple(_comparable(values.get(f.key), f.type) for f in duplicate_fields(source))


@dataclass(frozen=True)
class Match:
    source: str
    kind: str                     # sheet | workflow | added
    lead_key: str
    created_at: datetime | None
    archived: bool
    values: dict | None = field(default=None, compare=False, hash=False)    # form values (from_model)


def _chunks(items: list[str], size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class WorkflowHit(NamedTuple):
    lead_id: str
    live: bool          # created on or after the spin cut-off (spin_leads.is_live)
    in_spin: bool       # live and listed: shown in spin (spin_leads.spin_filter)
    values: dict | None = None    # the row as spin template values (workflow_values)
    koc: bool = False             # in the KOC -> Dhruva rule (budget / quantity)


def workflow_values(columns: dict[str, Any], details: dict[str, Any] | None) -> dict[str, Any]:
    """A workflow row -> the spin (and KOC -> Dhruva) upload field values: additional_details keys
    for detail fields, branch_id for the branch, the table's columns for the rest."""
    details = details if isinstance(details, dict) else {}
    out: dict[str, Any] = {}
    for f in FIELDS["spin"]:
        if f.detail:
            out[f.key] = details.get(f.key)
        elif f.key == "branch":
            out["branch"] = columns.get("branch_id")
        else:
            out[f.key] = columns.get(f.key)
    return out


def workflow_phones(e164s: set[str]) -> dict[str, list[WorkflowHit]]:
    """{e164: [WorkflowHit]} for workflow rows with these phones, over EVERY row of the table (any
    date, listed or not); callers pick what counts (the spin duplicate check: live rows; also_in:
    in_spin, and every row for KOC -> Dhruva). Read-only. No workflow database configured = no
    workflow leads. Raises WorkflowUnavailable on errors."""
    from app.db.session import read_engine

    if read_engine is None or not e164s:
        return {}
    try:
        from sqlalchemy import and_, false
        from sqlalchemy import select as sa_select

        from app.api.v1.endpoints import spin_leads as sl

        table = sl._get_table()
        pk = list(table.primary_key.columns)[0]
        tail = func.right(func.regexp_replace(cast(table.c.phone, Text), r"\D", "", "g"), 10)
        live = func.coalesce(and_(*sl.live_filter(table)), false()).label("live")
        in_spin = func.coalesce(and_(*sl.spin_filter(table)), false()).label("in_spin")
        names = [c for c in ("name", "email", "occasion", "branch_id") if c in table.c]
        extra = [table.c[c] for c in names]
        details_col = table.c.get(sl.REQUIRED_JSON_COLUMN)
        found: dict[str, list[WorkflowHit]] = {}
        with read_engine.connect() as conn:
            for chunk in _chunks(sorted({phones.last10(e) for e in e164s})):
                cols = [pk, table.c.phone, live, in_spin, *extra] + ([details_col] if details_col is not None else [])
                for row in conn.execute(sa_select(*cols).where(tail.in_(chunk))):
                    lead_id, phone, is_live, listed = row[:4]
                    e164 = phones.normalize(phone, lenient=True).e164
                    if e164 not in e164s:
                        continue
                    columns = dict(zip(names, row[4:4 + len(names)]))
                    details = row[-1] if details_col is not None else {}
                    found.setdefault(e164, []).append(WorkflowHit(
                        str(lead_id), bool(is_live), bool(listed), workflow_values(columns, details),
                        sl.koc_reason(details) is not None))
        return found
    except Exception as exc:  # noqa: BLE001
        logger.warning("Duplicate check: workflow database unavailable (%s)", type(exc).__name__)
        raise WorkflowUnavailable() from exc


def lookup(db: Session, e164s: set[str], source: str | None = None) -> dict[str, list[Match]]:
    """Every lead with one of these phones (archived and removed rows included), with its values.
    source koc_to_dhruva: only KOC -> Dhruva uploads and live workflow rows in its rule; any other
    (or None): the four KOC -> Vendors sources and live workflow rows as spin."""
    found: dict[str, list[Match]] = {}
    if not e164s:
        return found
    koc = source == KOC
    for name in ((KOC,) if koc else KOC_VENDOR_SOURCES):
        model, _, created_col = TABLES[name]
        for chunk in _chunks(sorted(e164s)):
            for lead in db.exec(select(model).where(model.phone_e164.in_(chunk))):
                kind = "sheet" if lead.origin == ORIGIN_SHEET else "added"
                found.setdefault(lead.phone_e164, []).append(
                    Match(name, kind, lead_key_of(name, lead), getattr(lead, created_col), bool(lead.is_deleted),
                          from_model(name, lead))
                )
    known = None
    for e164, hits in workflow_phones(e164s).items():
        for hit in hits:
            if not hit.live or (koc and not hit.koc):    # rows before the spin cut-off never count
                continue
            values = dict(hit.values or {})
            if values.get("branch") is not None:         # a branch id nobody knows reads as empty (as in exports)
                if known is None:
                    from app.services.branches import branches_by_id
                    known = list(branches_by_id(db).values())
                store = store_for(values["branch"], known)
                values["branch"] = store["id"] if store else None
            found.setdefault(e164, []).append(Match(KOC if koc else "spin", "workflow", hit.lead_id, None, False,
                                                    values))
    return found


def classify(source: str, e164: str, matches: dict[str, list[Match]], exclude_key: str | None = None,
             values: dict[str, Any] | None = None):
    """(duplicate dict or None, [other sources where the phone exists]). values: the lead's form
    values; a same-phone lead is a duplicate only when every duplicate field matches too."""
    rows = [m for m in matches.get(e164, []) if m.lead_key != exclude_key]
    others = sorted({m.source for m in rows if m.source != source})
    wanted = signature(source, values)
    same = [m for m in rows if m.source == source and signature(source, m.values) == wanted]
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

    Status, in this order (every row gets exactly one):
      duplicate  the phone matches an existing lead (existing_*), or an earlier row of the file
                 (in_file); counted as a duplicate even if other fields have errors, which are
                 still returned for information
      error      no usable phone, or field errors
      ok         will be imported"""
    ctx = context(db)
    cleaned = [clean_row(source, raw, ctx, upload=True) for _, raw in rows]
    phone_of = [c.phone.e164 if c.phone is not None and c.phone.e164 else None for c in cleaned]
    matches = lookup(db, {e for e in phone_of if e}, source)

    results: list[dict] = []
    first_row_for: dict[tuple, int] = {}         # (phone, signature) -> first row
    counts = {"total": len(rows), "ok": 0, "errors": 0, "duplicates": 0,
              "duplicates_by_reason": {r: 0 for r in DUPLICATE_REASONS}, "other_source_warnings": 0}
    for (row_number, raw), c, e164 in zip(rows, cleaned, phone_of):
        values = json_values(c.values)
        for error in c.errors:      # send back what was written, so the bulk save rejects it again
            values[error["field"]] = as_text(raw.get(error["field"])) or None
        result: dict[str, Any] = {
            "row": row_number, "values": values, "errors": c.errors, "duplicate": None, "warnings": [],
        }
        duplicate, others = None, []
        if e164:
            # Existing lead first, then an earlier row of the same file (every field the same)
            duplicate, others = classify(source, e164, matches, values=c.values)
            same_lead = (e164, signature(source, c.values))
            if duplicate is None and same_lead in first_row_for:
                duplicate = {"reason": "in_file", "duplicate_of_row": first_row_for[same_lead]}
            first_row_for.setdefault(same_lead, row_number)
            result["phone_e164"] = e164
        if others:
            result["warnings"].append({"type": "other_source", "sources": others})
            counts["other_source_warnings"] += 1
        if duplicate:
            result["duplicate"] = duplicate
            result["status"] = "duplicate"
            counts["duplicates"] += 1
            counts["duplicates_by_reason"][duplicate["reason"]] += 1
        elif not c.ok:
            result["status"] = "error"
            counts["errors"] += 1
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
    if source in DETAIL_SOURCES:
        model = KocDhruvaLead if source == KOC else CrmSpinLead
        lead = model(origin=origin, phone=kw["phone"], phone_e164=kw["phone_e164"], source="crm",
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

    for prefix in ("vendors", "analytics", "branches", "access"):
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
