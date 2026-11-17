from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import config
from .accounts import (DEFAULT_SETTINGS, PERMISSION_NAMES, authenticate, membership, notify,
                      save_setting, shop_settings)
from .db import atomic, connect, migrate, now_iso
from .documents import add_document, document_path
from .orders import (create_draft, create_order, draft_documents, find_draft,
                     finalize_page_count_review, quote_draft, update_document_settings, tracking_order)
from .payments import (PaymentGatewayError, create_razorpay_attempt, mark_counter_paid,
                       create_refund, process_webhook, verify_callback)
from .printing import (agent_document, authenticate_agent, claim_next_job, create_pairing_code,
                       create_print_jobs, list_agents, manual_job_state, owner_confirm_printed,
                       pair_agent, report_job, resolve_unknown_submission, update_heartbeat, update_queue_job)
from .qrcode import svg as make_qr_svg
from .security import csrf_valid, digest, issue_session, safe_equal, session_user

LOGGER = logging.getLogger("smarth_xerox")
LOGIN_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
LOGIN_LOCK = threading.Lock()
LOGIN_LIMIT = 8
LOGIN_WINDOW_SECONDS = 900


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str | list[str]] = field(default_factory=dict)


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def json_response(status: int, payload: dict, headers: dict[str, str] | None = None) -> Response:
    return Response(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
                    headers=headers or {})


def multipart_files(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    boundary = None
    try:
        content_type_header = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + b"\r\n"
        )["Content-Type"]
        if content_type_header:
            boundary = content_type_header.params.get("boundary")
    except Exception:
        boundary = None
    if not boundary or len(boundary) > 200:
        raise HttpError(400, "invalid_multipart", "Upload form data is invalid.")
    mime = (f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
            "MIME-Version: 1.0\r\n\r\n").encode() + body
    message = BytesParser(policy=policy.default).parsebytes(mime)
    if not message.is_multipart():
        raise HttpError(400, "invalid_multipart", "Upload form data is invalid.")
    files = []
    for part in message.iter_parts():
        disposition = part.get_content_disposition()
        field_name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if disposition == "form-data" and field_name == "files" and filename is not None:
            files.append((filename, part.get_payload(decode=True) or b""))
    if not files:
        raise HttpError(400, "missing_files", "Choose one or more documents to upload.")
    return files


class Application:
    def __init__(self):
        migrate()

    def handle(self, method: str, target: str, headers, body: bytes, remote_ip: str) -> Response:
        parsed = urlsplit(target)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        conn = connect()
        try:
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                self._check_origin(headers)
            if path.startswith("/api/"):
                return self._api(conn, method, path, query, headers, body, remote_ip)
            return json_response(404, {"error": {"code": "not_found", "message": "This route was not found."}})
        except HttpError as error:
            return json_response(error.status, {"error": {"code": error.code, "message": error.message}})
        except (ValueError, KeyError) as error:
            return json_response(422, {"error": {"code": "invalid_request", "message": str(error)}})
        except sqlite3.IntegrityError:
            return json_response(409, {"error": {"code": "conflict", "message": "This change conflicts with an existing record."}})
        except Exception:
            LOGGER.exception("Request failed: %s %s", method, path)
            return json_response(500, {"error": {"code": "internal_error", "message": "The request could not be completed."}})
        finally:
            conn.close()

    @staticmethod
    def _check_origin(headers) -> None:
        origin = headers.get("Origin")
        if not origin:
            return
        try:
            origin_host = urlsplit(origin).netloc.lower()
        except ValueError:
            origin_host = ""
        if not origin_host or origin_host != headers.get("Host", "").lower():
            raise HttpError(403, "cross_origin", "Cross-origin requests are not accepted.")

    @staticmethod
    def _json(body: bytes) -> dict:
        try:
            value = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HttpError(400, "invalid_json", "Request body must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise HttpError(400, "invalid_json", "Request body must be a JSON object.")
        return value

    @staticmethod
    def _cookie(headers, name: str) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("Cookie", ""))
            return cookie[name].value if name in cookie else None
        except Exception:
            return None

    def _user(self, conn: sqlite3.Connection, headers) -> dict:
        user = session_user(conn, self._cookie(headers, "smx_session"))
        if not user:
            raise HttpError(401, "authentication_required", "Sign in to continue.")
        return user

    def _shop_context(self, conn: sqlite3.Connection, headers, permission: str | None = None) -> tuple[dict, dict, dict]:
        user = self._user(conn, headers)
        slug = headers.get("X-Shop-Slug", "").strip()
        if slug:
            shop_row = conn.execute("SELECT * FROM shops WHERE slug = ?", (slug,)).fetchone()
        else:
            shop_row = conn.execute(
                "SELECT s.* FROM shops s JOIN shop_members m ON m.shop_id = s.id "
                "WHERE m.user_id = ? AND s.status = 'active' ORDER BY m.created_at LIMIT 1", (user["id"],)
            ).fetchone()
        if not shop_row:
            raise HttpError(404, "shop_not_found", "Shop not found for this account.")
        shop = dict(shop_row)
        member = membership(conn, user["id"], shop["id"])
        if user["global_role"] != "super_admin" and not member:
            raise HttpError(403, "shop_access_denied", "You do not have access to this shop.")
        if shop["status"] != "active" and user["global_role"] != "super_admin":
            raise HttpError(403, "shop_suspended", "This shop account is suspended.")
        if permission:
            from .security import has_permission
            if not has_permission(user, member, permission):
                raise HttpError(403, "permission_denied", "You do not have permission for this action.")
        if user["global_role"] == "super_admin" and member is None:
            member = {"role": "owner", "permissions": {}}
        return user, shop, member

    def _csrf(self, user: dict, headers) -> None:
        submitted = headers.get("X-CSRF-Token")
        if (not csrf_valid(user, submitted)
                or not safe_equal(submitted or "", self._cookie(headers, "smx_csrf") or "")):
            raise HttpError(403, "csrf_failed", "Refresh the dashboard and try again.")

    def _api(self, conn, method, path, query, headers, body, remote_ip) -> Response:
        segments = [part for part in path.strip("/").split("/") if part]
        if segments == ["api", "health"] and method == "GET":
            return json_response(200, {"status": "ok", "configured": bool(conn.execute("SELECT 1 FROM shops LIMIT 1").fetchone())})
        if segments[:3] == ["api", "public", "default-shop"] and method == "GET":
            return self._default_shop(conn)
        if len(segments) == 4 and segments[:3] == ["api", "public", "shops"] and method == "GET":
            return self._public_shop(conn, segments[3])
        if len(segments) == 5 and segments[:3] == ["api", "public", "shops"]:
            slug, action = segments[3], segments[4]
            if action == "drafts" and method == "POST":
                return self._create_draft(conn, slug, headers)
            if action == "documents" and method == "POST":
                return self._upload_documents(conn, slug, headers, body)
            if action == "documents" and method == "GET":
                shop = conn.execute("SELECT id FROM shops WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
                if not shop:
                    raise HttpError(404, "shop_not_found", "Shop not found.")
                draft = find_draft(conn, shop["id"], headers.get("X-Order-Draft", ""))
                if not draft:
                    raise HttpError(404, "draft_not_found", "This upload session expired. Start a new order.")
                rows = conn.execute(
                    "SELECT id, original_name, mime_type, size_bytes, page_count, needs_review, settings_json "
                    "FROM documents WHERE shop_id = ? AND draft_id = ? AND order_id IS NULL ORDER BY created_at, id",
                    (shop["id"], draft["id"]),
                ).fetchall()
                documents = [{**dict(row), "settings": json.loads(row["settings_json"])} for row in rows]
                for document in documents:
                    document.pop("settings_json", None)
                return json_response(200, {"documents": documents})
            if action == "quote" and method == "POST":
                return self._quote(conn, slug, headers)
            if action == "orders" and method == "POST":
                return self._create_order(conn, slug, headers, body)
        if len(segments) == 6 and segments[:3] == ["api", "public", "shops"] and segments[4] == "documents":
            if segments[5] and method == "DELETE":
                return self._delete_document(conn, segments[3], segments[5], headers)
        if len(segments) == 7 and segments[:3] == ["api", "public", "shops"] and segments[4] == "documents" and segments[6] == "settings" and method == "PATCH":
            return self._set_document_settings(conn, segments[3], segments[5], headers, body)
        if len(segments) == 6 and segments[:3] == ["api", "public", "shops"] and segments[4] == "orders":
            if segments[5] == "payment" and method == "POST":
                return self._create_payment(conn, segments[3], headers, body)
            if method == "GET":
                return self._track_order(conn, segments[3], segments[5], headers)
        if segments == ["api", "payments", "razorpay", "verify"] and method == "POST":
            return self._verify_payment(conn, headers, body)
        if segments == ["api", "webhooks", "razorpay"] and method == "POST":
            result = process_webhook(conn, body, headers.get("X-Razorpay-Signature", ""),
                                     headers.get("X-Razorpay-Event-Id"))
            return json_response(200, {"received": True, "result": result})
        if segments[:2] == ["api", "auth"]:
            return self._auth_routes(conn, segments[2:], method, headers, body, remote_ip)
        if segments[:3] == ["api", "agent", "pair"] and method == "POST":
            return self._pair_agent(conn, body)
        if segments[:2] == ["api", "agent"]:
            return self._agent_routes(conn, segments[2:], method, headers, body)
        if segments[:2] == ["api", "admin"]:
            return self._admin_routes(conn, segments[2:], method, query, headers, body)
        if segments[:2] == ["api", "super-admin"]:
            return self._super_admin_routes(conn, segments[2:], method, headers, body)
        raise HttpError(404, "not_found", "This API route was not found.")

    def _default_shop(self, conn) -> Response:
        rows = conn.execute("SELECT slug, name FROM shops WHERE status = 'active' ORDER BY name").fetchall()
        if not rows:
            return json_response(503, {"error": {"code": "setup_required", "message": "Initialize a shop to begin."}})
        if len(rows) == 1:
            return json_response(200, {"shop": dict(rows[0])})
        return json_response(200, {"shops": [dict(row) for row in rows]})

    def _public_shop(self, conn, slug: str) -> Response:
        row = conn.execute("SELECT * FROM shops WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
        if not row:
            raise HttpError(404, "shop_not_found", "Shop not found.")
        shop = dict(row)
        settings = shop_settings(conn, shop["id"])
        general, pricing, profile = settings["general"], settings["pricing"], settings["profile"]
        formats = general.get("formats", [])
        enabled_sizes = general.get("enabled_paper_sizes", [])
        required = ("bw_simplex_per_side", "bw_duplex_per_sheet", "color_simplex_per_side", "color_duplex_per_sheet")
        ready = bool(enabled_sizes) and all(
            isinstance(pricing.get("rates", {}).get(size), dict)
            and all(isinstance(pricing["rates"][size].get(key), int) for key in required)
            for size in enabled_sizes
        )
        return json_response(200, {"shop": {
            "slug": shop["slug"], "name": shop["name"], "profile": profile,
            "formats": formats, "max_file_bytes": general.get("max_file_bytes"),
            "max_files": general.get("max_files"), "paper_sizes": enabled_sizes,
            "add_ons": general.get("enabled_add_ons", []), "available": profile.get("service_available", True),
            "pricing_ready": ready, "cash_allowed": general.get("cash_allowed", False),
            "online_payment_available": bool(config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET),
        }})

    def _create_draft(self, conn, slug: str, headers) -> Response:
        shop = conn.execute("SELECT id FROM shops WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
        if not shop:
            raise HttpError(404, "shop_not_found", "Shop not found.")
        key = headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
            raise HttpError(400, "idempotency_required", "A valid upload session key is required.")
        with atomic(conn):
            draft, token = create_draft(conn, shop["id"], key)
        return json_response(201, {"draft": draft, "token": token})

    def _upload_documents(self, conn, slug: str, headers, body: bytes) -> Response:
        shop_row = conn.execute("SELECT * FROM shops WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
        if not shop_row:
            raise HttpError(404, "shop_not_found", "Shop not found.")
        shop = dict(shop_row)
        token = headers.get("X-Order-Draft", "")
        with atomic(conn):
            draft = find_draft(conn, shop["id"], token)
            if not draft:
                raise HttpError(401, "draft_expired", "This upload session has expired. Start a new order.")
            settings = shop_settings(conn, shop["id"])
            incoming = multipart_files(headers.get("Content-Type", ""), body)
            existing_count = conn.execute("SELECT count(*) FROM documents WHERE shop_id = ? AND draft_id = ?",
                                          (shop["id"], draft["id"])).fetchone()[0]
            if existing_count + len(incoming) > settings["general"].get("max_files", 10):
                raise HttpError(413, "too_many_files", "This shop's file limit has been reached.")
            created = []
            try:
                for filename, data in incoming:
                    created.append(add_document(conn, shop["id"], draft["id"], filename, data, settings))
            except Exception:
                for document in created:
                    try:
                        document_path(shop["id"], document["_storage_name"]).unlink(missing_ok=True)
                    except (FileNotFoundError, ValueError):
                        pass
                raise
        for document in created:
            document.pop("_storage_name", None)
        return json_response(201, {"documents": created})

    def _quote(self, conn, slug: str, headers) -> Response:
        shop_row = conn.execute("SELECT id FROM shops WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
        if not shop_row:
            raise HttpError(404, "shop_not_found", "Shop not found.")
        draft = find_draft(conn, shop_row["id"], headers.get("X-Order-Draft", ""))
        if not draft:
            raise HttpError(401, "draft_expired", "This upload session has expired. Start a new order.")
        return json_response(200, {"quote": quote_draft(conn, shop_row["id"], draft["id"], shop_settings(conn, shop_row["id"]))})

    def _set_document_settings(self, conn, slug: str, document_id: str, headers, body: bytes) -> Response:
        shop = conn.execute("SELECT id FROM shops WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
        if not shop:
            raise HttpError(404, "shop_not_found", "Shop not found.")
        token = headers.get("X-Order-Draft", "")
        draft = find_draft(conn, shop["id"], token)
        if not draft:
            raise HttpError(401, "draft_expired", "This upload session has expired. Start a new order.")
        payload = self._json(body)
        settings = payload.get("settings", {})
        with atomic(conn):
            updated = update_document_settings(conn, shop["id"], draft["id"], document_id, settings,
                                               shop_settings(conn, shop["id"]))
            quote = quote_draft(conn, shop["id"], draft["id"], shop_settings(conn, shop["id"]))
        return json_response(200, {"settings": updated, "quote": quote})

    def _delete_document(self, conn, slug: str, document_id: str, headers) -> Response:
        shop = conn.execute("SELECT id FROM shops WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
        if not shop:
            raise HttpError(404, "shop_not_found", "Shop not found.")
        draft = find_draft(conn, shop["id"], headers.get("X-Order-Draft", ""))
        if not draft:
            raise HttpError(401, "draft_expired", "This upload session has expired. Start a new order.")
        row = conn.execute("SELECT storage_name FROM documents WHERE shop_id = ? AND draft_id = ? AND id = ? AND order_id IS NULL",
                           (shop["id"], draft["id"], document_id)).fetchone()
        if not row:
            raise HttpError(404, "document_not_found", "Document not found in this upload session.")
        path = document_path(shop["id"], row["storage_name"])
        with atomic(conn):
            conn.execute("DELETE FROM documents WHERE shop_id = ? AND draft_id = ? AND id = ?",
                         (shop["id"], draft["id"], document_id))
        path.unlink(missing_ok=True)
        return json_response(200, {"deleted": True})

    def _create_order(self, conn, slug: str, headers, body: bytes) -> Response:
        shop_row = conn.execute("SELECT * FROM shops WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
        if not shop_row:
            raise HttpError(404, "shop_not_found", "Shop not found.")
        shop = dict(shop_row)
        token, key = headers.get("X-Order-Draft", ""), headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
            raise HttpError(400, "idempotency_required", "A valid order request key is required.")
        payload = self._json(body)
        name, phone, method = payload.get("customer_name", ""), payload.get("customer_phone", ""), payload.get("payment_method", "")
        if not isinstance(name, str) or len(name) > 120 or not isinstance(phone, str) or len(phone) > 32:
            raise HttpError(400, "invalid_customer", "Customer details are invalid.")
        with atomic(conn):
            draft = find_draft(conn, shop["id"], token)
            if not draft:
                # Idempotent retries can use the same draft token after the session expires.
                draft = conn.execute("SELECT id, shop_id, token_hash FROM drafts WHERE shop_id = ? AND token_hash = ?",
                                     (shop["id"], __import__("hashlib").sha256(token.encode()).hexdigest())).fetchone()
                draft = dict(draft) if draft else None
            if not draft:
                raise HttpError(401, "draft_expired", "This upload session has expired. Start a new order.")
            order, tracking_token, replay = create_order(conn, shop, draft, token, name, phone, method, key,
                                                          shop_settings(conn, shop["id"]))
        return json_response(200 if replay else 201, {
            "order": order,
            "tracking_url": f"/track?shop={shop['slug']}&ticket={order['ticket']}#{tracking_token}",
            "tracking_token": tracking_token,
        })

    def _track_order(self, conn, slug: str, ticket: str, headers) -> Response:
        shop = conn.execute("SELECT id FROM shops WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
        if not shop:
            raise HttpError(404, "order_not_found", "Order not found.")
        order = tracking_order(conn, shop["id"], ticket, headers.get("X-Tracking-Token", ""))
        if not order:
            raise HttpError(404, "order_not_found", "Order not found.")
        return json_response(200, {"order": order})

    def _create_payment(self, conn, slug: str, headers, body: bytes) -> Response:
        shop = conn.execute("SELECT id FROM shops WHERE slug = ? AND status = 'active'", (slug,)).fetchone()
        if not shop:
            raise HttpError(404, "order_not_found", "Order not found.")
        payload = self._json(body)
        order_id = payload.get("order_id", "")
        key = headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
            raise HttpError(400, "idempotency_required", "A valid payment request key is required.")
        payment = create_razorpay_attempt(conn, shop["id"], order_id, headers.get("X-Tracking-Token", ""), key)
        return json_response(201, {"payment": payment})

    def _verify_payment(self, conn, headers, body) -> Response:
        payload = self._json(body)
        order_id = payload.get("order_id", "")
        shop_slug = payload.get("shop", "")
        shop = conn.execute("SELECT id FROM shops WHERE slug = ? AND status = 'active'", (shop_slug,)).fetchone()
        if not shop:
            raise HttpError(404, "order_not_found", "Order not found.")
        result = verify_callback(conn, shop["id"], order_id, headers.get("X-Tracking-Token", ""),
                                 payload.get("razorpay_order_id", ""), payload.get("razorpay_payment_id", ""),
                                 payload.get("razorpay_signature", ""))
        return json_response(200, {"payment": result})

    def _auth_routes(self, conn, segments, method, headers, body, remote_ip) -> Response:
        if segments == ["login"] and method == "POST":
            key = remote_ip
            now = time.time()
            with LOGIN_LOCK:
                LOGIN_ATTEMPTS[key] = [value for value in LOGIN_ATTEMPTS[key] if now - value < LOGIN_WINDOW_SECONDS]
                if len(LOGIN_ATTEMPTS[key]) >= LOGIN_LIMIT:
                    raise HttpError(429, "login_rate_limited", "Too many sign-in attempts. Wait 15 minutes and try again.")
            payload = self._json(body)
            email, password = payload.get("email", ""), payload.get("password", "")
            if not isinstance(email, str) or not isinstance(password, str):
                raise HttpError(400, "invalid_login", "Email and password are required.")
            user = authenticate(conn, email, password)
            if not user:
                with LOGIN_LOCK:
                    LOGIN_ATTEMPTS[key].append(now)
                raise HttpError(401, "invalid_login", "Email or password is incorrect.")
            with LOGIN_LOCK:
                LOGIN_ATTEMPTS.pop(key, None)
            with atomic(conn):
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
                session, csrf, expiry = issue_session(conn, user["id"])
                from .accounts import log_action
                log_action(conn, None, user["id"], "auth.login", "user", user["id"])
            secure = "; Secure" if config.SECURE_COOKIES else ""
            cookie = f"smx_session={session}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200{secure}"
            return json_response(200, {"user": {"id": user["id"], "email": user["email"], "name": user["name"],
                                                   "global_role": user["global_role"]}, "csrf_token": csrf,
                                 "expires_at": expiry}, {"Set-Cookie": [
                                     cookie,
                                     f"smx_csrf={csrf}; Path=/; SameSite=Lax; Max-Age=43200{secure}",
                                 ]})
        if segments == ["me"] and method == "GET":
            user = self._user(conn, headers)
            rows = conn.execute(
                "SELECT s.slug, s.name, s.status, m.role, m.permissions_json FROM shops s "
                "JOIN shop_members m ON m.shop_id = s.id WHERE m.user_id = ? AND m.is_active = 1 ORDER BY s.name", (user["id"],)
            ).fetchall()
            shops = [{"slug": row["slug"], "name": row["name"], "status": row["status"], "role": row["role"],
                      "permissions": json.loads(row["permissions_json"] or "{}")} for row in rows]
            return json_response(200, {"user": {"id": user["id"], "email": user["email"], "name": user["name"],
                                                  "global_role": user["global_role"]}, "shops": shops,
                                 "csrf_token": self._csrf_for_session(conn, headers)})
        if segments == ["logout"] and method == "POST":
            user = self._user(conn, headers)
            self._csrf(user, headers)
            with atomic(conn):
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest(self._cookie(headers, "smx_session") or ""),))
            secure = "; Secure" if config.SECURE_COOKIES else ""
            return json_response(200, {"signed_out": True}, {"Set-Cookie": [
                f"smx_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}",
                f"smx_csrf=; Path=/; SameSite=Lax; Max-Age=0{secure}",
            ]})
        raise HttpError(404, "not_found", "This authentication route was not found.")

    @staticmethod
    def _csrf_for_session(conn, headers) -> str:
        session = Application._cookie(headers, "smx_session")
        csrf = Application._cookie(headers, "smx_csrf")
        if not session or not csrf:
            return ""
        row = conn.execute("SELECT csrf_hash FROM sessions WHERE token_hash = ?", (hashlib.sha256(session.encode()).hexdigest(),)).fetchone()
        return csrf if row and safe_equal(row["csrf_hash"], digest(csrf)) else ""

    def _pair_agent(self, conn, body) -> Response:
        payload = self._json(body)
        code, name = payload.get("pairing_code", ""), payload.get("device_name", "")
        if not isinstance(code, str) or not isinstance(name, str):
            raise HttpError(400, "invalid_pairing", "Pairing code and device name are required.")
        with atomic(conn):
            result = pair_agent(conn, code, name)
        return json_response(201, {"agent": result})

    @staticmethod
    def _agent(conn, headers) -> dict:
        authorization = headers.get("Authorization", "")
        token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        agent = authenticate_agent(conn, token)
        if not agent:
            raise HttpError(401, "agent_authentication_required", "Pair this Print Agent again to continue.")
        return agent

    def _agent_routes(self, conn, segments, method, headers, body) -> Response:
        agent = self._agent(conn, headers)
        if segments == ["me"] and method == "GET":
            shop = conn.execute("SELECT slug, name FROM shops WHERE id = ? AND status = 'active'", (agent["shop_id"],)).fetchone()
            if not shop:
                raise HttpError(403, "shop_unavailable", "This shop is suspended.")
            return json_response(200, {"agent": {"id": agent["id"], "name": agent["name"]}, "shop": dict(shop)})
        if segments == ["heartbeat"] and method == "POST":
            payload = self._json(body)
            with atomic(conn):
                update_heartbeat(conn, agent, str(payload.get("version", "")), payload.get("printers", []),
                                 payload.get("active_job_id"), payload.get("last_error"))
            return json_response(200, {"received": True, "server_time": now_iso()})
        if segments == ["jobs", "next"] and method == "GET":
            if not agent.get("last_heartbeat_at"):
                raise HttpError(409, "heartbeat_required", "Send printer discovery before polling for work.")
            with atomic(conn):
                refreshed = conn.execute("SELECT * FROM print_agents WHERE id = ? AND shop_id = ?", (agent["id"], agent["shop_id"])).fetchone()
                job = claim_next_job(conn, dict(refreshed), shop_settings(conn, agent["shop_id"]))
            return json_response(200, {"job": job})
        if len(segments) == 3 and segments[0] == "jobs" and segments[2] == "document" and method == "GET":
            with atomic(conn):
                document = agent_document(conn, agent, segments[1])
            if not document:
                raise HttpError(404, "document_not_found", "This document is not assigned to the agent.")
            if document["storage_name"].startswith("expired-"):
                raise HttpError(410, "document_expired", "This document expired under the shop retention policy.")
            try:
                path = document_path(document["shop_id"], document["storage_name"])
            except (FileNotFoundError, ValueError) as error:
                raise HttpError(410, "document_unavailable", "This private document is no longer available.") from error
            return Response(200, path.read_bytes(), document["mime_type"], {
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document['original_name'])}", "Cache-Control": "no-store",
            })
        if len(segments) == 3 and segments[0] == "jobs" and segments[2] == "report" and method == "POST":
            payload = self._json(body)
            with atomic(conn):
                job = report_job(conn, agent, segments[1], str(payload.get("state", "")),
                                 str(payload.get("detail", "")), payload.get("spooler_job_id"))
            return json_response(200, {"job": {"id": job["id"], "status": job["status"]}})
        raise HttpError(404, "not_found", "This Print Agent route was not found.")

    def _admin_context(self, conn, headers, permission: str | None = None):
        user, shop, member = self._shop_context(conn, headers, permission)
        return user, shop, shop_settings(conn, shop["id"])

    @staticmethod
    def _order_list(conn, shop_id: str, query: dict) -> dict:
        clauses, values = ["o.shop_id = ?"], [shop_id]
        for query_key, column in (("status", "o.status"), ("payment_status", "o.payment_status")):
            value = query.get(query_key, [""])[0]
            if value:
                clauses.append(f"{column} = ?")
                values.append(value)
        start, end = query.get("from", [""])[0], query.get("to", [""])[0]
        if start and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
            raise HttpError(400, "invalid_date", "Start date must use YYYY-MM-DD.")
        if end and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            raise HttpError(400, "invalid_date", "End date must use YYYY-MM-DD.")
        if start:
            clauses.append("o.created_at >= ?")
            values.append(start + "T00:00:00+00:00")
        if end:
            clauses.append("o.created_at < ?")
            values.append(end + "T23:59:59+00:00")
        search = query.get("search", [""])[0].strip()[:120]
        if search:
            clauses.append("(o.ticket LIKE ? OR o.customer_name LIKE ? OR o.customer_phone LIKE ? OR EXISTS "
                           "(SELECT 1 FROM documents d WHERE d.order_id = o.id AND d.shop_id = o.shop_id AND d.original_name LIKE ?))")
            values.extend([f"%{search}%"] * 4)
        try:
            limit = max(1, min(100, int(query.get("limit", ["30"])[0])))
            offset = max(0, int(query.get("offset", ["0"])[0]))
        except ValueError as exc:
            raise HttpError(400, "invalid_pagination", "Page size and offset must be whole numbers.") from exc
        where = " AND ".join(clauses)
        total = conn.execute(f"SELECT count(*) FROM orders o WHERE {where}", values).fetchone()[0]
        rows = conn.execute(
            "SELECT o.id, o.ticket, o.customer_name, o.customer_phone, o.status, o.payment_status, o.payment_method, "
            "o.subtotal_paise, o.tax_paise, o.total_paise, o.created_at, o.updated_at, "
            "(SELECT count(*) FROM documents d WHERE d.shop_id = o.shop_id AND d.order_id = o.id) AS document_count, "
            "(SELECT coalesce(sum(d.page_count), 0) FROM documents d WHERE d.shop_id = o.shop_id AND d.order_id = o.id) AS pages "
            f"FROM orders o WHERE {where} ORDER BY o.created_at DESC LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
        return {"orders": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _order_details(conn, shop_id: str, order_id: str) -> dict:
        row = conn.execute("SELECT * FROM orders WHERE shop_id = ? AND id = ?", (shop_id, order_id)).fetchone()
        if not row:
            raise HttpError(404, "order_not_found", "Order not found.")
        order = dict(row)
        current_settings = shop_settings(conn, shop_id)
        order["available_payment_methods"] = {
            "counter": bool(current_settings["general"].get("cash_allowed")),
            "razorpay": bool(config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET),
        }
        order["pricing_snapshot"] = json.loads(order.pop("pricing_snapshot_json"))
        order.pop("tracking_token_hash", None)
        order.pop("request_hash", None)
        order.pop("idempotency_key", None)
        order["documents"] = []
        for document in conn.execute("SELECT * FROM documents WHERE shop_id = ? AND order_id = ? ORDER BY created_at, id",
                                     (shop_id, order_id)):
            doc = dict(document)
            doc["settings"] = json.loads(doc.pop("settings_json"))
            doc.pop("storage_name", None)
            order["documents"].append(doc)
        order["payments"] = [dict(item) for item in conn.execute(
            "SELECT id, provider, provider_order_id, provider_payment_id, amount_paise, status, created_at, updated_at "
            "FROM payments WHERE shop_id = ? AND order_id = ? ORDER BY created_at DESC", (shop_id, order_id))]
        order["refunds"] = [dict(item) for item in conn.execute(
            "SELECT id, payment_id, amount_paise, provider_refund_id, status, reason, created_at "
            "FROM refunds WHERE shop_id = ? AND payment_id IN (SELECT id FROM payments WHERE shop_id = ? AND order_id = ?) "
            "ORDER BY created_at DESC", (shop_id, shop_id, order_id))]
        order["print_jobs"] = [dict(item) for item in conn.execute(
            "SELECT j.*, d.original_name FROM print_jobs j JOIN documents d ON d.id = j.document_id AND d.shop_id = j.shop_id "
            "WHERE j.shop_id = ? AND j.order_id = ? ORDER BY j.created_at", (shop_id, order_id))]
        return order

    def _admin_routes(self, conn, segments, method, query, headers, body) -> Response:
        if not segments:
            raise HttpError(404, "not_found", "This dashboard route was not found.")
        if segments == ["payments"] and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "reports.view")
            rows = conn.execute(
                "SELECT p.id, p.order_id, o.ticket, o.customer_name, p.provider, p.provider_order_id, "
                "p.provider_payment_id, p.amount_paise, p.status, p.created_at, p.updated_at "
                "FROM payments p JOIN orders o ON o.id = p.order_id AND o.shop_id = p.shop_id "
                "WHERE p.shop_id = ? ORDER BY p.created_at DESC LIMIT 250", (shop["id"],),
            ).fetchall()
            payments = [dict(row) for row in rows]
            refunds = [dict(row) for row in conn.execute(
                "SELECT r.id, r.payment_id, r.amount_paise, r.provider_refund_id, r.status, r.reason, r.created_at, "
                "p.order_id, o.ticket FROM refunds r JOIN payments p ON p.id = r.payment_id AND p.shop_id = r.shop_id "
                "JOIN orders o ON o.id = p.order_id AND o.shop_id = p.shop_id WHERE r.shop_id = ? "
                "ORDER BY r.created_at DESC LIMIT 250", (shop["id"],))]
            totals = {"revenue_paise": 0, "today_revenue_paise": 0, "online_paise": 0,
                      "counter_paise": 0, "failed_count": 0, "pending_paise": 0, "refunds_paise": 0,
                      "week_revenue_paise": 0, "month_revenue_paise": 0}
            from datetime import datetime
            from zoneinfo import ZoneInfo
            zone = ZoneInfo("Asia/Kolkata")
            today = datetime.now(zone).date()
            week_start = today.fromordinal(today.toordinal() - today.weekday())
            month_start = today.replace(day=1)
            for item in payments:
                if item["status"] in ("captured", "paid"):
                    totals["revenue_paise"] += item["amount_paise"]
                    bucket = "online_paise" if item["provider"] == "razorpay" else "counter_paise"
                    totals[bucket] += item["amount_paise"]
                    try:
                        local_day = datetime.fromisoformat(item["created_at"]).astimezone(zone).date()
                    except (TypeError, ValueError):
                        continue
                    if local_day == today:
                        totals["today_revenue_paise"] += item["amount_paise"]
                    if local_day >= week_start:
                        totals["week_revenue_paise"] += item["amount_paise"]
                    if local_day >= month_start:
                        totals["month_revenue_paise"] += item["amount_paise"]
                elif item["status"] in ("pending", "authorized", "creating"):
                    totals["pending_paise"] += item["amount_paise"]
                elif item["status"] in ("failed", "amount_mismatch", "duplicate_captured"):
                    totals["failed_count"] += 1
            totals["refunds_paise"] = sum(item["amount_paise"] for item in refunds
                                         if item["status"] not in ("failed", "rejected"))
            return json_response(200, {"payments": payments, "refunds": refunds, "totals": totals})
        if len(segments) == 3 and segments[0] == "orders" and segments[2] == "refunds" and method == "POST":
            user, shop, _ = self._admin_context(conn, headers, "payments.manage")
            self._csrf(user, headers)
            payload = self._json(body)
            with atomic(conn):
                if not conn.execute("SELECT 1 FROM orders WHERE shop_id = ? AND id = ?",
                                    (shop["id"], segments[1])).fetchone():
                    raise HttpError(404, "order_not_found", "Order not found.")
            try:
                refund = create_refund(conn, shop["id"], segments[1], payload.get("amount_paise"),
                                       payload.get("reason", ""), payload.get("idempotency_key", ""), user["id"])
            except PaymentGatewayError as error:
                raise HttpError(502, "refund_gateway_error", str(error)) from error
            return json_response(201, {"refund": refund})
        if len(segments) == 3 and segments[0] == "orders" and segments[2] == "review" and method == "POST":
            user, shop, settings = self._admin_context(conn, headers, "orders.manage")
            self._csrf(user, headers)
            payload = self._json(body)
            with atomic(conn):
                result = finalize_page_count_review(conn, shop["id"], segments[1], payload.get("page_counts", {}),
                                                    payload.get("payment_method", ""), settings, user["id"])
            return json_response(200, {"order": result})
        if segments == ["dashboard"] and method == "GET":
            user, shop, _ = self._admin_context(conn, headers, "orders.view")
            return json_response(200, {"dashboard": self._dashboard(conn, shop)})
        if segments == ["orders"] and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "orders.view")
            return json_response(200, self._order_list(conn, shop["id"], query))
        if len(segments) == 2 and segments[0] == "orders" and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "orders.view")
            return json_response(200, {"order": self._order_details(conn, shop["id"], segments[1])})
        if (len(segments) == 5 and segments[0] == "orders" and segments[2] == "documents"
                and segments[4] == "download" and method == "GET"):
            _, shop, _ = self._admin_context(conn, headers, "orders.download")
            doc = conn.execute("SELECT * FROM documents WHERE shop_id = ? AND order_id = ? AND id = ?",
                               (shop["id"], segments[1], segments[3])).fetchone()
            if not doc:
                raise HttpError(404, "document_not_found", "Document not found.")
            if doc["storage_name"].startswith("expired-"):
                raise HttpError(410, "document_expired", "The private document has expired and was removed by the shop's retention policy.")
            try:
                path = document_path(shop["id"], doc["storage_name"])
            except (FileNotFoundError, ValueError) as error:
                raise HttpError(410, "document_unavailable", "This private document is no longer available.") from error
            safe_name = doc["original_name"].replace('"', "")
            disposition = "inline" if doc["mime_type"] == "application/pdf" else "attachment"
            return Response(200, path.read_bytes(), doc["mime_type"], {
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(safe_name)}",
                "Cache-Control": "private, no-store",
            })
        if len(segments) == 2 and segments[0] == "orders" and method == "PATCH":
            user, shop, _ = self._admin_context(conn, headers, "orders.manage")
            self._csrf(user, headers)
            payload = self._json(body)
            return self._change_order_status(conn, shop, user, segments[1], payload.get("status", ""))
        if len(segments) == 3 and segments[0] == "orders" and segments[2] == "counter-payment" and method == "POST":
            user, shop, _ = self._admin_context(conn, headers, "payments.manage")
            self._csrf(user, headers)
            with atomic(conn):
                result = mark_counter_paid(conn, shop["id"], segments[1], user["id"])
            return json_response(200, {"order": result})
        if len(segments) == 3 and segments[0] == "orders" and segments[2] == "print-jobs" and method == "POST":
            user, shop, settings = self._admin_context(conn, headers, "printing.approve")
            self._csrf(user, headers)
            payload = self._json(body)
            with atomic(conn):
                jobs = create_print_jobs(conn, shop["id"], segments[1], payload.get("method", ""), user["id"],
                                         settings, payload.get("confirm_risk") is True,
                                         payload.get("confirm_reprint") is True)
            return json_response(201, {"jobs": jobs})
        if segments == ["print-jobs"] and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "orders.view")
            rows = conn.execute(
                "SELECT j.*, o.ticket, o.payment_status, d.original_name, d.page_count, d.settings_json "
                "FROM print_jobs j JOIN orders o ON o.id = j.order_id AND o.shop_id = j.shop_id "
                "JOIN documents d ON d.id = j.document_id AND d.shop_id = j.shop_id "
                "WHERE j.shop_id = ? ORDER BY CASE j.status WHEN 'queued' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END, j.priority DESC, j.created_at",
                (shop["id"],),
            ).fetchall()
            jobs = [{**dict(row), "settings": json.loads(row["settings_json"])} for row in rows]
            return json_response(200, {"jobs": jobs})
        if len(segments) == 3 and segments[0] == "print-jobs" and segments[2] in ("confirm", "state") and method == "POST":
            user, shop, _ = self._admin_context(conn, headers, "printing.approve")
            self._csrf(user, headers)
            payload = self._json(body)
            with atomic(conn):
                if segments[2] == "confirm":
                    job = owner_confirm_printed(conn, shop["id"], segments[1], user["id"])
                else:
                    job = manual_job_state(conn, shop["id"], segments[1], payload.get("status", ""),
                                           user["id"], payload.get("detail", ""))
            return json_response(200, {"job": job})
        if len(segments) == 3 and segments[0] == "print-jobs" and segments[2] == "resolve" and method == "POST":
            user, shop, _ = self._admin_context(conn, headers, "printing.approve")
            self._csrf(user, headers)
            payload = self._json(body)
            with atomic(conn):
                job = resolve_unknown_submission(conn, shop["id"], segments[1], payload.get("result", ""), user["id"])
            return json_response(200, {"job": job})
        if len(segments) == 3 and segments[0] == "print-jobs" and segments[2] == "queue" and method == "POST":
            user, shop, _ = self._admin_context(conn, headers, "printing.approve")
            self._csrf(user, headers)
            payload = self._json(body)
            with atomic(conn):
                job = update_queue_job(conn, shop["id"], segments[1], payload.get("action", ""),
                                       user["id"], payload.get("priority"))
            return json_response(200, {"job": job})
        if segments == ["agents"] and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "printing.approve")
            with atomic(conn):
                conn.execute("UPDATE print_agents SET status = 'offline' WHERE shop_id = ? AND revoked_at IS NULL "
                             "AND last_heartbeat_at < ?", (shop["id"], self._minutes_ago(1)))
            return json_response(200, {"agents": list_agents(conn, shop["id"])})
        if segments == ["agents", "pairing-code"] and method == "POST":
            user, shop, _ = self._admin_context(conn, headers, "settings.manage")
            self._csrf(user, headers)
            with atomic(conn):
                code, expires = create_pairing_code(conn, shop["id"], user["id"])
            return json_response(201, {"pairing_code": code, "expires_at": expires,
                                       "agent_command": "python3 print_agent/agent.py"})
        if len(segments) == 2 and segments[0] == "agents" and method == "DELETE":
            user, shop, _ = self._admin_context(conn, headers, "settings.manage")
            self._csrf(user, headers)
            with atomic(conn):
                updated = conn.execute("UPDATE print_agents SET revoked_at = ?, status = 'revoked', token_hash = ? "
                                       "WHERE shop_id = ? AND id = ? AND revoked_at IS NULL",
                                       (now_iso(), f"revoked:{uuid.uuid4()}", shop["id"], segments[1])).rowcount
                if not updated:
                    raise HttpError(404, "agent_not_found", "Print Agent not found.")
                conn.execute("UPDATE print_jobs SET status = 'failed', error_message = 'Print Agent was revoked.', updated_at = ? "
                             "WHERE shop_id = ? AND agent_id = ? AND status IN ('claimed','downloading')",
                             (now_iso(), shop["id"], segments[1]))
            return json_response(200, {"revoked": True})
        if segments == ["settings"] and method == "GET":
            user, shop, settings = self._admin_context(conn, headers, "settings.manage")
            return json_response(200, {"settings": {**settings, "profile": {**settings["profile"], "name": shop["name"]}},
                                       "shop": {"slug": shop["slug"], "status": shop["status"]},
                                       "payments": {"razorpay_configured": bool(config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET),
                                                    "webhook_configured": bool(config.RAZORPAY_WEBHOOK_SECRET)}})
        if segments == ["settings"] and method == "PUT":
            user, shop, settings = self._admin_context(conn, headers)
            payload = self._json(body)
            key, value = payload.get("key", ""), payload.get("value", {})
            required = {"profile": "settings.manage", "pricing": "prices.manage", "general": "settings.manage", "printing": "settings.manage"}
            if key not in required:
                raise HttpError(400, "invalid_setting", "Choose a supported shop setting.")
            self._admin_context(conn, headers, required[key])
            self._csrf(user, headers)
            cleaned = self._validate_setting(key, value, settings)
            with atomic(conn):
                save_setting(conn, shop["id"], key, cleaned, user["id"], tuple(DEFAULT_SETTINGS))
                if key == "profile":
                    conn.execute("UPDATE shops SET name = ? WHERE id = ?", (cleaned["name"], shop["id"]))
                    cleaned = {k: v for k, v in cleaned.items() if k != "name"}
                    conn.execute("UPDATE shop_settings SET value_json = ? WHERE shop_id = ? AND setting_key = 'profile'",
                                 (json.dumps(cleaned, separators=(",", ":")), shop["id"]))
            return json_response(200, {"saved": key})
        if segments == ["qr.svg"] and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "settings.manage")
            qr = make_qr_svg(f"{config.PUBLIC_BASE_URL}/?shop={shop['slug']}")
            return Response(200, qr.encode(), "image/svg+xml; charset=utf-8", {"Content-Disposition": "attachment; filename=shop-qr.svg", "Cache-Control": "private, no-store"})
        if segments == ["reports.csv"] and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "reports.view")
            return self._report_csv(conn, shop["id"], query)
        if segments == ["payments.csv"] and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "reports.view")
            return self._payment_report_csv(conn, shop["id"])
        if segments == ["notifications"] and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "orders.view")
            rows = conn.execute("SELECT * FROM notifications WHERE shop_id = ? AND audience = 'owner' "
                                "ORDER BY created_at DESC LIMIT 50", (shop["id"],)).fetchall()
            return json_response(200, {"notifications": [dict(row) for row in rows]})
        if len(segments) == 3 and segments[0] == "notifications" and segments[2] == "read" and method == "POST":
            user, shop, _ = self._admin_context(conn, headers, "orders.view")
            self._csrf(user, headers)
            with atomic(conn):
                conn.execute("UPDATE notifications SET read_at = ? WHERE shop_id = ? AND id = ? AND audience = 'owner'",
                             (now_iso(), shop["id"], segments[1]))
            return json_response(200, {"read": True})
        if segments == ["staff"] and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "staff.manage")
            rows = conn.execute("SELECT u.id, u.email, u.name, u.disabled, m.role, m.is_active, m.permissions_json, m.created_at "
                                "FROM shop_members m JOIN users u ON u.id = m.user_id WHERE m.shop_id = ? AND m.role = 'staff' "
                                "ORDER BY u.name", (shop["id"],)).fetchall()
            return json_response(200, {"staff": [{**dict(row), "permissions": json.loads(row["permissions_json"])} for row in rows]})
        if segments == ["audit"] and method == "GET":
            _, shop, _ = self._admin_context(conn, headers, "staff.manage")
            rows = conn.execute(
                "SELECT a.action, a.entity_type, a.entity_id, a.details_json, a.created_at, u.name AS actor_name "
                "FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id WHERE a.shop_id = ? "
                "ORDER BY a.created_at DESC LIMIT 100", (shop["id"],)
            ).fetchall()
            entries = []
            for row in rows:
                entry = dict(row)
                try:
                    entry["details"] = json.loads(entry.pop("details_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    entry["details"] = {}
                    entry.pop("details_json", None)
                entries.append(entry)
            return json_response(200, {"activity": entries})
        if segments == ["staff"] and method == "POST":
            user, shop, _ = self._admin_context(conn, headers, "staff.manage")
            self._csrf(user, headers)
            payload = self._json(body)
            from .security import password_hash
            name, email, password = payload.get("name", ""), payload.get("email", ""), payload.get("password", "")
            permissions = self._clean_permissions(payload.get("permissions", {}))
            if not isinstance(name, str) or not name.strip() or len(name) > 120 or not isinstance(email, str) or not isinstance(password, str):
                raise HttpError(400, "invalid_staff", "Staff name, email, and password are required.")
            member_id = str(uuid.uuid4())
            with atomic(conn):
                conn.execute("INSERT INTO users(id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                             (member_id, email.strip().lower(), name.strip(), password_hash(password), now_iso()))
                conn.execute("INSERT INTO shop_members(shop_id, user_id, role, permissions_json, created_at) VALUES (?, ?, 'staff', ?, ?)",
                             (shop["id"], member_id, json.dumps(permissions), now_iso()))
                from .accounts import log_action
                log_action(conn, shop["id"], user["id"], "staff.created", "user", member_id)
            return json_response(201, {"staff": {"id": member_id, "email": email.strip().lower(), "name": name.strip(), "permissions": permissions}})
        if len(segments) == 2 and segments[0] == "staff" and method == "PATCH":
            user, shop, _ = self._admin_context(conn, headers, "staff.manage")
            self._csrf(user, headers)
            payload = self._json(body)
            permissions = self._clean_permissions(payload["permissions"]) if "permissions" in payload else None
            enabled = payload.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                raise HttpError(400, "invalid_staff", "Enabled must be true or false.")
            with atomic(conn):
                member = conn.execute("SELECT u.id FROM shop_members m JOIN users u ON u.id = m.user_id "
                                      "WHERE m.shop_id = ? AND m.user_id = ? AND m.role = 'staff'",
                                      (shop["id"], segments[1])).fetchone()
                if not member:
                    raise HttpError(404, "staff_not_found", "Staff member not found.")
                if permissions is not None:
                    conn.execute("UPDATE shop_members SET permissions_json = ? WHERE shop_id = ? AND user_id = ?",
                                 (json.dumps(permissions), shop["id"], segments[1]))
                if enabled is not None:
                    conn.execute("UPDATE shop_members SET is_active = ? WHERE shop_id = ? AND user_id = ?",
                                 (int(enabled), shop["id"], segments[1]))
                from .accounts import log_action
                log_action(conn, shop["id"], user["id"], "staff.updated", "user", segments[1])
            return json_response(200, {"updated": True})
        raise HttpError(404, "not_found", "This dashboard route was not found.")

    @staticmethod
    def _clean_permissions(value: dict) -> dict:
        if not isinstance(value, dict) or set(value) - set(PERMISSION_NAMES):
            raise HttpError(400, "invalid_permissions", "Choose valid staff permissions.")
        if any(not isinstance(item, bool) for item in value.values()):
            raise HttpError(400, "invalid_permissions", "Staff permissions must be true or false.")
        return {key: value.get(key, False) for key in PERMISSION_NAMES}

    @staticmethod
    def _validate_setting(key: str, incoming: dict, current: dict) -> dict:
        if not isinstance(incoming, dict):
            raise HttpError(400, "invalid_setting", "Setting value must be an object.")
        value = {**current[key], **incoming}
        if key == "profile":
            allowed = {"name", "address", "phone", "email", "instructions", "availability", "service_available", "estimated_minutes"}
            if set(incoming) - allowed:
                raise HttpError(400, "invalid_profile", "Shop profile contains unsupported fields.")
            if "name" in incoming and (not isinstance(value["name"], str) or not value["name"].strip() or len(value["name"]) > 120):
                raise HttpError(400, "invalid_profile", "Shop name must contain 1 to 120 characters.")
            if not isinstance(value.get("service_available"), bool) or value.get("availability") not in ("open", "busy", "closed"):
                raise HttpError(400, "invalid_profile", "Shop availability settings are invalid.")
            for field_name in ("address", "phone", "email", "instructions"):
                if not isinstance(value.get(field_name, ""), str) or len(value[field_name]) > 500:
                    raise HttpError(400, "invalid_profile", f"{field_name.title()} is too long.")
            return value
        if key == "pricing":
            allowed = {"rates", "minimum_order_paise", "service_fee_paise", "tax_rate_bps", "discount_rate_bps", "binding_per_copy_paise", "stapling_per_copy_paise"}
            if set(incoming) - allowed or not isinstance(value.get("rates"), dict):
                raise HttpError(400, "invalid_pricing", "Pricing settings are invalid.")
            for paper, rates in value["rates"].items():
                if paper not in ("A4", "A3", "LEGAL", "LETTER") or not isinstance(rates, dict):
                    raise HttpError(400, "invalid_pricing", "Choose a supported paper size and price table.")
                for rate_name, amount in rates.items():
                    if rate_name not in ("bw_simplex_per_side", "bw_duplex_per_sheet", "color_simplex_per_side", "color_duplex_per_sheet"):
                        raise HttpError(400, "invalid_pricing", "A price table contains an unsupported unit.")
                    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= 100_000_000:
                        raise HttpError(400, "invalid_pricing", "Prices must be entered as whole paise amounts.")
            for field_name in ("minimum_order_paise", "service_fee_paise", "binding_per_copy_paise", "stapling_per_copy_paise"):
                amount = value.get(field_name, 0)
                if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= 100_000_000:
                    raise HttpError(400, "invalid_pricing", f"{field_name.replace('_', ' ').title()} must be whole paise.")
            tax = value.get("tax_rate_bps", 0)
            if isinstance(tax, bool) or not isinstance(tax, int) or not 0 <= tax <= 10000:
                raise HttpError(400, "invalid_pricing", "Tax must be between 0 and 100 percent.")
            discount = value.get("discount_rate_bps", 0)
            if isinstance(discount, bool) or not isinstance(discount, int) or not 0 <= discount <= 10000:
                raise HttpError(400, "invalid_pricing", "Discount must be between 0 and 100 percent.")
            return value
        if key == "general":
            allowed = set(DEFAULT_SETTINGS["general"])
            if set(incoming) - allowed:
                raise HttpError(400, "invalid_general", "General settings contain unsupported fields.")
            for setting, low, high in (("max_file_bytes", 1024, 100 * 1024 * 1024), ("max_files", 1, 30),
                                       ("max_pages", 1, 5000), ("retention_days", 1, 90)):
                item = value.get(setting)
                if isinstance(item, bool) or not isinstance(item, int) or not low <= item <= high:
                    raise HttpError(400, "invalid_general", f"{setting.replace('_', ' ').title()} is outside the allowed range.")
            if not isinstance(value.get("cash_allowed"), bool):
                raise HttpError(400, "invalid_general", "Counter payment must be enabled or disabled.")
            if value.get("currency") != "INR" or value.get("timezone") != "Asia/Kolkata":
                raise HttpError(400, "invalid_general", "V1 supports INR and Asia/Kolkata time only.")
            formats = value.get("formats")
            allowed_formats = {"pdf", "jpg", "jpeg", "png", "doc", "docx"}
            if not isinstance(formats, list) or not formats or set(formats) - allowed_formats:
                raise HttpError(400, "invalid_general", "Choose supported upload formats.")
            if set(formats) & {"doc", "docx"}:
                import shutil
                if not (shutil.which("libreoffice") or shutil.which("soffice")):
                    raise HttpError(422, "conversion_unavailable", "Install LibreOffice before enabling Word document uploads.")
            paper = value.get("enabled_paper_sizes")
            if not isinstance(paper, list) or not paper or set(paper) - {"A4", "A3", "LEGAL", "LETTER"}:
                raise HttpError(400, "invalid_general", "Choose supported paper sizes.")
            addons = value.get("enabled_add_ons")
            if not isinstance(addons, list) or set(addons) - {"binding", "stapling"}:
                raise HttpError(400, "invalid_general", "Choose supported additional services.")
            return value
        if key == "printing":
            allowed = set(DEFAULT_SETTINGS["printing"])
            if set(incoming) - allowed or value.get("mode") not in ("manual", "automatic"):
                raise HttpError(400, "invalid_printing", "Printing settings are invalid.")
            for flag in ("automatic_enabled", "manual_approval", "approve_color", "approve_large_jobs"):
                if not isinstance(value.get(flag), bool):
                    raise HttpError(400, "invalid_printing", f"{flag.replace('_', ' ').title()} must be true or false.")
            for setting, low, high in (("large_job_pages", 1, 5000), ("max_pages_per_job", 1, 5000),
                                       ("max_copies", 1, 99), ("retry_limit", 0, 5)):
                item = value.get(setting)
                if isinstance(item, bool) or not isinstance(item, int) or not low <= item <= high:
                    raise HttpError(400, "invalid_printing", f"{setting.replace('_', ' ').title()} is outside the allowed range.")
            for printer in ("default_printer", "color_printer"):
                if not isinstance(value.get(printer), str) or len(value[printer]) > 160:
                    raise HttpError(400, "invalid_printing", "Printer names must be 160 characters or fewer.")
            return value
        raise HttpError(400, "invalid_setting", "Setting value is invalid.")

    def _change_order_status(self, conn, shop: dict, user: dict, order_id: str, status: str) -> Response:
        transitions = {
            "payment_verification": {"cancelled"}, "awaiting_payment": {"cancelled"}, "confirmed": {"queued", "cancelled", "failed"},
            "queued": {"preparing_documents", "ready_for_printing", "printing", "cancelled", "failed"},
            "preparing_documents": {"ready_for_printing", "cancelled", "failed"},
            "ready_for_printing": {"printing", "cancelled", "failed"},
            "printing": {"partially_completed", "ready_for_collection", "completed", "failed"},
            "partially_completed": {"ready_for_collection", "completed", "failed"},
            "ready_for_collection": {"completed"}, "failed": {"queued", "cancelled"},
        }
        with atomic(conn):
            order = conn.execute("SELECT * FROM orders WHERE shop_id = ? AND id = ?", (shop["id"], order_id)).fetchone()
            if not order:
                raise HttpError(404, "order_not_found", "Order not found.")
            if status not in transitions.get(order["status"], set()):
                raise HttpError(409, "invalid_transition", f"Order cannot move from {order['status']} to {status}.")
            if status in ("queued", "preparing_documents", "ready_for_printing", "printing", "partially_completed", "ready_for_collection", "completed") and order["payment_status"] != "paid":
                raise HttpError(409, "payment_required", "Verify payment before moving an order into production.")
            if status in ("ready_for_collection", "completed"):
                unfinished = conn.execute("SELECT count(*) FROM print_jobs WHERE shop_id = ? AND order_id = ? AND status != 'completed'",
                                          (shop["id"], order_id)).fetchone()[0]
                if unfinished:
                    raise HttpError(409, "print_jobs_unfinished", "Confirm every document print before completing the order.")
            if status == "cancelled":
                started = conn.execute("SELECT count(*) FROM print_jobs WHERE shop_id = ? AND order_id = ? "
                                       "AND status IN ('claimed','downloading','submitting','submitted','spooler_reported','printing','completed')",
                                       (shop["id"], order_id)).fetchone()[0]
                if started:
                    raise HttpError(409, "print_already_started", "A print job has already started. Resolve or finish the print before cancelling this order.")
                conn.execute("UPDATE print_jobs SET status = 'cancelled', updated_at = ? WHERE shop_id = ? AND order_id = ? "
                             "AND status IN ('queued','paused')", (now_iso(), shop["id"], order_id))
            conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE shop_id = ? AND id = ?",
                         (status, now_iso(), shop["id"], order_id))
            from .accounts import log_action
            log_action(conn, shop["id"], user["id"], "order.status_changed", "order", order_id, {"status": status})
            notify(conn, shop["id"], "customer", "Order update", f"Your order is now {status.replace('_', ' ')}.", order_id)
        return json_response(200, {"order": {"id": order_id, "status": status}})

    @staticmethod
    def _minutes_ago(minutes: int) -> str:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")

    @staticmethod
    def _dashboard(conn, shop: dict) -> dict:
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        settings = shop_settings(conn, shop["id"])
        try:
            zone = ZoneInfo(settings["general"].get("timezone", "Asia/Kolkata"))
        except Exception:
            zone = timezone.utc
        local_now = datetime.now(zone)
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = local_start.astimezone(timezone.utc).isoformat(timespec="seconds")
        end_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
        today_orders = conn.execute("SELECT count(*) FROM orders WHERE shop_id = ? AND created_at >= ? AND created_at < ?",
                                    (shop["id"], start_utc, end_utc)).fetchone()[0]
        total_orders = conn.execute("SELECT count(*) FROM orders WHERE shop_id = ?", (shop["id"],)).fetchone()[0]
        counts = {row["status"]: row["count"] for row in conn.execute(
            "SELECT status, count(*) AS count FROM orders WHERE shop_id = ? GROUP BY status", (shop["id"],))}
        revenue = conn.execute("SELECT coalesce(sum(amount_paise),0) FROM payments WHERE shop_id = ? "
                               "AND status IN ('captured','paid') AND created_at >= ? AND created_at < ?",
                               (shop["id"], start_utc, end_utc)).fetchone()[0]
        pending = conn.execute("SELECT coalesce(sum(total_paise),0) FROM orders WHERE shop_id = ? "
                               "AND payment_status = 'pending' AND status NOT IN ('payment_verification','cancelled')",
                               (shop["id"],)).fetchone()[0]
        completed = conn.execute("SELECT j.status, d.page_count, d.settings_json FROM print_jobs j JOIN documents d "
                                 "ON d.id = j.document_id AND d.shop_id = j.shop_id WHERE j.shop_id = ? AND j.status = 'completed'",
                                 (shop["id"],)).fetchall()
        printed = {"pages": 0, "bw_pages": 0, "color_pages": 0}
        from .pricing import selected_page_count
        for row in completed:
            settings_doc = json.loads(row["settings_json"])
            selected = selected_page_count(settings_doc.get("page_selection", "all"), row["page_count"])
            pages = (selected or 0) * settings_doc.get("copies", 1)
            printed["pages"] += pages
            printed["color_pages" if settings_doc.get("color") == "color" else "bw_pages"] += pages
        agents = conn.execute("SELECT count(*) FROM print_agents WHERE shop_id = ? AND revoked_at IS NULL AND status = 'online' AND last_heartbeat_at >= ?",
                              (shop["id"], Application._minutes_ago(1))).fetchone()[0]
        return {"today_orders": today_orders, "total_orders": total_orders,
                "pending_orders": sum(counts.get(key, 0) for key in ("payment_verification", "awaiting_payment", "confirmed", "queued", "preparing_documents", "ready_for_printing")),
                "printing_orders": counts.get("printing", 0) + counts.get("partially_completed", 0),
                "completed_orders": counts.get("completed", 0), "failed_orders": counts.get("failed", 0),
                "today_revenue_paise": revenue, "pending_amount_paise": pending, "printed": printed,
                "print_agents_online": agents, "today": local_now.date().isoformat()}

    @staticmethod
    def _report_csv(conn, shop_id: str, query: dict) -> Response:
        start, end = query.get("from", [""])[0], query.get("to", [""])[0]
        clauses, params = ["shop_id = ?"], [shop_id]
        if start:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
                raise HttpError(400, "invalid_date", "Start date must use YYYY-MM-DD.")
            clauses.append("created_at >= ?")
            params.append(start + "T00:00:00+00:00")
        if end:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
                raise HttpError(400, "invalid_date", "End date must use YYYY-MM-DD.")
            clauses.append("created_at < ?")
            params.append(end + "T23:59:59+00:00")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("Ticket", "Customer", "Phone", "Order status", "Payment status", "Method", "Subtotal (paise)", "Tax (paise)", "Total (paise)", "Created at"))
        for order in conn.execute("SELECT ticket, customer_name, customer_phone, status, payment_status, payment_method, subtotal_paise, tax_paise, total_paise, created_at FROM orders WHERE "
                                   + " AND ".join(clauses) + " ORDER BY created_at DESC", params):
            writer.writerow(tuple(order))
        return Response(200, output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                        {"Content-Disposition": "attachment; filename=shop-orders.csv", "Cache-Control": "private, no-store"})

    @staticmethod
    def _payment_report_csv(conn: sqlite3.Connection, shop_id: str) -> Response:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("Type", "Ticket", "Provider", "Reference", "Amount (paise)", "Status", "Reason", "Created at"))
        rows = conn.execute(
            "SELECT 'payment' AS entry_type, o.ticket, p.provider, coalesce(p.provider_payment_id,p.provider_order_id,''), "
            "p.amount_paise, p.status, '', p.created_at FROM payments p JOIN orders o ON o.id = p.order_id AND o.shop_id = p.shop_id "
            "WHERE p.shop_id = ? UNION ALL SELECT 'refund', o.ticket, p.provider, coalesce(r.provider_refund_id,''), "
            "r.amount_paise, r.status, r.reason, r.created_at FROM refunds r JOIN payments p ON p.id = r.payment_id AND p.shop_id = r.shop_id "
            "JOIN orders o ON o.id = p.order_id AND o.shop_id = p.shop_id WHERE r.shop_id = ? ORDER BY 8 DESC",
            (shop_id, shop_id),
        )
        for row in rows:
            writer.writerow(tuple(row))
        return Response(200, output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                        {"Content-Disposition": "attachment; filename=shop-payments.csv", "Cache-Control": "private, no-store"})

    def _super_admin_routes(self, conn, segments, method, headers, body) -> Response:
        user = self._user(conn, headers)
        if user["global_role"] != "super_admin":
            raise HttpError(403, "super_admin_required", "Platform administrator access is required.")
        if segments == ["shops"] and method == "GET":
            rows = conn.execute("SELECT s.id, s.slug, s.name, s.status, s.created_at, "
                                "(SELECT count(*) FROM orders o WHERE o.shop_id = s.id) AS order_count "
                                "FROM shops s ORDER BY s.created_at DESC").fetchall()
            return json_response(200, {"shops": [dict(row) for row in rows]})
        if segments == ["shops"] and method == "POST":
            self._csrf(user, headers)
            payload = self._json(body)
            from .accounts import create_shop
            with atomic(conn):
                shop_id, owner_id = create_shop(conn, payload.get("name", ""), payload.get("slug", ""),
                                                payload.get("owner_email", ""), payload.get("owner_password", ""),
                                                payload.get("owner_name", ""), payload.get("address", ""), payload.get("phone", ""))
            return json_response(201, {"shop": {"id": shop_id, "owner_id": owner_id}})
        if len(segments) == 3 and segments[0] == "shops" and segments[2] in ("suspend", "activate") and method == "POST":
            self._csrf(user, headers)
            status = "suspended" if segments[2] == "suspend" else "active"
            with atomic(conn):
                changed = conn.execute("UPDATE shops SET status = ? WHERE id = ?", (status, segments[1])).rowcount
                if not changed:
                    raise HttpError(404, "shop_not_found", "Shop not found.")
                from .accounts import log_action
                log_action(conn, segments[1], user["id"], f"shop.{status}", "shop", segments[1])
            return json_response(200, {"shop": {"id": segments[1], "status": status}})
        if segments == ["analytics"] and method == "GET":
            shops = conn.execute("SELECT count(*) FROM shops").fetchone()[0]
            active = conn.execute("SELECT count(*) FROM shops WHERE status = 'active'").fetchone()[0]
            orders = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
            revenue = conn.execute("SELECT coalesce(sum(amount_paise), 0) FROM payments WHERE status IN ('captured','paid')").fetchone()[0]
            return json_response(200, {"analytics": {"shops": shops, "active_shops": active, "orders": orders, "revenue_paise": revenue}})
        raise HttpError(404, "not_found", "This platform administrator route was not found.")
