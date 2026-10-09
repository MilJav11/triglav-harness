import unittest

from manager import GATE_NAMES, evaluate_gates


class VerifiedExistingGateTests(unittest.TestCase):
    def all_true(self):
        return {name: True for name, _ in GATE_NAMES}

    def test_verified_existing_does_not_require_fake_repository_change(self):
        facts = self.all_true()

        # Historical/raw fact remains honest: there was no repository diff.
        facts["implementation_changed"] = False

        trusted, reasons = evaluate_gates(facts)

        self.assertTrue(trusted)
        self.assertEqual(reasons, [])

    def test_implementation_valid_fails_closed_when_not_proven(self):
        facts = self.all_true()
        facts["implementation_changed"] = False
        facts["implementation_valid"] = False

        trusted, reasons = evaluate_gates(facts)

        self.assertFalse(trusted)
        self.assertIn(
            "implementation was neither changed nor deterministically re-proven",
            reasons,
        )


if __name__ == "__main__":
    unittest.main()
