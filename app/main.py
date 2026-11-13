import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlmodel import Session, SQLModel

import app.models  # noqa: F401  (registers table models on SQLModel.metadata)
from app.api.v1.endpoints.analytics import warm_overview
from app.api.v1.endpoints.spin_leads import warm_first_pages
from app.api.v1.router import api_router
from app.core.config import settings
from app.db.session import engine, read_engine
from app.services.lead_crm import seed_vendors
from app.services.sync_scheduler import sync_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Creates missing tables on the PRIMARY (MySQL) database only.
    # The read-only PostgreSQL database is never modified.
    SQLModel.metadata.create_all(engine)

    # First run: create the vendors OHP, Phodeo and RJ with their areas
    with Session(engine) as session:
        seed_vendors(session)

    # Prepare the slow answers in the background, so the first visitor gets them instantly
    warm_overview()
    warm_first_pages()

    # Google Sheet → database sync every GOOGLE_SHEETS_SYNC_MINUTES (0 = off)
    sync_task = asyncio.create_task(sync_loop())
    yield
    sync_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sync_task

    engine.dispose()
    if read_engine is not None:
        read_engine.dispose()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
# Compress JSON responses (lead lists shrink to roughly a fifth)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api/v1")


@app.get("/")
def root():
    return {"message": "Hello World"}