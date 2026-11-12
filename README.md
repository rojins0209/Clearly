# Clearly: vendor invoice → reasoned AP decision

**Zamp AI Solutions Associate case study, PS-1 (Invoice processing). Rojins S Martin.**

Clearly takes a vendor invoice PDF (digital or scanned) and returns one of three decisions: **APPROVE**, **HOLD** or **REJECT**. Every decision comes with the reasons, the amount payable now, the person on our side who has to act, and a draft message to the vendor when they need to fix something. You can watch each stage run, and the dashboard shows history, value and the exception queue across runs.

The demo company is **Northwind Retail Pvt Ltd** (Bengaluru, GSTIN `29AABCN1234F1ZS`). It has six vendors in three states, seven POs and six goods receipts. All of it is fictional but structurally realistic, including valid GSTIN checksums.

---

## Run it

```bash
./run.sh                       # venv + deps + server on http://localhost:8000
# or
docker build -t clearly . && docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-... clearly
```

* **With `ANTHROPIC_API_KEY`**, Claude reads the PDF directly, scans included.
* **Without a key** the app still runs end to end, using Tesseract OCR plus a rules parser. The tests use this path.
* Copy `.env.example` to `.env` for local settings. `render.yaml` deploys it as a Docker web service on Render. Set the key in the dashboard.
* `python -m pytest`: 39 tests covering GST rules, every demo scenario end to end, decision policy on synthetic cases, the API, the run lifecycle, and the Claude path (mocked).
* `python scripts/generate_samples.py` regenerates the invoice PDFs.

---

## The process

```
PDF ─▶ 1 Read ─▶ 2 Extract ─▶ 3 Verify ─▶ 4 Validate ─▶ 5 Vendor & GST ─▶ 6 Duplicates ─▶ 7 Find PO ─▶ 8 3-way match ─▶ 9 Decide
        text or     Claude or     is each     required       approved vendor?    exact file,        explicit, or      price vs PO,        APPROVE / HOLD / REJECT
        scan (OCR)  OCR+rules     value       fields, dates, GSTIN checksum,     same number        inferred from     qty vs received,    + owner, action, amount,
                                  grounded?   arithmetic     tax head, bank      reformatted,       lines + amount    tax rate,           vendor message
                                                             vs master           near-duplicate     + date hint       cumulative billing
```

Every stage writes an event. The UI streams those events live (SSE), so you see each stage start, its result and its timing. Each stage can be expanded to show the individual checks.

---

## Demo order

| # | File | What it shows | Expected |
|---|------|---------------|----------|
| 1 | `01_happy_sri_lakshmi.pdf` | Clean digital invoice, PO quoted, CGST+SGST | **APPROVE** ₹68,440 |
| 2 | `02_happy_deccan_office.pdf` | Different layout, inter-state IGST, paper billed at ₹249.00 vs PO ₹248.50 (0.2%) | **APPROVE**, variance noted |
| 3 | `03_edge_partial_delivery_kaveri.pdf` | Bills 120 LED panels, the warehouse received 80 | **HOLD**, ₹2,14,052 approvable now |
| 4 | `04_edge_bank_change_malabar.pdf` | Every number matches, but "we've changed our bankers" | **HOLD**, Treasury call-back |
| 5 | `05_edge_wrong_gst_coastal.pdf` | Karnataka → Karnataka supply charged IGST | **REJECT**, credit note + reissue |
| 6 | `06_edge_scanned_no_po_nilgiri.pdf` | Scanned, no PO number, vendor has two open POs | **HOLD**, PO-4506 inferred; confirm, re-run, **APPROVE** |
| 7 | `07_duplicate_resend_sri_lakshmi.pdf` | #1 re-sent as a "reminder", number reformatted `INV/2026/417` | **REJECT**, duplicate (only after #1 ran) |

**Reset demo** (top right) clears the ledger so the sequence can be replayed.

---

## The four edge cases, and why these four

The brief asks for edge cases that show you understand the problem. Each of these four is a separate way a careful-looking invoice can still be wrong to pay. Each one tests a different part of the logic.

**1. Partial delivery (billed > received).** The most common real 3-way-match exception. A two-way check (invoice vs PO) passes, because the PO *was* for 120. Only the goods receipt shows that 40 haven't arrived. The process doesn't just say "mismatch". It works out the **amount that is payable now** (80 units + 30 coils + GST = ₹2,14,052), sends the hold to **Receiving** to confirm, and drafts a vendor note asking for a revised invoice or credit note. A reviewer can pay the received portion in one click. Only the quantity actually paid counts against the PO from then on.

**2. Changed bank details.** Everything about this invoice is correct, and that's the point. A system that only checks numbers would approve it. Bank details changed on an invoice is the classic payment-fraud pattern (business email compromise). The process holds it for **Treasury**, tells them to call back on the number *already in the vendor master* and never one printed on the invoice, and gives the evidence needed to update the master only after written confirmation.

**3. Wrong GST type.** An intra-state supply (Karnataka → Karnataka) must carry CGST + SGST. This invoice charges IGST. The total is right, so a tired reviewer would pay it, and we would lose input tax credit. The amount isn't the problem, the document is, so this is a **REJECT**, not a hold. The vendor message spells out the fix: a credit note plus a fresh invoice with the correct tax.

**4. Scanned invoice with no PO number.** This covers two problems from the brief at once: scanned images and implied PO references. The document has no text layer. We OCR it to have something to verify against. The OCR misreads the GSTIN (`…P12W`), and a format-aware repair corrects it, verified by the GSTIN checksum. There's no PO number, only "Your order dt. 14/10/2026". The vendor has two open POs, so we score both (line similarity, amount fit, order-date reference) and propose PO-4506 at 0.99 vs 0.56. Inferring the PO is a suggestion, not an authorisation, so it **holds for one-click confirmation**. The invoice then re-runs with an audit link to the original.

**Baseline controls** (built in, not featured as edge cases): exact-file and reformatted-number duplicates, near-duplicates (same vendor + amount within 45 days → hold, not reject), arithmetic that doesn't reconcile, unknown vendor, invalid GSTIN checksum, invoice not addressed to our GSTIN, CGST ≠ SGST, tax rate ≠ PO rate, unit price over tolerance, billing beyond the PO, no goods receipt, foreign currency, future or stale dates, and an auto-approval value limit.

---

## Design decisions

**1. The LLM reads, code decides.** Claude does one job: turn a messy PDF into fields, with the exact text each value came from. Every approve, hold or reject is made by deterministic Python that I can test, explain and change without re-prompting. A finance team can audit a rule. It can't audit a model's judgment call on a ₹2.8 lakh payment.

**2. Confidence comes from evidence, not from the model's opinion.** Model-reported confidence is poorly calibrated. Each critical field is scored on things we can check. Does the value literally appear in the document text? Do the amounts reconcile (qty × rate = amount, lines = subtotal, subtotal + tax = total)? Was it read from a scan, and was it OCR-repaired? The model's own confidence can only *lower* the score.

**3. "Is the document wrong, or did we misread it?"** This distinction drives the most important behaviour. A grounded value that breaks a rule means the document is wrong, so we act on it (reject, ask the vendor). A value we couldn't verify that breaks a rule might be a misreading, so a person looks first. A REJECT that depends on a low-confidence field is automatically downgraded to HOLD, with no vendor email. We never bounce a vendor's invoice because of our own OCR error.

**4. Reject vs hold means *who* has to act.** **Reject** means the document can't be paid as issued and the vendor must reissue it (wrong tax head, invalid GSTIN, not addressed to us, duplicate). **Hold** means it may well be payable, but someone *on our side* must act first: receive goods, approve a variance, verify bank details, confirm a PO, check our reading. Arithmetic errors are a hold, not a reject, because they could be our extraction.

**5. The output is a to-do list, not a log.** Each finding names an owner (AP, Procurement, Receiving, Treasury, Tax, Finance controller) and a concrete action. The dashboard rolls these up into *Why invoices didn't auto-approve* and *Who needs to act*. Vendor-facing problems produce a ready-to-send email.

**6. Tolerances are policy, kept in data.** Unit price may run up to 1% over PO (paper at +0.2% passes, with the impact noted). Billing below PO price is fine. Quantity has **no** upward tolerance: you can't pay for what you didn't receive. Rounding is ±₹1. These values live in `data/reference.json` → `policy`, not in code.

**7. Billing is cumulative.** Approved invoices consume PO and GRN quantity. A second invoice for goods already paid for fails even if it would pass on its own. Partial approvals consume only the quantity actually paid.

**8. Duplicates are matched on meaning, not exact strings.** `INV-2026-0417` and `INV/2026/417` normalise to the same key. Same vendor + same amount + close dates + *different* number is only a possible duplicate, so it holds. Legitimate repeat orders exist.

**9. Humans stay in the loop where judgment is genuinely needed.** Confirm an inferred PO, pay the received portion, or approve/reject a hold. Every one requires a note, and every re-run links back to its original. Nothing is overwritten.

**10. It degrades instead of breaking.** If the LLM call fails (timeout, overload), extraction falls back to OCR + rules and the stage shows a warning. The run still completes. With no API key at all, the app still runs every scenario.

**11. Above ₹5,00,000, a second approver signs.** A clean invoice above the auto-approval limit holds for the Finance controller.

A note on honesty: stages pause for 350 ms by default (`STAGE_DELAY_MS`) so the live view is readable in a demo. Real processing time is shown per stage and is mostly the LLM call.

---

## Run lifecycle

Every run is in exactly one state, defined in `app/states.py` and served at `GET /api/states`:

```
running ──▶ failed ──(retry)──▶ new run
   ├──▶ approved                     (final)
   ├──▶ rejected                     (final)
   └──▶ held ──(approve / pay received portion)──▶ approved by reviewer   (final)
             ├─(reject)─────────────────────────▶ rejected by reviewer   (final)
             └─(confirm PO)─────────────────────▶ superseded ──▶ new run
```

* Each run returns its `state` and the `actions` allowed right now (with method, path and body). The UI draws its buttons from that list, and an agent can drive the API the same way.
* Any other request gets **409** naming the current state and what is allowed. Resolutions are final, so the audit trail can't be overwritten.
* Transitions are single guarded database updates, so a double click or two reviewers at once produce exactly one outcome.
* On startup, runs cut off by a restart become `failed` with a retry, instead of spinning forever.

## Assumptions

* Invoices are in INR, from Indian GST-registered vendors, to one buyer entity (Northwind, Karnataka).
* Place of supply is taken from the invoice. If it's absent, the buyer's state is used (B2B goods delivered to our address).
* The PO's GST rate is the source of truth for the line's rate. I don't maintain an HSN rate table.
* GSTIN validation is structural (format + mod-36 checksum + state code). A live GSTN lookup (active/cancelled, filing status) is the obvious production addition.
* Invoices arrive one at a time through upload. In production, the intake would be the AP inbox.
* Reference data is a JSON stand-in for the ERP. The ledger is SQLite.

## What I'd build next

1. **Email intake.** Watch the AP mailbox, split multi-invoice PDFs, ignore statements and remittances.
2. **GSTN and GSTR-2B reconciliation.** Confirm the GSTIN is active and that the vendor actually filed this invoice before we claim ITC.
3. **Corrections loop.** Let reviewers fix a field in the side-by-side view and re-run. Track per-vendor extraction accuracy.
4. **ERP write-back.** Post approved invoices and short-pays to Tally, SAP or NetSuite. Sync vendors, POs and GRNs instead of a JSON file.
5. **Evals and monitoring.** A labelled invoice set scored per field on each prompt or model change. Langfuse traces per run, plus alerting on auto-approval-rate drift (the same setup I run for VoiceStack's agents).

---

## Layout

```
app/
  main.py              FastAPI: upload, SSE stream, runs, resolve/confirm, stats, reference
  pipeline.py          orchestrates the 9 stages, writes events + results
  stages/ingest.py     text layer vs scan, OCR
  stages/extract.py    Claude (PDF → tool call) and the OCR + rules fallback
  stages/confidence.py evidence-based field confidence
  stages/checks.py     validation, vendor & GST, duplicates, PO resolution, 3-way match
  decide.py            findings → decision, owners, payable amount, vendor message
  states.py            the run lifecycle state machine (states, allowed actions, guarded transitions)
  gst.py               GSTIN checksum, state codes, tax-head rule
data/reference.json    buyer, vendor master, POs, GRNs, policy
samples/               generated invoice PDFs (scripts/generate_samples.py)
static/                UI: dashboard, live run view, procurement data
tests/                 39 tests
```

The Dockerfile wasn't built in my sandbox (no Docker daemon). It's the standard python-slim + tesseract-ocr pattern. Build it once locally before deploying.
