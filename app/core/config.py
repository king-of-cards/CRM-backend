from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


def mysql_url(user: str, password: str, host: str, port: int, name: str) -> str:
    # quote_plus handles special characters like @ or # in the password
    return (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}?charset=utf8mb4"
    )


def postgres_url(user: str, password: str, host: str, port: int, name: str) -> str:
    return (
        f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}"
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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