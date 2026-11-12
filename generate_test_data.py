"""Generates internally consistent master data (vendors, POs, GRNs) and invoice PDFs for the demo.

Run:  python generate_test_data.py
Every number below is consistent with the PO data EXCEPT where a scenario deliberately breaks it.
"""
import csv
import io
import random

import pymupdf as fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

from config import BUYER, DATA_DIR, SAMPLES_DIR
from gstin import make_gstin

DATA_DIR.mkdir(exist_ok=True)
SAMPLES_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------- master data
VENDORS = [
    # id, legal name, aliases, state, PAN, status, bank, ifsc, bank name, address
    ("V001", "Nimbus Office Supplies Pvt Ltd", "Nimbus Office Supplies|Nimbus Office Supplies Private Limited",
     "29", "AAFCN4821P", "ACTIVE", "50200012345678", "HDFC0001234", "HDFC Bank", "14 Residency Road, Bengaluru 560025"),
    ("V002", "Kaveri Industrial Components Ltd", "Kaveri Industrial|Kaveri Industrial Components Limited",
     "33", "AAECK7712R", "ACTIVE", "91002003004005", "UTIB0000456", "Axis Bank", "Plot 22, SIDCO Estate, Coimbatore 641021"),
    ("V003", "BlueOrbit IT Services LLP", "BlueOrbit IT Services|Blue Orbit IT Services LLP",
     "29", "AAKFB3390L", "ACTIVE", "33445566778899", "ICIC0002211", "ICICI Bank", "5th Floor, Prestige Tech Park, Bengaluru 560103"),
    ("V004", "Sharma Packaging Co", "Sharma Packaging Company",
     "09", "AAHFS2201M", "INACTIVE", "12121212121212", "SBIN0004455", "State Bank of India", "Sector 63, Noida 201301"),
    ("V005", "Himalaya Logistics Pvt Ltd", "Himalaya Logistics|Himalaya Logistics Private Limited",
     "07", "AADCH5566N", "ACTIVE", "70707070707070", "KKBK0001122", "Kotak Mahindra Bank", "Okhla Phase II, New Delhi 110020"),
    ("V006", "Zenith Electricals Pvt Ltd", "Zenith Electricals|Zenith Electricals Private Limited",
     "27", "AABCZ9087Q", "ACTIVE", "60606060606060", "HDFC0009988", "HDFC Bank", "MIDC Bhosari, Pune 411026"),
]
VENDOR = {v[0]: v for v in VENDORS}
NO_GRN = {"PO-2026-0107"}

# po_number, vendor_id, po_date, status, [(item_code, description, qty, unit_price)]
POS = [
    ("PO-2026-0101", "V001", "2026-10-20", "OPEN", [
        ("NOS-PAP-01", "A4 Copier Paper 75 GSM (Box of 5 reams)", 40, 1450.00),
        ("NOS-TON-02", "Laser Toner Cartridge 88A", 12, 3200.00),
        ("NOS-CHR-03", "Ergonomic Office Chair", 6, 8750.00)]),
    ("PO-2026-0102", "V002", "2026-10-12", "OPEN", [
        ("KIC-BRG-01", "Deep Groove Ball Bearing 6205", 500, 185.00),
        ("KIC-SFT-02", "Precision Steel Shaft 25mm", 120, 640.00)]),
    ("PO-2026-0103", "V003", "2026-10-01", "OPEN", [
        ("BOS-SUP-01", "L2 Application Support (hours)", 120, 1500.00),
        ("BOS-LIC-02", "Monitoring Tool Licence (annual)", 10, 4800.00)]),
    ("PO-2026-0104", "V006", "2026-10-05", "OPEN", [
        ("ZEL-LED-01", "LED Panel Light 2x2 ft 40W", 100, 2400.00),
        ("ZEL-CAB-02", "FR Copper Cable 2.5 sq mm (90 m roll)", 50, 2150.00)]),
    ("PO-2026-0105", "V005", "2026-10-15", "OPEN", [
        ("HLG-FTL-01", "Full Truck Load Bengaluru to Delhi", 4, 68000.00)]),
    ("PO-2026-0106", "V001", "2026-10-28", "OPEN", [
        ("NOS-WBD-04", "Magnetic Whiteboard 6x4 ft", 10, 4600.00),
        ("NOS-MRK-05", "Whiteboard Marker Set (12 pcs)", 40, 380.00)]),
    # Added for the missing-GRN scenario: goods ordered, NO goods receipt recorded yet
    ("PO-2026-0107", "V006", "2026-11-03", "OPEN", [
        ("ZEL-FAN-03", "Industrial Exhaust Fan 24 in", 20, 5200.00)]),
    ("PO-2026-0090", "V004", "2026-08-02", "CLOSED", [
        ("SPC-BOX-01", "Corrugated Box 5-ply 18x12x12 in", 2000, 42.00)]),
]


def write_master_data():
    with open(DATA_DIR / "vendors.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["vendor_id", "legal_name", "aliases", "state_code", "gstin", "status",
                    "bank_account", "ifsc", "bank_name", "address"])
        for v in VENDORS:
            w.writerow([v[0], v[1], v[2], v[3], make_gstin(v[3], v[4]), v[5], v[6], v[7], v[8], v[9]])
    with open(DATA_DIR / "purchase_orders.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["po_number", "vendor_id", "po_date", "status", "currency", "line_no",
                    "item_code", "description", "quantity", "unit_price"])
        for po, vid, d, st, lines in POS:
            for i, (code, desc, q, p) in enumerate(lines, 1):
                w.writerow([po, vid, d, st, "INR", i, code, desc, q, f"{p:.2f}"])
    # Goods receipts: everything ordered has been received (enables the 3-way quantity check),
    # except POs listed in NO_GRN, which model "invoice arrived before the goods were receipted".
    with open(DATA_DIR / "goods_receipts.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["grn_number", "po_number", "item_code", "received_qty", "grn_date"])
        n = 5001
        for po, vid, d, st, lines in POS:
            if po in NO_GRN:
                continue
            for code, desc, q, p in lines:
                w.writerow([f"GRN-{n}", po, code, q, "2026-11-01"])
                n += 1


# ---------------------------------------------------------------- PDF rendering
def inr(x: float) -> str:
    """Indian digit grouping: 1,48,900.00"""
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, dec = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + dec


def compute(vendor_id, lines):
    sub = round(sum(q * p for _, _, q, p in lines), 2)
    intra = VENDOR[vendor_id][3] == BUYER["state_code"]
    if intra:
        cg = sg = round(sub * 0.09, 2)
        tax = {"CGST @ 9%": cg, "SGST @ 9%": sg}
    else:
        tax = {"IGST @ 18%": round(sub * 0.18, 2)}
    return sub, tax, round(sub + sum(tax.values()), 2)


def render_invoice(path, vendor_id, inv_no, inv_date, po, lines, layout="A", bank_override=None,
                   vendor_name_override=None, gstin_override=None):
    v = VENDOR.get(vendor_id)
    name = vendor_name_override or v[1]
    gstin = gstin_override or make_gstin(v[3], v[4])
    address = v[9] if v else "21 Market Street, Indore 452001"
    bank_acct, ifsc, bank_name = (bank_override or (v[6], v[7], v[8]))
    sub, tax, total = compute(vendor_id, lines) if v else (
        round(sum(q * p for _, _, q, p in lines), 2), {"IGST @ 18%": round(sum(q * p for _, _, q, p in lines) * .18, 2)}, 0)
    if not v:
        total = round(sub + sum(tax.values()), 2)

    c = canvas.Canvas(str(path), pagesize=A4)
    W, H = A4
    x0 = 18 * mm
    y = H - 18 * mm
    accent = colors.HexColor("#1D3557") if layout == "A" else colors.HexColor("#2A6F4E")

    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(accent)
    c.drawString(x0, y, "TAX INVOICE" if layout == "A" else "GST INVOICE - ORIGINAL FOR RECIPIENT")
    y -= 11 * mm
    c.setFillColor(colors.black)
    if layout == "A":
        c.setFont("Helvetica-Bold", 16)
        c.drawString(x0, y, name)
    else:
        c.setFont("Helvetica-Bold", 13)
        c.drawString(x0, y, f"Supplier: {name}")
    y -= 6 * mm
    c.setFont("Helvetica", 9)
    c.drawString(x0, y, address)
    y -= 5 * mm
    c.drawString(x0, y, f"GSTIN: {gstin}")
    y -= 10 * mm

    c.setFont("Helvetica", 10)
    if layout == "A":
        meta = [("Invoice No", inv_no), ("Invoice Date", inv_date), ("PO Number", po)]
    else:
        meta = [("Bill No.", inv_no), ("Dated", inv_date), ("Purchase Order Ref", po)]
    for label, val in meta:
        if val is None:
            continue
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x0, y, f"{label}:")
        c.setFont("Helvetica", 10)
        c.drawString(x0 + 42 * mm, y, val)
        y -= 6 * mm
    y -= 3 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x0, y, "Bill To:")
    c.setFont("Helvetica", 10)
    c.drawString(x0 + 42 * mm, y, BUYER["name"])
    y -= 5 * mm
    c.drawString(x0 + 42 * mm, y, f"Buyer GSTIN: {BUYER['gstin']}")
    y -= 10 * mm

    if layout == "A":
        header = ["S.No", "Item Code", "Description", "Qty", "Unit Price", "Amount"]
        rows = [[str(i), code, desc, f"{q:g}", inr(p), inr(q * p)] for i, (code, desc, q, p) in enumerate(lines, 1)]
        widths = [12 * mm, 26 * mm, 72 * mm, 14 * mm, 25 * mm, 27 * mm]
    else:
        header = ["Item Code", "Particulars", "Qty", "Rate", "Amount"]
        rows = [[code, desc, f"{q:g}", inr(p), inr(q * p)] for code, desc, q, p in lines]
        widths = [28 * mm, 82 * mm, 16 * mm, 25 * mm, 27 * mm]
    t = Table([header] + rows, colWidths=widths)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5") if layout == "A" else colors.HexColor("#E6F2EC")),
        ("ALIGN", (-3, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9AA5B1")),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    tw, th = t.wrapOn(c, W, H)
    t.drawOn(c, x0, y - th)
    y = y - th - 10 * mm

    tot_rows = [("Sub Total" if layout == "A" else "Taxable Value", sub)] + list(tax.items()) + \
               [("Grand Total" if layout == "A" else "Amount Payable", total)]
    for label, val in tot_rows:
        bold = label in ("Grand Total", "Amount Payable")
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        c.drawString(x0 + 100 * mm, y, label)
        c.drawRightString(x0 + 176 * mm, y, f"INR {inr(val)}")
        y -= 6 * mm
    y -= 6 * mm
    c.setFont("Helvetica", 9)
    c.drawString(x0, y, f"Bank: {bank_name}   A/C No: {bank_acct}   IFSC: {ifsc}")
    y -= 5 * mm
    c.drawString(x0, y, "Payment terms: Net 30 days. All amounts in INR.")
    y -= 14 * mm
    c.drawString(x0 + 120 * mm, y, f"For {name}")
    c.drawString(x0 + 120 * mm, y - 12 * mm, "Authorised Signatory")
    c.save()
    return sub, tax, total


def make_scanned(src_pdf, dst_pdf, seed=7):
    """Turn a digital PDF into a poor-quality 'scanned' image-only PDF (low DPI, skew, blur, noise, smudge)."""
    random.seed(seed)
    doc = fitz.open(src_pdf)
    pix = doc[0].get_pixmap(dpi=150)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    img = img.rotate(0.8, expand=True, fillcolor=235)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img = ImageEnhance.Contrast(img).enhance(0.7)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for _ in range(int(w * h * 0.004)):  # speckle noise
        x, y = random.randrange(w), random.randrange(h)
        draw.point((x, y), fill=random.choice([90, 120, 200]))
    # coffee-stain smudge over the invoice-number area
    draw.ellipse((int(w * 0.20), int(h * 0.170), int(w * 0.42), int(h * 0.215)), fill=185)
    img = img.filter(ImageFilter.GaussianBlur(0.4))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=40)
    out = fitz.open()
    page = out.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buf.getvalue())
    out.save(dst_pdf)


def po_lines(po_number, overrides=None):
    """Return lines from the PO, optionally overriding qty / price per item code."""
    overrides = overrides or {}
    for po, vid, d, st, lines in POS:
        if po == po_number:
            out = []
            for code, desc, q, p in lines:
                if code in overrides:
                    o = overrides[code]
                    if o is None:
                        continue
                    q, p = o.get("qty", q), o.get("price", p)
                out.append((code, desc, q, p))
            return out
    raise KeyError(po_number)


def write_samples(overwrite=False):
    """Writes sample PDFs. Existing files are kept unless overwrite=True, so the demo PDFs stay byte-identical."""
    S = SAMPLES_DIR
    _render = globals()["render_invoice"]

    def render_invoice(path, *a, **k):  # noqa: F811 - skip files that already exist
        if path.exists() and not overwrite and not path.name.startswith("_tmp"):
            return None
        return _render(path, *a, **k)
    # Happy path: exact match to PO-2026-0101
    render_invoice(S / "01_happy_path_nimbus.pdf", "V001", "INV-NOS-1042", "03-Nov-2026", "PO-2026-0101",
                   po_lines("PO-2026-0101"))
    # Edge case 2: same invoice re-sent by the vendor, different layout, reformatted number, different filename
    render_invoice(S / "02_duplicate_nimbus_resent_copy.pdf", "V001", "INV/NOS/1042", "03/11/2026", "PO-2026-0101",
                   po_lines("PO-2026-0101"), layout="B")
    # Edge case 1: poor-quality scan (image only)
    tmp = S / "_tmp_kaveri.pdf"
    if overwrite or not (S / "03_scanned_kaveri_poor_quality.pdf").exists():
        render_invoice(tmp, "V002", "KIC/2026/0457", "06-Nov-2026", "PO-2026-0102", po_lines("PO-2026-0102"), layout="A")
        make_scanned(tmp, S / "03_scanned_kaveri_poor_quality.pdf")
        tmp.unlink()
    # Edge case 3: split billing against PO-2026-0104 (100 LED panels + 50 cable rolls)
    render_invoice(S / "04a_split_zenith_part1.pdf", "V006", "ZE-26-0311", "02-Nov-2026", "PO-2026-0104",
                   po_lines("PO-2026-0104", {"ZEL-LED-01": {"qty": 60}, "ZEL-CAB-02": None}))
    render_invoice(S / "04b_split_zenith_part2.pdf", "V006", "ZE-26-0328", "06-Nov-2026", "PO-2026-0104",
                   po_lines("PO-2026-0104", {"ZEL-LED-01": {"qty": 40}}))
    render_invoice(S / "04c_split_zenith_overbill.pdf", "V006", "ZE-26-0342", "10-Nov-2026", "PO-2026-0104",
                   po_lines("PO-2026-0104", {"ZEL-LED-01": {"qty": 20}, "ZEL-CAB-02": None}))
    # Edge case 4: price mismatch (support billed at 1,650/hr vs PO 1,500/hr = +10%)
    render_invoice(S / "05_price_mismatch_blueorbit.pdf", "V003", "BOS/INV/2026/088", "07-Nov-2026", "PO-2026-0103",
                   po_lines("PO-2026-0103", {"BOS-SUP-01": {"qty": 80, "price": 1650.00}}))
    # Extra robustness tests
    render_invoice(S / "06_bank_change_himalaya.pdf", "V005", "HLG-7781", "05-Nov-2026", "PO-2026-0105",
                   po_lines("PO-2026-0105", {"HLG-FTL-01": {"qty": 2}}),
                   bank_override=("99887766554433", "YESB0000777", "Yes Bank"))
    render_invoice(S / "07_inactive_vendor_sharma.pdf", "V004", "SPC-552", "04-Nov-2026", "PO-2026-0090",
                   po_lines("PO-2026-0090", {"SPC-BOX-01": {"qty": 500}}), layout="B")
    render_invoice(S / "08_missing_po_nimbus.pdf", "V001", "INV-NOS-1077", "08-Nov-2026", None,
                   po_lines("PO-2026-0106"))
    if overwrite or not (S / "09_corrupted_file.pdf").exists():
        with open(S / "09_corrupted_file.pdf", "wb") as f:
            f.write(b"This is not really a PDF. " * 40)
    # Unknown vendor (not in vendor master)
    VENDOR["X999"] = ("X999", "Orion Traders", "", "23", "AAGFO1111A", "", "11112222333344", "PUNB0011223", "Punjab National Bank",
                      "21 Market Street, Indore 452001")
    render_invoice(S / "10_unknown_vendor_orion.pdf", "X999", "OT-1190", "09-Nov-2026", "PO-2026-0101",
                   [("OT-GEN-01", "Assorted Stationery Kit", 25, 950.00)])
    del VENDOR["X999"]
    # Missing goods receipt: correct invoice for PO-2026-0107, but nothing has been receipted yet
    render_invoice(S / "11_missing_grn_zenith.pdf", "V006", "ZE-26-0355", "11-Nov-2026", "PO-2026-0107",
                   po_lines("PO-2026-0107"))


if __name__ == "__main__":
    import sys
    write_master_data()
    write_samples(overwrite="--overwrite" in sys.argv)
    print("Master data ->", DATA_DIR)
    print("Sample invoices ->", sorted(p.name for p in SAMPLES_DIR.iterdir()))
