"""Stage 3 - Confidence: how much should we trust each extracted value?

We do NOT take the model's word for it. Each critical field is scored from evidence we can check:
  * grounded   - the value literally appears in the document's own text (text layer or OCR)
  * corroborated - amounts reconcile with each other (qty x rate = amount, lines = subtotal, subtotal + tax = total)
  * source     - text layer > OCR; OCR-repaired characters cost a little
  * model      - the LLM's self-reported confidence can only LOWER the score, never raise it
This separates "the document is wrong" (grounded value that fails a rule -> act on it)
from "we may have read it wrong" (ungrounded value -> send to a human).
"""
import re

from ..gst import validate_gstin
from ..util import parse_date

CRITICAL = ["invoice_number", "invoice_date", "vendor_gstin", "buyer_gstin", "total", "line_items"]
TRACKED = CRITICAL + ["po_reference", "subtotal", "cgst", "sgst", "igst", "bank_account_number", "bank_ifsc", "place_of_supply"]


def _flat(s: str) -> str:
    return re.sub(r"[^A-Z0-9.]", "", (s or "").upper().replace(",", ""))


def _grounded(field, value, src_flat) -> bool | None:
    if not src_flat:
        return None  # nothing to verify against
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return f"{value:.2f}" in src_flat or (float(value).is_integer() and str(int(value)) in src_flat)
    return _flat(str(value)) in src_flat


def arithmetic(f: dict, tol: float = 1.0) -> dict:
    items = f.get("line_items") or []
    out = {"lines_ok": True, "line_errors": [], "sum_lines": None, "subtotal_ok": None, "total_ok": None, "tax_sum": None}
    for i, it in enumerate(items, 1):
        q, r, a = it.get("quantity"), it.get("unit_price"), it.get("amount")
        if None not in (q, r, a) and abs(q * r - a) > tol:
            out["lines_ok"] = False
            out["line_errors"].append(f"line {i}: {q:g} x {r:,.2f} = {q * r:,.2f}, printed {a:,.2f}")
    if items and all(it.get("amount") is not None for it in items):
        out["sum_lines"] = round(sum(it["amount"] for it in items), 2)
    sub = f.get("subtotal") if f.get("subtotal") is not None else out["sum_lines"]
    if f.get("subtotal") is not None and out["sum_lines"] is not None:
        out["subtotal_ok"] = abs(out["sum_lines"] - f["subtotal"]) <= tol
    taxes = [f.get(k) for k in ("cgst", "sgst", "igst") if f.get(k) is not None]
    out["tax_sum"] = round(sum(taxes), 2) if taxes else 0.0
    if sub is not None and f.get("total") is not None:
        out["total_ok"] = abs(sub + out["tax_sum"] - f["total"]) <= tol
    return out


def assess(extraction: dict, ingest_out: dict) -> dict:
    f, ev = extraction["fields"], extraction.get("evidence", {})
    llm = extraction.get("llm_confidence", {})
    repaired = extraction.get("repaired", {})
    src_flat = _flat(ingest_out.get("text", ""))
    is_ocr = ingest_out.get("kind") == "scanned"
    math = arithmetic(f)
    corroborated = math["lines_ok"] and math["subtotal_ok"] is not False and math["total_ok"] is True

    report = {}
    for field in TRACKED:
        value = f.get(field)
        reasons, score = [], 1.0
        if field == "line_items":
            present = bool(value)
            g = None if not src_flat else (all(_grounded(field, it.get("amount"), src_flat) for it in value) if present else False)
        else:
            present = value not in (None, "")
            g = _grounded(field, value, src_flat) if present else False
        if not present:
            report[field] = {"value": None, "score": 0.0, "band": "missing", "reasons": ["not found on the document"],
                             "evidence": None}
            continue
        if g is False and isinstance(value, (int, float)) and ev.get(field):
            # a total built from several printed components (e.g. IGST @12% + IGST @18%)
            parts = [float(x.replace(",", "")) for x in re.findall(r"[\d,]+\.\d{2}", str(ev[field]))]
            if len(parts) > 1 and abs(sum(parts) - value) < 0.05 and all(_grounded(field, x, src_flat) for x in parts):
                g = True
                reasons.append(f"sum of {len(parts)} printed amounts")
        if field in repaired:
            # the raw OCR token was on the page; we changed characters using format rules
            g = True if g is False else g
            score -= 0.1
            reasons.append(repaired[field])
            if field.endswith("gstin"):
                if validate_gstin(value)[0]:
                    reasons.append("repair verified: corrected GSTIN passes the mod-36 checksum")
                else:
                    score -= 0.4
                    reasons.append("repair NOT verified: checksum still fails")
        if g is True:
            reasons.append("found in document text")
        elif g is None:
            score -= 0.25
            reasons.append("no text layer/OCR to verify against")
        else:
            score -= 0.4
            reasons.append("NOT found in document text")
        if is_ocr:
            score -= 0.05
            reasons.append("read from a scan (OCR)")
        if field in ("total", "subtotal", "cgst", "sgst", "igst", "line_items"):
            if corroborated:
                score = min(1.0, score + 0.1)
                reasons.append("amounts reconcile (qty x rate, lines, tax, total)")
            else:
                score -= 0.15
                reasons.append("amounts do not reconcile")
        if field == "invoice_date" and not parse_date(value):
            score -= 0.3
            reasons.append("could not parse as a date")
        mc = llm.get(field)
        if isinstance(mc, (int, float)) and mc < score:
            score = mc
            reasons.append(f"model reported low legibility ({mc:.2f})")
        score = max(0.0, min(1.0, round(score, 2)))
        band = "high" if score >= 0.85 else "medium" if score >= 0.6 else "low"
        report[field] = {"value": value if field != "line_items" else f"{len(value)} lines", "score": score,
                         "band": band, "reasons": reasons, "evidence": ev.get(field)}

    crit = {k: report[k] for k in CRITICAL}
    low = [k for k, v in crit.items() if v["band"] in ("low", "missing")]
    overall = min(v["score"] for v in crit.values())
    return {"fields": report, "overall": overall, "low_fields": low, "arithmetic": math,
            "source": "ocr" if is_ocr else ingest_out.get("kind")}
