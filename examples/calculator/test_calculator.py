"""Frozen deterministic behavior checks for the disposable calculator."""
import unittest

from calculator import add, multiply


class ExistingAdditionTests(unittest.TestCase):
    def test_positive_integers(self):
        self.assertEqual(add(2, 3), 5)

    def test_negative_integer(self):
        self.assertEqual(add(-7, 2), -5)

    def test_zero(self):
        self.assertEqual(add(0, 9), 9)

    def test_fractional_inputs(self):
        self.assertAlmostEqual(add(1.25, 2.5), 3.75)


class RequiredMultiplicationTests(unittest.TestCase):
    def test_positive_integers(self):
        self.assertEqual(multiply(6, 7), 42)

    def test_mixed_sign(self):
        self.assertEqual(multiply(-6, 7), -42)

    def test_two_negative_inputs(self):
        self.assertEqual(multiply(-6, -7), 42)

    def test_zero(self):
        self.assertEqual(multiply(0, 17), 0)

    def test_fractional_inputs(self):
        self.assertAlmostEqual(multiply(2.5, 4.0), 10.0)

    def test_large_integer(self):
        self.assertEqual(multiply(2**70, 3), 3 * 2**70)


if __name__ == "__main__":
    unittest.main()
