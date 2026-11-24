"""Read-only: list uploaded Spin leads (crm_spin_leads) that are the same lead as a live workflow row.

The duplicate rule (app/services/crm_leads/service.py): the same phone and no conflicting filled
field (a field empty on either side is ignored), against workflow spin_lead rows created on or after
SPIN_LIVE_FROM. Archived uploads are left out. Nothing is changed in either database (both are
opened read-only, SELECT only). From the project root, with the venv:

    venv\Scripts\python scripts\find_spin_duplicates.py

Prints one line per uploaded lead (id, key, name, phone, created date in India time) with the
matching workflow lead ids, so the uploaded copies can be archived or removed.
"""
import sys
from datetime import timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    from sqlmodel import Session

    from app.core.config import settings
    from app.db.session import make_engine, read_engine
    from app.services.crm_leads import service

    if read_engine is None:
        raise SystemExit("The read-only workflow database isn't configured (READ_DB_* in .env).")
    crm = make_engine(settings.sqlalchemy_url, settings.DB_SSL_CA, read_only=True, ssl_ca_setting="DB_SSL_CA")
    ist = timezone(timedelta(hours=5, minutes=30))
    with Session(crm) as db:
        found = service.uploaded_spin_duplicates(db)
        db.rollback()
    print(f"Spin cut-off: {settings.SPIN_LIVE_FROM} (IST)")
    print(f"Uploaded Spin leads that duplicate a live workflow lead: {len(found)}")
    if found:
        print(f"{'id':>6}  {'key':<10}  {'name':<30}  {'phone':<14}  {'created (IST)':<17}  workflow ids")
    for row in found:
        created = row["created_at"]
        created = (created if created.tzinfo else created.replace(tzinfo=timezone.utc)).astimezone(ist)
        print(f"{row['id']:>6}  {row['lead_key']:<10}  {(row['name'] or '')[:30]:<30}  {row['phone']:<14}  "
              f"{created:%d %b %Y %H:%M}  {', '.join(row['workflow_ids'])}")
    print("\nRead-only: nothing was changed.")


if __name__ == "__main__":
    main()
