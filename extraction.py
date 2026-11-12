"""Document understanding: PDF -> text lines (with confidence) -> structured invoice fields.

Design: two independent extractors.
  1. Rule-based parser (always runs, local, free, deterministic).
  2. Gemini (optional, free tier) for layouts the rules don't know.
When both run, agreement raises confidence and disagreement lowers it. Missing values stay None (never invented).
"""
import hashlib
import json
import re
from datetime import datetime

import pymupdf
import pytesseract
import requests
from PIL import Image, ImageFilter, ImageOps

from config import BUYER, GEMINI_MODEL, GEMINI_TIMEOUT_S
from models import ExtractedInvoice, FieldValue, LineItem


class IngestError(Exception):
    pass


# ------------------------------------------------------------------ 1. intake
def ingest(pdf_bytes: bytes):
    if not pdf_bytes:
        raise IngestError("The file is empty.")
    if b"%PDF" not in pdf_bytes[:1024]:
        raise IngestError("The file is not a PDF (missing PDF header). Ask the vendor to resend a valid PDF.")
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:  # corrupted
        raise IngestError(f"The PDF could not be opened: {e}")
    if doc.needs_pass:
        raise IngestError("The PDF is password-protected.")
    if doc.page_count == 0:
        raise IngestError("The PDF has no pages.")
    info = {"pages": doc.page_count, "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "size_kb": round(len(pdf_bytes) / 1024, 1)}
    return doc, info


# ------------------------------------------------------------------ 2. text
def text_layer_lines(doc):
    """Rebuild visual lines from the PDF's embedded text, grouping words by vertical position."""
    lines = []
    for page in doc:
        words = sorted(page.get_text("words"), key=lambda w: (round(w[1]), w[0]))
        current, cur_y = [], None
        for w in words:
            if cur_y is None or abs(w[1] - cur_y) <= 3:
                current.append(w)
                cur_y = w[1] if cur_y is None else cur_y
            else:
                lines.append([(x[4], 0.99) for x in sorted(current, key=lambda x: x[0])])
                current, cur_y = [w], w[1]
        if current:
            lines.append([(x[4], 0.99) for x in sorted(current, key=lambda x: x[0])])
    return lines


def ocr_lines(doc, dpi=300):
    """OCR every page with Tesseract; returns lines of (word, confidence 0-1)."""
    lines = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
        img = ImageOps.autocontrast(img).filter(ImageFilter.MedianFilter(3))
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--psm 6")
        grouped = {}
        for i, word in enumerate(data["text"]):
            if word.strip():
                key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                grouped.setdefault(key, []).append((word, max(float(data["conf"][i]), 0) / 100))
        lines.extend(grouped[k] for k in sorted(grouped))
    return lines


def get_lines(doc):
    """Use the embedded text layer if it exists; otherwise the document is a scan and needs OCR."""
    text_chars = sum(len(p.get_text().strip()) for p in doc)
    if text_chars >= 50 * doc.page_count:
        return text_layer_lines(doc), "text-layer", text_chars, None
    lines = ocr_lines(doc)
    confs = [c for line in lines for _, c in line]
    mean = round(sum(confs) / len(confs), 3) if confs else 0.0
    return lines, "ocr", sum(len(w) for l in lines for w, _ in l), mean


# ------------------------------------------------------------------ 3a. rule parser
MONEY = r"\d{1,3}(?:,\d{2,3})*\.\d{2}|\d+\.\d{2}"
MONEY_RE = re.compile(rf"^(?:{MONEY})$")
QTY_RE = re.compile(r"^\d+(?:\.\d+)?$")
CODE_RE = re.compile(r"^[A-Z]{2,5}-[A-Z0-9]{2,6}-\d{2}$")
GSTIN_FIND = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z])\b")
TOTALS_WORDS = re.compile(r"total|taxable|gst|payable|balance|amount due", re.I)


def to_amount(s):
    if s is None:
        return None
    s = s.replace(",", "").strip()
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def parse_date(s):
    if not s:
        return None
    s = s.strip().replace(" ", "-")
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _conf_for(value: str, line):
    """Confidence of a captured value = lowest OCR confidence among the words that make it up."""
    toks = [t for t in re.split(r"\s+", value) if t]
    confs = [c for w, c in line if any(t in w or w.strip(".,:|") in t and len(w.strip(".,:|")) > 1 for t in toks)]
    if not confs:
        confs = [c for _, c in line]
    return round(min(confs), 3) if confs else 0.0


LABELS = {
    "invoice_number": re.compile(r"(?:invoice\s*(?:no\.?|number|#)|bill\s*no\.?|inv\.?\s*no\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-_.]{2,})", re.I),
    "invoice_date": re.compile(r"(?:invoice\s*date|bill\s*date|dated|date\s*of\s*issue)\s*[:]?\s*(\d{1,2}[-/. ][A-Za-z]{3,9}[-/. ]\d{4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{4}-\d{2}-\d{2})", re.I),
    "po_number": re.compile(r"\b(P[O0][-/ ]?\d{4}[-/ ]?\d{4})\b"),
    "subtotal": re.compile(rf"(?:sub\s*-?\s*total|taxable\s*(?:value|amount))\s*[:]?\s*(?:INR|Rs\.?)?\s*({MONEY})", re.I),
    "cgst": re.compile(rf"CGST(?:\s*@\s*[\d.]+\s*%)?\s*[:]?\s*(?:INR|Rs\.?)?\s*({MONEY})", re.I),
    "sgst": re.compile(rf"SGST(?:\s*@\s*[\d.]+\s*%)?\s*[:]?\s*(?:INR|Rs\.?)?\s*({MONEY})", re.I),
    "igst": re.compile(rf"IGST(?:\s*@\s*[\d.]+\s*%)?\s*[:]?\s*(?:INR|Rs\.?)?\s*({MONEY})", re.I),
    "total": re.compile(rf"(?:grand\s*total|invoice\s*total|total\s*amount\s*(?:due|payable)?|amount\s*payable|total\s*due)\s*[:]?\s*(?:INR|Rs\.?)?\s*({MONEY})", re.I),
    "bank_account": re.compile(r"(?:A/?C|account)\s*(?:no\.?|number)?\s*[:]?\s*(\d{9,18})", re.I),
    "ifsc": re.compile(r"IFSC\s*[:]?\s*([A-Z]{4}[0O][A-Z0-9]{6})", re.I),
    "currency": re.compile(r"\b(INR|USD|EUR|GBP)\b"),
}


def rule_parse(lines):
    """Deterministic parser for common Indian GST invoice conventions. Returns {field: FieldValue} + line items."""
    texts = [" ".join(w for w, _ in l) for l in lines]
    fields = {}

    def put(name, value, conf, source="rules"):
        if value not in (None, "") and name not in fields:
            fields[name] = FieldValue(value=value, confidence=conf, source=source)

    # label: value pairs may sit on the same line or the value on the next line
    for i, t in enumerate(texts):
        joined = t + " " + (texts[i + 1] if i + 1 < len(texts) else "")
        for name, rx in LABELS.items():
            if name in fields:
                continue
            m = rx.search(t) or (rx.search(joined) if name in ("invoice_number", "invoice_date") else None)
            if not m:
                continue
            raw = m.group(1)
            src_line = lines[i] if rx.search(t) else lines[i] + (lines[i + 1] if i + 1 < len(lines) else [])
            conf = _conf_for(raw, src_line)
            if name == "invoice_date":
                val = parse_date(raw)
                put(name, val, conf if val else 0.0)
            elif name == "po_number":
                digits = re.sub(r"[^0-9]", "", raw.replace("O", "0")[2:])
                put(name, f"PO-{digits[:4]}-{digits[4:]}", conf)
            elif name in ("subtotal", "cgst", "sgst", "igst", "total"):
                put(name, to_amount(raw), conf)
            elif name == "ifsc":
                put(name, raw.upper()[:4] + raw.upper()[4:].replace("O", "0"), conf)
            else:
                put(name, raw.strip(), conf)

    # GSTINs: the one that is not the buyer's belongs to the vendor
    for i, t in enumerate(texts):
        for g in GSTIN_FIND.findall(t):
            if g != BUYER["gstin"] and "buyer" not in t.lower():
                put("vendor_gstin", g, _conf_for(g, lines[i]))

    # Vendor name: explicit "Supplier:/From:" label, else the first real line under the document title
    for i, t in enumerate(texts[:8]):
        m = re.match(r"^\s*(?:supplier|from|billed\s*by|vendor)\s*[:\-]\s*(.+)$", t, re.I)
        if m:
            put("vendor_name", m.group(1).strip(" |"), _conf_for(m.group(1), lines[i]))
            break
    if "vendor_name" not in fields:
        for i, t in enumerate(texts[:6]):
            clean = t.strip(" |.,'\"‘“")
            if re.search(r"invoice|original|duplicate|recipient", clean, re.I) or len(clean) < 4:
                continue
            if re.search(r"[A-Za-z]{3,}", clean):
                put("vendor_name", clean.strip(" |"), _conf_for(clean, lines[i]))
                break

    tax_parts = [fields[k].value for k in ("cgst", "sgst", "igst") if k in fields]
    if tax_parts:
        put("tax_total", round(sum(tax_parts), 2), min(fields[k].confidence for k in ("cgst", "sgst", "igst") if k in fields))

    # Line items: read numbers from the right (amount, unit price, qty), item code anywhere
    items = []
    for i, line in enumerate(lines):
        toks = [(w.strip("|"), c) for w, c in line if re.search(r"[A-Za-z0-9]", w)]
        t = " ".join(w for w, _ in toks)
        if TOTALS_WORDS.search(t) or len(toks) < 4:
            continue
        nums = [(j, w, c) for j, (w, c) in enumerate(toks) if MONEY_RE.match(w)]
        if len(nums) < 2:
            continue
        (ja, amount_s, ca), (jp, price_s, cp) = nums[-1], nums[-2]
        qj = jp - 1
        if qj < 0 or not QTY_RE.match(toks[qj][0]):
            continue
        code_idx = next((j for j, (w, _) in enumerate(toks[:qj]) if CODE_RE.match(w)), None)
        start = (code_idx + 1) if code_idx is not None else (1 if QTY_RE.match(toks[0][0]) else 0)
        desc = " ".join(w for w, _ in toks[start:qj]).strip(" .\"'”")
        if not desc:
            continue
        conf = round(min(ca, cp, toks[qj][1]), 3)
        items.append(LineItem(
            item_code=toks[code_idx][0] if code_idx is not None else None, description=desc,
            quantity=float(toks[qj][0]), unit_price=to_amount(price_s), amount=to_amount(amount_s), confidence=conf))
    return fields, items


# ------------------------------------------------------------------ 3b. Gemini (optional)
GEMINI_PROMPT = """You extract data from an Indian GST vendor invoice. Return ONLY JSON with these keys:
vendor_name, vendor_gstin, invoice_number, invoice_date (YYYY-MM-DD), po_number, currency,
subtotal, cgst, sgst, igst, total, bank_account, ifsc,
line_items: [{item_code, description, quantity, unit_price, amount}].
Rules: copy values exactly as printed. If a value is not clearly present or is unreadable, use null.
Never calculate or guess a missing value. Numbers must be plain numbers without commas.
The buyer is "%s" (GSTIN %s) - do NOT return the buyer as the vendor.

INVOICE TEXT:
%s"""


def gemini_extract(text: str, api_key: str):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": GEMINI_PROMPT % (BUYER["name"], BUYER["gstin"], text[:20000])}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    r = requests.post(url, params={"key": api_key}, json=body, timeout=GEMINI_TIMEOUT_S)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
    return json.loads(raw)


# ------------------------------------------------------------------ 3c. merge
SCALAR_FIELDS = ["vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "po_number", "currency",
                 "subtotal", "cgst", "sgst", "igst", "tax_total", "total", "bank_account", "ifsc"]


def _same(a, b):
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        try:
            return abs(float(a) - float(b)) <= 0.01
        except (TypeError, ValueError):
            return False
    na, nb = re.sub(r"[^A-Z0-9]", "", str(a).upper()), re.sub(r"[^A-Z0-9]", "", str(b).upper())
    return na == nb


def merge(rule_fields, rule_items, llm: dict | None):
    """Combine both extractors. Agreement -> high confidence; disagreement -> low (forces human review)."""
    out, notes = {}, []
    if llm:
        if llm.get("invoice_date"):
            llm["invoice_date"] = parse_date(str(llm["invoice_date"])) or llm["invoice_date"]
        tp = [llm.get(k) for k in ("cgst", "sgst", "igst") if isinstance(llm.get(k), (int, float))]
        if tp:
            llm["tax_total"] = round(sum(tp), 2)
    for f in SCALAR_FIELDS:
        r = rule_fields.get(f)
        lv = llm.get(f) if llm else None
        if r and llm is not None and lv is not None:
            if _same(r.value, lv):
                out[f] = FieldValue(value=r.value, confidence=max(r.confidence, 0.97), source="rules+ai agree")
            else:
                out[f] = FieldValue(value=lv, confidence=0.5, source="rules/ai disagree")
                notes.append(f"{f}: rules read '{r.value}', AI read '{lv}'")
        elif r:
            out[f] = r
        elif lv is not None:
            # AI-only value: useful but uncorroborated (LLMs return no calibrated confidence)
            out[f] = FieldValue(value=lv, confidence=0.8, source="ai only")
        else:
            out[f] = FieldValue(value=None, confidence=0.0, source="not found")

    items = rule_items
    if llm and llm.get("line_items"):
        llm_items = []
        for it in llm["line_items"]:
            try:
                llm_items.append(LineItem(item_code=it.get("item_code"), description=str(it.get("description") or ""),
                                          quantity=float(it["quantity"]), unit_price=float(it["unit_price"]),
                                          amount=float(it["amount"]), confidence=0.8))
            except (KeyError, TypeError, ValueError):
                continue
        sub = out["subtotal"].value

        def fits(lst):
            return sub is not None and lst and abs(sum(i.amount for i in lst) - sub) <= 1

        if not fits(rule_items) and fits(llm_items):
            items = llm_items
            notes.append("line items taken from AI (rule parser lines did not add up to subtotal)")
        elif not rule_items:
            items = llm_items
    return out, items, notes


def extract(lines, method, text_chars, ocr_conf, use_ai=False, api_key=None, log=None):
    rule_fields, rule_items = rule_parse(lines)
    llm, ai_status = None, "disabled"
    if use_ai and api_key:
        try:
            llm = gemini_extract("\n".join(" ".join(w for w, _ in l) for l in lines), api_key)
            ai_status = "ok"
        except Exception as e:  # network, quota (429), bad JSON -> fall back to rules, never block processing
            ai_status = f"failed, fell back to rules ({type(e).__name__}: {str(e)[:120]})"
    elif use_ai:
        ai_status = "no API key, using rules only"
    fields, items, notes = merge(rule_fields, rule_items, llm)
    inv = ExtractedInvoice(fields=fields, line_items=items, method=method, text_chars=text_chars,
                           ocr_mean_conf=ocr_conf, ai_status=ai_status, merge_notes=notes,
                           raw_text="\n".join(" ".join(w for w, _ in l) for l in lines))
    return inv


def render_preview(pdf_bytes: bytes, dpi=110):
    """PNG of page 1 for the UI."""
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        return doc[0].get_pixmap(dpi=dpi).tobytes("png")
    except Exception:
        return None
