"""Orchestrates the stages. Each stage is real code, timed, and reported to the UI through a callback as it runs.
Any unexpected error stops automation and routes the invoice to a human: an error never results in APPROVE."""
import time
import traceback

import db
import extraction as ex
import rules
from models import Check, Decision, StageResult

STAGES = [
    ("intake", "Intake & file check"),
    ("text", "Text extraction / OCR"),
    ("extract", "Structured field extraction"),
    ("confidence", "Confidence assessment"),
    ("validate", "Mandatory-field validation"),
    ("vendor", "Vendor verification"),
    ("duplicate", "Duplicate detection"),
    ("po", "PO matching (2-way + receipt check)"),
    ("financial", "Financial validation"),
    ("decision", "Decision & routing"),
    ("persist", "Save record & audit trail"),
]
NAMES = dict(STAGES)


def _status_from(checks):
    if any(c.outcome == "FAIL" and c.effect in ("REJECT", "DUPLICATE") for c in checks):
        return "fail"
    if any(c.outcome in ("FAIL", "WARN") and c.effect != "NONE" for c in checks):
        return "warn"
    return "ok"


def run(pdf_bytes: bytes, filename: str, use_ai=False, api_key=None, on_event=None):
    emit = on_event or (lambda *a: None)
    stages, checks, ctx = [], [], {"filename": filename}

    def stage(key, fn):
        emit("start", key, NAMES[key])
        t0 = time.perf_counter()
        try:
            status, summary, details = fn()
        except Exception as e:
            status, summary, details = "error", f"{type(e).__name__}: {e}", {"trace": traceback.format_exc()[-1500:]}
        sr = StageResult(key=key, name=NAMES[key], status=status, summary=summary, details=details,
                         duration_ms=round((time.perf_counter() - t0) * 1000, 1))
        stages.append(sr)
        emit("end", key, sr)
        return sr

    # 1. intake
    def s_intake():
        try:
            doc, info = ex.ingest(pdf_bytes)
        except ex.IngestError as e:
            return "fail", str(e), {}
        ctx.update(doc=doc, info=info)
        return "ok", f"{info['pages']} page(s), {info['size_kb']} KB, fingerprint {info['sha256'][:12]}…", info

    sr = stage("intake", s_intake)
    if sr.status in ("fail", "error"):
        unreadable = Check(code="UNREADABLE", stage="Intake", name="Readable PDF", outcome="FAIL", effect="REJECT",
                           message=sr.summary, action="Return to the vendor and request a valid PDF invoice.")
        checks.append(unreadable)
        nxt = unreadable.action
        dec = Decision(decision="REJECT", headline="Rejected: the file is not a readable PDF. " + sr.summary,
                       reasons=[sr.summary], next_action=nxt,
                       trace=rules.build_trace(checks, "REJECT", "REJECT", nxt, [unreadable]))
        for k, n in STAGES[1:-2]:
            skipped = StageResult(key=k, name=n, status="skipped", summary="Skipped: no readable document.")
            stages.append(skipped)
            emit("end", k, skipped)
        ctx["errors"] = [{"stage": sr.name, "error": sr.summary, "trace": sr.details.get("trace")}] if sr.status == "error" else []
        return _finish(stages, checks, dec, None, {}, [], ctx, pdf_bytes, emit, stage)

    # 2. text / OCR
    def s_text():
        lines, method, chars, ocr_conf = ex.get_lines(ctx["doc"])
        ctx.update(lines=lines, method=method, chars=chars, ocr_conf=ocr_conf)
        if method == "ocr":
            st = "warn" if (ocr_conf or 0) < 0.85 else "ok"
            return st, f"No text layer (scanned). OCR read {len(lines)} lines, mean word confidence {ocr_conf:.0%}.", \
                   {"method": method, "lines": len(lines), "mean_ocr_confidence": ocr_conf}
        return "ok", f"Digital PDF: read {chars} characters from the text layer ({len(lines)} lines).", {"method": method, "lines": len(lines)}

    # 3. structured extraction
    def s_extract():
        inv = ex.extract(ctx["lines"], ctx["method"], ctx["chars"], ctx["ocr_conf"], use_ai=use_ai, api_key=api_key)
        ctx["inv"] = inv
        found = sum(1 for f in inv.fields.values() if f.value is not None)
        engine = "rules + Gemini" if inv.ai_status == "ok" else "rules"
        st = "warn" if inv.ai_status.startswith("failed") or inv.merge_notes else "ok"
        return st, f"{found} fields and {len(inv.line_items)} line items extracted ({engine}). AI: {inv.ai_status}.", \
               {"fields": {k: v.model_dump() for k, v in inv.fields.items()}, "notes": inv.merge_notes}

    # 4. confidence
    def s_conf():
        low, low_lines, notes = rules.assess_confidence(ctx["inv"])
        ctx.update(low=low, low_lines=low_lines)
        missing = [f for f in ("vendor_name", "invoice_number", "invoice_date", "po_number", "total") if ctx["inv"].v(f) is None]
        if missing:
            notes.insert(0, "Not found on document (left empty, not guessed): " + ", ".join(missing) + ".")
        if missing and not (low or low_lines):
            return "warn", " ".join(notes), {"missing": missing, "notes": notes}
        if low or low_lines:
            return "warn", f"Uncertain: {', '.join(low) or ''}{' + ' if low and low_lines else ''}{f'{len(low_lines)} line(s)' if low_lines else ''}. " + " ".join(notes), \
                   {"low_confidence_fields": low, "notes": notes}
        return "ok", "All critical fields confident. " + " ".join(notes), {"notes": notes}

    def checked(fn):
        def wrapper():
            cs = fn()
            checks.extend(cs)
            fails = [c for c in cs if c.outcome in ("FAIL", "WARN") and c.effect != "NONE"]
            summ = f"{sum(c.outcome == 'PASS' for c in cs)} passed" + (f", {len(fails)} issue(s): " + fails[0].message if fails else "")
            return _status_from(cs), summ, {"checks": [c.model_dump() for c in cs]}
        return wrapper

    def c_vendor():
        cs, v = rules.verify_vendor(ctx["inv"])
        ctx["vendor"] = v
        return cs

    def c_po():
        cs, comp, plines = rules.match_po(ctx["inv"], ctx.get("vendor"))
        ctx.update(po_comparison=comp, persist_lines=plines)
        return cs

    critical = [("text", s_text), ("extract", s_extract), ("confidence", s_conf),
                ("validate", checked(lambda: rules.validate_mandatory(ctx["inv"], ctx["low"], ctx["low_lines"]))),
                ("vendor", checked(c_vendor)),
                ("duplicate", checked(lambda: rules.detect_duplicates(ctx["inv"], ctx.get("vendor"), ctx["info"]["sha256"]))),
                ("po", checked(c_po)),
                ("financial", checked(lambda: rules.validate_financials(ctx["inv"], ctx.get("vendor"))))]
    failed_stage = None
    for i, (key, fn) in enumerate(critical):
        sr = stage(key, fn)
        if sr.status == "error":
            # Every stage up to the decision is critical: a crash means the remaining checks never ran,
            # so automation stops here and a person takes over. An invoice is never approved after an error.
            failed_stage = sr
            checks.append(Check(code="SYSTEM_ERROR", stage=sr.name, name="System error", outcome="FAIL", effect="REVIEW",
                                message=f"'{sr.name}' failed unexpectedly ({sr.summary}). Automation stopped; later checks did not run.",
                                action="Process this invoice manually and report the error to the system owner."))
            for k, _ in critical[i + 1:]:
                skipped = StageResult(key=k, name=NAMES[k], status="skipped", summary=f"Skipped: '{sr.name}' failed.")
                stages.append(skipped)
                emit("end", k, skipped)
            break

    inv = ctx.get("inv")
    dec = None
    if failed_stage is not None:
        err = [c for c in checks if c.code == "SYSTEM_ERROR"]
        nxt = "Process this invoice manually; the automated checks are incomplete. Report the error to the system owner."
        dec = Decision(decision="REVIEW", headline=f"Needs review: automation stopped because '{failed_stage.name}' failed. "
                                                   "No automatic approval is possible.",
                       reasons=[c.message for c in checks if c.outcome in ("FAIL", "WARN") and c.effect != "NONE"],
                       next_action=nxt, exception_value=(inv.v("total") if inv else None) or 0,
                       trace=rules.build_trace(checks, "REVIEW", "REVIEW", nxt, err))
    ctx["errors"] = [{"stage": s.name, "error": s.summary, "trace": s.details.get("trace")} for s in stages if s.status == "error"]
    return _finish(stages, checks, dec, inv, ctx.get("po_comparison", {}), ctx.get("persist_lines", []), ctx, pdf_bytes, emit, stage)


def _finish(stages, checks, dec, inv, po_comp, plines, ctx, pdf_bytes, emit, stage):
    result = {"saved": False, "id": None, "persist_error": None}

    def s_decide():
        nonlocal dec
        if dec is None:
            dec = rules.decide(checks, ctx.get("low", []), inv)
        st = {"APPROVE": "ok", "REVIEW": "warn"}.get(dec.decision, "fail")
        return st, f"{dec.decision}: {dec.headline}", dec.model_dump()

    stage("decision", s_decide)
    if dec is None:  # the decision stage itself crashed: fail safe
        nxt = "Process this invoice manually."
        dec = Decision(decision="REVIEW", headline="Needs review: the decision engine failed, so no automatic decision was made.",
                       next_action=nxt, trace=rules.build_trace(checks, "REVIEW", "REVIEW", nxt, []))
    vendor = ctx.get("vendor")
    rec = {
        "filename": ctx.get("filename"), "file_hash": ctx.get("info", {}).get("sha256"),
        "vendor_id": vendor["vendor_id"] if vendor else None,
        "vendor_name": vendor["legal_name"] if vendor else (inv.v("vendor_name") if inv else None),
        "invoice_number": inv.v("invoice_number") if inv else None,
        "invoice_number_norm": rules.norm_invoice_number(inv.v("invoice_number")) if inv else None,
        "invoice_date": inv.v("invoice_date") if inv else None,
        "po_number": (po_comp.get("po") or {}).get("po_number") or (inv.v("po_number") if inv else None),
        "currency": (inv.v("currency") or "INR") if inv else None,
        "subtotal": inv.v("subtotal") if inv else None, "tax": inv.v("tax_total") if inv else None,
        "total": inv.v("total") if inv else None,
        "decision": dec.decision, "headline": dec.headline, "next_action": dec.next_action,
        "exception_value": dec.exception_value, "extraction_method": ctx.get("method"),
        "payload": {"decision": dec.model_dump(), "checks": [c.model_dump() for c in checks],
                    "stages": [s.model_dump() for s in stages], "errors": ctx.get("errors", []),
                    "extracted": inv.model_dump() if inv else None, "po_comparison": po_comp},
    }

    def s_persist():
        # invoice + lines + audit entry in one transaction (db.save_invoice): all saved or nothing saved
        details = dec.headline + (f" | errors: {len(ctx['errors'])} stage error(s), see record" if ctx.get("errors") else "")
        inv_id = db.save_invoice(rec, plines, pdf_bytes, audit_entry=("APilot", f"Processed -> {dec.decision}", details))
        result.update(saved=True, id=inv_id)
        return "ok", f"Saved as record #{inv_id} with its audit entry.", {"id": inv_id}

    sr = stage("persist", s_persist)
    if sr.status == "error":
        result["persist_error"] = sr.summary
        sr.summary = f"NOT SAVED: {sr.summary}. The decision below is shown but is not in the processing history."
        sr.status = "fail"
    rec["payload"]["stages"] = [s.model_dump() for s in stages]
    return {**result, "decision": dec, "checks": checks, "stages": stages, "extracted": inv,
            "po_comparison": po_comp, "record": rec}


def report(result: dict) -> dict:
    """A self-contained processing report (JSON-serialisable) - downloadable when the database save failed."""
    rec = {k: v for k, v in result["record"].items() if k != "payload"}
    return {"saved": result["saved"], "record_id": result["id"], "persist_error": result["persist_error"],
            "invoice": rec, **result["record"]["payload"]}
