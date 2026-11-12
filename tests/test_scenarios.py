"""End-to-end: every prepared invoice through the full pipeline, in demo order."""
import hashlib
import json
from pathlib import Path

from app import db, pipeline

S = Path(__file__).resolve().parent.parent / "samples"


def run(name, **kw):
    p = S / name
    rid = pipeline.start(name, str(p), hashlib.sha256(p.read_bytes()).hexdigest(), sync=True, **kw)
    r = db.one("SELECT * FROM runs WHERE id=?", (rid,))
    assert r["status"] == "done", r["headline"]
    r["result"] = json.loads(r["result"])
    r["codes"] = {f["code"]: f["severity"] for f in r["result"]["findings"]}
    return r


def test_happy_paths(fresh_db):
    a = run("01_happy_sri_lakshmi.pdf")
    assert a["decision"] == "APPROVE" and a["payable"] == 68440.0
    b = run("02_happy_deccan_office.pdf")
    assert b["decision"] == "APPROVE"
    assert b["codes"]["price_within_tol"] == "info"        # 249.00 vs 248.50 is 0.2%


def test_partial_delivery_holds_with_approvable_amount(fresh_db):
    r = run("03_edge_partial_delivery_kaveri.pdf")
    assert r["decision"] == "HOLD"
    assert r["codes"]["qty_exceeds_receipt"] == "hold"
    # 80 x 1450 + 30 x 2180 = 181,400 + 18% GST
    assert r["result"]["decision"]["approvable_now"] == 214052.0


def test_bank_change_is_held_for_treasury(fresh_db):
    r = run("04_edge_bank_change_malabar.pdf")
    assert r["decision"] == "HOLD"
    bank = next(f for f in r["result"]["findings"] if f["code"] == "bank_changed")
    assert bank["owner"] == "Treasury" and "number already on file" in bank["action"]
    assert r["codes"]["three_way_ok"] == "pass"             # numbers are fine - that's the point


def test_wrong_gst_head_rejected_with_vendor_message(fresh_db):
    r = run("05_edge_wrong_gst_coastal.pdf")
    assert r["decision"] == "REJECT"
    assert r["codes"]["wrong_tax_head"] == "reject"
    assert "credit note" in r["result"]["decision"]["vendor_message"]["body"]


def test_scanned_no_po_infers_then_human_confirms(fresh_db):
    r = run("06_edge_scanned_no_po_nilgiri.pdf")
    assert r["result"]["ingest"]["kind"] == "scanned"
    assert r["decision"] == "HOLD" and r["codes"]["po_inferred"] == "hold"
    cands = r["result"]["po_info"]["candidates"]
    assert cands[0]["po_number"] == "PO-4506" and cands[0]["score"] - cands[1]["score"] > 0.3
    r2 = run("06_edge_scanned_no_po_nilgiri.pdf", overrides={"po_number": "PO-4506", "confirmed_by": "test"}, parent=r["id"])
    assert r2["decision"] == "APPROVE"
    assert db.one("SELECT superseded_by FROM runs WHERE id=?", (r["id"],))["superseded_by"] == r2["id"]


def test_duplicate_only_after_original(fresh_db):
    first = run("07_duplicate_resend_sri_lakshmi.pdf")      # on its own it's a valid invoice
    assert first["decision"] == "APPROVE"
    db.reset()
    run("01_happy_sri_lakshmi.pdf")
    dup = run("07_duplicate_resend_sri_lakshmi.pdf")
    assert dup["decision"] == "REJECT" and dup["codes"]["dup_invoice"] == "reject"


def test_same_file_twice_is_caught(fresh_db):
    run("02_happy_deccan_office.pdf")
    again = run("02_happy_deccan_office.pdf")
    assert again["decision"] == "REJECT" and "dup_file" in again["codes"]


def test_cumulative_billing_blocks_second_invoice_for_same_goods(fresh_db):
    """Approved invoices consume PO/GRN quantity: a second invoice for the same goods can't pass."""
    run("01_happy_sri_lakshmi.pdf")
    from app.stages import checks
    # a new invoice (different number) for 500 more boxes: all 2000 ordered were already billed
    f = {"line_items": [{"description": "Corrugated box 3-ply 18x12x10 in", "hsn": "4819", "quantity": 500,
                         "unit_price": 18.5, "tax_rate": 18, "amount": 9250}]}
    from app.reference import po
    findings, rows, payable = checks.three_way(f, po("PO-4501"), run_id=-1)
    assert any(x["code"] == "over_po_qty" for x in findings)
