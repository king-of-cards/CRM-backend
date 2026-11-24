"""Move to several vendors per lead, and add Kraftyz from its sheet. Read-only preview by default.

Run AFTER migrations/2026-11-23_lead_vendors.sql (the new columns and table). From the project root,
with the venv:

    venv\\Scripts\\python scripts\\migrate_lead_vendors.py --sheet kraftyz_leads.xlsx
    venv\\Scripts\\python scripts\\migrate_lead_vendors.py --sheet kraftyz_leads.xlsx --apply --undo-file lead_vendors_undo.sql

1. Every CRM details row (crm_lead_meta) and hot flag of the KOC -> Vendors sources becomes the copy of
   the lead's current vendor (its manual vendor, else its main automatic vendor); leads without a
   vendor or "Unassigned" keep a vendor-less row.
2. Kraftyz is added (an extra vendor; the current ones keep the lead) to the Spin leads whose phone is
   in the sheet, with its copy filled from the sheet (Update mapped; Additional comments = notes).
   The "Testing" 9999999990 row is skipped. Without --sheet, only step 1 runs.

The preview prints: copies moved per vendor, rows kept (and why), conflicts; Kraftyz: matched leads,
per current vendor, already Kraftyz, unmatched phones, phones matching several leads, unknown Update
texts (left pending, copied into the notes as "Original update: …").
--apply plans again inside one MySQL transaction, applies both steps, writes the undo SQL
(--undo-file, required, must not exist) and commits; any error rolls everything back. The workflow
database is only read. Never commit the sheet (it is in .gitignore).
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def report(db, copies, kraftyz) -> None:
    from app.services import lead_crm

    names = {v.id: v.name for v in lead_crm.vendors_by_id(db).values()}
    moved = Counter((m["table"], names.get(m["to"], m["to"])) for m in copies.moves)
    print("1. CRM copies")
    for (table, vendor), n in sorted(moved.items(), key=lambda kv: str(kv[0])):
        print(f"   {table}: {n} row(s) become {vendor}'s copy")
    kept = Counter((k["table"], k["reason"]) for k in copies.kept)
    for (table, reason), n in sorted(kept.items()):
        print(f"   {table}: {n} row(s) stay the vendor-less row ({reason})")
    for c in copies.conflicts:
        print(f"   conflict: {c['table']} id {c['id']} ({c['source']} {c['lead_key']}): "
              f"{names.get(c['to'], c['to'])} already has a copy; left as it is")
    if kraftyz is None:
        print("2. Kraftyz: no --sheet given, skipped")
        return
    print("2. Kraftyz from the sheet")
    print(f"   leads that gain Kraftyz: {len(kraftyz.adds)}; already Kraftyz's: {len(kraftyz.already)}")
    per_vendor = Counter(", ".join(a["current"]) or "(no vendor)" for a in kraftyz.adds)
    for vendor, n in per_vendor.most_common():
        print(f"     next to {vendor}: {n}")
    statuses = Counter(a["update_status"] or "pending" for a in kraftyz.adds)
    print("   Kraftyz's Update:", dict(statuses))
    print(f"   unmatched phones ({len(kraftyz.unmatched)}):",
          ", ".join(f"row {r.row} …{r.phone[-4:]}" for r in kraftyz.unmatched) or "-")
    print(f"   skipped test rows: {len(kraftyz.skipped)}")
    for tail, keys in kraftyz.several:
        print(f"   phone …{tail[-4:]} matches {len(keys)} leads: {', '.join(keys)} (all get Kraftyz)")
    for text, n in kraftyz.unknown_updates.items():
        print(f"   unknown Update {text!r} ({n} row(s)): left pending, kept in the notes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sheet", type=Path, help="kraftyz_leads.xlsx (Kraftyz's existing leads)")
    parser.add_argument("--apply", action="store_true", help="apply (one transaction); default: preview")
    parser.add_argument("--undo-file", type=Path, help="where to write the undo SQL (required with --apply)")
    args = parser.parse_args()
    if args.apply and args.undo_file is None:
        raise SystemExit("--apply needs --undo-file <path> (the SQL that restores the previous rows).")
    if args.apply and args.undo_file.exists():
        raise SystemExit(f"{args.undo_file} already exists; pick a new file name.")
    if args.sheet is not None and not args.sheet.exists():
        raise SystemExit(f"{args.sheet} not found.")

    from sqlmodel import Session

    from app.core.config import settings
    from app.db.session import engine, make_engine, read_engine
    from app.services import lead_vendor_migration as migration

    if read_engine is None:
        raise SystemExit("The read-only workflow database isn't configured (READ_DB_* in .env).")
    rows = migration.read_sheet(args.sheet) if args.sheet else None
    bind = engine if args.apply else make_engine(settings.sqlalchemy_url, settings.DB_SSL_CA, read_only=True,
                                                 ssl_ca_setting="DB_SSL_CA")
    with Session(bind) as db:
        copies = migration.plan_copies(db)
        kraftyz = migration.plan_kraftyz(db, rows) if rows is not None else None
        report(db, copies, kraftyz)
        if not args.apply:
            db.rollback()
            print("\nPreview only: nothing was changed. Add --apply --undo-file <path> to apply.")
            return
        try:
            undo = migration.apply(db, copies, kraftyz)
            args.undo_file.write_text("-- Undo of scripts/migrate_lead_vendors.py: restores the previous rows.\n"
                                      "START TRANSACTION;\n" + "\n".join(undo) + "\nCOMMIT;\n",
                                      encoding="utf-8", newline="\n")
            db.commit()
        except Exception:
            db.rollback()
            args.undo_file.unlink(missing_ok=True)           # nothing was applied: no undo file
            print("\nFailed: everything was rolled back.")
            raise
    print(f"\nApplied. Undo SQL: {args.undo_file}. Restart the app so its caches start fresh.")


if __name__ == "__main__":
    main()
