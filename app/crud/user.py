from sqlmodel import Session, select

from app.models.user import User, UserCreate


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.exec(select(User).where(User.email == email)).first()


def list_users(db: Session, skip: int = 0, limit: int = 100) -> list[User]:
    return list(db.exec(select(User).offset(skip).limit(limit)).all())


def create_user(db: Session, data: UserCreate) -> User:
    user = User.model_validate(data)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
    