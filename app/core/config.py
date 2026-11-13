import os
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def mysql_url(user: str, password: str, host: str, port: int, name: str) -> str:
    # quote_plus handles special characters like @ or # in the password
    return (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}?charset=utf8mb4"
    )


def postgres_url(user: str, password: str, host: str, port: int, name: str) -> str:
    return (
        f"postgresql+pg8000://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}"
    )


class Settings(BaseSettings):
    # ENV_FILE selects the env file; ENV_FILE="" disables it (the tests use that)
    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE", ".env") or None, extra="ignore"
    )

    APP_NAME: str = "FastAPI RDS Backend"
    DEBUG: bool = False

    # ── Primary database: MySQL (read + write, tables created on startup) ──
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_NAME: str = "CRM"
    DB_SSL_CA: str | None = None
    DATABASE_URL: str | None = None  # optional full override

    # ── Read-only database: PostgreSQL (SELECT only, never modified) ──
    READ_DB_HOST: str | None = None   # leave empty to disable the read database
    READ_DB_PORT: int = 5432
    READ_DB_USER: str = ""
    READ_DB_PASSWORD: str = ""
    READ_DB_NAME: str = "workflow-db"    # 'postgres' exists on every server; change to your database
    READ_DB_SCHEMA: str = "public"    # PostgreSQL schema the tables live in
    READ_DB_SSL_CA: str | None = None
    READ_DATABASE_URL: str | None = None  # optional full override

    # ── Google Sheets (read-only, through a service account) ──
    GOOGLE_SERVICE_ACCOUNT_FILE: str | None = None
    # Short name -> sheet id, as JSON in .env: GOOGLE_SHEETS={"whatsapp": "1DAnp5..."}
    GOOGLE_SHEETS: dict[str, str] = {}
    GOOGLE_SHEET_CACHE_SECONDS: int = 60
    GOOGLE_SHEETS_SYNC_MINUTES: int = 5   # copy sheets into the database this often; 0 = off

    # Auth. JWT_SECRET_KEY has no default on purpose: the app refuses to start without it.
    JWT_SECRET_KEY: str = Field(min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15

    # Refresh tokens: HttpOnly cookie, rotated on every use
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=7, ge=1)
    REFRESH_TOKEN_MAX_LIFETIME_DAYS: int = Field(default=30, ge=1)  # hard limit per session family
    # A token rotated this recently may be used again (parallel tabs); 0 disables the grace window
    REFRESH_REUSE_GRACE_SECONDS: int = Field(default=30, ge=0)
    REFRESH_COOKIE_NAME: str = "koc_refresh"
    REFRESH_COOKIE_SECURE: bool = True
    REFRESH_COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"

    # Frontend origins allowed to call the API (CORS) and to use the refresh cookie (CSRF check)
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @model_validator(mode="after")
    def _check_auth_settings(self):
        if "*" in self.CORS_ORIGINS:
            raise ValueError("CORS_ORIGINS must list explicit origins (credentials are allowed)")
        if self.REFRESH_COOKIE_SAMESITE == "none" and not self.REFRESH_COOKIE_SECURE:
            raise ValueError("REFRESH_COOKIE_SAMESITE=none requires REFRESH_COOKIE_SECURE=true")
        return self

    @property
    def sqlalchemy_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return mysql_url(self.DB_USER, self.DB_PASSWORD, self.DB_HOST, self.DB_PORT, self.DB_NAME)

    @property
    def read_db_enabled(self) -> bool:
        return bool(self.READ_DATABASE_URL or self.READ_DB_HOST)

    @property
    def read_sqlalchemy_url(self) -> str:
        if self.READ_DATABASE_URL:
            return self.READ_DATABASE_URL
        return postgres_url(
            self.READ_DB_USER, self.READ_DB_PASSWORD,
            self.READ_DB_HOST or "", self.READ_DB_PORT, self.READ_DB_NAME,
        )


settings = Settings()