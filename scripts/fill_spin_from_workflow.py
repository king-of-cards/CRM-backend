"""Fill empty fields of uploaded Spin leads from the workflow ("Spin game"). Read-only preview by default.

For every uploaded Spin lead (crm_spin_leads, not archived), the workflow spin_lead rows with the same
phone (last 10 digits, any date) are candidates; rows whose filled name, event date, occasion or
branch conflict with the upload's are dropped, and of the rest the one created closest to the upload's
created date is used. Equally close rows: skipped. Only EMPTY fields of the upload are filled (name,
email, lead occasion, branch, pincode, occasion type, event date, budget, other wedding services,
quantity and every other Spin template field), mapped as the export maps them. Filled fields, the
created date and the CRM columns (assignment, update, amounts, notes, follow-up) are never changed.
The workflow database is only read. From the project root, with the venv:

    venv\\Scripts\\python scripts\\fill_spin_from_workflow.py                                      # preview
    venv\\Scripts\\python scripts\\fill_spin_from_workflow.py --apply --undo-file fill_undo.sql    # write

--apply plans again inside one MySQL transaction, fills the leads, adds a "filled_from_workflow"
activity entry to each, writes the undo SQL (--undo-file, required, must not exist) and commits; any
error rolls everything back. To undo later: run the undo file in MySQL.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def report(plan: list[dict]) -> None:
    filled = [r for r in plan if r.get("fields") and not r.get("skipped")]
    print(f"Uploaded Spin leads: {len(plan)}; would fill: {len(filled)}; skipped: {len(plan) - len(filled)}")
    for row in plan:
        head = f"{row['id']:>6}  {(row['name'] or '')[:28]:<28}  {row['phone']:<12}"
        if row.get("skipped"):
            where = f"workflow {row['workflow_id']}: " if row.get("workflow_id") else ""
            print(f"{head}  skipped: {where}{row['skipped']}")
        else:
            fields = ", ".join(f"{k}={v}" for k, v in sorted(row["fields"].items()))
            print(f"{head}  workflow {row['workflow_id']}: fill {fields}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the fills (one transaction); default: preview")
    parser.add_argument("--undo-file", type=Path, help="where to write the undo SQL (required with --apply)")
    args = parser.parse_args()
    if args.apply and args.undo_file is None:
        raise SystemExit("--apply needs --undo-file <path> (the SQL that restores the previous values).")
    if args.apply and args.undo_file.exists():
        raise SystemExit(f"{args.undo_file} already exists; pick a new file name.")

    from sqlmodel import Session

    from app.core.config import settings
    from app.db.session import engine, make_engine, read_engine
    from app.services.crm_leads import service

    if read_engine is None:
        raise SystemExit("The read-only workflow database isn't configured (READ_DB_* in .env).")
    bind = engine if args.apply else make_engine(settings.sqlalchemy_url, settings.DB_SSL_CA, read_only=True,
                                                 ssl_ca_setting="DB_SSL_CA")
    with Session(bind) as db:
        plan = service.plan_backfill(db)
        report(plan)
        if not args.apply:
            db.rollback()
            print("\nPreview only: nothing was changed. Add --apply --undo-file <path> to fill them.")
            return
        if not any(r.get("fields") and not r.get("skipped") for r in plan):
            print("\nNothing to fill.")
            return
        try:
            undo = service.apply_backfill(db, plan)
            args.undo_file.write_text("-- Undo of scripts/fill_spin_from_workflow.py: restores the previous values.\n"
                                      "START TRANSACTION;\n" + "\n".join(undo) + "\nCOMMIT;\n",
                                      encoding="utf-8", newline="\n")
            db.commit()
        except Exception:
            db.rollback()
            args.undo_file.unlink(missing_ok=True)           # nothing was applied: no undo file
            print("\nFailed: everything was rolled back.")
            raise
    print(f"\nApplied. Undo SQL: {args.undo_file}")


if __name__ == "__main__":
    main()
