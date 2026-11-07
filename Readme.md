# FastAPI Backend with AWS RDS MySQL

A REST API backend built with **FastAPI** and **SQLModel**, connected to an **AWS RDS MySQL** database. The project uses a layered structure (config → database → models → CRUD → routes) so new features can be added without touching existing code.

---

## Table of Contents

1. [Tech Stack](#tech-stack)
2. [Project Structure](#project-structure)
3. [How the Project Is Built](#how-the-project-is-built)
4. [Prerequisites](#prerequisites)
5. [Setup](#setup)
6. [Configuring AWS RDS](#configuring-aws-rds)
7. [Environment Variables](#environment-variables)
8. [Running the App](#running-the-app)
9. [API Endpoints](#api-endpoints)
10. [Adding a New Resource](#adding-a-new-resource)
11. [Troubleshooting](#troubleshooting)
12. [Production Notes](#production-notes)

---

## Tech Stack

| Component | Purpose |
|---|---|
| **FastAPI** | Web framework: routing, request validation, automatic API docs |
| **SQLModel** | ORM: defines database tables and API request/response models in one class |
| **SQLAlchemy** | Engine and connection pooling underneath SQLModel |
| **PyMySQL** | MySQL driver used to talk to RDS |
| **cryptography** | Required by PyMySQL for MySQL 8's `caching_sha2_password` authentication |
| **pydantic-settings** | Loads configuration from the `.env` file |
| **Uvicorn** | ASGI server that runs the app (installed with `fastapi[standard]`) |
| **AWS RDS (MySQL)** | Hosted MySQL database |

Requires **Python 3.10 or newer** (the code uses `str | None` type syntax).

---

## Project Structure

```
backend/
├── .env                      # Your real credentials (NOT committed to Git)
├── .env.example              # Template showing which variables are needed
├── .gitignore                # Keeps .env, venv/ and caches out of Git
├── requirements.txt          # Python dependencies
├── check_db.py               # Tests the database connection using .env
├── list_dbs.py               # Lists (and optionally creates) databases on the RDS server
└── app/
    ├── __init__.py
    ├── main.py               # App entry point: creates tables, mounts routes
    ├── core/
    │   ├── __init__.py
    │   └── config.py         # Reads .env into a `settings` object
    ├── db/
    │   ├── __init__.py
    │   └── session.py        # Engine, connection pool, get_db() dependency
    ├── models/
    │   ├── __init__.py       # Imports every table model (required for table creation)
    │   └── user.py           # User table + UserCreate / UserRead models
    ├── crud/
    │   ├── __init__.py
    │   └── user.py           # Database queries for users
    └── api/
        ├── __init__.py
        └── v1/
            ├── __init__.py
            ├── router.py     # Combines all endpoint routers
            └── endpoints/
                ├── __init__.py
                ├── health.py # GET /health/db
                └── users.py  # User routes
```

### What each part does

| Path | Responsibility | Spring Boot equivalent |
|---|---|---|
| `requirements.txt` | Lists dependencies | `pom.xml` / `build.gradle` |
| `.env` | Credentials and environment settings | `application.properties` |
| `app/main.py` | Starts the app, creates tables, registers routes | `@SpringBootApplication` class |
| `app/core/config.py` | Turns `.env` values into typed settings | `@ConfigurationProperties` |
| `app/db/session.py` | Connection pool and per-request session | `DataSource` + HikariCP |
| `app/models/` | Tables and request/response shapes | `@Entity` + DTOs |
| `app/crud/` | Database queries | `Repository` |
| `app/api/v1/endpoints/` | HTTP routes | `@RestController` |
| `app/api/v1/router.py` | Collects all routers under `/api/v1` | Controller scanning |

### Naming convention

- `app/models/user.py` and `app/crud/user.py` are **singular**.
- `app/api/v1/endpoints/users.py` is **plural**, because it is imported as `users` in `router.py`.

The `__init__.py` files are empty except `app/models/__init__.py`. They mark each folder as a Python package so imports like `from app.db.session import get_db` work. **Every folder inside `app/` needs one.**

---

## How the Project Is Built

### Configuration (`app/core/config.py`)

`Settings` (from `pydantic-settings`) reads `.env` automatically. It builds the MySQL connection URL from the individual values and URL-encodes the password with `quote_plus`, so passwords containing characters like `@` or `#` work correctly.

```
mysql+pymysql://<DB_USER>:<encoded DB_PASSWORD>@<DB_HOST>:<DB_PORT>/<DB_NAME>?charset=utf8mb4
```

### Database connection (`app/db/session.py`)

A single shared **engine** holds a connection pool to RDS:

| Setting | Value | Why |
|---|---|---|
| `pool_size` | 5 | Connections kept open |
| `max_overflow` | 10 | Extra connections allowed under load |
| `pool_pre_ping` | True | Tests a connection before use, so dropped RDS connections don't cause errors |
| `pool_recycle` | 1800 | Replaces connections every 30 min, before MySQL's idle timeout closes them |

`get_db()` is a FastAPI dependency that gives each request its own session and always closes it afterwards.

### Models (`app/models/user.py`)

SQLModel lets one file define both the table and the API shapes:

| Class | Role |
|---|---|
| `UserBase` | Shared fields and validation (`name`, `email`) |
| `User` (`table=True`) | The actual `users` table in MySQL |
| `UserCreate` | Request body for creating a user (no `id` or `created_at`) |
| `UserRead` | Response shape returned by the API |

Only classes with `table=True` become database tables.

### Table creation (`app/main.py`)

On startup, `SQLModel.metadata.create_all(engine)` creates any tables that don't exist yet. It **never modifies existing tables**. If you change a model's columns, the change won't apply to an existing table (see [Production Notes](#production-notes) for migrations).

### Request flow

Example: `POST /api/v1/users` with `{"name": "Asha", "email": "asha@example.com"}`

1. `main.py` routes the request through `router.py` to `endpoints/users.py`.
2. FastAPI validates the body against `UserCreate`. Invalid data is rejected with **422** before your code runs.
3. The endpoint receives a database session from `get_db()`.
4. The endpoint calls `crud/user.py` to check for a duplicate email (**409** if found), then inserts the row.
5. The saved `User` is returned as `UserRead` JSON with status **201**.

Each layer only talks to its neighbour: **routes → crud → models + session → config → `.env`**.

---

## Prerequisites

- Python 3.10+
- An AWS RDS MySQL instance
- Your RDS endpoint, master username and password
- Network access from your machine to the RDS instance (see [Configuring AWS RDS](#configuring-aws-rds))

---

## Setup

Run all commands from the `backend/` folder.

### 1. Create and activate a virtual environment

**Windows (PowerShell)**
```powershell
python -m venv venv
venv\Scripts\activate
```

**macOS / Linux**
```bash
python3 -m venv venv
source venv/bin/activate
```

You should see `(venv)` at the start of your terminal prompt.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

**Windows**
```powershell
copy .env.example .env
```

**macOS / Linux**
```bash
cp .env.example .env
```

Then fill in your values (see [Environment Variables](#environment-variables)).

### 4. Make sure the database exists

The database named in `DB_NAME` must already exist on the RDS server. To see which databases exist:

```bash
python list_dbs.py
```

If the one you need isn't listed, create it. Either add this to `list_dbs.py` and run it once:

```python
with engine.connect() as conn:
    conn.execute(text("CREATE DATABASE IF NOT EXISTS your_db_name"))
```

or run `CREATE DATABASE your_db_name;` from any MySQL client.

> Database names are **case-sensitive** on RDS. `CRM` and `crm` are different databases.

### 5. Test the connection

```bash
python check_db.py
```

Expected output:
```
Connecting to admin@your-db....rds.amazonaws.com:3306/your_db_name ...
OK  connected to database 'your_db_name' (MySQL 8.x.x)
```

If it fails, it prints the error code with a hint. See [Troubleshooting](#troubleshooting).

---

## Configuring AWS RDS

For your local machine to reach RDS, check these in the AWS console:

1. **Endpoint**: RDS → Databases → your instance → **Connectivity & security** → copy the **Endpoint**. This goes in `DB_HOST`.
2. **Public access**: To connect from your laptop, the instance must have **Publicly accessible = Yes** (RDS → your instance → **Modify** → Connectivity).
3. **Security group**: On the same tab, open the VPC security group → **Edit inbound rules** → add:
   - Type: **MySQL/Aurora**
   - Port: **3306**
   - Source: **My IP**
4. **Master credentials**: The username and password set when the instance was created go in `DB_USER` and `DB_PASSWORD`. Reset the password under **Modify** → **Master password** if needed.

> **Using a database shared with another app?** `create_all` only creates tables that don't already exist. If the database already has a `users` table with different columns, the user endpoints will fail. Use a separate database for this project, or change `__tablename__` in `app/models/user.py`.

---

## Environment Variables

`.env` lives in the `backend/` folder (same level as `requirements.txt`).

```dotenv
# App
APP_NAME=FastAPI RDS Backend
DEBUG=true

# AWS RDS MySQL
DB_HOST=your-db.xxxxxxxx.ap-south-1.rds.amazonaws.com
DB_PORT=3306
DB_USER=admin
DB_PASSWORD=your_password
DB_NAME=your_db_name

# Optional: path to the RDS CA bundle to enforce SSL
DB_SSL_CA=
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `APP_NAME` | No | `FastAPI RDS Backend` | Title shown in the API docs |
| `DEBUG` | No | `false` | When `true`, logs every SQL query to the console |
| `DB_HOST` | Yes | `localhost` | RDS endpoint |
| `DB_PORT` | No | `3306` | MySQL port |
| `DB_USER` | Yes | `root` | Database username |
| `DB_PASSWORD` | Yes | *(empty)* | Database password; special characters are handled automatically |
| `DB_NAME` | Yes | `appdb` | Database name (case-sensitive, must already exist) |
| `DB_SSL_CA` | No | *(empty)* | Path to `global-bundle.pem` to connect over SSL |
| `DATABASE_URL` | No | *(empty)* | Full connection URL that overrides all `DB_*` values (e.g. `sqlite:///./local.db` for offline testing) |

**Rules for `.env`:**
- No spaces around `=`.
- No trailing spaces after values.
- No quotes needed.
- Never commit `.env` to Git (it's already in `.gitignore`).

> The server port is **not** set in `.env`. Pass it on the command line instead (see below).

---

## Running the App

### Development (auto-reload on save)

```bash
fastapi dev app/main.py
```

On a different port:
```bash
fastapi dev app/main.py --port 8081
```

Alternative using Uvicorn directly:
```bash
uvicorn app.main:app --reload
```

A successful start shows:
```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000
```

On startup, the app connects to RDS and creates the `users` table if it doesn't exist.

### Production

```bash
fastapi run app/main.py --host 0.0.0.0 --port 8000
```

### Useful URLs

| URL | Description |
|---|---|
| http://127.0.0.1:8000/ | Hello World |
| http://127.0.0.1:8000/docs | Interactive Swagger UI to test every endpoint |
| http://127.0.0.1:8000/redoc | Alternative API documentation |
| http://127.0.0.1:8000/api/v1/health/db | Database connection check |

---

## API Endpoints

All routes are prefixed with `/api/v1`.

| Method | Path | Description | Success | Errors |
|---|---|---|---|---|
| GET | `/health/db` | Checks the database connection | 200 | 503 |
| POST | `/users` | Create a user | 201 | 409, 422 |
| GET | `/users` | List users (`?skip=0&limit=100`) | 200 | |
| GET | `/users/{user_id}` | Get one user by ID | 200 | 404 |

### Examples

**Health check**
```bash
curl http://127.0.0.1:8000/api/v1/health/db
```
```json
{"database": "connected"}
```

**Create a user**
```bash
curl -X POST http://127.0.0.1:8000/api/v1/users \
  -H "Content-Type: application/json" \
  -d '{"name": "Asha", "email": "asha@example.com"}'
```
```json
{"name": "Asha", "email": "asha@example.com", "id": 1, "created_at": "2026-11-07T09:18:58"}
```

PowerShell equivalent:
```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/users `
  -ContentType "application/json" `
  -Body '{"name": "Asha", "email": "asha@example.com"}'
```

**List users**
```bash
curl "http://127.0.0.1:8000/api/v1/users?skip=0&limit=10"
```

**Get one user**
```bash
curl http://127.0.0.1:8000/api/v1/users/1
```

### Error responses

| Status | When | Example body |
|---|---|---|
| 404 | User ID doesn't exist | `{"detail": "User not found"}` |
| 409 | Email already registered | `{"detail": "Email already registered"}` |
| 422 | Invalid body (bad email, empty name, missing field) | Validation details from FastAPI |
| 503 | Database unreachable (`/health/db`) | `{"detail": "Database unreachable: OperationalError"}` |

### Validation rules (`users`)

| Field | Rule |
|---|---|
| `name` | Required, 1–100 characters |
| `email` | Required, valid email format, unique, max 255 characters |

---

## Adding a New Resource

Example: adding `products`.

### 1. Model: `app/models/product.py`

```python
from datetime import datetime

from sqlalchemy import Column, DateTime, func
from sqlmodel import Field, SQLModel


class ProductBase(SQLModel):
    name: str = Field(min_length=1, max_length=200)
    price: float = Field(ge=0)


class Product(ProductBase, table=True):
    __tablename__ = "products"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime, server_default=func.now(), nullable=False),
    )


class ProductCreate(ProductBase):
    pass


class ProductRead(ProductBase):
    id: int
    created_at: datetime
```

### 2. Register the model: `app/models/__init__.py`

```python
from app.models.user import User, UserCreate, UserRead  # noqa: F401
from app.models.product import Product, ProductCreate, ProductRead  # noqa: F401
```

> **Don't skip this step.** Without the import, the `products` table won't be created.

### 3. Queries: `app/crud/product.py`

```python
from sqlmodel import Session, select

from app.models.product import Product, ProductCreate


def get_product(db: Session, product_id: int) -> Product | None:
    return db.get(Product, product_id)


def list_products(db: Session, skip: int = 0, limit: int = 100) -> list[Product]:
    return list(db.exec(select(Product).offset(skip).limit(limit)).all())


def create_product(db: Session, data: ProductCreate) -> Product:
    product = Product.model_validate(data)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product
```

### 4. Routes: `app/api/v1/endpoints/products.py`

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.crud import product as crud
from app.db.session import get_db
from app.models.product import ProductCreate, ProductRead

router = APIRouter(prefix="/products", tags=["products"])


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreate, db: Session = Depends(get_db)):
    return crud.create_product(db, payload)


@router.get("", response_model=list[ProductRead])
def list_products(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return crud.list_products(db, skip, limit)


@router.get("/{product_id}", response_model=ProductRead)
def get_product(product_id: int, db: Session = Depends(get_db)):
    product = crud.get_product(db, product_id)
    if not product:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product
```

### 5. Register the router: `app/api/v1/router.py`

```python
from fastapi import APIRouter

from app.api.v1.endpoints import health, products, users

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(users.router)
api_router.include_router(products.router)
```

Restart the app. The `products` table is created and the new routes appear in `/docs`.

### Checklist

- [ ] `app/models/<name>.py` created
- [ ] Model imported in `app/models/__init__.py`
- [ ] `app/crud/<name>.py` created
- [ ] `app/api/v1/endpoints/<names>.py` created
- [ ] Router registered in `app/api/v1/router.py`

---

## Troubleshooting

### Import errors

| Error | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'app'` | Missing `__init__.py` files, or an extra `__init__.py` in `backend/` | Create `__init__.py` in every folder inside `app/`; delete any `__init__.py` directly in `backend/` |
| `ImportError: cannot import name 'users' from 'app.api.v1.endpoints'` | File is named `user.py` instead of `users.py`, or is missing | Rename to `app/api/v1/endpoints/users.py` |
| `ModuleNotFoundError: No module named 'sqlmodel'` (or any package) | Dependencies not installed, or venv not activated | Activate the venv, then `pip install -r requirements.txt` |

Create all missing `__init__.py` files at once (PowerShell, from `backend/`):

```powershell
"app","app\core","app\db","app\models","app\crud","app\api","app\api\v1","app\api\v1\endpoints" | ForEach-Object {
    if (!(Test-Path "$_\__init__.py")) { New-Item "$_\__init__.py" -ItemType File | Out-Null; "created $_\__init__.py" }
}
```

List every Python file to check nothing is missing:

```powershell
Get-ChildItem app -Recurse -Filter *.py | ForEach-Object { $_.FullName.Replace("$PWD\", "") }
```

### Database errors

Run `python check_db.py` first. It identifies most of these.

| Code | Message | Fix |
|---|---|---|
| **1045** | Access denied for user | Wrong `DB_USER` or `DB_PASSWORD`; check for trailing spaces in `.env` |
| **1049** | Unknown database | `DB_NAME` doesn't exist. Run `python list_dbs.py` and match the name exactly (case-sensitive), or create the database |
| **1044** | Access denied to database | Database may not exist, or the user lacks permission on it |
| **2003** | Can't connect to MySQL server | Security group doesn't allow your IP on 3306, instance isn't publicly accessible, or wrong `DB_HOST`/`DB_PORT` |
| **2005** | Unknown MySQL server host | `DB_HOST` is misspelled; copy the endpoint exactly from the RDS console |
| — | `cryptography package is required` | `pip install cryptography` |
| — | `Unknown column ... in 'field list'` | An existing table has different columns than the model. Use a separate database, rename `__tablename__`, or migrate the table |

> **Your IP changes?** If you're on home or mobile internet, your public IP can change, and the security group rule set to "My IP" stops matching. Update the inbound rule when you get error 2003 after it previously worked.

### Startup errors

| Problem | Fix |
|---|---|
| `Application startup failed` | Scroll up in the log to the last `OperationalError` line; it holds the actual database error |
| Port already in use | Another app is using the port. Run on a different one: `fastapi dev app/main.py --port 8081` |
| Changes to a model aren't reflected in the table | `create_all` doesn't alter existing tables. Use migrations (below) or drop and recreate the table in development |

---

## Production Notes

**SSL to RDS.** Download the AWS CA bundle from https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem, save it in the project, and set:
```dotenv
DB_SSL_CA=global-bundle.pem
```
`*.pem` files are already in `.gitignore`.

**Restrict public access.** In production, run the API inside the same VPC as RDS (for example on EC2 or ECS), set **Publicly accessible = No**, and only allow the API's security group on port 3306.

**Use a dedicated database user.** Rather than the master `admin` account, create a user with access only to this project's database:
```sql
CREATE USER 'app_user'@'%' IDENTIFIED BY 'strong_password';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX ON your_db_name.* TO 'app_user'@'%';
```

**Keep secrets out of code and chat.** Never commit `.env`, never paste credentials into tickets or chat. Rotate the RDS password if it was ever exposed. In AWS, consider Secrets Manager or SSM Parameter Store instead of a `.env` file.

**Migrations.** `create_all` is fine for getting started but cannot change existing tables. Once the schema starts evolving, switch to **Alembic** (the Python equivalent of Flyway/Liquibase):
```bash
pip install alembic
alembic init migrations
```
Then remove the `create_all` call from `app/main.py` and manage schema changes with `alembic revision --autogenerate` and `alembic upgrade head`.

**Turn off debug.** Set `DEBUG=false` in production so SQL queries aren't logged.