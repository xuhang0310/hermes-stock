from __future__ import annotations

import argparse

from backend.app.core.database import initialize_default_database, session_scope
from backend.app.seed.seed import seed_demo


def main() -> int:
    parser = argparse.ArgumentParser(prog="hermes-stock-init-db")
    parser.add_argument(
        "--create-database",
        action="store_true",
        help="Create the MySQL database first. Requires a user with global CREATE DATABASE permission.",
    )
    args = parser.parse_args()

    initialize_default_database(create_database=args.create_database)
    with session_scope() as db:
        seed_demo(db)
    print("initialized mysql database and seeded demo data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
