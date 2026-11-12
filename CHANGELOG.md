# Changelog: audit revision

Each entry lists what was wrong, what changed, why it matters, and how it was tested.

**1. GST validation** (`rules.validate_gst`, `config.GST_RATE`)
- *Was:* only the total tax and a loose IGST/CGST check. A missing SGST, unequal CGST/SGST, or a wrong IGST amount with the right total could pass.
- *Now:* full regime validation: CGST + SGST present, equal, each half the rate, no IGST for intra-state; IGST only at the full rate for inter-state; the total at the configured rate; the regime is SKIPPED for unknown vendors. The rate is configurable and documented as a demo assumption.
- *Why:* a wrong GST type or split blocks input tax credit.
- *Tested:* 12 unit tests plus 2 end-to-end PDF tests.

**2. GRN / three-way match** (`rules.check_line_quantities`, `config.GRN_REQUIRED`)
- *Was:* when no GRN existed, the receipt check silently didn't run, and the line could still look fully matched.
- *Now:* a missing GRN gives REVIEW when mandatory, or an explicit SKIP when optional. A pass (`GRN_OK`) appears only when a GRN covers the cumulative billed quantity. A new PO-2026-0107 and sample 11 model the missing-GRN case.
- *Tested:* 3 workflow tests and scenario 11.

**3. Pipeline error handling** (`pipeline.py`)
- *Was:* only errors in the first three stages stopped processing. A crash later on (for example in vendor checks) let the remaining checks run on partial context. A database save failure still showed a result as if it had been processed normally.
- *Now:* every stage up to the decision is critical. An error stops processing, marks the later stages skipped, and forces REVIEW with a SYSTEM_ERROR check. Stack traces go into the record's `payload.errors`, shown in an expander. Save failures are marked NOT SAVED, and the UI keeps the result in session, shows a red warning and offers a JSON report.
- *Tested:* critical-failure test, a 4-stage never-approve test, a persistence-failure test, and an AppTest UI test.

**4. Atomic persistence** (`db.save_invoice`)
- *Was:* the audit entry was written in a separate transaction after the invoice.
- *Now:* invoice, lines and audit entry are written in one transaction.
- *Tested:* a forced audit failure leaves no invoice or line rows.

**5. Duplicate policy** (`rules.detect_duplicates`, `db.ACTIVE_STATES`)
- *Was:* the file-hash check matched records of any status, so a rejected file could never be reprocessed. That contradicted the README.
- *Now:* one policy everywhere. Approved and pending invoices count; rejected and duplicate records don't. A resubmission is logged as INFO and earlier records are kept. Fuzzy matches always give REVIEW, and the near-match condition was simplified.
- *Tested:* 5 workflow tests, plus the original scenarios 02 and 01-again.

**6. Human review** (`review.py`, `rules.revalidate_for_approval`, `db.review_decision`)
- *Was:* approval only changed the status, so a stale review could silently overbill a PO.
- *Now:* approval re-validates the PO, quantities, GRN and balance, and checks for duplicates approved meanwhile. Conflicts need an override (checkbox, reason, reviewer). Blocking conflicts can't be overridden. The full details (reviewer, timestamp, accepted findings, overridden validations, re-validation snapshot) are stored as JSON in the audit log. A guarded update prevents double decisions.
- *Tested:* 6 workflow tests and an AppTest UI test.

**7. Explanations and decision trace** (`rules.build_trace`, new SKIP outcome, "Decision trace" tab)
- *Was:* skipped checks were reported as INFO, partial billing wasn't described, and there was no structured trace.
- *Now:*
  - Per-line messages state units billed before, remaining, and received, and whether the billing is partial or completes the line.
  - Price messages state the tolerance.
  - A SKIP outcome exists and never counts as a pass.
  - Every decision stores the checks performed, passed, failed, warned and skipped, the decisive checks, the rule applied, and the next action. All of it is deterministic, with no LLM.
- *Tested:* exercised by every test, with assertions on SKIP and trace contents.

**8. Tests and docs:** 36 new tests (unittest plus Streamlit AppTest, no new dependencies), README rewritten to match the code, CHANGELOG added. Original sample PDFs and CSVs were verified byte-identical, except one appended PO row.
