"""Create a fresh, committed multi-file checkout defect; never reset an existing repo."""
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "runs" / "checkout-smoke"
root.mkdir(exist_ok=False)
files = {
    ".gitignore": "__pycache__/\n*.pyc\n",
    "README.md": """Checkout contract

Each line is a dict with string unit_price and integer quantity.
Round each extended line to cents using decimal ROUND_HALF_UP before summing.
Add shipping exactly once for a nonempty cart; an empty cart costs zero.
Return Decimal with two fractional digits. Never mutate the input lines.
Run: python -m unittest discover -v
""",
    "money.py": """from decimal import Decimal, ROUND_HALF_EVEN


def cents(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
""",
    "checkout.py": """from decimal import Decimal
from money import cents


def total(lines, shipping='0.00'):
    amount = Decimal('0.00')
    for line in lines:
        amount += cents(line['unit_price']) * line['quantity'] + cents(shipping)
    return cents(amount)
""",
    "test_checkout.py": """import copy
import unittest
from decimal import Decimal
from checkout import total
from money import cents


class CheckoutTests(unittest.TestCase):
    def test_half_cent_rounds_up(self):
        self.assertEqual(cents('1.005'), Decimal('1.01'))

    def test_negative_half_cent(self):
        self.assertEqual(cents('-1.005'), Decimal('-1.01'))

    def test_extension_is_rounded_after_multiplication(self):
        self.assertEqual(total([{'unit_price': '0.335', 'quantity': 3}]), Decimal('1.01'))

    def test_shipping_only_once(self):
        lines = [{'unit_price': '2.00', 'quantity': 2}, {'unit_price': '3.00', 'quantity': 1}]
        self.assertEqual(total(lines, '4.50'), Decimal('11.50'))

    def test_each_line_is_rounded_before_sum(self):
        lines = [{'unit_price': '0.005', 'quantity': 1}, {'unit_price': '0.005', 'quantity': 1}]
        self.assertEqual(total(lines), Decimal('0.02'))

    def test_empty_cart_has_no_shipping(self):
        self.assertEqual(total([], '4.50'), Decimal('0.00'))

    def test_does_not_mutate(self):
        lines = [{'unit_price': '2.25', 'quantity': 3}]
        before = copy.deepcopy(lines)
        self.assertEqual(total(lines), Decimal('6.75'))
        self.assertEqual(lines, before)

    def test_fixed_decimal_scale(self):
        result = total([{'unit_price': '1', 'quantity': 1}])
        self.assertIsInstance(result, Decimal)
        self.assertEqual(result.as_tuple().exponent, -2)
""",
}
for name, content in files.items():
    (root / name).write_text(content, encoding="utf-8")
def git(*args):
    subprocess.run(["git", "-c", f"safe.directory={root}", *args], cwd=root, check=True)
git("init", "-q")
git("add", ".")
git("-c", "user.name=Harness Smoke", "-c", "user.email=smoke@localhost", "commit", "-qm", "Checkout defect fixture")
print(root)
