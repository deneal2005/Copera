import tempfile
import unittest
from pathlib import Path

from smarth_xerox import config
from smarth_xerox.accounts import DEFAULT_SETTINGS, save_setting, setup_shop, shop_settings
from smarth_xerox.db import connect, migrate
from smarth_xerox.documents import add_document, document_path
from smarth_xerox.orders import create_draft, create_order, find_draft
from smarth_xerox.retention import cleanup_expired


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_config = config.DATABASE, config.STORAGE
        config.DATABASE = Path(self.temp.name) / "retention.sqlite3"
        config.STORAGE = Path(self.temp.name) / "private"
        migrate()
        self.conn = connect()
        with self.conn:
            self.shop_id, self.owner_id = setup_shop(
                self.conn, "North Copy", "north-copy", "owner@example.test", "long retention test password",
            )
            settings = shop_settings(self.conn, self.shop_id)
            settings["general"]["retention_days"] = 1
            save_setting(self.conn, self.shop_id, "general", settings["general"], self.owner_id, tuple(DEFAULT_SETTINGS))
            save_setting(self.conn, self.shop_id, "pricing", {
                "rates": {"A4": {"bw_simplex_per_side": 200, "bw_duplex_per_sheet": 125,
                                  "color_simplex_per_side": 1000, "color_duplex_per_sheet": 750}},
                "minimum_order_paise": 0, "service_fee_paise": 0, "tax_rate_bps": 0,
                "binding_per_copy_paise": 0, "stapling_per_copy_paise": 0,
            }, self.owner_id, tuple(DEFAULT_SETTINGS))

    def tearDown(self):
        self.conn.close()
        config.DATABASE, config.STORAGE = self.old_config
        self.temp.cleanup()

    def _add_pdf(self, key):
        draft, token = create_draft(self.conn, self.shop_id, key)
        doc = add_document(self.conn, self.shop_id, draft["id"], f"{key}.pdf",
                           b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF",
                           shop_settings(self.conn, self.shop_id))
        return draft, token, doc

    def test_cleanup_removes_expired_files_but_keeps_order_and_metadata(self):
        with self.conn:
            expired_draft, _, draft_doc = self._add_pdf("expired-draft")
            self.conn.execute("UPDATE drafts SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                              (expired_draft["id"],))
            _, order_token, order_doc = self._add_pdf("finished-order")
            draft = find_draft(self.conn, self.shop_id, order_token)
            shop = dict(self.conn.execute("SELECT * FROM shops WHERE id = ?", (self.shop_id,)).fetchone())
            order, _, _ = create_order(self.conn, shop, draft, order_token, "Asha", "9876543210", "counter",
                                       "retention-order-001", shop_settings(self.conn, self.shop_id))
            self.conn.execute("UPDATE orders SET status = 'completed' WHERE id = ?", (order["id"],))
            self.conn.execute("UPDATE documents SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                              (order_doc["id"],))

        draft_path = document_path(self.shop_id, draft_doc["_storage_name"])
        order_path = document_path(self.shop_id, order_doc["_storage_name"])
        result = cleanup_expired(self.conn)
        self.assertEqual(result, {"expired_drafts": 1, "expired_order_files": 1})
        self.assertFalse(draft_path.exists())
        self.assertFalse(order_path.exists())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM drafts WHERE id = ?", (expired_draft["id"],)).fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM orders WHERE id = ?", (order["id"],)).fetchone())
        saved_doc = self.conn.execute("SELECT storage_name, original_name FROM documents WHERE id = ?",
                                      (order_doc["id"],)).fetchone()
        self.assertTrue(saved_doc["storage_name"].startswith("expired-"))
        self.assertEqual(saved_doc["original_name"], "finished-order.pdf")
        entry = self.conn.execute("SELECT action FROM audit_log WHERE action = 'documents.retention_cleanup'").fetchone()
        self.assertIsNotNone(entry)


if __name__ == "__main__":
    unittest.main()
