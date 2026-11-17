import unittest

from smarth_xerox.qrcode import _encode, svg


class QrCodeTests(unittest.TestCase):
    def test_short_shop_url_produces_square_matrix_with_finder_patterns(self):
        matrix = _encode("https://shop.example/?shop=north-copy")
        self.assertEqual(len(matrix), 29)  # version 3
        self.assertTrue(matrix[0][0])
        self.assertFalse(matrix[1][1])
        self.assertTrue(matrix[3][3])
        self.assertEqual(len({len(row) for row in matrix}), 1)

    def test_url_too_long_fails_instead_of_returning_a_fake_code(self):
        with self.assertRaisesRegex(ValueError, "too long"):
            _encode("https://example.test/" + "x" * 120)

    def test_svg_is_vector_downloadable(self):
        image = svg("https://shop.example/?shop=copy")
        self.assertIn('viewBox="0 0 ', image)
        self.assertIn("shape-rendering=\"crispEdges\"", image)
        self.assertIn("<rect", image)


if __name__ == "__main__":
    unittest.main()
