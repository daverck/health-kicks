"""Operational CLI for the HealthKicks Cloud API.

Run locally (against any DATABASE_URL) or inside the container:

    uv run python -m app.cli promote-admin --first
    uv run python -m app.cli promote-admin ops@example.com
    docker compose exec api python -m app.cli promote-admin ops@example.com
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models import User, UserRole


def promote_admin(email: str | None, first: bool) -> int:
    """Promote one user (or the first registered user) to the admin role."""
    if bool(email) == bool(first):
        print("error: provide either an EMAIL or --first, not both/neither", file=sys.stderr)
        return 2

    with SessionLocal() as session:
        query = select(User).order_by(User.id)
        if email:
            query = query.where(User.email == email.strip().lower())
        else:
            query = query.limit(1)

        user = session.execute(query).scalars().first()
        if user is None:
            target = f"email '{email}'" if email else "database (no users registered yet)"
            print(f"error: no user found for {target}", file=sys.stderr)
            return 1

        if user.role == UserRole.admin:
            print(f"{user.email} (id={user.id}) is already an admin")
            return 0

        previous = user.role.value
        user.role = UserRole.admin
        session.commit()
        print(
            f"promoted {user.email} (id={user.id}): {previous} -> admin "
            f"[db={settings.database_url.split('@')[-1]}]"
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HealthKicks operational CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    promote = sub.add_parser("promote-admin", help="Grant the admin role to a user")
    promote.add_argument("email", nargs="?", help="Email of the user to promote")
    promote.add_argument(
        "--first",
        action="store_true",
        help="Promote the first registered user (bootstrap scenario)",
    )

    args = parser.parse_args()
    if args.command == "promote-admin":
        return promote_admin(args.email, args.first)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
