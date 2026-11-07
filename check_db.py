"""Verify the database credentials in .env before starting the app.

Usage:  python check_db.py
"""
import sys

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.db.session import engine

HINTS = {
    1044: "No access to database {db}: it may not exist, or this user lacks GRANTs on it.",
    1045: "Access denied: check DB_USER and DB_PASSWORD.",
    1049: "Unknown database: create it first with CREATE DATABASE {db};",
    2003: "Can't reach the host: check DB_HOST/DB_PORT, the RDS security group "
          "(inbound 3306 from your IP) and that the instance has Public access = Yes.",
    2005: "Unknown host: check DB_HOST is the exact RDS endpoint.",
}


def main() -> int:
    print(f"Connecting to {settings.DB_USER}@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME} ...")
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar()
            db = conn.execute(text("SELECT DATABASE()")).scalar()
        print(f"OK  connected to database '{db}' (MySQL {version})")
        return 0
    except OperationalError as exc:
        code = exc.orig.args[0] if exc.orig and exc.orig.args else None
        hint = HINTS.get(code, str(exc.orig)).format(db=settings.DB_NAME)
        print(f"FAILED [{code}]  {hint}")
        return 1


if __name__ == "__main__":
    sys.exit(main())