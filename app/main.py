"""HTTP API + static UI.  Run:  uvicorn app.main:app --reload"""
import asyncio
import hashlib
import json
import re
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, pipeline
from .reference import po as get_po, received_qty, ref
from .states import RunState, TransitionError, claim_resolution, describe, machine, recover_interrupted, state_of
from .util import now_iso

app = FastAPI(title="Clearly - AP invoice agent")
db.init()
recover_interrupted()


@app.exception_handler(TransitionError)
def _transition_error(request: Request, e: TransitionError):
    return JSONResponse(status_code=409, content={"detail": str(e), "state": e.state.value, "action": e.action})


@app.get("/api/states")
def states():
    """The run lifecycle: states, what each means, and which action moves a run where."""
    return machine()

SAMPLES = {
    "01_happy_sri_lakshmi.pdf": ("Happy path", "Clean digital invoice, PO quoted, intra-state CGST+SGST"),
    "02_happy_deccan_office.pdf": ("Happy path", "Inter-state IGST, different layout, price 0.2% over PO (within tolerance)"),
    "03_edge_partial_delivery_kaveri.pdf": ("Edge case 1", "Bills 120 LED panels, warehouse received 80"),
    "04_edge_bank_change_malabar.pdf": ("Edge case 2", "Everything matches, but bank details changed on the invoice"),
    "05_edge_wrong_gst_coastal.pdf": ("Edge case 3", "Karnataka vendor to Karnataka buyer, charged IGST"),
    "06_edge_scanned_no_po_nilgiri.pdf": ("Edge case 4", "Scanned image, no PO number, two open POs for this vendor"),
    "07_duplicate_resend_sri_lakshmi.pdf": ("Control", "Same invoice re-sent as a 'reminder' with the number reformatted"),
}


def _start_file(name: str, path: Path, overrides=None, parent=None) -> int:
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    return pipeline.start(name, str(path), h, overrides=overrides, parent=parent)


@app.get("/api/health")
def health():
    llm = config.EXTRACTOR == "llm" or (config.EXTRACTOR == "auto" and bool(config.ANTHROPIC_API_KEY))
    return {"ok": True, "extractor": f"Claude ({config.CLAUDE_MODEL})" if llm else "OCR + rules (no API key)", "llm": llm}


@app.get("/api/samples")
def samples():
    return [{"name": n, "tag": SAMPLES.get(n, ("Sample", ""))[0], "description": SAMPLES.get(n, ("", ""))[1]}
            for n in sorted(p.name for p in config.SAMPLES_DIR.glob("*.pdf"))]


@app.post("/api/runs/sample/{name}")
def run_sample(name: str):
    path = config.SAMPLES_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404, "No such sample")
    return {"run_id": _start_file(path.name, path)}


@app.post("/api/runs")
async def run_upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Please upload a PDF invoice")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "File too large (15 MB max)")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "invoice.pdf")[-80:]
    path = config.UPLOAD_DIR / f"{hashlib.sha256(data).hexdigest()[:12]}_{safe}"
    path.write_bytes(data)
    return {"run_id": _start_file(safe, path)}


@app.get("/api/runs")
def list_runs():
    rows = db.query("SELECT id, created_at, filename, status, decision, final_decision, vendor_name, invoice_number, "
                    "po_number, total, payable, extractor, duration_ms, headline, parent_run_id, superseded_by "
                    "FROM runs ORDER BY id DESC")
    for r in rows:
        r.update({k: v for k, v in describe(r).items() if k != "actions"})
    return rows


def _run(run_id):
    r = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not r:
        raise HTTPException(404, "Run not found")
    for k in ("result", "overrides", "resolution"):
        r[k] = json.loads(r[k]) if r.get(k) else None
    r.update(describe(r, r["result"]))
    return r


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    r = _run(run_id)
    r["events"] = [dict(e, detail=json.loads(e["detail"] or "{}")) for e in
                   db.query("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))]
    return r


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: int):
    r = _run(run_id)
    return FileResponse(r["file_path"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{r["filename"]}"'})


@app.get("/api/runs/{run_id}/page/{page}.png")
def run_page(run_id: int, page: int):
    """Server-rendered page image: works in every browser and in screen recordings."""
    r = _run(run_id)
    out = config.RUNTIME_DIR / "previews" / f"{r['file_hash'][:16]}_{page}.png"
    if not out.exists():
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(r["file_path"])
        if page >= len(doc):
            raise HTTPException(404, "No such page")
        out.parent.mkdir(exist_ok=True)
        doc[page].render(scale=1.7).to_pil().save(out)
    return FileResponse(out, media_type="image/png")


@app.get("/api/runs/{run_id}/events")
async def stream(run_id: int):
    _run(run_id)

    async def gen():
        last, idle = 0, 0
        while True:
            evs = db.query("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, last))
            for e in evs:
                last = e["seq"]
                e["detail"] = json.loads(e["detail"] or "{}")
                yield f"data: {json.dumps(e, default=str)}\n\n"
                if e["stage"] in ("done", "error"):
                    return
            idle = 0 if evs else idle + 1
            if idle > 1500:  # ~5 minutes with no progress
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ConfirmPO(BaseModel):
    po_number: str
    confirmed_by: str = "AP reviewer"


@app.post("/api/runs/{run_id}/confirm-po")
def confirm_po(run_id: int, body: ConfirmPO):
    r = _run(run_id)
    if r["state"] != RunState.HELD.value:
        raise TransitionError(run_id, RunState(r["state"]), "confirm_po")
    p = get_po(body.po_number.strip().upper())
    if not p:
        raise HTTPException(400, f"{body.po_number} is not a known purchase order")
    if r.get("vendor_id") and p["vendor_id"] != r["vendor_id"]:
        raise HTTPException(400, f"{p['po_number']} belongs to a different vendor ({p['vendor_id']})")
    overrides = dict(r["overrides"] or {}, po_number=p["po_number"], confirmed_by=body.confirmed_by)
    return {"run_id": pipeline.start(r["filename"], r["file_path"], r["file_hash"], overrides=overrides, parent=run_id)}


@app.post("/api/runs/{run_id}/retry")
def retry(run_id: int):
    r = _run(run_id)
    if r["state"] != RunState.FAILED.value:
        raise TransitionError(run_id, RunState(r["state"]), "retry")
    return {"run_id": pipeline.start(r["filename"], r["file_path"], r["file_hash"], overrides=r["overrides"],
                                     parent=run_id, parent_from=("failed",))}


class Resolve(BaseModel):
    decision: str
    note: str = ""
    by: str = "AP reviewer"
    amount: float | None = None   # approve only part of the invoice (e.g. the received quantity)


@app.post("/api/runs/{run_id}/resolve")
def resolve(run_id: int, body: Resolve):
    r = _run(run_id)
    if body.decision not in ("APPROVE", "REJECT"):
        raise HTTPException(400, "decision must be APPROVE or REJECT")
    if r["state"] != RunState.HELD.value:
        raise TransitionError(run_id, RunState(r["state"]), body.decision.lower())
    if not body.note.strip():
        raise HTTPException(400, "A note is required - it becomes the audit trail")
    partial = body.decision == "APPROVE" and body.amount is not None and body.amount < (r["total"] or 0) - 1
    if partial and body.amount > (r["result"]["decision"].get("approvable_now") or 0) + 0.01:
        raise HTTPException(400, "Partial amount can't exceed what the goods receipt supports")
    ok = claim_resolution(run_id, final_decision=body.decision,
                          resolution=json.dumps({"decision": body.decision, "note": body.note, "by": body.by, "at": now_iso(),
                                                 "partial": partial, "amount": body.amount if partial else None}),
                          payable=(body.amount if partial else r["total"]) if body.decision == "APPROVE" else 0)
    if not ok:  # lost a race with another click
        raise TransitionError(run_id, state_of(db.one("SELECT * FROM runs WHERE id=?", (run_id,))), body.decision.lower())
    if partial:
        # only the payable quantities count against the PO from now on
        for ln in r["result"]["match_lines"]:
            if ln.get("po_line"):
                db.execute("UPDATE billed_lines SET qty=? WHERE run_id=? AND po_line=?",
                           (ln.get("payable_qty", ln["qty"]), run_id, ln["po_line"]))
    return {"ok": True, **describe(db.one("SELECT * FROM runs WHERE id=?", (run_id,)))}


@app.get("/api/stats")
def stats():
    runs = db.query("SELECT * FROM runs WHERE status='done' AND superseded_by IS NULL")
    n = len(runs)
    by = {st: [] for st in RunState}
    for r in runs:
        by[state_of(r)].append(r)
    reasons, owners = {}, {}
    for r in by[RunState.HELD] + by[RunState.REJECTED]:
        res = json.loads(r["result"])
        for b in res["decision"]["blocking"]:
            key = re.sub(r"Line \d+: |\s*\(.*\)$", "", b["title"])
            key = re.sub(r"inferred PO-\d+", "inferred PO", key)
            key = re.sub(r"billed [\d.]+, received [\d.]+", "billed more than received", key)
            reasons[key] = reasons.get(key, 0) + 1
        for o in res["decision"]["routing"]:
            owners[o["owner"]] = owners.get(o["owner"], 0) + 1
    tot = lambda rs, k="total": round(sum(r[k] or 0 for r in rs), 2)
    auto = [r for r in by[RunState.APPROVED] if not r["parent_run_id"]]   # re-runs after human input aren't "auto"
    assisted = [r for r in by[RunState.APPROVED] if r["parent_run_id"]]
    rejected = by[RunState.REJECTED] + by[RunState.REJECTED_BY_REVIEWER]
    return {
        "processed": n, "auto_approved": len(auto), "held": len(by[RunState.HELD]), "rejected": len(rejected),
        "resolved": len(by[RunState.APPROVED_BY_REVIEWER]) + len(by[RunState.REJECTED_BY_REVIEWER]) + len(assisted),
        "auto_rate": round(len(auto) / n * 100) if n else 0,
        "value_processed": tot(runs),
        "value_approved": round(tot(by[RunState.APPROVED]) + tot(by[RunState.APPROVED_BY_REVIEWER], "payable"), 2),
        "value_held": tot(by[RunState.HELD]), "value_rejected": tot(rejected),
        "open_holds": len(by[RunState.HELD]),
        "failed": db.one("SELECT COUNT(*) c FROM runs WHERE status='error' AND superseded_by IS NULL")["c"],
        "avg_seconds": round(sum(r["duration_ms"] or 0 for r in runs) / n / 1000, 1) if n else 0,
        "reasons": sorted(reasons.items(), key=lambda x: -x[1]), "owners": sorted(owners.items(), key=lambda x: -x[1]),
    }


@app.get("/api/reference")
def reference():
    data = json.loads(json.dumps(ref()))
    for p in data["purchase_orders"]:
        recv, billed = received_qty(p["po_number"]), db.billed_qty(p["po_number"])
        for ln in p["lines"]:
            ln["received"] = recv.get(ln["line"], 0)
            ln["billed"] = billed.get(ln["line"], 0)
    return data


@app.post("/api/reset")
def reset():
    db.reset()
    return {"ok": True}


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
