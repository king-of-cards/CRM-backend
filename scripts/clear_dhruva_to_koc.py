"""Remove ALL Dhruva -> KOC leads and their activity, hot stars, notifications and CRM details. Preview by default.

Run AFTER migrations/2026-11-23_dhruva_to_koc_fields.sql. From the project root, with the venv:

    venv\\Scripts\\python scripts\\clear_dhruva_to_koc.py
    venv\\Scripts\\python scripts\\clear_dhruva_to_koc.py --apply --undo-file dhruva_to_koc_undo.sql

Removed: every crm_dhruva_leads row (archived too), their crm_dhruva_event_dates and crm_dhruva_phones, and the
crm_lead_activity, crm_hot_leads, crm_notifications (+ crm_notification_reads) and crm_lead_meta rows
with source 'dhruva'. Follow-up reminders come from the leads (no table). See
app/services/dhruva_clear.py. The preview shows the count grouped by who added the leads, with the
earliest and latest date added.

--apply works in one MySQL transaction, writes the undo SQL (--undo-file, required, must not exist:
INSERTs of every removed row, every column) and commits; any error rolls everything back. Restart
the app afterwards, then upload the leads again (GET /api/v1/dhruva/leads/template).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def report(the_plan) -> None:
    print(f"Dhruva -> KOC leads to remove: {the_plan.leads}")
    for g in the_plan.by_creator:
        archived = f", {g['archived']} archived" if g["archived"] else ""
        print(f"   added by {g['name']} (user {g['created_by']}): {g['leads']}{archived}; "
              f"earliest {g['earliest']}, latest {g['latest']} (UTC)")
    print("Related rows (source 'dhruva'):")
    for table, n in the_plan.related.items():
        print(f"   {table}: {n}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="remove (one transaction); default: preview")
    parser.add_argument("--undo-file", type=Path, help="where to write the undo SQL (required with --apply)")
    args = parser.parse_args()
    if args.apply and args.undo_file is None:
        raise SystemExit("--apply needs --undo-file <path> (the SQL that restores the removed rows).")
    if args.apply and args.undo_file.exists():
        raise SystemExit(f"{args.undo_file} already exists; pick a new file name.")

    from sqlmodel import Session

    from app.core.config import settings
    from app.db.session import engine, make_engine
    from app.services import dhruva_clear

    bind = engine if args.apply else make_engine(settings.sqlalchemy_url, settings.DB_SSL_CA, read_only=True,
                                                 ssl_ca_setting="DB_SSL_CA")
    with Session(bind) as db:
        the_plan = dhruva_clear.plan(db)
        report(the_plan)
        if not args.apply:
            db.rollback()
            print("\nPreview only: nothing was changed. Add --apply --undo-file <path> to remove.")
            return
        if not the_plan.leads and not any(the_plan.related.values()):
            db.rollback()
            print("\nNothing to remove.")
            return
        try:
            undo, removed = dhruva_clear.apply(db)
            args.undo_file.write_text("-- Undo of scripts/clear_dhruva_to_koc.py: restores the removed rows.\n"
                                      "START TRANSACTION;\n" + "\n".join(undo) + "\nCOMMIT;\n",
                                      encoding="utf-8", newline="\n")
            db.commit()
        except Exception:
            db.rollback()
            args.undo_file.unlink(missing_ok=True)           # nothing was applied: no undo file
            print("\nFailed: everything was rolled back.")
            raise
    print("\nRemoved: " + ", ".join(f"{t} {n}" for t, n in removed.items()))
    print(f"Undo SQL: {args.undo_file}. Restart the app so its caches start fresh.")


if __name__ == "__main__":
    main()
