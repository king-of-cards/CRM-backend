from fastapi import APIRouter, Depends

from app.api.deps import block_viewer_writes

from app.api.v1.endpoints import (
    analytics,
    auth,
    branches,
    crm_leads,
    dhruva,
    health,
    lead_crm,
    notifications,
    reminders,
    read_db,
    sheets,
    spin_leads,
    users,
    vendor_to_koc,
    vendors,
    wedding_leads,
    wedding_plans,
    whatsapp_leads,
)

# View-only logins: every write answers 403 (see deps.block_viewer_writes)
api_router = APIRouter(dependencies=[Depends(block_viewer_writes)])
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(read_db.router)
api_router.include_router(spin_leads.router)
api_router.include_router(spin_leads.gifts_router)       # /wedding-gifts-leads
api_router.include_router(sheets.router)
api_router.include_router(whatsapp_leads.router)
api_router.include_router(wedding_leads.router)
api_router.include_router(wedding_plans.router)
api_router.include_router(analytics.router)
api_router.include_router(vendors.router)
api_router.include_router(branches.router)
api_router.include_router(lead_crm.router)
api_router.include_router(notifications.router)
api_router.include_router(reminders.router)
api_router.include_router(crm_leads.router)
api_router.include_router(vendor_to_koc.router)
api_router.include_router(dhruva.router)
