"""APilot configuration: business rules, tolerances and paths live here so they are easy to explain and change."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SAMPLES_DIR = BASE_DIR / "samples"
DB_PATH = Path(os.getenv("APILOT_DB", BASE_DIR / "apilot.db"))

# The company that receives the invoices (our "client")
BUYER = {
    "name": "Acme Retail India Pvt Ltd",
    "gstin": "29AABCA1234K1ZU",   # valid checksum
    "state_code": "29",            # Karnataka
}

# ---- Business-rule tolerances (configurable per client) ----
TOLERANCES = {
    "unit_price_pct": 2.0,     # invoice unit price may exceed PO price by up to 2%
    "rounding_abs": 1.00,      # INR 1 allowed for rounding in arithmetic checks
    "po_value_pct": 1.0,       # invoice may exceed remaining PO value by up to 1%
}
# ---- GST (DEMO ASSUMPTION) ----
# Real GST rates depend on the HSN/SAC code of each item (0%, 5%, 12%, 18%, 28%...).
# The synthetic dataset uses a single 18% rate for every line. In production this would come
# from an item -> HSN -> rate table. Intra-state supply: CGST + SGST (half the rate each).
# Inter-state supply: IGST (the full rate). Override with env var APILOT_GST_RATE.
GST_RATE = float(os.getenv("APILOT_GST_RATE", "0.18"))

# ---- Three-way match (goods receipt) policy ----
# True: every PO line must have a goods receipt (GRN) before payment; missing GRN -> REVIEW.
# False: the GRN check is skipped (reported as SKIP, never as a pass) when no GRN exists.
GRN_REQUIRED = os.getenv("APILOT_GRN_REQUIRED", "true").lower() == "true"

# ---- Human review overrides ----
# If re-validation at approval time finds a new conflict (e.g. PO already consumed by another
# invoice), may a reviewer approve anyway with an explicit, audited override reason?
REVIEW_OVERRIDE_ALLOWED = True
OVERRIDE_REASON_MIN_CHARS = 15
CONFIDENCE_THRESHOLD = 0.85    # critical fields below this go to human review
CRITICAL_FIELDS = ["vendor_name", "invoice_number", "invoice_date", "total"]
DUPLICATE_WINDOW_DAYS = 45     # near-duplicate search window
VENDOR_NAME_MATCH = 88         # rapidfuzz score needed to accept a vendor-name match
LINE_DESC_MATCH = 80           # rapidfuzz score to match an invoice line to a PO line by description
NEAR_DUP_NUMBER_SIM = 70       # similarity of invoice numbers to call a near-duplicate

# ---- Optional AI extraction (Gemini free tier) ----
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT_S = 40


def gemini_api_key():
    """Read the key from env or Streamlit secrets. Returns None if not configured."""
    key = os.getenv("GEMINI_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        return st.secrets.get("GEMINI_API_KEY")
    except Exception:
        return None
