"""
Creates the first administrator, or resets one who is locked out.

There is no self-service way in, on purpose. A console that can create its own
first account is a console anyone who reaches the URL can create an account on,
and a migration that seeds a default password is a back door that ships to
every environment and is remembered in none of them. Bootstrapping is a thing
someone does at a shell, with the database credentials, once.

    python scripts/create_admin.py --email you@ardena.co.ke --role owner

The password is read from the terminal without echoing it. Pass ``--password``
instead only in a script, and know that it lands in the shell history.

Re-running with an email that already exists resets that account's password and
role rather than failing, which is the locked-out-owner recovery path.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.db.session import SessionLocal, dispose_engine
from app.models.admin import AdminUser
from app.services import admin_auth


def _read_password(given: str | None) -> str:
    if given:
        return given

    first = getpass.getpass("Password (at least 12 characters): ")
    second = getpass.getpass("Again: ")

    if first != second:
        print("Those do not match.")
        raise SystemExit(1)

    return first


async def run(email: str, password: str, full_name: str, role: str) -> int:
    try:
        return await _run(email, password, full_name, role)
    finally:
        # Inside the same event loop the pool was opened on. Disposing from a
        # second ``asyncio.run`` would be tearing down asyncpg connections that
        # belong to a loop that has already closed.
        await dispose_engine()


async def _run(email: str, password: str, full_name: str, role: str) -> int:
    async with SessionLocal() as session:
        existing = await session.scalar(
            select(AdminUser).where(
                AdminUser.email == admin_auth.normalise_email(email)
            )
        )

        if existing is not None:
            admin_auth.assert_password_ok(password)
            await admin_auth.set_password(
                session, admin=existing, password=password
            )
            existing.role = role
            existing.is_active = True
            if full_name:
                existing.full_name = full_name
            await session.commit()
            print(
                f"Reset {existing.email} ({existing.role}). "
                "Every existing session for that account has been revoked."
            )
            return 0

        admin = await admin_auth.create_admin(
            session,
            email=email,
            password=password,
            full_name=full_name,
            role=role,
        )
        await session.commit()
        print(f"Created {admin.email} ({admin.role}).")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="", help="Display name.")
    parser.add_argument(
        "--role",
        default="owner",
        choices=admin_auth.ROLES,
        help="Defaults to owner, since the first admin has to be able to add "
        "the others.",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Skips the prompt. Ends up in your shell history — prefer the prompt.",
    )
    args = parser.parse_args()

    password = _read_password(args.password)

    try:
        return asyncio.run(run(args.email, password, args.name, args.role))
    except Exception as error:  # noqa: BLE001 — a CLI, and the message is the point
        print(f"Failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
