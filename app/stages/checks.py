"""Stages 4-8: deterministic business checks. Every check returns Findings.

Severity ladder (worst wins in the decision):
  pass < info < warn < hold < reject
  * reject = the DOCUMENT is not payable as issued; the vendor must re-issue or it's a duplicate.
  * hold   = payable maybe, but someone on OUR side must act first (receive goods, approve a
             variance, verify bank details, confirm a PO, check our reading of the document).
Each finding names an owner and the action they should take, so the output is a to-do, not a log line.
"""
import re
from datetime import date

from rapidfuzz import fuzz

from .. import db
from ..gst import expected_tax_type, state_code_from_text, state_of, validate_gstin, normalize_gstin, STATE_CODES
from ..reference import buyer, grns_for_po, po as get_po, policy, pos_for_vendor, received_qty, ref, vendor_by_gstin
from ..util import money, norm_invoice_no, norm_text, parse_date

AP, BUYER, RECEIVING, TREASURY, TAX, VENDOR, CONTROLLER = (
    "AP team", "Procurement (PO owner)", "Receiving / warehouse", "Treasury", "Tax team", "Vendor", "Finance controller")


def F(code, severity, title, detail="", owner=None, action=None, vendor_ask=None, depends_on=None, data=None):
    return {"code": code, "severity": severity, "title": title, "detail": detail, "owner": owner, "action": action,
            "vendor_ask": vendor_ask, "depends_on": depends_on or [], "data": data or {}}


# ------------------------------------------------------------------ 4. document validation

def validate_document(f: dict, conf: dict, today: date | None = None) -> list:
    today = today or date.today()
    pol, out = policy(), []
    for field, label in (("invoice_number", "Invoice number"), ("invoice_date", "Invoice date"),
                         ("total", "Invoice total"), ("line_items", "Line items")):
        if not f.get(field):
            out.append(F("missing_" + field, "hold", f"{label} not found",
                         "Either it is not printed or we could not read it - a person needs to look.",
                         AP, f"Check the PDF for the {label.lower()}; if absent, request a corrected invoice.",
                         depends_on=[field]))
    d = parse_date(f.get("invoice_date"))
    if d:
        if d > today:
            out.append(F("future_date", "hold", "Invoice is dated in the future", f"Dated {d}, today is {today}.",
                         AP, "Confirm the date with the vendor before booking.", depends_on=["invoice_date"]))
        elif (today - d).days > pol["max_invoice_age_days"]:
            out.append(F("stale_invoice", "hold", "Invoice is older than policy allows",
                         f"{(today - d).days} days old (limit {pol['max_invoice_age_days']}). GST input credit may be time-barred.",
                         TAX, "Check ITC eligibility before paying.", depends_on=["invoice_date"]))
        else:
            out.append(F("date_ok", "pass", f"Invoice date {d:%d %b %Y} is within policy"))

    m = conf["arithmetic"]
    if f.get("currency") and f["currency"] != "INR":
        out.append(F("currency", "hold", f"Invoice is in {f['currency']}, vendor master and PO are in INR",
                     "Needs FX conversion at the contracted rate before matching.", AP,
                     "Confirm the FX basis with the PO owner.", depends_on=["total"]))
    math_problems = list(m["line_errors"])
    if m["subtotal_ok"] is False:
        math_problems.append(f"lines add up to {money(m['sum_lines'])}, subtotal printed {money(f['subtotal'])}")
    if m["total_ok"] is False:
        sub = f.get("subtotal") if f.get("subtotal") is not None else m["sum_lines"]
        math_problems.append(f"subtotal {money(sub)} + tax {money(m['tax_sum'])} = {money((sub or 0) + m['tax_sum'])}, "
                             f"total printed {money(f.get('total'))}")
    if math_problems:
        grounded = all(conf["fields"][k]["band"] in ("high", "medium") for k in ("total", "line_items"))
        out.append(F("arithmetic", "hold", "Invoice amounts don't add up", "; ".join(math_problems),
                     VENDOR if grounded else AP,
                     "Ask the vendor for a corrected invoice." if grounded else "Check our reading of the amounts against the PDF.",
                     vendor_ask="The amounts on the invoice do not reconcile (" + "; ".join(math_problems) + "). Please send a corrected invoice." if grounded else None,
                     depends_on=["total", "line_items"]))
    elif f.get("total") is not None:
        out.append(F("arithmetic_ok", "pass", "Amounts reconcile",
                     f"Lines {money(m['sum_lines'])} + tax {money(m['tax_sum'])} = total {money(f['total'])}"))
    return out


# ------------------------------------------------------------------ 5. vendor & GST compliance

def vendor_and_tax(f: dict, conf: dict) -> tuple[list, dict | None]:
    out, b = [], buyer()
    vendor = vendor_by_gstin(f.get("vendor_gstin"))
    ok, why = validate_gstin(f.get("vendor_gstin"))

    if f.get("vendor_gstin") and not ok:
        out.append(F("vendor_gstin_invalid", "reject", "Vendor GSTIN is invalid", f"{f['vendor_gstin']}: {why}.",
                     TAX, "Do not book. Input tax credit cannot be claimed on an invalid GSTIN.",
                     vendor_ask=f"The GSTIN printed on your invoice ({f['vendor_gstin']}) is not valid ({why}). Please re-issue the invoice with your correct GSTIN.",
                     depends_on=["vendor_gstin"]))
    elif ok:
        out.append(F("vendor_gstin_ok", "pass", "Vendor GSTIN is structurally valid (checksum verified)", normalize_gstin(f["vendor_gstin"])))

    if not vendor:
        cand = max(ref()["vendors"], key=lambda v: fuzz.token_set_ratio(norm_text(v["name"]), norm_text(f.get("vendor_name"))))
        score = fuzz.token_set_ratio(cand["name"].lower(), (f.get("vendor_name") or "").lower())
        if score >= 88 and f.get("vendor_gstin"):
            out.append(F("vendor_gstin_mismatch", "hold", "GSTIN on invoice does not match the vendor master",
                         f"Name matches '{cand['name']}' ({score}%), but master GSTIN is {cand['gstin']} and the invoice shows {f.get('vendor_gstin')}. "
                         "Could be a different registration of the same vendor - or an impersonation.",
                         BUYER, "Verify with the vendor using the contact on file, then update the master or reject.",
                         depends_on=["vendor_gstin"]))
            vendor = cand
        elif score >= 88:
            vendor = cand
            out.append(F("vendor_by_name", "warn", f"Vendor identified by name only: {cand['name']}",
                         "No GSTIN read from the invoice.", depends_on=["vendor_gstin"]))
        else:
            out.append(F("vendor_unknown", "hold", "Vendor is not in the approved vendor master",
                         f"'{f.get('vendor_name')}' / {f.get('vendor_gstin')}", BUYER,
                         "Route to procurement: onboard the vendor or reject the invoice."))
            return out, None
    else:
        out.append(F("vendor_ok", "pass", f"Approved vendor: {vendor['name']} ({vendor['vendor_id']})"))
    if vendor and vendor.get("status") != "active":
        out.append(F("vendor_inactive", "hold", "Vendor is not active in the master", vendor["status"], BUYER, "Confirm vendor status."))

    # Buyer GSTIN: must be ours, or we lose input tax credit
    bg = normalize_gstin(f.get("buyer_gstin"))
    if not bg:
        out.append(F("buyer_gstin_missing", "reject", "Our GSTIN is not on the invoice",
                     "A B2B tax invoice must carry the recipient's GSTIN for us to claim input tax credit.", TAX,
                     "Return to vendor.", vendor_ask=f"Please re-issue the invoice showing our GSTIN {b['gstin']}.",
                     depends_on=["buyer_gstin"]))
    elif bg != b["gstin"]:
        out.append(F("buyer_gstin_wrong", "reject", "Invoice is addressed to a different GSTIN",
                     f"Invoice shows {bg}; {b['name']} is {b['gstin']}.", TAX, "Return to vendor.",
                     vendor_ask=f"The invoice carries GSTIN {bg}, which is not ours. Please re-issue it to {b['name']}, GSTIN {b['gstin']}.",
                     depends_on=["buyer_gstin"]))
    else:
        out.append(F("buyer_gstin_ok", "pass", "Invoice is billed to our GSTIN", b["gstin"]))

    # Tax head: intra-state -> CGST+SGST, inter-state -> IGST
    supplier_state = state_of(f.get("vendor_gstin")) or (vendor and vendor["state"])
    pos_state = state_code_from_text(f.get("place_of_supply")) or b["state"]
    has_igst = (f.get("igst") or 0) > 0
    has_cs = (f.get("cgst") or 0) > 0 or (f.get("sgst") or 0) > 0
    charged = "IGST" if has_igst and not has_cs else "CGST+SGST" if has_cs and not has_igst else ("mixed" if has_igst else "none")
    if supplier_state:
        expected = expected_tax_type(supplier_state, pos_state)
        where = f"{STATE_CODES.get(supplier_state)} -> {STATE_CODES.get(pos_state)}"
        if charged == "none":
            out.append(F("no_tax", "hold", "No GST charged", f"Supply {where} should carry {expected}.", TAX,
                         "Confirm whether the supply is exempt.", depends_on=["cgst", "sgst", "igst"]))
        elif charged != expected:
            fix = ("CGST + SGST (intra-state supply)" if expected == "CGST+SGST" else "IGST (inter-state supply)")
            out.append(F("wrong_tax_head", "reject", f"Wrong GST type: charged {charged}, should be {expected}",
                         f"Supply is {where}. Tax paid under the wrong head can't be claimed as input credit by us; "
                         "the vendor has to issue a credit note and a fresh invoice.",
                         TAX, "Do not pay. Request credit note + corrected invoice.",
                         vendor_ask=f"Your invoice charges {charged} on a supply from {STATE_CODES.get(supplier_state)} to "
                                    f"{STATE_CODES.get(pos_state)}. This should be {fix}. Please issue a credit note against "
                                    f"this invoice and a fresh invoice with the correct tax.",
                         depends_on=["cgst", "sgst", "igst", "vendor_gstin", "place_of_supply"]))
        else:
            if charged == "CGST+SGST" and abs((f.get("cgst") or 0) - (f.get("sgst") or 0)) > 1:
                out.append(F("cgst_sgst_unequal", "reject", "CGST and SGST are not equal",
                             f"CGST {money(f.get('cgst'))} vs SGST {money(f.get('sgst'))}.", TAX, "Request corrected invoice.",
                             vendor_ask="CGST and SGST on your invoice should be equal halves. Please correct and re-issue.",
                             depends_on=["cgst", "sgst"]))
            else:
                out.append(F("tax_head_ok", "pass", f"{charged} is correct for {where}"))

    # Bank details vs master: the classic payment-fraud vector
    if vendor:
        mb = vendor["bank"]
        acct, ifsc = (f.get("bank_account_number") or "").strip(), (f.get("bank_ifsc") or "").strip().upper()
        if not acct and not ifsc:
            out.append(F("bank_absent", "info", "No bank details on invoice - payment goes to the account on file"))
        elif acct != mb["account_number"] or (ifsc and ifsc != mb["ifsc"]):
            note = f" Invoice note: \"{f['notes']}\"" if f.get("notes") else ""
            out.append(F("bank_changed", "hold", "Bank details differ from the vendor master",
                         f"Invoice: A/c {acct or '?'} / {ifsc or '?'}. Master: A/c {mb['account_number']} / {mb['ifsc']}.{note} "
                         "Changed bank details on an invoice are the most common invoice-fraud pattern.",
                         TREASURY, f"Do NOT pay to the new account. Call the vendor on the number already on file "
                                   f"({vendor.get('contact')}), not any number on the invoice. Update the master only after "
                                   "written confirmation on letterhead + cancelled cheque.",
                         depends_on=["bank_account_number", "bank_ifsc"],
                         data={"invoice_account": acct, "master_account": mb["account_number"]}))
        else:
            out.append(F("bank_ok", "pass", "Bank details match the vendor master", f"A/c ...{acct[-4:]} / {ifsc}"))
    return out, vendor


# ------------------------------------------------------------------ 6. duplicates

def duplicates(f: dict, vendor: dict | None, file_hash: str, run_id: int) -> list:
    out, pol = [], policy()
    prior = db.active_runs(exclude_id=run_id)
    same_file = next((r for r in prior if r["file_hash"] == file_hash), None)
    if same_file:
        return [F("dup_file", "reject", "This exact file was already processed",
                  f"Run #{same_file['id']} ({same_file['filename']}), decision {same_file.get('final_decision') or same_file['decision']}.",
                  AP, "Discard - no action needed.", data={"original_run": same_file["id"]})]
    if not vendor:
        return [F("dup_skipped", "info", "Duplicate check limited - vendor not identified")]
    key = norm_invoice_no(f.get("invoice_number"))
    mine = [r for r in prior if r["vendor_id"] == vendor["vendor_id"]]
    hit = next((r for r in mine if key and r["invoice_number_norm"] == key), None)
    if hit:
        same_fmt = hit["invoice_number"] == f.get("invoice_number")
        return [F("dup_invoice", "reject", "Duplicate invoice",
                  f"{vendor['name']} invoice '{f.get('invoice_number')}' matches '{hit['invoice_number']}' from run #{hit['id']}"
                  f"{'' if same_fmt else ' (same number, different formatting)'}, already "
                  f"{(hit.get('final_decision') or hit['decision']).lower()}d for {money(hit['total'])}.",
                  AP, "Do not pay again. Reply to vendor with the status of the original.",
                  vendor_ask=f"We already have invoice {hit['invoice_number']} ({money(hit['total'])}) on record; "
                             f"this copy will not be processed separately.",
                  depends_on=["invoice_number"], data={"original_run": hit["id"]})]
    d = parse_date(f.get("invoice_date"))
    for r in mine:
        rd = parse_date(r.get("invoice_date"))
        if r["total"] is not None and f.get("total") is not None and abs(r["total"] - f["total"]) < 1 and d and rd \
                and abs((d - rd).days) <= pol["possible_duplicate_window_days"]:
            out.append(F("dup_possible", "hold", "Possible duplicate: same vendor, same amount, close dates",
                         f"Run #{r['id']} invoice {r['invoice_number']} for {money(r['total'])} dated {rd}. "
                         "Different invoice number, so it may be a legitimate repeat order.",
                         AP, "Compare both invoices; release if they are different supplies.",
                         data={"original_run": r["id"]}))
            return out
    out.append(F("dup_none", "pass", "No duplicate found", f"Checked {len(mine)} prior invoice(s) from this vendor"))
    return out


# ------------------------------------------------------------------ 7. PO resolution

def _line_sim(inv_line, po_line) -> float:
    s = fuzz.token_set_ratio((inv_line.get("description") or "").lower(), po_line["description"].lower()) / 100
    if inv_line.get("hsn") and str(inv_line["hsn"]) == str(po_line["hsn"]):
        s = min(1.0, s * 0.8 + 0.2)
    else:
        s *= 0.85
    return round(s, 3)


def _score_po(f, p):
    items = f.get("line_items") or []
    if not items:
        return 0.0, {}
    cover = sum(max((_line_sim(it, pl) for pl in p["lines"]), default=0) for it in items) / len(items)
    po_value = sum(pl["qty"] * pl["unit_price"] for pl in p["lines"])
    inv_sub = f.get("subtotal") or sum(it.get("amount") or 0 for it in items)
    fit = 1 - min(1, abs(inv_sub - po_value) / max(inv_sub, po_value, 1))
    hint = 0.0
    refs = " ".join(filter(None, [f.get("other_references"), f.get("notes")]))
    pd = parse_date(p["date"])
    for m in re.finditer(r"\d{1,2}[\-/.]\d{1,2}[\-/.]\d{2,4}|\d{1,2}[\s\-][A-Za-z]{3,9}[\s\-]\d{2,4}", refs):
        if parse_date(m.group(0)) == pd:
            hint = 1.0
    score = round(0.6 * cover + 0.25 * fit + 0.15 * hint, 3)
    return score, {"line_coverage": round(cover, 2), "amount_fit": round(fit, 2), "date_reference": hint}


def resolve_po(f: dict, vendor: dict | None, overrides: dict) -> tuple[list, dict | None, dict]:
    pol = policy()
    if overrides.get("po_number"):
        p = get_po(overrides["po_number"])
        if p:
            return [F("po_confirmed", "pass", f"PO {p['po_number']} confirmed by {overrides.get('confirmed_by', 'AP user')}",
                      overrides.get("note", ""))], p, {"method": "human-confirmed"}
    raw = f.get("po_reference")
    if raw:
        m = re.search(r"(\d{3,})", raw)
        p = get_po(f"PO-{m.group(1)}") if m else None
        if p and vendor and p["vendor_id"] != vendor["vendor_id"]:
            return [F("po_other_vendor", "hold", f"{p['po_number']} belongs to a different vendor",
                      f"PO vendor {p['vendor_id']}, invoice vendor {vendor['vendor_id']}.", BUYER,
                      "Confirm the correct PO with the requester.", depends_on=["po_reference"])], None, {"method": "explicit"}
        if p:
            return [F("po_found", "pass", f"{p['po_number']} found (quoted on invoice)",
                      f"Raised {p['date']} by {p['buyer']}, status {p['status']}")], p, {"method": "explicit"}
        missing = F("po_not_found", "hold", f"PO '{raw}' quoted on the invoice does not exist",
                    "Typo, a PO from another entity, or a fabricated reference.", BUYER,
                    "Confirm the PO number with the requester.", depends_on=["po_reference"])
    else:
        missing = None
    if not vendor:
        return ([missing] if missing else []) + [F("po_none", "hold", "No PO and vendor unknown - cannot match", "", AP,
                                                    "Route to procurement.")], None, {"method": "none"}

    cands = []
    for p in pos_for_vendor(vendor["vendor_id"]):
        s, parts = _score_po(f, p)
        cands.append({"po_number": p["po_number"], "score": s, **parts})
    cands.sort(key=lambda c: -c["score"])
    info = {"method": "inferred", "candidates": cands}
    best = cands[0] if cands else None
    margin = best["score"] - (cands[1]["score"] if len(cands) > 1 else 0) if best else 0
    pre = [missing] if missing else []
    if best and best["score"] >= pol["implied_po_min_score"] and margin >= pol["implied_po_min_margin"]:
        p = get_po(best["po_number"])
        why = (f"Best of {len(cands)} open PO(s) for this vendor: score {best['score']:.2f} "
               f"(line match {best['line_coverage']:.2f}, amount fit {best['amount_fit']:.2f}"
               f"{', order date referenced on invoice' if best['date_reference'] else ''}); next best {margin:.2f} lower.")
        return pre + [F("po_inferred", "hold", f"No PO number on invoice - inferred {p['po_number']}", why, AP,
                        f"Confirm {p['po_number']} is the right PO (one click), then the invoice re-runs.",
                        data={"suggested_po": p["po_number"], "candidates": cands})], p, info
    return pre + [F("po_ambiguous" if cands else "po_none", "hold",
                    "No PO number and no confident match" if cands else "No PO number and no open PO for this vendor",
                    "; ".join(f"{c['po_number']}={c['score']:.2f}" for c in cands[:3]) or "", BUYER,
                    "Ask the requester which PO this belongs to, or raise a retrospective PO / non-PO approval.",
                    data={"candidates": cands})], None, info


# ------------------------------------------------------------------ 8. three-way match

def three_way(f: dict, p: dict | None, run_id: int) -> tuple[list, list, float | None]:
    if not p:
        return [F("match_skipped", "info", "3-way match skipped - no PO")], [], None
    pol, out = policy(), []
    grns = grns_for_po(p["po_number"])
    if not grns:
        return [F("no_grn", "hold", f"No goods receipt recorded against {p['po_number']}",
                  "We can't pay for goods we haven't confirmed receiving.", RECEIVING,
                  "Post the GRN if goods arrived; otherwise hold until delivery.")], [], 0.0
    recv = received_qty(p["po_number"])
    billed = db.billed_qty(p["po_number"], exclude_run=run_id)
    items = f.get("line_items") or []

    # greedy one-to-one assignment of invoice lines to PO lines by similarity
    pairs = sorted(((_line_sim(it, pl), i, pl["line"]) for i, it in enumerate(items) for pl in p["lines"]), reverse=True)
    assign, used = {}, set()
    for s, i, ln in pairs:
        if s >= 0.6 and i not in assign and ln not in used:
            assign[i] = (ln, s)
            used.add(ln)

    rows, payable = [], 0.0
    for i, it in enumerate(items):
        row = {"invoice_line": i + 1, "description": it.get("description"), "qty": it.get("quantity"),
               "rate": it.get("unit_price"), "amount": it.get("amount"), "checks": []}
        if i not in assign:
            out.append(F("line_not_on_po", "hold", f"Line {i + 1} '{it.get('description')}' is not on {p['po_number']}",
                         "Item billed that wasn't ordered.", BUYER, "Approve as an addition to the PO or ask vendor to remove it."))
            row["status"] = "not on PO"
            rows.append(row)
            continue
        ln, sim = assign[i]
        pl = next(x for x in p["lines"] if x["line"] == ln)
        q, r = it.get("quantity") or 0, it.get("unit_price") or 0
        got, prev = recv.get(ln, 0), billed.get(ln, 0)
        avail, po_left = max(0.0, got - prev), max(0.0, pl["qty"] - prev)
        row.update(po_line=ln, po_desc=pl["description"], match=sim, po_rate=pl["unit_price"], po_qty=pl["qty"],
                   received=got, billed_before=prev, gst_rate=pl["gst_rate"])
        status = "ok"
        # price
        diff = r - pl["unit_price"]
        tol = pl["unit_price"] * pol["price_tolerance_pct"] / 100
        if abs(diff) < 0.005:
            row["checks"].append("price = PO")
        elif diff < 0:
            row["checks"].append(f"price {money(r)} below PO {money(pl['unit_price'])}")
        elif diff <= tol:
            out.append(F("price_within_tol", "info", f"Line {i + 1}: price {diff / pl['unit_price'] * 100:.2f}% above PO, within {pol['price_tolerance_pct']}% tolerance",
                         f"{money(r)} vs PO {money(pl['unit_price'])} - impact {money(diff * q)}"))
            row["checks"].append("price within tolerance")
        else:
            status = "price variance"
            out.append(F("price_variance", "hold", f"Line {i + 1}: price {diff / pl['unit_price'] * 100:.1f}% above PO",
                         f"{money(r)} vs PO {money(pl['unit_price'])}; excess {money(diff * q)} (tolerance {pol['price_tolerance_pct']}%).",
                         BUYER, "Approve the price change or ask the vendor for a credit note.",
                         vendor_ask=f"Line '{it.get('description')}' is billed at {money(r)}; our PO rate is {money(pl['unit_price'])}. Please issue a credit note for the difference or confirm the revised rate with our buyer."))
        # tax rate
        if it.get("tax_rate") is not None and abs(it["tax_rate"] - pl["gst_rate"]) > 0.01:
            status = "tax rate"
            out.append(F("tax_rate_mismatch", "reject", f"Line {i + 1}: GST {it['tax_rate']:g}% charged, PO says {pl['gst_rate']}%",
                         f"HSN {it.get('hsn')}.", TAX, "Return for correction.",
                         vendor_ask=f"Line '{it.get('description')}' carries GST at {it['tax_rate']:g}%; it should be {pl['gst_rate']}%. Please re-issue."))
        # quantity vs ordered and received
        pay_q = q
        if q > po_left + 1e-9:
            status = "over PO"
            pay_q = min(q, avail)
            out.append(F("over_po_qty", "hold", f"Line {i + 1}: billing {q:g} but only {po_left:g} left on the PO",
                         f"Ordered {pl['qty']:g}, already billed {prev:g}.", BUYER,
                         "Amend the PO or ask the vendor to revise the invoice."))
        elif q > avail + 1e-9:
            status = "qty > received"
            pay_q = avail
            out.append(F("qty_exceeds_receipt", "hold", f"Line {i + 1}: billed {q:g}, received {got:g}" + (f" (already billed {prev:g})" if prev else ""),
                         f"'{pl['description']}' - {q - avail:g} unit(s) billed but not received. "
                         f"Approvable now: {avail:g} x {money(r)} = {money(avail * r)} + GST.",
                         RECEIVING, f"Confirm whether the remaining {q - avail:g} unit(s) have arrived. If not, pay for {avail:g} now and the rest on receipt.",
                         vendor_ask=f"Our goods receipt shows {got:g} of {q:g} '{pl['description']}' received. We can pay for the {avail:g} received now; "
                                    f"please send a revised invoice (or a credit note for {q - avail:g}) and bill the balance once delivered.",
                         data={"po_line": ln, "billed": q, "received": got, "approvable_qty": avail}))
        else:
            row["checks"].append(f"qty {q:g} <= received {got:g}")
        row["status"] = status
        row["payable_qty"] = pay_q
        line_pay = pay_q * min(r, pl["unit_price"] + tol)
        row["payable"] = round(line_pay * (1 + pl["gst_rate"] / 100), 2)
        payable += row["payable"]
        rows.append(row)
    if not any(x["severity"] in ("hold", "reject") for x in out):
        out.append(F("three_way_ok", "pass", f"3-way match passed against {p['po_number']} and {', '.join(g['grn_number'] for g in grns)}",
                     f"{len(rows)} line(s): price, quantity received and tax rate all within policy"))
    return out, rows, round(payable, 2)
