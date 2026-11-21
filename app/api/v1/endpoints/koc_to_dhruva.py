"""KOC -> Dhruva (source key koc_to_dhruva): the spin leads KOC shares with Dhruva by rule.

Rule and storage: app/services/koc_dhruva.py. Paths (prefix /api/v1/koc-to-dhruva):
  GET  leads, leads/stats, leads/filter-options, leads/export, leads/{lead_key}
  PUT  leads/{lead_key}/crm          Update, comments, quoted amount / date, conversion amount, follow-up
  POST / DELETE leads/{lead_key}/hot
  GET  leads/{lead_key}/activity
Who: admins, view-only and Dhruva logins read; admins and Dhruva logins edit (view-only logins get
the central 403 on writes); vendor logins: 403. No add or upload: rows come only from the rule.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from app.api.deps import AdminUser, DbSession, DhruvaSectionUser, PageSize, ReadAdmin, common_filter_params
from app.api.v1.endpoints import spin_leads as sl
from app.core import response_cache
from app.models.lead_meta import LeadMeta
from app.models.vendor_to_koc import STATUS_LABELS
from app.services import (
    access, also_in, hot_leads, kpis, koc_dhruva, lead_activity, lead_crm, lead_filters, periods, phones,
)
from app.services import vendor_lead_export as files
from app.services.access import Viewer

router = APIRouter(prefix="/koc-to-dhruva", tags=["koc to dhruva"])
SOURCE = koc_dhruva.SOURCE
MAX_AMOUNT = 100_000_000


def _viewer(user) -> Viewer:
    """Admins and view-only logins read like admins; a Dhruva login sees every lead of this section
    (access.visible_keys) and no other section's leads in also_in."""
    return Viewer(user, None)


# ── Filters (the shared layer + this section's own) ────────────────────
@dataclass
class KocFilters(sl.SpinFilters):
    occasion: str | None = None
    quantity_min: int | None = None
    quantity_max: int | None = None
    reason: str | None = None


def koc_filters(
    common: Annotated[lead_filters.CommonFilters,
                      Depends(common_filter_params(lead_filters.SPIN_ORIGINS, "KOC → Dhruva leads"))],
    branch: str | None = Query(None, description="Only leads from this branch id"),
    pincode: str | None = Query(None, max_length=10, description="Pincode starts with"),
    budget: str | None = Query(None, max_length=50, description="Exact estimated budget per card, e.g. 70-100"),
    occasion: str | None = Query(None, max_length=100, description="Occasion contains (any case)"),
    quantity_min: int | None = Query(None, ge=0, description="Quantity required at least"),
    quantity_max: int | None = Query(None, ge=0, description="Quantity required at most"),
    reason: Literal["budget", "quantity"] | None = Query(None, description="Why the lead is here"),
) -> KocFilters:
    if common.vendor_ids or common.unassigned:
        raise HTTPException(400, "KOC → Dhruva leads are never assigned to a vendor: vendor_id and unassigned "
                                 "don't apply")
    if quantity_min is not None and quantity_max is not None and quantity_min > quantity_max:
        raise HTTPException(400, "quantity_min must be at most quantity_max")
    spin = sl.spin_filters(common, branch, pincode, budget, None)      # no Other wedding services filter here
    return KocFilters(spin.common, spin.branch, spin.pincode, spin.budget,
                      occasion=(occasion or "").strip() or None, quantity_min=quantity_min,
                      quantity_max=quantity_max, reason=reason)


Filters = Annotated[KocFilters, Depends(koc_filters)]


def _filters_for(db, user, f: KocFilters) -> dict:
    flt = sl.filters_for(db, _viewer(user), f, SOURCE)
    for name in ("occasion", "quantity_min", "quantity_max", "reason"):
        if getattr(f, name) is not None:
            flt[name] = getattr(f, name)
    return flt


# ── Items ──────────────────────────────────────────────────────────────
def _enrich(item: dict) -> dict:
    row = koc_dhruva.row_of(item)
    item.update({
        "lead_key": row["key"], "potential_value": row["potential_value"],
        "occasion": row["occasion"], "event_date": row["event_date"], "pincode": row["pincode"],
        "estimated_budget_per_card": row["budget"], "quantity_required": row["quantity"],
        "received": row["created"].isoformat() if row["created"] else None,
    })
    return item


def _page(db, user, f: KocFilters, start: int, length: int, sort, order, flt: dict | None = None) -> dict:
    flt = _filters_for(db, user, f) if flt is None else flt
    data = sl.merged_page(db, start, length, f.common.search, sort, order, f.branch, flt, use_cache=False,
                          flow=SOURCE)
    for item in data["items"]:
        _enrich(item)
    return data


def _attach_also_in(db, user, items: list[dict]) -> None:
    # Uploaded rows (kd-<id>): their stored phones; workflow rows: the phone in the item
    e164_of = also_in.page_phones(db, SOURCE, [i["lead_key"] for i in items if i["lead_key"].startswith("kd-")])
    for item in items:
        if item["lead_key"] not in e164_of and item.get("phone"):
            e164 = phones.normalize(str(item["phone"]), lenient=True).e164
            if e164:
                e164_of[item["lead_key"]] = e164
    also_in.attach(db, _viewer(user), SOURCE, items, key_of=lambda i: i["lead_key"], e164_of=e164_of)


@router.get("/leads")
def list_leads(
    db: DbSession,
    user: DhruvaSectionUser,
    f: Filters,
    page: int = Query(1, ge=1),
    page_size: PageSize = 10,
    sort: str | None = Query(None, description="Column name, additional_details.<key>, or potential_value "
                                                "(budget lower bound x quantity; empty values last)"),
    order: Literal["asc", "desc"] = Query("asc"),
):
    """KOC -> Dhruva leads (workflow rows of any date; never CRM-uploaded spin leads) with this
    section's CRM details (crm), potential_value and also_in."""
    data = _page(db, user, f, (page - 1) * page_size, page_size, sort, order)
    _attach_also_in(db, user, data["items"])
    return {**data, "page": page, "page_size": page_size}


@router.get("/leads/stats")
def lead_stats(db: DbSession, user: DhruvaSectionUser, f: Filters):
    """{total, kpis} over exactly the leads the list shows for the same filters."""
    from app.services import crm_spin_leads

    flt = _filters_for(db, user, f)
    added = crm_spin_leads.matching(db, f.common.search, None, "asc", f.branch, flt, SOURCE)
    workflow = sl.list_spin_leads(None, 1, 1, f.common.search, None, "asc", f.branch, flt or None, keys_only=True,
                                  flow=SOURCE)
    keys = [lead.lead_key for lead in added] + list(workflow["keys"])
    return {"total": len(keys), "kpis": kpis.for_keys(db, SOURCE, keys)}


@router.get("/leads/filter-options")
def filter_options(db: DbSession, user: DhruvaSectionUser):
    """Dropdown values with counts over every lead of the section: budgets, pincodes, branches,
    origins, updates (from GET /spin-leads/filter-options' builder), plus occasions and the quantity range."""
    out = sl.filter_options(_viewer(user), db, SOURCE)
    out.pop("vendors", None)                                  # never assigned
    rows = koc_dhruva.rows(db) or []
    occasions: dict[str, dict] = {}
    for r in rows:
        if r["occasion"]:
            entry = occasions.setdefault(str(r["occasion"]).strip().lower(), {"value": str(r["occasion"]).strip(),
                                                                            "count": 0})
            entry["count"] += 1
    out["occasions"] = sorted(occasions.values(), key=lambda o: (-o["count"], o["value"].lower()))
    quantities = [r["quantity"] for r in rows if r["quantity"] is not None]
    out["quantity_range"] = {"min": min(quantities), "max": max(quantities)} if quantities else None
    return out


EXPORT_COLUMNS = [
    files.Column("Sl. No", "int", 8), files.Column("Name", "text", 28), files.Column("Phone", "phone", 16),
    files.Column("Email", "text", 28), files.Column("Occasion", "text", 20), files.Column("Event date", "text", 14),
    files.Column("Branch", "text", 24), files.Column("Branch code", "text", 22),
    files.Column("Estimated budget per card", "text", 18), files.Column("Quantity required", "int", 12),
    files.Column("Received", "datetime", 18), files.Column("Update", "text", 28),
    files.Column("Additional comments", "text", 40), files.Column("Quoted amount", "money", 16),
    files.Column("Quoted date", "date", 14), files.Column("Conversion amount", "money", 18),
    files.Column("Follow-up date", "date", 14), files.Column("Hot", "yesno", 8),
]


def _date(value) -> date | None:
    return date.fromisoformat(value) if value else None


def _store(item: dict, branches: list[dict]) -> dict:
    """The lead's branch as the spin export resolves it: the Branches page, then the built-in stores."""
    from app.services.stores import store_for

    return store_for((item.get("branch") or {}).get("id"), branches) or {}


def _branch_code(store: dict) -> str | None:
    """Code and city, as the spin export's Branch code (JNG-004 · Bangalore)."""
    return " · ".join(p for p in (store.get("code"), store.get("city")) if p) or None


@router.get("/leads/export", response_class=Response, responses={200: {
    "description": "The list as a file: " + ", ".join(c.heading for c in EXPORT_COLUMNS) + ".",
    "content": {files.XLSX_MEDIA_TYPE: {}, "text/csv": {}}}})
def export_leads(db: DbSession, user: DhruvaSectionUser, f: Filters, format: Literal["xlsx", "csv"] = Query("xlsx"),
                 sort: str | None = Query(None), order: Literal["asc", "desc"] = Query("asc")):
    """Every lead matching the same filters, in list order (50,000 at most)."""
    flt = _filters_for(db, user, f)
    total = _page(db, user, f, 0, 0, sort, order, flt)["total"]
    if total > files.EXPORT_MAX_ROWS:
        raise HTTPException(400, f"The export has {total:,} leads; the limit is {files.EXPORT_MAX_ROWS:,}. "
                                 "Narrow the filters.")
    items = _page(db, user, f, 0, total, sort, order, flt)["items"]
    from app.services.branches import branches_by_id

    branches = list(branches_by_id(db).values())       # + the built-in stores, as the spin export
    table = []
    for i, item in enumerate(items, start=1):
        crm = item.get("crm") or {}
        quantity = item["quantity_required"]
        store = _store(item, branches)
        table.append([
            i, koc_dhruva.pick(item, "name"), koc_dhruva.pick(item, "phone"), koc_dhruva.pick(item, "email"),
            item["occasion"], item["event_date"], store.get("name"), _branch_code(store),
            item["estimated_budget_per_card"], int(quantity) if quantity is not None else None,
            files.to_ist(datetime.fromisoformat(item["received"]).replace(tzinfo=timezone.utc))
            if item["received"] else None,
            STATUS_LABELS.get(crm.get("update_status"), crm.get("update_status")), crm.get("notes"),
            crm.get("quoted_amount"), _date(crm.get("quoted_date")), crm.get("conversion_amount"),
            _date(crm.get("follow_up_date")), bool(item.get("is_hot")),
        ])
    content = files.to_xlsx(EXPORT_COLUMNS, table) if format == "xlsx" else files.to_csv(EXPORT_COLUMNS, table)
    media = files.XLSX_MEDIA_TYPE if format == "xlsx" else files.CSV_MEDIA_TYPE
    return Response(content, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="koc_to_dhruva_leads_{periods.today_ist().isoformat()}.{format}"',
        "Cache-Control": "no-store"})


# ── Upload (admins): template, preview, save ──────────────────────────
@router.get("/leads/template")
def upload_template(db: DbSession, _: ReadAdmin, format: Literal["xlsx", "csv"] = Query("xlsx")):
    """The export's columns (without Sl. No) with one example row; Branch code, Quoted date and Hot
    are read-only. Only Phone is required."""
    from app.api.v1.endpoints.crm_leads import _branch_names, _file_response
    from app.services.crm_leads import tabular

    content, media_type, ext = tabular.template(SOURCE, format, _branch_names(db), [])
    return _file_response(content, media_type, tabular.filename("koc-to-dhruva-leads-template", ext))


@router.post("/leads/import/preview")
async def upload_preview(
    db: DbSession,
    _: AdminUser,
    file: UploadFile = File(...),
    sheet_name: str | None = Form(None, max_length=31,
                                  description='Excel sheet to read (default "Leads", else the first). Ignored for CSV.'),
):
    """Check an uploaded file (the history before the cut-off). Nothing is saved: send the rows'
    "values" (fixed where needed) to POST /leads/import. Duplicates: every field the same as an
    uploaded row (archived too), a live workflow row of the section, or an earlier row of the file."""
    from app.api.v1.endpoints.crm_leads import preview_file
    from app.services.crm_leads import tabular

    content = await file.read(tabular.MAX_UPLOAD_BYTES + 1)
    return preview_file(db, SOURCE, file.filename or "", content, sheet_name)


class KocImportRow(BaseModel):
    row: int = Field(ge=1, description="Row number in the file, echoed back in the results")
    values: dict[str, Any]


class KocImportBody(BaseModel):
    rows: list[KocImportRow] = Field(max_length=5000)


@router.post("/leads/import")
def upload_save(body: KocImportBody, db: DbSession, user: AdminUser):
    """Save every row that passes (checked again), into crm_koc_dhruva_leads (keys kd-<id>), with
    its Update, Additional comments, Quoted amount, Conversion amount and Follow-up date as this
    section's CRM details. Every uploaded row is listed (the budget / quantity rule is only shown)."""
    from app.api.v1.endpoints.crm_leads import import_rows_into

    out = import_rows_into(db, SOURCE, body.rows, user)
    response_cache.clear("analytics")
    access.forget()                       # the section's keys changed (koc_dhruva.rows)
    return out


# ── One lead ───────────────────────────────────────────────────────────
def _require(db, lead_key: str) -> None:
    if len(lead_key) > 100 or lead_key not in koc_dhruva.keys(db):
        raise HTTPException(404, "Lead not found")


@router.get("/leads/{lead_key}")
def get_lead(lead_key: str, db: DbSession, user: DhruvaSectionUser):
    _require(db, lead_key)
    empty = KocFilters(lead_filters.CommonFilters())
    items = _page(db, user, empty, 0, 1, None, "asc", {"ids_in": [lead_key]})["items"]
    if not items:
        raise HTTPException(404, "Lead not found")
    _attach_also_in(db, user, items)
    return items[0]


class KocCrmUpdate(BaseModel):
    """Send only what changes; null clears a field. No vendor: these leads are never assigned."""
    model_config = ConfigDict(extra="forbid")
    update_status: Literal["converted", "not_converted", "purchased_elsewhere", "purchased_with_us",
                           "will_call_back", "interested", "not_interested"] | None = None
    notes: str | None = Field(default=None, max_length=5000, description="Additional comments")
    quoted_amount: int | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    quoted_date: date | None = None
    conversion_amount: int | None = Field(default=None, ge=0, le=MAX_AMOUNT)
    follow_up_date: date | None = None


def _answer(db, lead_key: str, meta: LeadMeta | None) -> dict:
    from app.crud.user import user_names

    answer = lead_crm.meta_out(meta, lead_key, {}, None, None, names=user_names(db, [meta.updated_by] if meta else []))
    answer["is_hot"] = hot_leads.is_hot(db, SOURCE, lead_key)
    return answer


def _meta(db, lead_key: str) -> LeadMeta | None:
    return db.exec(select(LeadMeta).where(LeadMeta.source == SOURCE, LeadMeta.lead_key == lead_key)).first()


@router.get("/leads/{lead_key}/crm")
def get_crm(lead_key: str, db: DbSession, user: DhruvaSectionUser):
    _require(db, lead_key)
    return _answer(db, lead_key, _meta(db, lead_key))


@router.put("/leads/{lead_key}/crm")
def save_crm(lead_key: str, body: KocCrmUpdate, db: DbSession, user: DhruvaSectionUser):
    """Admins and Dhruva logins (the receiver works the lead). Saved under source koc_to_dhruva, so
    the spin record of the same lead never changes."""
    _require(db, lead_key)
    changes = {name: getattr(body, name) for name in body.model_fields_set}
    if "notes" in changes and changes["notes"] is not None:
        changes["notes"] = changes["notes"].strip() or None
    now = datetime.now(timezone.utc)
    meta = _meta(db, lead_key) or LeadMeta(source=SOURCE, lead_key=lead_key, updated_at=now)
    old = {name: getattr(meta, name) for name in changes}
    for name, value in changes.items():
        setattr(meta, name, value)
    meta.updated_at, meta.updated_by = now, user.id
    db.add(meta)
    db.commit()
    db.refresh(meta)
    changed = {name: (old[name], value) for name, value in changes.items() if old[name] != value}
    if changed:
        lead_activity.log_changes(db, source=SOURCE, lead_key=lead_key, changes=changed, actor_user_id=user.id,
                                  actor_name=user.name, actor_role=user.role)
    response_cache.clear("analytics")
    return _answer(db, lead_key, meta)


def _set_hot(db, lead_key: str, user, hot: bool) -> dict:
    _require(db, lead_key)
    was_hot = hot_leads.is_hot(db, SOURCE, lead_key)
    result = hot_leads.set_hot(db, SOURCE, lead_key, user.id, hot)
    if was_hot != hot:
        lead_activity.log_action(db, source=SOURCE, lead_key=lead_key, action="hot_on" if hot else "hot_off",
                                 field="is_hot", old=was_hot, new=hot, actor_user_id=user.id, actor_name=user.name,
                                 actor_role=user.role)
    return result


@router.post("/leads/{lead_key}/hot")
def mark_hot(lead_key: str, db: DbSession, user: DhruvaSectionUser):
    return _set_hot(db, lead_key, user, True)


@router.delete("/leads/{lead_key}/hot")
def unmark_hot(lead_key: str, db: DbSession, user: DhruvaSectionUser):
    return _set_hot(db, lead_key, user, False)


@router.get("/leads/{lead_key}/activity")
def get_activity(lead_key: str, db: DbSession, user: DhruvaSectionUser,
                 page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    _require(db, lead_key)
    return lead_activity.list_activity(db, source=SOURCE, lead_key=lead_key, page=page, page_size=page_size)
