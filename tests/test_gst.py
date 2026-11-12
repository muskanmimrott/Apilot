"""GST regime and amount validation (rules.validate_gst is a pure function)."""
import unittest

from tests.base import ROOT  # noqa: F401  (sets sys.path)
import rules

SUB = 148900.00          # 18% -> 26,802.00 total; 13,401.00 each half


def codes(checks, outcome="FAIL"):
    return {c.code for c in checks if c.outcome == outcome}


class GSTTests(unittest.TestCase):
    def test_correct_cgst_sgst(self):
        out = rules.validate_gst(SUB, 13401.0, 13401.0, None, intra=True)
        self.assertEqual(codes(out), set())
        self.assertIn("TAX_REGIME_OK", codes(out, "PASS"))
        self.assertIn("TAX_OK", codes(out, "PASS"))

    def test_missing_cgst(self):
        out = rules.validate_gst(SUB, None, 13401.0, None, intra=True)
        self.assertIn("MISSING_CGST", codes(out))
        self.assertIn("TAX_MISMATCH", codes(out))           # total tax is then only 9%
        self.assertTrue(all(c.effect == "REVIEW" for c in out if c.outcome == "FAIL"))

    def test_missing_sgst(self):
        out = rules.validate_gst(SUB, 13401.0, None, None, intra=True)
        self.assertIn("MISSING_SGST", codes(out))

    def test_unequal_cgst_sgst_even_when_total_is_right(self):
        out = rules.validate_gst(SUB, 12000.0, 14802.0, None, intra=True)   # sums to the correct 26,802
        self.assertIn("CGST_SGST_UNEQUAL", codes(out))
        self.assertIn("CGST_AMOUNT", codes(out))
        self.assertIn("SGST_AMOUNT", codes(out))
        self.assertIn("TAX_OK", codes(out, "PASS"))           # total is right, proportions are not
        self.assertNotIn("TAX_REGIME_OK", codes(out, "PASS"))

    def test_correct_igst(self):
        out = rules.validate_gst(SUB, None, None, 26802.0, intra=False)
        self.assertEqual(codes(out), set())
        self.assertIn("TAX_REGIME_OK", codes(out, "PASS"))

    def test_incorrect_igst(self):
        out = rules.validate_gst(SUB, None, None, 17868.0, intra=False)   # 12% instead of 18%
        self.assertIn("IGST_AMOUNT", codes(out))
        self.assertIn("TAX_MISMATCH", codes(out))

    def test_missing_igst(self):
        out = rules.validate_gst(SUB, None, None, None, intra=False)
        self.assertIn("NO_TAX", codes(out))

    def test_wrong_regime_igst_on_intra_state(self):
        out = rules.validate_gst(SUB, None, None, 26802.0, intra=True)
        self.assertIn("TAX_REGIME", codes(out))
        self.assertIn("MISSING_CGST", codes(out))
        self.assertIn("MISSING_SGST", codes(out))

    def test_wrong_regime_cgst_sgst_on_inter_state(self):
        out = rules.validate_gst(SUB, 13401.0, 13401.0, None, intra=False)
        self.assertIn("TAX_REGIME", codes(out))
        self.assertIn("MISSING_IGST", codes(out))
        self.assertIn("TAX_OK", codes(out, "PASS"))            # amount right, regime wrong

    def test_unknown_vendor_regime_is_skipped_not_passed(self):
        out = rules.validate_gst(SUB, None, None, 26802.0, intra=None)
        self.assertIn("GST_REGIME_SKIPPED", codes(out, "SKIP"))
        self.assertNotIn("TAX_REGIME_OK", codes(out, "PASS"))

    def test_configurable_rate(self):
        out = rules.validate_gst(10000.0, 600.0, 600.0, None, intra=True, rate=0.12)
        self.assertEqual(codes(out), set())
        out = rules.validate_gst(10000.0, 600.0, 600.0, None, intra=True)   # default 18% -> wrong
        self.assertIn("TAX_MISMATCH", codes(out))

    def test_rounding_tolerance(self):
        out = rules.validate_gst(SUB, 13401.50, 13401.50, None, intra=True)   # within INR 1
        self.assertEqual(codes(out), set())


if __name__ == "__main__":
    unittest.main()
