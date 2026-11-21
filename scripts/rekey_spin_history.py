"""Move CRM data from hidden workflow spin leads to their uploaded copies. Read-only preview by default.

Run AFTER the spin history upload (see app/services/spin_history_rekey.py for the rules). From the
project root, with the venv:

    python scripts/rekey_spin_history.py                       # preview only (SELECT only, nothing changes)
    python scripts/rekey_spin_history.py --apply --undo-file rekey_undo.sql

The preview lists every hidden spin lead with CRM data (crm_lead_meta, crm_hot_leads,
crm_lead_activity, crm_notifications), its uploaded copy (matched by the last 10 phone digits), what
would move and what would merge ("merge": an empty field of the uploaded copy filled from the old
row; "keep upload": the upload already filled it and wins), and the leads skipped (no copy, 2+
copies, a copy claimed twice, not in the workflow database).

--apply re-plans inside one MySQL transaction, checks nothing changed, moves the rows, writes the
undo SQL (--undo-file, required) and commits; any error or conflict rolls everything back.
To undo later: run the undo file's statements in MySQL (they restore the old keys and rows).
The workflow database is only ever read.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def workflow_rows(keys: set[str]) -> dict[str, tuple]:
    """{workflow id: (phone, created_at naive UTC)} for these ids (read-only engine: SELECT only)."""
    from sqlalchemy import select

    from app.api.v1.endpoints import spin_leads as sl
    from app.db.session import read_engine

    if read_engine is None:
        raise SystemExit("The read-only workflow database isn't configured (READ_DB_* in .env).")
    table = sl._get_table()
    pk = list(table.primary_key.columns)[0]
    created = sl._created_column(table)
    ids = sorted(int(k) for k in keys)
    out: dict[str, tuple] = {}
    with read_engine.connect() as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for lead_id, phone, at in conn.execute(select(pk, table.c.phone, created).where(pk.in_(chunk))):
                out[str(lead_id)] = (phone, sl.to_naive_utc(at))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="move the rows (one transaction); default: preview")
    parser.add_argument("--undo-file", type=Path, help="where to write the undo SQL (required with --apply)")
    args = parser.parse_args()
    if args.apply and args.undo_file is None:
        raise SystemExit("--apply needs --undo-file <path> (the SQL that restores the old keys).")
    if args.apply and args.undo_file.exists():
        raise SystemExit(f"{args.undo_file} already exists; pick a new file name.")

    from sqlmodel import Session

    from app.api.v1.endpoints import spin_leads as sl
    from app.core.config import settings
    from app.db.session import engine, make_engine
    from app.services import spin_history_rekey as rekey

    cutoff = sl.spin_live_from_utc()
    print(f"Spin cut-off: {settings.SPIN_LIVE_FROM} IST ({cutoff} UTC)")
    bind = engine if args.apply else make_engine(settings.sqlalchemy_url, settings.DB_SSL_CA, read_only=True,
                                                 ssl_ca_setting="DB_SSL_CA")
    with Session(bind) as db:
        keys = rekey._keys_with_data(db)
        the_plan = rekey.plan(db, workflow_rows(keys) if keys else {}, cutoff)
        print("\n".join(rekey.report(the_plan)))
        if not args.apply:
            db.rollback()
            print("\nPreview only: nothing was changed. Add --apply --undo-file <path> to move the rows.")
            return
        if not the_plan.moves:
            print("\nNothing to move.")
            return
        try:
            undo = rekey.apply(db, the_plan)
            args.undo_file.write_text(
                "-- Undo of scripts/rekey_spin_history.py: restores the old workflow keys and rows.\n"
                "START TRANSACTION;\n" + "\n".join(undo) + "\nCOMMIT;\n", encoding="utf-8", newline="\n")
            db.commit()
        except Exception:
            db.rollback()
            args.undo_file.unlink(missing_ok=True)       # nothing was applied: no undo file
            print("\nFailed: everything was rolled back.")
            raise
    print(f"\nApplied: {len(the_plan.moves)} lead(s) moved. Undo SQL: {args.undo_file}")


if __name__ == "__main__":
    main()
