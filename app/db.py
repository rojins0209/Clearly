"""SQLite persistence: runs (one per invoice processed), their stage events, and the
billing ledger used for duplicate detection and cumulative PO billing."""
import json
import sqlite3
import threading

from .config import DB_PATH

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT,
    status TEXT NOT NULL,              -- running | done | error
    decision TEXT,                     -- APPROVE | HOLD | REJECT
    final_decision TEXT,               -- after any human resolution
    vendor_name TEXT,
    vendor_id TEXT,
    invoice_number TEXT,
    invoice_number_norm TEXT,
    invoice_date TEXT,
    po_number TEXT,
    total REAL,
    payable REAL,
    extractor TEXT,
    duration_ms INTEGER,
    headline TEXT,
    overrides TEXT,                    -- JSON: human inputs that drove a re-run
    parent_run_id INTEGER,             -- set when this run is a re-run of another
    superseded_by INTEGER,
    resolution TEXT,                   -- JSON: human decision on a HOLD
    result TEXT                        -- full JSON result
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,              -- running | pass | warn | fail | skip
    title TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS billed_lines (
    run_id INTEGER NOT NULL,
    po_number TEXT NOT NULL,
    po_line INTEGER NOT NULL,
    qty REAL NOT NULL,
    amount REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
"""


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, conn() as c:
        c.executescript(SCHEMA)


def reset():
    with _lock, conn() as c:
        c.executescript("DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS events; DROP TABLE IF EXISTS billed_lines;")
        c.executescript(SCHEMA)


def execute(sql, args=()):
    with _lock, conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def query(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def create_run(filename, file_path, file_hash, created_at, overrides=None, parent=None):
    return execute(
        "INSERT INTO runs (created_at, filename, file_path, file_hash, status, overrides, parent_run_id) VALUES (?,?,?,?,?,?,?)",
        (created_at, filename, file_path, file_hash, "running", json.dumps(overrides or {}), parent))


def add_event(run_id, seq, ts, stage, status, title, detail=None):
    execute("INSERT INTO events (run_id, seq, ts, stage, status, title, detail) VALUES (?,?,?,?,?,?,?)",
            (run_id, seq, ts, stage, status, title, json.dumps(detail or {}, default=str)))


def update_run(run_id, **fields):
    if "result" in fields and not isinstance(fields["result"], str):
        fields["result"] = json.dumps(fields["result"], default=str)
    cols = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def active_runs(exclude_id=None):
    """Runs that count for duplicate detection: finished, not rejected, not superseded."""
    return query(
        "SELECT * FROM runs WHERE status='done' AND COALESCE(final_decision, decision) != 'REJECT' "
        "AND superseded_by IS NULL AND id != ?", (exclude_id or -1,))


def billed_qty(po_number, exclude_run=None) -> dict[int, float]:
    """Quantity already billed per PO line by APPROVED invoices (final decision)."""
    rows = query(
        "SELECT b.po_line, SUM(b.qty) q FROM billed_lines b JOIN runs r ON r.id=b.run_id "
        "WHERE b.po_number=? AND COALESCE(r.final_decision, r.decision)='APPROVE' AND r.superseded_by IS NULL "
        "AND r.id != ? GROUP BY b.po_line", (po_number, exclude_run or -1))
    return {r["po_line"]: r["q"] for r in rows}
