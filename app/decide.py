"""Stage 9 - Decision: fold every finding into APPROVE / HOLD / REJECT with reasons, owners,
the amount that can be paid now, and a ready-to-send message to the vendor when they must act."""
from .checks_order import RANK
from .reference import policy
from .stages.checks import AP, CONTROLLER, F, VENDOR
from .util import money


def decide(findings: list, conf: dict, fields: dict, vendor: dict | None, payable: float | None, po: dict | None) -> dict:
    low = set(conf["low_fields"])
    # If a REJECT rests on a value we couldn't verify, we can't be sure the document is wrong.
    # Downgrade to HOLD and have a person check the reading first.
    for x in findings:
        if x["severity"] == "reject" and low & set(x["depends_on"]):
            x["severity"] = "hold"
            x["owner"] = AP
            x["title"] += " (unverified reading)"
            x["action"] = "Check this value against the PDF. If the document really says this, reject; otherwise correct and re-run."
            x["vendor_ask"] = None
    if low:
        findings.append(F("low_confidence", "hold", "Low-confidence extraction - needs a human check",
                          "Fields: " + ", ".join(f"{k} ({conf['fields'][k]['band']})" for k in sorted(low)),
                          AP, "Open the side-by-side view, verify or correct these fields, then re-run.",
                          depends_on=sorted(low)))

    worst = max((RANK[x["severity"]] for x in findings), default=0)
    total = fields.get("total")
    if worst < RANK["hold"] and total and total > policy()["auto_approve_limit"]:
        findings.append(F("approval_limit", "hold", f"Above auto-approval limit of {money(policy()['auto_approve_limit'])}",
                          "All checks passed; amount needs a second approver per the approval matrix.",
                          CONTROLLER, "Approve in one click."))
        worst = RANK["hold"]
    decision = {RANK["reject"]: "REJECT", RANK["hold"]: "HOLD"}.get(worst, "APPROVE")

    blocking = [x for x in findings if x["severity"] == decision.lower()] if decision != "APPROVE" else []
    routing = {}
    for x in findings:
        if x["severity"] in ("hold", "reject") and x.get("owner"):
            routing.setdefault(x["owner"], []).append(x["action"])
    asks = [x["vendor_ask"] for x in findings if x.get("vendor_ask") and x["severity"] in ("hold", "reject")]

    vname = (vendor or {}).get("name") or fields.get("vendor_name") or "the vendor"
    inv = fields.get("invoice_number") or "(no number)"
    if decision == "APPROVE":
        headline = f"Approve {money(total)} to {vname} - all checks passed"
        pay_now = total
    elif decision == "REJECT":
        headline = f"Reject - {blocking[0]['title']}"
        pay_now = 0.0
    else:
        headline = f"Hold - {blocking[0]['title']}" + (f" (+{len(blocking) - 1} more)" if len(blocking) > 1 else "")
        pay_now = 0.0
    approvable = None
    if decision == "HOLD" and payable is not None and total and 0 < payable < total - 1 \
            and all(x["code"] in ("qty_exceeds_receipt", "over_po_qty", "price_variance") for x in blocking):
        approvable = payable
        headline += f" - {money(payable)} of {money(total)} is approvable now"

    message = None
    if asks:
        message = {
            "to": vname,
            "subject": f"Invoice {inv} - {'action needed' if decision == 'HOLD' else 'cannot be processed'}",
            "body": "Hello,\n\nThank you for invoice " + inv + (f" against {po['po_number']}" if po else "") + ".\n\n"
                    + "\n\n".join(f"{i}. {a}" for i, a in enumerate(asks, 1))
                    + "\n\nOnce we receive this we will process the payment promptly.\n\nRegards,\nAccounts Payable\nNorthwind Retail Pvt Ltd",
        }
    return {"decision": decision, "headline": headline, "pay_now": pay_now, "approvable_now": approvable,
            "blocking": blocking, "routing": [{"owner": k, "actions": v} for k, v in routing.items()],
            "vendor_message": message}
