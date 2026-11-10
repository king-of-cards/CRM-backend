from fastapi import APIRouter

from app.api.v1.endpoints import auth, health, read_db, spin_leads, users

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(read_db.router)
api_router.include_router(spin_leads.router)
