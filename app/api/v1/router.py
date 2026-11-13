from fastapi import APIRouter

from app.api.v1.endpoints import (
    analytics,
    auth,
    health,
    lead_crm,
    read_db,
    sheets,
    spin_leads,
    users,
    vendors,
    wedding_leads,
    wedding_plans,
    whatsapp_leads,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(read_db.router)
api_router.include_router(spin_leads.router)
api_router.include_router(sheets.router)
api_router.include_router(whatsapp_leads.router)
api_router.include_router(wedding_leads.router)
api_router.include_router(wedding_plans.router)
api_router.include_router(analytics.router)
api_router.include_router(vendors.router)
api_router.include_router(lead_crm.router)