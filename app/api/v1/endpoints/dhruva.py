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
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status

from app.api.deps import AdminUser, DbSession, DhruvaSectionUser, OptionalPeriod, PageSize
from app.crud import dhruva as crud
from app.models.dhruva import ADMIN_FIELDS, DhruvaBulkCreate, DhruvaLeadCreate, DhruvaLeadUpdate
from app.models.user import Role
from app.models.vendor_to_koc import STATUS_LABELS
from app.services import dhruva_import as parser
from app.services import also_in as also_in_service
from app.services import lead_filters, periods
from app.services import lead_activity
from app.services.access import Viewer
from app.services import vendor_lead_export as files

router = APIRouter(prefix="/dhruva", tags=["dhruva leads"])

SOURCE_MANUAL, SOURCE_BULK = "manual", "bulk_upload"
TEMPLATE_COLUMNS = [
    files.Column("Name", "text", 28), files.Column("Venue", "text", 28), files.Column("Date", "text", 18),
    files.Column("Phone", "phone", 16), files.Column("Alt phone", "phone", 16), files.Column("Event type", "text", 22),
]


def is_admin(user) -> bool:
    return user.role == Role.ADMIN.value


def viewer_of(user) -> Viewer:
    """For also_in: admins see every section; a Dhruva login (no vendor) sees none."""
    return Viewer(user, None)


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
    for row in saved:
        lead_activity.log_action(db, source="dhruva", lead_key=str(row.id), action="created", field="name",
            old=None, new=row.name, actor_user_id=user.id, actor_name=user.name, actor_role=user.role)
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
    lead_activity.log_action(db, source="dhruva", lead_key=str(row.id), action="created", field="name",
        old=None, new=row.name, actor_user_id=user.id, actor_name=user.name, actor_role=user.role)
    return crud.serialize(*crud.get(db, row.id), admin=is_admin(user), expose_status_updater_id=True)


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


# ── List, stats, export, filter options (one filter definition) ────────
SortField = Literal["created_at", "updated_at", "event_date", "name", "venue", "event_type", "update_status",
                    "follow_up_date"]
StatusFilter = Literal["pending", "converted", "not_converted", "purchased_elsewhere", "purchased_with_us",
                       "will_call_back", "interested", "not_interested"]


@dataclass(frozen=True)
class ListQuery:
    filters: crud.DhruvaFilters
    sort: str
    order: str


def list_query(
    user: DhruvaSectionUser,
    db: DbSession,
    period: OptionalPeriod,
    search: str | None = Query(None, max_length=100, description="Name, venue, event type, date text, phone"),
    event_from: str | None = Query(None, description="Event date from, YYYY-MM-DD (included)"),
    event_to: str | None = Query(None, description="Event date to, YYYY-MM-DD (included)"),
    upcoming_days: int | None = Query(None, description="Event from today to today + N days"),
    venue: str | None = Query(None, max_length=100, description="Venue contains (any case)"),
    event_type: list[str] | None = Query(None, description="Repeatable; event type contains (any case)"),
    update_status: list[StatusFilter] | None = Query(None, description="Repeatable; pending = no update yet"),
    quote: str | None = Query(None, description="Quoted amount: has, none, gt:<amount> or lt:<amount>"),
    conversion: str | None = Query(None, description="Conversion amount: has, none, gt:<amount> or lt:<amount>"),
    follow_up_from: date | None = Query(None),
    follow_up_to: date | None = Query(None),
    follow_up_due: bool = Query(False, description="Follow-up date today or earlier, open leads only"),
    has_phone: Literal["yes", "no"] | None = Query(None),
    also_in: Literal["yes", "no"] | None = Query(None, description="The phone also exists in another section "
                                                 "(admins only see them; for Dhruva logins nothing matches)"),
    sort: SortField = Query("created_at"),
    order: Literal["asc", "desc"] = Query("desc"),
) -> ListQuery:
    """Shared by GET /leads, /leads/stats and /leads/export. date_from / date_to: received
    (created_at), IST days, the other lists' rules. 400 for invalid values."""
    start = lead_filters._event_day(event_from, "event_from")
    end = lead_filters._event_day(event_to, "event_to")
    if upcoming_days is not None:
        if start or end:
            raise HTTPException(400, "Use either upcoming_days or event_from / event_to, not both")
        if not 1 <= upcoming_days <= lead_filters.MAX_UPCOMING_DAYS:
            raise HTTPException(400, f"upcoming_days must be between 1 and {lead_filters.MAX_UPCOMING_DAYS}")
        start = periods.today_ist()
        end = start + timedelta(days=upcoming_days)
    for low, high, name in ((start, end, "event"), (follow_up_from, follow_up_to, "follow_up")):
        if low and high and low > high:
            raise HTTPException(400, f"{name}_from must be on or before {name}_to")
    created_from, created_to = period.utc_bounds() if period is not None else (None, None)
    filters = crud.DhruvaFilters(
        search=search, created_from=created_from, created_to=created_to, event_from=start, event_to=end,
        venue=(venue or "").strip() or None, event_types=[e.strip() for e in event_type or [] if e.strip()],
        statuses=list(update_status or []), quote=lead_filters.parse_quote(quote),
        conversion=lead_filters.parse_quote(conversion, "conversion"),
        follow_up_from=follow_up_from, follow_up_to=follow_up_to,
        follow_up_due_by=periods.today_ist() if follow_up_due else None,
        has_phone=None if has_phone is None else has_phone == "yes",
    )
    if also_in:
        found = {int(k) for k in also_in_service.matching_keys(db, viewer_of(user), also_in_service.DHRUVA)}
        if also_in == "yes":
            filters.ids_in = found
        else:
            filters.ids_not_in = found
    return ListQuery(filters, sort, order)


ListQueryDep = Annotated[ListQuery, Depends(list_query)]


@router.get("/leads")
def list_leads(db: DbSession, user: DhruvaSectionUser, query: ListQueryDep, page: int = Query(1, ge=1),
               page_size: PageSize = 10):
    """{items (each with sl_no), total, page, page_size, pages}."""
    out = crud.list_page(db, query.filters, query.sort, query.order, page, page_size, admin=is_admin(user))
    also_in_service.attach(db, viewer_of(user), also_in_service.DHRUVA, out["items"], key_of=lambda i: i["id"],
                           e164_of={str(i["id"]): {i["phone_e164"], i["alt_phone_e164"]} for i in out["items"]})
    return out


@router.get("/leads/stats")
def lead_stats(db: DbSession, _user: DhruvaSectionUser, query: ListQueryDep):
    """{total, kpis, by_status, no_phone, follow_ups_due, upcoming_30_days} over the same leads."""
    return crud.stats(db, query.filters, periods.today_ist())


EXPORT_COLUMNS = [
    files.Column("Sl. No", "int", 8), files.Column("Name", "text", 28), files.Column("Venue", "text", 28),
    files.Column("Date", "text", 18), files.Column("Phone", "phone", 16), files.Column("Alt phone", "phone", 16),
    files.Column("Event type", "text", 24), files.Column("Update", "text", 28),
    files.Column("Additional comments", "text", 40), files.Column("Follow-up date", "date", 14),
    files.Column("Quoted amount", "money", 16), files.Column("Conversion amount", "money", 18),
    files.Column("Received", "datetime", 18), files.Column("Updated by", "text", 20),
    files.Column("Updated by ID", "int", 14), files.Column("Updated at", "datetime", 18),
]


@router.get("/leads/export", response_class=Response, responses={200: {
    "description": "The list as a file: " + ", ".join(c.heading for c in EXPORT_COLUMNS) + ".",
    "content": {files.XLSX_MEDIA_TYPE: {}, "text/csv": {}}}})
def export_leads(db: DbSession, _user: DhruvaSectionUser, query: ListQueryDep,
                 format: Literal["xlsx", "csv"] = Query("xlsx")):
    """Every lead matching the same filters, in list order (50,000 at most). The same columns for
    admins and Dhruva logins. It uploads back: every row is then a duplicate."""
    total = crud.count(db, query.filters)
    if total > files.EXPORT_MAX_ROWS:
        raise HTTPException(400, f"The export has {total:,} leads; the limit is {files.EXPORT_MAX_ROWS:,}. "
                                 "Narrow the filters.")
    table = []
    for i, (row, _creator, updater) in enumerate(crud.all_rows(db, query.filters, query.sort, query.order), start=1):
        table.append([
            i, row.name, row.venue, row.event_date_text or (row.event_date.isoformat() if row.event_date else None),
            row.phone_e164 or row.phone, row.alt_phone_e164 or row.alt_phone, row.event_type,
            STATUS_LABELS.get(row.update_status, row.update_status), row.additional_comments, row.follow_up_date,
            row.quoted_amount, row.conversion_amount, files.to_ist(row.created_at), updater,
            row.status_updated_by, files.to_ist(row.status_updated_at),
        ])
    content = files.to_xlsx(EXPORT_COLUMNS, table) if format == "xlsx" else files.to_csv(EXPORT_COLUMNS, table)
    media = files.XLSX_MEDIA_TYPE if format == "xlsx" else files.CSV_MEDIA_TYPE
    return Response(content, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="dhruva_leads_{periods.today_ist().isoformat()}.{format}"',
        "Cache-Control": "no-store"})


@router.get("/leads/filter-options")
def filter_options(db: DbSession, _user: DhruvaSectionUser):
    """Dropdown values with counts over every listed lead: venues and event types (spellings that
    differ only in case or spacing are one value), updates (pending first), has_phone, sources."""
    return crud.filter_options(db)


# ── One lead ───────────────────────────────────────────────────────────
@router.get("/leads/{lead_id}")
def get_lead(lead_id: int, db: DbSession, user: DhruvaSectionUser):
    found = crud.get(db, lead_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return crud.serialize(*found, admin=is_admin(user), expose_status_updater_id=True)


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
    before = {field: (row.event_date_text or row.event_date if field == "date" else getattr(row, field))
              for field in sent}
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
        if lead.e164s and not body.allow_duplicate:
            dup = crud.check_duplicates(db, [lead], exclude_id=row.id)[0]
            if dup:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    {"message": "Another lead with the same details and phone already exists.",
                                     "duplicate": dup})
        basic = crud.lead_columns(lead)
    admin_changes = {k: getattr(body, k) for k in admin_sent}
    crud.update(db, row, basic, admin_changes, user.id)
    after = {field: (row.event_date_text or row.event_date if field == "date" else getattr(row, field))
             for field in sent}
    lead_activity.log_changes(db, source="dhruva", lead_key=str(row.id),
        changes={field: (before[field], after[field]) for field in sent}, actor_user_id=user.id,
        actor_name=user.name, actor_role=user.role)
    return crud.serialize(*crud.get(db, lead_id), admin=is_admin(user), expose_status_updater_id=True)


@router.post("/leads/{lead_id}/archive")
def archive_lead(lead_id: int, db: DbSession, _admin: AdminUser):
    """Admins only. Archived leads leave the list, stats, export and dashboard; they still count as
    duplicates."""
    return _set_archived(db, lead_id, True, _admin)


@router.post("/leads/{lead_id}/unarchive")
def unarchive_lead(lead_id: int, db: DbSession, _admin: AdminUser):
    return _set_archived(db, lead_id, False, _admin)


def _set_archived(db, lead_id: int, archived: bool, user) -> dict:
    found = crud.get(db, lead_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    was_archived = bool(found[0].is_deleted)
    crud.set_archived(db, found[0], archived)
    if was_archived != archived:
        lead_activity.log_action(db, source="dhruva", lead_key=str(lead_id),
            action="archived" if archived else "unarchived", field="archived", old=was_archived,
            new=archived, actor_user_id=user.id, actor_name=user.name, actor_role=user.role)
    return crud.serialize(*crud.get(db, lead_id), admin=True)


@router.get("/leads/{lead_id}/activity")
def get_activity(lead_id: int, db: DbSession, user: DhruvaSectionUser,
                 page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    found = crud.get(db, lead_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return lead_activity.list_activity(db, source="dhruva", lead_key=str(lead_id), page=page, page_size=page_size)


@router.get("/reminders/follow-ups")
def dhruva_follow_up_reminders(db: DbSession, user: DhruvaSectionUser, days: int = Query(7, ge=0, le=30)):
    if is_admin(user):
        from app.api.v1.endpoints.reminders import _make_response, _reminder_rows
        return _make_response([r for r in _reminder_rows(db, None, dhruva_only=True) if r["source"] == "dhruva"],
                              days, True)
    from app.api.v1.endpoints.reminders import _make_response, _reminder_rows
    rows = [r for r in _reminder_rows(db, None, dhruva_only=True) if r["source"] == "dhruva"]
    return _make_response(rows, days, False)
