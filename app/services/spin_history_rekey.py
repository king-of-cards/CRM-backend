"""Move CRM data from hidden workflow spin leads to their uploaded copies (run AFTER the history upload).

Workflow spin rows created before SPIN_LIVE_FROM are hidden from spin (see spin_leads.spin_filter),
so the history is uploaded again as CRM spin leads (crm_spin_leads, keys "crm-<id>"). Updates,
quotes, hot flags and activity saved earlier on a hidden row (source "spin", key = the workflow id)
would stay behind on the old key. This module matches each such row to its uploaded copy by phone
(last 10 digits) and moves the data to the new key:

  crm_lead_meta      moved; if the upload already created a row for the new key, it is merged
                     field by field: a field the upload filled is never overwritten (reported as
                     "kept"), an empty one is filled from the old row ("merged"); the old row is
                     then deleted. "Assigned to": vendor_id counts as filled when the upload set a
                     vendor OR said Unassigned (auto_assign = False).
  crm_hot_leads      moved; dropped when the new key is already hot (reported)
  crm_lead_activity  moved (history is append-only: no conflicts)
  crm_notifications  moved, so notification links open the new lead

Only hidden leads (created before the cut-off) are touched. A phone with no uploaded copy, or with
2 or more, is reported and skipped; so is an uploaded copy that two hidden leads point to.
`plan` only reads; `apply` changes the session (the caller commits or rolls back, in one
transaction) and returns the SQL that undoes it. CLI: scripts/rekey_spin_history.py.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlmodel import Session, select

from app.models.crm_spin_lead import CrmSpinLead
from app.models.hot_lead import CrmHotLead
from app.models.lead_activity import LeadActivity
from app.models.lead_meta import LeadMeta
from app.models.lead_origin import CRM_KEY_PREFIX
from app.models.notification import CrmNotification
from app.services import phones

SOURCE = "spin"
MERGE_FIELDS = ("vendor_id", "update_status", "quoted_date", "quoted_amount", "conversion_amount", "notes",
                "follow_up_date")
META_COLUMNS = ("id", "source", "lead_key", "vendor_id", "auto_assign", "update_status", "quoted_date",
                "follow_up_date", "quoted_amount", "conversion_amount", "interest", "converted", "converted_at",
                "notes", "updated_at", "updated_by")


@dataclass
class Move:
    old_key: str
    new_key: str
    phone_tail: str
    meta: str = "none"                     # none | move | merge
    merged: dict[str, Any] = field(default_factory=dict)      # field -> value taken from the old row
    kept: dict[str, tuple[Any, Any]] = field(default_factory=dict)   # field -> (upload value, old value)
    hot: str = "none"                      # none | move | drop (new key already hot)
    activity: int = 0
    notifications: int = 0


@dataclass
class Plan:
    moves: list[Move] = field(default_factory=list)
    no_match: list[tuple[str, str]] = field(default_factory=list)                 # (old key, phone tail)
    ambiguous: list[tuple[str, str, list[str]]] = field(default_factory=list)     # (old key, tail, new keys)
    shared_copy: list[tuple[str, list[str]]] = field(default_factory=list)        # (new key, old keys)
    no_phone: list[str] = field(default_factory=list)       # not found in the workflow, or no phone
    live: list[str] = field(default_factory=list)           # on/after the cut-off: still in spin, left alone


def _keys_with_data(db: Session) -> set[str]:
    """Workflow keys (digits) with spin CRM data in any of the four tables."""
    keys: set[str] = set()
    for model in (LeadMeta, CrmHotLead, LeadActivity, CrmNotification):
        keys |= {k for k in db.exec(select(model.lead_key).where(model.source == SOURCE)) if k and str(k).isdigit()}
    return keys


def _filled(meta: LeadMeta, name: str) -> bool:
    if name == "vendor_id":
        return meta.vendor_id is not None or meta.auto_assign is False
    return getattr(meta, name) not in (None, "")


def plan(db: Session, workflow: dict[str, tuple[str | None, datetime | None]], cutoff_utc: datetime) -> Plan:
    """workflow: {workflow id: (phone, created_at naive UTC)} for (at least) the keys with CRM data.
    cutoff_utc: the spin cut-off as naive UTC (spin_leads.spin_live_from_utc())."""
    out = Plan()
    copies: dict[str, list[str]] = {}
    for lead in db.exec(select(CrmSpinLead).where(CrmSpinLead.is_deleted == False)):  # noqa: E712
        tail = phones.last10(lead.phone_e164 or lead.phone or "")
        if len(tail) == 10:
            copies.setdefault(tail, []).append(lead.lead_key)

    candidates: list[tuple[str, str, str]] = []
    for key in sorted(_keys_with_data(db), key=int):
        phone, created = workflow.get(key, (None, None))
        if created is not None and created >= cutoff_utc:
            out.live.append(key)
            continue
        tail = phones.last10(phone or "")
        if created is None or len(tail) != 10:
            out.no_phone.append(key)
            continue
        found = copies.get(tail, [])
        if not found:
            out.no_match.append((key, tail))
        elif len(found) > 1:
            out.ambiguous.append((key, tail, sorted(found)))
        else:
            candidates.append((key, found[0], tail))

    by_copy: dict[str, list[str]] = {}
    for old, new, _ in candidates:
        by_copy.setdefault(new, []).append(old)
    out.shared_copy = sorted((new, olds) for new, olds in by_copy.items() if len(olds) > 1)
    shared = {new for new, _ in out.shared_copy}

    for old, new, tail in candidates:
        if new in shared:
            continue
        move = Move(old, new, tail)
        old_meta = _meta(db, old)
        new_meta = _meta(db, new)
        if old_meta is not None:
            move.meta = "merge" if new_meta is not None else "move"
            if new_meta is not None:
                for name in MERGE_FIELDS:
                    old_value = getattr(old_meta, name)
                    if old_value in (None, ""):
                        continue
                    if _filled(new_meta, name):
                        current = getattr(new_meta, name)
                        if current != old_value:
                            move.kept[name] = (current, old_value)
                    else:
                        move.merged[name] = old_value
        if _hot(db, old) is not None:
            move.hot = "drop" if _hot(db, new) is not None else "move"
        move.activity = len(_rows(db, LeadActivity, old))
        move.notifications = len(_rows(db, CrmNotification, old))
        out.moves.append(move)
    return out


def _meta(db: Session, key: str) -> LeadMeta | None:
    return db.exec(select(LeadMeta).where(LeadMeta.source == SOURCE, LeadMeta.lead_key == key)).first()


def _hot(db: Session, key: str) -> CrmHotLead | None:
    return db.exec(select(CrmHotLead).where(CrmHotLead.source == SOURCE, CrmHotLead.lead_key == key)).first()


def _rows(db: Session, model, key: str) -> list:
    return list(db.exec(select(model).where(model.source == SOURCE, model.lead_key == key)))


# ── Apply ──────────────────────────────────────────────────────────────
def sql_value(value: Any) -> str:
    """A MySQL literal for the undo SQL."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return "'" + value.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(value, date):
        return "'" + value.isoformat() + "'"
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


def _where_ids(ids: list[int]) -> str:
    return f"WHERE source = 'spin' AND id IN ({', '.join(str(i) for i in sorted(ids))})"


def apply(db: Session, the_plan: Plan) -> list[str]:
    """Move the planned rows (not committed). Returns the undo SQL statements, in order.
    Raises ValueError if the data changed since the plan (the caller rolls back)."""
    undo: list[str] = []
    for move in the_plan.moves:
        if not move.new_key.startswith(CRM_KEY_PREFIX) or move.old_key.startswith(CRM_KEY_PREFIX):
            raise ValueError(f"unexpected keys {move.old_key} -> {move.new_key}")
        old_meta, new_meta = _meta(db, move.old_key), _meta(db, move.new_key)
        has_old, has_new = old_meta is not None, new_meta is not None
        if ((move.meta == "none" and has_old) or (move.meta == "move" and (not has_old or has_new))
                or (move.meta == "merge" and not (has_old and has_new))):
            raise ValueError(f"crm_lead_meta changed for {move.old_key} / {move.new_key}; preview again")
        if move.meta == "move":
            old_meta.lead_key = move.new_key
            db.add(old_meta)
            undo.append(f"UPDATE crm_lead_meta SET lead_key = {sql_value(move.old_key)} {_where_ids([old_meta.id])};")
        elif move.meta == "merge":
            restore = {name: getattr(new_meta, name) for name in move.merged}
            if any(_filled(new_meta, name) for name in move.merged):
                raise ValueError(f"crm_lead_meta of {move.new_key} changed; preview again")
            values = {c: getattr(old_meta, c) for c in META_COLUMNS}
            for name, value in move.merged.items():
                setattr(new_meta, name, value)
            db.add(new_meta)
            db.delete(old_meta)
            columns = ", ".join(META_COLUMNS)
            undo.append(f"INSERT INTO crm_lead_meta ({columns}) VALUES "
                        f"({', '.join(sql_value(values[c]) for c in META_COLUMNS)});")
            if restore:
                sets = ", ".join(f"{name} = {sql_value(value)}" for name, value in restore.items())
                undo.append(f"UPDATE crm_lead_meta SET {sets} {_where_ids([new_meta.id])};")
        old_hot = _hot(db, move.old_key)
        if (old_hot is not None) != (move.hot != "none"):
            raise ValueError(f"crm_hot_leads changed for {move.old_key}; preview again")
        if move.hot == "move":
            if _hot(db, move.new_key) is not None:
                raise ValueError(f"{move.new_key} became hot; preview again")
            old_hot.lead_key = move.new_key
            db.add(old_hot)
            undo.append(f"UPDATE crm_hot_leads SET lead_key = {sql_value(move.old_key)} {_where_ids([old_hot.id])};")
        elif move.hot == "drop":
            undo.append("INSERT INTO crm_hot_leads (id, source, lead_key, hot_marked_by, hot_marked_at) VALUES "
                        f"({old_hot.id}, 'spin', {sql_value(move.old_key)}, {old_hot.hot_marked_by}, "
                        f"{sql_value(old_hot.hot_marked_at)});")
            db.delete(old_hot)
        for model, table in ((LeadActivity, "crm_lead_activity"), (CrmNotification, "crm_notifications")):
            rows = _rows(db, model, move.old_key)
            for row in rows:
                row.lead_key = move.new_key
                db.add(row)
            if rows:
                undo.append(f"UPDATE {table} SET lead_key = {sql_value(move.old_key)} "
                            f"{_where_ids([r.id for r in rows])};")
    db.flush()
    return undo


def report(the_plan: Plan) -> list[str]:
    """Human-readable preview lines (no names or phones beyond the last 4 digits)."""
    lines = [f"Matched: {len(the_plan.moves)} hidden spin lead(s) -> uploaded copies"]
    for m in the_plan.moves:
        parts = [f"meta {m.meta}"]
        if m.merged:
            parts.append("merge " + ", ".join(f"{k}={v}" for k, v in m.merged.items()))
        if m.kept:
            parts.append("keep upload " + ", ".join(f"{k}={new} (old {old})" for k, (new, old) in m.kept.items()))
        parts.append(f"hot {m.hot}")
        parts.append(f"activity {m.activity}")
        parts.append(f"notifications {m.notifications}")
        lines.append(f"  {m.old_key:>8} -> {m.new_key:<12} (phone …{m.phone_tail[-4:]}): " + "; ".join(parts))
    lines.append(f"No uploaded copy (skipped): {len(the_plan.no_match)}")
    lines += [f"  {k:>8} (phone …{t[-4:]})" for k, t in the_plan.no_match]
    lines.append(f"2+ uploaded copies with the phone (skipped): {len(the_plan.ambiguous)}")
    lines += [f"  {k:>8} (phone …{t[-4:]}): {', '.join(keys)}" for k, t, keys in the_plan.ambiguous]
    lines.append(f"Uploaded copy claimed by 2+ hidden leads (skipped): {len(the_plan.shared_copy)}")
    lines += [f"  {new}: {', '.join(olds)}" for new, olds in the_plan.shared_copy]
    lines.append(f"Not in the workflow database / no phone (skipped): {len(the_plan.no_phone)}"
                 + (f" ({', '.join(the_plan.no_phone)})" if the_plan.no_phone else ""))
    lines.append(f"Still live in spin (on/after the cut-off, left alone): {len(the_plan.live)}")
    return lines
