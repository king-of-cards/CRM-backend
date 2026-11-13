"""Branches (stores) of spin leads, admin only.

GET   /branches          all branches with their vendor and number of spin leads
POST  /branches          add a branch (give the spin wheel's branch id, or leave it empty)
PATCH /branches/{id}     change a branch (name, code, city, vendor, active…)
POST  /branches/import   add new stores from the workflow database's spin_branch table
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from app.api.deps import AdminUser, DbSession
from app.core import response_cache
from app.models.branch import Branch
from app.models.vendor import Vendor
from app.services import branches as branch_service

router = APIRouter(prefix="/branches", tags=["branches"])


class BranchIn(BaseModel):
    id: int | None = Field(default=None, ge=1, description="The spin wheel's branch id; empty = next free id")
    name: str = Field(min_length=1, max_length=150)
    code: str | None = Field(default=None, max_length=50)
    city: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=500)
    contact_number: str | None = Field(default=None, max_length=40)
    manager_name: str | None = Field(default=None, max_length=150)
    vendor_id: int | None = None
    is_active: bool = True


class BranchPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    code: str | None = Field(default=None, max_length=50)
    city: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=500)
    contact_number: str | None = Field(default=None, max_length=40)
    manager_name: str | None = Field(default=None, max_length=150)
    vendor_id: int | None = None
    is_active: bool | None = None


def _lead_counts() -> dict[str, int]:
    from app.api.v1.endpoints.spin_leads import spin_branch_counts

    return response_cache.get(("branches", "lead_counts"), spin_branch_counts, fresh=300, stale=3600)


def _out(b: Branch, vendors: dict[int, Vendor], counts: dict[str, int]) -> dict:
    v = vendors.get(b.vendor_id) if b.vendor_id else None
    auto = None if v else branch_service.auto_vendor([x for x in sorted(vendors.values(), key=lambda x: (x.priority, x.id)) if x.is_active], b)
    return {
        "id": b.id,
        "name": b.name,
        "code": b.code,
        "slug": b.slug,
        "city": b.city,
        "address": b.address,
        "contact_number": b.contact_number,
        "manager_name": b.manager_name,
        "vendor": {"id": v.id, "name": v.name} if v else None,       # picked for this branch
        "auto_vendor": {"id": auto.id, "name": auto.name} if auto else None,  # from the area rules
        "is_active": b.is_active,
        "source": b.source,
        "spin_leads": counts.get(str(b.id), 0),
    }


def _check_vendor(db, vendor_id: int | None) -> None:
    if vendor_id is not None and db.get(Vendor, vendor_id) is None:
        raise HTTPException(404, "Vendor not found")


def _changed() -> None:
    branch_service.forget_cache()
    response_cache.clear("spin")      # spin pages carry branch names and vendors
    response_cache.clear("vendors")


@router.get("")
def list_branches(_: AdminUser, db: DbSession):
    vendors = {v.id: v for v in db.exec(select(Vendor))}
    counts = _lead_counts()
    branches = db.exec(select(Branch).order_by(Branch.is_active.desc(), Branch.city, Branch.name)).all()
    known = {str(b.id) for b in branches}
    return {
        "branches": [_out(b, vendors, counts) for b in branches],
        # Branch ids found on spin leads that aren't set up here yet
        "unknown_branch_ids": sorted((k for k in counts if k not in known), key=lambda k: (len(k), k)),
    }


@router.post("", status_code=201)
def create_branch(body: BranchIn, _: AdminUser, db: DbSession):
    if body.id is not None and db.get(Branch, body.id):
        raise HTTPException(409, f"Branch id {body.id} already exists")
    _check_vendor(db, body.vendor_id)
    now = datetime.now(timezone.utc)
    data = body.model_dump()
    data["name"] = data["name"].strip()
    if data["id"] is None:
        data.pop("id")
    branch = Branch(**data, source="manual", created_at=now, updated_at=now)
    db.add(branch)
    db.commit()
    db.refresh(branch)
    _changed()
    return _out(branch, {v.id: v for v in db.exec(select(Vendor))}, _lead_counts())


@router.patch("/{branch_id}")
def update_branch(branch_id: int, body: BranchPatch, _: AdminUser, db: DbSession):
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise HTTPException(404, "Branch not found")
    data = body.model_dump(exclude_unset=True)
    if "vendor_id" in data:
        _check_vendor(db, data["vendor_id"])
    for field, value in data.items():
        setattr(branch, field, value.strip() if isinstance(value, str) else value)
    branch.updated_at = datetime.now(timezone.utc)
    db.add(branch)
    db.commit()
    db.refresh(branch)
    _changed()
    return _out(branch, {v.id: v for v in db.exec(select(Vendor))}, _lead_counts())


@router.post("/import")
def import_branches(_: AdminUser, db: DbSession):
    result = branch_service.import_from_workflow(db)
    if not result["available"]:
        raise HTTPException(503, "The workflow database's spin_branch table can't be read right now.")
    _changed()
    return result