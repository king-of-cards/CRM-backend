"""One-time move to several vendors per lead (scripts/migrate_lead_vendors.py).

1. CRM copies: every crm_lead_meta row of the KOC -> Vendors sources is still the vendor-less row
   (copy_vendor_id 0). Each becomes the copy of the lead's current vendor: its manual vendor (the
   legacy crm_lead_meta.vendor_id, which also becomes a crm_lead_assignments "added" row), else its
   main automatic vendor (sheet / branch / area). A lead without a vendor, or "Unassigned", keeps it
   as the vendor-less row. Hot flags (crm_hot_leads, vendor_id 0) follow the same way.
2. Kraftyz from the sheet (kraftyz_leads.xlsx): the gift rule only adds Kraftyz to leads received on
   or after WEDDING_GIFTS_FROM. Older leads get Kraftyz only when they are in the sheet: matched to
   Spin leads (live workflow and uploaded) by phone (last 10 digits), Kraftyz is added as an extra
   vendor (the current ones keep the lead) and its copy is filled from the sheet: Update (mapped,
   UPDATE_MAP) and Additional comments (notes). The "Testing" 9999999990 row is skipped.

plan_*() only read; apply() changes the session (the caller commits or rolls back, in one
transaction) and returns the undo SQL (snapshot of every touched row before and after).
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlmodel import Session, select

from app.models.hot_lead import CrmHotLead
from app.models.lead_assignment import LeadAssignment
from app.models.lead_meta import LeadMeta
from app.services import lead_crm, phones

SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
TEST_PHONES = {"9999999990"}
# The sheet's "Updates" -> the Update of Kraftyz's copy (any case and spacing; apostrophes ignored)
UPDATE_MAP = {
    "already booked someone else": "purchased_elsewhere",
    "not interested": "not_interested",
    "will get back": "will_call_back",
    "didnt pickup call": "will_call_back",
    "non reachable": "will_call_back",
    "catalogue to be shared": "interested",
    "catalogue shared": "interested",
}


def norm_update(text: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"['’`]", "", str(text or ""))).strip().casefold()


def map_update(text: Any) -> tuple[str | None, bool]:
    """(Update, known): empty -> (None, True) = pending; an unknown text -> (None, False)."""
    key = norm_update(text)
    if not key:
        return None, True
    return (UPDATE_MAP[key], True) if key in UPDATE_MAP else (None, False)


# ── 1. Copies ──────────────────────────────────────────────────────────
def _entries(db: Session, source: str) -> dict[str, list[tuple[int, str]]]:
    from app.services import access

    sets = lead_crm.vendor_sets(db, source, access._rows(db, source))
    return {key: [(v.id, how) for v, how in vendors] for key, vendors in sets.items()}


def _target(entries: list[tuple[int, str]]) -> int | None:
    """The vendor a lead's old single copy belongs to: its first main (non-partner) vendor."""
    return next((vid for vid, how in entries if how != "partner"), None)


@dataclass
class CopyPlan:
    moves: list[dict] = field(default_factory=list)       # {table, id, source, lead_key, to, legacy}
    kept: list[dict] = field(default_factory=list)        # {table, id, source, lead_key, reason}
    conflicts: list[dict] = field(default_factory=list)   # the vendor already has a copy


def plan_copies(db: Session) -> CopyPlan:
    out = CopyPlan()
    for source in SOURCES:
        entries = _entries(db, source)
        existing = {(m.lead_key, m.copy_vendor_id) for m in db.exec(select(LeadMeta).where(LeadMeta.source == source))}
        for meta in db.exec(select(LeadMeta).where(LeadMeta.source == source, LeadMeta.copy_vendor_id == 0)):
            row = {"table": "crm_lead_meta", "id": meta.id, "source": source, "lead_key": meta.lead_key}
            if meta.auto_assign is False and meta.vendor_id is None:
                out.kept.append({**row, "reason": "Unassigned"})
                continue
            to = meta.vendor_id or _target(entries.get(meta.lead_key, []))
            if to is None:
                out.kept.append({**row, "reason": "no vendor"})
            elif (meta.lead_key, to) in existing:
                out.conflicts.append({**row, "to": to})
            else:
                out.moves.append({**row, "to": to, "legacy": meta.vendor_id is not None})
        hot_existing = {(h.lead_key, h.vendor_id) for h in db.exec(select(CrmHotLead).where(CrmHotLead.source == source))}
        legacy = {m.lead_key: m.vendor_id for m in db.exec(select(LeadMeta).where(
            LeadMeta.source == source, LeadMeta.copy_vendor_id == 0, LeadMeta.vendor_id.is_not(None)))}
        for hot in db.exec(select(CrmHotLead).where(CrmHotLead.source == source, CrmHotLead.vendor_id == 0)):
            row = {"table": "crm_hot_leads", "id": hot.id, "source": source, "lead_key": hot.lead_key}
            to = legacy.get(hot.lead_key) or _target(entries.get(hot.lead_key, []))
            if to is None:
                out.kept.append({**row, "reason": "no vendor"})
            elif (hot.lead_key, to) in hot_existing:
                out.conflicts.append({**row, "to": to})
            else:
                out.moves.append({**row, "to": to, "legacy": False})
    return out


# ── 2. Kraftyz from the sheet ──────────────────────────────────────────
@dataclass
class SheetRow:
    row: int
    name: str | None
    phone: str
    update_text: str | None
    notes: str | None


def read_sheet(path_or_bytes) -> list[SheetRow]:
    """The sheet's rows (columns: Relation, Timestamp, Name, Phone, Branch, occasion_date, occasion,
    Updates, Additional comments; header names any case and spacing)."""
    import io

    from openpyxl import load_workbook

    source = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    ws = load_workbook(source, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = [re.sub(r"[^a-z0-9]", "", str(h or "").lower()) for h in rows[0]]
    col = {name: head.index(name) for name in ("name", "phone", "updates", "additionalcomments") if name in head}
    if "phone" not in col:
        raise ValueError("The sheet has no Phone column")
    out = []
    for number, values in enumerate(rows[1:], start=2):
        def get(name):
            i = col.get(name)
            value = values[i] if i is not None and i < len(values) else None
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            return None if value is None else str(value).strip() or None
        phone = get("phone")
        if not phone:
            continue
        out.append(SheetRow(number, get("name"), phone, get("updates"), get("additionalcomments")))
    return out


@dataclass
class KraftyzPlan:
    vendor_id: int | None = None
    adds: list[dict] = field(default_factory=list)        # {key, row, current: [names], update_status, notes}
    already: list[dict] = field(default_factory=list)     # Kraftyz already has the lead
    unmatched: list[SheetRow] = field(default_factory=list)
    skipped: list[SheetRow] = field(default_factory=list)  # test rows
    unknown_updates: dict[str, int] = field(default_factory=dict)
    several: list[tuple[str, list[str]]] = field(default_factory=list)   # a phone matching 2+ leads


def spin_phones(db: Session) -> dict[str, list[str]]:
    """{last 10 digits: [lead keys]} of the Spin leads: live workflow rows and uploaded ones."""
    from app.models.crm_spin_lead import CrmSpinLead
    from app.services import also_in

    out: dict[str, list[str]] = {}
    for key, e164 in also_in.workflow_phone_index("spin").items():
        out.setdefault(phones.last10(e164), []).append(key)
    for lead in db.exec(select(CrmSpinLead).where(CrmSpinLead.is_deleted == False)):  # noqa: E712
        if lead.phone_e164:
            out.setdefault(phones.last10(lead.phone_e164), []).append(lead.lead_key)
    return out


def plan_kraftyz(db: Session, rows: list[SheetRow]) -> KraftyzPlan:
    out = KraftyzPlan()
    vendor = lead_crm.vendor_by_name(lead_crm.active_vendors(db), lead_crm.gift_vendor_name())
    if vendor is None:
        raise ValueError(f"No active vendor named {lead_crm.gift_vendor_name()!r}")
    out.vendor_id = vendor.id
    by_phone = spin_phones(db)
    entries = _entries(db, "spin")
    names = lead_crm.vendors_by_id(db)
    seen: set[str] = set()
    for row in rows:
        tail = phones.last10(row.phone)
        if tail in TEST_PHONES or (row.name or "").strip().casefold() == "testing":
            out.skipped.append(row)
            continue
        keys = [k for k in by_phone.get(tail, []) if k in entries]
        if not keys:
            out.unmatched.append(row)
            continue
        if len(keys) > 1:
            out.several.append((tail, keys))
        status, known = map_update(row.update_text)
        notes = row.notes
        if not known:
            out.unknown_updates[row.update_text] = out.unknown_updates.get(row.update_text, 0) + 1
            notes = "\n".join(p for p in (notes, f"Original update: {row.update_text}") if p)
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            current = entries.get(key, [])
            item = {"key": key, "row": row.row, "phone": tail,
                    "current": [names[vid].name for vid, how in current if vid in names and how != "partner"],
                    "update_status": status, "notes": notes}
            if vendor.id in [vid for vid, _ in current]:
                out.already.append(item)
            else:
                item["entries"] = current
                out.adds.append(item)
    return out


# ── Apply (one transaction) with undo SQL ─────────────────────────────
TABLES = {
    "crm_lead_assignments": (LeadAssignment, ("id", "source", "lead_key", "vendor_id", "state", "updated_by", "updated_at")),
    "crm_lead_meta": (LeadMeta, ("id", "source", "lead_key", "copy_vendor_id", "vendor_id", "auto_assign", "update_status",
                                 "quoted_date", "follow_up_date", "quoted_amount", "conversion_amount", "interest",
                                 "converted", "converted_at", "notes", "updated_at", "updated_by")),
    "crm_hot_leads": (CrmHotLead, ("id", "source", "lead_key", "vendor_id", "hot_marked_by", "hot_marked_at")),
}


def _snapshot(db: Session, leads: set[tuple[str, str]]) -> dict[str, dict[int, dict]]:
    out: dict[str, dict[int, dict]] = {}
    for table, (model, columns) in TABLES.items():
        rows = {}
        for source in {s for s, _ in leads}:
            keys = sorted(k for s, k in leads if s == source)
            for i in range(0, len(keys), 500):
                for row in db.exec(select(model).where(model.source == source, model.lead_key.in_(keys[i:i + 500]))):
                    rows[row.id] = {c: getattr(row, c) for c in columns}
        out[table] = rows
    return out


def _value(value: Any) -> str:
    from app.services.spin_history_rekey import sql_value

    if isinstance(value, (datetime, date)) or value is None or isinstance(value, (bool, int, float, str)):
        return sql_value(value)
    return sql_value(str(value))


def _undo(before: dict, after: dict) -> list[str]:
    sql = []
    for table, (_, columns) in TABLES.items():
        old, new = before[table], after[table]
        added = sorted(set(new) - set(old))
        if added:
            sql.append(f"DELETE FROM {table} WHERE id IN ({', '.join(map(str, added))});")
        for row_id in sorted(set(old) & set(new)):
            if old[row_id] != new[row_id]:
                sets = ", ".join(f"{c} = {_value(old[row_id][c])}" for c in columns if c != "id")
                sql.append(f"UPDATE {table} SET {sets} WHERE id = {row_id};")
        for row_id in sorted(set(old) - set(new)):
            values = ", ".join(_value(old[row_id][c]) for c in columns)
            sql.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values});")
    return sql


def apply(db: Session, copies: CopyPlan, kraftyz: KraftyzPlan | None) -> list[str]:
    """Apply both plans (not committed) and return the undo SQL. Re-run plan_* inside the same
    transaction first, so the plans match the data."""
    leads = {(m["source"], m["lead_key"]) for m in copies.moves}
    if kraftyz is not None:
        leads |= {("spin", a["key"]) for a in kraftyz.adds}
    before = _snapshot(db, leads)
    now = lead_crm._now()
    for move in copies.moves:
        if move["table"] == "crm_lead_meta":
            meta = db.get(LeadMeta, move["id"])
            if move["legacy"]:
                db.add(LeadAssignment(source=move["source"], lead_key=move["lead_key"], vendor_id=move["to"],
                                      state="added", updated_by=meta.updated_by, updated_at=now))
                meta.vendor_id = None
            meta.copy_vendor_id = move["to"]
            db.add(meta)
        else:
            hot = db.get(CrmHotLead, move["id"])
            hot.vendor_id = move["to"]
            db.add(hot)
    db.flush()
    if kraftyz is not None:
        for item in kraftyz.adds:
            lead_crm.add_vendor(db, "spin", item["key"], kraftyz.vendor_id, item["entries"], None)
            copy = lead_crm.copy_row(db, "spin", item["key"], kraftyz.vendor_id, create=True)
            if copy.update_status is None:
                copy.update_status = item["update_status"]
            if not copy.notes:
                copy.notes = item["notes"]
            copy.updated_at = now
            db.add(copy)
            db.flush()
    after = _snapshot(db, leads)
    return _undo(before, after)
