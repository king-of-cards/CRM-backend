"""One-time fix: bring the existing crm_vendors table up to date (keeps its rows).

Your database already had a crm_vendors table with only id, name, is_active and created_at.
This script adds the missing columns, then fills in the area rules for RJ, Phodeo and OHP
(and adds any of the three that are missing). Safe to run more than once.

Run from the backend folder (venv active):
    python fix_vendors_table.py
"""
import json
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from app.db.session import engine
from app.services.lead_crm import DEFAULT_VENDORS

# column -> MySQL definition (JSON columns start empty and are filled below)
COLUMNS = {
    "coverage": "VARCHAR(300) NULL",
    "areas": "JSON NULL",
    "pincodes": "JSON NULL",
    "pincode_prefixes": "JSON NULL",
    "is_fallback": "BOOL NOT NULL DEFAULT 0",
    "priority": "INT NOT NULL DEFAULT 100",
    "contact_name": "VARCHAR(200) NULL",
    "phone": "VARCHAR(40) NULL",
    "email": "VARCHAR(254) NULL",
    "notes": "TEXT NULL",
    "updated_at": "DATETIME NULL",
}

now = datetime.now(timezone.utc).replace(tzinfo=None)

with engine.begin() as conn:
    existing = {c["name"] for c in inspect(conn).get_columns("crm_vendors")}
    print("Columns before:", sorted(existing))

    # 1. Add the missing columns
    for name, definition in COLUMNS.items():
        if name not in existing:
            conn.execute(text(f"ALTER TABLE crm_vendors ADD COLUMN `{name}` {definition}"))
            print(f"  + added {name}")

    # 2. No empty values in the list columns / dates
    for col in ("areas", "pincodes", "pincode_prefixes"):
        conn.execute(text(f"UPDATE crm_vendors SET `{col}` = '[]' WHERE `{col}` IS NULL"))
    conn.execute(text("UPDATE crm_vendors SET updated_at = COALESCE(updated_at, created_at, :now)"), {"now": now})
    conn.execute(text("UPDATE crm_vendors SET created_at = COALESCE(created_at, :now)"), {"now": now})

    # 3. Area rules for RJ, Phodeo and OHP (matched by name, any capitalisation)
    rows = {r.name.strip().lower(): r.id for r in conn.execute(text("SELECT id, name FROM crm_vendors"))}
    for v in DEFAULT_VENDORS:
        values = {
            "coverage": v["coverage"],
            "areas": json.dumps(v["areas"]),
            "pincodes": json.dumps(v["pincodes"]),
            "pincode_prefixes": json.dumps(v["pincode_prefixes"]),
            "is_fallback": v["is_fallback"],
            "priority": v["priority"],
            "now": now,
        }
        vid = rows.get(v["name"].lower())
        if vid:
            conn.execute(
                text(
                    "UPDATE crm_vendors SET name=:name, coverage=:coverage, areas=:areas, pincodes=:pincodes, "
                    "pincode_prefixes=:pincode_prefixes, is_fallback=:is_fallback, priority=:priority, "
                    "updated_at=:now WHERE id=:id"
                ),
                {**values, "name": v["name"], "id": vid},
            )
            print(f"  ~ rules set for {v['name']} (id {vid})")
        else:
            conn.execute(
                text(
                    "INSERT INTO crm_vendors (name, coverage, areas, pincodes, pincode_prefixes, is_fallback, "
                    "priority, is_active, created_at, updated_at) VALUES (:name, :coverage, :areas, :pincodes, "
                    ":pincode_prefixes, :is_fallback, :priority, 1, :now, :now)"
                ),
                {**values, "name": v["name"]},
            )
            print(f"  + added vendor {v['name']}")

    # 4. Show the result
    print("\nVendors now:")
    for r in conn.execute(text("SELECT id, name, priority, is_fallback, coverage FROM crm_vendors ORDER BY priority, id")):
        print(f"  {r.id:>3}  {r.name:<10} priority {r.priority:<4} fallback={bool(r.is_fallback)}  {r.coverage or ''}")

print("\nDone. Start the backend: python -m fastapi dev app\\main.py")