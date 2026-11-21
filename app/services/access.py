"""Who may see and change which lead.

  Admin        sees every lead; assigns vendors; can also change update, quote, conversion amount, notes.
  Vendor user  (crm_users.role = vendor, linked to one vendor in crm_vendor_users) sees only the
               leads whose effective assignment is their vendor, in every source; can change
               update, quote, conversion amount and notes on those leads; can't assign.
  View only    (crm_users.role = viewer) sees everything an admin sees (Viewer.sees_all) and changes
               nothing: every write answers 403 (app/api/deps.block_viewer_writes).
  Unassigned   leads are visible to admins (and view-only logins) only.
  KOC -> Dhruva (koc_to_dhruva, a Dhruva section) is never assigned: admins, view-only and Dhruva
               logins see every lead of it, vendor logins none.
  Spin         workflow rows before the spin cut-off (SPIN_LIVE_FROM) are hidden from everyone:
               not in the assignment map, and can_see says no (see spin_leads.spin_filter).

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
    vendor_id: int | None          # None for admins and view-only logins

    @property
    def is_admin(self) -> bool:
        """May change data as an admin (write paths)."""
        return self.user.role == Role.ADMIN.value

    @property
    def sees_all(self) -> bool:
        """Reads like an admin: every lead and the admin-only fields (admins and view-only logins)."""
        return self.user.role in (Role.ADMIN.value, Role.VIEWER.value)


class NotLinked(Exception):
    """A vendor login without a vendor in crm_vendor_users."""


class DhruvaOnly(Exception):
    """A Dhruva login: it may only use the Dhruva leads section."""


def viewer_for(db: Session, user: User) -> Viewer:
    if user.role in (Role.ADMIN.value, Role.VIEWER.value):
        return Viewer(user, None)
    if user.role == Role.DHRUVA.value:
        raise DhruvaOnly()
    link = db.get(VendorUser, user.id)
    if link is None:
        raise NotLinked()
    return Viewer(user, link.vendor_id)


DHRUVA_SECTION_SOURCES = ("koc_to_dhruva",)


def _dhruva_section_reader(viewer: Viewer) -> bool:
    return viewer.sees_all or viewer.user.role == Role.DHRUVA.value


def _rows(db: Session, source: str) -> list[tuple]:
    """(lead_key, location, pincode, sheet vendor[, branch vendor id[, is_gift]]) for every listed
    lead: the same inputs the list endpoints give lead_crm.attach. Archived/removed leads left out."""
    if source == "spin":
        from app.api.v1.endpoints.vendors import _spin_rows

        return _spin_rows(db)
    if source == "koc_to_dhruva":
        from app.services import koc_dhruva

        return [(key, None, None, None) for key in sorted(koc_dhruva.keys(db))]
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
    """Lead keys this viewer may see in the source; None = all (admin, view only)."""
    if source in DHRUVA_SECTION_SOURCES:
        return None if _dhruva_section_reader(viewer) else set()
    if viewer.sees_all:
        return None
    return {key for key, vendor_id in assignments(db, source).items() if vendor_id == viewer.vendor_id}


def hidden_spin_key(lead_key: str) -> bool:
    """A workflow row that is not in spin (before the cut-off, or not listed). CRM spin leads never
    are; when the workflow database can't be read, nothing is hidden."""
    from app.models.lead_origin import CRM_KEY_PREFIX
    from app.services import also_in

    if str(lead_key).startswith(CRM_KEY_PREFIX):
        return False
    row = also_in.workflow_row(str(lead_key))
    return row is not None and not row[1]


def can_see(db: Session, viewer: Viewer, source: str, lead_key: str) -> bool:
    if source in DHRUVA_SECTION_SOURCES:
        return _dhruva_section_reader(viewer)
    if source == "spin" and hidden_spin_key(lead_key):
        return False
    if viewer.sees_all:
        return True
    return assignments(db, source).get(str(lead_key)) == viewer.vendor_id


def forget() -> None:
    """Call after anything that can change an assignment (CRM details, leads, vendors, branches)."""
    response_cache.clear("access")
