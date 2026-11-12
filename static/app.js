// Clearly - vanilla JS front end. Hash routes: #/  #/run/:id  #/reference
const $app = document.getElementById("app");
const STAGES = [
  ["ingest", "Read document"], ["extract", "Extract fields"], ["confidence", "Verify extraction"],
  ["validate", "Validate invoice"], ["compliance", "Vendor & GST checks"], ["duplicates", "Duplicate check"],
  ["po", "Find purchase order"], ["match", "3-way match"], ["decision", "Decision"],
];
const FIELD_LABELS = {
  invoice_number: "Invoice number", invoice_date: "Invoice date", vendor_gstin: "Vendor GSTIN", buyer_gstin: "Buyer GSTIN",
  total: "Total", line_items: "Line items", po_reference: "PO reference", subtotal: "Taxable value", cgst: "CGST",
  sgst: "SGST", igst: "IGST", bank_account_number: "Bank account", bank_ifsc: "IFSC", place_of_supply: "Place of supply",
};
let liveSource = null;

const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (x) => {
  if (x === null || x === undefined || isNaN(x)) return "—";
  return "₹" + Number(x).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const inrShort = (x) => {
  if (!x) return "₹0";
  if (x >= 1e7) return "₹" + (x / 1e7).toFixed(2) + " Cr";
  if (x >= 1e5) return "₹" + (x / 1e5).toFixed(2) + " L";
  return inr(x);
};
const when = (iso) => iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
const toast = (msg) => { const t = document.getElementById("toast"); t.textContent = msg; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2200); };
const chip = (d) => `<span class="chip ${esc(d)}">${esc(d)}</span>`;
// One place that turns the server's run state into a badge. The server owns the state machine (/api/states).
const STATE_CHIP = {
  running: ["running", "RUNNING"], failed: ["error", "FAILED"], approved: ["APPROVE", "APPROVE"], held: ["HOLD", "HOLD"],
  rejected: ["REJECT", "REJECT"], approved_by_reviewer: ["APPROVE", "APPROVE"], rejected_by_reviewer: ["REJECT", "REJECT"],
  superseded: ["superseded", "SUPERSEDED"],
};
const stateChip = (r) => { const [cls, txt] = STATE_CHIP[r.state] || ["", r.state]; return `<span class="chip ${cls}" title="${esc(r.state_label || "")}">${txt}</span>`; };
const tagClass = (t) => t.startsWith("Happy") ? "happy" : t.startsWith("Edge") ? "edge" : "ctrl";

// ---------------------------------------------------------------- router
async function route() {
  if (liveSource) { liveSource.close(); liveSource = null; }
  const h = location.hash || "#/";
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active",
    (a.dataset.nav === "dash" && (h === "#/" || h.startsWith("#/run"))) || (a.dataset.nav === "ref" && h.startsWith("#/reference"))));
  window.scrollTo(0, 0);
  try {
    if (h.startsWith("#/run/")) return await renderRun(+h.split("/")[2]);
    if (h.startsWith("#/reference")) return await renderReference();
    return await renderDashboard();
  } catch (e) {
    $app.innerHTML = `<div class="card card-b">Something went wrong: ${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- dashboard
async function renderDashboard() {
  const [samples, runs, stats] = await Promise.all([api("/api/samples"), api("/api/runs"), api("/api/stats")]);
  $app.innerHTML = `
  <div class="page-head">
    <div><h1>Accounts payable · invoice decisions</h1>
      <p>Drop a vendor invoice. Clearly reads it, checks it against the vendor master, POs and goods receipts, and decides: approve, hold or reject, and who needs to act.</p></div>
  </div>
  <div class="grid g-intake">
    <div class="card"><div class="card-h"><h2>Process an invoice</h2><span class="muted small">PDF · digital or scanned</span></div>
      <div class="card-b" style="height:calc(100% - 50px)">
        <label class="drop" id="drop"><input type="file" id="file" accept="application/pdf" hidden />
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 16V4m0 0l-4 4m4-4l4 4M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3"/></svg>
          <strong>Drop an invoice PDF here</strong><span class="small">or click to choose a file</span></label>
      </div></div>
    <div class="card"><div class="card-h"><h2>Prepared demo invoices</h2><span class="muted small">click to run</span></div>
      <div class="card-b" style="padding:4px 16px"><div class="samples">
        ${samples.map((s) => `<div class="sample" data-sample="${esc(s.name)}">
          <span class="tag ${tagClass(s.tag)}">${esc(s.tag)}</span>
          <div><div class="desc">${esc(s.description)}</div><div class="fname">${esc(s.name)}</div></div>
          <span class="go">Run →</span></div>`).join("")}
      </div></div></div>
  </div>
  <div class="kpis">
    ${kpi("Invoices processed", stats.processed, `avg ${stats.avg_seconds}s each`)}
    ${kpi("Auto-approved", stats.auto_rate + "%", `${stats.auto_approved} of ${stats.processed}`)}
    ${kpi("Value approved", inrShort(stats.value_approved), "cleared for payment")}
    ${kpi("Open holds", stats.open_holds, inrShort(stats.value_held) + " waiting" + (stats.resolved ? ` · ${stats.resolved} resolved` : ""))}
    ${kpi("Rejected", stats.rejected, inrShort(stats.value_rejected) + " returned")}
    ${kpi("Value processed", inrShort(stats.value_processed), "all invoices")}
  </div>
  <div class="grid g-2" style="margin-bottom:16px">
    <div class="card"><div class="card-h"><h2>Why invoices didn't auto-approve</h2></div><div class="card-b">${bars(stats.reasons)}</div></div>
    <div class="card"><div class="card-h"><h2>Who needs to act</h2></div><div class="card-b">${bars(stats.owners, "#2f5bea")}</div></div>
  </div>
  <div class="card"><div class="card-h"><h2>Run history</h2><span class="muted small">${runs.length} run(s)</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>#</th><th>Received</th><th>Vendor</th><th>Invoice</th><th>PO</th><th class="num">Amount</th><th>Decision</th><th>Reason</th><th>Extractor</th><th class="num">Time</th></tr></thead>
      <tbody>${runs.length ? runs.map(runRow).join("") : `<tr><td colspan="10" class="empty" style="padding:22px">No invoices processed yet. Run one of the prepared invoices above.</td></tr>`}</tbody>
    </table></div></div>`;

  document.querySelectorAll("[data-sample]").forEach((el) => el.onclick = async () => {
    const { run_id } = await post(`/api/runs/sample/${encodeURIComponent(el.dataset.sample)}`);
    location.hash = `#/run/${run_id}`;
  });
  document.querySelectorAll("tr[data-run]").forEach((tr) => tr.onclick = () => location.hash = `#/run/${tr.dataset.run}`);
  const drop = document.getElementById("drop"), input = document.getElementById("file");
  const upload = async (file) => {
    if (!file) return;
    const fd = new FormData(); fd.append("file", file);
    try { const { run_id } = await api("/api/runs", { method: "POST", body: fd }); location.hash = `#/run/${run_id}`; }
    catch (e) { toast(e.message); }
  };
  input.onchange = () => upload(input.files[0]);
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));
}

const kpi = (label, value, sub) => `<div class="card kpi"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
function bars(items, color) {
  if (!items.length) return `<div class="empty">Nothing yet.</div>`;
  const max = Math.max(...items.map((x) => x[1]));
  return `<div class="bars">${items.map(([k, v]) => `<div class="bar-row"><span>${esc(k)}</span>
    <div class="bar"><i style="width:${(v / max) * 100}%;${color ? "background:" + color : ""}"></i></div><b class="num">${v}</b></div>`).join("")}</div>`;
}
function runRow(r) {
  const byReviewer = r.state === "approved_by_reviewer" || r.state === "rejected_by_reviewer";
  const d = stateChip(r) + (byReviewer ? `<div class="muted small">${r.state === "approved_by_reviewer" && r.payable < r.total - 1 ? "partial · " : ""}reviewer</div>` : "");
  const sup = r.superseded_by ? `<div class="muted small">re-run as #${r.superseded_by}</div>` : "";
  return `<tr class="click ${r.state === "superseded" ? "superseded" : ""}" data-run="${r.id}">
    <td class="muted">${r.id}</td><td class="small nowrap">${when(r.created_at)}</td>
    <td>${esc(r.vendor_name || r.filename)}</td><td class="mono">${esc(r.invoice_number || "—")}</td>
    <td class="mono nowrap">${esc(r.po_number || "—")}</td><td class="num">${inr(r.total)}</td><td>${d}${sup}</td>
    <td class="small" style="max-width:340px">${esc((r.headline || "").replace(/^(Approve|Hold|Reject) - /, ""))}</td>
    <td class="small muted">${esc(r.extractor || "")}</td><td class="num small muted">${r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + "s" : ""}</td></tr>`;
}

// ---------------------------------------------------------------- run view
async function renderRun(id) {
  const run = await api(`/api/runs/${id}`);
  const banners = [];
  if (run.parent_run_id) banners.push(run.overrides?.po_number
    ? `Re-run of <a href="#/run/${run.parent_run_id}">#${run.parent_run_id}</a> after human input: <b>${esc(run.overrides.po_number)} confirmed by ${esc(run.overrides.confirmed_by || "reviewer")}</b>`
    : `Retry of <a href="#/run/${run.parent_run_id}">#${run.parent_run_id}</a>, which failed.`);
  if (run.state === "superseded") banners.push(`This run was superseded. See <a href="#/run/${run.superseded_by}">#${run.superseded_by}</a> for the current decision.`);
  $app.innerHTML = `
  <div class="run-head">
    <a class="back" href="#/">← Dashboard</a>
    <h1>${esc(run.filename)}</h1><span class="muted">Run #${run.id}</span>
    <span id="runStatus"></span>
    <span class="muted small" style="margin-left:auto">${when(run.created_at)}</span>
  </div>
  ${banners.map((b) => `<div class="banner">${b}</div>`).join("")}
  <div class="run-layout">
    <div class="card"><div class="card-h"><h2>Live run</h2><span class="muted small" id="elapsed"></span></div>
      <ol class="timeline" id="timeline">${STAGES.map(([k, l]) => `<li class="step pending" data-stage="${k}">
        <span class="dot"></span><div class="label"><span>${l}</span><span class="ms"></span></div>
        <div class="title muted">Waiting</div><div class="details"></div></li>`).join("")}</ol></div>
    <div style="display:grid;gap:16px">
      <div id="decision" class="card"><div class="waiting"><div class="spinner"></div>Working through the checks…</div></div>
      <div id="tabsCard" class="card" style="display:none"></div>
    </div>
  </div>`;
  document.querySelectorAll(".step").forEach((s) => s.onclick = () => s.classList.toggle("open"));
  setStatus(run);

  if (run.status === "running") {
    let seen = 0;
    liveSource = new EventSource(`/api/runs/${id}/events`);
    liveSource.onmessage = async (m) => {
      const ev = JSON.parse(m.data); seen++;
      applyEvent(ev);
      if (ev.stage === "done" || ev.stage === "error") {
        liveSource.close(); liveSource = null;
        const full = await api(`/api/runs/${id}`);
        setStatus(full); renderOutcome(full);
      }
    };
    liveSource.onerror = () => {};
  } else {
    run.events.forEach(applyEvent);
    renderOutcome(run);
  }
}

function setStatus(run) {
  const el = document.getElementById("runStatus"); if (!el) return;
  el.innerHTML = stateChip(run) + (run.extractor ? ` <span class="pill" style="margin-left:6px">${esc(run.extractor)}</span>` : "");
}

function applyEvent(ev) {
  if (ev.stage === "done") { const e = document.getElementById("elapsed"); if (e) e.textContent = ev.title; return; }
  if (ev.stage === "error") {
    const d = document.getElementById("decision");
    if (d) d.innerHTML = `<div class="card-b"><b style="color:var(--bad)">${esc(ev.title)}</b><pre class="mono small" style="white-space:pre-wrap">${esc(ev.detail?.trace || "")}</pre></div>`;
    document.querySelectorAll(".step.running").forEach((s) => { s.className = "step fail"; });
    if (ev.detail?.retryable) return;
    return;
  }
  const li = document.querySelector(`.step[data-stage="${ev.stage}"]`); if (!li) return;
  const open = li.classList.contains("open");
  li.className = `step ${ev.status}${open ? " open" : ""}`;
  li.querySelector(".title").textContent = ev.status === "running" ? "Running…" : ev.title;
  li.querySelector(".title").classList.toggle("muted", ev.status === "running");
  if (ev.detail?.ms !== undefined) li.querySelector(".ms").textContent = ev.detail.ms < 1000 ? `${ev.detail.ms} ms` : `${(ev.detail.ms / 1000).toFixed(1)} s`;
  const icon = { pass: "✓", warn: "!", fail: "✕" }[ev.status] || "";
  li.querySelector(".dot").textContent = icon;
  if (ev.status !== "running") li.querySelector(".details").innerHTML = stageDetails(ev);
}

function stageDetails(ev) {
  const d = ev.detail || {};
  if (d.findings) return d.findings.map(findingHtml).join("");
  if (ev.stage === "ingest") return `<div class="small muted">Type: ${esc(d.kind)} · pages: ${d.pages} · sha256 ${esc(d.sha256)}…${d.ocr_error ? "<br>" + esc(d.ocr_error) : ""}</div>`;
  if (ev.stage === "extract") return `<div class="small muted">Extractor: ${esc(d.extractor)}${d.usage ? ` · ${d.usage.input_tokens} in / ${d.usage.output_tokens} out tokens` : ""}</div>
    ${(d.warnings || []).map((w) => `<div class="small" style="color:var(--hold)">${esc(w)}</div>`).join("")}`;
  if (ev.stage === "confidence" && d.fields) return ["invoice_number", "invoice_date", "vendor_gstin", "buyer_gstin", "total", "line_items", "bank_account_number", "po_reference"]
    .filter((k) => d.fields[k]).map((k) => `<div class="small" style="display:flex;justify-content:space-between;padding:2px 0"><span>${FIELD_LABELS[k]}</span>${confHtml(d.fields[k])}</div>`).join("");
  if (ev.stage === "decision" && d.decision) return `<div class="small">${esc(d.decision.headline)}</div>`;
  return "";
}

const findingHtml = (f) => `<div class="finding"><span class="sev ${f.severity}">${f.severity}</span><div>
  <div class="t">${esc(f.title)}</div>${f.detail ? `<div class="d">${esc(f.detail)}</div>` : ""}
  ${f.action && ["hold", "reject"].includes(f.severity) ? `<div class="a"><b>${esc(f.owner || "")}:</b> ${esc(f.action)}</div>` : ""}</div></div>`;
const confHtml = (v) => `<span class="conf ${v.band}"><i><b style="width:${Math.round(v.score * 100)}%"></b></i>${v.band}</span>`;

function renderOutcome(run) {
  const box = document.getElementById("decision");
  if (run.state === "failed") return renderFailed(run);
  if (run.status !== "done" || !run.result) return;
  const res = run.result, d = res.decision;
  const finalD = run.final_decision || d.decision;
  const inferred = res.findings.find((f) => f.code === "po_inferred");
  // Buttons come from the server's allowed actions for this state - never re-derived here.
  const act = Object.fromEntries((run.actions || []).map((a) => [a.name, a]));
  const canResolve = run.actions && run.actions.length > 0;

  box.className = `card decision ${finalD}`;
  box.innerHTML = `
    <div class="top">
      <div class="verdict">${finalD === "APPROVE" ? "Approve" : finalD === "HOLD" ? "Hold" : "Reject"}${run.final_decision ? ' <span class="small muted" style="font-weight:500">(reviewer decision; agent said ' + d.decision + ")</span>" : ""}</div>
      <div class="headline">${esc(d.headline.replace(/^(Approve|Hold|Reject) - /, ""))}</div>
      <div class="amounts">
        <div><span>Invoice total</span><b>${inr(run.total)}</b></div>
        <div><span>Pay now</span><b>${inr(run.final_decision ? run.payable : d.pay_now)}</b></div>
        ${d.approvable_now ? `<div><span>Approvable now (received qty)</span><b style="color:var(--ok)">${inr(d.approvable_now)}</b></div>` : ""}
        <div><span>Vendor</span><b style="font-size:14px">${esc(run.vendor_name || "—")}</b></div>
        <div><span>PO</span><b style="font-size:14px">${esc(run.po_number || "—")}</b></div>
      </div>
    </div>
    <div class="body">
      ${d.routing.length ? `<div><h3 style="margin-bottom:6px">What happens next</h3>${d.routing.map((r) => `<div class="route"><div class="owner">${esc(r.owner)}</div><ul>${r.actions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>`).join("")}</div>`
        : `<div><h3 style="margin-bottom:6px">What happens next</h3><div class="small">Queued for payment run. No one needs to touch this invoice.</div></div>`}
      ${d.vendor_message ? `<div><h3 style="margin-bottom:6px">Draft message to vendor</h3><div class="email">
        <div class="eh"><span><b>Subject:</b> ${esc(d.vendor_message.subject)}</span><button class="btn ghost sm" id="copyMail">Copy</button></div>
        <pre>${esc(d.vendor_message.body)}</pre></div></div>` : ""}
      ${run.resolution ? `<div class="resolution"><b>${esc(run.resolution.decision)}${run.resolution.partial ? " (partial: " + inr(run.resolution.amount) + ")" : ""}</b> by ${esc(run.resolution.by)} · ${when(run.resolution.at)}<br>${esc(run.resolution.note)}</div>` : ""}
      ${canResolve ? `<div class="resolve"><h3>Reviewer action</h3>
        <div class="actions">
          ${act.confirm_po ? `<button class="btn" id="confirmPo">Confirm ${esc(inferred.data.suggested_po)} &amp; re-run</button>` : ""}
          ${act.approve_partial ? `<button class="btn ok" id="partialBtn">Pay received portion · ${inr(d.approvable_now)}</button>` : ""}
          ${act.approve ? `<button class="btn ${act.approve_partial || act.confirm_po ? "ghost" : "ok"}" id="approveBtn">Approve in full</button>` : ""}
          ${act.reject ? `<button class="btn bad" id="rejectBtn">Reject</button>` : ""}
        </div>
        <textarea id="note" placeholder="Note for the audit trail (required to approve or reject a hold)"></textarea></div>` : ""}
    </div>`;

  const copy = document.getElementById("copyMail");
  if (copy) copy.onclick = () => { navigator.clipboard.writeText(`Subject: ${d.vendor_message.subject}\n\n${d.vendor_message.body}`); toast("Copied"); };
  const busy = (fn) => async (e) => {
    const btns = box.querySelectorAll(".resolve button"); btns.forEach((b) => b.disabled = true);
    try { await fn(); } catch (err) { toast(err.message); btns.forEach((b) => b.disabled = false); route(); }
  };
  const cp = document.getElementById("confirmPo");
  if (cp) cp.onclick = busy(async () => { const r = await post(`/api/runs/${run.id}/confirm-po`, { po_number: inferred.data.suggested_po, confirmed_by: "AP reviewer" }); location.hash = `#/run/${r.run_id}`; });
  const resolve = (decision, amount) => busy(async () => {
    await post(`/api/runs/${run.id}/resolve`, { decision, amount, note: document.getElementById("note").value });
    toast(`Marked ${decision}`); route();
  });
  const bind = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };
  bind("approveBtn", resolve("APPROVE"));
  bind("rejectBtn", resolve("REJECT"));
  bind("partialBtn", resolve("APPROVE", d.approvable_now));
  renderTabs(run);
}

function renderFailed(run) {
  const box = document.getElementById("decision");
  const retry = (run.actions || []).find((a) => a.name === "retry");
  box.className = "card decision REJECT";
  box.innerHTML = `<div class="top"><div class="verdict">Failed</div><div class="headline">${esc((run.headline || "").replace(/^Error: /, ""))}</div></div>
    <div class="body"><div class="small">Nothing was decided and nothing was booked against any PO.</div>
    ${retry ? `<div class="actions"><button class="btn" id="retryBtn">Retry this invoice</button></div>` : ""}</div>`;
  const b = document.getElementById("retryBtn");
  if (b) b.onclick = async () => { b.disabled = true; try { const r = await post(`/api/runs/${run.id}/retry`); location.hash = `#/run/${r.run_id}`; } catch (e) { toast(e.message); b.disabled = false; } };
}

function renderTabs(run) {
  const res = run.result, card = document.getElementById("tabsCard");
  const f = res.extraction.fields, conf = res.confidence.fields;
  card.style.display = "";
  const fieldRows = Object.keys(FIELD_LABELS).map((k) => {
    const c = conf[k]; if (!c) return "";
    let val = k === "line_items" ? `${(f.line_items || []).length} lines` : f[k];
    if (["total", "subtotal", "cgst", "sgst", "igst"].includes(k) && val != null) val = inr(val);
    return `<tr><td><b>${FIELD_LABELS[k]}</b></td><td class="mono">${esc(val ?? "—")}</td><td>${confHtml(c)}<div class="why">${esc(c.reasons.join(" · "))}</div></td></tr>`;
  }).join("");
  const items = (f.line_items || []).map((it, i) => `<tr><td>${i + 1}</td><td>${esc(it.description)}</td><td class="mono">${esc(it.hsn || "")}</td>
    <td class="num">${it.quantity ?? "—"}</td><td class="num">${inr(it.unit_price)}</td><td class="num">${it.tax_rate != null ? it.tax_rate + "%" : "—"}</td><td class="num">${inr(it.amount)}</td></tr>`).join("");
  const lines = (res.match_lines || []).map((l) => `<tr>
      <td>${l.invoice_line}</td><td>${esc(l.description)}${l.po_desc ? `<div class="why">→ PO line ${l.po_line}: ${esc(l.po_desc)} (match ${(l.match * 100).toFixed(0)}%)</div>` : ""}</td>
      <td class="num">${l.qty}</td><td class="num">${l.po_qty ?? "—"}</td><td class="num">${l.received ?? "—"}</td><td class="num">${l.billed_before ?? "—"}</td>
      <td class="num">${inr(l.rate)}</td><td class="num">${inr(l.po_rate)}</td>
      <td class="${l.status === "ok" ? "status-ok" : "status-bad"}">${esc(l.status)}</td><td class="num">${inr(l.payable)}</td></tr>`).join("");
  const byStage = {};
  res.findings.forEach((x) => (byStage[x.stage] = byStage[x.stage] || []).push(x));
  const stageNames = Object.fromEntries(STAGES);
  const ev = res.extraction.evidence || {};

  card.innerHTML = `
    <div class="tabs"><div class="tab active" data-tab="review">Document &amp; extracted fields</div><div class="tab" data-tab="match">3-way match</div>
      <div class="tab" data-tab="checks">All checks (${res.findings.length})</div><div class="tab" data-tab="text">Source text</div></div>
    <div class="pane active" data-pane="review"><div class="review">
      <div class="doc"><div class="doc-h"><span class="small muted">${esc(run.filename)} · ${res.ingest.pages} page(s) · ${esc(res.ingest.kind)}</span><a class="small" href="/api/runs/${run.id}/file" target="_blank">Open PDF ↗</a></div>
        ${Array.from({ length: res.ingest.pages || 1 }, (_, i) => `<img src="/api/runs/${run.id}/page/${i}.png" alt="Invoice page ${i + 1}" />`).join("")}</div>
      <div style="overflow:auto;max-height:760px">
        <table class="fields"><thead><tr><th>Field</th><th>Value</th><th>Confidence &amp; why</th></tr></thead><tbody>${fieldRows}</tbody></table>
        <div style="padding:14px 12px 6px"><h3>Line items as read</h3></div>
        <table class="fields"><thead><tr><th>#</th><th>Description</th><th>HSN</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">GST</th><th class="num">Amount</th></tr></thead><tbody>${items || `<tr><td colspan="7" class="empty">None</td></tr>`}</tbody></table>
        ${Object.keys(ev).length ? `<div style="padding:14px 12px 6px"><h3>Evidence (where each value came from)</h3></div><div style="padding:0 12px 14px" class="small">${Object.entries(ev).filter(([k]) => FIELD_LABELS[k]).map(([k, v]) => `<div style="padding:3px 0"><b>${FIELD_LABELS[k]}:</b> <span class="ev">${esc(v)}</span></div>`).join("")}</div>` : ""}
      </div></div></div>
    <div class="pane" data-pane="match"><div class="table-wrap">${lines ? `<table><thead><tr><th>#</th><th>Invoice line → PO line</th><th class="num">Billed</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Billed before</th><th class="num">Rate</th><th class="num">PO rate</th><th>Status</th><th class="num">Payable incl. GST</th></tr></thead><tbody>${lines}</tbody></table>`
      : `<div class="card-b empty">No 3-way match: ${res.po ? "no lines" : "no purchase order resolved"}.</div>`}
      ${res.po_info?.candidates ? `<div class="card-b"><h3 style="margin-bottom:8px">PO candidates considered</h3><table><thead><tr><th>PO</th><th class="num">Score</th><th class="num">Line match</th><th class="num">Amount fit</th><th class="num">Order date referenced</th></tr></thead><tbody>
        ${res.po_info.candidates.map((c) => `<tr><td class="mono">${c.po_number}</td><td class="num"><b>${c.score.toFixed(2)}</b></td><td class="num">${c.line_coverage}</td><td class="num">${c.amount_fit}</td><td class="num">${c.date_reference ? "yes" : "no"}</td></tr>`).join("")}</tbody></table></div>` : ""}
    </div></div>
    <div class="pane" data-pane="checks">${Object.entries(byStage).map(([s, xs]) => `<div class="stage-group"><h3>${stageNames[s] || s}</h3>${xs.map(findingHtml).join("")}</div>`).join("")}</div>
    <div class="pane" data-pane="text"><pre class="mono small" style="padding:16px;margin:0;white-space:pre-wrap;max-height:640px;overflow:auto">${esc(res.source_text || "(no text)")}</pre></div>`;
  card.querySelectorAll(".tab").forEach((t) => t.onclick = () => {
    card.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
    card.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === t.dataset.tab));
  });
}

// ---------------------------------------------------------------- reference
async function renderReference() {
  const r = await api("/api/reference");
  const vendor = Object.fromEntries(r.vendors.map((v) => [v.vendor_id, v]));
  const p = r.policy;
  $app.innerHTML = `
  <div class="page-head"><div><h1>Procurement data</h1><p>The system of record the agent checks invoices against. In production this would be the ERP (vendor master, PO and GRN tables).</p></div></div>
  <div class="grid g-2" style="margin-bottom:16px">
    <div class="card"><div class="card-h"><h2>Buyer entity</h2></div><div class="card-b kv">
      <span>Name</span><b>${esc(r.buyer.name)}</b><span>GSTIN</span><b class="mono">${r.buyer.gstin}</b><span>State</span><b>Karnataka (29)</b><span>Address</span><b style="font-weight:500">${esc(r.buyer.address)}</b></div></div>
    <div class="card"><div class="card-h"><h2>Policy &amp; tolerances</h2></div><div class="card-b kv">
      <span>Unit price above PO</span><b>≤ ${p.price_tolerance_pct}% auto-accepted</b>
      <span>Quantity billed</span><b>≤ received − already billed</b>
      <span>Arithmetic rounding</span><b>± ${inr(p.rounding_tolerance)}</b>
      <span>Possible duplicate window</span><b>${p.possible_duplicate_window_days} days, same vendor + amount</b>
      <span>Inferred PO</span><b>score ≥ ${p.implied_po_min_score}, margin ≥ ${p.implied_po_min_margin}, human confirms</b>
      <span>Auto-approval limit</span><b>${inr(p.auto_approve_limit)}</b>
      <span>Max invoice age</span><b>${p.max_invoice_age_days} days</b></div></div>
  </div>
  <div class="card" style="margin-bottom:16px"><div class="card-h"><h2>Vendor master</h2></div><div class="table-wrap"><table>
    <thead><tr><th>ID</th><th>Vendor</th><th>GSTIN</th><th>State</th><th>Bank account on file</th><th>Verified contact</th></tr></thead>
    <tbody>${r.vendors.map((v) => `<tr><td class="muted">${v.vendor_id}</td><td><b>${esc(v.name)}</b></td><td class="mono">${v.gstin}</td><td>${v.state}</td>
      <td class="mono">${v.bank.account_number} · ${v.bank.ifsc}</td><td class="small">${esc(v.contact)}</td></tr>`).join("")}</tbody></table></div></div>
  <div class="card"><div class="card-h"><h2>Purchase orders &amp; goods receipts</h2><span class="muted small">Billed = approved invoices so far</span></div>
    ${r.purchase_orders.map((po) => {
      const grns = r.goods_receipts.filter((g) => g.po_number === po.po_number);
      return `<div class="ref-po"><div class="h"><b class="mono">${po.po_number}</b><span>${esc(vendor[po.vendor_id].name)}</span>
        <span class="muted small">${po.date} · ${esc(po.buyer)} · GRN: ${grns.map((g) => g.grn_number + (g.remarks ? ` (${esc(g.remarks)})` : "")).join(", ") || "none"}</span></div>
        <table><thead><tr><th>Line</th><th>Item</th><th>HSN</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Billed</th><th class="num">Unit price</th><th class="num">GST</th></tr></thead><tbody>
        ${po.lines.map((l) => `<tr><td>${l.line}</td><td>${esc(l.description)}</td><td class="mono">${l.hsn}</td><td class="num">${l.qty} ${l.unit}</td>
          <td class="num" style="${l.received < l.qty ? "color:var(--hold);font-weight:600" : ""}">${l.received}</td><td class="num">${l.billed}</td><td class="num">${inr(l.unit_price)}</td><td class="num">${l.gst_rate}%</td></tr>`).join("")}
        </tbody></table></div>`;
    }).join("")}</div>`;
}

// ---------------------------------------------------------------- boot
document.getElementById("resetBtn").onclick = async () => {
  if (!confirm("Clear all runs and the billing ledger?")) return;
  await post("/api/reset"); toast("Demo reset"); location.hash = "#/"; route();
};
api("/api/health").then((h) => { const e = document.getElementById("extractor"); e.textContent = "Extractor: " + h.extractor; e.classList.add(h.llm ? "llm" : "rules"); });
route();
