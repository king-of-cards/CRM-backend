from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel

import app.models  # noqa: F401  (registers table models on SQLModel.metadata)
from app.api.v1.router import api_router
from app.core.config import settings
from app.db.session import engine, read_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Creates missing tables on the PRIMARY (MySQL) database only.
    # The read-only PostgreSQL database is never modified.
    SQLModel.metadata.create_all(engine)
    yield
    engine.dispose()
    if read_engine is not None:
        read_engine.dispose()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
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