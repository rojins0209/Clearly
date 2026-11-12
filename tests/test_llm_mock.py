"""The Claude path, with the API mocked: checks request shape and response parsing."""
from types import SimpleNamespace
from pathlib import Path

from app import config
from app.stages import extract as ex
from app.stages.ingest import ingest

S = Path(__file__).resolve().parent.parent / "samples"


class FakeMessages:
    def __init__(self, payload, fail=False):
        self.payload, self.fail, self.kwargs = payload, fail, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.fail:
            raise RuntimeError("overloaded")
        block = SimpleNamespace(type="tool_use", input=self.payload)
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=1500, output_tokens=400))


def patch(monkeypatch, payload, fail=False):
    msgs = FakeMessages(payload, fail)
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=msgs))
    monkeypatch.setattr(config, "EXTRACTOR", "llm")
    return msgs


def test_llm_payload_is_parsed(monkeypatch):
    msgs = patch(monkeypatch, {
        "invoice_number": "INV-2026-0417", "total": "68,440.00", "cgst": 5220, "sgst": 5220,
        "line_items": [{"description": "Box", "quantity": "2000", "unit_price": "18.50", "amount": "37,000.00", "tax_rate": 18}],
        "evidence": {"total": "Total INR 68,440.00"}, "confidence": {"total": 0.98}})
    p = str(S / "01_happy_sri_lakshmi.pdf")
    out = ex.extract(p, ingest(p))
    assert out["extractor"].startswith("llm:")
    assert out["fields"]["total"] == 68440.0 and out["fields"]["line_items"][0]["quantity"] == 2000.0
    doc = msgs.kwargs["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf"
    assert msgs.kwargs["tool_choice"]["name"] == "record_invoice"


def test_llm_failure_falls_back_to_rules(monkeypatch):
    patch(monkeypatch, {}, fail=True)
    p = str(S / "01_happy_sri_lakshmi.pdf")
    out = ex.extract(p, ingest(p))
    assert out["extractor"] == "rules (LLM failed)" and out["warnings"]
    assert out["fields"]["invoice_number"] == "INV-2026-0417"


def test_llm_low_self_confidence_caps_score(monkeypatch):
    from app.stages.confidence import assess
    patch(monkeypatch, {"invoice_number": "INV-2026-0417", "total": 68440, "subtotal": 58000, "cgst": 5220, "sgst": 5220,
                        "line_items": [{"description": "a", "quantity": 2000, "unit_price": 18.5, "amount": 37000},
                                       {"description": "b", "quantity": 500, "unit_price": 42, "amount": 21000}],
                        "evidence": {}, "confidence": {"invoice_number": 0.4}})
    p = str(S / "01_happy_sri_lakshmi.pdf")
    ing = ingest(p)
    c = assess(ex.extract(p, ing), ing)
    assert c["fields"]["invoice_number"]["band"] == "low" and "invoice_number" in c["low_fields"]
