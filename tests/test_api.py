import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)
S = Path(__file__).resolve().parent.parent / "samples"


def wait(rid):
    for _ in range(200):
        r = c.get(f"/api/runs/{rid}").json()
        if r["status"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_upload_run_and_dashboard(fresh_db):
    with open(S / "01_happy_sri_lakshmi.pdf", "rb") as fh:
        rid = c.post("/api/runs", files={"file": ("inv.pdf", fh, "application/pdf")}).json()["run_id"]
    r = wait(rid)
    assert r["decision"] == "APPROVE"
    assert [e["stage"] for e in r["events"] if e["status"] != "running"][-1] == "done"
    s = c.get("/api/stats").json()
    assert s["processed"] == 1 and s["auto_rate"] == 100
    assert c.get(f"/api/runs/{rid}/page/0.png").status_code == 200


def test_rejects_non_pdf():
    assert c.post("/api/runs", files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 400


def test_resolve_requires_note_and_partial_approval(fresh_db):
    rid = c.post("/api/runs/sample/03_edge_partial_delivery_kaveri.pdf").json()["run_id"]
    r = wait(rid)
    assert c.post(f"/api/runs/{rid}/resolve", json={"decision": "APPROVE"}).status_code == 400
    amt = r["result"]["decision"]["approvable_now"]
    assert c.post(f"/api/runs/{rid}/resolve", json={"decision": "APPROVE", "amount": amt, "note": "pay received"}).status_code == 200
    ref = c.get("/api/reference").json()
    led = next(p for p in ref["purchase_orders"] if p["po_number"] == "PO-4503")["lines"][0]
    assert led["billed"] == 80     # only the received quantity counts against the PO
