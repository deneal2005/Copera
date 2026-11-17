import tempfile
import unittest
from pathlib import Path

from smarth_xerox.accounts import authenticate, membership, setup_shop, shop_settings
from smarth_xerox.db import connect, migrate


class DatabaseAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "test.sqlite3"
        migrate(self.database)
        self.conn = connect(self.database)
        with self.conn:
            self.shop_a, self.owner_a = setup_shop(
                self.conn, "North Copy", "north-copy", "owner@example.test", "a long and unique passphrase",
            )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_migrations_are_idempotent_and_shop_setup_is_one_time(self):
        migrate(self.database)
        expected = len(list(Path(__file__).resolve().parents[1].joinpath("migrations").glob("*.sql")))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], expected)
        with self.assertRaisesRegex(ValueError, "already configured"):
            with self.conn:
                setup_shop(self.conn, "Second", "second", "other@example.test", "another long password")

    def test_password_is_authenticated_and_owner_membership_is_shop_scoped(self):
        user = authenticate(self.conn, "OWNER@example.test", "a long and unique passphrase")
        self.assertIsNotNone(user)
        self.assertIsNone(authenticate(self.conn, "owner@example.test", "incorrect passphrase"))
        self.assertEqual(membership(self.conn, user["id"], self.shop_a)["role"], "owner")
        self.assertIsNone(membership(self.conn, user["id"], "another-shop-id"))

    def test_price_configuration_starts_unset(self):
        self.assertEqual(shop_settings(self.conn, self.shop_a)["pricing"]["rates"], {})


if __name__ == "__main__":
    unittest.main()
