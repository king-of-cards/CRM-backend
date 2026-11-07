from urllib.parse import quote_plus

from sqlalchemy import create_engine, text

from app.core.config import settings as s

# Connect to the server without selecting a database
engine = create_engine(
    f"mysql+pymysql://{s.DB_USER}:{quote_plus(s.DB_PASSWORD)}@{s.DB_HOST}:{s.DB_PORT}/"
)

with engine.connect() as conn:
    print("Databases on this server:")
    for (name,) in conn.execute(text("SHOW DATABASES")):
        print("  ", name)