import hashlib
import hmac
import unittest

from smarth_xerox.pricing import quote_order, selected_page_count
from smarth_xerox.security import password_hash, verify_password, verify_razorpay_callback, verify_razorpay_webhook


class PageRangeTests(unittest.TestCase):
    def test_counts_unique_selected_pages(self):
        self.assertEqual(selected_page_count("1-5, 8, 10-12", 12), 9)
        self.assertEqual(selected_page_count("1-3,2-4", 4), 4)
        self.assertEqual(selected_page_count("all", 8), 8)

    def test_rejects_invalid_or_out_of_bounds_ranges(self):
        for value in ("0", "5-3", "1-9", "1,,2", "all,2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                selected_page_count(value, 5)


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.pricing = {
            "rates": {"A4": {
                "bw_simplex_per_side": 200,
                "bw_duplex_per_sheet": 250,
                "color_simplex_per_side": 1000,
                "color_duplex_per_sheet": 1500,
            }},
            "minimum_order_paise": 100,
            "service_fee_paise": 20,
            "tax_rate_bps": 500,
            "binding_per_copy_paise": 300,
        }

    def test_duplex_rate_is_independent_and_addons_are_per_copy(self):
        quote = quote_order(self.pricing, [{
            "id": "doc", "original_name": "notes.pdf", "page_count": 5,
            "settings": {"color": "bw", "sides": "double", "paper_size": "A4", "copies": 2, "add_ons": ["binding"]},
        }])
        self.assertEqual(quote["items"][0]["print_units"], 6)
        self.assertEqual(quote["items"][0]["print_paise"], 1500)
        self.assertEqual(quote["items"][0]["add_ons_paise"], 600)
        self.assertEqual(quote["total_paise"], 2226)

    def test_client_cannot_supply_unknown_or_unconfigured_price(self):
        with self.assertRaisesRegex(ValueError, "not configured"):
            quote_order(self.pricing, [{"page_count": 1, "settings": {"paper_size": "A3"}}])

    def test_percentage_discount_is_applied_before_tax(self):
        pricing = {**self.pricing, "minimum_order_paise": 0, "service_fee_paise": 20,
                   "tax_rate_bps": 500, "discount_rate_bps": 1000}
        quote = quote_order(pricing, [{"page_count": 5, "settings": {
            "color": "bw", "sides": "single", "paper_size": "A4", "copies": 1,
        }}])
        self.assertEqual(quote["subtotal_paise"], 1000)
        self.assertEqual(quote["discount_paise"], 100)
        self.assertEqual(quote["tax_paise"], 46)
        self.assertEqual(quote["total_paise"], 966)


class SecurityTests(unittest.TestCase):
    def test_scrypt_password_hash_is_salted_and_verifiable(self):
        first, second = password_hash("correct horse battery staple"), password_hash("correct horse battery staple")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct horse battery staple", first))
        self.assertFalse(verify_password("wrong password", first))

    def test_razorpay_signatures_use_expected_messages(self):
        secret = "test-secret"
        callback = hmac.new(secret.encode(), b"order_1|pay_1", hashlib.sha256).hexdigest()
        raw = b'{"event":"payment.captured"}'
        webhook = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        self.assertTrue(verify_razorpay_callback("order_1", "pay_1", callback, secret))
        self.assertFalse(verify_razorpay_callback("order_1", "pay_2", callback, secret))
        self.assertTrue(verify_razorpay_webhook(raw, webhook, secret))
        self.assertFalse(verify_razorpay_webhook(raw + b" ", webhook, secret))


if __name__ == "__main__":
    unittest.main()
