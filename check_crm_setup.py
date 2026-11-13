"""Checks that every backend file for the lead CRM columns is in place.

Run from the backend folder (venv active):
    python check_crm_setup.py
"""
import importlib
import inspect

CHECKS = [
    ("app.services.lead_crm", "def seed_vendors", "service code (vendor rules)"),
    ("app.api.v1.endpoints.lead_crm", "router = APIRouter", "PUT /leads/{source}/{key}/crm endpoint"),
    ("app.api.v1.endpoints.vendors", "router = APIRouter", "vendors endpoints"),
    ("app.api.v1.endpoints.spin_leads", "lead_crm.attach", "spin leads send 'crm'"),
    ("app.api.v1.endpoints.whatsapp_leads", "lead_crm.attach", "international leads send 'crm'"),
    ("app.api.v1.endpoints.wedding_leads", "lead_crm.attach", "wedding leads send 'crm'"),
    ("app.api.v1.endpoints.wedding_plans", "lead_crm.attach", "wedding planner leads send 'crm'"),
    ("app.api.v1.router", "lead_crm.router", "router includes the lead CRM endpoint"),
    ("app.main", "seed_vendors", "startup creates the vendors"),
]

ok = True
for module, needle, what in CHECKS:
    try:
        source = inspect.getsource(importlib.import_module(module))
        good = needle in source
    except Exception as exc:  # noqa: BLE001
        good = False
        what += f"  (import failed: {exc})"
    ok &= good
    print(f"{'✓' if good else '✗'} {module.replace('.', '/')}.py  {what}")

print("\nAll good. Restart the backend if it is running." if ok else "\nReplace the ✗ files (see the guide), then run this again.")