"""Dhruva leads: the leads Dhruva logins (role "dhruva") add or upload.

Who:
  admins          everything: add, upload, edit every field, archive / unarchive
  Dhruva logins   see every Dhruva lead; add, upload, edit the basic fields (name, venue, phone,
                  alt phone, date, event type); see the admin fields read-only (403 when sent)
  vendor logins   403 everywhere here (and Dhruva logins get 403 everywhere else)

Paths (prefix /api/v1/dhruva): upload/preview, leads/bulk, leads (POST add, GET list),
leads/stats, leads/export, leads/template, leads/filter-options, leads/{id} (GET, PATCH),
leads/{id}/archive, leads/{id}/unarchive.
"""
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile, status

from app.api.deps import AdminUser, DbSession, DhruvaSectionUser
from app.crud import dhruva as crud
from app.models.dhruva import ADMIN_FIELDS, DhruvaBulkCreate, DhruvaLeadCreate, DhruvaLeadUpdate
from app.models.user import Role
from app.services import dhruva_import as parser
from app.services import periods
from app.services import vendor_lead_export as files

router = APIRouter(prefix="/dhruva", tags=["dhruva leads"])

SOURCE_MANUAL, SOURCE_BULK = "manual", "bulk_upload"
TEMPLATE_COLUMNS = [
    files.Column("Name", "text", 28), files.Column("Venue", "text", 28), files.Column("Date", "text", 18),
    files.Column("Phone", "phone", 16), files.Column("Alt phone", "phone", 16), files.Column("Event type", "text", 22),
]


def is_admin(user) -> bool:
    return user.role == Role.ADMIN.value


def _status_of(lead: parser.CleanLead, duplicate: dict | None) -> str:
    if duplicate:
        return "duplicate"
    return "ok" if lead.ok else "error"


def _check(db, leads: list[parser.CleanLead], template_check: bool = True) -> list[dict | None]:
    if template_check:
        for lead in leads:
            if parser.is_template_example(lead) and parser.EXAMPLE_ROW_ERROR not in lead.errors:
                lead.errors.append(parser.EXAMPLE_ROW_ERROR)
    return crud.check_duplicates(db, leads)


# ── Upload preview ─────────────────────────────────────────────────────
@router.post("/upload/preview")
def upload_preview(
    db: DbSession,
    user: DhruvaSectionUser,
    file: UploadFile = File(..., description=".xlsx or .csv, at most 5 MB and 5,000 rows"),
    sheet_name: str | None = Form(None, description=".xlsx only; default the first sheet"),
):
    """Reads and checks the file; saves nothing. Each row: status ok / error ("needs fixing") /
    duplicate, flags (no_phone), the cleaned values to send to POST /leads/bulk, and the date as
    read (event_date). Duplicates: in_file (an earlier row) or existing (a saved lead)."""
    content = file.file.read(parser.MAX_FILE_BYTES + 1)
    try:
        data = parser.read_file(file.filename or "", content, sheet_name or None)
    except parser.FileError as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    today = periods.today_ist()
    leads = [parser.clean_lead(raw, today) for _, raw in data.rows]
    duplicates = _check(db, leads)
    rows, counts = [], {"total": len(leads), "valid": 0, "needs_fixing": 0, "duplicates": 0, "no_phone": 0,
                        "duplicates_by_reason": dict.fromkeys(crud.DUPLICATE_REASONS, 0)}
    for (row_number, _), lead, dup in zip(data.rows, leads, duplicates):
        status_ = _status_of(lead, dup)
        if dup and dup["reason"] == "in_file":
            dup = {"reason": "in_file", "duplicate_of_row": data.rows[dup["row_index"]][0]}
        counts[{"ok": "valid", "error": "needs_fixing", "duplicate": "duplicates"}[status_]] += 1
        if dup:
            counts["duplicates_by_reason"][dup["reason"]] += 1
        counts["no_phone"] += lead.no_phone and not any(e["field"] in ("phone", "alt_phone") for e in lead.errors)
        rows.append({"row": row_number, "status": status_, "values": lead.values(),
                     "event_date": lead.event_date.isoformat() if lead.event_date else None,
                     "flags": ["no_phone"] if lead.no_phone else [], "errors": lead.errors, "duplicate": dup})
    return {"sheet": data.sheet, "sheets": data.sheets, "heading_row": data.heading_row, "columns": data.columns,
            "ignored_columns": data.ignored_columns, "rows": rows, "counts": counts}


# ── Bulk save and single add ───────────────────────────────────────────
@router.post("/leads/bulk")
def bulk_create(body: DhruvaBulkCreate, db: DbSession, user: DhruvaSectionUser):
    """Checks every lead again (whatever the preview said), saves the valid ones, skips duplicates.
    Returns {added, duplicates_skipped, failed, errors: [{index, field, message}], ids}."""
    today = periods.today_ist()
    leads = [parser.clean_lead(item.model_dump(), today) for item in body.leads]
    duplicates = _check(db, leads)
    added, skipped, failed, errors, ids = 0, 0, 0, [], []
    saved = []
    for index, (lead, dup) in enumerate(zip(leads, duplicates)):
        if dup:
            skipped += 1
        elif not lead.ok:
            failed += 1
            errors += [{"index": index, **e} for e in lead.errors]
        else:
            saved.append(crud.create(db, lead, SOURCE_BULK, user.id, commit=False))
            added += 1
    db.commit()
    ids = [row.id for row in saved]
    return {"added": added, "duplicates_skipped": skipped, "failed": failed, "errors": errors, "ids": ids}


@router.post("/leads", status_code=status.HTTP_201_CREATED)
def create_lead(body: DhruvaLeadCreate, db: DbSession, user: DhruvaSectionUser):
    """One lead. 422 with field errors; 409 if it's a duplicate (send allow_duplicate: true to save
    anyway)."""
    lead = parser.clean_lead(body.model_dump(exclude={"allow_duplicate"}), periods.today_ist())
    if not lead.ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, lead.errors)
    dup = crud.check_duplicates(db, [lead])[0]
    if dup and not body.allow_duplicate:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": "This lead already exists.", "duplicate": dup})
    row = crud.create(db, lead, SOURCE_MANUAL, user.id)
    return crud.serialize(*crud.get(db, row.id), admin=is_admin(user))


# ── Template ───────────────────────────────────────────────────────────
@router.get("/leads/template", response_class=Response, responses={200: {
    "description": "Upload template: Name, Venue, Date, Phone, Alt phone, Event type and one example row.",
    "content": {files.XLSX_MEDIA_TYPE: {}, "text/csv": {}}}})
def template(_user: DhruvaSectionUser, format: Literal["xlsx", "csv"] = Query("xlsx")):
    e = parser.TEMPLATE_EXAMPLE
    rows = [[e["name"], e["venue"], e["date"], e["phone"], e["alt_phone"] or None, e["event_type"]]]
    content = files.to_xlsx(TEMPLATE_COLUMNS, rows) if format == "xlsx" else files.to_csv(TEMPLATE_COLUMNS, rows)
    media = files.XLSX_MEDIA_TYPE if format == "xlsx" else files.CSV_MEDIA_TYPE
    return Response(content, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="dhruva_template.{format}"', "Cache-Control": "no-store"})


# ── One lead ───────────────────────────────────────────────────────────
@router.get("/leads/{lead_id}")
def get_lead(lead_id: int, db: DbSession, user: DhruvaSectionUser):
    found = crud.get(db, lead_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return crud.serialize(*found, admin=is_admin(user))


@router.patch("/leads/{lead_id}")
def update_lead(lead_id: int, body: DhruvaLeadUpdate, db: DbSession, user: DhruvaSectionUser):
    """Only the fields sent change; null clears one. Basic fields (name, venue, date, phone,
    alt_phone, event_type): admins and Dhruva logins; a changed phone is checked for duplicates
    (409 unless allow_duplicate). Admin fields (update_status, additional_comments, quoted_amount,
    conversion_amount, follow_up_date): admins only (Dhruva logins: 403)."""
    sent = body.model_fields_set - {"allow_duplicate"}
    if not sent:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Send at least one field to change.")
    admin_sent = sorted(sent & set(ADMIN_FIELDS))
    if admin_sent and not is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Only admins can change {', '.join(admin_sent)}.")
    found = crud.get(db, lead_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    row = found[0]
    basic: dict = {}
    basic_sent = sent - set(ADMIN_FIELDS)
    if basic_sent:
        current = {"name": row.name, "venue": row.venue, "date": row.event_date_text or row.event_date,
                   "phone": row.phone, "alt_phone": row.alt_phone, "event_type": row.event_type}
        merged = {**current, **{k: getattr(body, k) for k in basic_sent}}
        if "date" in basic_sent and isinstance(merged["date"], str) and merged["date"] == row.event_date_text:
            merged["date"] = row.event_date or row.event_date_text   # unchanged text keeps its date
        lead = parser.clean_lead(merged, periods.today_ist())
        if "date" not in basic_sent:
            lead.event_date, lead.event_date_text = row.event_date, row.event_date_text
        if not lead.ok:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, lead.errors)
        phone_changed = lead.e164s != {e for e in (row.phone_e164, row.alt_phone_e164) if e}
        if phone_changed and lead.e164s and not body.allow_duplicate:
            dup = crud.check_duplicates(db, [lead], exclude_id=row.id)[0]
            if dup:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    {"message": "Another lead has this phone.", "duplicate": dup})
        basic = crud.lead_columns(lead)
    admin_changes = {k: getattr(body, k) for k in admin_sent}
    crud.update(db, row, basic, admin_changes, user.id)
    return crud.serialize(*crud.get(db, lead_id), admin=is_admin(user))


@router.post("/leads/{lead_id}/archive")
def archive_lead(lead_id: int, db: DbSession, _admin: AdminUser):
    """Admins only. Archived leads leave the list, stats, export and dashboard; they still count as
    duplicates."""
    return _set_archived(db, lead_id, True)


@router.post("/leads/{lead_id}/unarchive")
def unarchive_lead(lead_id: int, db: DbSession, _admin: AdminUser):
    return _set_archived(db, lead_id, False)


def _set_archived(db, lead_id: int, archived: bool) -> dict:
    found = crud.get(db, lead_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    crud.set_archived(db, found[0], archived)
    return crud.serialize(*crud.get(db, lead_id), admin=True)
