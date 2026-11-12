"""Stage 2 - Extract: turn the document into structured fields.

Two extractors behind one interface:
  * LLM (Claude): reads the PDF directly (works for text PDFs and scans), returns fields,
    the exact text each value was read from, and a self-reported confidence.
  * Rules (fallback): regex parser over the text layer / OCR text. Runs with no API key,
    and is what the tests use, so decision logic can be tested deterministically.

The extractor only READS. It never decides anything: every decision downstream is made
by deterministic, auditable code.
"""
import base64
import json
import re

from .. import config
from ..util import parse_amount

FIELDS = ["invoice_number", "invoice_date", "due_date", "po_reference", "other_references",
          "vendor_name", "vendor_gstin", "vendor_state", "buyer_name", "buyer_gstin", "place_of_supply",
          "currency", "subtotal", "cgst", "sgst", "igst", "total",
          "bank_account_name", "bank_account_number", "bank_ifsc", "notes"]


def extract(path: str, ingest_out: dict) -> dict:
    mode = config.EXTRACTOR
    use_llm = mode == "llm" or (mode == "auto" and config.ANTHROPIC_API_KEY)
    if use_llm:
        try:
            out = extract_llm(path)
            out["extractor"] = f"llm:{config.CLAUDE_MODEL}"
            return out
        except Exception as e:  # never let a provider hiccup kill the run - degrade and say so
            fallback = extract_rules(ingest_out.get("text", ""), ocr=ingest_out.get("kind") == "scanned")
            fallback["extractor"] = "rules (LLM failed)"
            fallback["warnings"] = [f"LLM extraction failed ({type(e).__name__}: {str(e)[:160]}); fell back to rules parser"]
            return fallback
    out = extract_rules(ingest_out.get("text", ""), ocr=ingest_out.get("kind") == "scanned")
    out["extractor"] = "ocr+rules" if ingest_out.get("kind") == "scanned" else "rules"
    return out


# ------------------------------------------------------------------ LLM extractor

_str = {"type": ["string", "null"]}
_num = {"type": ["number", "null"]}

TOOL = {
    "name": "record_invoice",
    "description": "Record the fields printed on this invoice.",
    "input_schema": {
        "type": "object",
        "properties": {
            **{f: _str for f in ["invoice_number", "invoice_date", "due_date", "po_reference", "other_references",
                                 "vendor_name", "vendor_gstin", "vendor_state", "buyer_name", "buyer_gstin",
                                 "place_of_supply", "currency", "bank_account_name", "bank_account_number",
                                 "bank_ifsc", "notes"]},
            **{f: _num for f in ["subtotal", "cgst", "sgst", "igst", "total"]},
            "line_items": {"type": "array", "items": {"type": "object", "properties": {
                "description": _str, "hsn": _str, "quantity": _num, "unit": _str,
                "unit_price": _num, "tax_rate": _num, "amount": _num}}},
            "evidence": {"type": "object", "description": "For each field you filled, the exact text on the page it came from.",
                         "additionalProperties": {"type": "string"}},
            "confidence": {"type": "object", "description": "0-1 per field: how legible/unambiguous the printed value was.",
                           "additionalProperties": {"type": "number"}},
        },
        "required": ["line_items", "evidence", "confidence"],
    },
}

PROMPT = """You are reading a vendor invoice for an accounts-payable team in India.
Record exactly what is printed. Rules:
- Do not guess or infer. If a field is not printed, use null. In particular, only fill po_reference if a
  purchase order number is explicitly printed; put any indirect reference (e.g. "your order dated ...") in other_references.
- Amounts are plain numbers (no commas, no currency symbol). cgst/sgst/igst are the TOTAL tax amounts of each type.
- Dates: copy as printed (e.g. "16-Oct-2026").
- tax_rate per line is the GST % if printed per line, otherwise the single rate that applies.
- Copy GSTIN, account number and IFSC character by character.
- notes: any printed remark that would matter to AP (e.g. bank detail changes, "duplicate", "revised").
- evidence: the exact text snippet each value came from. confidence: lower it for blurry, cut-off or ambiguous values."""


def extract_llm(path: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=90)
    pdf_b64 = base64.standard_b64encode(open(path, "rb").read()).decode()
    msg = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=4096,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "record_invoice"},
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
            {"type": "text", "text": PROMPT}]}],
    )
    block = next(b for b in msg.content if b.type == "tool_use")
    data = dict(block.input)
    fields = {f: data.get(f) for f in FIELDS}
    for f in ("subtotal", "cgst", "sgst", "igst", "total"):
        fields[f] = parse_amount(fields[f])
    items = []
    for li in data.get("line_items") or []:
        items.append({k: (parse_amount(li.get(k)) if k in ("quantity", "unit_price", "tax_rate", "amount") else li.get(k))
                      for k in ("description", "hsn", "quantity", "unit", "unit_price", "tax_rate", "amount")})
    fields["line_items"] = items
    return {"fields": fields, "evidence": data.get("evidence") or {}, "llm_confidence": data.get("confidence") or {},
            "repaired": {}, "usage": {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}}


# ------------------------------------------------------------------ rules extractor

GSTIN_TOKEN = re.compile(r"\b[0-9OI]{2}[A-Z0-9]{5}[0-9OIl]{4}[A-Z0-9][0-9A-Z][Z2][0-9A-Z]\b")
AMT = r"([\d,]+\.\d{2})"


def _ocr_fix_gstin(tok: str) -> str:
    # positions 2-6 and 11 are letters, 7-10 digits, in a GSTIN
    t = list(tok.upper())
    to_letter = {"0": "O", "1": "I", "5": "S", "8": "B"}
    to_digit = {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8"}
    for i in list(range(2, 7)) + [11]:
        t[i] = to_letter.get(t[i], t[i])
    for i in list(range(0, 2)) + list(range(7, 11)):
        t[i] = to_digit.get(t[i], t[i])
    if t[13] == "2":  # 14th character of every GSTIN is the literal 'Z'
        t[13] = "Z"
    return "".join(t)


def _ocr_fix_ifsc(tok: str) -> str:
    # IFSC = 4 letters (bank) + '0' + 6 alphanumerics (branch)
    t = list(tok.upper())
    to_letter = {"0": "O", "1": "I", "5": "S", "8": "B"}
    for i in range(4):
        t[i] = to_letter.get(t[i], t[i])
    if len(t) > 4 and t[4] in "OQD":
        t[4] = "0"
    return "".join(t)


def _find(pattern, text, flags=re.I | re.M):
    m = re.search(pattern, text, flags)
    return (m.group(1).strip(), m.group(0).strip()) if m else (None, None)


def extract_rules(text: str, ocr: bool = False) -> dict:
    f: dict = {k: None for k in FIELDS}
    ev: dict = {}
    repaired: dict = {}
    t = text.replace("·", " ")
    if ocr:  # common OCR artefacts: '+' read for ':', stray punctuation inside table rows
        t = re.sub(r"(?m)^(\s*[A-Za-z .']+?)\s[+;!]\s*:?\s", r"\1 : ", t)
        t = re.sub(r"(?m)^(.*\d\.\d{2}.*)$", lambda m: re.sub(r"\s[;:,'‘|]\s", " ", m.group(1)), t)
        t = re.sub(r"(?<=[A-Za-z0-9])\.(?=\s+\d)", "", t)

    def put(field, value, evidence):
        if value is not None and value != "":
            f[field] = value
            ev[field] = evidence

    v, e = _find(r"(?:Invoice\s*(?:No\.?|Number|#)|Bill\s*No\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]{2,})", t)
    put("invoice_number", v, e)
    v, e = _find(r"(?:Invoice\s*date|Bill\s*Date|Dated|Date)\s*[:]?\s*(\d{1,2}[\-/. ][A-Za-z0-9]{2,9}[\-/. ]\d{2,4})", t)
    put("invoice_date", v, e)
    v, e = _find(r"Due\s*date\s*[:]?\s*(\d{1,2}[\-/.][A-Za-z0-9]{2,9}[\-/.]\d{2,4})", t)
    put("due_date", v, e)
    v, e = _find(r"(?:Buyer'?s\s*Order\s*No\.?|Your\s*PO|P\.?O\.?\s*(?:Ref|No\.?|Number)?)\s*[:#]?\s*(PO[-\s]?\d{3,})", t)
    if not v:
        v, e = _find(r"\b(PO-\d{3,})\b", t)
    put("po_reference", v, e)
    v, e = _find(r"^\s*Ref(?:erence)?\.?\s*:\s*(.+)$", t)
    if v and not re.search(r"PO[-\s]?\d", v):
        put("other_references", v.rstrip(" ;,.|"), e)

    # GSTINs: vendor block is printed before the buyer block on Indian invoices
    tokens = []
    for line in t.splitlines():
        for m in GSTIN_TOKEN.finditer(re.sub(r"[^\w\s:]", " ", line.upper())):
            tok = m.group(0)
            fixed = _ocr_fix_gstin(tok) if ocr else tok
            tokens.append((fixed, line.strip(), fixed != tok))
    buyer_tok = next((x for x in tokens if re.search(r"customer|buyer|bill(ed)?\s*to", x[1], re.I)), None)
    vendor_tok = next((x for x in tokens if x is not buyer_tok), None)
    if buyer_tok is None and len(tokens) > 1:
        buyer_tok = tokens[1]
    for field, tok in (("vendor_gstin", vendor_tok), ("buyer_gstin", buyer_tok)):
        if tok:
            put(field, tok[0], tok[1])
            if tok[2]:
                repaired[field] = "OCR character confusion corrected using GSTIN format"

    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    for ln in lines[:3]:
        if re.fullmatch(r"(TAX\s+)?INVOICE", ln, re.I):
            continue
        name = re.split(r"\s+(?:Invoice\s*No|INVOICE\b)", ln)[0].strip()
        if len(name) > 3:
            put("vendor_name", name.title() if name.isupper() else name, ln)
            break
    v, e = _find(r"(?:State\s*Name|State)\s*:\s*(.+)$", t)
    put("vendor_state", v, e)
    v, e = _find(r"Place\s*of\s*Supply\s*:?\s*(.+)$", t)
    put("place_of_supply", v, e)
    for i, ln in enumerate(lines):
        m = re.match(r"^(?:To|Buyer\s*\(Bill to\)|Bill(?:ed)?\s*to)(?![A-Za-z])[\s:.?]*(.*)$", ln, re.I)
        if m:
            cand = m.group(1) if re.search(r"[a-z]{3}", m.group(1)) and "DETAILS" not in m.group(1) else lines[i + 1]
            cand = re.split(r"\s{2,}|\s+Invoice\b", cand)[0].strip(" .?:7")
            put("buyer_name", cand, ln)
            break

    f["currency"] = "USD" if re.search(r"\bUSD\b|US\$", t) else ("INR" if re.search(r"INR|Rupees|₹|Rs\.?", t) else None)
    if f["currency"]:
        ev["currency"] = f["currency"]

    # line items: description  HSN  qty [unit]  rate  [gst%]  amount
    row = re.compile(r"^(?:\d{1,2}\s+)?(?P<desc>[A-Za-z].*?)\s+(?P<hsn>\d{4,8})\s+(?P<qty>\d+(?:\.\d+)?)"
                     r"(?:\s+(?P<unit>[A-Za-z]{2,8}))?\s+(?P<rate>[\d,]+\.\d{2})(?:\s+(?P<gst>\d{1,2}(?:\.\d+)?)%)?"
                     r"\s+(?P<amt>[\d,]+\.\d{2})\b")
    items = []
    for ln in lines:
        m = row.match(ln)
        if m:
            items.append({"description": m["desc"].strip(), "hsn": m["hsn"], "quantity": float(m["qty"]),
                          "unit": m["unit"], "unit_price": parse_amount(m["rate"]),
                          "tax_rate": float(m["gst"]) if m["gst"] else None, "amount": parse_amount(m["amt"]),
                          "_evidence": ln})
    f["line_items"] = items
    if items:
        ev["line_items"] = " / ".join(i.pop("_evidence") for i in items)

    # tolerate stamp/watermark letters bleeding into labels ("TEaxable Value", "I SGST")
    v, e = _find(r"(?:T\w{0,2}axable\s*(?:Value|Amount)|Sub\s*-?total)\s*:?\s*(?:INR\s*)?" + AMT, t)
    put("subtotal", parse_amount(v), e)
    rates = {}
    for kind in ("CGST", "SGST", "IGST"):
        hits = re.findall(rf"^(?:\S\s+)?{kind}\s*@?\s*([\d.]+)\s*%\s*(?:INR\s*)?{AMT}\s*$", t, re.I | re.M)
        if hits:
            put(kind.lower(), round(sum(parse_amount(a) for _, a in hits), 2),
                "; ".join(f"{kind} @ {r}% {a}" for r, a in hits))
            rates[kind] = [float(r) for r, _ in hits]
    # a single printed rate applies to every line that didn't print its own
    single = None
    if "IGST" in rates and len(rates["IGST"]) == 1:
        single = rates["IGST"][0]
    elif "CGST" in rates and len(rates["CGST"]) == 1:
        single = rates["CGST"][0] * 2
    for it in items:
        if it["tax_rate"] is None and single is not None:
            it["tax_rate"] = single

    tot = None
    for m in re.finditer(r"^(?:Grand\s*Total|Amount\s*due(?:\s*\(INR\))?|Total(?:\s*Amount)?)\s*:?\s*(?:INR|Rs\.?|₹)?\s*" + AMT + r"\s*$",
                         t, re.I | re.M):
        tot = (m.group(1), m.group(0))
    if tot:
        put("total", parse_amount(tot[0]), tot[1])

    v, e = _find(r"A/c\.?\s*Name\s*:\s*(.+)$", t)
    put("bank_account_name", v, e)
    v, e = _find(r"A/c\.?\s*No\.?\s*:?\s*([0-9]{6,20})", t)
    put("bank_account_number", v, e)
    v, e = _find(r"IFSC\s*(?:Code)?\s*:?\s*([A-Z0-9]{11})", t)
    if v:
        fixed = _ocr_fix_ifsc(v) if ocr else v
        put("bank_ifsc", fixed, e)
        if fixed != v:
            repaired["bank_ifsc"] = f"OCR read '{v}', corrected to '{fixed}' using IFSC format (4 letters + 0 + 6)"
    notes = [ln for ln in lines if re.search(r"changed our bank|new (bank|account)|revised|duplicate|w\.e\.f", ln, re.I)]
    if notes:
        put("notes", " ".join(notes), notes[0])

    return {"fields": f, "evidence": ev, "llm_confidence": {}, "repaired": repaired}


if __name__ == "__main__":  # quick manual check: python -m app.stages.extract samples/x.pdf
    import sys
    from .ingest import ingest
    ing = ingest(sys.argv[1])
    print(json.dumps(extract(sys.argv[1], ing), indent=2, default=str))
