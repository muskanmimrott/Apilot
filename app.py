"""APilot: AI-powered Accounts Payable intelligence. Streamlit UI.
Run:  streamlit run app.py"""
import html
import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import db
import pipeline
import config
import review
import rules
from config import CONFIDENCE_THRESHOLD, GEMINI_MODEL, SAMPLES_DIR, TOLERANCES, gemini_api_key
from extraction import render_preview
from master_data import goods_receipts, purchase_orders, vendors

st.set_page_config(page_title="APilot · Invoice decisions", page_icon="🧾", layout="wide")
db.init()

COLORS = {"APPROVE": "#1E7B4F", "REVIEW": "#A86400", "REJECT": "#B42318", "DUPLICATE": "#6A3FB5"}
STATUS_LABEL = {"APPROVED": "Approved", "PENDING_REVIEW": "Pending review", "REJECTED": "Rejected",
                "DUPLICATE": "Duplicate", "HUMAN_APPROVED": "Approved by reviewer", "HUMAN_REJECTED": "Rejected by reviewer"}
STATUS_COLOR = {"APPROVED": "#1E7B4F", "HUMAN_APPROVED": "#3E9B6E", "PENDING_REVIEW": "#A86400",
                "REJECTED": "#B42318", "HUMAN_REJECTED": "#D0584F", "DUPLICATE": "#6A3FB5"}
STAGE_ICON = {"ok": "✓", "warn": "!", "fail": "✕", "error": "✕", "skipped": "–", "running": "…", "waiting": " "}
STAGE_COLOR = {"ok": "#1E7B4F", "warn": "#A86400", "fail": "#B42318", "error": "#B42318", "skipped": "#8A94A6",
               "running": "#24508F", "waiting": "#C3CAD6"}
SCENARIOS = {
    "01_happy_path_nimbus.pdf": "Happy path: clean invoice that matches its PO",
    "02_duplicate_nimbus_resent_copy.pdf": "Edge case: same invoice re-sent with a reformatted number",
    "03_scanned_kaveri_poor_quality.pdf": "Edge case: poor-quality scan, some fields unreadable",
    "04a_split_zenith_part1.pdf": "Edge case: split billing, part 1 of PO (60 of 100 panels)",
    "04b_split_zenith_part2.pdf": "Edge case: split billing, part 2 (remaining 40 panels + cables)",
    "04c_split_zenith_overbill.pdf": "Edge case: split billing, a third invoice after PO is used up",
    "05_price_mismatch_blueorbit.pdf": "Edge case: unit price 10% above PO",
    "06_bank_change_himalaya.pdf": "Extra: bank account differs from vendor master",
    "07_inactive_vendor_sharma.pdf": "Extra: inactive vendor, closed PO",
    "08_missing_po_nimbus.pdf": "Extra: no PO number on invoice",
    "09_corrupted_file.pdf": "Extra: corrupted file",
    "10_unknown_vendor_orion.pdf": "Extra: vendor not in vendor master",
    "11_missing_grn_zenith.pdf": "Extra: goods not yet receipted (no GRN)",
}

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, .stDataFrame, button, input, textarea { font-family: 'IBM Plex Sans', system-ui, sans-serif; }
.block-container { padding-top: 2rem; max-width: 1280px; }
h1, h2, h3 { letter-spacing: -0.01em; color: #172033; }
.num { font-variant-numeric: tabular-nums; }
.stamp { border: 3px solid var(--c); color: var(--c); border-radius: 6px; padding: 14px 18px; margin: 4px 0 14px;
         background: color-mix(in srgb, var(--c) 6%, white); }
.stamp .word { font-size: 1.9rem; font-weight: 700; letter-spacing: 0.06em; line-height: 1.1; }
.stamp .head { color: #172033; font-size: 1rem; margin-top: 6px; }
.stamp .act { color: #3A4559; font-size: 0.92rem; margin-top: 8px; }
.stage { display: flex; gap: 12px; align-items: flex-start; padding: 7px 0; border-bottom: 1px solid #E4E8EF; }
.stage .dot { flex: 0 0 26px; height: 26px; border-radius: 50%; color: white; font-weight: 700; text-align: center;
              line-height: 26px; font-size: 0.85rem; background: var(--c); }
.stage .t { font-weight: 600; color: #172033; }
.stage .s { color: #4A5568; font-size: 0.88rem; }
.stage .ms { margin-left: auto; color: #8A94A6; font-size: 0.8rem; white-space: nowrap; }
.chip { display: inline-block; padding: 2px 9px; border-radius: 10px; color: white; font-size: 0.78rem; font-weight: 600; background: var(--c); }
.kpi { background: white; border: 1px solid #DDE2EA; border-radius: 6px; padding: 12px 14px; }
.kpi .v { font-size: 1.55rem; font-weight: 700; color: #172033; font-variant-numeric: tabular-nums; }
.kpi .l { color: #5A6578; font-size: 0.85rem; }
.muted { color: #5A6578; font-size: 0.9rem; }
</style>
""", unsafe_allow_html=True)


def inr(x):
    return "—" if x is None else f"₹{x:,.2f}"


def esc(s):
    return html.escape(str(s or ""))


# ------------------------------------------------------------------ shared renderers
def stamp(decision: dict):
    c = COLORS[decision["decision"]]
    st.markdown(f"""<div class="stamp" style="--c:{c}"><div class="word">{decision['decision']}</div>
        <div class="head">{esc(decision['headline'])}</div>
        <div class="act"><b>Next action:</b> {esc(decision['next_action'])}</div></div>""", unsafe_allow_html=True)


def stage_row(name, status, summary="", ms=None):
    c = STAGE_COLOR[status]
    tail = f'<div class="ms">{ms:.0f} ms</div>' if ms is not None else ""
    return f"""<div class="stage" style="--c:{c}"><div class="dot">{STAGE_ICON[status]}</div>
        <div><div class="t">{esc(name)}</div><div class="s">{esc(summary)}</div></div>{tail}</div>"""


def render_stages(stages):
    st.markdown("".join(stage_row(s["name"], s["status"], s["summary"], s["duration_ms"]) for s in stages),
                unsafe_allow_html=True)


def render_checks(checks):
    icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "INFO": "ℹ️", "SKIP": "⏭️"}
    rows = [{"": icon[c["outcome"]], "Stage": c["stage"], "Check": c["name"],
             "Effect": "" if c["effect"] == "NONE" else c["effect"], "Detail": c["message"],
             "At risk (₹)": c["amount_at_risk"] or None} for c in checks]
    order = {"FAIL": 0, "WARN": 1, "SKIP": 2, "INFO": 3, "PASS": 4}
    rows.sort(key=lambda r: order[{v: k for k, v in icon.items()}[r[""]]])
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Detail": st.column_config.TextColumn(width="large"),
                                "At risk (₹)": st.column_config.NumberColumn(format="%.2f")})


def render_fields(extracted):
    if not extracted:
        st.info("Nothing was extracted from this document.")
        return
    label = {"vendor_name": "Vendor name", "vendor_gstin": "Vendor GSTIN", "invoice_number": "Invoice number",
             "invoice_date": "Invoice date", "po_number": "PO number", "currency": "Currency", "subtotal": "Subtotal",
             "cgst": "CGST", "sgst": "SGST", "igst": "IGST", "tax_total": "Total tax", "total": "Invoice total",
             "bank_account": "Bank account", "ifsc": "IFSC"}
    rows = [{"Field": label.get(k, k), "Value": "— not found —" if f["value"] is None else str(f["value"]),
             "Confidence": f["confidence"], "Source": f["source"],
             "Status": "missing" if f["value"] is None else ("verify" if f["confidence"] < CONFIDENCE_THRESHOLD else "ok")}
            for k, f in extracted["fields"].items()]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
    method = "OCR (scanned document)" if extracted["method"] == "ocr" else "Embedded PDF text"
    ocr = f" · mean OCR word confidence {extracted['ocr_mean_conf']:.0%}" if extracted.get("ocr_mean_conf") else ""
    st.caption(f"Read via: {method}{ocr} · AI extraction: {extracted['ai_status']} · "
               f"Fields under {CONFIDENCE_THRESHOLD:.0%} confidence are sent to a person.")
    for n in extracted.get("merge_notes", []):
        st.warning(n)
    if extracted["line_items"]:
        st.markdown("**Line items as read from the invoice**")
        st.dataframe(pd.DataFrame(extracted["line_items"]).rename(columns={
            "item_code": "Item code", "description": "Description", "quantity": "Qty", "unit_price": "Unit price",
            "amount": "Amount", "confidence": "Confidence"}), hide_index=True, width="stretch",
            column_config={"Confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})


def render_po(comp):
    if not comp or not comp.get("po"):
        st.info("No purchase order was matched for this invoice.")
        return
    po = comp["po"]
    a, b, c, d = st.columns(4)
    a.metric("Purchase order", po["po_number"])
    b.metric("PO value", inr(po["value"]))
    c.metric("Billed before this invoice", inr(comp.get("billed_before")))
    d.metric("Remaining before this invoice", inr(comp.get("remaining_before")))
    st.caption(f"Vendor {po['vendor_id']} · PO date {po['po_date']} · status {po['status']} · "
               f"{comp.get('prior_invoices', 0)} earlier approved invoice(s), {comp.get('pending_reviews', 0)} pending review")
    if comp["lines"]:
        df = pd.DataFrame(comp["lines"]).rename(columns={
            "invoice_item": "Item", "description": "Description", "po_item": "PO item", "qty_ordered": "Ordered",
            "qty_received": "Received (GRN)", "qty_billed_before": "Billed before", "qty_invoiced": "This invoice",
            "price_po": "PO price", "price_invoiced": "Invoice price", "price_variance_pct": "Price var %",
            "amount_invoiced": "Amount", "matched_by": "Matched by", "result": "Result"})
        cols = [c for c in ["Item", "Description", "Ordered", "Received (GRN)", "Billed before", "This invoice", "PO price",
                            "Invoice price", "Price var %", "Matched by", "Result"] if c in df.columns]
        st.dataframe(df[cols], hide_index=True, width="stretch")


def render_trace(trace):
    if not trace:
        st.info("This record was processed before decision traces were added.")
        return
    c = st.columns(5)
    c[0].metric("Checks performed", trace["checks_performed"])
    c[1].metric("Passed", len(trace["passed"]))
    c[2].metric("Failed", len(trace["failed"]))
    c[3].metric("Warnings", len(trace["warnings"]))
    c[4].metric("Skipped", len(trace["skipped"]))
    st.markdown(f"**Decision:** {trace['decision']}  \n**Rule applied:** {esc(trace['rule_applied'])}  \n"
                f"**Next action:** {esc(trace['next_action'])}")
    if trace["decisive_checks"]:
        st.markdown("**Checks that decided the outcome**")
        for d in trace["decisive_checks"]:
            st.markdown(f"- {esc(d)}")
    for title, key in [("Failed", "failed"), ("Warnings", "warnings"), ("Skipped (could not run, never counted as passed)", "skipped"),
                       ("Passed", "passed"), ("Information", "info")]:
        if trace[key]:
            with st.expander(f"{title} ({len(trace[key])})", expanded=key in ("failed", "skipped")):
                for item in trace[key]:
                    st.markdown(f"- {esc(item)}")


def render_detail(rec):
    p = rec["payload"]
    stamp(p["decision"])
    left, right = st.columns([2, 3], gap="large")
    with left:
        img = render_preview(rec["pdf"]) if rec.get("pdf") else None
        if img:
            st.image(img, caption=rec["filename"], width="stretch")
        else:
            st.warning("No preview: the file could not be rendered as a PDF.")
        if rec.get("pdf"):
            st.download_button("Download original", rec["pdf"], file_name=rec["filename"] or "invoice.pdf",
                               mime="application/pdf", key=f"dl{rec.get('id') or 'unsaved'}")
    with right:
        t0, t1, t2, t3, t4, t5 = st.tabs(["Decision trace", "Checks", "Extracted data", "PO comparison", "Processing steps", "History"])
        with t0:
            render_trace(p["decision"].get("trace"))
        with t1:
            render_checks(p["checks"])
        with t2:
            render_fields(p.get("extracted"))
        with t3:
            render_po(p.get("po_comparison"))
        with t4:
            render_stages(p["stages"])
            for e in p.get("errors", []):
                with st.expander(f"Error details: {e['stage']}"):
                    st.code(e.get("trace") or e["error"], language=None)
            if p.get("extracted"):
                with st.expander("Raw text the system read"):
                    st.code(p["extracted"]["raw_text"] or "(none)", language=None)
        with t5:
            if rec.get("id"):
                st.dataframe(pd.DataFrame(db.list_audit(rec["id"])), hide_index=True, width="stretch")
            else:
                st.warning("Not saved, so there is no history for this result.")


def goto(invoice_id):
    """Button callback (runs before the rerun, so it may change the navigation widget's value)."""
    st.session_state["explore_id"] = invoice_id
    st.session_state["page"] = "Invoice explorer"


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown("## 🧾 APilot")
    st.caption("Invoice PDFs in, explainable AP decisions out.")
    page = st.radio("Go to", ["Dashboard", "Process invoice", "Review queue", "Invoice explorer", "Audit trail",
                              "Master data & rules"], key="page", label_visibility="collapsed")
    pending_n = sum(1 for r in db.list_invoices() if r["status"] == "PENDING_REVIEW")
    if pending_n:
        st.markdown(f'<span class="chip" style="--c:#A86400">{pending_n} waiting for review</span>', unsafe_allow_html=True)
    st.divider()
    key = gemini_api_key()
    use_ai = st.toggle("Use Gemini for extraction", value=bool(key), disabled=not key,
                       help="Local rules always run. With a Gemini key, AI extraction runs too and the two are cross-checked.")
    st.caption(f"AI: {'Gemini ' + GEMINI_MODEL + ' available' if key else 'no GEMINI_API_KEY set, so local rules + OCR only'}")
    st.divider()
    with st.popover("Reset demo data"):
        st.write("Deletes all processed invoices and the audit trail. Master data is kept.")
        if st.button("Delete all records", type="primary"):
            db.reset()
            st.rerun()


# ------------------------------------------------------------------ pages
def kpi(col, label, value):
    col.markdown(f'<div class="kpi"><div class="v">{value}</div><div class="l">{label}</div></div>', unsafe_allow_html=True)


if page == "Dashboard":
    st.title("Accounts payable overview")
    rows = db.list_invoices()
    if not rows:
        st.info("No invoices processed yet. Go to **Process invoice** and run the happy-path sample to begin.")
        st.stop()
    df = pd.DataFrame(rows)
    uniq = df[df.status != "DUPLICATE"]
    approved = df.status.isin(["APPROVED", "HUMAN_APPROVED"]).sum()
    held = df[df.status.isin(["PENDING_REVIEW", "REJECTED", "DUPLICATE", "HUMAN_REJECTED"])]
    c = st.columns(6)
    kpi(c[0], "Invoices processed", len(df))
    kpi(c[1], "Approved", int(approved))
    kpi(c[2], "Pending review", int((df.status == "PENDING_REVIEW").sum()))
    kpi(c[3], "Rejected", int(df.status.isin(["REJECTED", "HUMAN_REJECTED"]).sum()))
    kpi(c[4], "Duplicates caught", int((df.status == "DUPLICATE").sum()))
    kpi(c[5], "Auto-approved (no human)", f"{(df.decision == 'APPROVE').sum() / max(len(uniq), 1):.0%}")
    st.write("")
    c = st.columns(3)
    kpi(c[0], "Value of unique invoices received", inr(uniq.total.fillna(0).sum()))
    kpi(c[1], "Value held for exceptions", inr(held.total.fillna(0).sum()))
    kpi(c[2], "Duplicate payments prevented", inr(df[df.status == "DUPLICATE"].total.fillna(0).sum()))
    st.caption("All figures are computed live from the invoices in this database, not estimates.")
    g1, g2 = st.columns(2)
    with g1:
        counts = df.status.value_counts()
        fig = go.Figure(go.Pie(labels=[STATUS_LABEL[s] for s in counts.index], values=counts.values, hole=0.55,
                               marker_colors=[STATUS_COLOR[s] for s in counts.index], sort=False))
        fig.update_layout(title="Outcome of every invoice", height=320, margin=dict(t=50, b=10, l=10, r=10),
                          font_family="IBM Plex Sans", legend=dict(orientation="v"))
        st.plotly_chart(fig, width="stretch")
    with g2:
        v = df.assign(vendor=df.vendor_name.fillna("Unknown"), label=df.status.map(STATUS_LABEL)) \
              .groupby(["vendor", "status"], as_index=False).total.sum()
        fig = go.Figure()
        for s in v.status.unique():
            sub = v[v.status == s]
            fig.add_bar(y=sub.vendor, x=sub.total, name=STATUS_LABEL[s], orientation="h", marker_color=STATUS_COLOR[s])
        fig.update_layout(barmode="stack", title="Invoice value by vendor (₹)", height=320,
                          margin=dict(t=50, b=10, l=10, r=10), font_family="IBM Plex Sans", legend=dict(orientation="h", y=-0.2))
        st.plotly_chart(fig, width="stretch")
    st.subheader("Processing history")
    show = df[["id", "received_at", "vendor_name", "invoice_number", "po_number", "total", "status", "headline"]].copy()
    show["status"] = show.status.map(STATUS_LABEL)
    st.dataframe(show.rename(columns={"id": "#", "received_at": "Received", "vendor_name": "Vendor", "invoice_number": "Invoice",
                                      "po_number": "PO", "total": "Total (₹)", "status": "Status", "headline": "Why"}),
                 hide_index=True, width="stretch",
                 column_config={"Total (₹)": st.column_config.NumberColumn(format="%.2f"),
                                "Why": st.column_config.TextColumn(width="large")})

elif page == "Process invoice":
    st.title("Process an invoice")
    st.markdown('<p class="muted">Upload a vendor invoice PDF, or pick one of the prepared demo invoices. '
                'Every step below is the real code running, in order.</p>', unsafe_allow_html=True)
    src = st.radio("Source", ["Demo invoice", "Upload a PDF"], horizontal=True, label_visibility="collapsed")
    pdf_bytes, name = None, None
    if src == "Upload a PDF":
        up = st.file_uploader("Invoice PDF", type=["pdf"])
        if up:
            pdf_bytes, name = up.getvalue(), up.name
    else:
        files = sorted(p.name for p in SAMPLES_DIR.glob("*.pdf"))
        name = st.selectbox("Demo invoice", files, format_func=lambda f: f"{f}  ·  {SCENARIOS.get(f, '')}")
        pdf_bytes = (SAMPLES_DIR / name).read_bytes()
    if pdf_bytes:
        left, right = st.columns([2, 3], gap="large")
        with left:
            img = render_preview(pdf_bytes)
            if img:
                st.image(img, caption=name, width="stretch")
            else:
                st.warning("This file cannot be previewed. It may not be a valid PDF; processing will check.")
        with right:
            go_btn = st.button("Process invoice", type="primary", width="stretch")
            box = st.container()
            if go_btn:
                slots = {k: box.empty() for k, _ in pipeline.STAGES}
                for k, n in pipeline.STAGES:
                    slots[k].markdown(stage_row(n, "waiting", "Waiting"), unsafe_allow_html=True)

                def on_event(kind, key, payload):
                    if kind == "start":
                        slots[key].markdown(stage_row(payload, "running", "Running…"), unsafe_allow_html=True)
                    else:
                        slots[key].markdown(stage_row(payload.name, payload.status, payload.summary, payload.duration_ms),
                                            unsafe_allow_html=True)

                res = pipeline.run(pdf_bytes, name, use_ai=use_ai, api_key=key, on_event=on_event)
                for s_ in res["stages"]:  # final state of every stage (incl. skipped and the save result)
                    slots[s_.key].markdown(stage_row(s_.name, s_.status, s_.summary, None if s_.status == "skipped" else s_.duration_ms),
                                           unsafe_allow_html=True)
                # Keep the result in the session even if the database save failed, so nothing is lost on screen.
                st.session_state["last_result"] = {"saved": res["saved"], "id": res["id"], "filename": name,
                                                   "error": res["persist_error"], "record": {**res["record"], "pdf": pdf_bytes},
                                                   "report": json.dumps(pipeline.report(res), indent=2, default=str)}
        last = st.session_state.get("last_result")
        if last and last["filename"] == name:
            st.divider()
            if last["saved"]:
                rec = db.get_invoice(last["id"])
                st.subheader(f"Result · saved as record #{last['id']}")
                render_detail(rec)
            else:
                st.error(f"**This result was NOT saved to the processing history.** {last['error']}  \n"
                         "The invoice is not in the dashboard, review queue or audit trail, and it will not count "
                         "towards PO balances or duplicate checks. Download the report below, fix the storage problem, "
                         "and process the invoice again.")
                st.download_button("Download processing report (JSON)", last["report"],
                                   file_name=f"apilot_report_{name}.json", mime="application/json")
                st.subheader("Result · not saved")
                render_detail({**last["record"], "id": None})

elif page == "Review queue":
    st.title("Review queue")
    pending = [r for r in db.list_invoices() if r["status"] == "PENDING_REVIEW"]
    if not pending:
        st.success("Nothing is waiting for review.")
    for r in pending:
        with st.expander(f"#{r['id']} · {r['vendor_name'] or 'Unknown vendor'} · {r['invoice_number'] or 'no invoice number'} · {inr(r['total'])}",
                         expanded=len(pending) <= 3):
            rec = db.get_invoice(r["id"])
            d = rec["payload"]["decision"]
            st.markdown(f"**Why it's here:** {esc(d['headline'])}")
            for reason in d["reasons"]:
                st.markdown(f"- {esc(reason)}")
            st.markdown(f"**Recommended action:** {esc(d['next_action'])}")
            # Re-validate against today's PO data: things may have changed while this invoice waited.
            reval = rules.revalidate_for_approval(rec)
            st.markdown("**Re-checked just now against current PO data:** " + ", ".join(reval["checked"]))
            if reval["blocking"]:
                st.error("Approval blocked: " + " ".join(reval["blocking"]))
            if reval["conflicts"]:
                st.warning("**New or remaining conflicts if approved now:**\n\n" + "\n".join(f"- {esc(c)}" for c in reval["conflicts"]))
            if not reval["blocking"] and not reval["conflicts"]:
                st.success("No PO balance, quantity, receipt or duplicate conflicts as of now.")
            comment = st.text_area("Reviewer note (required)", key=f"c{r['id']}",
                                   placeholder="e.g. Called vendor on file number; price increase approved by buyer.")
            name_ = st.text_input("Your name (required)", key=f"n{r['id']}", placeholder="e.g. Priya S., AP team")
            override, reason_ = False, ""
            if reval["conflicts"] and not reval["blocking"] and config.REVIEW_OVERRIDE_ALLOWED:
                override = st.checkbox("Override the conflicts above (recorded in the audit trail)", key=f"ov{r['id']}")
                if override:
                    reason_ = st.text_area("Override reason (required)", key=f"or{r['id']}",
                                           placeholder="e.g. PO amendment PO-2026-0104-A approved by procurement head.")
            a, b, c = st.columns([1, 1, 2])
            if a.button("Approve for payment", key=f"a{r['id']}", type="primary", disabled=bool(reval["blocking"])):
                try:
                    out = review.approve(r["id"], name_, comment, override=override, override_reason=reason_)
                    st.toast(f"#{r['id']} approved by {name_}" + (" with override" if out["override"] else ""))
                    st.rerun()
                except review.ReviewError as e:
                    st.error(str(e))
            if b.button("Reject", key=f"r{r['id']}"):
                try:
                    review.reject(r["id"], name_, comment)
                    st.toast(f"#{r['id']} rejected by {name_}")
                    st.rerun()
                except review.ReviewError as e:
                    st.error(str(e))
            c.button("Open full details", key=f"o{r['id']}", on_click=goto, args=(r["id"],))

elif page == "Invoice explorer":
    st.title("Invoice explorer")
    rows = db.list_invoices()
    if not rows:
        st.info("No invoices yet.")
        st.stop()
    ids = [r["id"] for r in rows]
    default = ids.index(st.session_state["explore_id"]) if st.session_state.get("explore_id") in ids else 0
    lab = {r["id"]: f"#{r['id']} · {r['vendor_name'] or 'Unknown'} · {r['invoice_number'] or '—'} · {STATUS_LABEL[r['status']]}" for r in rows}
    sel = st.selectbox("Invoice", ids, index=default, format_func=lab.get)
    rec = db.get_invoice(sel)
    if rec["status"] != rec["decision"] and rec["status"].startswith("HUMAN"):
        st.info(f"System decided **{rec['decision']}**; current status is **{STATUS_LABEL[rec['status']]}** (see History).")
    render_detail(rec)

elif page == "Audit trail":
    st.title("Audit trail")
    st.markdown('<p class="muted">Append-only log of every system decision and every human action.</p>', unsafe_allow_html=True)
    a = pd.DataFrame(db.list_audit())
    if a.empty:
        st.info("No activity yet.")
    else:
        a.columns = ["Timestamp", "Record #", "Invoice", "Vendor", "Actor", "Action", "Details"]
        st.dataframe(a, hide_index=True, width="stretch", column_config={"Details": st.column_config.TextColumn(width="large")})
        st.download_button("Download audit trail (CSV)", a.to_csv(index=False), "apilot_audit_trail.csv", "text/csv")

else:
    st.title("Master data & rules")
    st.markdown('<p class="muted">The procurement data APilot checks against, and the rules it applies.</p>', unsafe_allow_html=True)
    t1, t2, t3 = st.tabs(["Purchase orders", "Vendor master", "Rules & tolerances"])
    with t1:
        rows = []
        for po in purchase_orders().values():
            billed, val, n = db.billed_against_po(po["po_number"])
            rows.append({"PO": po["po_number"], "Vendor": vendors()[po["vendor_id"]]["legal_name"], "Status": po["status"],
                         "PO value (₹)": po["value"], "Billed (₹)": val, "Remaining (₹)": round(po["value"] - val, 2),
                         "Used": val / po["value"], "Approved invoices": n})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"Used": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.0f%%")})
        po_sel = st.selectbox("PO lines", list(purchase_orders()))
        grn = goods_receipts().get(po_sel, {})
        billed, _, _ = db.billed_against_po(po_sel)
        st.dataframe(pd.DataFrame([{**l, "received_qty (GRN)": grn.get(l["item_code"]), "billed_qty": billed.get(l["item_code"], 0)}
                                   for l in purchase_orders()[po_sel]["lines"]]), hide_index=True, width="stretch")
    with t2:
        v = pd.DataFrame(vendors().values())
        v["aliases"] = v.aliases.apply(", ".join)
        v["bank_account"] = v.bank_account.apply(lambda s: "…" + s[-4:])
        st.dataframe(v, hide_index=True, width="stretch")
    with t3:
        st.markdown(f"""
| Rule | Setting | Effect if broken |
|---|---|---|
| Unit price above PO price | tolerance **{TOLERANCES['unit_price_pct']}%** | Review |
| Invoice above remaining PO value | tolerance **{TOLERANCES['po_value_pct']}%** | Review (Reject if PO fully billed) |
| Arithmetic (lines, tax, total) | rounding **₹{TOLERANCES['rounding_abs']:.2f}** | Review |
| GST rate (demo assumption) | **{config.GST_RATE:.0%}** on every line; real rates depend on HSN/SAC codes | Review |
| GST type | same state: CGST + SGST, equal halves; other state: IGST only | Review |
| Goods receipt (3-way) | **{"mandatory" if config.GRN_REQUIRED else "optional (skipped if absent)"}** | Review if missing or short |
| Quantity billed (cumulative) | ≤ ordered; ≤ received per goods receipt | Review, or Reject if line already fully billed |
| Extraction confidence | ≥ **{CONFIDENCE_THRESHOLD:.0%}** on vendor, invoice no., date, total, PO | Review |
| Vendor | must exist and be ACTIVE | Reject |
| Bank account | must match vendor master | Review (fraud signal) |
| Duplicate | same file, or same vendor + normalised invoice no. | Duplicate |
| Near-duplicate | same vendor + amount, similar number or same date | Review |
| Safety valve | a Reject based on low-confidence data | becomes Review |
| System error | any stage fails unexpectedly | Automation stops; Review (never Approve) |
| Reviewer approval | re-validated against current PO data | New conflicts need an audited override |
""")
