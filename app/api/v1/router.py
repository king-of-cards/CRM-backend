from fastapi import APIRouter

from app.api.v1.endpoints import health, read_db, users

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(users.router)
api_router.include_router(read_db.router)