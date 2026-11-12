"""Deterministic AP business rules. AI never makes the decision; these functions do, and every one explains itself.

Each rule returns Check objects: outcome (PASS/WARN/FAIL/INFO) + effect (NONE/REVIEW/REJECT/DUPLICATE)
+ a plain-English message + a recommended action.
"""
import re
from datetime import date

from rapidfuzz import fuzz

import db
import config
from config import (BUYER, CONFIDENCE_THRESHOLD, CRITICAL_FIELDS, DUPLICATE_WINDOW_DAYS,
                    LINE_DESC_MATCH, NEAR_DUP_NUMBER_SIM, TOLERANCES, VENDOR_NAME_MATCH)
from gstin import is_valid_gstin
from master_data import goods_receipts, purchase_orders, vendors
from models import Check, Decision, ExtractedInvoice

R = TOLERANCES["rounding_abs"]


def money(x):
    return "n/a" if x is None else f"INR {x:,.2f}"


def norm_invoice_number(s):
    """'INV-NOS-1042', 'INV/NOS/1042', 'inv nos 1042' -> 'NOS1042'."""
    if not s:
        return None
    s = re.sub(r"[^A-Z0-9]", "", str(s).upper())
    s = re.sub(r"^(INVOICE|INV|BILL|TAXINVOICE)", "", s)
    return s or None


# ------------------------------------------------------------- confidence assessment
def assess_confidence(inv: ExtractedInvoice):
    """Cross-checks the extraction against itself. If independently-read numbers add up
    (lines -> subtotal, subtotal + tax -> total) they corroborate each other, so confidence is raised.
    Returns the list of critical fields that are still uncertain."""
    notes = []
    items, sub, tax, total = inv.line_items, inv.v("subtotal"), inv.v("tax_total"), inv.v("total")
    lines_ok = bool(items) and sub is not None and abs(sum(i.amount for i in items) - sub) <= R
    totals_ok = None not in (sub, tax, total) and abs(sub + tax - total) <= R
    if lines_ok and totals_ok:
        for f in ("subtotal", "cgst", "sgst", "igst", "tax_total", "total"):
            fv = inv.fields.get(f)
            if fv and fv.value is not None and fv.confidence < 0.95:
                fv.confidence, fv.source = 0.95, fv.source + " +arithmetic verified"
        for i in items:
            if abs(i.quantity * i.unit_price - i.amount) <= R:
                i.confidence = max(i.confidence, 0.95)
        notes.append("Line items, subtotal, tax and total are arithmetically consistent: numbers corroborated.")
    elif items or sub is not None:
        notes.append("Amounts do not reconcile with each other, so numeric fields are not corroborated.")
    low = [f for f in CRITICAL_FIELDS + ["po_number"]
           if inv.v(f) is not None and inv.c(f) < CONFIDENCE_THRESHOLD]
    low_lines = [i.description for i in items if i.confidence < CONFIDENCE_THRESHOLD]
    return low, low_lines, notes


# ------------------------------------------------------------- mandatory fields
def validate_mandatory(inv: ExtractedInvoice, low, low_lines):
    S, out = "Validation", []
    labels = {"vendor_name": "Vendor name", "invoice_number": "Invoice number",
              "invoice_date": "Invoice date", "total": "Invoice total"}
    for f, label in labels.items():
        if inv.v(f) is None and not (f == "vendor_name" and inv.v("vendor_gstin")):
            out.append(Check(code=f"MISSING_{f.upper()}", stage=S, name=f"{label} present", outcome="FAIL",
                             effect="REVIEW", message=f"{label} could not be found or read on the invoice. No value was assumed.",
                             action=f"Read the {label.lower()} from the original document, or ask the vendor to resend a clear copy."))
        else:
            out.append(Check(code=f"HAS_{f.upper()}", stage=S, name=f"{label} present", outcome="PASS",
                             message=f"{label}: {inv.v(f) if inv.v(f) is not None else inv.v('vendor_gstin')}"))
    if not inv.line_items:
        out.append(Check(code="NO_LINES", stage=S, name="Line items present", outcome="FAIL", effect="REVIEW",
                         message="No line items could be read, so quantities and prices cannot be checked.",
                         action="Key the line items manually from the document."))
    else:
        out.append(Check(code="HAS_LINES", stage=S, name="Line items present", outcome="PASS",
                         message=f"{len(inv.line_items)} line item(s) read."))
    if inv.v("currency") is None:
        out.append(Check(code="NO_CURRENCY", stage=S, name="Currency stated", outcome="WARN", effect="NONE",
                         message="Currency not stated; INR assumed for comparison only (shown here for transparency)."))
    d = inv.v("invoice_date")
    if d and d > date.today().isoformat():
        out.append(Check(code="FUTURE_DATE", stage=S, name="Invoice date plausible", outcome="FAIL", effect="REVIEW",
                         message=f"Invoice is dated in the future ({d}).", action="Confirm the invoice date with the vendor."))
    if low:
        out.append(Check(code="LOW_CONFIDENCE", stage=S, name="Extraction confidence", outcome="FAIL", effect="REVIEW",
                         message="Uncertain reading of: " + ", ".join(f"{f} ({inv.v(f)}, {inv.c(f):.0%})" for f in low),
                         action="Verify the highlighted fields against the original document before approving."))
    if low_lines:
        out.append(Check(code="LOW_CONF_LINES", stage=S, name="Line-item confidence", outcome="FAIL", effect="REVIEW",
                         message=f"{len(low_lines)} line item(s) were read with low confidence.",
                         action="Verify line quantities and prices against the original document."))
    if not low and not low_lines:
        out.append(Check(code="CONF_OK", stage=S, name="Extraction confidence", outcome="PASS",
                         message=f"All critical fields at or above {CONFIDENCE_THRESHOLD:.0%} confidence."))
    return out


# ------------------------------------------------------------- vendor
def resolve_vendor(inv: ExtractedInvoice):
    V = vendors()
    g = inv.v("vendor_gstin")
    if g:
        for v in V.values():
            if v["gstin"] == g:
                return v, "GSTIN exact match", 100
    name = inv.v("vendor_name")
    best, score = None, 0
    if name:
        for v in V.values():
            for cand in [v["legal_name"]] + v["aliases"]:
                s = fuzz.token_sort_ratio(name.lower(), cand.lower())
                if s > score:
                    best, score = v, s
    if best and score >= VENDOR_NAME_MATCH:
        return best, f"name match ({score:.0f}%)", score
    return None, f"no match (best {score:.0f}%)", score


def verify_vendor(inv: ExtractedInvoice):
    S, out = "Vendor", []
    v, how, score = resolve_vendor(inv)
    if not v:
        out.append(Check(code="VENDOR_UNKNOWN", stage=S, name="Vendor in vendor master", outcome="FAIL", effect="REJECT",
                         message=f"'{inv.v('vendor_name')}' (GSTIN {inv.v('vendor_gstin') or 'n/a'}) is not in the vendor master.",
                         action="Do not pay. If this is a genuine new supplier, complete vendor onboarding first."))
        return out, None
    out.append(Check(code="VENDOR_FOUND", stage=S, name="Vendor in vendor master", outcome="PASS",
                     message=f"{v['legal_name']} ({v['vendor_id']}) identified by {how}."))
    if v["status"] != "ACTIVE":
        out.append(Check(code="VENDOR_INACTIVE", stage=S, name="Vendor active", outcome="FAIL", effect="REJECT",
                         message=f"{v['legal_name']} is marked {v['status']} in the vendor master.",
                         action="Do not pay. Procurement must reactivate the vendor before any invoice is accepted."))
    else:
        out.append(Check(code="VENDOR_ACTIVE", stage=S, name="Vendor active", outcome="PASS", message="Vendor is active and approved."))
    g = inv.v("vendor_gstin")
    if g:
        if not is_valid_gstin(g):
            out.append(Check(code="GSTIN_INVALID", stage=S, name="GSTIN valid", outcome="FAIL", effect="REVIEW",
                             message=f"GSTIN '{g}' fails the format/checksum test (could be a misprint or misread).",
                             action="Confirm the GSTIN; an invalid GSTIN blocks input tax credit."))
        elif g != v["gstin"]:
            out.append(Check(code="GSTIN_MISMATCH", stage=S, name="GSTIN matches master", outcome="FAIL", effect="REVIEW",
                             message=f"Invoice GSTIN {g} differs from master GSTIN {v['gstin']}.",
                             action="Confirm with the vendor which registration is correct."))
        else:
            out.append(Check(code="GSTIN_OK", stage=S, name="GSTIN matches master", outcome="PASS", message=f"GSTIN {g} valid and on file."))
    else:
        out.append(Check(code="GSTIN_SKIPPED", stage=S, name="GSTIN matches master", outcome="SKIP",
                         message="Skipped: no vendor GSTIN was read from the invoice."))
    acct = inv.v("bank_account")
    if not acct:
        out.append(Check(code="BANK_SKIPPED", stage=S, name="Bank details match master", outcome="SKIP",
                         message="Skipped: no bank account was printed on (or read from) the invoice."))
    if acct:
        if acct != v["bank_account"]:
            out.append(Check(code="BANK_CHANGED", stage=S, name="Bank details match master", outcome="FAIL", effect="REVIEW",
                             message=f"Bank account on invoice (…{acct[-4:]}) differs from the account on file (…{v['bank_account'][-4:]}). "
                                     "Changed bank details are a common invoice-fraud pattern.",
                             action="Hold payment. Verify the change by phone using the contact already on file, not the one on the invoice."))
        else:
            out.append(Check(code="BANK_OK", stage=S, name="Bank details match master", outcome="PASS", message="Bank account matches the vendor master."))
    return out, v


# ------------------------------------------------------------- duplicates
# Policy (consistent everywhere):
#   earlier record APPROVED / HUMAN_APPROVED / PENDING_REVIEW  -> it still counts -> DUPLICATE
#   earlier record REJECTED / HUMAN_REJECTED / DUPLICATE       -> does not count -> reprocessing allowed
#   exact same file or same vendor + normalised number            -> DUPLICATE (REVIEW if the number was misread)
#   same vendor + same amount + similar number or same date       -> REVIEW (fuzzy, never automatic)
def detect_duplicates(inv: ExtractedInvoice, vendor, file_hash):
    S, out = "Duplicate", []
    earlier = db.find_by_hash(file_hash) if file_hash else []
    active = [f for f in earlier if f["status"] in db.ACTIVE_STATES]
    if active:
        f = active[0]
        out.append(Check(code="DUP_FILE", stage=S, name="Same file already received", outcome="FAIL", effect="DUPLICATE",
                         amount_at_risk=inv.v("total") or 0,
                         message=f"This exact file was already processed as record #{f['id']} ({f['invoice_number'] or 'no number'}, "
                                 f"status {f['status']}, received {f['received_at']}).",
                         action="Do not pay twice. No further action needed."))
        return out
    if earlier:
        ids = ", ".join(f"#{f['id']} ({f['status']})" for f in earlier)
        out.append(Check(code="DUP_FILE_RESUBMIT", stage=S, name="Same file already received", outcome="INFO",
                         message=f"This file was seen before as {ids}. Those records are rejected/duplicates, so it is reprocessed "
                                 "as a fresh submission; the earlier records and their audit history are kept."))
    if not vendor:
        out.append(Check(code="DUP_SKIPPED", stage=S, name="Vendor-level duplicate check", outcome="SKIP",
                         message="Skipped: vendor not identified, so invoice history cannot be searched by vendor."))
        return out
    n = norm_invoice_number(inv.v("invoice_number"))
    prior = db.prior_invoices_for_vendor(vendor["vendor_id"])   # active states only
    total, d = inv.v("total"), inv.v("invoice_date")
    if n:
        for p in prior:
            if p["invoice_number_norm"] == n:
                certain = inv.c("invoice_number") >= CONFIDENCE_THRESHOLD
                out.append(Check(code="DUP_NUMBER", stage=S, name="Invoice number already processed", outcome="FAIL",
                                 effect="DUPLICATE" if certain else "REVIEW", amount_at_risk=total or 0,
                                 message=f"'{inv.v('invoice_number')}' is the same invoice as '{p['invoice_number']}' (record #{p['id']}, "
                                         f"status {p['status']}) once formatting is removed (both normalise to '{n}')."
                                         + ("" if certain else " The invoice number was read with low confidence, so a person must confirm."),
                                 action="Do not pay. Reply to the vendor that this invoice is already being processed."))
                return out
    else:
        out.append(Check(code="DUP_NUMBER_SKIPPED", stage=S, name="Invoice number already processed", outcome="SKIP",
                         message="Skipped: no invoice number was read, so only the amount/date near-match check could run."))
    for p in prior:
        if total is None or p["total"] is None or abs(p["total"] - total) > R:
            continue
        sim = fuzz.ratio(n or "", p["invoice_number_norm"] or "") if n else 0
        same_date = bool(d and p["invoice_date"] and d == p["invoice_date"])
        days = abs((date.fromisoformat(d) - date.fromisoformat(p["invoice_date"])).days) if d and p["invoice_date"] else None
        if (sim >= NEAR_DUP_NUMBER_SIM and (days is None or days <= DUPLICATE_WINDOW_DAYS)) or same_date:
            why = f"invoice-number similarity {sim:.0f}%" if sim >= NEAR_DUP_NUMBER_SIM else "same invoice date"
            out.append(Check(code="DUP_NEAR", stage=S, name="Possible duplicate", outcome="FAIL", effect="REVIEW",
                             amount_at_risk=total,
                             message=f"Possible duplicate: same vendor and same amount ({money(total)}) as record #{p['id']} "
                                     f"('{p['invoice_number']}', {p['status']}); {why}. Not treated as a confirmed duplicate.",
                             action="Compare both documents; pay only if they are genuinely different supplies."))
            return out
    out.append(Check(code="DUP_NONE", stage=S, name="Duplicate check", outcome="PASS",
                     message=f"No match among {len(prior)} approved/pending invoice(s) from this vendor "
                             "(exact file, normalised number and amount near-match checks)."))
    return out


# ------------------------------------------------------------- PO matching (2-way + 3-way qty)
def _match_line(item, po_lines):
    if item.item_code:
        for pl in po_lines:
            if pl["item_code"] == item.item_code:
                return pl, "item code"
    best, score = None, 0
    for pl in po_lines:
        s = fuzz.token_set_ratio(item.description.lower(), pl["description"].lower())
        if s > score:
            best, score = pl, s
    return (best, f"description {score:.0f}%") if score >= LINE_DESC_MATCH else (None, None)


def _infer_po(inv, vendor):
    """No PO on the invoice: find the vendor's open PO whose items best fit the invoice lines."""
    cands = []
    for po in purchase_orders().values():
        if po["vendor_id"] != vendor["vendor_id"] or po["status"] != "OPEN":
            continue
        hits = sum(1 for it in inv.line_items if _match_line(it, po["lines"])[0])
        if hits:
            cands.append((hits / max(len(inv.line_items), 1), po))
    cands.sort(key=lambda x: -x[0])
    if cands and cands[0][0] >= 0.5 and (len(cands) == 1 or cands[0][0] > cands[1][0]):
        return cands[0][1], cands[0][0]
    return None, 0


def check_line_quantities(code, qty, ordered, billed_before, received, unit_price, S="PO match"):
    """Cumulative quantity rules for one PO line (shared by processing and review re-validation).
    Returns (checks, issues). received=None means no goods receipt exists for the line."""
    out, issues = [], []
    after = billed_before + qty
    remaining_before = max(ordered - billed_before, 0)
    if after > ordered + 1e-9:
        over = after - ordered
        if billed_before >= ordered:
            issues.append("line already fully billed")
            out.append(Check(code="QTY_FULLY_BILLED", stage=S, name="Quantity within PO", outcome="FAIL", effect="REJECT",
                             amount_at_risk=round(qty * unit_price, 2),
                             message=f"{code}: all {ordered:g} units ordered were already billed on earlier approved invoices "
                                     f"(0 remaining); this invoice bills {qty:g} more.",
                             action="Reject. Ask the vendor to cancel this invoice or issue a credit note."))
        else:
            issues.append("quantity exceeds remaining")
            out.append(Check(code="QTY_OVER_PO", stage=S, name="Quantity within PO", outcome="FAIL", effect="REVIEW",
                             amount_at_risk=round(over * unit_price, 2),
                             message=f"{code}: {billed_before:g} already billed + {qty:g} on this invoice = {after:g}, but only {ordered:g} "
                                     f"were ordered. Only {remaining_before:g} remained, so {over:g} unit(s) are over the PO.",
                             action="Get a PO amendment from procurement, or ask the vendor to correct the quantity."))
    else:
        kind = "completes the line" if abs(after - ordered) < 1e-9 else "partial billing"
        out.append(Check(code="QTY_OK", stage=S, name="Quantity within PO", outcome="PASS",
                         message=f"{code}: bills {qty:g} of {remaining_before:g} remaining ({billed_before:g} of {ordered:g} "
                                 f"billed before) - {kind}; {ordered - after:g} will remain after approval."))
    # three-way: receipt
    if received is None:
        if config.GRN_REQUIRED:
            issues.append("no goods receipt")
            out.append(Check(code="GRN_MISSING", stage=S, name="Goods received (3-way match)", outcome="FAIL", effect="REVIEW",
                             amount_at_risk=round(qty * unit_price, 2),
                             message=f"{code}: no goods receipt (GRN) is recorded, so delivery cannot be confirmed. "
                                     "Three-way matching is mandatory under the current policy.",
                             action="Hold until the warehouse/requester records the goods receipt, then re-check."))
        else:
            out.append(Check(code="GRN_SKIPPED", stage=S, name="Goods received (3-way match)", outcome="SKIP",
                             message=f"{code}: no goods receipt recorded; GRN check skipped because it is optional under the "
                                     "current policy. This is a two-way match only."))
    elif after > received + 1e-9:
        issues.append("billed beyond receipt")
        out.append(Check(code="QTY_NOT_RECEIVED", stage=S, name="Goods received (3-way match)", outcome="FAIL", effect="REVIEW",
                         amount_at_risk=round((after - received) * unit_price, 2),
                         message=f"{code}: {after:g} units billed in total (including this invoice) but only {received:g} received "
                                 f"per goods receipt; {after - received:g} unit(s) not yet delivered.",
                         action="Hold until the warehouse confirms receipt of the remaining units."))
    else:
        out.append(Check(code="GRN_OK", stage=S, name="Goods received (3-way match)", outcome="PASS",
                         message=f"{code}: {received:g} received per GRN covers {after:g} billed in total."))
    return out, issues


def match_po(inv: ExtractedInvoice, vendor):
    S, out = "PO match", []
    comp = {"po": None, "lines": [], "billed_before": 0, "remaining_before": None}
    persist_lines = []
    if not vendor:
        out.append(Check(code="PO_SKIPPED", stage=S, name="PO match", outcome="SKIP",
                         message="Skipped: vendor not identified, so no purchase order can be matched."))
        return out, comp, persist_lines
    POS = purchase_orders()
    po_no = inv.v("po_number")
    po = POS.get(po_no) if po_no else None
    if po_no and not po:
        out.append(Check(code="PO_NOT_FOUND", stage=S, name="PO exists", outcome="FAIL", effect="REVIEW",
                         message=f"PO '{po_no}' does not exist in the procurement system.",
                         action="Ask the vendor for the correct PO number."))
        return out, comp, persist_lines
    if not po_no:
        po, share = _infer_po(inv, vendor)
        if not po:
            out.append(Check(code="PO_MISSING", stage=S, name="PO reference", outcome="FAIL", effect="REVIEW",
                             message="No PO number on the invoice and no open PO of this vendor fits the line items.",
                             action="Ask the requester which PO this relates to, or route as a non-PO invoice."))
            return out, comp, persist_lines
        out.append(Check(code="PO_INFERRED", stage=S, name="PO reference", outcome="WARN", effect="REVIEW",
                         message=f"No PO number on the invoice. Most likely {po['po_number']} ({share:.0%} of invoice lines match "
                                 "this vendor's only fitting open PO). Inferred, not confirmed.",
                         action=f"Confirm the invoice belongs to {po['po_number']}, then approve."))
    else:
        out.append(Check(code="PO_FOUND", stage=S, name="PO exists", outcome="PASS", message=f"{po_no} found ({po['status']})."))
    comp["po"] = {k: po[k] for k in ("po_number", "vendor_id", "po_date", "status", "currency", "value")}

    if po["vendor_id"] != vendor["vendor_id"]:
        out.append(Check(code="PO_WRONG_VENDOR", stage=S, name="PO belongs to vendor", outcome="FAIL", effect="REJECT",
                         message=f"{po['po_number']} was issued to {vendors()[po['vendor_id']]['legal_name']}, not to {vendor['legal_name']}.",
                         action="Reject and return to the vendor: they are billing against another supplier's PO."))
        return out, comp, persist_lines
    if po["status"] != "OPEN":
        out.append(Check(code="PO_CLOSED", stage=S, name="PO open", outcome="FAIL", effect="REJECT",
                         message=f"{po['po_number']} is {po['status']}; no further invoices can be booked against it.",
                         action="Reject. If the supply is genuine, procurement must raise a new PO."))
    cur = inv.v("currency") or "INR"
    if cur != po["currency"]:
        out.append(Check(code="CURRENCY_MISMATCH", stage=S, name="Currency matches PO", outcome="FAIL", effect="REVIEW",
                         message=f"Invoice currency {cur} differs from PO currency {po['currency']}.",
                         action="Confirm the exchange rate and currency with procurement."))

    billed, billed_value, n_prior = db.billed_against_po(po["po_number"])
    grn = goods_receipts().get(po["po_number"], {})
    remaining_value = round(po["value"] - billed_value, 2)
    comp.update(billed_before=billed_value, remaining_before=remaining_value, prior_invoices=n_prior,
                pending_reviews=db.pending_against_po(po["po_number"]), grn_policy="mandatory" if config.GRN_REQUIRED else "optional")
    tol = TOLERANCES["unit_price_pct"]
    line_problem = False
    for it in inv.line_items:
        pl, how = _match_line(it, po["lines"])
        row = {"invoice_item": it.item_code or "-", "description": it.description, "qty_invoiced": it.quantity,
               "price_invoiced": it.unit_price, "amount_invoiced": it.amount, "matched_by": how or "no match"}
        if not pl:
            line_problem = True
            out.append(Check(code="LINE_NOT_ON_PO", stage=S, name="Line on PO", outcome="FAIL", effect="REVIEW", amount_at_risk=it.amount,
                             message=f"'{it.description}' ({money(it.amount)}) is not on {po['po_number']}.",
                             action="Ask the requester to confirm this extra item, or request a credit note."))
            row["result"] = "Not on PO"
            comp["lines"].append(row)
            continue
        code = pl["item_code"]
        prev = billed.get(code, 0)
        ordered = pl["quantity"]
        received = grn.get(code)
        variance = (it.unit_price - pl["unit_price"]) / pl["unit_price"] * 100
        row.update(po_item=code, qty_ordered=ordered, qty_billed_before=prev, qty_received=received,
                   qty_remaining_after=max(ordered - prev - it.quantity, 0), price_po=pl["unit_price"],
                   price_variance_pct=round(variance, 2))
        issues = []
        if variance > tol:
            excess = round((it.unit_price - pl["unit_price"]) * it.quantity, 2)
            issues.append("price above tolerance")
            out.append(Check(code="PRICE_VARIANCE", stage=S, name="Unit price within tolerance", outcome="FAIL", effect="REVIEW",
                             amount_at_risk=excess,
                             message=f"{code}: billed {money(it.unit_price)} per unit vs PO {money(pl['unit_price'])} "
                                     f"(+{variance:.1f}%; allowed up to +{tol:g}%). Overcharge {money(excess)} before tax "
                                     f"on {it.quantity:g} unit(s).",
                             action="Ask the vendor for a credit note for the difference, or get buyer approval for the new price."))
        elif variance > 0:
            out.append(Check(code="PRICE_MINOR", stage=S, name="Unit price within tolerance", outcome="PASS",
                             message=f"{code}: +{variance:.2f}% vs PO price, within the +{tol:g}% tolerance."))
        elif variance < 0:
            out.append(Check(code="PRICE_LOWER", stage=S, name="Unit price within tolerance", outcome="PASS",
                             message=f"{code}: billed {abs(variance):.1f}% below the PO price (favourable)."))
        else:
            out.append(Check(code="PRICE_OK", stage=S, name="Unit price within tolerance", outcome="PASS",
                             message=f"{code}: unit price {money(it.unit_price)} equals the PO price."))
        qchecks, qissues = check_line_quantities(code, it.quantity, ordered, prev, received, it.unit_price, S)
        out.extend(qchecks)
        issues += qissues
        line_problem |= bool(issues)
        row["result"] = ", ".join(issues) if issues else "OK"
        comp["lines"].append(row)
        persist_lines.append({"po_number": po["po_number"], "item_code": code, "quantity": it.quantity,
                              "unit_price": it.unit_price, "amount": it.amount})
    sub = inv.v("subtotal") or sum(i.amount for i in inv.line_items)
    if remaining_value <= R and sub > 0:
        out.append(Check(code="PO_EXHAUSTED", stage=S, name="PO balance available", outcome="FAIL", effect="REJECT", amount_at_risk=sub,
                         message=f"{po['po_number']} is fully billed ({money(billed_value)} of {money(po['value'])}); "
                                 f"this invoice would add {money(sub)}.",
                         action="Reject. Nothing remains on this PO."))
    elif sub > remaining_value * (1 + TOLERANCES["po_value_pct"] / 100) + R:
        out.append(Check(code="PO_OVERRUN", stage=S, name="PO balance available", outcome="FAIL", effect="REVIEW",
                         amount_at_risk=round(sub - remaining_value, 2),
                         message=f"Invoice subtotal {money(sub)} exceeds the remaining PO balance {money(remaining_value)} "
                                 f"by {money(sub - remaining_value)}.",
                         action="Request a PO amendment before approving."))
    else:
        partial = sub < remaining_value - R
        out.append(Check(code="PO_BALANCE_OK", stage=S, name="PO balance available", outcome="PASS",
                         message=f"Subtotal {money(sub)} fits the remaining balance {money(remaining_value)} "
                                 f"({n_prior} earlier approved invoice(s) on this PO). "
                                 + (f"Partial billing: {money(remaining_value - sub)} will remain open on the PO."
                                    if partial else "This invoice completes the PO value.")))
    if not line_problem and inv.line_items:
        out.append(Check(code="LINES_MATCH", stage=S, name="Line items match PO", outcome="PASS",
                         message=f"All {len(inv.line_items)} invoice line(s) match {po['po_number']} on item, price and quantity"
                                 + (" (PO lines not billed here stay open)." if len(inv.line_items) < len(po["lines"]) else ".")))
    return out, comp, persist_lines


# ------------------------------------------------------------- GST
def validate_gst(sub, cgst, sgst, igst, intra, rate=None, S="Financial"):
    """GST regime + amount checks. Pure function (no I/O) so every case is unit-testable.
    intra=True  -> supplier and buyer in the same state  -> CGST + SGST, each rate/2, equal, no IGST.
    intra=False -> different states                      -> IGST at the full rate, no CGST/SGST.
    intra=None  -> vendor unknown, regime cannot be determined -> regime check SKIPPED; total rate still checked.
    rate defaults to config.GST_RATE (a DEMO assumption: real rates depend on each item's HSN/SAC code)."""
    rate = config.GST_RATE if rate is None else rate
    out = []
    present = lambda x: x is not None and abs(x) > 0.005  # noqa: E731
    act = "Ask the vendor for a corrected invoice; wrong GST blocks input tax credit."
    if sub is None:
        out.append(Check(code="GST_SKIPPED", stage=S, name="GST", outcome="SKIP",
                         message="Skipped: no subtotal, so GST cannot be recomputed."))
        return out
    if not (present(cgst) or present(sgst) or present(igst)):
        out.append(Check(code="NO_TAX", stage=S, name="GST components present", outcome="FAIL", effect="REVIEW",
                         message=f"No CGST, SGST or IGST found; {rate:.0%} GST ({money(sub * rate)}) was expected under the demo rate.",
                         action="Confirm whether the supply is taxable and the applicable rate."))
        return out
    half = round(sub * rate / 2, 2)
    full = round(sub * rate, 2)
    if intra is None:
        out.append(Check(code="GST_REGIME_SKIPPED", stage=S, name="GST type correct", outcome="SKIP",
                         message="Skipped: vendor not identified, so intra- vs inter-state supply cannot be determined."))
    elif intra:
        regime_ok = True
        if present(igst):
            regime_ok = False
            out.append(Check(code="TAX_REGIME", stage=S, name="GST type correct", outcome="FAIL", effect="REVIEW",
                             amount_at_risk=igst,
                             message=f"Supplier and buyer are in the same state, so CGST + SGST is required, but IGST "
                                     f"({money(igst)}) was charged.", action=act))
        for label, val in (("CGST", cgst), ("SGST", sgst)):
            if not present(val):
                regime_ok = False
                out.append(Check(code=f"MISSING_{label}", stage=S, name=f"{label} present", outcome="FAIL", effect="REVIEW",
                                 message=f"Intra-state supply requires {label} at {rate / 2:.1%} ({money(half)}), but no {label} was found.",
                                 action=act))
            elif abs(val - half) > R:
                regime_ok = False
                out.append(Check(code=f"{label}_AMOUNT", stage=S, name=f"{label} amount correct", outcome="FAIL", effect="REVIEW",
                                 amount_at_risk=round(abs(val - half), 2),
                                 message=f"{label} charged {money(val)} but {rate / 2:.1%} of {money(sub)} is {money(half)} "
                                         f"(difference {money(val - half)}).", action=act))
        if present(cgst) and present(sgst) and abs(cgst - sgst) > R:
            regime_ok = False
            out.append(Check(code="CGST_SGST_UNEQUAL", stage=S, name="CGST equals SGST", outcome="FAIL", effect="REVIEW",
                             amount_at_risk=round(abs(cgst - sgst), 2),
                             message=f"CGST ({money(cgst)}) and SGST ({money(sgst)}) must be equal halves of GST; "
                                     f"they differ by {money(abs(cgst - sgst))}.", action=act))
        if regime_ok:
            out.append(Check(code="TAX_REGIME_OK", stage=S, name="GST type correct", outcome="PASS",
                             message=f"Intra-state supply: CGST {money(cgst)} + SGST {money(sgst)}, each {rate / 2:.1%} of subtotal."))
    else:
        regime_ok = True
        if present(cgst) or present(sgst):
            regime_ok = False
            out.append(Check(code="TAX_REGIME", stage=S, name="GST type correct", outcome="FAIL", effect="REVIEW",
                             amount_at_risk=round((cgst or 0) + (sgst or 0), 2),
                             message="Inter-state supply, so IGST is required, but CGST/SGST "
                                     f"({money((cgst or 0) + (sgst or 0))}) was charged.", action=act))
        if not present(igst):
            regime_ok = False
            out.append(Check(code="MISSING_IGST", stage=S, name="IGST present", outcome="FAIL", effect="REVIEW",
                             message=f"Inter-state supply requires IGST at {rate:.0%} ({money(full)}), but no IGST was found.",
                             action=act))
        elif abs(igst - full) > R:
            regime_ok = False
            out.append(Check(code="IGST_AMOUNT", stage=S, name="IGST amount correct", outcome="FAIL", effect="REVIEW",
                             amount_at_risk=round(abs(igst - full), 2),
                             message=f"IGST charged {money(igst)} but {rate:.0%} of {money(sub)} is {money(full)} "
                                     f"(difference {money(igst - full)}).", action=act))
        if regime_ok:
            out.append(Check(code="TAX_REGIME_OK", stage=S, name="GST type correct", outcome="PASS",
                             message=f"Inter-state supply: IGST {money(igst)} = {rate:.0%} of subtotal."))
    tax = round(sum(x for x in (cgst, sgst, igst) if x), 2)
    if abs(tax - full) > R:
        out.append(Check(code="TAX_MISMATCH", stage=S, name="Total GST at configured rate", outcome="FAIL", effect="REVIEW",
                         amount_at_risk=round(abs(tax - full), 2),
                         message=f"Total GST charged {money(tax)} but {rate:.0%} of {money(sub)} is {money(full)} "
                                 f"(configured demo rate).", action="Confirm the GST rate / HSN code with the vendor."))
    else:
        out.append(Check(code="TAX_OK", stage=S, name="Total GST at configured rate", outcome="PASS",
                         message=f"Total GST {money(tax)} = {rate:.0%} of subtotal (configured demo rate)."))
    return out


# ------------------------------------------------------------- financial validation
def validate_financials(inv: ExtractedInvoice, vendor):
    S, out = "Financial", []
    items, sub, total = inv.line_items, inv.v("subtotal"), inv.v("total")
    cg, sg, ig, tax = inv.v("cgst"), inv.v("sgst"), inv.v("igst"), inv.v("tax_total")
    bad = [i for i in items if abs(i.quantity * i.unit_price - i.amount) > R]
    for i in bad:
        out.append(Check(code="LINE_MATH", stage=S, name="Line amount = qty x price", outcome="FAIL", effect="REVIEW",
                         amount_at_risk=round(abs(i.amount - i.quantity * i.unit_price), 2),
                         message=f"'{i.description}': {i.quantity:g} x {money(i.unit_price)} = {money(i.quantity * i.unit_price)}, "
                                 f"but invoice shows {money(i.amount)}.", action="Ask the vendor for a corrected invoice."))
    if items and not bad:
        out.append(Check(code="LINE_MATH_OK", stage=S, name="Line amount = qty x price", outcome="PASS", message="Every line multiplies out correctly."))
    if sub is None:
        out.append(Check(code="NO_SUBTOTAL", stage=S, name="Subtotal", outcome="FAIL", effect="REVIEW",
                         message="Subtotal not found.", action="Read the subtotal from the document."))
    elif items:
        s = round(sum(i.amount for i in items), 2)
        if abs(s - sub) > R:
            out.append(Check(code="SUBTOTAL_MISMATCH", stage=S, name="Lines add up to subtotal", outcome="FAIL", effect="REVIEW",
                             amount_at_risk=round(abs(sub - s), 2),
                             message=f"Line items add up to {money(s)} but the subtotal says {money(sub)}.",
                             action="Ask the vendor for a corrected invoice."))
        else:
            out.append(Check(code="SUBTOTAL_OK", stage=S, name="Lines add up to subtotal", outcome="PASS", message=f"Lines sum to {money(s)}."))
    intra = (vendor["state_code"] == BUYER["state_code"]) if vendor else None
    out.extend(validate_gst(sub, cg, sg, ig, intra))
    if None not in (sub, tax, total):
        if abs(sub + tax - total) > R:
            out.append(Check(code="TOTAL_MISMATCH", stage=S, name="Total = subtotal + tax", outcome="FAIL", effect="REVIEW",
                             amount_at_risk=round(abs(total - sub - tax), 2),
                             message=f"{money(sub)} + {money(tax)} = {money(sub + tax)}, but the invoice total is {money(total)}.",
                             action="Ask the vendor for a corrected invoice."))
        else:
            out.append(Check(code="TOTAL_OK", stage=S, name="Total = subtotal + tax", outcome="PASS", message=f"Total {money(total)} reconciles."))
    return out


# ------------------------------------------------------------- decision engine
NEXT_ACTION = {
    "APPROVE": "Release to the payment run on the due date.",
    "DUPLICATE": "Do not pay. Inform the vendor that the original invoice is already being processed.",
}
RULE_TEXT = {
    "DUPLICATE": "Rule 1: any confirmed duplicate -> DUPLICATE (checked first, so a duplicate is never paid).",
    "REJECT": "Rule 2: any confirmed business-rule violation (vendor, PO, quantity) -> REJECT.",
    "REJECT_SOFTENED": "Rule 2a (safety valve): a rejection that relies on low-confidence extracted data -> REVIEW instead.",
    "REVIEW": "Rule 3: any discrepancy, missing data, inference or system error -> REVIEW by a person.",
    "APPROVE": "Rule 4: no blocking or review-level failures -> APPROVE.",
}


def build_trace(checks: list[Check], decision: str, rule_key: str, next_action: str, decisive: list[Check]):
    """Deterministic decision trace built only from the executed checks (no LLM)."""
    def names(outcome):
        return [f"{c.stage}: {c.name} - {c.message}" for c in checks if c.outcome == outcome]
    return {
        "checks_performed": sum(1 for c in checks if c.outcome != "SKIP"),
        "passed": names("PASS"), "failed": names("FAIL"), "warnings": names("WARN"),
        "skipped": names("SKIP"), "info": names("INFO"),
        "decisive_checks": [f"{c.code} ({c.effect}): {c.message}" for c in decisive],
        "rule_applied": RULE_TEXT[rule_key], "decision": decision, "next_action": next_action,
    }


def decide(checks: list[Check], low_conf_fields: list[str], inv: ExtractedInvoice) -> Decision:
    failing = [c for c in checks if c.outcome in ("FAIL", "WARN") and c.effect != "NONE"]
    effects = {c.effect for c in failing}
    at_risk = round(sum(c.amount_at_risk for c in failing), 2)
    reasons = [c.message for c in failing]
    actions = list(dict.fromkeys(c.action for c in failing if c.action))
    total = inv.v("total") if inv else None

    def make(decision, rule_key, headline, next_action, decisive, value):
        return Decision(decision=decision, headline=headline, reasons=reasons, next_action=next_action,
                        exception_value=value, trace=build_trace(checks, decision, rule_key, next_action, decisive))

    if "DUPLICATE" in effects:
        dup = [c for c in failing if c.effect == "DUPLICATE"]
        return make("DUPLICATE", "DUPLICATE", "Duplicate invoice: " + dup[0].message, NEXT_ACTION["DUPLICATE"], dup, total or at_risk)
    if "REJECT" in effects:
        rej = [c for c in failing if c.effect == "REJECT"]
        if low_conf_fields:
            nxt = "Verify the extracted fields first. " + " ".join(actions[:2])
            return make("REVIEW", "REJECT_SOFTENED",
                        "Would be rejected, but key fields were read with low confidence, so a person must confirm.",
                        nxt, rej, total or at_risk)
        return make("REJECT", "REJECT", "Rejected: " + rej[0].message,
                    " ".join(dict.fromkeys(c.action for c in rej if c.action)), rej, total or at_risk)
    if "REVIEW" in effects:
        n = len(failing)
        return make("REVIEW", "REVIEW", f"Needs review: {n} issue{'s' if n > 1 else ''} found. " + failing[0].message,
                    " ".join(actions[:3]), failing, total or at_risk)
    passed = sum(1 for c in checks if c.outcome == "PASS")
    skipped = sum(1 for c in checks if c.outcome == "SKIP")
    note = f" ({skipped} check(s) skipped; see trace)" if skipped else ""
    return make("APPROVE", "APPROVE", f"Approved: all {passed} checks passed{note}. Invoice matches its PO within tolerance.",
                NEXT_ACTION["APPROVE"], [], 0)


# ------------------------------------------------------------- review re-validation
def revalidate_for_approval(rec: dict) -> dict:
    """Run again, against TODAY's data, the checks that can change while an invoice waits in review:
    PO status and balance, cumulative quantities vs ordered and received, and duplicates approved meanwhile.
    Returns {"conflicts": [...], "blocking": [...], "checked": [...], "snapshot": {...}}."""
    conflicts, blocking, checked = [], [], []
    lines = db.invoice_lines(rec["id"])
    po_no = rec.get("po_number")
    dups = db.approved_same_number(rec.get("vendor_id"), rec.get("invoice_number_norm"), rec["id"])
    checked.append("Duplicate approved meanwhile")
    for d in dups:
        blocking.append(f"Invoice {d['invoice_number']} (record #{d['id']}) with the same number was approved meanwhile; "
                        "approving this one would pay twice.")
    po = purchase_orders().get(po_no) if po_no else None
    snapshot = {"po_number": po_no}
    if not po or not lines:
        conflicts.append("No PO lines are linked to this invoice, so PO balance and quantities cannot be re-validated "
                         "(non-PO or unmatched invoice).")
        return {"conflicts": conflicts, "blocking": blocking, "checked": checked, "snapshot": snapshot}
    checked += ["PO status", "Cumulative quantity vs ordered", "Cumulative quantity vs received (GRN)", "PO value balance"]
    if po["status"] != "OPEN":
        blocking.append(f"{po_no} is now {po['status']}.")
    billed, billed_value, n = db.billed_against_po(po_no, exclude_id=rec["id"])
    grn = goods_receipts().get(po_no, {})
    ordered = {l["item_code"]: l["quantity"] for l in po["lines"]}
    price = {l["item_code"]: l["unit_price"] for l in po["lines"]}
    snapshot.update(billed_value_now=billed_value, approved_invoices_now=n, po_value=po["value"],
                    lines=[{"item_code": l["item_code"], "qty": l["quantity"], "billed_now": billed.get(l["item_code"], 0),
                            "ordered": ordered.get(l["item_code"]), "received": grn.get(l["item_code"])} for l in lines])
    for l in lines:
        code = l["item_code"]
        qchecks, _ = check_line_quantities(code, l["quantity"], ordered.get(code, 0), billed.get(code, 0), grn.get(code),
                                           price.get(code, l["unit_price"]), S="Re-validation")
        for c in qchecks:
            if c.outcome == "FAIL":
                conflicts.append(c.message)
    sub = rec.get("subtotal") or sum(l["amount"] for l in lines)
    remaining = round(po["value"] - billed_value, 2)
    if sub > remaining * (1 + TOLERANCES["po_value_pct"] / 100) + R:
        conflicts.append(f"PO balance now {money(remaining)} (after {n} approved invoice(s)); this invoice's subtotal "
                         f"{money(sub)} would overbill {po_no} by {money(sub - remaining)}.")
    snapshot["remaining_value_now"] = remaining
    return {"conflicts": conflicts, "blocking": blocking, "checked": checked, "snapshot": snapshot}
