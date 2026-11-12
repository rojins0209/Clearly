import re
from datetime import date, datetime

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_amount(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"[^\d.\-]", "", str(s))
    try:
        return float(t) if t not in ("", ".", "-") else None
    except ValueError:
        return None


def parse_date(s) -> date | None:
    """Indian invoices are day-first: 16-Oct-2026, 19/10/2026, 22.10.2026, 2026-10-22."""
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
        return _safe(y, mo, d)
    m = re.search(r"(\d{1,2})[\s\-/.]+([A-Za-z]{3,9})[\s\-/.,]+(\d{2,4})", s)
    if m and m.group(2)[:3].lower() in MONTHS:
        d, mon, y = m.groups()
        y = int(y) + (2000 if len(y) == 2 else 0)
        return _safe(y, MONTHS[mon[:3].lower()], int(d))
    m = re.search(r"(\d{1,2})[\-/.](\d{1,2})[\-/.](\d{2,4})", s)
    if m:
        d, mo, y = m.groups()
        y = int(y) + (2000 if len(y) == 2 else 0)
        return _safe(y, int(mo), int(d))
    return None


def _safe(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def norm_invoice_no(s: str | None) -> str:
    """INV-2026-0417 and INV/2026/417 normalise to the same key: INV|2026|417."""
    toks = re.findall(r"[A-Z]+|\d+", (s or "").upper())
    return "|".join(t.lstrip("0") or "0" if t.isdigit() else t for t in toks)


def norm_text(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def money(x: float | None) -> str:
    if x is None:
        return "—"
    neg = x < 0
    whole, frac = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail, parts = whole[:-3], whole[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + "₹" + whole + "." + frac


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
