"""SQLite persistence: invoices, invoice lines (for PO consumption), and an append-only audit log."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    filename TEXT, file_hash TEXT,
    vendor_id TEXT, vendor_name TEXT,
    invoice_number TEXT, invoice_number_norm TEXT, invoice_date TEXT,
    po_number TEXT, currency TEXT,
    subtotal REAL, tax REAL, total REAL,
    decision TEXT NOT NULL,          -- what the system decided: APPROVE / REVIEW / REJECT / DUPLICATE
    status TEXT NOT NULL,            -- current state: APPROVED / PENDING_REVIEW / REJECTED / DUPLICATE / HUMAN_APPROVED / HUMAN_REJECTED
    headline TEXT, next_action TEXT, exception_value REAL DEFAULT 0,
    extraction_method TEXT,
    payload TEXT,                    -- full JSON: extracted fields, checks, stages, PO comparison
    pdf BLOB
);
CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER REFERENCES invoices(id),
    po_number TEXT, item_code TEXT, quantity REAL, unit_price REAL, amount REAL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, invoice_id INTEGER, actor TEXT, action TEXT, details TEXT
);
"""

APPROVED_STATES = ("APPROVED", "HUMAN_APPROVED")
# Duplicate policy: an invoice "still counts" if it was approved or is waiting for review.
# Rejected (by system or reviewer) and DUPLICATE records do not block a resubmission.
ACTIVE_STATES = ("APPROVED", "HUMAN_APPROVED", "PENDING_REVIEW")
STATUS_FOR = {"APPROVE": "APPROVED", "REVIEW": "PENDING_REVIEW", "REJECT": "REJECTED", "DUPLICATE": "DUPLICATE"}


def now():
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def reset():
    with conn() as c:
        c.executescript("DROP TABLE IF EXISTS invoice_lines; DROP TABLE IF EXISTS invoices; DROP TABLE IF EXISTS audit_log;")
    init()


def audit(invoice_id, actor, action, details=""):
    with conn() as c:
        c.execute("INSERT INTO audit_log (ts, invoice_id, actor, action, details) VALUES (?,?,?,?,?)",
                  (now(), invoice_id, actor, action, details))


def save_invoice(rec: dict, lines: list[dict], pdf_bytes: bytes, audit_entry: tuple | None = None) -> int:
    """Invoice, its lines and the first audit entry are written in ONE transaction:
    either all are saved or none are (no half-saved history entries)."""
    with conn() as c:
        cur = c.execute(
            """INSERT INTO invoices (received_at, filename, file_hash, vendor_id, vendor_name, invoice_number,
               invoice_number_norm, invoice_date, po_number, currency, subtotal, tax, total, decision, status,
               headline, next_action, exception_value, extraction_method, payload, pdf)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now(), rec.get("filename"), rec.get("file_hash"), rec.get("vendor_id"), rec.get("vendor_name"),
             rec.get("invoice_number"), rec.get("invoice_number_norm"), rec.get("invoice_date"), rec.get("po_number"),
             rec.get("currency"), rec.get("subtotal"), rec.get("tax"), rec.get("total"), rec["decision"],
             STATUS_FOR[rec["decision"]], rec.get("headline"), rec.get("next_action"), rec.get("exception_value", 0),
             rec.get("extraction_method"), json.dumps(rec.get("payload", {}), default=str), pdf_bytes))
        inv_id = cur.lastrowid
        for ln in lines:
            c.execute("INSERT INTO invoice_lines (invoice_id, po_number, item_code, quantity, unit_price, amount) VALUES (?,?,?,?,?,?)",
                      (inv_id, ln.get("po_number"), ln.get("item_code"), ln.get("quantity"), ln.get("unit_price"), ln.get("amount")))
        if audit_entry:
            actor, action, details = audit_entry
            c.execute("INSERT INTO audit_log (ts, invoice_id, actor, action, details) VALUES (?,?,?,?,?)",
                      (now(), inv_id, actor, action, details))
    return inv_id


def review_decision(invoice_id, new_status, actor, details: dict) -> bool:
    """Human decision on a PENDING_REVIEW invoice. Status change + audit entry are atomic, and the
    update only succeeds if the invoice is still pending (prevents double approval from two tabs)."""
    if new_status not in ("HUMAN_APPROVED", "HUMAN_REJECTED"):
        raise ValueError(new_status)
    with conn() as c:
        cur = c.execute("UPDATE invoices SET status=? WHERE id=? AND status='PENDING_REVIEW'", (new_status, invoice_id))
        if cur.rowcount != 1:
            return False
        action = f"PENDING_REVIEW -> {new_status}" + (" (OVERRIDE)" if details.get("override") else "")
        c.execute("INSERT INTO audit_log (ts, invoice_id, actor, action, details) VALUES (?,?,?,?,?)",
                  (now(), invoice_id, actor, action, json.dumps(details, default=str)))
    return True


def set_status(invoice_id, status, actor, comment):
    """Backwards-compatible wrapper used by older code paths."""
    return review_decision(invoice_id, status, actor, {"comment": comment})


def list_invoices():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, received_at, filename, vendor_id, vendor_name, invoice_number, invoice_date, po_number, currency, "
            "subtotal, tax, total, decision, status, headline, next_action, exception_value, extraction_method "
            "FROM invoices ORDER BY id DESC")]


def get_invoice(invoice_id):
    with conn() as c:
        r = c.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["payload"] = json.loads(d["payload"] or "{}")
    return d


def find_by_hash(file_hash):
    """All earlier records of this exact file, any status (the duplicate rule decides which ones count)."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, invoice_number, status, received_at FROM invoices WHERE file_hash=? ORDER BY id", (file_hash,))]


def prior_invoices_for_vendor(vendor_id):
    """Invoices that 'count' for duplicate detection: approved or still in review.
    Rejected ones are excluded on purpose, so a vendor can legitimately resubmit a corrected invoice."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, invoice_number, invoice_number_norm, invoice_date, total, status, received_at FROM invoices "
            f"WHERE vendor_id=? AND status IN {ACTIVE_STATES}", (vendor_id,))]


def billed_against_po(po_number, exclude_id=None):
    """Quantities and value already billed on approved invoices -> {item_code: qty}, value, invoice count.
    exclude_id lets the review re-validation leave out the invoice being approved."""
    ex = exclude_id or -1
    with conn() as c:
        rows = c.execute(
            f"SELECT l.item_code, SUM(l.quantity) q, SUM(l.amount) a FROM invoice_lines l JOIN invoices i ON i.id=l.invoice_id "
            f"WHERE l.po_number=? AND i.status IN {APPROVED_STATES} AND i.id<>? GROUP BY l.item_code", (po_number, ex)).fetchall()
        n = c.execute(f"SELECT COUNT(*) FROM invoices WHERE po_number=? AND status IN {APPROVED_STATES} AND id<>?",
                      (po_number, ex)).fetchone()[0]
    return {r["item_code"]: r["q"] for r in rows}, round(sum(r["a"] or 0 for r in rows), 2), n


def invoice_lines(invoice_id):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM invoice_lines WHERE invoice_id=?", (invoice_id,))]


def approved_same_number(vendor_id, number_norm, exclude_id):
    """Another invoice with the same normalised number approved meanwhile (e.g. while this one sat in review)."""
    if not vendor_id or not number_norm:
        return []
    with conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT id, invoice_number, status FROM invoices WHERE vendor_id=? AND invoice_number_norm=? "
            f"AND id<>? AND status IN {APPROVED_STATES}", (vendor_id, number_norm, exclude_id))]


def pending_against_po(po_number):
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM invoices WHERE po_number=? AND status='PENDING_REVIEW'",
                         (po_number,)).fetchone()[0]


def list_audit(invoice_id=None):
    with conn() as c:
        q = "SELECT a.ts, a.invoice_id, i.invoice_number, i.vendor_name, a.actor, a.action, a.details FROM audit_log a " \
            "LEFT JOIN invoices i ON i.id=a.invoice_id"
        if invoice_id:
            return [dict(r) for r in c.execute(q + " WHERE a.invoice_id=? ORDER BY a.id", (invoice_id,))]
        return [dict(r) for r in c.execute(q + " ORDER BY a.id DESC")]
