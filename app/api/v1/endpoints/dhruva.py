"""Dhruva -> KOC leads (crm_dhruva_leads): leads Dhruva logins add, and admins upload.

Who:
  admins          everything: upload, add, edit every field, archive / unarchive
  view-only       read everything (the central 403 on writes)
  Dhruva logins   see every lead; add, edit the lead fields (name, phone numbers, event dates,
                  event type, venue); see the KOC fields read-only (403 when sent)
  vendor logins   403 everywhere here (and Dhruva logins get 403 everywhere else)

Fields: see app/models/dhruva.py. sent_at is set when the lead is added or uploaded and never
edited. Every create and change is logged (GET leads/{id}/activity; also in GET leads/{id}).

Paths (prefix /api/v1/dhruva): leads/template, upload/preview, leads/bulk (admins), leads (POST add,
GET list), leads/stats, leads/export, leads/filter-options, leads/{id} (GET, PATCH),
leads/{id}/hot, leads/{id}/archive, leads/{id}/unarchive, leads/{id}/activity, reminders/follow-ups.
"""
import io
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status

from app.api.deps import AdminUser, DbSession, DhruvaSectionUser, OptionalPeriod, PageSize, ReadAdmin
from app.crud import dhruva as crud
from app.models.dhruva import ADMIN_FIELDS, LEAD_FIELDS, DhruvaBulkCreate, DhruvaLeadCreate, DhruvaLeadUpdate
from app.models.user import Role
from app.models.vendor_to_koc import STATUS_LABELS
from app.services import dhruva_import as parser
from app.services import also_in as also_in_service
from app.services import lead_filters, periods
from app.services import lead_activity
from app.services import hot_leads
from app.services.access import Viewer
from app.services import vendor_lead_export as files

router = APIRouter(prefix="/dhruva", tags=["dhruva leads"])

SOURCE_MANUAL, SOURCE_BULK = "manual", "bulk_upload"
ACTIVITY_ON_LEAD = 50                    # newest activity entries returned with GET /leads/{id}


def is_admin(user) -> bool:
    """May make admin changes (write paths)."""
    return user.role == Role.ADMIN.value


def sees_all(user) -> bool:
    """Reads the admin-only fields: admins and view-only logins."""
    return user.role in (Role.ADMIN.value, Role.VIEWER.value)


def viewer_of(user) -> Viewer:
    """For also_in: admins and view-only logins see every section; a Dhruva login (no vendor) sees none."""
    return Viewer(user, None)


def _status_of(lead: parser.CleanLead, duplicate: dict | None) -> str:
    if duplicate:
        return "duplicate"
    return "ok" if lead.ok else "error"


def _check(db, leads: list[parser.CleanLead]) -> list[dict | None]:
    for lead in leads:
        if parser.is_template_example(lead) and parser.EXAMPLE_ROW_ERROR not in lead.errors:
            lead.errors.append(parser.EXAMPLE_ROW_ERROR)
    return crud.check_duplicates(db, leads)


def _log_created(db, row, user) -> None:
    lead_activity.log_action(db, source="dhruva", lead_key=str(row.id), action="created", field="lead",
                             old=None, new=row.name, actor_user_id=user.id, actor_name=user.name,
                             actor_role=user.role)


# ── Template (admins and view-only) ────────────────────────────────────
@router.get("/leads/template", response_class=Response, responses={200: {
    "description": "Upload template: sheet Leads with " + ", ".join(parser.LABELS.values())
                   + " (every column Text) and one example row.",
    "content": {files.XLSX_MEDIA_TYPE: {}, "text/csv": {}}}})
def template(_user: ReadAdmin, format: Literal["xlsx", "csv"] = Query("xlsx")):
    headings = [label for _, label in parser.HEADINGS]
    example = [parser.TEMPLATE_EXAMPLE[key] or None for key, _ in parser.HEADINGS]
    if format == "csv":
        content = files.to_csv([files.Column(h, "text", 20) for h in headings], [example])
        media = files.CSV_MEDIA_TYPE
    else:
        content = _template_xlsx(headings, example)
        media = files.XLSX_MEDIA_TYPE
    return Response(content, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="dhruva_to_koc_template.{format}"', "Cache-Control": "no-store"})


def _template_xlsx(headings: list[str], example: list) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = parser.SHEET_NAME
    ws.append(headings)
    for i, heading in enumerate(headings, start=1):
        ws.cell(row=1, column=i).font = Font(bold=True, color="C00000" if heading == "Name" else None)
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(heading) + 4)
        for r in range(2, 2002):                          # Text: Excel keeps dates and numbers as typed
            ws.cell(row=r, column=i).number_format = "@"
        value = example[i - 1]
        ws.cell(row=2, column=i).value = None if value is None else str(value)
    ws.freeze_panes = "A2"
    info = wb.create_sheet("Instructions")
    for line in (
        "One lead per row on the Leads sheet. Only Name is required. Delete the example row before uploading.",
        "Phone numbers: 10 digits each, comma-separated. Duplicates: any same phone number, or (no phone) the same name and first event date.",
        "Event date: real dates as YYYY-MM-DD, comma-separated (2026-12-04, 2026-12-05).",
        "Update: " + ", ".join(STATUS_LABELS.values()) + " (empty = Pending).",
        "Needs Dhruva intervention: Yes or No. Amounts in rupees. Follow-up date: YYYY-MM-DD.",
    ):
        info.append([line])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Upload preview and bulk save (admins) ──────────────────────────────
@router.post("/upload/preview")
def upload_preview(
    db: DbSession,
    user: AdminUser,
    file: UploadFile = File(..., description=".xlsx or .csv, at most 5 MB and 5,000 rows"),
    sheet_name: str | None = Form(None, description='.xlsx only; default the sheet "Leads", else the first'),
):
    """Reads and checks the file; saves nothing. Each row: status ok / error / duplicate, flags
    (no_phone), the cleaned values to send to POST /leads/bulk, the errors and the duplicate
    (in_file: an earlier row; existing: a saved lead)."""
    content = file.file.read(parser.MAX_FILE_BYTES + 1)
    try:
        data = parser.read_file(file.filename or "", content, sheet_name or None)
    except parser.FileError as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    leads = [parser.clean_lead(raw) for _, raw in data.rows]
    duplicates = _check(db, leads)
    rows, counts = [], {"total": len(leads), "valid": 0, "needs_fixing": 0, "duplicates": 0, "no_phone": 0,
                        "duplicates_by_reason": dict.fromkeys(crud.DUPLICATE_REASONS, 0)}
    for (row_number, raw), lead, dup in zip(data.rows, leads, duplicates):
        status_ = _status_of(lead, dup)
        values = lead.values()
        for problem in lead.errors:          # send back what was written, so the bulk save rejects it again
            if problem["field"] in raw:
                values[problem["field"]] = parser.as_text(raw[problem["field"]]).strip() or None
        if dup and dup["reason"] == "in_file":
            dup = {"reason": "in_file", "duplicate_of_row": data.rows[dup["row_index"]][0]}
        counts[{"ok": "valid", "error": "needs_fixing", "duplicate": "duplicates"}[status_]] += 1
        if dup:
            counts["duplicates_by_reason"][dup["reason"]] += 1
        counts["no_phone"] += lead.no_phone and not any(e["field"] == "phones" for e in lead.errors)
        rows.append({"row": row_number, "status": status_, "values": values,
                     "flags": ["no_phone"] if lead.no_phone else [], "errors": lead.errors, "duplicate": dup})
    return {"sheet": data.sheet, "sheets": data.sheets, "heading_row": data.heading_row, "columns": data.columns,
            "ignored_columns": data.ignored_columns, "rows": rows, "counts": counts}


@router.post("/leads/bulk")
def bulk_create(body: DhruvaBulkCreate, db: DbSession, user: AdminUser):
    """Admins. Checks every lead again (whatever the preview said), saves the valid ones (sent_at =
    now), skips duplicates. Returns {added, duplicates_skipped, failed, errors: [{index, field,
    message}], ids}."""
    leads = [parser.clean_lead(item.model_dump()) for item in body.leads]
    duplicates = _check(db, leads)
    added, skipped, failed, errors = 0, 0, 0, []
    to_save = []
    for index, (lead, dup) in enumerate(zip(leads, duplicates)):
        if dup:
            skipped += 1
        elif not lead.ok:
            failed += 1
            errors += [{"index": index, **e} for e in lead.errors]
        else:
            to_save.append(lead)
            added += 1
    saved = [(row.id, row.name) for row in crud.create_many(db, to_save, SOURCE_BULK, user.id)]
    db.commit()                               # one transaction; ids and names read before it (no refreshes)
    lead_activity.log_actions(db, [{"source": "dhruva", "lead_key": str(i), "action": "created", "field": "lead",
                                    "old": None, "new": name} for i, name in saved],
                              actor_user_id=user.id, actor_name=user.name, actor_role=user.role)
    return {"added": added, "duplicates_skipped": skipped, "failed": failed, "errors": errors,
            "ids": [i for i, _ in saved]}


# ── Single add ─────────────────────────────────────────────────────────
@router.post("/leads", status_code=status.HTTP_201_CREATED)
def create_lead(body: DhruvaLeadCreate, db: DbSession, user: DhruvaSectionUser):
    """One lead (sent_at = now). Dhruva logins send the lead fields; admins may also send the KOC
    fields (Dhruva logins: 403). 422 with field errors; 409 if it's a duplicate (send
    allow_duplicate: true to save anyway)."""
    sent = body.model_fields_set - {"allow_duplicate"}
    admin_sent = sorted(sent & set(ADMIN_FIELDS))
    if admin_sent and not is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Only admins can set {', '.join(admin_sent)}.")
    lead = parser.clean_lead(body.model_dump(exclude={"allow_duplicate"}))
    if not lead.ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, lead.errors)
    dup = crud.check_duplicates(db, [lead])[0]
    if dup and not body.allow_duplicate:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": "This lead already exists.", "duplicate": dup})
    row = crud.create(db, lead, SOURCE_MANUAL, user.id)
    _log_created(db, row, user)
    return _serialize_lead(db, crud.get(db, row.id), user)


# ── List, stats, export, filter options (one filter definition) ────────
SortField = Literal["created_at", "updated_at", "sent_at", "event_date", "name", "venue", "event_type",
                    "update_status", "follow_up_date", "whatsapp_status"]
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
    search: str | None = Query(None, max_length=100, description="Name, venue, event type, any phone number "
                                                                 "(digits), WhatsApp status, response, message "
                                                                 "sent, notes, comments"),
    event_from: str | None = Query(None, description="Any event date from, YYYY-MM-DD (included)"),
    event_to: str | None = Query(None, description="Any event date to, YYYY-MM-DD (included)"),
    upcoming_days: int | None = Query(None, description="Any event date from today to today + N days"),
    venue: str | None = Query(None, max_length=100, description="Venue contains (any case)"),
    event_type: list[str] | None = Query(None, description="Repeatable; event type contains (any case)"),
    update_status: list[StatusFilter] | None = Query(None, description="Repeatable; pending = no update yet"),
    quote: str | None = Query(None, description="Quoted amount: has, none, gt:<amount> or lt:<amount>"),
    conversion: str | None = Query(None, description="Conversion amount: has, none, gt:<amount> or lt:<amount>"),
    follow_up_from: date | None = Query(None),
    follow_up_to: date | None = Query(None),
    follow_up_due: bool = Query(False, description="Follow-up date today or earlier, open leads only"),
    has_phone: Literal["yes", "no"] | None = Query(None),
    needs_intervention: Literal["yes", "no"] | None = Query(None, description="Needs Dhruva intervention"),
    hot: Literal["yes", "no"] | None = Query(None, description="Hot lead filter"),
    also_in: Literal["yes", "no"] | None = Query(None, description="The phone also exists in another section "
                                                 "(admins only see them; for Dhruva logins nothing matches)"),
    sort: SortField = Query("created_at", description="event_date: the earliest event date"),
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
        needs_intervention=None if needs_intervention is None else needs_intervention == "yes",
        hot=None if hot is None else hot == "yes",
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
    out = crud.list_page(db, query.filters, query.sort, query.order, page, page_size, admin=sees_all(user))
    also_in_service.attach(db, viewer_of(user), also_in_service.DHRUVA, out["items"], key_of=lambda i: i["id"],
                           e164_of={str(i["id"]): set(i["phones_e164"]) for i in out["items"]})
    return out


@router.get("/leads/stats")
def lead_stats(db: DbSession, _user: DhruvaSectionUser, query: ListQueryDep):
    """{total, kpis, by_status, no_phone, no_event_date, needs_intervention, follow_ups_due,
    upcoming_30_days} over the same leads."""
    return crud.stats(db, query.filters, periods.today_ist())


EXPORT_COLUMNS = [
    files.Column("Sl. No", "int", 8), files.Column("Name", "text", 28), files.Column("Phone numbers", "text", 26),
    files.Column("Event date", "text", 24),
    files.Column("Event type", "text", 22), files.Column("Venue", "text", 28),
    files.Column("WhatsApp status", "text", 18), files.Column("Response", "text", 30),
    files.Column("Message sent", "text", 20), files.Column("Follow-up notes", "text", 36),
    files.Column("Comments", "text", 36), files.Column("Needs Dhruva intervention", "yesno", 14),
    files.Column("Update", "text", 26), files.Column("Quoted amount", "money", 16),
    files.Column("Conversion amount", "money", 18), files.Column("Follow-up date", "date", 14),
    files.Column("Sent at", "datetime", 18), files.Column("Added by", "text", 18),
    files.Column("Updated by", "text", 18), files.Column("Updated at", "datetime", 18), files.Column("Hot", "yesno", 8),
]


@router.get("/leads/export", response_class=Response, responses={200: {
    "description": "The list as a file: " + ", ".join(c.heading for c in EXPORT_COLUMNS) + ".",
    "content": {files.XLSX_MEDIA_TYPE: {}, "text/csv": {}}}})
def export_leads(db: DbSession, _user: DhruvaSectionUser, query: ListQueryDep,
                 format: Literal["xlsx", "csv"] = Query("xlsx")):
    """Every lead matching the same filters, in list order (50,000 at most). The same columns for
    admins and Dhruva logins. The upload template's columns come first (an export uploads back:
    every row is then a duplicate; the other columns are ignored)."""
    total = crud.count(db, query.filters)
    if total > files.EXPORT_MAX_ROWS:
        raise HTTPException(400, f"The export has {total:,} leads; the limit is {files.EXPORT_MAX_ROWS:,}. "
                                 "Narrow the filters.")
    found = crud.all_rows(db, query.filters, query.sort, query.order)
    dates = crud.dates_of(db, [row.id for row, _, _ in found])
    numbers = crud.phones_of(db, [row.id for row, _, _ in found])
    marked_hot = hot_leads.hot_keys(db, "dhruva")
    table = []
    for i, (row, creator, updater) in enumerate(found, start=1):
        table.append([
            i, row.name, parser.phones_text(numbers.get(row.id, [])), parser.dates_text(dates.get(row.id, [])),
            row.event_type,
            row.venue, row.whatsapp_status, row.response, row.message_sent, row.follow_up_notes,
            row.additional_comments, bool(row.needs_intervention),
            STATUS_LABELS.get(row.update_status, row.update_status), row.quoted_amount, row.conversion_amount,
            row.follow_up_date, files.to_ist(row.sent_at) if row.sent_at else None, creator, updater,
            files.to_ist(row.status_updated_at) if row.status_updated_at else None, str(row.id) in marked_hot,
        ])
    content = files.to_xlsx(EXPORT_COLUMNS, table) if format == "xlsx" else files.to_csv(EXPORT_COLUMNS, table)
    media = files.XLSX_MEDIA_TYPE if format == "xlsx" else files.CSV_MEDIA_TYPE
    return Response(content, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="dhruva_leads_{periods.today_ist().isoformat()}.{format}"',
        "Cache-Control": "no-store"})


@router.get("/leads/filter-options")
def filter_options(db: DbSession, _user: DhruvaSectionUser):
    """Dropdown values with counts over every listed lead: venues, event types and WhatsApp statuses
    (spellings that differ only in case or spacing are one value), updates (pending first),
    has_phone, needs_intervention, sources."""
    return crud.filter_options(db)


# ── One lead ───────────────────────────────────────────────────────────
@router.get("/leads/{lead_id}")
def get_lead(lead_id: int, db: DbSession, user: DhruvaSectionUser):
    """The lead with its newest activity entries (activity: who, when, field, old -> new)."""
    found = crud.get(db, lead_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    out = _serialize_lead(db, found, user)
    out["activity"] = lead_activity.list_activity(db, source="dhruva", lead_key=str(lead_id), page=1,
                                                  page_size=ACTIVITY_ON_LEAD)["items"]
    return out


def _serialize_lead(db, found, user):
    row, creator_name, updater_name = found
    return crud.serialize(row, creator_name, updater_name, admin=sees_all(user), expose_status_updater_id=True,
                          is_hot=hot_leads.is_hot(db, "dhruva", str(row.id)),
                          dates=crud.dates_of(db, [row.id]).get(row.id, []),
                          numbers=crud.phones_of(db, [row.id]).get(row.id, []))


@router.patch("/leads/{lead_id}")
def update_lead(lead_id: int, body: DhruvaLeadUpdate, db: DbSession, user: DhruvaSectionUser):
    """Only the fields sent change; null clears one (not name). Lead fields (name, phones,
    event_dates, event_type, venue): admins and Dhruva logins; changed phones, name or event dates
    are checked for duplicates (409 unless allow_duplicate). KOC fields (whatsapp_status,
    response, message_sent, follow_up_notes, comments, needs_intervention, update_status,
    quoted_amount, conversion_amount, follow_up_date): admins only (Dhruva logins: 403). Each
    changed field is logged (old -> new)."""
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
    current_dates = crud.dates_of(db, [row.id]).get(row.id, [])
    current_numbers = crud.phones_of(db, [row.id]).get(row.id, [])
    before = crud.values_of(row, current_dates, current_numbers)
    lead = parser.clean_lead(body.model_dump(include=sent), keys=sent)
    if not lead.ok:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, lead.errors)
    if sent & {"name", "phones", "event_dates"} and not body.allow_duplicate:
        merged = parser.clean_lead({**before, **{k: getattr(lead, k) for k in sent if k in LEAD_FIELDS}},
                                   keys={"name", "phones", "event_dates"})
        dup = crud.check_duplicates(db, [merged], exclude_id=row.id)[0]
        if dup:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                {"message": "Another lead with one of these phone numbers (or the same name and "
                                            "first event date) already exists.", "duplicate": dup})
    changes = crud.columns_of(lead)
    for derived in ("event_date", "phone", "phone_e164"):               # set with the lists
        changes.pop(derived, None)
    dates = lead.event_dates if "event_dates" in sent else None
    numbers = lead.phones if "phones" in sent else None
    crud.update(db, row, changes, dates, admin_changed=bool(admin_sent), user_id=user.id, numbers=numbers)
    after = crud.values_of(row, crud.dates_of(db, [row.id]).get(row.id, []),
                           crud.phones_of(db, [row.id]).get(row.id, []))
    lead_activity.log_changes(db, source="dhruva", lead_key=str(row.id),
                              changes={k: (before[k], after[k]) for k in before}, actor_user_id=user.id,
                              actor_name=user.name, actor_role=user.role)
    return get_lead(lead_id, db, user)


@router.post("/leads/{lead_id}/hot")
def mark_lead_hot(lead_id: int, db: DbSession, user: DhruvaSectionUser):
    return _set_hot(db, lead_id, user, True)


@router.delete("/leads/{lead_id}/hot")
def unmark_lead_hot(lead_id: int, db: DbSession, user: DhruvaSectionUser):
    return _set_hot(db, lead_id, user, False)


def _set_hot(db, lead_id: int, user, hot: bool) -> dict:
    found = crud.get(db, lead_id)
    if found is None or found[0].is_deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    row = found[0]
    was_hot = hot_leads.is_hot(db, "dhruva", str(lead_id))
    result = hot_leads.set_hot(db, "dhruva", str(lead_id), user.id, hot)
    if was_hot != hot:
        lead_activity.log_action(db, source="dhruva", lead_key=str(lead_id),
            action="hot_on" if hot else "hot_off", field="is_hot", old=was_hot, new=hot,
            actor_user_id=user.id, actor_name=user.name, actor_role=user.role)
    return {"id": row.id, **result}


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
    return _serialize_lead(db, crud.get(db, lead_id), user)


@router.get("/leads/{lead_id}/activity")
def get_activity(lead_id: int, db: DbSession, user: DhruvaSectionUser,
                 page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    found = crud.get(db, lead_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return lead_activity.list_activity(db, source="dhruva", lead_key=str(lead_id), page=page, page_size=page_size)


DHRUVA_REMINDER_SOURCES = ("dhruva", "koc_to_dhruva")        # both Dhruva directions


@router.get("/reminders/follow-ups")
def dhruva_follow_up_reminders(db: DbSession, user: DhruvaSectionUser, days: int = Query(7, ge=0, le=30),
                               sources: str | None = Query(None, description="dhruva, koc_to_dhruva or both "
                                                                             "(default both)")):
    """Follow-ups of both Dhruva directions: Dhruva -> KOC (source "dhruva") and KOC -> Dhruva
    (source "koc_to_dhruva"); each item says which. sources narrows it (same parsing as
    GET /reminders/follow-ups, only these two values)."""
    from app.api.v1.endpoints.reminders import _make_response, _reminder_rows
    from app.services.lead_sources import parse_sources

    wanted = parse_sources(sources, allowed=DHRUVA_REMINDER_SOURCES)
    rows = [r for r in _reminder_rows(db, None, dhruva_only=True) if r["source"] in wanted]
    return _make_response(rows, days, sees_all(user))
