"""Decision policy on synthetic extractions (no PDFs): the judgment calls, isolated."""
import copy

from app.decide import decide
from app.stages import checks
from app.stages.confidence import arithmetic

BASE = {
    "invoice_number": "INV-9", "invoice_date": "20-Oct-2026", "vendor_name": "Sri Lakshmi Packaging Industries",
    "vendor_gstin": "29AAKFS4521M1ZP", "buyer_gstin": "29AABCN1234F1ZS", "place_of_supply": "29-Karnataka",
    "currency": "INR", "subtotal": 1000.0, "cgst": 90.0, "sgst": 90.0, "igst": None, "total": 1180.0,
    "bank_account_number": "50200061234871", "bank_ifsc": "HDFC0001753", "notes": None,
    "line_items": [{"description": "x", "quantity": 10, "unit_price": 100.0, "amount": 1000.0, "tax_rate": 18}],
}


def conf_for(f, low=()):
    fields = {k: {"band": "high", "score": 1.0, "reasons": []} for k in
              ["invoice_number", "invoice_date", "vendor_gstin", "buyer_gstin", "total", "line_items"]}
    for k in low:
        fields[k] = {"band": "low", "score": 0.4, "reasons": []}
    return {"fields": fields, "low_fields": list(low), "arithmetic": arithmetic(f), "overall": 1.0}


def test_invalid_gstin_rejects_when_reading_is_trusted():
    f = dict(BASE, vendor_gstin="29AAKFS4521M1ZQ")
    findings, _ = checks.vendor_and_tax(f, conf_for(f))
    assert decide(findings, conf_for(f), f, None, None, None)["decision"] == "REJECT"


def test_invalid_gstin_only_holds_when_reading_is_low_confidence():
    """Is the document wrong, or did we misread it? If unsure, a human looks - we don't bounce the vendor."""
    f = dict(BASE, vendor_gstin="29AAKFS4521M1ZQ")
    c = conf_for(f, low=["vendor_gstin"])
    findings, _ = checks.vendor_and_tax(f, c)
    d = decide(findings, c, f, None, None, None)
    assert d["decision"] == "HOLD" and d["vendor_message"] is None


def test_wrong_buyer_gstin_rejects():
    f = dict(BASE, buyer_gstin="29AAKFS4521M1ZP")
    findings, _ = checks.vendor_and_tax(f, conf_for(f))
    assert any(x["code"] == "buyer_gstin_wrong" for x in findings)


def test_unequal_cgst_sgst_rejects():
    f = dict(BASE, cgst=100.0, sgst=80.0)
    findings, _ = checks.vendor_and_tax(f, conf_for(f))
    assert any(x["code"] == "cgst_sgst_unequal" and x["severity"] == "reject" for x in findings)


def test_totals_that_dont_add_up_hold():
    f = dict(BASE, total=1280.0)
    findings = checks.validate_document(f, conf_for(f))
    assert any(x["code"] == "arithmetic" and x["severity"] == "hold" for x in findings)


def test_foreign_currency_holds():
    f = dict(BASE, currency="USD")
    assert any(x["code"] == "currency" for x in checks.validate_document(f, conf_for(f)))


def test_future_and_stale_dates_hold():
    from datetime import date
    for d, code in (("20-Dec-2026", "future_date"), ("01-Jan-2026", "stale_invoice")):
        f = dict(BASE, invoice_date=d)
        assert any(x["code"] == code for x in checks.validate_document(f, conf_for(f), today=date(2026, 11, 10)))


def test_approval_limit_holds_clean_large_invoice():
    f = dict(BASE, total=900000.0)
    d = decide([], conf_for(f), f, None, None, None)
    assert d["decision"] == "HOLD" and d["routing"][0]["owner"] == "Finance controller"


def test_unknown_vendor_holds_for_procurement():
    f = dict(BASE, vendor_gstin="07AABCU9603R1ZM", vendor_name="Totally New Traders")
    findings, vendor = checks.vendor_and_tax(f, conf_for(f))
    assert vendor is None and any(x["code"] == "vendor_unknown" for x in findings)
