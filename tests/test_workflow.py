"""End-to-end tests of the pipeline, duplicate policy, error handling, persistence and human review."""
import json
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from tests.base import ROOT, TempDBTest
import config
import db
import extraction
import generate_test_data as gen
import pipeline
import review
import rules


def make_pdf(path, vendor_id, inv_no, po, lines, tax_override=None):
    """Render a synthetic invoice; tax_override replaces the GST block to create deliberate GST errors."""
    if tax_override is None:
        gen.render_invoice(path, vendor_id, inv_no, "09-Nov-2026", po, lines)
        return
    real = gen.compute

    def fake(vid, ls):
        sub, _, _ = real(vid, ls)
        return sub, tax_override, round(sub + sum(tax_override.values()), 2)
    with mock.patch.object(gen, "compute", fake):
        gen.render_invoice(path, vendor_id, inv_no, "09-Nov-2026", po, lines)


class GRNTests(TempDBTest):
    def test_missing_grn_mandatory_goes_to_review(self):
        res = self.run_file("11_missing_grn_zenith.pdf")
        self.assertEqual(res["decision"].decision, "REVIEW")
        self.assertIn("GRN_MISSING", self.codes(res, "FAIL"))
        self.assertNotIn("GRN_OK", self.codes(res, "PASS"))

    def test_missing_grn_optional_is_skipped_never_passed(self):
        config.GRN_REQUIRED = False
        res = self.run_file("11_missing_grn_zenith.pdf")
        self.assertEqual(res["decision"].decision, "APPROVE")
        self.assertIn("GRN_SKIPPED", self.codes(res, "SKIP"))
        self.assertNotIn("GRN_OK", self.codes(res, "PASS"))
        self.assertTrue(any("Goods received" in s for s in res["decision"].trace["skipped"]))

    def test_happy_path_has_real_three_way_pass(self):
        res = self.run_file("01_happy_path_nimbus.pdf")
        self.assertEqual(res["decision"].decision, "APPROVE")
        self.assertEqual(self.codes(res).count("GRN_OK"), 3)


class GSTEndToEndTests(TempDBTest):
    def test_igst_charged_on_intra_state_invoice(self):
        p = self.db_path.parent / "igst_wrong.pdf"
        make_pdf(p, "V001", "INV-NOS-2001", "PO-2026-0101", gen.po_lines("PO-2026-0101"),
                 tax_override={"IGST @ 18%": 26802.00})
        res = self.run_file(p)
        self.assertEqual(res["decision"].decision, "REVIEW")
        self.assertIn("TAX_REGIME", self.codes(res, "FAIL"))

    def test_unequal_cgst_sgst_end_to_end(self):
        p = self.db_path.parent / "split_wrong.pdf"
        make_pdf(p, "V001", "INV-NOS-2002", "PO-2026-0101", gen.po_lines("PO-2026-0101"),
                 tax_override={"CGST @ 9%": 12000.00, "SGST @ 9%": 14802.00})
        res = self.run_file(p)
        self.assertEqual(res["decision"].decision, "REVIEW")
        self.assertIn("CGST_SGST_UNEQUAL", self.codes(res, "FAIL"))


class ErrorHandlingTests(TempDBTest):
    def test_critical_stage_failure_stops_and_reviews(self):
        with mock.patch.object(rules, "verify_vendor", side_effect=RuntimeError("vendor service down")):
            res = self.run_file("01_happy_path_nimbus.pdf")      # would otherwise be APPROVE
        d = res["decision"]
        self.assertEqual(d.decision, "REVIEW")
        st = {s.key: s.status for s in res["stages"]}
        self.assertEqual(st["vendor"], "error")
        for k in ("duplicate", "po", "financial"):
            self.assertEqual(st[k], "skipped", k)
        self.assertIn("SYSTEM_ERROR", self.codes(res))
        self.assertTrue(res["saved"])
        rec = db.get_invoice(res["id"])
        self.assertEqual(rec["status"], "PENDING_REVIEW")
        self.assertIn("vendor service down", rec["payload"]["errors"][0]["trace"])   # stack trace kept internally

    def test_failure_in_early_stage_never_approves(self):
        for target in [(extraction, "get_lines"), (rules, "match_po"), (rules, "validate_financials"), (rules, "decide")]:
            with self.subTest(target=target[1]), mock.patch.object(*target, side_effect=ValueError("boom")):
                res = self.run_file("04a_split_zenith_part1.pdf")
                self.assertEqual(res["decision"].decision, "REVIEW")
                db.reset()

    def test_persistence_failure_is_not_reported_as_saved(self):
        with mock.patch.object(db, "save_invoice", side_effect=sqlite3.OperationalError("disk I/O error")):
            res = self.run_file("01_happy_path_nimbus.pdf")
        self.assertFalse(res["saved"])
        self.assertIsNone(res["id"])
        self.assertIn("disk I/O error", res["persist_error"])
        persist = [s for s in res["stages"] if s.key == "persist"][0]
        self.assertEqual(persist.status, "fail")
        self.assertIn("NOT SAVED", persist.summary)
        self.assertEqual(db.list_invoices(), [])          # no phantom history entry
        self.assertEqual(db.list_audit(), [])
        rep = json.loads(json.dumps(pipeline.report(res), default=str))   # downloadable report is serialisable
        self.assertFalse(rep["saved"])
        self.assertEqual(rep["decision"]["decision"], "APPROVE")

    def test_save_is_atomic(self):
        """Invoice row, its lines and the audit entry share one transaction: if the audit insert fails,
        nothing remains (no history entry without an audit record)."""
        stamps = iter([db.now(), None])          # invoice gets a timestamp; audit entry gets NULL -> NOT NULL violation
        with mock.patch.object(db, "now", lambda: next(stamps)):
            res = self.run_file("01_happy_path_nimbus.pdf")
        self.assertFalse(res["saved"])
        self.assertIn("NOT NULL", res["persist_error"])
        self.assertEqual(db.list_invoices(), [])
        with db.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoice_lines").fetchone()[0], 0)


class DuplicatePolicyTests(TempDBTest):
    def test_rejected_invoice_can_be_reprocessed(self):
        first = self.run_file("07_inactive_vendor_sharma.pdf")
        second = self.run_file("07_inactive_vendor_sharma.pdf")
        self.assertEqual(first["decision"].decision, "REJECT")
        self.assertEqual(second["decision"].decision, "REJECT")     # re-evaluated, not called a duplicate
        self.assertIn("DUP_FILE_RESUBMIT", self.codes(second, "INFO"))
        self.assertEqual(len(db.list_invoices()), 2)                 # earlier record retained
        self.assertEqual(len(db.list_audit(first["id"])), 1)

    def test_human_rejected_invoice_can_be_resubmitted(self):
        r1 = self.run_file("05_price_mismatch_blueorbit.pdf")
        review.reject(r1["id"], "Tester", "Price not agreed, vendor to send corrected invoice")
        r2 = self.run_file("05_price_mismatch_blueorbit.pdf")
        self.assertEqual(r2["decision"].decision, "REVIEW")

    def test_pending_review_invoice_counts_as_duplicate(self):
        self.run_file("05_price_mismatch_blueorbit.pdf")
        r2 = self.run_file("05_price_mismatch_blueorbit.pdf")
        self.assertEqual(r2["decision"].decision, "DUPLICATE")

    def test_normalised_number_duplicate(self):
        self.run_file("01_happy_path_nimbus.pdf")
        r = self.run_file("02_duplicate_nimbus_resent_copy.pdf")
        self.assertEqual(r["decision"].decision, "DUPLICATE")
        self.assertIn("DUP_NUMBER", self.codes(r, "FAIL"))

    def test_fuzzy_match_is_review_not_rejection(self):
        db.save_invoice({"filename": "x", "file_hash": "h", "vendor_id": "V001", "vendor_name": "Nimbus",
                         "invoice_number": "INV-NOS-1043", "invoice_number_norm": "NOS1043", "invoice_date": "2026-11-01",
                         "total": 175702.0, "decision": "APPROVE", "payload": {}}, [], b"")
        r = self.run_file("01_happy_path_nimbus.pdf")
        self.assertEqual(r["decision"].decision, "REVIEW")
        self.assertIn("DUP_NEAR", self.codes(r, "FAIL"))


class ReviewTests(TempDBTest):
    def _pending_over_po(self):
        """Pending invoice for the last 40 LED panels; then another invoice consumes them."""
        self.run_file("04a_split_zenith_part1.pdf")                        # 60 of 100 approved
        p = self.db_path.parent / "zenith_pricey.pdf"
        make_pdf(p, "V006", "ZE-26-0399", "PO-2026-0104", [("ZEL-LED-01", "LED Panel Light 2x2 ft 40W", 40, 2500.00)])
        pending = self.run_file(p)                                           # +4.2% price -> REVIEW
        self.assertEqual(pending["decision"].decision, "REVIEW")
        self.assertEqual(self.run_file("04b_split_zenith_part2.pdf")["decision"].decision, "APPROVE")   # uses the last 40
        return pending["id"]

    def test_revalidation_detects_po_consumed_meanwhile(self):
        inv_id = self._pending_over_po()
        reval = rules.revalidate_for_approval(db.get_invoice(inv_id))
        self.assertTrue(any("ZEL-LED-01" in c and "already billed" in c for c in reval["conflicts"]), reval)
        self.assertTrue(any("would overbill" in c for c in reval["conflicts"]), reval)
        with self.assertRaises(review.ReviewError):
            review.approve(inv_id, "Asha", "Price increase agreed by buyer")          # no override
        with self.assertRaises(review.ReviewError):
            review.approve(inv_id, "Asha", "Price agreed", override=True, override_reason="ok")   # reason too short
        self.assertEqual(db.get_invoice(inv_id)["status"], "PENDING_REVIEW")

    def test_override_is_fully_audited(self):
        inv_id = self._pending_over_po()
        review.approve(inv_id, "Asha (AP lead)", "Price increase agreed by buyer", override=True,
                       override_reason="PO amendment PO-2026-0104-A raises LED qty to 140, approved by procurement")
        self.assertEqual(db.get_invoice(inv_id)["status"], "HUMAN_APPROVED")
        entry = db.list_audit(inv_id)[-1]
        self.assertEqual(entry["actor"], "Asha (AP lead)")
        self.assertIn("OVERRIDE", entry["action"])
        details = json.loads(entry["details"])
        self.assertTrue(details["override"])
        self.assertIn("PO-2026-0104-A", details["override_reason"])
        self.assertTrue(details["overridden_validations"])
        self.assertTrue(details["decided_at"])
        self.assertTrue(details["accepted_review_findings"])

    def test_clean_approval_and_double_approval_guard(self):
        r = self.run_file("05_price_mismatch_blueorbit.pdf")
        out = review.approve(r["id"], "Ravi", "Rate card revision confirmed by buyer email")
        self.assertFalse(out["override"])
        with self.assertRaises(review.ReviewError):
            review.approve(r["id"], "Ravi", "again")

    def test_required_fields(self):
        r = self.run_file("05_price_mismatch_blueorbit.pdf")
        with self.assertRaises(review.ReviewError):
            review.approve(r["id"], "", "note")
        with self.assertRaises(review.ReviewError):
            review.approve(r["id"], "Ravi", "  ")

    def test_duplicate_approved_meanwhile_blocks_approval(self):
        r = self.run_file("05_price_mismatch_blueorbit.pdf")
        db.save_invoice({"filename": "x", "file_hash": "h2", "vendor_id": "V003", "invoice_number": "BOS-INV-2026-088",
                         "invoice_number_norm": rules.norm_invoice_number("BOS/INV/2026/088"), "total": 1.0,
                         "decision": "APPROVE", "payload": {}}, [], b"")
        reval = rules.revalidate_for_approval(db.get_invoice(r["id"]))
        self.assertTrue(reval["blocking"])
        with self.assertRaises(review.ReviewError):
            review.approve(r["id"], "Ravi", "note", override=True, override_reason="trying to override a duplicate")


class PersistenceTests(TempDBTest):
    def test_history_survives_restart(self):
        r = self.run_file("01_happy_path_nimbus.pdf")
        code = ("import sys; sys.path.insert(0, %r); import db; db.DB_PATH = %r; "
                "rows = db.list_invoices(); print(len(rows), rows[0]['status'], len(db.list_audit()))") % (str(ROOT), str(self.db_path))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out, ["1", "APPROVED", "1"])
        self.assertIsNotNone(r["id"])


if __name__ == "__main__":
    unittest.main()
