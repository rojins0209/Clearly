"""The run lifecycle as one explicit state machine.

A run's state used to be implied by five columns (status, decision, final_decision,
superseded_by, parent_run_id) and re-derived separately by the API, the stats and the UI.
This module is now the single source of truth. The columns are unchanged, so existing
databases keep working; `state_of()` derives the state, `actions_for()` says what may
happen next, and every write that changes state goes through `transition()`, which is
atomic (guarded UPDATE) so double clicks and races can't corrupt the history.

    running ──▶ failed ──(retry)──▶ [new run]
       │
       ├──▶ approved                              (terminal)
       ├──▶ rejected                              (terminal)
       └──▶ held ──(approve / approve_partial)──▶ approved_by_reviewer   (terminal)
                 ├─(reject)───────────────────▶ rejected_by_reviewer   (terminal)
                 └─(confirm_po)───────────────▶ superseded ──▶ [new run, parent linked]
"""
from enum import Enum

from . import db


class RunState(str, Enum):
    RUNNING = "running"
    FAILED = "failed"
    APPROVED = "approved"
    HELD = "held"
    REJECTED = "rejected"
    APPROVED_BY_REVIEWER = "approved_by_reviewer"
    REJECTED_BY_REVIEWER = "rejected_by_reviewer"
    SUPERSEDED = "superseded"


TERMINAL = {RunState.APPROVED, RunState.REJECTED, RunState.APPROVED_BY_REVIEWER,
            RunState.REJECTED_BY_REVIEWER, RunState.SUPERSEDED}

# What the run means for money, whoever decided it. None = no outcome yet / not current.
OUTCOME = {
    RunState.APPROVED: "APPROVE", RunState.APPROVED_BY_REVIEWER: "APPROVE",
    RunState.REJECTED: "REJECT", RunState.REJECTED_BY_REVIEWER: "REJECT",
    RunState.HELD: "HOLD",
}

LABELS = {
    RunState.RUNNING: "Running", RunState.FAILED: "Failed", RunState.APPROVED: "Approved",
    RunState.HELD: "On hold", RunState.REJECTED: "Rejected",
    RunState.APPROVED_BY_REVIEWER: "Approved by reviewer", RunState.REJECTED_BY_REVIEWER: "Rejected by reviewer",
    RunState.SUPERSEDED: "Superseded",
}

DESCRIPTIONS = {
    RunState.RUNNING: "The pipeline is processing the invoice.",
    RunState.FAILED: "The run stopped with an error (or the server restarted mid-run). Retry creates a new run.",
    RunState.APPROVED: "All checks passed; queued for payment. No action possible.",
    RunState.HELD: "Payable maybe, but someone on our side must act. Resolve it or confirm the suggested PO.",
    RunState.REJECTED: "The document can't be paid as issued; the vendor must re-issue. No action possible.",
    RunState.APPROVED_BY_REVIEWER: "A reviewer approved a held invoice (in full or the received portion). Final.",
    RunState.REJECTED_BY_REVIEWER: "A reviewer rejected a held invoice. Final.",
    RunState.SUPERSEDED: "Replaced by a re-run after human input; see superseded_by for the current run.",
}

ACTIONS = {
    "approve": {"label": "Approve in full", "method": "POST", "path": "/api/runs/{id}/resolve",
                "body": {"decision": "APPROVE", "note": "<required>"}, "to": RunState.APPROVED_BY_REVIEWER},
    "approve_partial": {"label": "Pay received portion", "method": "POST", "path": "/api/runs/{id}/resolve",
                        "body": {"decision": "APPROVE", "amount": "<approvable_now>", "note": "<required>"},
                        "to": RunState.APPROVED_BY_REVIEWER},
    "reject": {"label": "Reject", "method": "POST", "path": "/api/runs/{id}/resolve",
               "body": {"decision": "REJECT", "note": "<required>"}, "to": RunState.REJECTED_BY_REVIEWER},
    "confirm_po": {"label": "Confirm PO & re-run", "method": "POST", "path": "/api/runs/{id}/confirm-po",
                   "body": {"po_number": "<PO-xxxx>"}, "to": RunState.SUPERSEDED},
    "retry": {"label": "Retry", "method": "POST", "path": "/api/runs/{id}/retry", "body": {}, "to": None},
}


def state_of(r: dict) -> RunState:
    if r.get("superseded_by"):
        return RunState.SUPERSEDED
    if r["status"] == "running":
        return RunState.RUNNING
    if r["status"] == "error":
        return RunState.FAILED
    if r.get("final_decision") == "APPROVE":
        return RunState.APPROVED_BY_REVIEWER
    if r.get("final_decision") == "REJECT":
        return RunState.REJECTED_BY_REVIEWER
    return {"APPROVE": RunState.APPROVED, "REJECT": RunState.REJECTED}.get(r.get("decision"), RunState.HELD)


def actions_for(r: dict, result: dict | None = None) -> list[str]:
    s = state_of(r)
    if s == RunState.FAILED:
        return ["retry"]
    if s != RunState.HELD:
        return []
    acts = []
    dec = (result or {}).get("decision") or {}
    if any(f.get("code") == "po_inferred" for f in (result or {}).get("findings", [])):
        acts.append("confirm_po")
    if dec.get("approvable_now"):
        acts.append("approve_partial")
    return acts + ["approve", "reject"]


def describe(r: dict, result: dict | None = None) -> dict:
    s = state_of(r)
    names = actions_for(r, result)
    return {
        "state": s.value, "state_label": LABELS[s], "outcome": OUTCOME.get(s),
        "terminal": s in TERMINAL,
        "actions": [{"name": n, "label": ACTIONS[n]["label"], "method": ACTIONS[n]["method"],
                     "path": ACTIONS[n]["path"].format(id=r["id"]), "body": ACTIONS[n]["body"]} for n in names],
    }


def machine() -> dict:
    """Machine-readable description: for docs, the UI, and agents driving the API."""
    return {
        "states": [{"name": s.value, "label": LABELS[s], "terminal": s in TERMINAL, "description": DESCRIPTIONS[s]}
                   for s in RunState],
        "actions": {n: {**{k: v for k, v in a.items() if k != "to"}, "from": "failed" if n == "retry" else "held",
                        "to": a["to"].value if a["to"] else "new run"} for n, a in ACTIONS.items()},
    }


class TransitionError(Exception):
    def __init__(self, run_id: int, state: RunState, action: str):
        self.run_id, self.state, self.action = run_id, state, action
        allowed = actions_for(db.one("SELECT * FROM runs WHERE id=?", (run_id,)) or {"status": "done", "id": run_id})
        super().__init__(f"Run #{run_id} is '{state.value}'; '{action}' is not allowed. "
                         f"Allowed now: {', '.join(allowed) or 'none'}.")


# Guarded writes: the WHERE clause re-checks the source state inside the same statement,
# so of two concurrent requests exactly one succeeds.
_FROM_HELD = "status='done' AND decision='HOLD' AND final_decision IS NULL AND superseded_by IS NULL"


def claim_resolution(run_id: int, **fields) -> bool:
    cols = ", ".join(f"{k}=?" for k in fields)
    with db._lock, db.conn() as c:
        return c.execute(f"UPDATE runs SET {cols} WHERE id=? AND {_FROM_HELD}", (*fields.values(), run_id)).rowcount == 1


def claim_supersede(run_id: int, new_run_id: int, from_states=("held",)) -> bool:
    where = {"held": _FROM_HELD, "failed": "status='error' AND superseded_by IS NULL"}
    cond = " OR ".join(f"({where[s]})" for s in from_states)
    with db._lock, db.conn() as c:
        return c.execute(f"UPDATE runs SET superseded_by=? WHERE id=? AND ({cond})", (new_run_id, run_id)).rowcount == 1


def recover_interrupted() -> int:
    """On startup, any run still 'running' was cut off by a restart: no thread will finish it."""
    from .util import now_iso
    stuck = db.query("SELECT id FROM runs WHERE status='running'")
    for r in stuck:
        db.update_run(r["id"], status="error", finished_at=now_iso(),
                      headline="Interrupted: the server restarted while this invoice was processing. Retry to run it again.")
        seq = (db.one("SELECT MAX(seq) m FROM events WHERE run_id=?", (r["id"],)) or {}).get("m") or 0
        db.add_event(r["id"], seq + 1, now_iso(), "error", "fail", "Interrupted by a server restart", {"retryable": True})
    return len(stuck)
