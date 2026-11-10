from collections.abc import Generator

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine, make_url
from sqlmodel import Session, create_engine

from app.core.config import settings


def make_engine(url: str, ssl_ca: str | None = None, read_only: bool = False) -> Engine:
    backend = make_url(url).get_backend_name()  # "mysql", "postgresql" or "sqlite"
    connect_args: dict = {}

    if backend == "mysql":
        if ssl_ca:
            connect_args["ssl"] = {"ca": ssl_ca}
    elif backend == "postgresql":
        if ssl_ca:
            connect_args.update(sslmode="verify-full", sslrootcert=ssl_ca)
        if read_only:
            # PostgreSQL itself rejects INSERT/UPDATE/DELETE/CREATE on these connections
            connect_args["options"] = "-c default_transaction_read_only=on"

    kwargs = {"echo": settings.DEBUG, "connect_args": connect_args}
    if backend != "sqlite":
        kwargs.update(
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,   # test connection before use (handles dropped RDS connections)
            pool_recycle=1800,    # recycle every 30 min, before the server drops idle connections
        )
    engine = create_engine(url, **kwargs)

    if read_only and backend == "mysql":
        # MySQL equivalent: put every new connection in READ ONLY mode
        @event.listens_for(engine, "connect")
        def _set_read_only(dbapi_conn, _):
            with dbapi_conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")

    return engine


# ── Primary database: MySQL (read + write) ──
engine = make_engine(settings.sqlalchemy_url, settings.DB_SSL_CA)

# ── Read-only database: PostgreSQL (None if not configured in .env) ──
read_engine = (
    make_engine(settings.read_sqlalchemy_url, settings.READ_DB_SSL_CA, read_only=True)
    if settings.read_db_enabled
    else None
)


def get_db() -> Generator[Session, None, None]:
    """Session for the primary (MySQL) database."""
    with Session(engine) as session:
        yield session


def get_read_db() -> Generator[Session, None, None]:
    """Session for the read-only (PostgreSQL) database."""
    if read_engine is None:
        raise HTTPException(503, "Read database is not configured (set READ_DB_HOST in .env)")
    with Session(read_engine) as session:
        yield session


def current_database(conn: Connection) -> str | None:
    sql = "SELECT current_database()" if conn.dialect.name == "postgresql" else "SELECT DATABASE()"
    return conn.exec_driver_sql(sql).scalar()


def is_read_only(conn: Connection) -> bool:
    """True if the current session is read-only (PostgreSQL, MySQL 5.7/8 or MariaDB)."""
    if conn.dialect.name == "postgresql":
        return conn.exec_driver_sql("SHOW transaction_read_only").scalar() == "on"
    for var in ("@@session.transaction_read_only", "@@session.tx_read_only"):
        try:
            return bool(conn.exec_driver_sql(f"SELECT {var}").scalar())
        except Exception:
            conn.rollback()
    return False