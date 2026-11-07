from collections.abc import Generator

from sqlmodel import Session, create_engine

from app.core.config import settings

connect_args = {}
if settings.DB_SSL_CA:
    connect_args["ssl"] = {"ca": settings.DB_SSL_CA}

engine_kwargs = {"echo": settings.DEBUG, "connect_args": connect_args}
if settings.sqlalchemy_url.startswith("mysql"):
    engine_kwargs.update(
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,   # test connection before use (handles dropped RDS connections)
        pool_recycle=1800,    # recycle every 30 min, below MySQL's wait_timeout
    )

engine = create_engine(settings.sqlalchemy_url, **engine_kwargs)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    with Session(engine) as session:
        yield session
        