from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlmodel import Session

from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health/db")
def db_health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"database": "connected"}
    except Exception as exc:
        raise HTTPException(503, f"Database unreachable: {exc.__class__.__name__}")