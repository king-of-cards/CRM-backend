"""Routes for the read-only PostgreSQL database.

/read/health                 always available
/read/schemas, /read/tables  table explorer, only when DEBUG=true (it exposes raw data)
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import MetaData, Table, inspect, select
from sqlmodel import Session

from app.core.config import settings
from app.db.session import current_database, get_read_db, is_read_only

router = APIRouter(prefix="/read", tags=["read-only database"])

SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast"}


def require_debug():
    if not settings.DEBUG:
        raise HTTPException(403, "Table explorer is only available when DEBUG=true")


def _check_schema(db: Session, schema: str) -> None:
    if schema not in inspect(db.get_bind()).get_schema_names():
        raise HTTPException(404, f"Schema '{schema}' not found")


def _check_table(db: Session, schema: str, table_name: str) -> None:
    # Only allow real schema/table names, which also prevents SQL injection
    _check_schema(db, schema)
    if table_name not in inspect(db.get_bind()).get_table_names(schema=schema):
        raise HTTPException(404, f"Table '{schema}.{table_name}' not found")


@router.get("/health")
def read_db_health(db: Session = Depends(get_read_db)):
    try:
        conn = db.connection()
        return {
            "database": "connected",
            "engine": conn.dialect.name,
            "name": current_database(conn),
            "read_only": is_read_only(conn),
        }
    except Exception as exc:
        raise HTTPException(503, f"Read database unreachable: {exc.__class__.__name__}")


@router.get("/schemas", dependencies=[Depends(require_debug)])
def list_schemas(db: Session = Depends(get_read_db)):
    names = inspect(db.get_bind()).get_schema_names()
    return {"schemas": [s for s in names if s not in SYSTEM_SCHEMAS]}


@router.get("/tables", dependencies=[Depends(require_debug)])
def list_tables(
    schema: str = Query(settings.READ_DB_SCHEMA),
    db: Session = Depends(get_read_db),
):
    _check_schema(db, schema)
    insp = inspect(db.get_bind())
    return {
        "schema": schema,
        "tables": insp.get_table_names(schema=schema),
        "views": insp.get_view_names(schema=schema),
    }


@router.get("/tables/{table_name}/columns", dependencies=[Depends(require_debug)])
def table_columns(
    table_name: str,
    schema: str = Query(settings.READ_DB_SCHEMA),
    db: Session = Depends(get_read_db),
):
    _check_table(db, schema, table_name)
    cols = inspect(db.get_bind()).get_columns(table_name, schema=schema)
    return [
        {"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]}
        for c in cols
    ]


@router.get("/tables/{table_name}/rows", dependencies=[Depends(require_debug)])
def table_rows(
    table_name: str,
    schema: str = Query(settings.READ_DB_SCHEMA),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_read_db),
):
    _check_table(db, schema, table_name)
    table = Table(table_name, MetaData(), schema=schema, autoload_with=db.get_bind())
    rows = db.execute(select(table).limit(limit).offset(offset)).mappings().all()
    return [dict(r) for r in rows]