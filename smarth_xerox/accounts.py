from __future__ import annotations

import json
import re
import sqlite3
import uuid

from .db import now_iso
from .security import password_hash, verify_password

DEFAULT_SETTINGS = {
    "profile": {
        "address": "", "phone": "", "email": "", "instructions": "",
        "availability": "open", "service_available": True, "estimated_minutes": None,
    },
    "pricing": {
        "rates": {}, "minimum_order_paise": 0, "service_fee_paise": 0,
        "tax_rate_bps": 0, "discount_rate_bps": 0,
        "binding_per_copy_paise": 0, "stapling_per_copy_paise": 0,
    },
    "general": {
        "max_file_bytes": 10 * 1024 * 1024, "max_files": 10, "max_pages": 1000,
        "retention_days": 7, "currency": "INR", "timezone": "Asia/Kolkata",
        "cash_allowed": True, "formats": ["pdf", "jpg", "jpeg", "png"],
        "enabled_paper_sizes": ["A4"], "enabled_add_ons": [],
    },
    "printing": {
        "mode": "manual", "automatic_enabled": False, "manual_approval": True,
        "approve_color": True, "approve_large_jobs": True, "large_job_pages": 100,
        "max_pages_per_job": 200, "max_copies": 20, "retry_limit": 2,
        "default_printer": "", "color_printer": "",
    },
}

PERMISSION_NAMES = (
    "orders.view", "orders.download", "orders.manage", "printing.approve",
    "payments.manage", "reports.view", "prices.manage", "settings.manage", "staff.manage",
)


def log_action(conn: sqlite3.Connection, shop_id: str | None, actor_id: str | None,
               action: str, entity_type: str, entity_id: str | None = None, details: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(id, shop_id, actor_id, action, entity_type, entity_id, details_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), shop_id, actor_id, action, entity_type, entity_id,
         json.dumps(details or {}, separators=(",", ":")), now_iso()),
    )


def notify(conn: sqlite3.Connection, shop_id: str, audience: str, title: str,
           message: str, order_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO notifications(id, shop_id, order_id, audience, title, message, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), shop_id, order_id, audience, title[:100], message[:500], now_iso()),
    )


def setup_shop(conn: sqlite3.Connection, name: str, slug: str, email: str, password: str,
               owner_name: str = "Shop owner", address: str = "", phone: str = "") -> tuple[str, str]:
    if conn.execute("SELECT 1 FROM shops LIMIT 1").fetchone():
        raise ValueError("A shop is already configured. Use the owner dashboard to manage it.")
    clean_slug = slug.strip().lower()
    if len(clean_slug) > 60 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", clean_slug):
        raise ValueError("Shop URL must use lowercase letters, numbers, and hyphens.")
    clean_email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", clean_email):
        raise ValueError("Enter a valid owner email address.")
    if not name.strip() or len(name.strip()) > 120:
        raise ValueError("Shop name must contain 1 to 120 characters.")
    shop_id, user_id, timestamp = str(uuid.uuid4()), str(uuid.uuid4()), now_iso()
    hashed_password = password_hash(password)
    profile = {**DEFAULT_SETTINGS["profile"], "address": address.strip(), "phone": phone.strip(), "email": clean_email}
    conn.execute("INSERT INTO shops(id, slug, name, created_at) VALUES (?, ?, ?, ?)",
                 (shop_id, clean_slug, name.strip(), timestamp))
    conn.execute("INSERT INTO users(id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                 (user_id, clean_email, owner_name.strip() or "Shop owner", hashed_password, timestamp))
    conn.execute("INSERT INTO shop_members(shop_id, user_id, role, created_at) VALUES (?, ?, 'owner', ?)",
                 (shop_id, user_id, timestamp))
    for key, value in DEFAULT_SETTINGS.items():
        initial = profile if key == "profile" else value
        conn.execute(
            "INSERT INTO shop_settings(shop_id, setting_key, value_json, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)",
            (shop_id, key, json.dumps(initial, separators=(",", ":")), user_id, timestamp),
        )
    log_action(conn, shop_id, user_id, "shop.initialized", "shop", shop_id)
    return shop_id, user_id


def create_shop(conn: sqlite3.Connection, name: str, slug: str, email: str, password: str,
                owner_name: str, address: str = "", phone: str = "") -> tuple[str, str]:
    clean_slug = slug.strip().lower()
    if len(clean_slug) > 60 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", clean_slug):
        raise ValueError("Shop URL must use lowercase letters, numbers, and hyphens.")
    clean_email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", clean_email):
        raise ValueError("Enter a valid owner email address.")
    if not name.strip() or len(name.strip()) > 120:
        raise ValueError("Shop name must contain 1 to 120 characters.")
    shop_id, user_id, timestamp = str(uuid.uuid4()), str(uuid.uuid4()), now_iso()
    profile = {**DEFAULT_SETTINGS["profile"], "address": address.strip(), "phone": phone.strip(), "email": clean_email}
    conn.execute("INSERT INTO shops(id, slug, name, created_at) VALUES (?, ?, ?, ?)",
                 (shop_id, clean_slug, name.strip(), timestamp))
    conn.execute("INSERT INTO users(id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                 (user_id, clean_email, owner_name.strip() or "Shop owner", password_hash(password), timestamp))
    conn.execute("INSERT INTO shop_members(shop_id, user_id, role, created_at) VALUES (?, ?, 'owner', ?)",
                 (shop_id, user_id, timestamp))
    for key, value in DEFAULT_SETTINGS.items():
        initial = profile if key == "profile" else value
        conn.execute("INSERT INTO shop_settings(shop_id, setting_key, value_json, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (shop_id, key, json.dumps(initial, separators=(",", ":")), user_id, timestamp))
    log_action(conn, shop_id, None, "shop.created", "shop", shop_id)
    return shop_id, user_id


def create_super_admin(conn: sqlite3.Connection, email: str, name: str, password: str) -> str:
    clean_email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", clean_email):
        raise ValueError("Enter a valid admin email address.")
    user_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users(id, email, name, password_hash, global_role, created_at) VALUES (?, ?, ?, ?, 'super_admin', ?)",
        (user_id, clean_email, name.strip() or "Platform admin", password_hash(password), now_iso()),
    )
    log_action(conn, None, user_id, "admin.created", "user", user_id)
    return user_id


def authenticate(conn: sqlite3.Connection, email: str, password: str) -> dict | None:
    user = conn.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email.strip(),)).fetchone()
    if not user or user["disabled"] or not verify_password(password, user["password_hash"]):
        return None
    return dict(user)


def membership(conn: sqlite3.Connection, user_id: str, shop_id: str) -> dict | None:
    row = conn.execute(
        "SELECT role, permissions_json, is_active FROM shop_members WHERE shop_id = ? AND user_id = ?",
        (shop_id, user_id),
    ).fetchone()
    if not row or not row["is_active"]:
        return None
    return {"role": row["role"], "permissions": json.loads(row["permissions_json"] or "{}"), "is_active": bool(row["is_active"])}


def shop_settings(conn: sqlite3.Connection, shop_id: str) -> dict:
    result = json.loads(json.dumps(DEFAULT_SETTINGS))
    rows = conn.execute("SELECT setting_key, value_json FROM shop_settings WHERE shop_id = ?", (shop_id,))
    for row in rows:
        if row["setting_key"] in result:
            result[row["setting_key"]] = json.loads(row["value_json"])
    return result


def save_setting(conn: sqlite3.Connection, shop_id: str, key: str, value: dict,
                 actor_id: str, allowed_keys: tuple[str, ...]) -> None:
    if key not in allowed_keys:
        raise ValueError("This setting cannot be changed here.")
    if not isinstance(value, dict):
        raise ValueError("Settings must be sent as an object.")
    if key == "pricing":
        conn.execute(
            "INSERT INTO pricing_history(id, shop_id, actor_id, snapshot_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), shop_id, actor_id, json.dumps(value, separators=(",", ":")), now_iso()),
        )
    conn.execute(
        "INSERT INTO shop_settings(shop_id, setting_key, value_json, updated_by, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(shop_id, setting_key) DO UPDATE SET value_json=excluded.value_json, "
        "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (shop_id, key, json.dumps(value, separators=(",", ":")), actor_id, now_iso()),
    )
    log_action(conn, shop_id, actor_id, f"settings.{key}.updated", "shop_setting", key)
