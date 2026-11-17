import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smarth_xerox import config
from smarth_xerox.accounts import DEFAULT_SETTINGS, save_setting, setup_shop, shop_settings
from smarth_xerox.db import connect, migrate, now_iso
from smarth_xerox.documents import add_document
from smarth_xerox.orders import create_draft, create_order, find_draft
from smarth_xerox.payments import process_webhook, verify_callback


class PaymentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "payments.sqlite3"
        self.old_values = (config.DATABASE, config.STORAGE, config.RAZORPAY_KEY_ID,
                           config.RAZORPAY_KEY_SECRET, config.RAZORPAY_WEBHOOK_SECRET)
        config.DATABASE = self.database
        config.STORAGE = Path(self.temp.name) / "private"
        config.RAZORPAY_KEY_ID = "rzp_test_key"
        config.RAZORPAY_KEY_SECRET = "callback-secret"
        config.RAZORPAY_WEBHOOK_SECRET = "webhook-secret"
        migrate()
        self.conn = connect()
        with self.conn:
            self.shop_id, self.owner_id = setup_shop(
                self.conn, "North Copy", "north-copy", "owner@example.test", "a long and unique passphrase",
            )
            save_setting(self.conn, self.shop_id, "pricing", {
                "rates": {"A4": {"bw_simplex_per_side": 200, "bw_duplex_per_sheet": 125,
                                  "color_simplex_per_side": 1000, "color_duplex_per_sheet": 750}},
                "minimum_order_paise": 0, "service_fee_paise": 0, "tax_rate_bps": 0,
                "binding_per_copy_paise": 0, "stapling_per_copy_paise": 0,
            }, self.owner_id, tuple(DEFAULT_SETTINGS))
            self.shop = dict(self.conn.execute("SELECT * FROM shops WHERE id = ?", (self.shop_id,)).fetchone())
            draft, self.tracking_token = create_draft(self.conn, self.shop_id, "payment-draft-001")
            pdf = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF"
            add_document(self.conn, self.shop_id, draft["id"], "payment.pdf", pdf, shop_settings(self.conn, self.shop_id))
            self.order, self.tracking_token, _ = create_order(
                self.conn, self.shop, find_draft(self.conn, self.shop_id, self.tracking_token),
                self.tracking_token, "Asha", "9876543210", "razorpay", "payment-order-001",
                shop_settings(self.conn, self.shop_id),
            )
            self.payment_id = "payment-db-001"
            self.provider_order_id = "order_provider_001"
            self.conn.execute(
                "INSERT INTO payments(id, shop_id, order_id, provider, provider_order_id, amount_paise, status, "
                "idempotency_key, created_at, updated_at) VALUES (?, ?, ?, 'razorpay', ?, ?, 'pending', ?, ?, ?)",
                (self.payment_id, self.shop_id, self.order["id"], self.provider_order_id,
                 self.order["total_paise"], "provider-payment-attempt-001", now_iso(), now_iso()),
            )

    def tearDown(self):
        self.conn.close()
        (config.DATABASE, config.STORAGE, config.RAZORPAY_KEY_ID,
         config.RAZORPAY_KEY_SECRET, config.RAZORPAY_WEBHOOK_SECRET) = self.old_values
        self.temp.cleanup()

    def callback_signature(self, payment_id="pay_provider_001"):
        message = f"{self.provider_order_id}|{payment_id}".encode()
        return hmac.new(config.RAZORPAY_KEY_SECRET.encode(), message, hashlib.sha256).hexdigest()

    def test_callback_requires_valid_signature_and_provider_captured_amount(self):
        with patch("smarth_xerox.payments._razorpay_request") as request:
            with self.assertRaisesRegex(ValueError, "could not be verified"):
                verify_callback(self.conn, self.shop_id, self.order["id"], self.tracking_token,
                                self.provider_order_id, "pay_provider_001", "not-a-signature")
            request.assert_not_called()
        with patch("smarth_xerox.payments._razorpay_request", return_value={
            "order_id": self.provider_order_id, "amount": self.order["total_paise"] - 1,
            "currency": "INR", "status": "captured",
        }):
            with self.assertRaisesRegex(ValueError, "not captured"):
                verify_callback(self.conn, self.shop_id, self.order["id"], self.tracking_token,
                                self.provider_order_id, "pay_provider_001", self.callback_signature())
        self.assertEqual(self.conn.execute("SELECT payment_status FROM orders WHERE id = ?", (self.order["id"],)).fetchone()[0], "pending")
        with patch("smarth_xerox.payments._razorpay_request", return_value={
            "order_id": self.provider_order_id, "amount": self.order["total_paise"],
            "currency": "INR", "status": "captured",
        }):
            result = verify_callback(self.conn, self.shop_id, self.order["id"], self.tracking_token,
                                     self.provider_order_id, "pay_provider_001", self.callback_signature())
        self.assertEqual(result["payment_status"], "paid")
        self.assertEqual(self.conn.execute("SELECT payment_status FROM orders WHERE id = ?", (self.order["id"],)).fetchone()[0], "paid")

    def sign_webhook(self, body):
        return hmac.new(config.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()

    def test_webhook_is_signed_deduplicated_and_refund_events_update_order_state(self):
        event = {"event": "payment.captured", "payload": {"payment": {"entity": {
            "id": "pay_provider_001", "order_id": self.provider_order_id,
            "amount": self.order["total_paise"], "currency": "INR", "status": "captured",
        }}}}
        body = json.dumps(event, separators=(",", ":")).encode()
        with self.assertRaisesRegex(ValueError, "Invalid payment webhook"):
            process_webhook(self.conn, body, "invalid", "event-captured-001")
        self.assertEqual(process_webhook(self.conn, body, self.sign_webhook(body), "event-captured-001"), "paid")
        self.assertEqual(process_webhook(self.conn, body, self.sign_webhook(body), "event-captured-001"), "duplicate")
        with self.conn:
            self.conn.execute(
                "INSERT INTO refunds(id, shop_id, payment_id, amount_paise, provider_refund_id, status, created_at, idempotency_key, reason) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                ("refund-001", self.shop_id, self.payment_id, self.order["total_paise"], "rfnd_provider_001",
                 now_iso(), "refund-idempotency-001", "Customer request"),
            )
        refund_event = {"event": "refund.processed", "payload": {"refund": {"entity": {"id": "rfnd_provider_001"}}}}
        refund_body = json.dumps(refund_event, separators=(",", ":")).encode()
        self.assertEqual(process_webhook(self.conn, refund_body, self.sign_webhook(refund_body), "event-refund-001"), "processed")
        self.assertEqual(self.conn.execute("SELECT payment_status FROM orders WHERE id = ?", (self.order["id"],)).fetchone()[0], "refunded")

    def test_webhook_amount_mismatch_never_marks_order_paid(self):
        event = {"event": "payment.captured", "payload": {"payment": {"entity": {
            "id": "pay_mismatch", "order_id": self.provider_order_id,
            "amount": self.order["total_paise"] + 1, "currency": "INR", "status": "captured",
        }}}}
        body = json.dumps(event, separators=(",", ":")).encode()
        self.assertEqual(process_webhook(self.conn, body, self.sign_webhook(body), "event-mismatch-001"), "mismatch")
        self.assertEqual(self.conn.execute("SELECT payment_status FROM orders WHERE id = ?", (self.order["id"],)).fetchone()[0], "pending")
        self.assertEqual(self.conn.execute("SELECT status FROM payments WHERE id = ?", (self.payment_id,)).fetchone()[0], "amount_mismatch")


if __name__ == "__main__":
    unittest.main()
