"""Procurement-side reference data: buyer entity, vendor master, POs, GRNs, policy."""
import json
from functools import lru_cache

from .config import DATA_DIR
from .gst import normalize_gstin


@lru_cache
def ref() -> dict:
    return json.loads((DATA_DIR / "reference.json").read_text())


def policy() -> dict:
    return ref()["policy"]


def buyer() -> dict:
    return ref()["buyer"]


def vendor_by_gstin(gstin: str | None):
    g = normalize_gstin(gstin)
    return next((v for v in ref()["vendors"] if v["gstin"] == g), None) if g else None


def vendor_by_id(vid: str):
    return next((v for v in ref()["vendors"] if v["vendor_id"] == vid), None)


def po(po_number: str | None):
    return next((p for p in ref()["purchase_orders"] if p["po_number"] == po_number), None)


def pos_for_vendor(vid: str, open_only=True):
    return [p for p in ref()["purchase_orders"] if p["vendor_id"] == vid and (p["status"] == "open" or not open_only)]


def grns_for_po(po_number: str):
    return [g for g in ref()["goods_receipts"] if g["po_number"] == po_number]


def received_qty(po_number: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for g in grns_for_po(po_number):
        for ln in g["lines"]:
            out[ln["line"]] = out.get(ln["line"], 0) + ln["qty_received"]
    return out
