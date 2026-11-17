import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from smarth_xerox import config
from smarth_xerox.accounts import create_shop, create_super_admin, setup_shop
from smarth_xerox.db import connect, migrate

import app as server_app


class HttpWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = (config.DATABASE, config.STORAGE, config.PUBLIC_BASE_URL, server_app.application)
        config.DATABASE = Path(self.temp.name) / "test.sqlite3"
        config.STORAGE = Path(self.temp.name) / "private"
        migrate()
        conn = connect()
        with conn:
            self.shop_id, self.owner_id = setup_shop(
                conn, "North Copy", "north-copy", "owner@example.test", "a long and unique passphrase",
            )
            self.other_shop, self.other_owner = create_shop(
                conn, "South Copy", "south-copy", "south@example.test", "another long passphrase 123",
                "South owner",
            )
        conn.close()
        server_app.application = server_app.Application()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), server_app.RequestHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        config.PUBLIC_BASE_URL = f"http://127.0.0.1:{self.server.server_port}"
        self.base = config.PUBLIC_BASE_URL
        self.cookies = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        _, login = self.call("POST", "/api/auth/login", {"email": "owner@example.test", "password": "a long and unique passphrase"})
        self.csrf = login["csrf_token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        config.DATABASE, config.STORAGE, config.PUBLIC_BASE_URL, server_app.application = self.old
        self.temp.cleanup()

    def call(self, method, path, value=None, headers=None, raw=None, content_type="application/json"):
        data = raw if raw is not None else (json.dumps(value).encode() if value is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": content_type, **(headers or {})})
        try:
            response = self.client.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        body = response.read()
        if response.headers.get_content_type() == "application/json":
            return response.status, json.loads(body)
        return response.status, body

    def admin_headers(self):
        return {"X-CSRF-Token": self.csrf, "X-Shop-Slug": "north-copy"}

    def test_customer_order_counter_payment_and_manual_print_are_connected(self):
        prices = {
            "rates": {"A4": {"bw_simplex_per_side": 200, "bw_duplex_per_sheet": 125,
                              "color_simplex_per_side": 1000, "color_duplex_per_sheet": 750}},
            "minimum_order_paise": 0, "service_fee_paise": 0, "tax_rate_bps": 0,
            "binding_per_copy_paise": 0, "stapling_per_copy_paise": 0,
        }
        status, result = self.call("PUT", "/api/admin/settings", {"key": "pricing", "value": prices}, self.admin_headers())
        self.assertEqual(status, 200, result)
        status, result = self.call("POST", "/api/public/shops/north-copy/drafts", headers={"Idempotency-Key": "draft-key-001"})
        self.assertEqual(status, 201, result)
        draft_token = result["token"]
        boundary = "----codex-test-boundary"
        pdf = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF"
        multipart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"notes.pdf\"\r\n"
                     "Content-Type: application/pdf\r\n\r\n").encode() + pdf + f"\r\n--{boundary}--\r\n".encode()
        status, result = self.call("POST", "/api/public/shops/north-copy/documents", raw=multipart,
                                   content_type=f"multipart/form-data; boundary={boundary}",
                                   headers={"X-Order-Draft": draft_token})
        self.assertEqual(status, 201, result)
        self.assertEqual(result["documents"][0]["page_count"], 1)
        status, restored = self.call("GET", "/api/public/shops/north-copy/documents",
                                     headers={"X-Order-Draft": draft_token})
        self.assertEqual(status, 200, restored)
        self.assertEqual(restored["documents"][0]["original_name"], "notes.pdf")
        status, denied_restore = self.call("GET", "/api/public/shops/north-copy/documents",
                                           headers={"X-Order-Draft": "invalid-draft-token"})
        self.assertEqual(status, 404, denied_restore)
        status, result = self.call("POST", "/api/public/shops/north-copy/quote", headers={"X-Order-Draft": draft_token})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["quote"]["total_paise"], 200)
        status, result = self.call("POST", "/api/public/shops/north-copy/orders",
                                   {"customer_name": "Asha", "customer_phone": "9876543210", "payment_method": "counter",
                                    "total_paise": 1, "payment_status": "paid"},
                                   {"X-Order-Draft": draft_token, "Idempotency-Key": "order-key-001"})
        self.assertEqual(status, 201, result)
        order = result["order"]
        track_token = result["tracking_token"]
        status, track = self.call("GET", f"/api/public/shops/north-copy/orders/{order['ticket']}",
                                  headers={"X-Tracking-Token": track_token})
        self.assertEqual(status, 200, track)
        self.assertEqual(track["order"]["payment_status"], "pending")
        self.assertNotIn("documents", track["order"])
        status, denied = self.call("GET", f"/api/public/shops/south-copy/orders/{order['ticket']}",
                                   headers={"X-Tracking-Token": track_token})
        self.assertEqual(status, 404)
        status, denied = self.call("GET", f"/api/admin/orders/{order['id']}", headers={**self.admin_headers(), "X-Shop-Slug": "south-copy"})
        self.assertEqual(status, 403)
        status, paid = self.call("POST", f"/api/admin/orders/{order['id']}/counter-payment", headers=self.admin_headers())
        self.assertEqual(status, 200, paid)
        self.assertEqual(paid["order"]["payment_status"], "paid")
        status, created = self.call("POST", f"/api/admin/orders/{order['id']}/print-jobs",
                                    {"method": "manual"}, self.admin_headers())
        self.assertEqual(status, 201, created)
        job = created["jobs"][0]
        status, details = self.call("GET", f"/api/admin/orders/{order['id']}", headers=self.admin_headers())
        document_id = details["order"]["documents"][0]["id"]
        status, doc = self.call("GET", f"/api/admin/orders/{order['id']}/documents/{document_id}/download", headers=self.admin_headers())
        self.assertEqual(status, 200)
        self.assertTrue(doc.startswith(b"%PDF-"))
        status, completed = self.call("POST", f"/api/admin/print-jobs/{job['id']}/state",
                                      {"status": "completed"}, self.admin_headers())
        self.assertEqual(status, 200, completed)
        status, track = self.call("GET", f"/api/public/shops/north-copy/orders/{order['ticket']}",
                                  headers={"X-Tracking-Token": track_token})
        self.assertEqual(track["order"]["status"], "completed")
        refund_request = {"amount_paise": 100, "reason": "Short print run", "idempotency_key": "refund-request-001"}
        status, refund = self.call("POST", f"/api/admin/orders/{order['id']}/refunds", refund_request, self.admin_headers())
        self.assertEqual(status, 201, refund)
        self.assertEqual(refund["refund"]["status"], "processed")
        status, repeated = self.call("POST", f"/api/admin/orders/{order['id']}/refunds", refund_request, self.admin_headers())
        self.assertEqual(status, 201, repeated)
        self.assertEqual(repeated["refund"]["id"], refund["refund"]["id"])
        status, payments = self.call("GET", "/api/admin/payments", headers=self.admin_headers())
        self.assertEqual(status, 200, payments)
        self.assertEqual(payments["totals"]["refunds_paise"], 100)
        status, overshoot = self.call("POST", f"/api/admin/orders/{order['id']}/refunds",
                                      {"amount_paise": 101, "reason": "Too much", "idempotency_key": "refund-request-002"},
                                      self.admin_headers())
        self.assertEqual(status, 422, overshoot)
        status, csv_data = self.call("GET", "/api/admin/payments.csv", headers=self.admin_headers())
        self.assertEqual(status, 200)
        self.assertIn(b"refund", csv_data.lower())
        status, activity = self.call("GET", "/api/admin/audit", headers=self.admin_headers())
        self.assertEqual(status, 200, activity)
        self.assertTrue(any(item["action"] == "print.manual.approved" for item in activity["activity"]))

    def test_customer_can_request_page_count_review_and_pay_only_after_owner_confirms_price(self):
        prices = {
            "rates": {"A4": {"bw_simplex_per_side": 250, "bw_duplex_per_sheet": 150,
                              "color_simplex_per_side": 1000, "color_duplex_per_sheet": 700}},
            "minimum_order_paise": 0, "service_fee_paise": 0, "tax_rate_bps": 0,
            "binding_per_copy_paise": 0, "stapling_per_copy_paise": 0,
        }
        status, result = self.call("PUT", "/api/admin/settings", {"key": "pricing", "value": prices}, self.admin_headers())
        self.assertEqual(status, 200, result)
        status, draft = self.call("POST", "/api/public/shops/north-copy/drafts", headers={"Idempotency-Key": "review-draft-001"})
        self.assertEqual(status, 201, draft)
        boundary = "----review-pdf-boundary"
        pdf = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF"
        multipart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"scan.pdf\"\r\n"
                     "Content-Type: application/pdf\r\n\r\n").encode() + pdf + f"\r\n--{boundary}--\r\n".encode()
        status, uploaded = self.call("POST", "/api/public/shops/north-copy/documents", raw=multipart,
                                     content_type=f"multipart/form-data; boundary={boundary}",
                                     headers={"X-Order-Draft": draft["token"]})
        self.assertEqual(status, 201, uploaded)
        status, quote = self.call("POST", "/api/public/shops/north-copy/quote", headers={"X-Order-Draft": draft["token"]})
        self.assertEqual(status, 200, quote)
        self.assertTrue(quote["quote"]["needs_review"])
        status, created = self.call("POST", "/api/public/shops/north-copy/orders",
                                    {"customer_name": "Mira", "customer_phone": "9999999999", "payment_method": "review"},
                                    {"X-Order-Draft": draft["token"], "Idempotency-Key": "review-order-001"})
        self.assertEqual(status, 201, created)
        order = created["order"]
        token = created["tracking_token"]
        self.assertEqual(order["status"], "payment_verification")
        self.assertNotIn("payments", created)
        status, track = self.call("GET", f"/api/public/shops/north-copy/orders/{order['ticket']}", headers={"X-Tracking-Token": token})
        self.assertEqual(track["order"]["status"], "payment_verification")
        self.assertEqual(track["order"]["customer_notice"], None)
        status, details = self.call("GET", f"/api/admin/orders/{order['id']}", headers=self.admin_headers())
        self.assertTrue(details["order"]["documents"][0]["needs_review"])
        document_id = details["order"]["documents"][0]["id"]
        status, reviewed = self.call("POST", f"/api/admin/orders/{order['id']}/review",
                                     {"page_counts": {document_id: 8}, "payment_method": "counter"}, self.admin_headers())
        self.assertEqual(status, 200, reviewed)
        self.assertEqual(reviewed["order"]["total_paise"], 2000)
        status, track = self.call("GET", f"/api/public/shops/north-copy/orders/{order['ticket']}", headers={"X-Tracking-Token": token})
        self.assertEqual(track["order"]["status"], "awaiting_payment")
        self.assertIn("₹20.00", track["order"]["customer_notice"])

    def test_super_admin_can_create_suspend_and_activate_shop(self):
        conn = connect()
        with conn:
            create_super_admin(conn, "platform@example.test", "Platform Admin", "platform admin passphrase 123")
        conn.close()
        status, _ = self.call("POST", "/api/auth/logout", {}, {"X-CSRF-Token": self.csrf})
        self.assertEqual(status, 200)
        status, login = self.call("POST", "/api/auth/login", {
            "email": "platform@example.test", "password": "platform admin passphrase 123",
        })
        self.assertEqual(status, 200, login)
        self.csrf = login["csrf_token"]
        headers = {"X-CSRF-Token": self.csrf}
        status, shops = self.call("GET", "/api/super-admin/shops")
        self.assertEqual(status, 200, shops)
        self.assertEqual(len(shops["shops"]), 2)
        status, created = self.call("POST", "/api/super-admin/shops", {
            "name": "West Campus Copy", "slug": "west-campus-copy", "owner_email": "west@example.test",
            "owner_password": "west owner passphrase 123", "owner_name": "West Owner",
        }, headers)
        self.assertEqual(status, 201, created)
        shop_id = created["shop"]["id"]
        status, suspended = self.call("POST", f"/api/super-admin/shops/{shop_id}/suspend", {}, headers)
        self.assertEqual(status, 200, suspended)
        self.assertEqual(suspended["shop"]["status"], "suspended")
        status, active = self.call("POST", f"/api/super-admin/shops/{shop_id}/activate", {}, headers)
        self.assertEqual(status, 200, active)
        self.assertEqual(active["shop"]["status"], "active")
        status, analytics = self.call("GET", "/api/super-admin/analytics")
        self.assertEqual(status, 200, analytics)
        self.assertEqual(analytics["analytics"]["shops"], 3)


if __name__ == "__main__":
    unittest.main()
