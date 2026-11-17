from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from .db import now_iso

SESSION_TTL_HOURS = 12


def random_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def password_hash(password: str) -> str:
    if len(password) < 12 or len(password) > 256:
        raise ValueError("Password must contain 12 to 256 characters.")
    salt = secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=1 << 14, r=8, p=1, dklen=64)
    return "scrypt$16384$8$1${}${}".format(salt.hex(), result.hex())


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, expected_hex = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(expected_hex)),
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected_hex))
    except (ValueError, TypeError, MemoryError):
        return False


def safe_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def issue_session(conn, user_id: str) -> tuple[str, str, str]:
    session_token, csrf_token = random_token(), random_token(24)
    expiry = (datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO sessions(token_hash, csrf_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (digest(session_token), digest(csrf_token), user_id, expiry, now_iso()),
    )
    return session_token, csrf_token, expiry


def session_user(conn, session_token: str | None) -> dict | None:
    if not session_token:
        return None
    row = conn.execute(
        "SELECT u.id, u.email, u.name, u.global_role, s.csrf_hash, s.expires_at "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0",
        (digest(session_token), now_iso()),
    ).fetchone()
    return dict(row) if row else None


def has_permission(user: dict, membership: dict | None, permission: str) -> bool:
    if user.get("global_role") == "super_admin":
        return True
    if not membership:
        return False
    if membership.get("role") == "owner":
        return True
    return bool(membership.get("permissions", {}).get(permission, False))


def csrf_valid(user: dict, submitted: str | None) -> bool:
    return bool(submitted and safe_equal(digest(submitted), user.get("csrf_hash", "")))


def verify_razorpay_callback(order_id: str, payment_id: str, signature: str, secret: str) -> bool:
    if not all((order_id, payment_id, signature, secret)):
        return False
    expected = hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return safe_equal(expected, signature)


def verify_razorpay_webhook(raw_body: bytes, signature: str, secret: str) -> bool:
    if not raw_body or not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return safe_equal(expected, signature)
