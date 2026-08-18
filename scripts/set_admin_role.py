"""Explicit local-only administrator role assignment.

This command is never exposed through HTTP. It requires a confirmed email and
an explicit confirmation flag so public registration can never create an
administrator.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("Refusing role change without --confirm")

    email = args.email.strip().lower()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            raise SystemExit("User not found")
        if user.email_verified_at is None or user.account_status != "active":
            raise SystemExit("Only an active, verified account can become an administrator")
        user.role = "admin"
        db.commit()
        print(f"Administrator role granted to user id {user.id}")


if __name__ == "__main__":
    main()
