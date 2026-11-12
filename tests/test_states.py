"""The run lifecycle: every legal transition works, every illegal one is refused with 409."""
import time

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.states import RunState, recover_interrupted, state_of

c = TestClient(app)


def wait(rid):
    for _ in range(300):
        r = c.get(f"/api/runs/{rid}").json()
        if r["state"] != "running":
            return r
        time.sleep(0.03)
    raise AssertionError("run did not finish")


def run(name):
    return wait(c.post(f"/api/runs/sample/{name}").json()["run_id"])


def names(r):
    return [a["name"] for a in r["actions"]]


def test_terminal_states_expose_no_actions(fresh_db):
    a = run("01_happy_sri_lakshmi.pdf")
    assert a["state"] == "approved" and a["terminal"] and a["actions"] == []
    r = run("05_edge_wrong_gst_coastal.pdf")
    assert r["state"] == "rejected" and r["actions"] == []


def test_held_actions_depend_on_the_hold(fresh_db):
    assert names(run("03_edge_partial_delivery_kaveri.pdf")) == ["approve_partial", "approve", "reject"]
    assert names(run("04_edge_bank_change_malabar.pdf")) == ["approve", "reject"]
    assert names(run("06_edge_scanned_no_po_nilgiri.pdf")) == ["confirm_po", "approve", "reject"]


def test_resolution_is_final(fresh_db):
    r = run("04_edge_bank_change_malabar.pdf")
    ok = c.post(f"/api/runs/{r['id']}/resolve", json={"decision": "REJECT", "note": "vendor did not confirm"})
    assert ok.status_code == 200 and ok.json()["state"] == "rejected_by_reviewer"
    again = c.post(f"/api/runs/{r['id']}/resolve", json={"decision": "APPROVE", "note": "changed my mind"})
    assert again.status_code == 409 and again.json()["state"] == "rejected_by_reviewer"
    assert c.get(f"/api/runs/{r['id']}").json()["resolution"]["decision"] == "REJECT"   # history intact


def test_cannot_act_on_approved_or_rejected_runs(fresh_db):
    a = run("01_happy_sri_lakshmi.pdf")
    assert c.post(f"/api/runs/{a['id']}/confirm-po", json={"po_number": "PO-4501"}).status_code == 409
    assert c.post(f"/api/runs/{a['id']}/resolve", json={"decision": "REJECT", "note": "x"}).status_code == 409
    assert c.post(f"/api/runs/{a['id']}/retry").status_code == 409


def test_double_confirm_creates_exactly_one_rerun(fresh_db):
    s = run("06_edge_scanned_no_po_nilgiri.pdf")
    first = c.post(f"/api/runs/{s['id']}/confirm-po", json={"po_number": "PO-4506"})
    second = c.post(f"/api/runs/{s['id']}/confirm-po", json={"po_number": "PO-4506"})
    assert first.status_code == 200 and second.status_code == 409
    child = wait(first.json()["run_id"])
    assert child["state"] == "approved"
    parent = c.get(f"/api/runs/{s['id']}").json()
    assert parent["state"] == "superseded" and parent["superseded_by"] == child["id"]
    assert db.one("SELECT COUNT(*) n FROM runs")["n"] == 2       # no orphan row from the refused click
    assert c.post(f"/api/runs/{s['id']}/resolve", json={"decision": "REJECT", "note": "x"}).status_code == 409


def test_confirm_po_validates_the_po(fresh_db):
    s = run("06_edge_scanned_no_po_nilgiri.pdf")
    assert c.post(f"/api/runs/{s['id']}/confirm-po", json={"po_number": "PO-9999"}).status_code == 400
    assert c.post(f"/api/runs/{s['id']}/confirm-po", json={"po_number": "PO-4501"}).status_code == 400   # other vendor
    assert c.get(f"/api/runs/{s['id']}").json()["state"] == "held"


def test_partial_amount_capped_by_goods_receipt(fresh_db):
    r = run("03_edge_partial_delivery_kaveri.pdf")
    too_much = r["result"]["decision"]["approvable_now"] + 5000
    assert c.post(f"/api/runs/{r['id']}/resolve", json={"decision": "APPROVE", "amount": too_much, "note": "x"}).status_code == 400


def test_interrupted_run_recovers_and_can_retry(fresh_db):
    rid = db.create_run("01_happy_sri_lakshmi.pdf", "samples/01_happy_sri_lakshmi.pdf", "h", "2026-11-10T10:00:00")
    assert recover_interrupted() == 1
    r = c.get(f"/api/runs/{rid}").json()
    assert r["state"] == "failed" and names(r) == ["retry"]
    assert r["events"][-1]["stage"] == "error"                  # live view's stream closes instead of hanging
    new = c.post(f"/api/runs/{rid}/retry").json()["run_id"]
    assert wait(new)["state"] == "approved"
    assert c.post(f"/api/runs/{rid}/retry").status_code == 409   # one retry only


def test_machine_is_self_describing():
    m = c.get("/api/states").json()
    assert {s["name"] for s in m["states"]} == {s.value for s in RunState}
    assert m["actions"]["confirm_po"]["from"] == "held" and m["actions"]["retry"]["from"] == "failed"


def test_state_derivation_matches_columns():
    base = {"id": 1, "status": "done", "decision": "HOLD", "final_decision": None, "superseded_by": None}
    assert state_of(base) == RunState.HELD
    assert state_of({**base, "final_decision": "APPROVE"}) == RunState.APPROVED_BY_REVIEWER
    assert state_of({**base, "superseded_by": 9}) == RunState.SUPERSEDED
    assert state_of({**base, "status": "error"}) == RunState.FAILED
