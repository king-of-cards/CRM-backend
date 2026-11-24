"""Fill the empty fields of the uploaded KOC -> Dhruva rows from the workflow. Read-only preview by default.

From the project root, with the venv:

    venv\\Scripts\\python scripts\\backfill_koc_dhruva_fields.py
    venv\\Scripts\\python scripts\\backfill_koc_dhruva_fields.py --apply --undo-file koc_dhruva_backfill_undo.sql

Every empty field of a crm_koc_dhruva_leads row (Email, Occasion, Event date, Branch, Estimated
budget per card, Quantity, Pincode, Other wedding services, Relation, …) is filled from the workflow
row (any date) with the same last 10 phone digits and stored; filled values are never overwritten.
A Received that is only the upload time becomes the workflow row's created_at. Budgets Excel turned
into dates ("01 Oct 2026") are read back as the range ("1-10"). See app/services/koc_dhruva_backfill.py.

--apply plans again inside one MySQL transaction, fills, writes the undo SQL (--undo-file, required,
must not exist) and commits; any error rolls everything back. The workflow database is only read.
Restart the app afterwards so its caches start fresh.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def report(the_plan) -> None:
    print(f"Rows to change: {len(the_plan.items)}")
    print("Per field (rows that get it):")
    for name, n in sorted(the_plan.per_field.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"   {name}: {n}")
    print(f"No workflow row with the phone ({len(the_plan.no_match)}):")
    for r in the_plan.no_match:
        print(f"   {r['lead_key']}  phone {r['phone']}  {r['name'] or '-'}")
    if the_plan.skipped:
        print(f"Not filled, no single matching workflow row ({len(the_plan.skipped)}):")
        for r in the_plan.skipped:
            print(f"   {r['lead_key']}  phone {r['phone']}: {r['reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="fill (one transaction); default: preview")
    parser.add_argument("--undo-file", type=Path, help="where to write the undo SQL (required with --apply)")
    args = parser.parse_args()
    if args.apply and args.undo_file is None:
        raise SystemExit("--apply needs --undo-file <path> (the SQL that restores the previous values).")
    if args.apply and args.undo_file.exists():
        raise SystemExit(f"{args.undo_file} already exists; pick a new file name.")

    from sqlmodel import Session

    from app.core.config import settings
    from app.db.session import engine, make_engine, read_engine
    from app.services import koc_dhruva_backfill as backfill

    if read_engine is None:
        raise SystemExit("The read-only workflow database isn't configured (READ_DB_* in .env).")
    bind = engine if args.apply else make_engine(settings.sqlalchemy_url, settings.DB_SSL_CA, read_only=True,
                                                 ssl_ca_setting="DB_SSL_CA")
    with Session(bind) as db:
        the_plan = backfill.plan(db)
        report(the_plan)
        if not args.apply:
            db.rollback()
            print("\nPreview only: nothing was changed. Add --apply --undo-file <path> to fill.")
            return
        if not the_plan.items:
            db.rollback()
            print("\nNothing to fill.")
            return
        try:
            undo = backfill.apply(db, the_plan)
            args.undo_file.write_text("-- Undo of scripts/backfill_koc_dhruva_fields.py: restores the previous values.\n"
                                      "START TRANSACTION;\n" + "\n".join(undo) + "\nCOMMIT;\n",
                                      encoding="utf-8", newline="\n")
            db.commit()
        except Exception:
            db.rollback()
            args.undo_file.unlink(missing_ok=True)           # nothing was applied: no undo file
            print("\nFailed: everything was rolled back.")
            raise
    print(f"\nFilled {len(the_plan.items)} row(s). Undo SQL: {args.undo_file}. "
          "Restart the app so its caches start fresh.")


if __name__ == "__main__":
    main()
