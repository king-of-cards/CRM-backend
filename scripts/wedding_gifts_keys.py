"""Read-only: list the workflow lead keys of the wedding_gifts flow for the migration.

The keys live in the workflow PostgreSQL database (spin_lead), which MySQL can't query, so the
migration carries them as literal values. This script reads them with the app's read-only engine
(SELECT only) using the same rule as the app (spin_leads.base_filter(table, "wedding_gifts")) and
writes them into the block between "-- BEGIN WORKFLOW KEYS" and "-- END WORKFLOW KEYS".

    python scripts/wedding_gifts_keys.py                  # print the INSERT statement
    python scripts/wedding_gifts_keys.py --update migrations/2026-11-20_wedding_gifts_source.sql

Run it from the project root with the venv, right before applying the migration, so gift leads
that arrived since the file was written are included. CRM-added gift leads (crm-<id>) are found by
the migration itself (crm_spin_leads is in MySQL).
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BEGIN, END = "-- BEGIN WORKFLOW KEYS", "-- END WORKFLOW KEYS"


def gift_keys() -> list[str]:
    from sqlalchemy import select

    from app.api.v1.endpoints import spin_leads as sl
    from app.db.session import read_engine

    if read_engine is None:
        raise SystemExit("The read-only workflow database isn't configured (READ_DB_* in .env).")
    table = sl._get_table()
    pk = list(table.primary_key.columns)[0]
    with read_engine.connect() as conn:          # read-only engine: SELECT only
        ids = conn.execute(select(pk).where(*sl.base_filter(table, sl.WEDDING_GIFTS)).order_by(pk)).scalars()
        return [str(i) for i in ids]


def insert_sql(keys: list[str]) -> str:
    if not keys:
        return f"{BEGIN} (0 keys)\n{END}"
    values = ",\n".join(", ".join(f"('{k}')" for k in keys[i:i + 10]) for i in range(0, len(keys), 10))
    return f"{BEGIN} ({len(keys)} keys)\nINSERT INTO tmp_wedding_gift_keys (lead_key) VALUES\n{values};\n{END}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", type=Path, help="migration file whose key block is replaced")
    args = parser.parse_args()
    keys = gift_keys()
    if not all(k.isdigit() for k in keys):
        raise SystemExit("Unexpected non-numeric workflow id; nothing written.")
    block = insert_sql(keys)
    if args.update is None:
        print(block)
        return
    text = args.update.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    if len(pattern.findall(text)) != 1:
        raise SystemExit(f"{args.update}: expected exactly one {BEGIN} ... {END} block; nothing written.")
    args.update.write_text(pattern.sub(lambda _: block, text), encoding="utf-8", newline="\n")
    print(f"{args.update}: {len(keys)} workflow keys written.")


if __name__ == "__main__":
    main()
