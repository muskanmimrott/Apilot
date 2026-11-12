"""End-to-end test: runs every sample invoice through the real pipeline on a fresh database
and asserts the expected business decision. Run:  python test_pipeline.py"""
import os
import sys
import tempfile

os.environ["APILOT_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
import db  # noqa: E402
import pipeline  # noqa: E402
from config import SAMPLES_DIR  # noqa: E402

# Order matters: duplicates and split billing depend on history.
EXPECTED = [
    ("01_happy_path_nimbus.pdf", "APPROVE"),
    ("02_duplicate_nimbus_resent_copy.pdf", "DUPLICATE"),
    ("01_happy_path_nimbus.pdf", "DUPLICATE"),            # exact same file uploaded twice
    ("03_scanned_kaveri_poor_quality.pdf", "REVIEW"),
    ("04a_split_zenith_part1.pdf", "APPROVE"),
    ("04b_split_zenith_part2.pdf", "APPROVE"),
    ("04c_split_zenith_overbill.pdf", "REJECT"),
    ("05_price_mismatch_blueorbit.pdf", "REVIEW"),
    ("06_bank_change_himalaya.pdf", "REVIEW"),
    ("07_inactive_vendor_sharma.pdf", "REJECT"),
    ("08_missing_po_nimbus.pdf", "REVIEW"),
    ("09_corrupted_file.pdf", "REJECT"),
    ("10_unknown_vendor_orion.pdf", "REJECT"),
]
# Scenarios added after the original 13 (kept separate so the original list is never weakened)
ADDED = [
    ("11_missing_grn_zenith.pdf", "REVIEW"),               # goods not receipted yet -> 3-way match cannot pass
]
EXPECTED = EXPECTED + ADDED

db.init()
fails = 0
for name, want in EXPECTED:
    res = pipeline.run((SAMPLES_DIR / name).read_bytes(), name)
    got = res["decision"].decision
    errs = [s for s in res["stages"] if s.status == "error"]
    ok = got == want and not errs
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name:40s} expected {want:9s} got {got:9s} | {res['decision'].headline[:110]}")
    for s in errs:
        print("      STAGE ERROR:", s.name, s.summary, s.details.get("trace", "")[-600:])
print(f"\n{len(EXPECTED) - fails}/{len(EXPECTED)} scenarios behave as expected "
      f"({len(EXPECTED) - len(ADDED)} original + {len(ADDED)} added).")
sys.exit(1 if fails else 0)
