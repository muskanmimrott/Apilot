"""Human review actions. The UI calls these; tests call them directly, so the same rules apply everywhere.

Approving re-validates the invoice against the CURRENT PO data first (another invoice may have used up the
PO while this one waited). New conflicts block approval unless the reviewer records an explicit override.
Blocking conflicts (duplicate approved meanwhile, PO closed) can never be overridden."""
from datetime import datetime

import db
import rules
from config import OVERRIDE_REASON_MIN_CHARS, REVIEW_OVERRIDE_ALLOWED


class ReviewError(Exception):
    pass


def _common(rec, reviewer, comment):
    if not rec:
        raise ReviewError("Invoice not found.")
    if rec["status"] != "PENDING_REVIEW":
        raise ReviewError(f"Invoice #{rec['id']} is no longer pending (status {rec['status']}).")
    if not (reviewer or "").strip():
        raise ReviewError("Reviewer name is required for the audit trail.")
    if not (comment or "").strip():
        raise ReviewError("A reviewer note is required for the audit trail.")


def approve(invoice_id, reviewer, comment, override=False, override_reason=""):
    rec = db.get_invoice(invoice_id)
    _common(rec, reviewer, comment)
    reval = rules.revalidate_for_approval(rec)   # re-run at click time, never on stale screen data
    if reval["blocking"]:
        raise ReviewError("Cannot approve: " + " ".join(reval["blocking"]))
    if reval["conflicts"]:
        if not REVIEW_OVERRIDE_ALLOWED:
            raise ReviewError("Cannot approve: " + " ".join(reval["conflicts"]))
        if not override:
            raise ReviewError("New conflicts found at approval time. Tick 'Override' and give a reason, or reject: "
                              + " ".join(reval["conflicts"]))
        if len((override_reason or "").strip()) < OVERRIDE_REASON_MIN_CHARS:
            raise ReviewError(f"Override reason must be at least {OVERRIDE_REASON_MIN_CHARS} characters.")
    original = rec["payload"]["decision"]
    details = {
        "reviewer": reviewer.strip(), "comment": comment.strip(),
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "system_decision": original["decision"],
        "accepted_review_findings": original.get("reasons", []),   # what the system flagged and the human accepted
        "revalidation": reval,
        "override": bool(reval["conflicts"]),
        "override_reason": override_reason.strip() if reval["conflicts"] else "",
        "overridden_validations": reval["conflicts"],
    }
    if not db.review_decision(invoice_id, "HUMAN_APPROVED", reviewer.strip(), details):
        raise ReviewError("Another reviewer decided this invoice first; refresh the queue.")
    return details


def reject(invoice_id, reviewer, comment):
    rec = db.get_invoice(invoice_id)
    _common(rec, reviewer, comment)
    details = {"reviewer": reviewer.strip(), "comment": comment.strip(),
               "decided_at": datetime.now().isoformat(timespec="seconds"),
               "system_decision": rec["payload"]["decision"]["decision"],
               "review_findings": rec["payload"]["decision"].get("reasons", [])}
    if not db.review_decision(invoice_id, "HUMAN_REJECTED", reviewer.strip(), details):
        raise ReviewError("Another reviewer decided this invoice first; refresh the queue.")
    return details
