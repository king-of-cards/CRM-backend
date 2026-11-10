"""Creates the first admin user. Run once from the project root:  python create_admin.py"""
import getpass

from sqlmodel import Session

from app.crud import user as crud
from app.db.session import engine
from app.models.user import Role, UserCreate


def main() -> None:
    name = input("Admin name: ").strip()
    email = input("Admin email: ").strip()
    password = getpass.getpass("Password (min 8 characters, hidden while typing): ")

    with Session(engine) as db:
        if crud.get_user_by_email(db, email):
            print("A user with that email already exists. Nothing created.")
            return
        user = crud.create_user(
            db, UserCreate(name=name, email=email, password=password, role=Role.ADMIN)
        )
        print(f"Admin created: id={user.id}, email={user.email}")


if __name__ == "__main__":
    main()
