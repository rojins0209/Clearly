"""Generate realistic, deliberately inconsistent vendor invoices for the demo.

Each vendor gets its own layout, label vocabulary and number formatting, the way
real AP inboxes look. One invoice is rasterised into an image-only "scan".

Run:  python scripts/generate_samples.py
"""
import io
import random
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"
OUT.mkdir(exist_ok=True)
W, H = A4

BUYER = ("Northwind Retail Pvt Ltd", "No. 14, Outer Ring Road, Bellandur", "Bengaluru, Karnataka 560103",
         "29AABCN1234F1ZS")

# ---------------------------------------------------------------- formatting

def inr(x: float) -> str:
    """Indian digit grouping: 2,82,492.00"""
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + whole + "." + frac


def western(x: float) -> str:
    return f"{x:,.2f}"


ONES = "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen".split()
TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _words(n: int) -> str:
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " Hundred" + ("" if n % 100 == 0 else " " + _words(n % 100))
    for div, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        if n >= div:
            return _words(n // div) + f" {name}" + ("" if n % div == 0 else " " + _words(n % div))
    return ""


def amount_in_words(x: float) -> str:
    rupees = int(round(x * 100)) // 100
    paise = int(round(x * 100)) % 100
    s = "Rupees " + _words(rupees)
    if paise:
        s += f" and {_words(paise)} Paise"
    return s + " Only"


# ---------------------------------------------------------------- invoice model

def build(lines, tax_mode, tax_rate=None):
    """lines: (desc, hsn, qty, unit, rate[, line_tax_rate]). tax_mode: 'CGST+SGST' | 'IGST'."""
    rows, sub, tax_by_rate = [], 0.0, {}
    for ln in lines:
        desc, hsn, qty, unit, rate = ln[:5]
        r = ln[5] if len(ln) > 5 else tax_rate
        amt = round(qty * rate, 2)
        rows.append(dict(desc=desc, hsn=hsn, qty=qty, unit=unit, rate=rate, amount=amt, tax_rate=r))
        sub += amt
        tax_by_rate[r] = tax_by_rate.get(r, 0) + amt
    sub = round(sub, 2)
    taxes = []
    for r, base in sorted(tax_by_rate.items()):
        t = round(base * r / 100, 2)
        if tax_mode == "IGST":
            taxes.append((f"IGST @ {r}%", t))
        else:
            taxes.append((f"CGST @ {r/2:g}%", round(t / 2, 2)))
            taxes.append((f"SGST @ {r/2:g}%", round(t / 2, 2)))
    total = round(sub + sum(t for _, t in taxes), 2)
    return rows, sub, taxes, total


# ---------------------------------------------------------------- layouts

def layout_classic(c, inv, fmt=inr):
    """Bordered table, centred title, vendor block top-left. Typical Tally-style print."""
    c.setFont("Helvetica-Bold", 15)
    c.drawCentredString(W / 2, H - 22 * mm, inv.get("title", "TAX INVOICE"))
    c.setFont("Helvetica-Bold", 11)
    y = H - 34 * mm
    c.drawString(18 * mm, y, inv["vendor"])
    c.setFont("Helvetica", 9)
    for line in inv["vendor_addr"]:
        y -= 4.5 * mm
        c.drawString(18 * mm, y, line)
    y -= 4.5 * mm
    c.drawString(18 * mm, y, f"GSTIN/UIN: {inv['vendor_gstin']}")
    y -= 4.5 * mm
    c.drawString(18 * mm, y, f"State Name: {inv['vendor_state']}")

    # meta box right
    mx, my = 120 * mm, H - 34 * mm
    meta = [("Invoice No.", inv["number"]), ("Dated", inv["date_str"])]
    if inv.get("po"):
        meta.append(("Buyer's Order No.", inv["po"]))
    if inv.get("po_ref_text"):
        meta.append(("Reference", inv["po_ref_text"]))
    meta.append(("Terms of Payment", inv.get("terms", "30 days")))
    for k, v in meta:
        c.setFont("Helvetica", 8)
        c.drawString(mx, my, k)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(mx + 32 * mm, my, str(v))
        my -= 5 * mm

    y -= 10 * mm
    c.setFont("Helvetica", 8)
    c.drawString(18 * mm, y, "Buyer (Bill to)")
    c.setFont("Helvetica-Bold", 10)
    y -= 4.5 * mm
    c.drawString(18 * mm, y, BUYER[0])
    c.setFont("Helvetica", 9)
    for line in BUYER[1:3]:
        y -= 4.5 * mm
        c.drawString(18 * mm, y, line)
    y -= 4.5 * mm
    c.drawString(18 * mm, y, f"GSTIN/UIN: {BUYER[3]}")
    y -= 4.5 * mm
    c.drawString(18 * mm, y, f"Place of Supply: {inv['pos']}")

    y -= 9 * mm
    cols = [18, 28, 98, 118, 140, 162, 192]
    heads = ["Sl", "Description of Goods", "HSN/SAC", "Quantity", "Rate", "Amount"]
    c.setLineWidth(0.6)
    c.rect(18 * mm, y - 2 * mm, 174 * mm, 7 * mm)
    c.setFont("Helvetica-Bold", 8.5)
    for i, h in enumerate(heads):
        c.drawString((cols[i] + 1.5) * mm, y, h)
    y -= 8 * mm
    c.setFont("Helvetica", 9)
    for i, r in enumerate(inv["rows"], 1):
        c.drawString(19.5 * mm, y, str(i))
        c.drawString(29.5 * mm, y, r["desc"])
        c.drawString(99.5 * mm, y, r["hsn"])
        c.drawString(119.5 * mm, y, f"{r['qty']:g} {r['unit']}")
        c.drawRightString(160 * mm, y, fmt(r["rate"]))
        c.drawRightString(190 * mm, y, fmt(r["amount"]))
        y -= 6 * mm
    c.line(18 * mm, y + 2 * mm, 192 * mm, y + 2 * mm)
    y -= 4 * mm
    totals = [("Taxable Value", inv["sub"])] + inv["taxes"] + [("Total", inv["total"])]
    for k, v in totals:
        c.setFont("Helvetica-Bold" if k == "Total" else "Helvetica", 9.5 if k == "Total" else 9)
        c.drawRightString(160 * mm, y, k)
        c.drawRightString(190 * mm, y, ("INR " if k == "Total" else "") + fmt(v))
        y -= 5.5 * mm
    y -= 2 * mm
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(18 * mm, y, "Amount Chargeable (in words): " + amount_in_words(inv["total"]))
    _bank_block(c, inv, y - 12 * mm)
    _stamp(c, inv)


def layout_modern(c, inv, fmt=western):
    """Coloured header band, meta on the right, zebra table, totals card."""
    accent = colors.HexColor(inv.get("accent", "#1f4e79"))
    c.setFillColor(accent)
    c.rect(0, H - 38 * mm, W, 38 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(16 * mm, H - 17 * mm, inv["vendor"])
    c.setFont("Helvetica", 8.5)
    c.drawString(16 * mm, H - 23 * mm, " | ".join(inv["vendor_addr"]))
    c.drawString(16 * mm, H - 28 * mm, f"GSTIN {inv['vendor_gstin']}  ·  {inv['vendor_state']}")
    c.setFont("Helvetica-Bold", 20)
    c.drawRightString(W - 16 * mm, H - 17 * mm, "INVOICE")
    c.setFillColor(colors.black)

    y = H - 50 * mm
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(colors.HexColor("#666666"))
    c.drawString(16 * mm, y, "BILLED TO")
    c.drawString(120 * mm, y, "INVOICE DETAILS")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    by = y
    for line in [BUYER[0], BUYER[1], BUYER[2], f"GSTIN {BUYER[3]}", f"Place of supply: {inv['pos']}"]:
        by -= 5 * mm
        c.drawString(16 * mm, by, line)
    my = y
    meta = [("Invoice #", inv["number"]), ("Invoice date", inv["date_str"]), ("Due date", inv.get("due", ""))]
    if inv.get("po"):
        meta.append(("Your PO", inv["po"]))
    for k, v in meta:
        my -= 5 * mm
        c.setFont("Helvetica", 9)
        c.drawString(120 * mm, my, k)
        c.setFont("Helvetica-Bold", 9)
        c.drawRightString(W - 16 * mm, my, v)

    y = by - 12 * mm
    c.setFillColor(colors.HexColor("#eef2f7"))
    c.rect(16 * mm, y - 2.5 * mm, W - 32 * mm, 8 * mm, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(18 * mm, y, "Item")
    c.drawString(98 * mm, y, "SAC/HSN")
    c.drawRightString(132 * mm, y, "Qty")
    c.drawRightString(152 * mm, y, "Unit price")
    c.drawRightString(166 * mm, y, "GST")
    c.drawRightString(W - 18 * mm, y, "Line total")
    y -= 8 * mm
    c.setFont("Helvetica", 9)
    for r in inv["rows"]:
        c.drawString(18 * mm, y, r["desc"])
        c.drawString(98 * mm, y, r["hsn"])
        c.drawRightString(132 * mm, y, f"{r['qty']:g}")
        c.drawRightString(152 * mm, y, fmt(r["rate"]))
        c.drawRightString(166 * mm, y, f"{r['tax_rate']:g}%")
        c.drawRightString(W - 18 * mm, y, fmt(r["amount"]))
        y -= 7 * mm
    y -= 4 * mm
    c.setStrokeColor(colors.HexColor("#cccccc"))
    c.line(110 * mm, y + 3 * mm, W - 16 * mm, y + 3 * mm)
    for k, v in [("Subtotal", inv["sub"])] + inv["taxes"] + [("Amount due (INR)", inv["total"])]:
        bold = k.startswith("Amount due")
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10 if bold else 9)
        c.drawString(112 * mm, y, k)
        c.drawRightString(W - 18 * mm, y, fmt(v))
        y -= 6 * mm
    if inv.get("notice"):
        y -= 4 * mm
        c.setFillColor(colors.HexColor("#fff4e5"))
        c.rect(16 * mm, y - 9 * mm, W - 32 * mm, 13 * mm, fill=1, stroke=0)
        c.setFillColor(colors.HexColor("#8a4b00"))
        c.setFont("Helvetica-Bold", 9)
        c.drawString(19 * mm, y, "IMPORTANT")
        c.setFont("Helvetica", 8.5)
        c.drawString(19 * mm, y - 5 * mm, inv["notice"])
        c.setFillColor(colors.black)
        y -= 14 * mm
    _bank_block(c, inv, y - 6 * mm, label="Remit to")
    _stamp(c, inv)


def layout_plain(c, inv, fmt=inr):
    """Courier, no boxes, labels on the left. Small-business word-processor invoice."""
    c.setFont("Courier-Bold", 13)
    y = H - 20 * mm
    c.drawString(18 * mm, y, inv["vendor"].upper())
    c.setFont("Courier", 9)
    for line in inv["vendor_addr"] + [f"GST No: {inv['vendor_gstin']}", f"State: {inv['vendor_state']}"]:
        y -= 4.5 * mm
        c.drawString(18 * mm, y, line)
    y -= 9 * mm
    c.setFont("Courier-Bold", 11)
    c.drawString(18 * mm, y, inv.get("title", "INVOICE"))
    c.setFont("Courier", 9)
    y -= 7 * mm
    fields = [("Bill No", inv["number"]), ("Bill Date", inv["date_str"])]
    if inv.get("po"):
        fields.append(("P.O. Ref", inv["po"]))
    if inv.get("po_ref_text"):
        fields.append(("Ref", inv["po_ref_text"]))
    fields += [("To", BUYER[0]), ("", BUYER[1] + ", " + BUYER[2]), ("Customer GSTIN", BUYER[3]),
               ("Place of Supply", inv["pos"])]
    for k, v in fields:
        c.drawString(18 * mm, y, f"{k:<16}{': ' if k else '  '}{v}")
        y -= 4.8 * mm
    y -= 4 * mm
    c.drawString(18 * mm, y, "-" * 92)
    y -= 4.5 * mm
    c.drawString(18 * mm, y, f"{'Particulars':<38}{'HSN':<8}{'Qty':>8}{'Rate':>14}{'Amount':>16}")
    y -= 3.5 * mm
    c.drawString(18 * mm, y, "-" * 92)
    for r in inv["rows"]:
        y -= 5 * mm
        c.drawString(18 * mm, y, f"{r['desc'][:37]:<38}{r['hsn']:<8}{r['qty']:>8g}{fmt(r['rate']):>14}{fmt(r['amount']):>16}")
    y -= 3.5 * mm
    c.drawString(18 * mm, y, "-" * 92)
    for k, v in [("Taxable Amount", inv["sub"])] + inv["taxes"] + [("Grand Total", inv["total"])]:
        y -= 5 * mm
        c.setFont("Courier-Bold" if k == "Grand Total" else "Courier", 9)
        c.drawString(18 * mm, y, f"{k:>68}{fmt(v):>16}")
    c.setFont("Courier", 8.5)
    y -= 8 * mm
    c.drawString(18 * mm, y, amount_in_words(inv["total"]))
    _bank_block(c, inv, y - 10 * mm, font="Courier")
    _stamp(c, inv)


def _bank_block(c, inv, y, label="Bank Details", font="Helvetica"):
    b = inv["bank"]
    c.setFont(font + ("-Bold" if font == "Helvetica" else "-Bold"), 9)
    c.drawString(18 * mm, y, label)
    c.setFont(font, 9)
    for line in [f"A/c Name: {b[0]}", f"A/c No: {b[1]}", f"IFSC: {b[2]}", f"Bank: {b[3]}"]:
        y -= 4.5 * mm
        c.drawString(18 * mm, y, line)
    c.setFont(font, 8)
    c.drawRightString(W - 18 * mm, y, f"for {inv['vendor']}")
    c.drawRightString(W - 18 * mm, y - 10 * mm, "Authorised Signatory")
    c.drawString(18 * mm, 14 * mm, "This is a computer generated invoice. Subject to Bengaluru jurisdiction.")


def _stamp(c, inv):
    if not inv.get("stamp"):
        return
    c.saveState()
    c.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.35))
    c.setFont("Helvetica-Bold", 34)
    c.translate(W / 2, H / 2)
    c.rotate(28)
    c.drawCentredString(0, 0, inv["stamp"])
    c.restoreState()


# ---------------------------------------------------------------- scenarios

def scenarios():
    S = []

    rows, sub, taxes, total = build([
        ("Corrugated box 3-ply 18x12x10 in", "4819", 2000, "pcs", 18.50),
        ("BOPP packaging tape 48mm x 65m", "3919", 500, "rolls", 42.00)], "CGST+SGST", 18)
    lakshmi = dict(vendor="Sri Lakshmi Packaging Industries",
                   vendor_addr=["Plot 22, KIADB Industrial Area, Bommasandra", "Bengaluru, Karnataka 560099"],
                   vendor_gstin="29AAKFS4521M1ZP", vendor_state="Karnataka, Code: 29",
                   date_str="16-Oct-2026", po="PO-4501", pos="29-Karnataka",
                   rows=rows, sub=sub, taxes=taxes, total=total,
                   bank=("Sri Lakshmi Packaging Industries", "50200061234871", "HDFC0001753", "HDFC Bank, Bommasandra"))
    S.append(("01_happy_sri_lakshmi.pdf", layout_classic, dict(lakshmi, number="INV-2026-0417")))

    rows, sub, taxes, total = build([
        ("Ergonomic mesh office chair", "9401", 25, "nos", 6450.00),
        ("A4 copier paper 75 GSM (500 sheets)", "4802", 200, "reams", 249.00)], "IGST", 18)
    S.append(("02_happy_deccan_office.pdf", layout_modern, dict(
        vendor="Deccan Office Supplies Pvt Ltd", vendor_addr=["8-2-293 Road No. 14, Banjara Hills", "Hyderabad, Telangana 500034"],
        vendor_gstin="36AADCD7788K1ZZ", vendor_state="Telangana (36)", number="DOS/26-27/1182",
        date_str="19/10/2026", due="18/11/2026", po="PO-4502", pos="Karnataka (29)",
        rows=rows, sub=sub, taxes=taxes, total=total,
        bank=("Deccan Office Supplies Pvt Ltd", "917020045531902", "UTIB0000553", "Axis Bank, Banjara Hills"))))

    rows, sub, taxes, total = build([
        ("LED panel light 2x2 ft 40W", "9405", 120, "nos", 1450.00),
        ("FR PVC wire 2.5 sq mm (90 m coil)", "8544", 30, "coils", 2180.00)], "CGST+SGST", 18)
    S.append(("03_edge_partial_delivery_kaveri.pdf", layout_plain, dict(
        vendor="Kaveri Electricals & Lighting", vendor_addr=["No. 7, SP Road, Chickpet", "Bengaluru 560002"],
        vendor_gstin="29AACFK3310Q1Z4", vendor_state="Karnataka", number="KEL/1043", title="TAX INVOICE",
        date_str="22.10.2026", po="PO-4503", pos="Karnataka",
        rows=rows, sub=sub, taxes=taxes, total=total,
        bank=("Kaveri Electricals and Lighting", "38291044576", "SBIN0040221", "SBI Chickpet"))))

    rows, sub, taxes, total = build([
        ("FTL freight Kochi to Bengaluru DC (32 ft)", "996511", 6, "trips", 38500.00, 12),
        ("Loading and unloading charges", "996719", 6, "trips", 2500.00, 18)], "IGST")
    S.append(("04_edge_bank_change_malabar.pdf", layout_modern, dict(
        vendor="Malabar Logistics Solutions Pvt Ltd", vendor_addr=["Door 41/2210, MG Road, Ernakulam", "Kochi, Kerala 682016"],
        vendor_gstin="32AAGCM9045R1ZV", vendor_state="Kerala (32)", number="MLS/INV/2026/0098",
        date_str="29/10/2026", due="13/11/2026", po="PO-4504", pos="Karnataka (29)", accent="#0b6e4f",
        rows=rows, sub=sub, taxes=taxes, total=total,
        notice="We have changed our bankers. Kindly remit all payments to the new account below w.e.f. 25-Oct-2026.",
        bank=("Malabar Logistics Solutions", "7781200045671", "ICIC0007781", "ICICI Bank, Vyttila"))))

    # Intra-state supply (KA -> KA) but vendor charged IGST: legally wrong tax head.
    rows, sub, taxes, total = build([
        ("Floor cleaner concentrate 5 L", "3402", 60, "cans", 640.00),
        ("Liquid hand wash refill 5 L", "3401", 40, "cans", 520.00)], "IGST", 18)
    S.append(("05_edge_wrong_gst_coastal.pdf", layout_classic, dict(
        vendor="Coastal Cleaning Chemicals", vendor_addr=["14/3 Peenya 2nd Stage", "Bengaluru, Karnataka 560058"],
        vendor_gstin="29AAHFC2210L1ZK", vendor_state="Karnataka, Code: 29", number="CCC-2026-311",
        date_str="23-Oct-2026", po="PO-4505", pos="29-Karnataka", terms="Immediate",
        rows=rows, sub=sub, taxes=taxes, total=total,
        bank=("Coastal Cleaning Chemicals", "1234500987612", "CNRB0002651", "Canara Bank, Peenya"))))

    # Scanned, no PO number, only an indirect reference to the order date.
    rows, sub, taxes, total = build([
        ("Spiral notebook A5 160 pages", "4820", 300, "nos", 65.00),
        ("Ball pen blue (box of 50)", "9608", 20, "boxes", 410.00),
        ("Whiteboard marker assorted (pack of 10)", "9608", 40, "packs", 230.00)], "IGST", 18)
    S.append(("06_edge_scanned_no_po_nilgiri.pdf", layout_plain, dict(
        vendor="Nilgiri Stationers", vendor_addr=["112 Oppanakara Street", "Coimbatore, Tamil Nadu 641001"],
        vendor_gstin="33AAJFN6620P1ZW", vendor_state="Tamil Nadu (33)", number="NS/0457", title="CASH / CREDIT BILL",
        date_str="25/10/2026", po=None, po_ref_text="Your order dt. 14/10/2026 (Mr. Arjun)", pos="Karnataka (29)",
        rows=rows, sub=sub, taxes=taxes, total=total, scan=True,
        bank=("Nilgiri Stationers", "6123450098", "IOBA0001290", "Indian Overseas Bank, Town Hall"))))

    # Same invoice re-sent from a different billing tool: number re-formatted, "REMINDER" stamp.
    S.append(("07_duplicate_resend_sri_lakshmi.pdf", layout_classic,
              dict(lakshmi, number="INV/2026/417", date_str="16-Oct-2026", stamp="PAYMENT REMINDER")))
    return S


def render(layout, inv) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Invoice {inv['number']}")
    layout(c, inv)
    c.showPage()
    c.save()
    return buf.getvalue()


def to_scan(pdf_bytes: bytes) -> bytes:
    """Rasterise to a noisy, slightly rotated greyscale image-only PDF (no text layer)."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter
    rnd = random.Random(7)
    page = pdfium.PdfDocument(pdf_bytes)[0]
    img = page.render(scale=150 / 72).to_pil().convert("L")
    img = img.rotate(0.8, expand=True, fillcolor=245, resample=Image.BICUBIC)
    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * 0.004)):
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = rnd.choice((60, 120, 200))
    img = img.filter(ImageFilter.GaussianBlur(0.6)).point(lambda v: min(255, int(v * 0.95 + 12)))
    out = io.BytesIO()
    img.save(out, "PDF", resolution=150)
    return out.getvalue()


def main():
    for fname, layout, inv in scenarios():
        data = render(layout, inv)
        if inv.get("scan"):
            data = to_scan(data)
        (OUT / fname).write_bytes(data)
        print(f"{fname:42s} total={inv['total']:>12,.2f}")


if __name__ == "__main__":
    main()
