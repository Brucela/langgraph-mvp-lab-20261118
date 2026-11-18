import os
import unittest

from demo import calculator


class CalculatorTest(unittest.TestCase):
    def test_subtotal(self):
        self.assertEqual(calculator.subtotal([100, 199, 50]), 349)
        self.assertEqual(calculator.subtotal([]), 0)

    @unittest.skipUnless(os.getenv("MVP_REQUIRE_DISCOUNT") == "1", "Issue #1 acceptance cases")
    def test_apply_discount_in_integer_cents(self):
        apply_discount = calculator.apply_discount
        self.assertEqual(apply_discount(1000, 15), 850)
        self.assertEqual(apply_discount(999, 10), 899)
        self.assertEqual(apply_discount(101, 50), 50)
        self.assertEqual(apply_discount(32, 0), 32)
        self.assertEqual(apply_discount(32, 100), 0)
        for cents, percent in [(-1, 25), (100, -1), (100, 101)]:
            with self.subTest(cents=cents, percent=percent):
                with self.assertRaises(ValueError):
                    apply_discount(cents, percent)
