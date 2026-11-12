"""Runs one invoice through every stage, emitting an event per stage for the live view."""
import os
import threading
import time
import traceback
from datetime import date

from . import config, db
from .checks_order import RANK
from .decide import decide
from .stages import checks
from .stages.confidence import assess
from .stages.extract import extract
from .stages.ingest import ingest
from .util import money, norm_invoice_no, now_iso, parse_date

STAGES = [
    ("ingest", "Read document"),
    ("extract", "Extract fields"),
    ("confidence", "Verify extraction"),
    ("validate", "Validate invoice"),
    ("compliance", "Vendor & GST checks"),
    ("duplicates", "Duplicate check"),
    ("po", "Find purchase order"),
    ("match", "3-way match"),
    ("decision", "Decision"),
]


def _today() -> date:
    v = os.getenv("CLEARLY_TODAY")
    return parse_date(v) if v else date.today()


class Run:
    def __init__(self, run_id: int):
        self.id, self.seq = run_id, 0

    def emit(self, stage, status, title, detail=None):
        self.seq += 1
        db.add_event(self.id, self.seq, now_iso(), stage, status, title, detail)

    def stage(self, stage, fn, summarize):
        label = dict(STAGES)[stage]
        self.emit(stage, "running", label)
        t0 = time.perf_counter()
        result = fn()
        ms = int((time.perf_counter() - t0) * 1000)
        status, title, detail = summarize(result)
        detail = {**(detail or {}), "ms": ms}
        if config.STAGE_DELAY_MS:
            time.sleep(config.STAGE_DELAY_MS / 1000)
        self.emit(stage, status, title, detail)
        return result


def _status(findings):
    worst = max((RANK[f["severity"]] for f in findings), default=0)
    return "fail" if worst >= RANK["reject"] else "warn" if worst >= RANK["warn"] else "pass"


def _summary(findings, ok_text):
    bad = [f for f in findings if f["severity"] in ("hold", "reject")]
    if bad:
        return bad[0]["title"] + (f" (+{len(bad) - 1})" if len(bad) > 1 else "")
    info = [f for f in findings if f["severity"] in ("info", "warn")]
    return info[0]["title"] if info else ok_text


def execute(run_id: int, path: str, overrides: dict):
    run = Run(run_id)
    t_start = time.perf_counter()
    try:
        ing = run.stage("ingest", lambda: ingest(path), lambda r: (
            "fail" if r["kind"] == "unreadable" else "pass",
            {"text": f"Digital PDF, {r['pages']} page(s), text layer present",
             "scanned": f"Scanned image, {r['pages']} page(s) - no text layer" + (", OCR applied" if r.get("ocr_used") else ""),
             "unreadable": "File could not be opened as a PDF"}[r["kind"]],
            {"kind": r["kind"], "pages": r.get("pages"), "sha256": r["sha256"][:16], "ocr_error": r.get("ocr_error")}))
        if ing["kind"] == "unreadable":
            raise ValueError("Unreadable PDF: " + ing.get("error", ""))

        ex = run.stage("extract", lambda: extract(path, ing), lambda r: (
            "warn" if r.get("warnings") else "pass",
            f"{sum(1 for k, v in r['fields'].items() if v not in (None, '', []))} fields, "
            f"{len(r['fields'].get('line_items') or [])} line items via {r['extractor']}",
            {"fields": r["fields"], "extractor": r["extractor"], "warnings": r.get("warnings"), "usage": r.get("usage")}))
        f = ex["fields"]

        conf = run.stage("confidence", lambda: assess(ex, ing), lambda r: (
            "warn" if r["low_fields"] else ("warn" if any(v["band"] == "medium" for v in r["fields"].values()) else "pass"),
            ("Needs human check: " + ", ".join(r["low_fields"])) if r["low_fields"]
            else f"All critical fields verified (min score {r['overall']:.2f})",
            {"fields": r["fields"], "overall": r["overall"], "source": r["source"]}))

        v_find = run.stage("validate", lambda: checks.validate_document(f, conf, _today()),
                           lambda r: (_status(r), _summary(r, "Required fields present, dates and arithmetic OK"), {"findings": r}))

        comp, vendor = run.stage("compliance", lambda: checks.vendor_and_tax(f, conf),
                                 lambda r: (_status(r[0]), _summary(r[0], "Vendor approved, GSTINs valid, tax head correct, bank matches"),
                                            {"findings": r[0], "vendor": r[1] and {k: r[1][k] for k in ("vendor_id", "name", "gstin")}}))

        dups = run.stage("duplicates", lambda: checks.duplicates(f, vendor, ing["sha256"], run_id),
                         lambda r: (_status(r), _summary(r, "No duplicate found"), {"findings": r}))

        po_find, po, po_info = run.stage("po", lambda: checks.resolve_po(f, vendor, overrides),
                                         lambda r: (_status(r[0]), _summary(r[0], r[0][-1]["title"] if r[0] else "No PO"),
                                                    {"findings": r[0], "po": r[1] and r[1]["po_number"], "info": r[2]}))

        m_find, rows, payable = run.stage("match", lambda: checks.three_way(f, po, run_id),
                                          lambda r: (_status(r[0]), _summary(r[0], "3-way match passed"),
                                                     {"findings": r[0], "lines": r[1], "payable": r[2]}))

        findings = []
        for stage, items in (("validate", v_find), ("compliance", comp), ("duplicates", dups), ("po", po_find), ("match", m_find)):
            for x in items:
                x["stage"] = stage
                findings.append(x)

        dec = run.stage("decision", lambda: decide(findings, conf, f, vendor, payable, po), lambda r: (
            {"APPROVE": "pass", "HOLD": "warn", "REJECT": "fail"}[r["decision"]], r["headline"], {"decision": r}))

        ms = int((time.perf_counter() - t_start) * 1000)
        result = {"ingest": {k: v for k, v in ing.items() if k != "text"}, "source_text": ing.get("text", "")[:20000],
                  "extraction": ex, "confidence": conf, "findings": findings, "po": po, "po_info": po_info,
                  "match_lines": rows, "payable": payable, "decision": dec, "overrides": overrides}
        db.update_run(run_id, status="done", finished_at=now_iso(), decision=dec["decision"],
                      vendor_name=(vendor or {}).get("name") or f.get("vendor_name"), vendor_id=(vendor or {}).get("vendor_id"),
                      invoice_number=f.get("invoice_number"), invoice_number_norm=norm_invoice_no(f.get("invoice_number")),
                      invoice_date=str(parse_date(f.get("invoice_date")) or f.get("invoice_date") or ""),
                      po_number=po and po["po_number"], total=f.get("total"), payable=dec["pay_now"],
                      extractor=ex["extractor"], duration_ms=ms, headline=dec["headline"], result=result)
        for r in rows:
            if r.get("po_line"):
                db.execute("INSERT INTO billed_lines (run_id, po_number, po_line, qty, amount) VALUES (?,?,?,?,?)",
                           (run_id, po["po_number"], r["po_line"], r["qty"] or 0, r["amount"] or 0))
        run.emit("done", "pass", f"Finished in {ms / 1000:.1f}s", {"decision": dec["decision"]})
    except Exception as e:
        db.update_run(run_id, status="error", finished_at=now_iso(), headline=f"Error: {e}",
                      duration_ms=int((time.perf_counter() - t_start) * 1000))
        run.emit("error", "fail", f"Run failed: {e}", {"trace": traceback.format_exc()[-1500:]})


def start(filename: str, path: str, file_hash: str, overrides: dict | None = None, parent: int | None = None,
          sync=False, parent_from=("held",)) -> int:
    """Create a run and process it. With `parent`, the parent is atomically moved to 'superseded'
    first; if it isn't in an allowed state (already resolved, already re-run, double click), nothing
    is created and TransitionError is raised."""
    from .states import TransitionError, claim_supersede, state_of
    run_id = db.create_run(filename, path, file_hash, now_iso(), overrides, parent)
    if parent and not claim_supersede(parent, run_id, parent_from):
        db.execute("DELETE FROM runs WHERE id=?", (run_id,))
        p = db.one("SELECT * FROM runs WHERE id=?", (parent,))
        raise TransitionError(parent, state_of(p), "confirm_po" if "held" in parent_from else "retry")
    if sync:
        execute(run_id, path, overrides or {})
    else:
        threading.Thread(target=execute, args=(run_id, path, overrides or {}), daemon=True).start()
    return run_id
