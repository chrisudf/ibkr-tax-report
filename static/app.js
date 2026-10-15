"use strict";

const $ = (id) => document.getElementById(id);
const dropzone = $("dropzone");
const fileInput = $("file-input");
const fileList = $("file-list");
const analyzeBtn = $("analyze");
const errBox = $("error");
const MAX_UPLOAD_MB = Number(document.body.dataset.maxUploadMb) || 10;

let files = [];
let current = null;        // the result on screen: { token, deleteUrl, expiresAt }
let expiryTimer = null;

function fmtMoney(v) {
  if (v === "" || v === null || v === undefined) return "";
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  const s = Math.abs(n).toLocaleString("en-AU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return n < 0 ? `(${s})` : s;
}
function fmtSize(b) {
  return b > 1024 * 1024 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB";
}

function renderFiles() {
  fileList.innerHTML = "";
  files.forEach((f, i) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = f.name;
    const size = document.createElement("span");
    size.className = "size";
    size.textContent = fmtSize(f.size);
    const rm = document.createElement("button");
    rm.textContent = "✕";
    rm.title = "Remove";
    rm.onclick = () => { files.splice(i, 1); renderFiles(); };
    li.append(name, size, rm);
    fileList.appendChild(li);
  });
  analyzeBtn.disabled = files.length === 0;
}

function addFiles(list) {
  for (const f of list) {
    if (!/\.csv$/i.test(f.name)) {
      showError(`${f.name} is not a CSV file. This tool reads only the CSV export of an IBKR ` +
        `Activity Statement or Realized Summary, not the PDF, HTML or Excel versions. Run the ` +
        `statement again in the IBKR portal with CSV as the format and add that file ` +
        `(see "Exporting from IBKR" in the README).`);
      continue;
    }
    if (!files.some((x) => x.name === f.name && x.size === f.size)) files.push(f);
  }
  renderFiles();
}

function showError(msg) {
  errBox.textContent = msg;
  errBox.hidden = false;
}

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") fileInput.click();
});
fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
["dragenter", "dragover"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) =>
  dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("drag"); }));
dropzone.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));

async function runReport(url, fd) {
  errBox.hidden = true;
  $("busy").hidden = false;
  analyzeBtn.disabled = true;
  $("demo").disabled = true;
  try {
    const resp = await fetch(url, { method: "POST", body: fd });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      // the reverse proxy answers 413 itself, without the app's JSON body
      if (resp.status === 413 && !data.error) {
        throw new Error(`Upload too large: the limit is ${MAX_UPLOAD_MB} MB per request.`);
      }
      throw new Error(data.error || `server error (${resp.status})`);
    }
    renderResults(data);
  } catch (e) {
    showError(e.message);
  } finally {
    $("busy").hidden = true;
    analyzeBtn.disabled = files.length === 0;
    $("demo").disabled = false;
  }
}

analyzeBtn.addEventListener("click", () => {
  const total = files.reduce((n, f) => n + f.size, 0);
  if (total > MAX_UPLOAD_MB * 1024 * 1024) {
    showError(`These files add up to ${fmtSize(total)}; the limit is ${MAX_UPLOAD_MB} MB per ` +
      `report. A year's Activity Statement CSV is usually a few hundred KB — check that ` +
      `these are the CSV statement exports.`);
    return;
  }
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f, f.name));
  if ($("fy").value) fd.append("fy", $("fy").value);
  fd.append("entity", $("entity").value);
  if ($("carried").value) fd.append("carried_losses", $("carried").value);
  runReport("/api/analyze", fd);
});

// The sample statements are on the server: nothing is uploaded. The year is
// left to auto-detect so a value typed for the user's own files does not apply.
$("demo").addEventListener("click", () => {
  const fd = new FormData();
  fd.append("entity", $("entity").value);
  if ($("carried").value) fd.append("carried_losses", $("carried").value);
  runReport("/api/demo", fd);
});

// ---------------------------------------------------------------- retention

function setDownloadsGone(message) {
  ["dl-pdf", "dl-csv", "dl-zip"].forEach((id) => {
    const a = $(id);
    a.removeAttribute("href");
    a.classList.add("disabled");
    a.setAttribute("aria-disabled", "true");
  });
  $("delete-results").hidden = true;
  $("expiry-note").textContent = message;
  $("expiry-note").classList.add("gone");
  if (expiryTimer) clearTimeout(expiryTimer);
  expiryTimer = null;
}

function showRetention(data) {
  if (expiryTimer) clearTimeout(expiryTimer);
  ["dl-pdf", "dl-csv", "dl-zip"].forEach((id) => {
    $(id).classList.remove("disabled");
    $(id).removeAttribute("aria-disabled");
  });
  $("delete-results").hidden = false;
  $("delete-results").disabled = false;
  $("expiry-note").classList.remove("gone");
  const expiresAt = new Date(Date.now() + data.expires_in * 1000);
  current = { token: data.token, deleteUrl: data.delete_url, expiresAt };
  const hhmm = expiresAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("expiry-note").textContent =
    `The report files are kept on the server until ${hhmm} (${Math.round(data.expires_in / 60)} ` +
    `minutes) and then deleted automatically. Download what you need before then.`;
  const token = data.token;
  expiryTimer = setTimeout(() => {
    if (current && current.token === token) {
      setDownloadsGone("These report files have expired and were deleted from the server. " +
        "Generate the report again to download them.");
    }
  }, data.expires_in * 1000);
}

$("delete-results").addEventListener("click", async () => {
  // A new report can render while this request is in flight; the outcome
  // then belongs to the old one and must not touch the page.
  const target = current;
  if (!target) return;
  const btn = $("delete-results");
  btn.disabled = true;
  try {
    const resp = await fetch(target.deleteUrl, { method: "DELETE" });
    if (current !== target) return;
    if (resp.ok || resp.status === 404) {
      setDownloadsGone("Deleted: the report files are no longer on the server. What is on " +
        "this page stays only in your browser until you close or reload it.");
      current = null;
    } else {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || `server error (${resp.status})`);
    }
  } catch (e) {
    if (current !== target) return;
    btn.disabled = false;
    showError(`Could not delete the results: ${e.message}`);
  }
});

// ---------------------------------------------------------------- results

const TABLE_DEFS = {
  closed_lots: { title: "Closed parcels", cols: ["category", "symbol", "qty", "open_date", "close_date", "days_held", "open_cash", "close_cash", "gain_native", "gain_aud", "short", "expiry", "discount_eligible", "note"] },
  d2_open: { title: "D2 open written options", cols: ["symbol", "write_date", "qty", "premium_native", "currency", "fx", "premium_aud", "expires", "after_fy_end"] },
  transfers: { title: "Assignments / exercises", cols: ["option", "kind", "date", "option_written", "qty", "premium_native", "folded_into"] },
  carry_forward: { title: "Carry-forward parcels", cols: ["category", "symbol", "qty", "acquired", "cost_native", "currency", "fx", "cost_aud"] },
  unmatched: { title: "Unmatched closes", cols: ["category", "symbol", "close_date", "qty", "close_cash", "ib_realized_pl", "gain_aud", "note"] },
};
const NUM_COLS = new Set(["qty", "open_cash", "close_cash", "gain_native", "gain_aud",
  "premium_native", "premium_aud", "cost_native", "cost_aud", "ib_realized_pl",
  "days_held", "fx", "my_qty", "stmt_qty", "my_cost", "stmt_cost", "mine", "ibkr", "diff"]);

function td(col, v) {
  const cell = document.createElement("td");
  if (NUM_COLS.has(col)) {
    cell.className = "num";
    cell.textContent = col === "fx" ? (v ? Number(v).toFixed(4) : "")
      : col === "days_held" || col === "qty" ? String(v)
      : fmtMoney(v);
    if (Number(v) < 0 && col !== "qty") cell.classList.add("neg");
  } else if (col === "note") {
    cell.className = "note";
    cell.textContent = v || "";
  } else {
    cell.textContent = v === null || v === undefined ? "" : String(v);
  }
  return cell;
}

function buildTable(cols, rows) {
  const wrap = document.createElement("div");
  wrap.className = "tbl-wrap";
  const table = document.createElement("table");
  table.className = "data";
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  cols.forEach((c) => {
    const th = document.createElement("th");
    th.textContent = c.replace(/_/g, " ");
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  const tbody = document.createElement("tbody");
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    cols.forEach((c) => tr.appendChild(td(c, r[c])));
    tbody.appendChild(tr);
  });
  table.append(thead, tbody);
  wrap.appendChild(table);
  return wrap;
}

function stat(label, value, opts = {}) {
  const div = document.createElement("div");
  div.className = "stat" + (opts.hero ? " hero" : "") + (opts.warn ? " warn" : "");
  const k = document.createElement("div");
  k.className = "k";
  k.textContent = label;
  const v = document.createElement("div");
  v.className = "v" + (Number(value) < 0 ? " neg" : "");
  v.textContent = fmtMoney(value);
  div.append(k, v);
  return div;
}

function renderResults(data) {
  const s = data.summary;
  $("results").hidden = false;
  // Show which year was inferred: an unnoticed wrong year makes every figure wrong
  // while the report itself still looks perfectly healthy.
  if (data.fy_detected && !data.demo) {
    const fyBox = $("fy");
    if (!fyBox.value) fyBox.value = data.fy_detected;
    const hint = $("fy-hint");
    hint.textContent = "auto-detected from the statement — change it if you are reporting a different year";
    hint.hidden = false;
  }
  $("res-title").textContent = `${data.meta.account} — ${data.meta.fy} (${s.entity})`;
  $("demo-badge").hidden = !data.demo;
  $("res-meta").textContent =
    `Statement period ${data.meta.period} · ${data.meta.fx_source} · generated ${data.meta.generated}`;
  $("dl-pdf").href = data.downloads.pdf;
  $("dl-csv").href = data.downloads.csv;
  $("dl-zip").href = data.downloads.zip;
  showRetention(data);

  const head = $("headline");
  head.innerHTML = "";
  head.append(
    stat("Net capital gain (18A)", s.net_capital_gain_18A, { hero: true }),
    stat("Total gains (18H)", s.total_capital_gains_18H),
    stat("Closed parcels net", s.closed_net),
    stat("D2 open written options", s.d2_open_written),
    stat("Losses applied", s.losses_applied_total),
    stat("Discount applied", s.discount_applied),
  );
  // Counted in 18A, but the uploaded data cannot show whether an assignment
  // after 30 June disregards it — keep it in view next to the headline.
  if (s.d2_outcome_unknown) {
    head.append(stat("D2 fate after 30 June unknown — see warnings", s.d2_outcome_unknown, { warn: true }));
  }
  const lbls = $("ato-labels");
  const oi = s.other_income;
  lbls.innerHTML = "";
  [["Carried forward (18V)", s.losses_carried_forward_18V],
   ["Dividends (gross)", oi.dividends_aud],
   ["Withholding tax", oi.withholding_tax_aud],
   ["Interest (net)", oi.interest_aud],
   ["Fees", oi.fees_aud],
   ["FX P/L (Div 775, income)", oi.forex_pl_aud],
   ["Deferred-basis alt.", s.deferred_alternative],
  ].forEach(([k, v]) => {
    const span = document.createElement("span");
    span.className = "lbl";
    const b = document.createElement("b");
    b.textContent = fmtMoney(v);
    span.append(k + ": ", b);
    lbls.appendChild(span);
  });

  const warn = data.warnings || [];
  $("warnings-card").hidden = warn.length === 0;
  $("warnings").innerHTML = "";
  warn.forEach((w, i) => {
    const li = document.createElement("li");
    li.textContent = w;
    if (i === 0 && s.d2_outcome_unknown) li.className = "lead";
    $("warnings").appendChild(li);
  });

  const flags = (data.amendment_flags || []).concat(data.cross_year_notes || []);
  $("flags-card").hidden = flags.length === 0;
  $("flags").innerHTML = "";
  flags.forEach((f) => {
    const li = document.createElement("li");
    li.textContent = f;
    $("flags").appendChild(li);
  });

  const rec = data.reconciliation;
  const okRows = rec.rows_mismatched === 0;
  $("recon-summary").innerHTML = "";
  const pill = document.createElement("span");
  pill.className = okRows ? "ok-pill" : "bad-pill";
  pill.textContent = `${rec.rows_ok}/${rec.rows_checked} closing rows match IBKR Realized P/L`;
  $("recon-summary").appendChild(pill);
  if (rec.positions_applicable) {
    const pill2 = document.createElement("span");
    pill2.className = rec.position_diffs.length === 0 ? "ok-pill" : "bad-pill";
    pill2.style.marginLeft = "8px";
    pill2.textContent = `${rec.positions_ok} year-end positions agree` +
      (rec.position_diffs.length ? `, ${rec.position_diffs.length} differences` : "");
    $("recon-summary").appendChild(pill2);
  }
  const rt = $("recon-tables");
  rt.innerHTML = "";
  if (rec.row_mismatches && rec.row_mismatches.length) {
    rt.appendChild(buildTable(["symbol", "date", "mine", "ibkr", "diff"], rec.row_mismatches));
  }
  if (rec.position_diffs && rec.position_diffs.length) {
    rt.appendChild(buildTable(["symbol", "my_qty", "stmt_qty", "my_cost", "stmt_cost", "note"],
      rec.position_diffs));
  }

  // tabs
  const tabs = $("tabs");
  const host = $("table-host");
  tabs.innerHTML = "";
  const keys = Object.keys(TABLE_DEFS).filter((k) => (data.tables[k].total || 0) > 0 || k === "closed_lots");
  const show = (key) => {
    [...tabs.children].forEach((b) => b.classList.toggle("active", b.dataset.key === key));
    host.innerHTML = "";
    const t = data.tables[key];
    host.appendChild(buildTable(TABLE_DEFS[key].cols, t.rows));
    if (t.total > t.rows.length) {
      const p = document.createElement("p");
      p.className = "tbl-more";
      p.textContent = `Showing first ${t.rows.length} of ${t.total} rows — the CSV/PDF downloads contain all rows.`;
      host.appendChild(p);
    }
  };
  keys.forEach((k) => {
    const b = document.createElement("button");
    b.dataset.key = k;
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = ` (${data.tables[k].total})`;
    b.textContent = TABLE_DEFS[k].title;
    b.appendChild(count);
    b.onclick = () => show(k);
    tabs.appendChild(b);
  });
  show("closed_lots");
  $("results").scrollIntoView({ behavior: "smooth" });
}
