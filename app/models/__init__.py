# Import every table model here so SQLModel.metadata knows about all tables
from app.models.user import User, UserCreate, UserRead  # noqa: F401
