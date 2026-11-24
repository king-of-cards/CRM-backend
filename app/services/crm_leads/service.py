"""Added/imported leads: create, edit, archive, look up, and duplicate checks.

A duplicate is a lead of the same source with the same phone (E.164) and no conflicting field:
every field filled in the new row (duplicate_fields: the lead's own fields, without read-only, CRM
and arrival-date columns) equals the existing lead's value, compared after lead_values.dup_norm
(trim, spaces, any case, dates as ISO, N/A-like = empty). A field empty on either side is ignored;
a filled field that differs from a filled existing value makes it another lead. Reasons:
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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NamedTuple

from sqlalchemy import Text, cast, func
from sqlmodel import Session, col, select

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


def insert_rows(db: Session, objects: list) -> None:
    """New model objects (no id needed back) in one multi-row INSERT per table, not one INSERT
    each (MySQL can't batch ORM inserts that read ids back). Not committed."""
    from sqlalchemy import insert

    by_model: dict[type, list] = {}
    for obj in objects:
        by_model.setdefault(type(obj), []).append(obj)
    for model, rows in by_model.items():
        columns = [c.name for c in model.__table__.columns if not c.primary_key]
        db.execute(insert(model.__table__), [{name: getattr(row, name) for name in columns} for row in rows])


def save_assignment(db: Session, source: str, lead_key: str, assignment, user_id: int | None,
                    *, empty_means_unassigned: bool, crm: dict[str, Any] | None = None,
                    out: list | None = None) -> None:
    """"Assigned to" (and, from uploads, Update / Quoted amount / Conversion amount) of a NEW
    lead (not committed).
      manual vendor(s)  crm_lead_assignments "added" rows; the CRM values go to the first one's copy
      "<vendor> · auto" automatic (nothing stored); CRM values on the vendor-less row, moved to the
                        main automatic vendor's copy after the commit (place_auto_copies)
      Unassigned        auto_assign = False on the vendor-less row: stays unassigned until an admin assigns it
      empty             uploads: unassigned; single add: automatic
    No row is written when nothing needs storing (automatic, no CRM values). Returns True when CRM
    values wait on the vendor-less row of an automatic lead (call place_auto_copies after the commit).
    out: collect the new rows there (a bulk save inserts them with insert_rows) instead of db.add."""
    from app.models.lead_assignment import LeadAssignment
    from app.models.lead_meta import LeadMeta

    add = db.add if out is None else out.append

    if assignment is None:
        assignment = ("none", []) if empty_means_unassigned else ("auto", [])
    mode, vendor_ids = assignment
    if isinstance(vendor_ids, int):                 # (mode, one id): older callers
        vendor_ids = [vendor_ids]
    vendor_ids = list(vendor_ids or [])
    crm = {k: v for k, v in (crm or {}).items() if v is not None}
    now = _now()
    if mode == "manual" and vendor_ids:
        for vendor_id in vendor_ids:
            add(LeadAssignment(source=source, lead_key=lead_key, vendor_id=vendor_id, state="added",
                               updated_by=user_id, updated_at=now))
        if crm:
            add(LeadMeta(source=source, lead_key=lead_key, copy_vendor_id=vendor_ids[0], updated_at=now,
                         updated_by=user_id, **crm))
        return False
    if mode != "none" and not crm:
        return False
    add(LeadMeta(source=source, lead_key=lead_key, updated_at=now, updated_by=user_id,
                 auto_assign=mode != "none", **crm))
    return mode != "none"


def place_auto_copies(db: Session, source: str, keys: list[str]) -> None:
    """After an upload's commit: the CRM values of new automatic leads (on their vendor-less row)
    become their main automatic vendor's copy (its partner vendor's when it has no main one), so the
    vendor sees them. Leads without a vendor keep the vendor-less row. Committed here; best effort
    (left on the vendor-less row, where admins still see them, if the vendors can't be worked out)."""
    from app.models.lead_meta import LeadMeta
    from app.services import access

    if not keys:
        return
    try:
        access.forget()
        entries = access.vendor_entries(db, source)
        copies: dict[tuple[str, int | None], LeadMeta] = {}       # every copy of these leads, in one query per 500
        for chunk in _chunks(sorted({str(k) for k in keys})):
            for m in db.exec(select(LeadMeta).where(LeadMeta.source == source, col(LeadMeta.lead_key).in_(chunk))
                             .order_by(LeadMeta.id)):
                copies.setdefault((m.lead_key, m.copy_vendor_id), m)
        for key in keys:
            vendors = entries.get(str(key)) or []
            target = next((vid for vid, how in vendors if how != "partner"), vendors[0][0] if vendors else None)
            base = copies.get((str(key), 0))
            if target is None or base is None or base.auto_assign is False:
                continue
            if copies.get((str(key), target)) is not None:
                continue
            base.copy_vendor_id = target
            db.add(base)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("Upload: could not move CRM values to the vendors' copies", exc_info=True)


def save_koc_crm(db: Session, lead_key: str, values: dict[str, Any], user_id: int | None,
                 out: list | None = None) -> None:
    """KOC -> Dhruva upload: the row's Update, comments, amounts and follow-up -> the section's CRM
    details (crm_lead_meta, source koc_to_dhruva; never a vendor). Nothing stored when all are empty."""
    from app.models.lead_meta import LeadMeta

    crm = {f.key: values.get(f.key) for f in KOC_CRM_FIELDS if values.get(f.key) is not None}
    if crm:
        (db.add if out is None else out.append)(LeadMeta(source=KOC, lead_key=lead_key, updated_at=_now(),
                                                         updated_by=user_id, **crm))


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


DEFAULT_LEAD_SOURCE = "crm"      # source column of added / uploaded leads without a Source


def _dup_value(values: dict[str, Any], key: str) -> Any:
    value = values.get(key)
    if key == "source" and isinstance(value, str) and value.strip().lower() == DEFAULT_LEAD_SOURCE:
        return None                  # "crm" = no Source given: never conflicts
    return value


def signature(source: str, values: dict[str, Any] | None) -> tuple:
    """The lead's comparable values (lead_values.dup_norm), one per duplicate field."""
    values = values or {}
    return tuple(_comparable(_dup_value(values, f.key), f.type) for f in duplicate_fields(source))


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
    created: datetime | None = None   # created_at (naive UTC), to pick between rows with one phone


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


def workflow_phones(e164s: set[str], by_last10: bool = False) -> dict[str, list[WorkflowHit]]:
    """{e164: [WorkflowHit]} for workflow rows with these phones, over EVERY row of the table (any
    date, listed or not); callers pick what counts (the spin duplicate check: live rows; also_in:
    in_spin, and every row for KOC -> Dhruva). by_last10: every row whose phone ends with the same
    10 digits, keyed by those 10 digits. Read-only. No workflow database configured = no workflow
    leads. Raises WorkflowUnavailable on errors."""
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
        created_col = sl._created_column(table)
        names_created = names + (["created_at"] if created_col is not None else [])
        extra = [table.c[c] for c in names] + ([created_col] if created_col is not None else [])
        details_col = table.c.get(sl.REQUIRED_JSON_COLUMN)
        found: dict[str, list[WorkflowHit]] = {}
        with read_engine.connect() as conn:
            for chunk in _chunks(sorted({phones.last10(e) for e in e164s})):
                cols = [pk, table.c.phone, live, in_spin, *extra] + ([details_col] if details_col is not None else [])
                for row in conn.execute(sa_select(*cols).where(tail.in_(chunk))):
                    lead_id, phone, is_live, listed = row[:4]
                    if by_last10:
                        e164 = re.sub(r"\D", "", str(phone or ""))[-10:]
                    else:
                        e164 = phones.normalize(phone, lenient=True).e164
                        if e164 not in e164s:
                            continue
                    columns = dict(zip(names_created, row[4:4 + len(names_created)]))
                    details = row[-1] if details_col is not None else {}
                    found.setdefault(e164, []).append(WorkflowHit(
                        str(lead_id), bool(is_live), bool(listed), workflow_values(columns, details),
                        sl.koc_reason(details) is not None, sl.to_naive_utc(columns.get("created_at"))))
        return found
    except Exception as exc:  # noqa: BLE001
        logger.warning("Duplicate check: workflow database unavailable (%s)", type(exc).__name__)
        raise WorkflowUnavailable() from exc


def lookup(db: Session, e164s: set[str], source: str | None = None,
           workflow_out: dict | None = None) -> dict[str, list[Match]]:
    """Every lead with one of these phones (archived and removed rows included), with its values.
    source koc_to_dhruva: only KOC -> Dhruva uploads and live workflow rows in its rule; any other
    (or None): the four KOC -> Vendors sources and live workflow rows as spin. workflow_out: filled
    with every workflow row found ({e164: [WorkflowHit]}, any date) for the spin fill."""
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
    workflow = workflow_phones(e164s)
    if workflow_out is not None:
        workflow_out.update(workflow)
    for e164, hits in workflow.items():
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
    values; a same-phone lead is a duplicate only when no field conflicts (lead_values.compatible)."""
    rows = [m for m in matches.get(e164, []) if m.lead_key != exclude_key]
    others = sorted({m.source for m in rows if m.source != source})
    wanted = signature(source, values)
    same = [m for m in rows if m.source == source
            and lead_values.compatible(wanted, signature(source, m.values))]
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


def uploaded_spin_duplicates(db: Session) -> list[dict]:
    """Read-only: CRM spin leads (crm_spin_leads, not archived) that are the same lead as a live
    workflow row under the duplicate rule (same phone, no conflicting filled field), e.g. today's
    leads uploaded again. [{id, lead_key, name, phone, created_at, workflow_ids}]. Raises
    WorkflowUnavailable when the workflow database can't be read."""
    leads = list(db.exec(select(CrmSpinLead).where(CrmSpinLead.is_deleted == False)))  # noqa: E712
    matches = lookup(db, {lead.phone_e164 for lead in leads if lead.phone_e164}, "spin")
    out = []
    for lead in leads:
        mine = signature("spin", from_model("spin", lead))
        hits = [m.lead_key for m in matches.get(lead.phone_e164, [])
                if m.kind == "workflow" and lead_values.compatible(mine, signature("spin", m.values))]
        if hits:
            out.append({"id": lead.id, "lead_key": lead.lead_key, "name": lead.name, "phone": lead.phone,
                        "created_at": lead.created_at, "workflow_ids": sorted(hits, key=int)})
    return out


# ── Filling uploaded spin leads from the workflow ("Spin game") ────────
# The upload's filled values of these fields must not conflict with the workflow row it is filled from
FILL_CONFLICT_KEYS = ("name", "occasionDate", "occasionType", "branch")
FILL_SKIP_KEYS = frozenset(VIRTUAL_KEYS | {"phone", "created_at"})     # never filled
FILLED_ACTION = "filled_from_workflow"


def _known_branch(branch_id: Any, known: list[dict]) -> int | None:
    """A workflow branch id as the CRM stores it, or None when the CRM doesn't know it (as exports)."""
    if branch_id in (None, ""):
        return None
    store = store_for(branch_id, known)
    return store["id"] if store else None


def plan_fill(values: dict[str, Any], created: datetime | None, hits: list[WorkflowHit],
              known_branches: list[dict]) -> dict | None:
    """Which workflow row (any date) an uploaded spin lead is filled from, and with what.
    values: the lead's spin form values; created: its created date (naive UTC), None when the
    upload left it empty. -> None (no workflow row has the phone), {"skipped": reason}, or
    {"workflow_id", "fields": {key: value}} (only fields empty in the upload, filled in the row)."""
    if not hits:
        return None
    defs = {f.key: f for f in FIELDS["spin"]}

    def comparable(key, value):
        return lead_values.dup_norm(value, defs[key].type)

    candidates = []
    for hit in hits:
        theirs = dict(hit.values or {})
        theirs["branch"] = _known_branch(theirs.get("branch"), known_branches)
        if all(not comparable(k, values.get(k)) or not comparable(k, theirs.get(k))
               or comparable(k, values.get(k)) == comparable(k, theirs.get(k)) for k in FILL_CONFLICT_KEYS):
            candidates.append((hit, theirs))
    if not candidates:
        return {"skipped": "The Spin game leads with this phone have another name, event date, occasion or branch"}
    if len(candidates) > 1:
        if created is None:
            return {"skipped": f"{len(candidates)} Spin game leads with this phone and no created date to choose by"}
        when = created.astimezone(timezone.utc).replace(tzinfo=None) if created.tzinfo else created
        far = datetime.max.replace(year=9000)

        def distance(pair):
            return abs(((pair[0].created or far) - when).total_seconds())
        candidates.sort(key=distance)
        if distance(candidates[0]) == distance(candidates[1]):
            return {"skipped": "Two Spin game leads with this phone are equally close to the created date"}
    hit, theirs = candidates[0]
    fields = {f.key: theirs[f.key] for f in FIELDS["spin"]
              if f.key not in FILL_SKIP_KEYS and lead_values.is_na(values.get(f.key))
              and not lead_values.is_na(theirs.get(f.key))}           # empty in the upload, filled in the row
    return {"workflow_id": hit.lead_id, "fields": fields}


def fill_lead(lead: CrmSpinLead, fields: dict[str, Any]) -> dict[str, Any]:
    """Fill these (planned) fields on an uploaded spin lead, never overwriting a filled one (not
    committed). Returns the lead's previous name, email, occasion, branch_id and additional_details."""
    before = {"name": lead.name, "email": lead.email, "occasion": lead.occasion, "branch_id": lead.branch_id,
              "additional_details": dict(lead.additional_details or {})}
    details = dict(lead.additional_details or {})
    defs = {f.key: f for f in FIELDS["spin"]}
    for key, value in fields.items():
        if key == "branch":
            if lead.branch_id is None:
                lead.branch_id = int(value) if str(value).isdigit() else value
        elif defs[key].detail:
            if lead_values.is_na(details.get(key)):
                details[key] = value
        elif lead_values.is_na(getattr(lead, key)):
            setattr(lead, key, value)
    lead.additional_details = details                        # a new dict, so the JSON change is saved
    lead.updated_at = _now()
    return before


# ── KOC -> Dhruva uploads: empty fields from the workflow row of the phone ──
# Every field of a KOC -> Dhruva row the upload left empty is filled from the workflow row (any date)
# with the same last 10 phone digits, chosen as for Spin (plan_fill: no conflicting name, event
# date, occasion or branch; the closest created date when several), and stored in the row. Uploaded
# values are never overwritten. Received: the upload's, else the workflow row's created_at, else now.
KOC_FILL_KEYS = tuple(f.key for f in FIELDS[KOC] if f.key not in ("phone", "created_at", "source"))


def plan_koc_fill(values: dict[str, Any], hits: list[WorkflowHit], known_branches: list[dict]) -> dict | None:
    """-> None (no workflow row has the phone), {"skipped": reason}, or {"workflow_id", "fields",
    "received"}: the empty fields to fill, and the Received to use (None: keep the upload's)."""
    plan = plan_fill(values, values.get("created_at"), hits, known_branches)
    if plan is None or "skipped" in plan:
        return plan
    hit = next(h for h in hits if h.lead_id == plan["workflow_id"])
    received = None
    if values.get("created_at") is None and hit.created is not None:
        received = hit.created.replace(tzinfo=timezone.utc)          # naive UTC in the workflow
    return {"workflow_id": plan["workflow_id"], "received": received,
            "fields": {k: _koc_value(k, v) for k, v in plan["fields"].items() if k in KOC_FILL_KEYS}}


def _koc_value(key: str, value: Any) -> Any:
    """A workflow value as an upload stores it (quantity a whole number, pincode text, email lower case)."""
    kind = next(f.type for f in FIELDS[KOC] if f.key == key)
    if kind == "int" and not isinstance(value, bool):
        try:
            number = float(str(value).replace(",", "").strip())
            return int(number) if number == int(number) else value
        except ValueError:
            return value
    if kind == "pincode":
        return str(value).strip()
    if kind == "email" and isinstance(value, str):
        return value.strip().lower()
    return value


def plan_backfill(db: Session) -> list[dict]:
    """Read-only: for every uploaded spin lead (not archived) that a workflow row has the phone of,
    {id, lead_key, name, phone, workflow_id, fields} or {…, skipped}. Raises WorkflowUnavailable."""
    from app.services.branches import branches_by_id

    leads = list(db.exec(select(CrmSpinLead).where(CrmSpinLead.is_deleted == False)  # noqa: E712
                         .order_by(CrmSpinLead.id)))
    workflow = workflow_phones({lead.phone_e164 for lead in leads if lead.phone_e164})
    known = list(branches_by_id(db).values())
    out = []
    for lead in leads:
        plan = plan_fill(from_model("spin", lead), lead.created_at, workflow.get(lead.phone_e164, []), known)
        row = {"id": lead.id, "lead_key": lead.lead_key, "name": lead.name, "phone": lead.phone}
        if plan is None:
            row["skipped"] = "No Spin game lead with this phone"
        elif "skipped" in plan:
            row["skipped"] = plan["skipped"]
        elif not plan["fields"]:
            row.update(workflow_id=plan["workflow_id"], fields={}, skipped="Nothing empty to fill")
        else:
            row.update(workflow_id=plan["workflow_id"], fields=plan["fields"])
        out.append(row)
    return out


def apply_backfill(db: Session, plan: list[dict]) -> list[str]:
    """Fill the planned leads (not committed; the caller commits or rolls back in one transaction) and
    log one filled_from_workflow activity per lead (written with the caller's commit). Returns the undo
    SQL statements."""
    import json

    from app.models.lead_activity import LeadActivity
    from app.services.spin_history_rekey import sql_value

    undo = []
    for row in plan:
        if row.get("skipped") or not row.get("fields"):
            continue
        lead = db.get(CrmSpinLead, row["id"])
        before = fill_lead(lead, row["fields"])
        db.add(lead)
        db.add(LeadActivity(source="spin", lead_key=lead.lead_key, action=FILLED_ACTION,
                            field=", ".join(sorted(row["fields"])), old_value=None,
                            new_value=f"workflow lead {row['workflow_id']}", actor_user_id=None,
                            actor_name="Fill from Spin game", actor_role="system", created_at=_now()))
        details = json.dumps(before["additional_details"], ensure_ascii=False)
        undo.append(f"UPDATE crm_spin_leads SET name = {sql_value(before['name'])}, email = {sql_value(before['email'])}, "
                    f"occasion = {sql_value(before['occasion'])}, branch_id = {sql_value(before['branch_id'])}, "
                    f"additional_details = CAST({sql_value(details)} AS JSON) WHERE id = {lead.id};")
    undo.append(f"DELETE FROM crm_lead_activity WHERE source = 'spin' AND action = '{FILLED_ACTION}' "
                f"AND actor_name = 'Fill from Spin game' AND lead_key IN "
                f"({', '.join(sql_value(r['lead_key']) for r in plan if r.get('fields') and not r.get('skipped')) or 'NULL'});")
    db.flush()
    return undo


# ── KOC -> Dhruva uploads: the same lead in Spin (shown only) ─────────
# A KOC -> Dhruva upload row is matched with the Spin leads (as the spin list: uploaded and not
# archived, workflow rows in spin, i.e. on or after the cut-off) by the last 10 digits of the
# phone, only to show the link. A KOC -> Dhruva lead never creates a Spin lead and never runs vendor
# rules; matched Spin leads and their vendors are left as they are.
KOC_SPIN_CREATED_PREFIX = "from KOC → Dhruva "     # activity new_value of the Spin leads an older upload created


def _vendor_names(db: Session) -> dict[int, str]:
    from app.models.vendor import Vendor

    return {v.id: v.name for v in db.exec(select(Vendor))}


def spin_vendors(db: Session, keys: list[str], names: dict[int, str] | None = None) -> dict[str, list[dict]]:
    """{spin lead key: [{"id", "name"}]}: the lead's vendors now (lead_crm.vendor_set). Read-only."""
    from app.services import access

    entries = access.vendor_entries(db, "spin")
    names = _vendor_names(db) if names is None else names
    return {k: [{"id": vid, "name": names.get(vid)} for vid, _ in entries.get(str(k)) or []] for k in keys}


def spin_matches(db: Session, e164s: set[str]) -> dict[str, list[dict]]:
    """{last 10 digits: [{"lead_key", "origin", "name"}]} for the Spin leads with these phones.
    Raises WorkflowUnavailable when the workflow database can't be read."""
    wanted = {phones.last10(e) for e in e164s if e}
    found: dict[str, list[dict]] = {}
    if not wanted:
        return found
    rows = db.exec(select(CrmSpinLead.id, CrmSpinLead.phone_e164, CrmSpinLead.name, CrmSpinLead.origin)
                   .where(CrmSpinLead.is_deleted == False))  # noqa: E712
    for lead_id, e164, name, origin in rows:
        tail = phones.last10(e164 or "")
        if tail in wanted:
            found.setdefault(tail, []).append({"lead_key": f"{CRM_KEY_PREFIX}{lead_id}", "origin": origin,
                                               "name": name})
    for tail, hits in workflow_phones({e for e in e164s if e}, by_last10=True).items():
        if tail not in wanted:
            continue
        for hit in hits:
            if hit.in_spin:
                found.setdefault(tail, []).append({"lead_key": hit.lead_id, "origin": "workflow",
                                                   "name": (hit.values or {}).get("name")})
    return found


def plan_spin(db: Session, rows: list[tuple[dict, str | None]], counts: dict) -> None:
    """KOC -> Dhruva preview / save: result["spin"] and result["also_in"] on every row (read-only).
      {"action": "matched", "leads": [{lead_key, origin, name, vendors: [{id, name}]}]}
      {"action": "not_in_spin"}   no Spin lead with this phone (or no usable phone)
    counts: spin_matched, spin_not_in_spin."""
    found = spin_matches(db, {e for _, e in rows if e})
    keys = sorted({m["lead_key"] for hits in found.values() for m in hits})
    vendors = spin_vendors(db, keys) if keys else {}
    counts.update({"spin_matched": 0, "spin_not_in_spin": 0})
    for result, e164 in rows:
        hits = found.get(phones.last10(e164)) if e164 else None
        if not hits:
            result["spin"] = {"action": "not_in_spin"}
            result["also_in"] = []
            counts["spin_not_in_spin"] += 1
            continue
        leads = [{**m, "vendors": vendors.get(m["lead_key"], [])} for m in hits]
        result["spin"] = {"action": "matched", "leads": leads}
        result["also_in"] = [{"source": "spin", "count": len(leads),
                              "vendors": sorted({v["name"] for m in leads for v in m["vendors"] if v["name"]})}]
        counts["spin_matched"] += 1


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
    workflow: dict[str, list[WorkflowHit]] = {}
    matches = lookup(db, {e for e in phone_of if e}, source, workflow_out=workflow)
    known = None
    by_tail = workflow_phones({e for e in phone_of if e}, by_last10=True) if source == KOC else {}

    results: list[dict] = []
    earlier: dict[str, list[tuple[int, tuple]]] = {}     # phone -> [(row, signature)] of earlier rows
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
        if e164 and by_tail.get(phones.last10(e164)):
            # KOC -> Dhruva: empty fields from the workflow row of the phone (stored on save)
            if known is None:
                from app.services.branches import branches_by_id
                known = list(branches_by_id(db).values())
            plan = plan_koc_fill(c.values, by_tail[phones.last10(e164)], known)
            if plan and "skipped" in plan:
                result["fill_skipped"] = plan["skipped"]
            elif plan and (plan["fields"] or plan["received"]):
                c.values.update(plan["fields"])
                if plan["received"] is not None:
                    c.values["created_at"] = plan["received"]
                result["fill"] = {"workflow_id": plan["workflow_id"], "fields": json_values(plan["fields"]),
                                  "received": _iso(plan["received"])}
        if e164:
            # Existing lead first, then an earlier row of the same file (every field the same)
            duplicate, others = classify(source, e164, matches, values=c.values)
            mine = signature(source, c.values)
            if duplicate is None:
                first = next((row for row, theirs in earlier.get(e164, []) if lead_values.compatible(mine, theirs)),
                             None)
                if first is not None:
                    duplicate = {"reason": "in_file", "duplicate_of_row": first}
            earlier.setdefault(e164, []).append((row_number, mine))
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
            if source == "spin" and workflow.get(e164):
                # Spin: empty fields will be filled from the workflow row of this phone ("Spin game")
                if known is None:
                    from app.services.branches import branches_by_id
                    known = list(branches_by_id(db).values())
                plan = plan_fill(c.values, c.values.get("created_at"), workflow[e164], known)
                if plan and "skipped" in plan:
                    result["fill_skipped"] = plan["skipped"]
                elif plan and plan["fields"]:
                    result["fill"] = {"workflow_id": plan["workflow_id"], "fields": json_values(plan["fields"])}
        results.append(result)
    if source == KOC:                  # the same lead in Spin: shown, never created
        plan_spin(db, list(zip(results, phone_of)), counts)
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


def create(db: Session, source: str, c: Cleaned, origin: str, user_id: int | None, flush: bool = True):
    """Add one lead (not committed). The caller checked duplicates. flush=False: a bulk save
    flushes all its new leads at once (the lead has no id / key until then)."""
    now = _now()
    kw = to_model_kwargs(source, c.values, c.phone)
    if source in DETAIL_SOURCES:
        model = KocDhruvaLead if source == KOC else CrmSpinLead
        lead = model(origin=origin, phone=kw["phone"], phone_e164=kw["phone_e164"], source=DEFAULT_LEAD_SOURCE,
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
    if source in DETAIL_SOURCES and not lead.source:
        lead.source = DEFAULT_LEAD_SOURCE          # an empty Source column (KOC -> Dhruva upload)
    if source in ("whatsapp", "wedding") and lead.submitted_at is None:
        lead.submitted_at = now
    db.add(lead)
    if flush:
        db.flush()                              # gives spin leads their id (crm-<id>)
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
