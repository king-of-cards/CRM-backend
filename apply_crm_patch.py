"""Adds the CRM details (vendor, interest, quote, converted) to the four lead endpoints.

Edits these files in place (a .bak copy of each is kept first):
  app/api/v1/endpoints/spin_leads.py
  app/api/v1/endpoints/whatsapp_leads.py
  app/api/v1/endpoints/wedding_leads.py
  app/api/v1/endpoints/wedding_plans.py

Run from the backend folder (venv active):
    python apply_crm_patch.py
Safe to run again: files that already have the change are skipped.
"""
import shutil
from pathlib import Path

BASE = Path("app/api/v1/endpoints")
LIST_RETURN = '    return {"items": [_out(r) for r in rows], "total": total, "page": page, "page_size": page_size}'

PATCHES = {
    "whatsapp_leads.py": [
        ("from app.models.whatsapp_lead import WhatsAppLead\n",
         "from app.models.whatsapp_lead import WhatsAppLead\nfrom app.services import lead_crm\n"),
        ('        "id": lead.id,\n        "country": lead.country,',
         '        "id": lead.id,\n        "lead_key": lead.sheet_key,\n        "country": lead.country,'),
        (LIST_RETURN,
         '    # Add the CRM details (vendor, interest, quote, converted) from the sidecar table\n'
         '    items = lead_crm.attach(db, "whatsapp", [_out(r) for r in rows], key=lambda i: i["lead_key"], location=lambda i: i["country"])\n'
         '    return {"items": items, "total": total, "page": page, "page_size": page_size}'),
    ],
    "wedding_leads.py": [
        ("from app.models.wedding_lead import WeddingLead\n",
         "from app.models.wedding_lead import WeddingLead\nfrom app.services import lead_crm\n"),
        ('        "id": lead.id,\n        "name": lead.name,',
         '        "id": lead.id,\n        "lead_key": lead.sheet_key,\n        "name": lead.name,'),
        (LIST_RETURN,
         '    # Add the CRM details (vendor, interest, quote, converted) from the sidecar table\n'
         '    items = lead_crm.attach(\n'
         '        db,\n'
         '        "wedding",\n'
         '        [_out(r) for r in rows],\n'
         '        key=lambda i: i["lead_key"],\n'
         '        location=lambda i: i["city"],\n'
         '        sheet_vendor=lambda i: i["shared_with"],\n'
         '    )\n'
         '    return {"items": items, "total": total, "page": page, "page_size": page_size}'),
    ],
    "wedding_plans.py": [
        ("from app.models.wedding_plan import WeddingPlan\n",
         "from app.models.wedding_plan import WeddingPlan\nfrom app.services import lead_crm\n"),
        (LIST_RETURN,
         '    # Add the CRM details (vendor, interest, quote, converted) from the sidecar table\n'
         '    items = lead_crm.attach(\n'
         '        db,\n'
         '        "wedding_planner",\n'
         '        [_out(r) for r in rows],\n'
         '        key=lambda i: i["plan_id"],\n'
         '        location=lambda i: i["city"] or i["home_city"],\n'
         '        sheet_vendor=lambda i: i["shared_with"],\n'
         '    )\n'
         '    return {"items": items, "total": total, "page": page, "page_size": page_size}'),
    ],
    "spin_leads.py": [
        ("from app.api.deps import AdminUser\n", "from app.api.deps import AdminUser, DbSession\n"),
        ('@router.get("")\ndef spin_leads_endpoint(\n    _admin: AdminUser,',
         'def _spin_location_keys(columns: list[dict]) -> tuple[str | None, str | None]:\n'
         '    """Names of the pincode and city/location fields in this table\'s output, if any."""\n'
         '    names = [c["name"] for c in columns]\n'
         '    lower = {n: n.lower().split(".")[-1] for n in names}\n'
         '    pin = next((n for n in names if "pincode" in lower[n] or lower[n] == "pin"), None)\n'
         '    city = next((n for n in names if "city" in lower[n] or "location" in lower[n]), None)\n'
         '    return pin, city\n'
         '\n\n'
         '@router.get("")\ndef spin_leads_endpoint(\n    _admin: AdminUser,\n    db: DbSession,'),
        ('    return response_cache.get(\n'
         '        _cache_key(page, page_size, search, sort, order),\n'
         '        lambda: list_spin_leads(None, page, page_size, search, sort, order),\n'
         '        fresh=FRESH_SECONDS,\n'
         '        stale=STALE_SECONDS,\n'
         '    )',
         '    data = response_cache.get(\n'
         '        _cache_key(page, page_size, search, sort, order),\n'
         '        lambda: list_spin_leads(None, page, page_size, search, sort, order),\n'
         '        fresh=FRESH_SECONDS,\n'
         '        stale=STALE_SECONDS,\n'
         '    )\n'
         '\n'
         '    # Sidecar: the workflow database is read-only, so the CRM details (vendor, interest,\n'
         '    # quote, converted) come from our own crm_lead_meta table, matched by the lead\'s id.\n'
         '    from app.services import lead_crm\n'
         '\n'
         '    pk = next((c.name for c in _get_table().primary_key.columns), "id")\n'
         '    pin_key, city_key = _spin_location_keys(data["columns"])\n'
         '    items = lead_crm.attach(\n'
         '        db,\n'
         '        "spin",\n'
         '        data["items"],\n'
         '        key=lambda i: i.get(pk),\n'
         '        location=lambda i: i.get(city_key) if city_key else None,\n'
         '        pincode=lambda i: str(i.get(pin_key) or "") if pin_key else None,\n'
         '    )\n'
         '    return {**data, "items": items}'),
    ],
}

problems = 0
for name, edits in PATCHES.items():
    path = BASE / name
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    if "lead_crm.attach" in text:
        print(f"= {name}: already done")
        continue
    missing = [old.splitlines()[0][:60] for old, _ in edits if old not in text]
    if missing:
        problems += 1
        print(f"✗ {name}: not changed, because this text was not found:")
        for m in missing:
            print(f"     {m!r}")
        continue
    for old, new in edits:
        text = text.replace(old, new, 1)
    shutil.copyfile(path, path.with_suffix(".py.bak"))
    path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    print(f"✓ {name}: CRM details added (backup: {name}.bak)")

if problems:
    print("\nSome files didn't match (they may have been edited by hand). Paste the full file from the guide instead.")
else:
    print("\nDone. Now run: python check_crm_setup.py")