"""Where a lead row in a crm_* table came from.

  sheet     copied from the Google Sheet by the sync (the only rows the sync may touch)
  added     entered by hand in the CRM
  imported  loaded from a file in the CRM

Added and imported rows live in the same tables as the sheet rows, but the sheet sync never
inserts, updates, deletes or marks them removed (see app/services/sheet_sync.py).
"""
import uuid

from sqlmodel import Field

ORIGIN_SHEET = "sheet"
ORIGIN_ADDED = "added"
ORIGIN_IMPORTED = "imported"
ORIGINS = (ORIGIN_SHEET, ORIGIN_ADDED, ORIGIN_IMPORTED)
CRM_ORIGINS = (ORIGIN_ADDED, ORIGIN_IMPORTED)

# Prefix of the keys given to added/imported rows. Sheet keys are SHA-1 hex digests or the
# sheet's Plan IDs (KOC-...), so they never start with this.
CRM_KEY_PREFIX = "crm-"


def origin_field():
    """The `origin` column. server_default fills existing rows with 'sheet'."""
    return Field(
        default=ORIGIN_SHEET,
        max_length=10,
        index=True,
        sa_column_kwargs={"server_default": ORIGIN_SHEET},
    )


def new_crm_key() -> str:
    """Key (sheet_key / plan_id / crm_lead_meta.lead_key) for an added or imported row."""
    return CRM_KEY_PREFIX + uuid.uuid4().hex
