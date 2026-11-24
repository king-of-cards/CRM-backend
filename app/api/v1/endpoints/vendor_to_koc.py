"""Vendor to KOC: leads that vendors send to KOC.

Vendor users add leads and work only with their own vendor (vendor_id from the login, never
from input). They see KOC's update (update_status, additional_comments, follow_up_date,
status_updated_at, status_updated_by_name) read-only and may filter and sort by it. Admins see
every vendor and edit those fields (PATCH).
"""
from dataclasses import dataclass
from datetime import date, timezone
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlmodel import select

from app.api.deps import AdminUser, DbSession, LeadViewer, NotDhruvaUser, OptionalPeriod, PageSize, ReadAdmin
from app.crud import vendor_to_koc as crud
from app.services import vendor_lead_export as lead_export
from app.models.user import Role, User
from app.models.vendor import Vendor
from app.models.vendor_to_koc import (
    LeadBulkCreate, LeadCreate, LeadUpdate, UpdateStatus, VendorCreate, VendorRead, VendorUpdate, VendorUserLink,
    VendorToKocLead,
)
from app.services import periods
from app.services import also_in as also_in_service
from app.services import notifications
from app.services import lead_activity
from app.services.lead_filters import parse_quote
from app.services.access import Viewer
from app.services.vendor_lead_import import (
    MAX_FILE_BYTES, PHONE_RE, ImportFileError, flag_template_example, parse_file_date,
    parse_json_date, read_file, validate_lead,
)

router = APIRouter(prefix="/vendor-to-koc", tags=["vendor to KOC"])
logger = logging.getLogger(__name__)

StatusFilter = Literal[
    "pending", "converted", "not_converted", "purchased_elsewhere", "purchased_with_us",
    "will_call_back", "interested", "not_interested",
]
SortField = Literal[
    "created_at", "updated_at", "lead_date", "name", "city", "phone", "vendor_name",
    "update_status", "follow_up_date", "is_hot",
]

LEAD_RESPONSE_DOC = ("Admins get every field. Vendor users get the same object without "
                     "status_updated_by (a user id); status_updated_by_name is there for everyone.")


def _unprocessable(errors: list[dict]) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors)


ONLY_VENDORS_ADD = "Only vendor logins can add Vendor → KOC leads"


def require_vendor_login(scope: LeadViewer) -> Viewer:
    """Adding leads (preview, bulk, manual) is for vendor logins only: admins (and view only) get 403."""
    if scope.sees_all:
        raise HTTPException(status.HTTP_403_FORBIDDEN, ONLY_VENDORS_ADD)
    return scope


VendorLogin = Annotated[Viewer, Depends(require_vendor_login)]


def _target_vendor(db: DbSession, scope: Viewer) -> Vendor:
    """The vendor new leads belong to: always the login's vendor (a vendor_id sent is ignored)."""
    vendor = db.get(Vendor, scope.vendor_id)
    if vendor is None or not vendor.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Vendor is inactive")
    return vendor


def _phone_ok(phone: str | None) -> bool:
    return phone is not None and PHONE_RE.match(phone) is not None


# ── Upload preview ─────────────────────────────────────────────────────
@router.post("/upload/preview")
def upload_preview(
    db: DbSession,
    scope: VendorLogin,
    file: UploadFile = File(..., description=".xlsx or .csv, at most 5 MB and 5,000 rows"),
    sheet_name: str | None = Form(None, description=".xlsx only; default is the first sheet"),
    vendor_id: int | None = Form(None, description="Ignored: the vendor comes from the login"),
):
    """Vendor logins only (admins: 403). Parses and validates the file. Saves nothing."""
    vendor = _target_vendor(db, scope)
    data = file.file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "File is larger than 5 MB")
    try:
        rows = read_file(file.filename, data, sheet_name or None)
    except ImportFileError as exc:
        raise HTTPException(exc.status_code, exc.message) from None

    leads = [(row, flag_template_example(
                  validate_lead(v["name"], v["date"], v["phone"], v["city"], parse_file_date)))
             for row, v in rows]
    existing = crud.existing_keys(
        db, vendor.id, {lead.phone for _, lead in leads if _phone_ok(lead.phone)})
    seen = crud.LeadKeys()
    out, valid, invalid, duplicates = [], 0, 0, 0
    for row, lead in leads:
        # A duplicate: the same phone, and no filled name / date / city differing from this vendor's
        # lead or an earlier row
        key = crud.lead_key(lead)
        duplicate = _phone_ok(lead.phone) and (key in existing or key in seen)
        if lead.ok:
            seen.add(key)
            if duplicate:
                duplicates += 1
            else:
                valid += 1
        else:
            invalid += 1
        out.append({"row": row, "name": lead.name, "date": lead.date_display,
                    "phone": lead.phone, "city": lead.city, "errors": lead.errors,
                    "duplicate": bool(duplicate)})
    return {"rows": out, "valid_count": valid, "invalid_count": invalid,
            "duplicate_count": duplicates}


# ── Leads ──────────────────────────────────────────────────────────────
@router.post("/leads/bulk")
def bulk_create_leads(body: LeadBulkCreate, db: DbSession, scope: VendorLogin):
    """Vendor logins only (admins: 403). Validates every lead again, saves the valid ones and
    skips duplicates."""
    vendor = _target_vendor(db, scope)
    errors, failed, duplicates = [], 0, 0
    seen = crud.LeadKeys()
    valid = []
    for index, item in enumerate(body.leads):
        lead = flag_template_example(
            validate_lead(item.name, item.date, item.phone, item.city, parse_json_date))
        if not lead.ok:
            failed += 1
            errors.extend({"index": index, **e} for e in lead.errors)
        elif crud.lead_key(lead) in seen:
            duplicates += 1
        else:
            seen.add(crud.lead_key(lead))
            valid.append(lead)
    existing = crud.existing_keys(db, vendor.id, {lead.phone for lead in valid})
    new = [lead for lead in valid if crud.lead_key(lead) not in existing]
    saved = crud.insert_new(db, vendor.id, new, scope.user.id) if new else []
    added = len(saved)
    if added:
        lead_activity.log_actions(db, [{"source": "vendor_to_koc", "lead_key": str(row.id), "action": "created",
                                        "field": "name", "old": None, "new": row.name} for row in saved],
                                  actor_user_id=scope.user.id, actor_name=scope.user.name, actor_role=scope.user.role)
    duplicates += len(valid) - added           # in the database before, or saved meanwhile
    if added:
        try:
            notifications.notify_admins_vendor_added(
                db, vendor_id=vendor.id, actor_user_id=scope.user.id, actor_name=scope.user.name, added=added,
            )
        except Exception:
            db.rollback()
            logger.exception("Could not create vendor bulk-add notification")
    return {"added": added, "duplicates_skipped": duplicates, "failed": failed, "errors": errors}


@router.post("/leads", status_code=status.HTTP_201_CREATED,
             responses={201: {"description": LEAD_RESPONSE_DOC}})
def create_lead(body: LeadCreate, db: DbSession, scope: VendorLogin):
    """Vendor logins only (admins: 403)."""
    vendor = _target_vendor(db, scope)
    lead = validate_lead(body.name, body.date, body.phone, body.city, parse_json_date)
    if not lead.ok:
        raise _unprocessable(lead.errors)
    try:
        lead_id = crud.create_lead(db, vendor.id, lead, scope.user.id)
    except crud.DuplicateLead:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This lead already exists for this vendor (the same phone, name, date and city)")
    lead, vendor_name, updater_name, creator_name = crud.get_lead(db, lead_id)
    response = crud.serialize(lead, vendor_name, False, updater_name, creator_name)
    lead_name = lead.name
    lead_activity.log_action(db, source="vendor_to_koc", lead_key=str(lead_id), action="created",
        field="name", old=None, new=lead.name, actor_user_id=scope.user.id,
        actor_name=scope.user.name, actor_role=scope.user.role)
    try:
        notifications.notify_admins_vendor_added(
            db, vendor_id=vendor.id, actor_user_id=scope.user.id, actor_name=scope.user.name,
            added=1, lead_id=lead_id, lead_name=lead_name,
        )
    except Exception:
        db.rollback()
        logger.exception("Could not create vendor lead notification")
    return response


@dataclass(frozen=True)
class LeadQuery:
    filters: crud.LeadFilters
    sort: str
    order: str


def lead_query(
    scope: LeadViewer,
    db: DbSession,
    period: OptionalPeriod,
    search: str | None = Query(None, description="Matches name, phone or city"),
    vendor_id: list[int] | None = Query(None, description="Admins only, repeatable"),
    update_status: list[StatusFilter] | None = Query(None, description="Repeatable; pending = no update yet"),
    lead_date_from: date | None = Query(None, description="The lead's own date from (inclusive)"),
    lead_date_to: date | None = Query(None, description="The lead's own date to (inclusive)"),
    follow_up_from: date | None = Query(None, description="Follow-up date from (inclusive)"),
    follow_up_to: date | None = Query(None, description="Follow-up date to (inclusive)"),
    follow_up_due: bool = Query(False, description="Follow-up date today or earlier, open leads"),
    city: str | None = Query(None, max_length=100, description="City contains (case-insensitive)"),
    source: list[Literal["bulk_upload", "manual"]] | None = Query(None, description="Repeatable"),
    hot: Literal["yes", "no"] | None = Query(None, description="Hot lead filter"),
    quote: str | None = Query(None, description="Quoted amount: has, none, gt:<amount> or lt:<amount>"),
    conversion: str | None = Query(None, description="Conversion amount: has, none, gt:<amount> or lt:<amount>"),
    also_in: Literal["yes", "no"] | None = Query(None, description="The phone also exists in one of the 4 "
                                                 "sources (vendors: their own leads there)"),
    sort: SortField = Query("created_at"),
    order: Literal["asc", "desc"] = Query("desc"),
) -> LeadQuery:
    """Filters, search and sort shared by GET /leads, GET /leads/stats and GET /leads/export.
    date_from / date_to: arrival (created_at), IST days, the other lists' rules (400 when invalid).
    Vendor users are always limited to their own vendor (vendor_id is ignored for them)."""
    for low, high, name in ((lead_date_from, lead_date_to, "lead_date"), (follow_up_from, follow_up_to, "follow_up")):
        if low and high and low > high:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name}_from must be on or before {name}_to")
    created_from, created_to = period.utc_bounds() if period is not None else (None, None)
    filters = crud.LeadFilters(
        vendor_ids=(vendor_id or None) if scope.sees_all else [scope.vendor_id],
        search=search, statuses=update_status, created_from=created_from, created_to=created_to,
        lead_date_from=lead_date_from, lead_date_to=lead_date_to,
        follow_up_from=follow_up_from, follow_up_to=follow_up_to,
        follow_up_due_by=periods.today_ist() if follow_up_due else None,
        city=(city or "").strip() or None, sources=source or None,
        hot=None if hot is None else hot == "yes",
        quote=parse_quote(quote), conversion=parse_quote(conversion, "conversion"),
    )
    if also_in:
        found = {int(k) for k in also_in_service.matching_keys(db, scope, also_in_service.VTK)}
        if also_in == "yes":
            filters.ids_in = found
        else:
            filters.ids_not_in = found
    return LeadQuery(filters=filters, sort=sort, order=order)


LeadQueryDep = Annotated[LeadQuery, Depends(lead_query)]


@router.get("/leads", responses={200: {"description": LEAD_RESPONSE_DOC}})
def list_leads(
    db: DbSession,
    scope: LeadViewer,
    query: LeadQueryDep,
    page: int = Query(1, ge=1),
    page_size: PageSize = 10,
):
    out = crud.list_leads(db, query.filters, query.sort, query.order, page, page_size, admin=scope.sees_all)
    also_in_service.attach(db, scope, also_in_service.VTK, out["items"], key_of=lambda i: i["id"],
                           e164_of={str(i["id"]): also_in_service.vtk_e164(i["phone"]) for i in out["items"]})
    return out


# Registered before /leads/{lead_id}, so "export" and "template" aren't read as lead ids.
def _download(content: bytes, file_format: str, filename: str) -> Response:
    media_type = lead_export.XLSX_MEDIA_TYPE if file_format == "xlsx" else lead_export.CSV_MEDIA_TYPE
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}.{file_format}"',
        "Cache-Control": "no-store",          # contains personal data
    })


@router.get("/leads/export", response_class=Response, responses={200: {
    "description": "The file: Sl. No, Vendor (admins only), Date, Name, Phone, City, Update, "
                   "Additional comments, Follow-up date, Quoted amount, Conversion amount, Updated by, "
                   "Updated at.",
    "content": {lead_export.XLSX_MEDIA_TYPE: {}, "text/csv": {}}}})
def export_leads(
    db: DbSession,
    scope: LeadViewer,
    query: LeadQueryDep,
    format: Literal["xlsx", "csv"] = Query("xlsx"),
):
    """All leads matching the same filters, search and sort as GET /leads (no paging)."""
    total = crud.count_leads(db, query.filters)
    if total > lead_export.EXPORT_MAX_ROWS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"The export has {total:,} leads; the limit is "
                            f"{lead_export.EXPORT_MAX_ROWS:,}. Narrow the filters, "
                            "for example by date range or vendor.")
    rows = crud.export_rows(db, query.filters, query.sort, query.order, admin=scope.sees_all)
    columns, table = lead_export.lead_table(rows, admin=scope.sees_all)
    content = (lead_export.to_xlsx(columns, table) if format == "xlsx"
               else lead_export.to_csv(columns, table))
    if scope.sees_all:
        prefix = "vendor_to_koc"
    else:
        prefix = lead_export.filename_slug(db.get(Vendor, scope.vendor_id).name)
    return _download(content, format, f"{prefix}_leads_{lead_export.today_ist().isoformat()}")


@router.get("/leads/stats")
def lead_stats(db: DbSession, scope: LeadViewer, query: LeadQueryDep):
    """Header numbers over exactly the leads GET /leads lists for the same filters: total and
    by_source, by_status (incl. pending), follow_ups_due and kpis; admins also by_vendor."""
    return crud.lead_stats(db, query.filters, admin=scope.sees_all, today=periods.today_ist())


@router.get("/leads/template", response_class=Response, responses={200: {
    "description": "Empty upload template: Name, Date, Phone, City and one example row.",
    "content": {lead_export.XLSX_MEDIA_TYPE: {}, "text/csv": {}}}})
def download_template(_user: NotDhruvaUser, format: Literal["xlsx", "csv"] = Query("xlsx")):
    """Any logged-in user except Dhruva logins. Delete the example row before uploading: the
    preview flags it."""
    content = lead_export.template_xlsx() if format == "xlsx" else lead_export.template_csv()
    return _download(content, format, "vendor_to_koc_template")


@router.get("/leads/{lead_id}", responses={200: {"description": LEAD_RESPONSE_DOC}})
def get_lead(lead_id: int, db: DbSession, scope: LeadViewer):
    found = crud.get_lead(db, lead_id, vendor_id=None if scope.sees_all else scope.vendor_id)
    if found is None:       # also for another vendor's lead, so its existence isn't revealed
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    lead, vendor_name, updater_name, creator_name = found
    return crud.serialize(lead, vendor_name, scope.sees_all, updater_name, creator_name)


@router.post("/leads/{lead_id}/hot")
def mark_vendor_lead_hot(lead_id: int, db: DbSession, scope: LeadViewer):
    found = crud.get_lead(db, lead_id, vendor_id=None if scope.sees_all else scope.vendor_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    was_hot = found[0].is_hot
    crud.set_hot(db, found[0], scope.user.id, True)
    if not was_hot:
        lead_activity.log_action(db, source="vendor_to_koc", lead_key=str(lead_id), action="hot_on", field="is_hot",
            old=False, new=True, actor_user_id=scope.user.id, actor_name=scope.user.name, actor_role=scope.user.role)
    marked_at = found[0].hot_marked_at
    marked_at = marked_at if marked_at.tzinfo else marked_at.replace(tzinfo=timezone.utc)
    return {"id": lead_id, "is_hot": True, "hot_marked_by": scope.user.id,
            "hot_marked_at": marked_at.isoformat() if marked_at else None}


@router.delete("/leads/{lead_id}/hot")
def unmark_vendor_lead_hot(lead_id: int, db: DbSession, scope: LeadViewer):
    found = crud.get_lead(db, lead_id, vendor_id=None if scope.sees_all else scope.vendor_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    was_hot = found[0].is_hot
    crud.set_hot(db, found[0], scope.user.id, False)
    if was_hot:
        lead_activity.log_action(db, source="vendor_to_koc", lead_key=str(lead_id), action="hot_off", field="is_hot",
            old=True, new=False, actor_user_id=scope.user.id, actor_name=scope.user.name, actor_role=scope.user.role)
    return {"id": lead_id, "is_hot": False, "hot_marked_by": None, "hot_marked_at": None}


@router.patch("/leads/{lead_id}")
def update_lead(lead_id: int, body: LeadUpdate, db: DbSession, admin: AdminUser):
    """Admins only. Only the fields sent are changed; null clears a field
    (update_status: null sets the lead back to pending)."""
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise _unprocessable([{"type": "missing", "loc": ["body"],
                               "msg": "Send at least one of update_status, additional_comments, "
                                      "follow_up_date, quoted_amount, conversion_amount"}])
    found = crud.get_lead(db, lead_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    before = {k: getattr(found[0], k) for k in changes}
    crud.update_lead(db, found[0], changes, admin.id)
    lead_activity.log_changes(db, source="vendor_to_koc", lead_key=str(lead_id),
        changes={k: (before[k], getattr(found[0], k)) for k in changes}, actor_user_id=admin.id,
        actor_name=admin.name, actor_role=admin.role)
    lead, vendor_name, updater_name, creator_name = crud.get_lead(db, lead_id)
    return crud.serialize(lead, vendor_name, True, updater_name, creator_name)


@router.get("/leads/{lead_id}/activity")
def get_lead_activity(lead_id: int, db: DbSession, scope: LeadViewer,
                      page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    found = crud.get_lead(db, lead_id, vendor_id=None if scope.sees_all else scope.vendor_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return lead_activity.list_activity(db, source="vendor_to_koc", lead_key=str(lead_id), page=page,
        page_size=page_size, allowed_fields=None if scope.sees_all else lead_activity.VTK_VISIBLE_FIELDS)


# ── Vendors (admin only) ───────────────────────────────────────────────
@router.get("/vendors", response_model=list[VendorRead])
def list_vendors(db: DbSession, _admin: ReadAdmin):
    """All vendors (active and inactive) with their lead counts, sorted by name."""
    return crud.list_vendors(db)


@router.post("/vendors", response_model=VendorRead, status_code=status.HTTP_201_CREATED)
def create_vendor(body: VendorCreate, db: DbSession, _admin: AdminUser):
    try:
        vendor = crud.create_vendor(db, body.name)
    except crud.DuplicateVendorName:
        raise HTTPException(status.HTTP_409_CONFLICT, "Vendor name already exists")
    return crud.vendor_read(db, vendor)


@router.patch("/vendors/{vendor_id}", response_model=VendorRead)
def update_vendor(vendor_id: int, body: VendorUpdate, db: DbSession, _admin: AdminUser):
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")
    try:
        vendor = crud.update_vendor(db, vendor, body.name, body.is_active)
    except crud.DuplicateVendorName:
        raise HTTPException(status.HTTP_409_CONFLICT, "Vendor name already exists")
    return crud.vendor_read(db, vendor)


@router.put("/vendors/{vendor_id}/users/{user_id}", response_model=VendorUserLink)
def link_vendor_user(vendor_id: int, user_id: int, db: DbSession, _admin: AdminUser):
    """Links an existing vendor login to a vendor (or moves it to another vendor)."""
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.role != Role.VENDOR.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "User is not a vendor user")
    crud.link_user(db, user_id, vendor_id)
    return VendorUserLink(user_id=user_id, vendor_id=vendor_id, vendor_name=vendor.name)


@router.delete("/vendors/{vendor_id}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT,
               response_class=Response)
def unlink_vendor_user(vendor_id: int, user_id: int, db: DbSession, _admin: AdminUser):
    """Unlinks a vendor login from this vendor. The login stays (and can still log in) but gets
    403 on lead endpoints until linked again. 404 if it isn't linked to this vendor."""
    if not crud.unlink_user(db, user_id, vendor_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This login isn't linked to this vendor")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
