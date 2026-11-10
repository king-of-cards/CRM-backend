"""Verify both database connections configured in .env.

Usage:  python check_db.py
"""
import sys

from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.db.session import current_database, engine, is_read_only, read_engine

MYSQL_HINTS = {
    1044: "No access to database '{db}': it may not exist, or this user lacks GRANTs on it.",
    1045: "Access denied: check the user and password.",
    1049: "Unknown database '{db}': check the name (case-sensitive).",
    2003: "Can't reach the host: check host/port, the RDS security group "
          "(inbound 3306 from your IP) and that the instance has Public access = Yes.",
    2005: "Unknown host: check the host is the exact RDS endpoint.",
}
POSTGRES_HINTS = [
    ("password authentication failed", "Access denied: check READ_DB_USER and READ_DB_PASSWORD."),
    ("does not exist", "Database '{db}' does not exist: set READ_DB_NAME=postgres to list the real names."),
    ("could not translate host name", "Unknown host: check READ_DB_HOST is the exact RDS endpoint."),
    ("timeout", "Can't reach the host: check the RDS security group (inbound 5432 from your IP) "
                "and that the instance has Public access = Yes."),
    ("connection refused", "Connection refused: check READ_DB_HOST and READ_DB_PORT (PostgreSQL uses 5432)."),
    ("no pg_hba.conf entry", "Server rejected the connection: check SSL settings and that this user may log in."),
]
MYSQL_SYSTEM_DBS = {"information_schema", "mysql", "performance_schema", "sys"}
PG_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast"}


def hint_for(exc: OperationalError, backend: str, db_name: str) -> str:
    if backend == "mysql":
        code = exc.orig.args[0] if exc.orig and exc.orig.args else None
        return f"[{code}] " + MYSQL_HINTS.get(code, str(exc.orig)).format(db=db_name)
    message = str(exc.orig)
    for needle, hint in POSTGRES_HINTS:
        if needle in message.lower():
            return hint.format(db=db_name)
    return message.strip()


def check(label, eng, user, host, port, db_name) -> bool:
    backend = eng.dialect.name
    print(f"\n[{label}] {backend}  {user}@{host}:{port}/{db_name or '(no database selected)'}")
    try:
        with eng.connect() as conn:
            version = conn.exec_driver_sql("SELECT version()").scalar().split(" on ")[0]
            print(f"  OK  connected ({version})")
            print(f"      database: {current_database(conn)}, read-only session: {is_read_only(conn)}")

            if backend == "postgresql":
                dbs = [d for (d,) in conn.exec_driver_sql(
                    "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname")]
                schemas = [s for s in inspect(conn).get_schema_names() if s not in PG_SYSTEM_SCHEMAS]
                print(f"      databases on this server: {dbs}")
                print(f"      schemas in this database: {schemas}")
                if settings.READ_DB_SCHEMA in schemas:
                    tables = inspect(conn).get_table_names(schema=settings.READ_DB_SCHEMA)
                    print(f"      tables in '{settings.READ_DB_SCHEMA}': {tables}")
            elif not db_name:
                dbs = [d for (d,) in conn.exec_driver_sql("SHOW DATABASES") if d not in MYSQL_SYSTEM_DBS]
                print(f"      databases on this server: {dbs}")
        return True
    except OperationalError as exc:
        print(f"  FAILED  {hint_for(exc, backend, db_name)}")
        return False


def main() -> int:
    # Keep this script's output clean even when DEBUG=true
    engine.echo = False
    if read_engine is not None:
        read_engine.echo = False

    ok = check("PRIMARY", engine, settings.DB_USER, settings.DB_HOST, settings.DB_PORT, settings.DB_NAME)
    if read_engine is None:
        print("\n[READ-ONLY] not configured (READ_DB_HOST is empty), skipping")
    else:
        ok &= check("READ-ONLY", read_engine, settings.READ_DB_USER, settings.READ_DB_HOST,
                    settings.READ_DB_PORT, settings.READ_DB_NAME)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())