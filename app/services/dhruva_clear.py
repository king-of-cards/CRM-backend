"""Remove every Dhruva -> KOC lead (scripts/clear_dhruva_to_koc.py), before uploading them again.

Removed: every crm_dhruva_leads row (archived too) and everything kept for them: their event dates
(crm_dhruva_event_dates), phone numbers (crm_dhruva_phones), activity (crm_lead_activity), hot stars (crm_hot_leads), notifications
(crm_notifications and their crm_notification_reads) and CRM details (crm_lead_meta), all with
source 'dhruva'. Follow-up reminders are read from the leads themselves (no table), so they go with
them. Nothing of any other section is touched.

The tables are read by reflection, so the undo SQL re-inserts every column as it was (also columns
the app no longer uses, such as event_date_text).
"""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import MetaData, Table, select
from sqlmodel import Session

from app.services.koc_spin_cleanup import sql_value

SOURCE = "dhruva"
LEADS = "crm_dhruva_leads"
# (table, how its rows are picked), in delete order; restored in the reverse order
CHILD_TABLES = (
    ("crm_notification_reads", "reads"),
    ("crm_notifications", "source"),
    ("crm_lead_activity", "source"),
    ("crm_hot_leads", "source"),
    ("crm_lead_meta", "source"),
    ("crm_dhruva_event_dates", "lead_id"),
    ("crm_dhruva_phones", "lead_id"),
)


@dataclass
class Plan:
    leads: int = 0
    by_creator: list[dict] = field(default_factory=list)   # {created_by, name, leads, archived, earliest, latest}
    related: dict[str, int] = field(default_factory=dict)  # table -> rows


def _tables(db: Session) -> dict[str, Table]:
    meta = MetaData()
    names = [LEADS, "crm_users"] + [t for t, _ in CHILD_TABLES]
    conn = db.connection()
    from sqlalchemy import inspect

    existing = set(inspect(conn).get_table_names())
    meta.reflect(bind=conn, only=[n for n in names if n in existing])
    return dict(meta.tables)


def _child_rows(db: Session, tables: dict[str, Table], name: str, how: str, lead_ids: list[int]) -> list:
    table = tables.get(name)
    if table is None:
        return []
    conn = db.connection()
    if how == "source":
        stmt = select(table).where(table.c.source == SOURCE)
    elif how == "lead_id":
        stmt = select(table).where(table.c.lead_id.in_(lead_ids)) if lead_ids else None
    else:                                                       # reads of the removed notifications
        notes = tables.get("crm_notifications")
        if notes is None:
            return []
        ids = [r.id for r in conn.execute(select(notes.c.id).where(notes.c.source == SOURCE))]
        stmt = select(table).where(table.c.notification_id.in_(ids)) if ids else None
    return list(conn.execute(stmt).mappings()) if stmt is not None else []


def plan(db: Session) -> Plan:
    tables = _tables(db)
    leads_t = tables[LEADS]
    conn = db.connection()
    rows = list(conn.execute(select(leads_t)).mappings())
    users = tables.get("crm_users")
    names = {r.id: r.name for r in conn.execute(select(users.c.id, users.c.name))} if users is not None else {}
    groups: dict[Any, list] = defaultdict(list)
    for r in rows:
        groups[r["created_by"]].append(r)
    out = Plan(leads=len(rows))
    for created_by, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        dates = [r["created_at"] for r in items if r["created_at"] is not None]
        out.by_creator.append({"created_by": created_by, "name": names.get(created_by, "Deleted user"),
                               "leads": len(items), "archived": sum(1 for r in items if r["is_deleted"]),
                               "earliest": min(dates) if dates else None, "latest": max(dates) if dates else None})
    lead_ids = [r["id"] for r in rows]
    for name, how in CHILD_TABLES:
        out.related[name] = len(_child_rows(db, tables, name, how, lead_ids))
    return out


def _insert(table: Table, row) -> str:
    columns = list(row.keys())
    values = ", ".join(sql_value(row[c]) for c in columns)
    return f"INSERT INTO {table.name} ({', '.join(columns)}) VALUES ({values});"


def apply(db: Session) -> tuple[list[str], dict[str, int]]:
    """Remove everything (not committed). Returns (undo SQL: INSERTs, parents first; rows removed per table)."""
    tables = _tables(db)
    leads_t = tables[LEADS]
    conn = db.connection()
    leads = list(conn.execute(select(leads_t)).mappings())
    lead_ids = [r["id"] for r in leads]
    children = {name: _child_rows(db, tables, name, how, lead_ids) for name, how in CHILD_TABLES}
    undo = [_insert(leads_t, r) for r in leads]
    for name, _ in reversed(CHILD_TABLES):
        undo += [_insert(tables[name], r) for r in children[name]]
    removed = {}
    for name, _ in CHILD_TABLES:
        table = tables.get(name)
        ids = [r["id"] for r in children[name]]
        if table is not None and ids:
            for i in range(0, len(ids), 500):
                conn.execute(table.delete().where(table.c.id.in_(ids[i:i + 500])))
        removed[name] = len(ids)
    for i in range(0, len(lead_ids), 500):
        conn.execute(leads_t.delete().where(leads_t.c.id.in_(lead_ids[i:i + 500])))
    removed[LEADS] = len(lead_ids)
    return undo, removed
