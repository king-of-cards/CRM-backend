"""Who may see and change which lead.

  Admin        sees every lead; assigns vendors; can also change interest, quote, converted, notes.
  Vendor user  (crm_users.role = vendor, linked to one vendor in crm_vendor_users) sees only the
               leads whose effective assignment is their vendor, in every source; can change
               interest, quote, converted and notes on those leads; can't assign.
  Unassigned   leads are visible to admins only.

The effective assignment is the one shown in "Assigned to" (lead_crm.meta_out): manual, then the
sheet's "leads shared", then the branch's vendor, then the area rules, unless the lead was
uploaded unassigned (crm_lead_meta.auto_assign = False). The same function decides what a vendor
sees, so the list, the export and the chip can never disagree.
"""
import logging
from dataclasses import dataclass

from sqlmodel import Session, select

from app.core import response_cache
from app.models.user import Role, User
from app.models.vendor_user import VendorUser
from app.models.wedding_lead import WeddingLead
from app.models.wedding_plan import WeddingPlan
from app.models.whatsapp_lead import WhatsAppLead
from app.services import lead_crm

logger = logging.getLogger(__name__)

SOURCES = ("spin", "whatsapp", "wedding", "wedding_planner")
CACHE_SECONDS = 30          # rebuilt after this (never served stale); cleared on every change


@dataclass(frozen=True)
class Viewer:
    user: User
    vendor_id: int | None          # None for admins

    @property
    def is_admin(self) -> bool:
        return self.user.role == Role.ADMIN.value


class NotLinked(Exception):
    """A vendor login without a vendor in crm_vendor_users."""


def viewer_for(db: Session, user: User) -> Viewer:
    if user.role == Role.ADMIN.value:
        return Viewer(user, None)
    link = db.get(VendorUser, user.id)
    if link is None:
        raise NotLinked()
    return Viewer(user, link.vendor_id)


def _rows(db: Session, source: str) -> list[tuple]:
    """(lead_key, location, pincode, sheet vendor[, branch vendor id]) for every listed lead:
    the same inputs the list endpoints give lead_crm.attach. Archived/removed leads left out."""
    if source == "spin":
        from app.api.v1.endpoints.vendors import _spin_rows

        return _spin_rows(db)
    if source == "whatsapp":
        return [(k, c, None, None) for k, c in db.exec(
            select(WhatsAppLead.sheet_key, WhatsAppLead.country).where(WhatsAppLead.is_deleted == False))]  # noqa: E712
    if source == "wedding":
        return [(k, c, None, s) for k, c, s in db.exec(
            select(WeddingLead.sheet_key, WeddingLead.city, WeddingLead.shared_with)
            .where(WeddingLead.is_deleted == False))]  # noqa: E712
    return [(k, c or h, None, s) for k, c, h, s in db.exec(
        select(WeddingPlan.plan_id, WeddingPlan.city, WeddingPlan.home_city, WeddingPlan.shared_with)
        .where(WeddingPlan.is_deleted == False))]  # noqa: E712


def assignments(db: Session, source: str) -> dict[str, int | None]:
    """{lead_key: effective vendor id or None} for every listed lead of the source."""
    def build():
        infos = lead_crm.effective_vendor_counts(db, source, _rows(db, source))
        return {info["lead_key"]: (info["vendor"] or {}).get("id") for info in infos}

    return response_cache.get(("access", source), build, fresh=CACHE_SECONDS, stale=CACHE_SECONDS)


def visible_keys(db: Session, viewer: Viewer, source: str) -> set[str] | None:
    """Lead keys this viewer may see in the source; None = all (admin)."""
    if viewer.is_admin:
        return None
    return {key for key, vendor_id in assignments(db, source).items() if vendor_id == viewer.vendor_id}


def can_see(db: Session, viewer: Viewer, source: str, lead_key: str) -> bool:
    if viewer.is_admin:
        return True
    return assignments(db, source).get(str(lead_key)) == viewer.vendor_id


def forget() -> None:
    """Call after anything that can change an assignment (CRM details, leads, vendors, branches)."""
    response_cache.clear("access")
