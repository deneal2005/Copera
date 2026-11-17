#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import sqlite3
from pathlib import Path

from smarth_xerox import config
from smarth_xerox.accounts import create_super_admin, setup_shop
from smarth_xerox.db import connect, migrate


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart Xerox Management maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("setup", help="create the first shop and owner account")
    subparsers.add_parser("create-super-admin", help="create a platform administrator")
    backup_parser = subparsers.add_parser("backup", help="create a consistent SQLite backup")
    backup_parser.add_argument("--output", required=True, help="destination file")
    args = parser.parse_args()
    migrate()
    conn = connect()
    try:
        if args.command == "setup":
            name = input("Shop name: ").strip()
            slug = input("Public URL slug (lowercase, e.g. campus-copy): ").strip()
            email = input("Owner email: ").strip()
            owner_name = input("Owner name: ").strip() or "Shop owner"
            address = input("Shop address (optional): ").strip()
            phone = input("Shop phone (optional): ").strip()
            password = getpass.getpass("Owner password (12+ characters): ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                raise SystemExit("Passwords did not match.")
            with conn:
                shop_id, _ = setup_shop(conn, name, slug, email, password, owner_name, address, phone)
            print(f"Shop initialized ({shop_id[:8]}). Set rates in the owner dashboard before accepting orders.")
        elif args.command == "create-super-admin":
            email = input("Platform admin email: ").strip()
            name = input("Platform admin name: ").strip() or "Platform admin"
            password = getpass.getpass("Password (12+ characters): ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                raise SystemExit("Passwords did not match.")
            with conn:
                admin_id = create_super_admin(conn, email, name, password)
            print(f"Platform admin created ({admin_id[:8]}).")
        elif args.command == "backup":
            destination = Path(args.output).expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(destination)
            try:
                conn.backup(target)
            finally:
                target.close()
            print(f"Database backup written to {destination}")
    except sqlite3.IntegrityError as exc:
        raise SystemExit("Could not complete setup: email or shop URL is already in use.") from exc
    finally:
        conn.close()


if __name__ == "__main__":
    main()
