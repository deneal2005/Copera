from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from .accounts import notify
from .db import now_iso
from .pricing import quote_order
from .security import digest, random_token

DEFAULT_ORDER_STATUS = "awaiting_payment"


def create_draft(conn, shop_id: str, idempotency_key: str) -> tuple[dict, str]:
    existing = conn.execute(
        "SELECT id, token_hash, expires_at FROM drafts WHERE shop_id = ? AND idempotency_key = ?",
        (shop_id, idempotency_key),
    ).fetchone()
    if existing:
        raise ValueError("This upload session already exists. Reload the page to start a fresh order.")
    draft_id, token = str(uuid.uuid4()), random_token(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO drafts(id, token_hash, shop_id, idempotency_key, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (draft_id, digest(token), shop_id, idempotency_key, expires, now_iso()),
    )
    return {"id": draft_id, "expires_at": expires}, token


def find_draft(conn, shop_id: str, token: str) -> dict | None:
    row = conn.execute(
        "SELECT id, shop_id, token_hash FROM drafts WHERE shop_id = ? AND token_hash = ? AND expires_at > ?",
        (shop_id, digest(token), now_iso()),
    ).fetchone()
    return dict(row) if row else None


def draft_documents(conn, shop_id: str, draft_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, original_name, size_bytes, page_count, needs_review, settings_json FROM documents "
        "WHERE shop_id = ? AND draft_id = ? AND order_id IS NULL ORDER BY created_at, id",
        (shop_id, draft_id),
    )
    return [{**dict(row), "settings": json.loads(row["settings_json"])} for row in rows]


def update_document_settings(conn, shop_id: str, draft_id: str, document_id: str, settings: dict,
                              shop_config: dict) -> dict:
    allowed = {"color", "sides", "paper_size", "copies", "page_selection", "add_ons", "orientation", "instructions"}
    if set(settings) - allowed:
        raise ValueError("The document settings contain unsupported fields.")
    if settings.get("color", "bw") not in ("bw", "color") or settings.get("sides", "single") not in ("single", "double"):
        raise ValueError("Choose supported print settings.")
    if settings.get("paper_size", "A4") not in shop_config["general"].get("enabled_paper_sizes", ["A4"]):
        raise ValueError("This paper size is not enabled by the shop.")
    copies = settings.get("copies", 1)
    maximum = shop_config["printing"].get("max_copies", 20)
    if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= maximum:
        raise ValueError(f"Copies must be between 1 and {maximum}.")
    add_ons = settings.get("add_ons", [])
    enabled = set(shop_config["general"].get("enabled_add_ons", []))
    if not isinstance(add_ons, list) or set(add_ons) - enabled:
        raise ValueError("A selected additional service is not offered by the shop.")
    if len(str(settings.get("instructions", ""))) > 500:
        raise ValueError("Custom instructions must be 500 characters or fewer.")
    row = conn.execute(
        "SELECT id, settings_json FROM documents WHERE shop_id = ? AND draft_id = ? AND id = ? AND order_id IS NULL",
        (shop_id, draft_id, document_id),
    ).fetchone()
    if not row:
        raise ValueError("Document not found in this upload session.")
    updated = {**json.loads(row["settings_json"]), **settings}
    conn.execute("UPDATE documents SET settings_json = ? WHERE shop_id = ? AND id = ?",
                 (json.dumps(updated, separators=(",", ":")), shop_id, document_id))
    return updated


def quote_draft(conn, shop_id: str, draft_id: str, shop_config: dict) -> dict:
    docs = draft_documents(conn, shop_id, draft_id)
    return quote_order(shop_config["pricing"], docs)


def _ticket(conn, shop_id: str) -> str:
    year = datetime.now(timezone.utc).year
    row = conn.execute(
        "INSERT INTO ticket_counters(shop_id, year, next_number) VALUES (?, ?, 2) "
        "ON CONFLICT(shop_id, year) DO UPDATE SET next_number=next_number+1 RETURNING next_number - 1 AS number",
        (shop_id, year),
    ).fetchone()
    return f"XRX-{year}-{row['number']:05d}"


def create_order(conn, shop: dict, draft: dict, draft_token: str, customer_name: str,
                 customer_phone: str, payment_method: str, idempotency_key: str,
                 shop_config: dict) -> tuple[dict, str, bool]:
    request_payload = {
        "draft_id": draft["id"], "customer_name": customer_name.strip(),
        "customer_phone": customer_phone.strip(), "payment_method": payment_method,
    }
    request_hash = hashlib.sha256(json.dumps(request_payload, sort_keys=True).encode()).hexdigest()
    prior = conn.execute("SELECT id, request_hash, ticket, status, payment_status, total_paise FROM orders "
                         "WHERE shop_id = ? AND idempotency_key = ?", (shop["id"], idempotency_key)).fetchone()
    if prior:
        if prior["request_hash"] != request_hash:
            raise ValueError("This order request key was already used with different details.")
        return dict(prior), draft_token, True
    if shop.get("status") != "active":
        raise ValueError("This shop is not accepting orders right now.")
    profile = shop_config["profile"]
    if not profile.get("service_available", True):
        raise ValueError("This shop is currently unavailable for orders.")
    documents = draft_documents(conn, shop["id"], draft["id"])
    if not documents:
        raise ValueError("Add at least one document before placing an order.")
    if len(documents) > shop_config["general"].get("max_files", 10):
        raise ValueError("This order has more files than the shop allows.")
    quote = quote_order(shop_config["pricing"], documents)
    if quote["needs_review"]:
        if payment_method != "review":
            raise ValueError("A document page count needs shop verification. Request a price review before choosing payment.")
    else:
        if payment_method not in ("counter", "razorpay"):
            raise ValueError("Choose an available payment method.")
        if payment_method == "counter" and not shop_config["general"].get("cash_allowed", False):
            raise ValueError("Counter payment is not enabled by this shop.")
    order_id, ticket, timestamp = str(uuid.uuid4()), _ticket(conn, shop["id"]), now_iso()
    start_status = "payment_verification" if quote["needs_review"] else "awaiting_payment"
    conn.execute(
        "INSERT INTO orders(id, shop_id, ticket, tracking_token_hash, customer_name, customer_phone, status, "
        "payment_status, payment_method, subtotal_paise, tax_paise, total_paise, pricing_snapshot_json, "
        "idempotency_key, request_hash, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, shop["id"], ticket, draft["token_hash"], customer_name.strip() or None,
         customer_phone.strip() or None, start_status, payment_method, quote["subtotal_paise"],
         quote["tax_paise"], quote["total_paise"], json.dumps(quote, separators=(",", ":")),
         idempotency_key, request_hash, timestamp, timestamp),
    )
    conn.execute("UPDATE documents SET order_id = ?, draft_id = NULL WHERE shop_id = ? AND draft_id = ?",
                 (order_id, shop["id"], draft["id"]))
    if payment_method == "counter":
        conn.execute(
            "INSERT INTO payments(id, shop_id, order_id, provider, amount_paise, status, idempotency_key, created_at, updated_at) "
            "VALUES (?, ?, ?, 'counter', ?, 'pending', ?, ?, ?)",
            (str(uuid.uuid4()), shop["id"], order_id, quote["total_paise"], f"counter:{order_id}", timestamp, timestamp),
        )
    notify(conn, shop["id"], "owner", "New print order",
           f"{ticket} needs page-count review before its final price and payment can be confirmed." if quote["needs_review"]
           else f"{ticket} is waiting for payment verification.", order_id)
    return {
        "id": order_id, "ticket": ticket, "status": start_status, "payment_status": "pending",
        "total_paise": quote["total_paise"], "payment_method": payment_method,
        "items": quote["items"], "needs_review": quote["needs_review"], "created_at": timestamp,
    }, draft_token, False


def finalize_page_count_review(conn, shop_id: str, order_id: str, page_counts: dict,
                               payment_method: str, shop_config: dict, actor_id: str) -> dict:
    if not isinstance(page_counts, dict):
        raise ValueError("Page counts must be provided for every document awaiting review.")
    order = conn.execute("SELECT * FROM orders WHERE shop_id = ? AND id = ?", (shop_id, order_id)).fetchone()
    if not order:
        raise ValueError("Order not found.")
    if order["status"] != "payment_verification" or order["payment_status"] != "pending":
        raise ValueError("This order is not waiting for page-count review.")
    unknown = conn.execute("SELECT id FROM documents WHERE shop_id = ? AND order_id = ? AND needs_review = 1",
                           (shop_id, order_id)).fetchall()
    expected = {row["id"] for row in unknown}
    if not expected or set(page_counts) != expected:
        raise ValueError("Enter a verified page count for each document awaiting review.")
    max_pages = shop_config["general"].get("max_pages", 1000)
    for document_id in expected:
        pages = page_counts[document_id]
        if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= max_pages:
            raise ValueError(f"Page counts must be whole numbers between 1 and {max_pages}.")
        conn.execute("UPDATE documents SET page_count = ?, needs_review = 0 WHERE shop_id = ? AND order_id = ? AND id = ?",
                     (pages, shop_id, order_id, document_id))
    if payment_method not in ("counter", "razorpay"):
        raise ValueError("Choose how the customer will pay after the price is confirmed.")
    if payment_method == "counter" and not shop_config["general"].get("cash_allowed", False):
        raise ValueError("Counter payment is disabled for this shop.")
    if payment_method == "razorpay":
        from . import config
        if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
            raise ValueError("Online payment is not configured for this installation.")
    documents = []
    for row in conn.execute("SELECT id, original_name, page_count, settings_json FROM documents "
                             "WHERE shop_id = ? AND order_id = ? ORDER BY created_at, id", (shop_id, order_id)):
        documents.append({**dict(row), "settings": json.loads(row["settings_json"])})
    quote = quote_order(shop_config["pricing"], documents)
    if quote["needs_review"]:
        raise ValueError("One or more page counts still need review.")
    timestamp = now_iso()
    conn.execute("UPDATE orders SET status = 'awaiting_payment', payment_method = ?, subtotal_paise = ?, tax_paise = ?, "
                 "total_paise = ?, pricing_snapshot_json = ?, updated_at = ? WHERE shop_id = ? AND id = ?",
                 (payment_method, quote["subtotal_paise"], quote["tax_paise"], quote["total_paise"],
                  json.dumps(quote, separators=(",", ":")), timestamp, shop_id, order_id))
    if payment_method == "counter":
        conn.execute("INSERT INTO payments(id, shop_id, order_id, provider, amount_paise, status, idempotency_key, created_at, updated_at) "
                     "VALUES (?, ?, ?, 'counter', ?, 'pending', ?, ?, ?)",
                     (str(uuid.uuid4()), shop_id, order_id, quote["total_paise"], f"counter:{order_id}", timestamp, timestamp))
    from .accounts import log_action
    log_action(conn, shop_id, actor_id, "order.page_count_reviewed", "order", order_id,
               {"page_counts": page_counts, "payment_method": payment_method, "total_paise": quote["total_paise"]})
    notify(conn, shop_id, "customer", "Price confirmed",
           f"The shop confirmed your document pages. The order total is ₹{quote['total_paise'] / 100:.2f}. "
           + ("Pay at the counter when you arrive." if payment_method == "counter" else "Open your tracking page to pay online."), order_id)
    notify(conn, shop_id, "owner", "Price confirmed", f"{order['ticket']} is ready for payment.", order_id)
    return {"id": order_id, "ticket": order["ticket"], "status": "awaiting_payment",
            "payment_method": payment_method, "payment_status": "pending", "total_paise": quote["total_paise"],
            "quote": quote}


def tracking_order(conn, shop_id: str, ticket: str, tracking_token: str) -> dict | None:
    row = conn.execute(
        "SELECT id, ticket, customer_name, status, payment_status, payment_method, total_paise, created_at, updated_at, "
        "(SELECT n.message FROM notifications n WHERE n.shop_id = orders.shop_id AND n.order_id = orders.id "
        "AND n.audience = 'customer' ORDER BY n.created_at DESC LIMIT 1) AS customer_notice "
        "FROM orders WHERE shop_id = ? AND ticket = ? AND tracking_token_hash = ?",
        (shop_id, ticket, hashlib.sha256(tracking_token.encode()).hexdigest()),
    ).fetchone()
    if not row:
        return None
    return dict(row)
