"""Remove the Spin leads that a KOC -> Dhruva upload created. Read-only preview by default.

From the project root, with the venv:

    venv\\Scripts\\python scripts\\remove_koc_created_spin_leads.py
    venv\\Scripts\\python scripts\\remove_koc_created_spin_leads.py --apply --undo-file koc_spin_undo.sql

Which leads: the Spin leads (crm_spin_leads, crm-<id>) carrying the activity entry the upload wrote
when it created them: source 'spin', action 'created', new_value 'from KOC → Dhruva kd-<id>'.
Each is removed with its vendor assignments, per-vendor copies (crm_lead_meta), hot stars, activity
and notifications. The KOC -> Dhruva rows are kept. Leads someone already worked on (Update not
pending, notes, quote, conversion, follow-up, hot, or any other activity entry) are SKIPPED and
listed. Imported Spin leads without the entry that look like a KOC -> Dhruva copy are only listed.

--apply plans again inside one MySQL transaction, removes, writes the undo SQL (--undo-file,
required, must not exist: INSERTs of every removed row) and commits; any error rolls everything
back. Restart the app afterwards so its caches start fresh. The workflow database is only read
(to show each lead's vendors).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def report(the_plan) -> None:
    remove = the_plan.remove
    print(f"Spin leads to remove: {len(remove)}")
    if remove:
        ids = [r["id"] for r in remove]
        print(f"   crm-{min(ids)} … crm-{max(ids)}")
    for r in remove:
        archived = " (archived)" if r["archived"] else ""
        print(f"   {r['lead_key']}{archived}  phone {r['phone']}  {r['name'] or '-'}  "
              f"vendors: {', '.join(r['vendors']) or '(none)'}  from {r['koc_key']}")
    print(f"Skipped, already worked on: {len(the_plan.skipped)}")
    for r in the_plan.skipped:
        print(f"   {r['lead_key']}  phone {r['phone']}  vendors: {', '.join(r['vendors']) or '(none)'}  "
              f"from {r['koc_key']}: {'; '.join(r['reasons'])}")
    if the_plan.missing:
        print(f"Already gone (logged, no Spin lead): {', '.join(the_plan.missing)}")
    if the_plan.unlinked:
        print(f"Look like KOC -> Dhruva copies but have no upload entry (NOT removed): {len(the_plan.unlinked)}")
        for r in the_plan.unlinked:
            print(f"   {r['lead_key']}  phone {r['phone']}  {r['name'] or '-'}  like {r['koc_key']}")


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
    from app.services import koc_spin_cleanup as cleanup

    bind = engine if args.apply else make_engine(settings.sqlalchemy_url, settings.DB_SSL_CA, read_only=True,
                                                 ssl_ca_setting="DB_SSL_CA")
    with Session(bind) as db:
        the_plan = cleanup.plan(db)
        report(the_plan)
        if not args.apply:
            db.rollback()
            print("\nPreview only: nothing was changed. Add --apply --undo-file <path> to remove.")
            return
        if not the_plan.remove:
            db.rollback()
            print("\nNothing to remove.")
            return
        try:
            undo = cleanup.apply(db, the_plan)
            args.undo_file.write_text("-- Undo of scripts/remove_koc_created_spin_leads.py: restores the removed rows.\n"
                                      "START TRANSACTION;\n" + "\n".join(undo) + "\nCOMMIT;\n",
                                      encoding="utf-8", newline="\n")
            db.commit()
        except Exception:
            db.rollback()
            args.undo_file.unlink(missing_ok=True)           # nothing was applied: no undo file
            print("\nFailed: everything was rolled back.")
            raise
    print(f"\nRemoved {len(the_plan.remove)} Spin lead(s). Undo SQL: {args.undo_file}. "
          "Restart the app so its caches start fresh.")


if __name__ == "__main__":
    main()
