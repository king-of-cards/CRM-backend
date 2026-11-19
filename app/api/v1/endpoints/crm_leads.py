"""Leads added or imported in the CRM, for all four sources (admin only).

  GET   /crm-leads/{source}/fields              form fields (and branches for spin)
  GET   /crm-leads/{source}/template            Excel (default) or CSV template
  POST  /crm-leads/{source}/import/preview      upload .xlsx/.csv -> checked rows; saves nothing
  POST  /crm-leads/{source}/import              save the rows that pass; duplicates are skipped
  GET   /crm-leads/{source}/export              the source's list as .xlsx (default) or .csv
  POST  /crm-leads/{source}                     add one lead (409 on a duplicate phone unless allow_duplicate)
  GET   /crm-leads/{source}/{lead_key}          one added/imported lead (for the edit form)
  PATCH /crm-leads/{source}/{lead_key}          edit it (only the fields sent)
  POST  /crm-leads/{source}/{lead_key}/archive  hide it from lists, counts and exports
  POST  /crm-leads/{source}/{lead_key}/unarchive

source: spin | whatsapp | wedding | wedding_planner. Leads are stored with the sheet rows
(origin added/imported, which the sheet sync never touches) or, for spin, in crm_spin_leads.
CRM fields (vendor, update, quote, conversion amount, notes) are not here: they live in crm_lead_meta
and are edited with PUT /leads/{source}/{lead_key}/crm, using the lead_key returned here.
"""
from typing import Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import select

from app.api.deps import AdminUser, DbSession, LeadViewer, ReadAdmin
from app.api.v1.endpoints import spin_leads as spin_ep
from app.api.v1.endpoints import wedding_leads as wedding_ep
from app.api.v1.endpoints import wedding_plans as planner_ep
from app.api.v1.endpoints import whatsapp_leads as whatsapp_ep
from app.models.branch import Branch
from app.models.lead_origin import ORIGIN_ADDED, ORIGIN_IMPORTED
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.services.crm_leads import export as export_cols
from app.services.crm_leads import service, tabular
from app.services.crm_leads.fields import (
    FIELDS, SOURCE_LABELS, UPLOAD_CRM_KEYS, clean_row, from_model, header_map, norm_header, read_only_headers,
)
from app.services.branches import branches_by_id
from app.services.lead_crm import active_vendors
from app.services import hot_leads
from app.services import lead_activity
from app.services.stores import branch_names

router = APIRouter(prefix="/crm-leads", tags=["crm leads (add, import, export)"])

Source = Literal["spin", "whatsapp", "wedding", "wedding_planner"]
Format = Literal["xlsx", "csv"]


def _workflow_down() -> HTTPException:
    return HTTPException(503, "Duplicate check against the workflow leads isn't possible right now. Try again shortly.")


def _file_response(content: bytes, media_type: str, name: str) -> Response:
    return Response(content, media_type=media_type, headers={"Content-Disposition": tabular.content_disposition(name)})


# ── Fields and template ────────────────────────────────────────────────
def _branch_names(db) -> list[str]:
    """Branch names uploads accept: the Branches page's, plus the built-in stores."""
    return branch_names([{"id": b.id, "name": b.name} for b in db.exec(select(Branch))])


@router.get("/{source}/fields")
def fields(source: Source, _: ReadAdmin, db: DbSession):
    answer: dict[str, Any] = {
        "source": source,
        "label": SOURCE_LABELS[source],
        "fields": [f.as_dict() for f in FIELDS[source]],
    }
    if source == "spin":
        answer["branches"] = [{"id": b.id, "name": b.name, "city": b.city}
                              for b in db.exec(select(Branch).order_by(Branch.name))]
    return answer


@router.get("/{source}/template")
def template(source: Source, _: ReadAdmin, db: DbSession, format: Format = "xlsx"):
    content, media_type, ext = tabular.template(
        source, format, _branch_names(db) if source == "spin" else [], [v.name for v in active_vendors(db)]
    )
    return _file_response(content, media_type, tabular.filename(f"{source}-leads-template", ext))


# ── Import ─────────────────────────────────────────────────────────────
@router.post("/{source}/import/preview")
async def import_preview(
    source: Source,
    _: AdminUser,
    db: DbSession,
    file: UploadFile = File(...),
    sheet_name: str | None = Form(
        None, max_length=31, description='Excel sheet to read (default "Leads", else the first sheet). Ignored for CSV.'
    ),
):
    """Check an uploaded file. Nothing is saved: send the rows' "values" to POST /import."""
    content = await file.read(tabular.MAX_UPLOAD_BYTES + 1)
    try:
        headers, data = tabular.read_upload(file.filename or "", content, sheet_name)
    except tabular.UploadError as exc:
        raise HTTPException(400, str(exc))

    names = header_map(source, upload=True)          # + Update, Quoted amount, Conversion amount
    read_only = read_only_headers(source)
    mapping: dict[int, str] = {}
    matched: dict[str, str] = {}
    ignored: list[str] = []
    read_only_found: list[str] = []
    for i, header in enumerate(headers):
        normalized = norm_header(header)
        key = names.get(normalized)
        if key and key not in mapping.values():
            mapping[i] = key
            matched[header] = key
        elif normalized in read_only:
            read_only_found.append(header)       # e.g. Quoted date: shown in exports, not read back
        elif header:
            ignored.append(header)
    if "phone" not in mapping.values():
        phone_label = next(f.label for f in FIELDS[source] if f.key == "phone")
        raise HTTPException(400, f"The file has no {phone_label} (phone) column. Use the template's column names.")
    if not data:
        raise HTTPException(400, "The file has no leads (only a header row).")

    rows = [(number, {key: values[i] for i, key in mapping.items()}) for number, values in data]
    try:
        results, counts, _ = service.check_rows(db, source, rows)
    except service.WorkflowUnavailable:
        raise _workflow_down()
    return {"source": source, "columns": matched, "ignored_columns": ignored, "read_only_columns": read_only_found,
            "rows": results, "counts": counts}


class ImportRow(BaseModel):
    row: int = Field(ge=1, description="Row number in the file, echoed back in the results")
    values: dict[str, Any]


class ImportBody(BaseModel):
    rows: list[ImportRow] = Field(max_length=tabular.MAX_UPLOAD_ROWS)


@router.post("/{source}/import")
def import_rows(source: Source, body: ImportBody, user: AdminUser, db: DbSession):
    """Save every row that passes (origin=imported). Rows with errors and duplicates are
    skipped and reported; each row is checked again here, whatever the preview said."""
    if not body.rows:
        raise HTTPException(400, "No rows to import.")
    try:
        results, counts, cleaned = service.check_rows(db, source, [(r.row, r.values) for r in body.rows])
    except service.WorkflowUnavailable:
        raise _workflow_down()
    imported = 0
    for result, c in zip(results, cleaned):
        if result["status"] != "ok":
            continue
        lead = service.create(db, source, c, ORIGIN_IMPORTED, user.id)
        result["status"] = "imported"
        result["lead_key"] = service.lead_key_of(source, lead)
        service.save_assignment(db, source, result["lead_key"], c.assignment, user.id, empty_means_unassigned=True,
                                crm={k: c.values.get(k) for k in UPLOAD_CRM_KEYS})
        imported += 1
    db.commit()
    for result in results:
        if result.get("status") == "imported":
            lead_activity.log_action(db, source=source, lead_key=result["lead_key"], action="created",
                field="lead", old=None, new="created", actor_user_id=user.id,
                actor_name=user.name, actor_role=user.role)
    if imported:
        service.clear_caches()
    counts["imported"] = imported
    counts["skipped"] = counts["total"] - imported
    return {"source": source, "rows": results, "counts": counts}


# ── Export (one route per source: each has its own list filters) ───────
def _check_cap(total: int) -> None:
    if total > tabular.EXPORT_MAX_ROWS:
        raise HTTPException(
            400,
            f"This export has {total:,} leads; the limit is {tabular.EXPORT_MAX_ROWS:,}. Narrow the filters and try again.",
        )


def _export(source: str, format: str, columns, items, viewer) -> Response:
    if not viewer.sees_all:               # vendor logins: no Assigned to column
        columns = [c for c in columns if c[0] != export_cols.ASSIGNED_HEADER]
    headers, rows, kinds = export_cols.build(columns, items)
    content, media_type, ext = tabular.write_table(format, SOURCE_LABELS[source], headers, rows, kinds)
    return _file_response(content, media_type, tabular.filename(f"{source}-leads", ext))


def _sheet_export(db, source: str, model, module, filters, sort: str, order: str, format: str, viewer) -> Response:
    _check_cap(db.exec(select(func.count()).select_from(model).where(*filters)).one())
    rows = db.exec(select(model).where(*filters).order_by(*module.ordering(sort, order))).all()
    items = module.attach_crm(db, [module._out(r) for r in rows])
    return _export(source, format, [*export_cols.SHEET_COLUMNS[source], export_cols.HOT_COLUMN], items, viewer)


@router.get("/whatsapp/export")
def export_whatsapp(
    viewer: LeadViewer,
    db: DbSession,
    f: whatsapp_ep.Filters,
    format: Format = "xlsx",
    sort: Literal["submitted_at", "event_date", "country", "phone", "first_seen_at"] = "submitted_at",
    order: Literal["asc", "desc"] = "desc",
):
    """Exactly the requesting user's International list: same filters (GET /whatsapp-leads) and sort."""
    return _sheet_export(db, "whatsapp", WhatsAppLead, whatsapp_ep, whatsapp_ep.build_filters(db, viewer, f),
                         sort, order, format, viewer)


@router.get("/wedding/export")
def export_wedding(
    viewer: LeadViewer,
    db: DbSession,
    f: wedding_ep.Filters,
    format: Format = "xlsx",
    sort: Literal["submitted_at", "event_date", "budget_amount", "name", "city"] = "submitted_at",
    order: Literal["asc", "desc"] = "desc",
):
    """Exactly the requesting user's Wedding list: same filters (GET /wedding-leads) and sort."""
    return _sheet_export(db, "wedding", WeddingLead, wedding_ep, wedding_ep.build_filters(db, viewer, f),
                         sort, order, format, viewer)


@router.get("/wedding_planner/export")
def export_wedding_planner(
    viewer: LeadViewer,
    db: DbSession,
    f: planner_ep.Filters,
    format: Format = "xlsx",
    sort: Literal["updated", "wedding_date", "total_budget", "total_guests", "plan_estimate", "name"] = "updated",
    order: Literal["asc", "desc"] = "desc",
):
    """Exactly the requesting user's Wedding planner list: same filters (GET /wedding-plans) and sort."""
    return _sheet_export(db, "wedding_planner", WeddingPlan, planner_ep, planner_ep.build_filters(db, viewer, f),
                         sort, order, format, viewer)


@router.get("/spin/export")
def export_spin(
    viewer: LeadViewer,
    db: DbSession,
    f: spin_ep.Filters,
    format: Format = "xlsx",
    sort: str | None = Query(None),
    order: Literal["asc", "desc"] = Query("asc"),
):
    """Exactly the requesting user's spin list: same filters (GET /spin-leads) and sort, workflow and CRM leads."""
    filters = spin_ep.filters_for(db, viewer, f)
    total = spin_ep.merged_page(db, 0, 0, f.common.search, sort, order, f.branch, filters, use_cache=False)["total"]
    _check_cap(total)
    data = spin_ep.merged_page(db, 0, total, f.common.search, sort, order, f.branch, filters, use_cache=False)
    branches = list(branches_by_id(db).values())       # + the built-in stores, as the screen shows them
    return _export("spin", format, [*export_cols.spin_columns(data["columns"], branches), export_cols.HOT_COLUMN],
                    data["items"], viewer)


# ── Single lead ────────────────────────────────────────────────────────
class AddBody(BaseModel):
    values: dict[str, Any] = Field(description="Field key -> value (see GET /crm-leads/{source}/fields)")
    allow_duplicate: bool = Field(False, description="Save even if the phone already exists in this source")


def _duplicate_409(duplicate: dict, others: list[str]) -> HTTPException:
    return HTTPException(409, {
        "message": "A lead with this phone number already exists.",
        "duplicate": duplicate,
        "warnings": [{"type": "other_source", "sources": others}] if others else [],
    })


def _lead_out(db, source: str, lead):
    item = service.out(source, lead)
    item["is_hot"] = hot_leads.is_hot(db, source, item["lead_key"])
    return item


@router.post("/{source}", status_code=201)
def add_lead(source: Source, body: AddBody, user: AdminUser, db: DbSession):
    if source == "spin":
        raise HTTPException(405, "Spin leads can only be added by bulk upload", headers={"Allow": "GET"})
    c = clean_row(source, body.values, service.context(db))
    if not c.ok:
        raise HTTPException(422, {"message": "Please fix the highlighted fields.", "errors": c.errors})
    try:
        duplicate, others = service.classify(source, c.phone.e164, service.lookup(db, {c.phone.e164}))
    except service.WorkflowUnavailable:
        raise _workflow_down()
    if duplicate and not body.allow_duplicate:
        raise _duplicate_409(duplicate, others)
    lead = service.create(db, source, c, ORIGIN_ADDED, user.id)
    service.save_assignment(db, source, service.lead_key_of(source, lead), c.assignment, user.id,
                            empty_means_unassigned=False)
    db.commit()
    db.refresh(lead)
    lead_activity.log_action(db, source=source, lead_key=service.lead_key_of(source, lead), action="created",
        field="lead", old=None, new="created", actor_user_id=user.id, actor_name=user.name, actor_role=user.role)
    service.clear_caches()
    warnings = [{"type": "other_source", "sources": others}] if others else []
    if duplicate:
        warnings.append({"type": "duplicate_allowed", "duplicate": duplicate})
    return {"lead": _lead_out(db, source, lead), "warnings": warnings}


def _crm_lead_or_error(db, source: str, lead_key: str):
    lead = service.find_crm_lead(db, source, lead_key)
    if lead is not None:
        return lead
    if service.is_sheet_lead(db, source, lead_key):
        where = "the workflow (spin wheel)" if source == "spin" else "the Google Sheet"
        raise HTTPException(409, f"This lead comes from {where} and can't be changed here.")
    raise HTTPException(404, "Lead not found")


@router.get("/{source}/{lead_key}")
def get_lead(source: Source, lead_key: str, _: ReadAdmin, db: DbSession):
    return _lead_out(db, source, _crm_lead_or_error(db, source, lead_key))


class EditBody(BaseModel):
    values: dict[str, Any] = Field(description="Only the fields to change; send null to clear one")


@router.patch("/{source}/{lead_key}")
def edit_lead(source: Source, lead_key: str, body: EditBody, user: AdminUser, db: DbSession):
    lead = _crm_lead_or_error(db, source, lead_key)
    raw = {k: v for k, v in body.values.items() if k in {f.key for f in FIELDS[source]} and k != "assigned_to"}
    if not raw:
        raise HTTPException(422, {"message": "Nothing to change.", "errors": []})
    if source == "whatsapp" and "country" in raw and "phone" not in raw:
        raw["phone"] = from_model(source, lead)["phone"]     # the country decides how the phone is read
    c = clean_row(source, raw, service.context(db), partial=True)
    if not c.ok:
        raise HTTPException(422, {"message": "Please fix the highlighted fields.", "errors": c.errors})
    if "phone" in raw and c.phone.e164 != lead.phone_e164:
        try:
            duplicate, others = service.classify(source, c.phone.e164, service.lookup(db, {c.phone.e164}),
                                                 exclude_key=lead_key)
        except service.WorkflowUnavailable:
            raise _workflow_down()
        if duplicate:
            raise _duplicate_409(duplicate, others)
    before = service.out(source, lead)
    service.update(db, source, lead, c)
    db.commit()
    db.refresh(lead)
    after = service.out(source, lead)
    lead_activity.log_changes(db, source=source, lead_key=lead_key,
        changes={field: (before.get(field), after.get(field)) for field in raw}, actor_user_id=user.id,
        actor_name=user.name, actor_role=user.role)
    service.clear_caches()
    return {"lead": _lead_out(db, source, lead)}
@router.post("/{source}/{lead_key}/archive")
def archive_lead(source: Source, lead_key: str, user: AdminUser, db: DbSession):
    """Hidden from lists, counts and exports. The phone still counts in duplicate checks and
    the lead's CRM details (crm_lead_meta) are kept."""
    lead = _crm_lead_or_error(db, source, lead_key)
    was_archived = bool(lead.is_deleted)
    service.set_archived(db, lead, True)
    db.commit()
    if not was_archived:
        lead_activity.log_action(db, source=source, lead_key=lead_key, action="archived", field="archived",
            old=False, new=True, actor_user_id=user.id, actor_name=user.name, actor_role=user.role)
    service.clear_caches()
    return {"lead": _lead_out(db, source, lead)}


@router.post("/{source}/{lead_key}/unarchive")
def unarchive_lead(source: Source, lead_key: str, user: AdminUser, db: DbSession):
    lead = _crm_lead_or_error(db, source, lead_key)
    was_archived = bool(lead.is_deleted)
    service.set_archived(db, lead, False)
    db.commit()
    if was_archived:
        lead_activity.log_action(db, source=source, lead_key=lead_key, action="unarchived", field="archived",
            old=True, new=False, actor_user_id=user.id, actor_name=user.name, actor_role=user.role)
    service.clear_caches()
    return {"lead": _lead_out(db, source, lead)}
