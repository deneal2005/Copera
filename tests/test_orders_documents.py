import json
import tempfile
import unittest
from pathlib import Path

from smarth_xerox import config
from smarth_xerox.accounts import setup_shop
from smarth_xerox.db import connect, migrate
from smarth_xerox.documents import validate_document, add_document
from smarth_xerox.orders import (create_draft, create_order, find_draft,
                                 finalize_page_count_review, tracking_order)


class DocumentOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "test.sqlite3"
        migrate(self.database)
        self.conn = connect(self.database)
        self.old_storage = config.STORAGE
        config.STORAGE = Path(self.temp.name) / "private"
        with self.conn:
            self.shop_id, self.owner_id = setup_shop(
                self.conn, "North Copy", "north-copy", "owner@example.test", "a long and unique passphrase",
            )
        self.shop = dict(self.conn.execute("SELECT * FROM shops WHERE id = ?", (self.shop_id,)).fetchone())
        self.shop_config = json.loads(json.dumps({
            "profile": {"service_available": True},
            "general": {"max_file_bytes": 1024 * 1024, "max_files": 10, "max_pages": 50,
                        "formats": ["pdf"], "cash_allowed": True, "enabled_paper_sizes": ["A4"], "enabled_add_ons": []},
            "printing": {"max_copies": 20},
            "pricing": {"rates": {"A4": {"bw_simplex_per_side": 200, "bw_duplex_per_sheet": 125}},
                        "minimum_order_paise": 0, "service_fee_paise": 0, "tax_rate_bps": 0},
        }))

    def tearDown(self):
        self.conn.close()
        config.STORAGE = self.old_storage
        self.temp.cleanup()

    def test_validates_document_signature_and_counts_pages(self):
        pdf = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF"
        parsed = validate_document(pdf, "lecture.pdf", self.shop_config)
        self.assertEqual(parsed[3], 1)
        self.assertFalse(parsed[4])
        with self.assertRaisesRegex(ValueError, "do not match"):
            validate_document(b"not a pdf", "lecture.pdf", self.shop_config)

    def test_order_is_server_priced_idempotent_and_tracked_with_secret(self):
        pdf = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF"
        with self.conn:
            draft, token = create_draft(self.conn, self.shop_id, "draft-session-1")
            stored = add_document(self.conn, self.shop_id, draft["id"], "lecture.pdf", pdf, self.shop_config)
            doc_settings = {"color": "bw", "sides": "single", "paper_size": "A4", "copies": 2,
                            "page_selection": "all", "add_ons": []}
            self.conn.execute("UPDATE documents SET settings_json = ? WHERE id = ?",
                              (json.dumps(doc_settings), stored["id"]))
            current_draft = find_draft(self.conn, self.shop_id, token)
            order, track_secret, replayed = create_order(
                self.conn, self.shop, current_draft, token, "Asha", "9876543210", "counter", "order-request-1", self.shop_config,
            )
            repeated, repeated_secret, repeated_flag = create_order(
                self.conn, self.shop, current_draft, token, "Asha", "9876543210", "counter", "order-request-1", self.shop_config,
            )
        self.assertFalse(replayed)
        self.assertTrue(repeated_flag)
        self.assertEqual(order["id"], repeated["id"])
        self.assertEqual(track_secret, repeated_secret)
        self.assertEqual(order["total_paise"], 400)
        self.assertTrue(order["ticket"].startswith("XRX-"))
        self.assertIsNotNone(tracking_order(self.conn, self.shop_id, order["ticket"], token))
        self.assertIsNone(tracking_order(self.conn, "other-shop", order["ticket"], token))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM documents WHERE id = ? AND draft_id IS NOT NULL", (stored["id"],)).fetchone())

    def test_idempotency_key_cannot_be_reused_with_changed_details(self):
        pdf = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF"
        with self.conn:
            draft, token = create_draft(self.conn, self.shop_id, "draft-session-2")
            stored = add_document(self.conn, self.shop_id, draft["id"], "lecture.pdf", pdf, self.shop_config)
            current_draft = find_draft(self.conn, self.shop_id, token)
            create_order(self.conn, self.shop, current_draft, token, "Asha", "", "counter", "same-key", self.shop_config)
            with self.assertRaisesRegex(ValueError, "different details"):
                create_order(self.conn, self.shop, current_draft, token, "Anya", "", "counter", "same-key", self.shop_config)

    def test_unverified_pdf_waits_for_staff_price_review_before_payment(self):
        pdf = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF"
        with self.conn:
            draft, token = create_draft(self.conn, self.shop_id, "draft-review-1")
            stored = add_document(self.conn, self.shop_id, draft["id"], "scanned.pdf", pdf, self.shop_config)
            self.assertTrue(stored["needs_review"])
            current = find_draft(self.conn, self.shop_id, token)
            order, tracking_secret, replay = create_order(
                self.conn, self.shop, current, token, "Asha", "9876543210", "review", "review-order-1", self.shop_config,
            )
        self.assertFalse(replay)
        self.assertEqual(order["status"], "payment_verification")
        self.assertTrue(order["needs_review"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM payments WHERE order_id = ?", (order["id"],)).fetchone()[0], 0)
        with self.conn:
            finalized = finalize_page_count_review(
                self.conn, self.shop_id, order["id"], {stored["id"]: 7}, "counter", self.shop_config, self.owner_id,
            )
        self.assertEqual(finalized["status"], "awaiting_payment")
        self.assertEqual(finalized["total_paise"], 1400)
        self.assertEqual(self.conn.execute("SELECT page_count FROM documents WHERE id = ?", (stored["id"],)).fetchone()[0], 7)
        tracking = tracking_order(self.conn, self.shop_id, order["ticket"], tracking_secret)
        self.assertEqual(tracking["customer_notice"], "The shop confirmed your document pages. The order total is ₹14.00. Pay at the counter when you arrive.")
        self.assertEqual(self.conn.execute("SELECT amount_paise FROM payments WHERE order_id = ?", (order["id"],)).fetchone()[0], 1400)


if __name__ == "__main__":
    unittest.main()
