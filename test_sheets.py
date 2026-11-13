"""Check the Google Sheets connection without starting the server.

Run from the backend folder (venv active):
    python test_sheets.py

For every sheet in GOOGLE_SHEETS it prints the sheet name, tabs, header row and row count.
It does NOT print row contents, so it's safe to paste the output in a chat.
"""
import json
import sys

from app.core.config import settings


def fail(message: str) -> None:
    print(f"\n✗ {message}")
    sys.exit(1)


print("Google Sheets connection test")
print("─" * 40)

if not settings.GOOGLE_SERVICE_ACCOUNT_FILE:
    fail("GOOGLE_SERVICE_ACCOUNT_FILE is missing in .env")
if not settings.GOOGLE_SHEETS:
    fail('GOOGLE_SHEETS is missing in .env, e.g. GOOGLE_SHEETS={"whatsapp": "<sheet id>"}')
print(f"✓ .env settings found ({len(settings.GOOGLE_SHEETS)} sheet(s): {', '.join(settings.GOOGLE_SHEETS)})")

try:
    with open(settings.GOOGLE_SERVICE_ACCOUNT_FILE, encoding="utf-8") as f:
        key = json.load(f)
except FileNotFoundError:
    fail(f"Key file not found: {settings.GOOGLE_SERVICE_ACCOUNT_FILE!r} (run this from the backend folder)")
except json.JSONDecodeError:
    fail("The key file is not valid JSON. Download the key again (Keys → Add key → JSON).")
email = key.get("client_email")
if not email:
    fail("The key file has no client_email. Is this a service account key?")
print(f"✓ Key file OK. Service account: {email}")

import gspread  # noqa: E402

from app.services import google_sheets  # noqa: E402

problems = 0
for source in settings.GOOGLE_SHEETS:
    print(f"\n[{source}]")
    try:
        tabs = google_sheets.list_worksheets(source)
        data = google_sheets.read_sheet(source, refresh=True)
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"  ✗ Sheet not found. Share it with {email} (Viewer) and check its id.")
        problems += 1
        continue
    except gspread.exceptions.APIError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        if status == 403:
            print(f"  ✗ Access denied (403). Enable the Google Sheets API, and share the sheet with {email}.")
        else:
            print(f"  ✗ Google API error ({status}): {exc}")
        problems += 1
        continue

    print(f"  ✓ {data['sheet']}")
    print(f"    Tabs:    {', '.join(t['title'] for t in tabs)}")
    print(f"    Rows:    {len(data['rows'])} in '{data['worksheet']}' (excluding header and empty rows)")
    print(f"    Columns: {', '.join(data['columns'])}")

if problems:
    fail(f"{problems} sheet(s) could not be read. Fix the messages above and run again.")
print("\n✓ All good. The backend can read your sheets.")