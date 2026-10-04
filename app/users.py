"""Accounts are invite-only: they're created here, by someone with access to the server.

    python -m app.users create you@example.com
    python -m app.users login-link you@example.com   # a one-time link, valid 15 minutes
    python -m app.users list
"""
import argparse
import sys

from sqlalchemy import text

from app import auth
from app.config import APP_ORIGINS
from app.db import engine


def login_link(email: str) -> str:
    user = auth.find_user(email)
    if user is None:
        raise SystemExit(f"No user {auth.normalize_email(email)}: create it first.")
    # The page, not the API: opening a link only shows a button, and the sign-in happens
    # when it's pressed, so a mail scanner or chat preview fetching the link can't spend it.
    return f"{APP_ORIGINS[0]}/login?link={auth.create_login_link(user.id)}"


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="invite someone: they can then sign in with Google")
    create.add_argument("email")
    create.add_argument("--login-link", action="store_true", help="also print a one-time sign-in link")
    link = commands.add_parser("login-link", help="print a one-time sign-in link")
    link.add_argument("email")
    commands.add_parser("list", help="list users")
    args = parser.parse_args(argv)

    if args.command == "create":
        try:
            user = auth.create_user(args.email)
        except auth.UserExists:
            raise SystemExit(f"{auth.normalize_email(args.email)} already exists.")
        print(f"Created {user.email}. They can sign in with Google using that address.")
        if args.login_link:
            print(f"One-time sign-in link (15 minutes): {login_link(user.email)}")
    elif args.command == "login-link":
        print(login_link(args.email))
    elif args.command == "list":
        with engine.connect() as conn:
            for row in conn.execute(text("SELECT email, created_at FROM users ORDER BY created_at")):
                print(f"{row.email}  (since {row.created_at:%Y-%m-%d})")


if __name__ == "__main__":
    main(sys.argv[1:])
