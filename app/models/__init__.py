# Import every table model here so SQLModel.metadata knows about all tables
from app.models.branch import Branch  # noqa: F401
from app.models.refresh_token import RefreshToken  # noqa: F401
from app.models.sheet_sync_run import SheetSyncRun  # noqa: F401
from app.models.user import User, UserCreate, UserRead  # noqa: F401
from app.models.vendor import Vendor  # noqa: F401
from app.models.vendor_user import VendorUser  # noqa: F401
from app.models.lead_meta import LeadMeta  # noqa: F401
from app.models.hot_lead import CrmHotLead  # noqa: F401
from app.models.notification import CrmNotification, CrmNotificationRead  # noqa: F401
from app.models.lead_activity import LeadActivity  # noqa: F401
from app.models.wedding_lead import WeddingLead  # noqa: F401
from app.models.wedding_plan import WeddingPlan  # noqa: F401
from app.models.crm_spin_lead import CrmSpinLead  # noqa: F401
from app.models.whatsapp_lead import WhatsAppLead  # noqa: F401
from app.models.vendor_to_koc import VendorToKocLead  # noqa: F401
from app.models.koc_dhruva_lead import KocDhruvaLead  # noqa: F401
