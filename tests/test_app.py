"""Streamlit UI tests (streamlit.testing AppTest: runs the real app script headlessly, no browser)."""
import sqlite3
import unittest
from unittest import mock

from tests.base import ROOT, TempDBTest
import db
from streamlit.testing.v1 import AppTest

APP = str(ROOT / "app.py")
PAGES = ["Dashboard", "Process invoice", "Review queue", "Invoice explorer", "Audit trail", "Master data & rules"]


def button(at, label):
    return next(b for b in at.button if b.label == label)


class AppTests(TempDBTest):
    def app(self):
        return AppTest.from_file(APP, default_timeout=90).run()

    def test_every_page_renders_empty_and_populated(self):
        at = self.app()
        for p in PAGES:
            at.sidebar.radio[0].set_value(p).run()
            self.assertFalse(at.exception, f"{p} (empty db): {at.exception}")
        for f in ["01_happy_path_nimbus.pdf", "03_scanned_kaveri_poor_quality.pdf", "05_price_mismatch_blueorbit.pdf",
                  "09_corrupted_file.pdf", "11_missing_grn_zenith.pdf"]:
            self.run_file(f)
        for p in PAGES:
            at.sidebar.radio[0].set_value(p).run()
            self.assertFalse(at.exception, f"{p} (with data): {at.exception}")

    def test_process_happy_path_through_ui(self):
        at = self.app()
        at.sidebar.radio[0].set_value("Process invoice").run()
        button(at, "Process invoice").click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('class="word">APPROVE' in m.value for m in at.markdown))
        self.assertEqual(len(db.list_invoices()), 1)
        self.assertTrue(any("saved as record #1" in h.value for h in at.subheader))

    def test_persistence_failure_shown_with_report(self):
        at = self.app()
        at.sidebar.radio[0].set_value("Process invoice").run()
        with mock.patch.object(db, "save_invoice", side_effect=sqlite3.OperationalError("database is locked")):
            button(at, "Process invoice").click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any("NOT saved" in e.value for e in at.error))
        self.assertTrue(any("not saved" in h.value for h in at.subheader))
        self.assertEqual(db.list_invoices(), [])

    def test_review_override_through_ui(self):
        from tests.test_workflow import ReviewTests
        helper = ReviewTests("test_override_is_fully_audited")
        helper.db_path, helper.run_file = self.db_path, self.run_file
        inv_id = ReviewTests._pending_over_po(helper)
        at = self.app()
        at.sidebar.radio[0].set_value("Review queue").run()
        self.assertTrue(any("New or remaining conflicts" in w.value for w in at.warning))
        at.text_area(key=f"c{inv_id}").input("Buyer approved the higher unit price").run()
        at.text_input(key=f"n{inv_id}").input("Asha").run()
        at.button(key=f"a{inv_id}").click().run()                       # no override -> refused
        self.assertTrue(any("Override" in e.value for e in at.error))
        self.assertEqual(db.get_invoice(inv_id)["status"], "PENDING_REVIEW")
        at.checkbox(key=f"ov{inv_id}").check().run()
        at.text_area(key=f"or{inv_id}").input("PO amendment PO-2026-0104-A approved by procurement head").run()
        at.button(key=f"a{inv_id}").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(db.get_invoice(inv_id)["status"], "HUMAN_APPROVED")
        self.assertIn("OVERRIDE", db.list_audit(inv_id)[-1]["action"])


if __name__ == "__main__":
    unittest.main()
