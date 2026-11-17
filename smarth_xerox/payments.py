from __future__ import annotations

import base64
import json
import sqlite3
import re
import urllib.error
import urllib.request
import uuid

from . import config
from .accounts import log_action, notify, shop_settings
from .db import atomic, now_iso
from .printing import create_print_jobs
from .security import digest, verify_razorpay_callback, verify_razorpay_webhook

RAZORPAY_API = "https://api.razorpay.com/v1"


class PaymentGatewayError(Exception):
    pass


def _razorpay_request(method: str, path: str, payload: dict | None = None,
                      extra_headers: dict[str, str] | None = None) -> dict:
    if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
        raise PaymentGatewayError("Online payment is not configured for this installation.")
    auth = base64.b64encode(f"{config.RAZORPAY_KEY_ID}:{config.RAZORPAY_KEY_SECRET}".encode()).decode()
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{RAZORPAY_API}{path}", data=body, method=method,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json", **(extra_headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(65536)).get("error", {}).get("description", "")
        except Exception:
            detail = ""
        raise PaymentGatewayError(detail[:200] or "The payment provider could not process this request.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise PaymentGatewayError("Could not reach the payment provider. You can retry payment or pay at the counter.") from exc
    if not isinstance(result, dict):
        raise PaymentGatewayError("The payment provider returned an invalid response.")
    return result


def create_razorpay_attempt(conn: sqlite3.Connection, shop_id: str, order_id: str,
                            tracking_token: str, idempotency_key: str) -> dict:
    if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
        raise PaymentGatewayError("Online payment is not configured. Choose payment at the counter.")
    order = conn.execute(
        "SELECT o.id, o.ticket, o.total_paise, o.payment_status, o.status, o.payment_method, s.name "
        "FROM orders o JOIN shops s ON s.id = o.shop_id WHERE o.shop_id = ? AND o.id = ? AND o.tracking_token_hash = ?",
        (shop_id, order_id, digest(tracking_token)),
    ).fetchone()
    if not order:
        raise ValueError("Order not found.")
    if order["payment_status"] in ("paid", "refunded"):
        raise ValueError("This order is already paid or refunded.")
    if order["status"] != "awaiting_payment":
        raise ValueError("This order is no longer accepting payment.")
    if order["payment_method"] != "razorpay":
        raise ValueError("This order is set to payment at the counter.")
    if order["total_paise"] < 100:
        raise ValueError("Razorpay orders must be at least ₹1.00. Configure the minimum shop charge.")
    existing = conn.execute(
        "SELECT * FROM payments WHERE shop_id = ? AND idempotency_key = ?",
        (shop_id, idempotency_key),
    ).fetchone()
    if existing:
        if existing["order_id"] != order_id:
            raise ValueError("This payment request key belongs to a different order.")
        if existing["provider_order_id"]:
            return {"payment_id": existing["id"], "razorpay_order_id": existing["provider_order_id"],
                    "amount": existing["amount_paise"], "currency": "INR", "key_id": config.RAZORPAY_KEY_ID,
                    "shop_name": order["name"], "ticket": order["ticket"]}
        raise PaymentGatewayError("A payment attempt is already being prepared. Retry with a new payment attempt.")
    payment_id = str(uuid.uuid4())
    timestamp = now_iso()
    conn.execute(
        "INSERT INTO payments(id, shop_id, order_id, provider, amount_paise, status, idempotency_key, created_at, updated_at) "
        "VALUES (?, ?, ?, 'razorpay', ?, 'creating', ?, ?, ?)",
        (payment_id, shop_id, order_id, order["total_paise"], idempotency_key, timestamp, timestamp),
    )
    try:
        remote = _razorpay_request("POST", "/orders", {
            "amount": order["total_paise"], "currency": "INR", "receipt": payment_id.replace("-", ""),
            "notes": {"ticket": order["ticket"]},
        })
        if remote.get("amount") != order["total_paise"] or remote.get("currency") != "INR" or not remote.get("id"):
            raise PaymentGatewayError("The payment provider order did not match the expected amount.")
    except PaymentGatewayError:
        conn.execute("UPDATE payments SET status = 'failed', updated_at = ? WHERE id = ? AND status = 'creating'",
                     (now_iso(), payment_id))
        raise
    conn.execute(
        "UPDATE payments SET provider_order_id = ?, status = 'pending', updated_at = ? WHERE id = ?",
        (remote["id"], now_iso(), payment_id),
    )
    log_action(conn, shop_id, None, "payment.order_created", "payment", payment_id,
               {"provider_order_id": remote["id"]})
    return {"payment_id": payment_id, "razorpay_order_id": remote["id"], "amount": order["total_paise"],
            "currency": "INR", "key_id": config.RAZORPAY_KEY_ID, "shop_name": order["name"],
            "ticket": order["ticket"]}


def _capture(conn: sqlite3.Connection, payment: sqlite3.Row, provider_payment_id: str) -> str:
    if payment["status"] == "captured" and payment["provider_payment_id"] == provider_payment_id:
        return "already_paid"
    order = conn.execute("SELECT payment_status, status FROM orders WHERE shop_id = ? AND id = ?",
                         (payment["shop_id"], payment["order_id"])).fetchone()
    if order and order["payment_status"] in ("paid", "refunded"):
        conn.execute("UPDATE payments SET status = 'duplicate_captured', provider_payment_id = ?, updated_at = ? WHERE id = ?",
                     (provider_payment_id, now_iso(), payment["id"]))
        notify(conn, payment["shop_id"], "owner", "Possible duplicate payment",
               "A second payment was captured for an order already marked paid; review the payment and refund if needed.", payment["order_id"])
        return "duplicate_captured"
    conn.execute("UPDATE payments SET status = 'captured', provider_payment_id = ?, updated_at = ? WHERE id = ?",
                 (provider_payment_id, now_iso(), payment["id"]))
    if order and order["status"] == "cancelled":
        conn.execute("UPDATE orders SET payment_status = 'paid', updated_at = ? WHERE shop_id = ? AND id = ?",
                     (now_iso(), payment["shop_id"], payment["order_id"]))
        notify(conn, payment["shop_id"], "owner", "Payment received for a cancelled order",
               "A payment completed after this ticket was cancelled. Review the payment and refund the customer.", payment["order_id"])
        return "paid_cancelled"
    conn.execute("UPDATE orders SET payment_status = 'paid', status = 'confirmed', updated_at = ? WHERE shop_id = ? AND id = ?",
                 (now_iso(), payment["shop_id"], payment["order_id"]))
    notify(conn, payment["shop_id"], "owner", "Payment received", "A print order has been paid and is ready for review.", payment["order_id"])
    _queue_automatic_if_allowed(conn, payment["shop_id"], payment["order_id"])
    return "paid"


def _queue_automatic_if_allowed(conn: sqlite3.Connection, shop_id: str, order_id: str) -> None:
    settings = shop_settings(conn, shop_id)
    printing = settings["printing"]
    if printing.get("mode") != "automatic" or not printing.get("automatic_enabled") or printing.get("manual_approval", True):
        return
    documents = conn.execute("SELECT page_count, settings_json FROM documents WHERE shop_id = ? AND order_id = ?",
                             (shop_id, order_id)).fetchall()
    requires_review = any(
        (json.loads(row["settings_json"]).get("color") == "color" and printing.get("approve_color", True))
        or ((row["page_count"] or 0) >= printing.get("large_job_pages", 100) and printing.get("approve_large_jobs", True))
        for row in documents
    )
    if requires_review:
        notify(conn, shop_id, "owner", "Print approval required",
               "A paid order has color or high-page-count print settings that need owner approval before automatic printing.", order_id)
        return
    try:
        create_print_jobs(conn, shop_id, order_id, "automatic", None, settings, False)
    except ValueError as error:
        notify(conn, shop_id, "owner", "Automatic print job was not queued", str(error), order_id)


def verify_callback(conn: sqlite3.Connection, shop_id: str, order_id: str, tracking_token: str,
                    provider_order_id: str, provider_payment_id: str, signature: str) -> dict:
    if not verify_razorpay_callback(provider_order_id, provider_payment_id, signature, config.RAZORPAY_KEY_SECRET):
        raise ValueError("Payment could not be verified. The order remains unpaid.")
    payment = conn.execute(
        "SELECT * FROM payments WHERE shop_id = ? AND order_id = ? AND provider_order_id = ? AND provider = 'razorpay'",
        (shop_id, order_id, provider_order_id),
    ).fetchone()
    order = conn.execute(
        "SELECT id, ticket, total_paise FROM orders WHERE shop_id = ? AND id = ? AND tracking_token_hash = ?",
        (shop_id, order_id, digest(tracking_token)),
    ).fetchone()
    if not payment or not order:
        raise ValueError("Payment could not be linked to this order.")
    remote = _razorpay_request("GET", f"/payments/{provider_payment_id}")
    if (remote.get("order_id") != provider_order_id or remote.get("amount") != order["total_paise"]
            or remote.get("currency") != "INR" or remote.get("status") != "captured"):
        raise ValueError("The payment is not captured for the expected amount. The order remains unpaid.")
    with atomic(conn):
        current = conn.execute(
            "SELECT * FROM payments WHERE shop_id = ? AND order_id = ? AND provider_order_id = ? AND provider = 'razorpay'",
            (shop_id, order_id, provider_order_id),
        ).fetchone()
        current_order = conn.execute(
            "SELECT * FROM orders WHERE shop_id = ? AND id = ? AND tracking_token_hash = ?",
            (shop_id, order_id, digest(tracking_token)),
        ).fetchone()
        if not current or not current_order or current["amount_paise"] != current_order["total_paise"]:
            raise ValueError("Payment could not be linked to this order.")
        result = _capture(conn, current, provider_payment_id)
    return {"ticket": order["ticket"], "payment_status": "paid" if result in ("paid", "already_paid", "paid_cancelled") else result}


def process_webhook(conn: sqlite3.Connection, raw_body: bytes, signature: str, event_id: str | None) -> str:
    if not verify_razorpay_webhook(raw_body, signature, config.RAZORPAY_WEBHOOK_SECRET):
        raise ValueError("Invalid payment webhook signature.")
    if not event_id or len(event_id) > 200:
        raise ValueError("Missing payment webhook event ID.")
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid payment webhook body.") from exc
    event_type = event.get("event", "")
    payload = event.get("payload", {})
    payment_entity = payload.get("payment", {}).get("entity", {})
    if not payment_entity and event_type == "order.paid":
        payment_entity = payload.get("payment", {}).get("entity", {})
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO webhook_events(event_id, event_type, received_at) VALUES (?, ?, ?)",
            (event_id, event_type[:100], now_iso()),
        ).rowcount
        if not inserted:
            return "duplicate"
        if event_type in ("refund.processed", "refund.failed"):
            refund_entity = payload.get("refund", {}).get("entity", {})
            provider_refund_id = refund_entity.get("id")
            refund = conn.execute("SELECT * FROM refunds WHERE provider_refund_id = ?", (provider_refund_id,)).fetchone()
            if refund:
                status = "processed" if event_type == "refund.processed" else "failed"
                conn.execute("UPDATE refunds SET status = ? WHERE id = ?", (status, refund["id"]))
                if status == "processed":
                    totals = conn.execute(
                        "SELECT p.shop_id, p.order_id, p.amount_paise, "
                        "(SELECT coalesce(sum(r.amount_paise),0) FROM refunds r WHERE r.payment_id = p.id "
                        "AND r.status IN ('processed','completed')) AS refunded "
                        "FROM payments p WHERE p.id = ?", (refund["payment_id"],),
                    ).fetchone()
                    if totals and totals["refunded"] >= totals["amount_paise"]:
                        conn.execute("UPDATE orders SET payment_status = 'refunded', updated_at = ? "
                                     "WHERE shop_id = ? AND id = ?",
                                     (now_iso(), totals["shop_id"], totals["order_id"]))
                conn.execute("UPDATE webhook_events SET processed_at = ? WHERE event_id = ?", (now_iso(), event_id))
                return status
            conn.execute("UPDATE webhook_events SET processed_at = ? WHERE event_id = ?", (now_iso(), event_id))
            return "unmatched"
        provider_order_id = payment_entity.get("order_id")
        provider_payment_id = payment_entity.get("id")
        if not provider_order_id or not provider_payment_id:
            conn.execute("UPDATE webhook_events SET processed_at = ? WHERE event_id = ?", (now_iso(), event_id))
            return "ignored"
        payment = conn.execute(
            "SELECT * FROM payments WHERE provider = 'razorpay' AND provider_order_id = ?",
            (provider_order_id,),
        ).fetchone()
        if not payment:
            conn.execute("UPDATE webhook_events SET processed_at = ? WHERE event_id = ?", (now_iso(), event_id))
            return "unmatched"
        order = conn.execute("SELECT total_paise FROM orders WHERE shop_id = ? AND id = ?",
                             (payment["shop_id"], payment["order_id"])).fetchone()
        if payment_entity.get("amount") != order["total_paise"] or payment_entity.get("currency") != "INR":
            conn.execute("UPDATE payments SET status = 'amount_mismatch', updated_at = ? WHERE id = ?",
                         (now_iso(), payment["id"]))
            log_action(conn, payment["shop_id"], None, "payment.amount_mismatch", "payment", payment["id"])
            result = "mismatch"
        elif event_type in ("payment.captured", "order.paid") and payment_entity.get("status") in ("captured", None):
            result = _capture(conn, payment, provider_payment_id)
        elif event_type == "payment.failed" and payment["status"] != "captured":
            conn.execute("UPDATE payments SET status = 'failed', provider_payment_id = ?, updated_at = ? WHERE id = ?",
                         (provider_payment_id, now_iso(), payment["id"]))
            result = "failed"
        elif event_type == "payment.authorized" and payment["status"] not in ("captured", "duplicate_captured"):
            conn.execute("UPDATE payments SET status = 'authorized', provider_payment_id = ?, updated_at = ? WHERE id = ?",
                         (provider_payment_id, now_iso(), payment["id"]))
            result = "authorized"
        else:
            result = "ignored"
        conn.execute("UPDATE webhook_events SET processed_at = ? WHERE event_id = ?", (now_iso(), event_id))
        return result


def mark_counter_paid(conn: sqlite3.Connection, shop_id: str, order_id: str, actor_id: str) -> dict:
    order = conn.execute("SELECT id, ticket, payment_status, payment_method, status FROM orders WHERE shop_id = ? AND id = ?",
                         (shop_id, order_id)).fetchone()
    if not order:
        raise ValueError("Order not found.")
    if order["payment_method"] != "counter":
        raise ValueError("This order is not configured for counter payment.")
    if order["status"] == "cancelled":
        raise ValueError("A cancelled order cannot receive a counter payment.")
    if order["payment_status"] == "refunded":
        raise ValueError("A fully refunded order cannot be marked paid again.")
    if order["payment_status"] == "paid":
        return dict(order)
    if order["status"] != "awaiting_payment":
        raise ValueError("This order is not currently accepting payment.")
    timestamp = now_iso()
    conn.execute("UPDATE payments SET status = 'paid', updated_at = ? WHERE shop_id = ? AND order_id = ? AND provider = 'counter' AND status = 'pending'",
                 (timestamp, shop_id, order_id))
    conn.execute("UPDATE orders SET payment_status = 'paid', status = 'confirmed', updated_at = ? WHERE shop_id = ? AND id = ?",
                 (timestamp, shop_id, order_id))
    log_action(conn, shop_id, actor_id, "payment.counter_received", "order", order_id)
    notify(conn, shop_id, "owner", "Counter payment recorded", f"{order['ticket']} is now paid.", order_id)
    _queue_automatic_if_allowed(conn, shop_id, order_id)
    return {**dict(order), "payment_status": "paid", "status": "confirmed"}


def create_refund(conn: sqlite3.Connection, shop_id: str, order_id: str, amount_paise: int,
                  reason: str, idempotency_key: str, actor_id: str) -> dict:
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int) or amount_paise <= 0:
        raise ValueError("Refund amount must be a positive whole number of paise.")
    if not isinstance(reason, str):
        raise ValueError("Refund reason must be text.")
    reason = reason.strip()
    if len(reason) > 240:
        raise ValueError("Refund reason must be 240 characters or fewer.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,36}", idempotency_key or ""):
        raise ValueError("Refund request key must contain 10 to 36 letters, numbers, hyphens, or underscores.")

    existing = conn.execute("SELECT * FROM refunds WHERE shop_id = ? AND idempotency_key = ?",
                            (shop_id, idempotency_key)).fetchone()
    if existing:
        refund = dict(existing)
        if refund["amount_paise"] != amount_paise or refund["reason"] != reason:
            raise ValueError("This refund request key was already used with different details.")
        payment = conn.execute("SELECT * FROM payments WHERE shop_id = ? AND id = ?",
                               (shop_id, refund["payment_id"])).fetchone()
        if not payment or payment["order_id"] != order_id:
            raise ValueError("The payment record for this refund is unavailable.")
    else:
        with atomic(conn):
            payment = conn.execute(
                "SELECT p.* FROM payments p JOIN orders o ON o.id = p.order_id AND o.shop_id = p.shop_id "
                "WHERE p.shop_id = ? AND p.order_id = ? AND o.payment_status = 'paid' "
                "AND p.status IN ('captured','paid') ORDER BY CASE p.provider WHEN 'razorpay' THEN 0 ELSE 1 END, p.created_at DESC LIMIT 1",
                (shop_id, order_id),
            ).fetchone()
            if not payment:
                raise ValueError("This order has no verified payment available to refund.")
            if payment["provider"] == "razorpay" and not payment["provider_payment_id"]:
                raise ValueError("The verified online transaction reference is missing.")
            if payment["provider"] == "razorpay" and amount_paise < 100:
                raise ValueError("Razorpay refunds must be at least ₹1.00.")
            reserved = conn.execute(
                "SELECT coalesce(sum(amount_paise),0) FROM refunds WHERE shop_id = ? AND payment_id = ? "
                "AND status NOT IN ('failed','rejected')", (shop_id, payment["id"]),
            ).fetchone()[0]
            if amount_paise > payment["amount_paise"] - reserved:
                raise ValueError("Refund amount exceeds the remaining refundable balance.")
            refund_id = str(uuid.uuid4())
            timestamp = now_iso()
            status = "processed" if payment["provider"] == "counter" else "processing"
            conn.execute(
                "INSERT INTO refunds(id, shop_id, payment_id, amount_paise, provider_refund_id, status, "
                "created_at, idempotency_key, reason) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (refund_id, shop_id, payment["id"], amount_paise, status, timestamp, idempotency_key, reason),
            )
            from .accounts import log_action
            log_action(conn, shop_id, actor_id, "refund.counter_recorded" if payment["provider"] == "counter" else "refund.requested",
                       "refund", refund_id, {"amount_paise": amount_paise, "reason": reason})
            if payment["provider"] == "counter":
                if reserved + amount_paise >= payment["amount_paise"]:
                    conn.execute("UPDATE orders SET payment_status = 'refunded', updated_at = ? "
                                 "WHERE shop_id = ? AND id = ?", (timestamp, shop_id, order_id))
                return dict(conn.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone())
            existing = conn.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone()
            payment = conn.execute("SELECT * FROM payments WHERE id = ?", (payment["id"],)).fetchone()
            refund = dict(existing)

    if payment["provider"] == "counter":
        return dict(existing)
    if refund.get("provider_refund_id") and refund["status"] != "processing":
        return refund
    remote = _razorpay_request(
        "POST", f"/payments/{payment['provider_payment_id']}/refund",
        {"amount": amount_paise, "receipt": refund["id"].replace("-", ""),
         "notes": {"shop_refund_id": refund["id"], "reason": reason[:240]}},
        {"X-Refund-Idempotency": idempotency_key},
    )
    if remote.get("amount") != amount_paise or not remote.get("id"):
        raise PaymentGatewayError("The payment provider refund response did not match the requested amount.")
    status = "processed" if remote.get("status") == "processed" else "pending"
    conn.execute("UPDATE refunds SET provider_refund_id = ?, status = ? WHERE id = ? AND shop_id = ?",
                 (remote["id"], status, refund["id"], shop_id))
    if status == "processed":
        total = conn.execute("SELECT coalesce(sum(amount_paise),0) FROM refunds WHERE payment_id = ? "
                             "AND status IN ('processed','completed')", (refund["payment_id"],)).fetchone()[0]
        if total >= payment["amount_paise"]:
            conn.execute("UPDATE orders SET payment_status = 'refunded', updated_at = ? WHERE shop_id = ? AND id = ?",
                         (now_iso(), shop_id, order_id))
    return dict(conn.execute("SELECT * FROM refunds WHERE id = ? AND shop_id = ?",
                             (refund["id"], shop_id)).fetchone())
