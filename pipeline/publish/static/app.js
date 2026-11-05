/* Sales & Marketing Insights dashboard. Plain JS, no build step.
 * Talks only to the pipeline API (same origin). All labels coming from data are
 * inserted with textContent, never innerHTML. */
"use strict";

// ---------- small helpers ----------------------------------------------------
const $ = (s, r = document) => r.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") el.style.cssText = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function s(tag, attrs = {}) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  return el;
}
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const fmt = {
  count: (v) => v == null ? "–" : Math.round(v).toLocaleString("en-US"),
  usd: (v) => v == null ? "–" : "$" + Math.round(v).toLocaleString("en-US"),
  usdShort(v) {
    if (v == null) return "–";
    const a = Math.abs(v);
    if (a >= 1e6) return "$" + (v / 1e6).toFixed(a >= 1e7 ? 1 : 2).replace(/\.0+$/, "") + "M";
    if (a >= 1e4) return "$" + Math.round(v / 1e3) + "K";
    return "$" + Math.round(v).toLocaleString("en-US");
  },
  countShort(v) {
    if (v == null) return "–";
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (a >= 1e4) return Math.round(v / 1e3) + "K";
    return Math.round(v).toLocaleString("en-US");
  },
  pct: (v) => v == null ? "–" : (v * 100).toFixed(1) + "%",
  days: (v) => v == null ? "–" : Math.round(v) + " days",
};
const UNIT_FMT = { usd: fmt.usd, ratio: fmt.pct, count: fmt.count, days: fmt.days, correlation: (v) => v == null ? "–" : v.toFixed(2) };

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthLabel = (m, long = false) => `${MONTHS[+m.slice(5, 7) - 1]} ${long ? m.slice(0, 4) : "'" + m.slice(2, 4)}`;
function shiftMonth(m, n) {
  let y = +m.slice(0, 4), mo = +m.slice(5, 7) + n;
  while (mo <= 0) { y--; mo += 12; }
  while (mo > 12) { y++; mo -= 12; }
  return `${y}-${String(mo).padStart(2, "0")}`;
}
const rangeLabel = (a, b) => a === b ? monthLabel(a, true) : `${monthLabel(a, true)} – ${monthLabel(b, true)}`;

const SOURCE_NAMES = {
  ORGANIC_SEARCH: "Organic search", PAID_SEARCH: "Paid search", EMAIL_MARKETING: "Email marketing",
  SOCIAL_MEDIA: "Organic social", PAID_SOCIAL: "Paid social", REFERRALS: "Referrals",
  DIRECT_TRAFFIC: "Direct traffic", OFFLINE: "Offline sources", OTHER_CAMPAIGNS: "Other campaigns",
  AI_REFERRALS: "AI referrals", UNKNOWN: "Unknown",
};
const prettySource = (k) => SOURCE_NAMES[k] || k.toLowerCase().replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

// ---------- API --------------------------------------------------------------
function token() { try { return sessionStorage.getItem("dash_token"); } catch { return null; } }
async function askToken() {
  const dlg = $("#token-dialog");
  dlg.showModal();
  await new Promise((r) => dlg.addEventListener("close", r, { once: true }));
  try { sessionStorage.setItem("dash_token", $("#token-input").value); } catch { /* ignore */ }
}
async function api(path) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const t = token();
    const res = await fetch(path, { headers: t ? { Authorization: `Bearer ${t}` } : {} });
    if (res.status === 401) { await askToken(); continue; }
    if (!res.ok) throw new Error(`${path}: ${res.status}`);
    return res.json();
  }
  throw new Error("not authorized");
}

// ---------- state ------------------------------------------------------------
const state = {
  manifest: null, opts: null, metrics: {}, findings: null,
  start: null, end: null, compare: true, summary: null,
  page: "overview",
};
const MIN_SAMPLE = 20;

// Color follows the entity (fixed slot by its position in the full list), never its rank.
function colorFor(dim, key) {
  if (key === "Other") return cssVar("--s-other");
  if (dim === "none" || key === "All") return cssVar("--s1");
  const list = dim === "campaign" ? state.opts.campaigns.map((c) => c.id)
    : dim === "pipeline" ? state.opts.pipelines.map((p) => p.id)
    : dim === "source" ? state.opts.sources : state.opts.industries;
  const i = list.indexOf(key);
  return i >= 0 && i < 8 ? cssVar(`--s${i + 1}`) : cssVar("--s-other");
}
function labelFor(dim, key) {
  if (dim === "campaign") return (state.opts.campaigns.find((c) => c.id === key) || {}).name || key;
  if (dim === "source") return prettySource(key);
  if (dim === "pipeline") return (state.opts.pipelines.find((p) => p.id === key) || {}).name || key;
  if (key === "All") return "Company total";
  return key;
}

// ---------- charts -----------------------------------------------------------
function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0, 1];
  const raw = max / count, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= raw);
  const ticks = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(v);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

function placeTooltip(chartEl, tip, x, y) {
  tip.style.display = "block";
  const w = tip.offsetWidth, cw = chartEl.clientWidth;
  tip.style.left = Math.max(0, Math.min(cw - w, x + 14)) + "px";
  tip.style.top = Math.max(0, y - tip.offsetHeight - 10) + "px";
}

function tableToggle(container, buildTable) {
  const wrap = h("div", { class: "table-wrap", hidden: true });
  const btn = h("button", { class: "linkbtn", type: "button" }, "Show as table");
  btn.addEventListener("click", () => {
    wrap.hidden = !wrap.hidden;
    btn.textContent = wrap.hidden ? "Show as table" : "Hide table";
    if (!wrap.hidden) wrap.replaceChildren(buildTable());
  });
  container.append(h("div", { style: "margin-top:8px" }, btn), wrap);
}

/** Line chart with crosshair tooltip. series: [{key,label,color,values}] */
function lineChart(el, { months, series, format, short, height = 260, left }) {
  el.classList.remove("loading");
  el.replaceChildren();
  if (!months.length || !series.length) { el.append(h("p", { class: "empty" }, "No data for this period.")); return; }
  const hidden = el._hidden || (el._hidden = new Set());
  const visible = series.filter((sr) => !hidden.has(sr.key));
  const W = Math.max(el.clientWidth, 300), H = height;
  const max = Math.max(1, ...visible.flatMap((sr) => sr.values));
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1];
  const L = left || Math.max(40, 10 + 7 * Math.max(...ticks.map((t) => short(t).length)));
  const R = 16, T = 10, B = 28, pw = W - L - R, ph = H - T - B;
  const x = (i) => L + (months.length === 1 ? pw / 2 : (i * pw) / (months.length - 1));
  const y = (v) => T + ph - (v / top) * ph;

  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
    "aria-label": `${series.map((sr) => sr.label).join(", ")} by month` });
  const grid = s("g", { class: "grid" });
  for (const t of ticks) {
    grid.append(s("line", { x1: L, x2: W - R, y1: y(t), y2: y(t) }));
    const tx = s("text", { x: L - 8, y: y(t) + 4, "text-anchor": "end" }); tx.textContent = short(t); svg.append(tx);
  }
  svg.prepend(grid);
  const every = Math.ceil(months.length / Math.max(2, Math.floor(pw / 64)));
  months.forEach((m, i) => {
    const last = months.length - 1;
    if (i !== last && (i % every || last - i < every)) return;
    const tx = s("text", { x: x(i), y: H - 8, "text-anchor": "middle" }); tx.textContent = monthLabel(m); svg.append(tx);
  });

  for (const sr of visible) {
    const d = sr.values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    if (visible.length === 1) {
      svg.append(s("path", { d: `${d}L${x(sr.values.length - 1)},${y(0)}L${x(0)},${y(0)}Z`, fill: sr.color, opacity: 0.1 }));
    }
    svg.append(s("path", { d, class: "line", stroke: sr.color }));
    const li = sr.values.length - 1;
    svg.append(s("circle", { cx: x(li), cy: y(sr.values[li]), r: 4, fill: sr.color, stroke: cssVar("--surface"), "stroke-width": 2 }));
  }

  // Hover / keyboard layer
  const cross = s("line", { class: "crosshair", y1: T, y2: T + ph, visibility: "hidden" });
  const dots = s("g");
  svg.append(cross, dots);
  const hit = s("rect", { x: L, y: T, width: pw, height: ph, fill: "transparent", tabindex: 0 });
  svg.append(hit);
  el.append(svg);
  const tip = h("div", { class: "tooltip" });
  el.append(tip);

  const show = (i) => {
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    dots.replaceChildren(...visible.map((sr) => s("circle", { cx: x(i), cy: y(sr.values[i]), r: 4, fill: sr.color,
      stroke: cssVar("--surface"), "stroke-width": 2 })));
    const rows = [...visible].sort((a, b) => b.values[i] - a.values[i]).map((sr) =>
      h("div", { class: "t-row" }, h("span", { class: "lk", style: `background:${sr.color}` }), sr.label, h("strong", {}, format(sr.values[i]))));
    tip.replaceChildren(h("div", { class: "t-head" }, monthLabel(months[i], true)), ...rows);
    const r = svg.getBoundingClientRect();
    placeTooltip(el, tip, (x(i) / W) * r.width, (Math.min(...visible.map((sr) => y(sr.values[i]))) / H) * r.height);
  };
  const hide = () => { cross.setAttribute("visibility", "hidden"); dots.replaceChildren(); tip.style.display = "none"; };
  let cur = months.length - 1;
  hit.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    cur = Math.max(0, Math.min(months.length - 1, Math.round(((px - L) / pw) * (months.length - 1))));
    show(cur);
  });
  hit.addEventListener("pointerleave", hide);
  hit.addEventListener("focus", () => show(cur));
  hit.addEventListener("blur", hide);
  hit.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") cur = Math.max(0, cur - 1);
    else if (e.key === "ArrowRight") cur = Math.min(months.length - 1, cur + 1);
    else return;
    e.preventDefault(); show(cur);
  });

  if (series.length > 1) {
    const legend = h("div", { class: "legend" });
    for (const sr of series) {
      legend.append(h("button", { type: "button", "aria-pressed": String(!hidden.has(sr.key)), title: "Click to show/hide",
        onclick: () => { hidden.has(sr.key) ? hidden.delete(sr.key) : hidden.add(sr.key); lineChart(el, { months, series, format, short, height, left }); } },
        h("span", { class: "lk", style: `background:${sr.color}` }), sr.label));
    }
    el.append(legend);
  }
  tableToggle(el, () => h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Month"), ...series.map((sr) => h("th", {}, sr.label)))),
    h("tbody", {}, ...months.map((m, i) => h("tr", {}, h("td", {}, monthLabel(m, true)), ...series.map((sr) => h("td", {}, format(sr.values[i]))))))));
}

/** Horizontal bar chart. rows: [{key,label,color,value,note}] */
function barChart(el, { rows, format }) {
  el.replaceChildren();
  rows = rows.filter((r) => r.value != null).sort((a, b) => b.value - a.value);
  if (!rows.length) { el.append(h("p", { class: "empty" }, "No data for this period.")); return; }
  const W = Math.max(el.clientWidth, 300);
  const labelW = Math.min(220, W * 0.38), valueW = 90, bh = 20, gap = 14, T = 4;
  const H = T + rows.length * (bh + gap);
  const max = Math.max(...rows.map((r) => Math.max(r.value, r.hi ?? 0)), 0) || 1;
  const pw = W - labelW - valueW;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Bar chart" });
  svg.append(s("line", { class: "baseline", x1: labelW, x2: labelW, y1: 0, y2: H - gap / 2 }));
  const tip = h("div", { class: "tooltip" });
  rows.forEach((r, i) => {
    const y0 = T + i * (bh + gap);
    const lab = s("text", { x: labelW - 10, y: y0 + bh / 2 + 4, "text-anchor": "end", class: "label" });
    let text = r.label;
    const maxChars = Math.floor((labelW - 14) / 7);
    if (text.length > maxChars) text = text.slice(0, maxChars - 1) + "…";
    lab.textContent = text;
    svg.append(lab);
    const w = Math.max(0, (r.value / max) * pw);
    if (w > 0) {
      const rr = Math.min(4, w);
      svg.append(s("path", { fill: r.color,
        d: `M${labelW},${y0}H${labelW + w - rr}Q${labelW + w},${y0} ${labelW + w},${y0 + rr}V${y0 + bh - rr}Q${labelW + w},${y0 + bh} ${labelW + w - rr},${y0 + bh}H${labelW}Z` }));
    }
    let tipX = labelW + w;
    if (r.lo != null && r.hi != null) {
      const x1 = labelW + (Math.max(0, r.lo) / max) * pw, x2 = labelW + (r.hi / max) * pw, ym = y0 + bh / 2;
      svg.append(s("line", { class: "whisker", x1, x2, y1: ym, y2: ym }),
        s("line", { class: "whisker", x1, x2: x1, y1: ym - 5, y2: ym + 5 }),
        s("line", { class: "whisker", x1: x2, x2, y1: ym - 5, y2: ym + 5 }));
      tipX = Math.max(tipX, x2);
    }
    const val = s("text", { x: tipX + 8, y: y0 + bh / 2 + 4, class: "value" });
    val.textContent = format(r.value) + (r.note ? " *" : "");
    svg.append(val);
    const hit = s("rect", { x: 0, y: y0 - gap / 2, width: W, height: bh + gap, fill: "transparent", tabindex: 0 });
    const showTip = () => {
      tip.replaceChildren(h("div", { class: "t-head" }, r.label),
        h("div", { class: "t-row" }, h("span", { class: "lk", style: `background:${r.color}` }), "Value", h("strong", {}, format(r.value))),
        r.lo != null ? h("div", { class: "muted", style: "margin-top:4px" }, `Likely between ${format(r.lo)} and ${format(r.hi)}`) : null,
        r.flag ? h("div", { style: "margin-top:4px" }, r.flag) : null,
        r.note ? h("div", { class: "muted", style: "margin-top:4px" }, r.note) : null);
      const b = svg.getBoundingClientRect();
      placeTooltip(el, tip, ((labelW + w) / W) * b.width, (y0 / H) * b.height);
    };
    hit.addEventListener("pointerenter", showTip);
    hit.addEventListener("focus", showTip);
    hit.addEventListener("pointerleave", () => (tip.style.display = "none"));
    hit.addEventListener("blur", () => (tip.style.display = "none"));
    svg.append(hit);
  });
  el.append(svg, tip);
  if (rows.some((r) => r.lo != null)) el.append(h("p", { class: "muted" }, "The thin line on each bar shows the likely range. Wide line = less certain."));
  if (rows.some((r) => r.note)) el.append(h("p", { class: "muted" }, "* Small group. Treat as a hint, not a conclusion."));
}

// ---------- period handling --------------------------------------------------
function computePeriod() {
  const months = state.opts.months, first = months[0], last = months[months.length - 1];
  const p = $("#preset").value;
  let start = first, end = last;
  if (p === "custom") { start = $("#from").value; end = $("#to").value; if (start > end) [start, end] = [end, start]; }
  else if (p === "ytd") start = last.slice(0, 4) + "-01";
  else if (p !== "all") start = shiftMonth(last, -(+p - 1));
  if (start < first) start = first;
  state.start = start; state.end = end;
  state.compare = $("#compare").checked;
  $("#from-wrap").hidden = $("#to-wrap").hidden = p !== "custom";
  $("#scope").textContent = `Showing ${rangeLabel(start, end)}`;
  try { localStorage.setItem("dash_period", JSON.stringify({ p, from: $("#from").value, to: $("#to").value, c: state.compare })); } catch { /* ignore */ }
}

function change(cur, prev, kind) {
  // kind: "pct" -> relative change; "pts" -> percentage-point change for rates
  if (cur == null || prev == null) return null;
  if (kind === "pts") return { v: (cur - prev) * 100, text: `${cur - prev >= 0 ? "+" : ""}${((cur - prev) * 100).toFixed(1)} pts` };
  if (!prev) return null;
  const r = (cur - prev) / Math.abs(prev);
  return { v: r, text: `${r >= 0 ? "+" : ""}${(r * 100).toFixed(0)}%` };
}

// ---------- overview ---------------------------------------------------------
const TILES = [
  { key: "new_contacts", label: "New contacts", f: fmt.count, hint: "People added to HubSpot in the period." },
  { key: "deals_created", label: "Deals created", f: fmt.count, hint: "New deals opened in the period." },
  { key: "deals_won", label: "Deals won", f: fmt.count, hint: "Deals marked Closed won with a close date in the period." },
  { key: "won_value", label: "Won deal value", f: fmt.usdShort, hint: "Total Amount on deals won in the period. Booked value, not cash received." },
  { key: "win_rate", label: "Win rate", f: fmt.pct, kind: "pts", hint: "Of deals that finished (won or lost) in the period, the share won." },
  { key: "sessions", label: "Website visits", f: fmt.countShort, hint: "Website sessions in the period.", needsTraffic: true },
];

function renderTiles() {
  const sum = state.summary, wrap = $("#tiles");
  wrap.replaceChildren();
  const first = state.opts.months[0];
  const prev = sum.previous;
  const prevPartial = prev && prev.start < first;
  for (const t of TILES) {
    if (t.needsTraffic && !state.opts.has_traffic) continue;
    const cur = sum.activity[t.key];
    let deltaEl = h("div", { class: "delta" }, " ");
    if (state.compare && prev) {
      if (prev.end < first) deltaEl = h("div", { class: "delta" }, "No earlier data to compare");
      else {
        const c = change(cur, prev.activity[t.key], t.kind || "pct");
        if (c) {
          const good = c.v > 0, flat = Math.abs(c.v) < (t.kind === "pts" ? 0.5 : 0.005);
          deltaEl = h("div", { class: `delta ${flat ? "" : good ? "good" : "bad"}` },
            `${flat ? "≈" : good ? "▲" : "▼"} ${c.text} vs ${rangeLabel(prev.start, prev.end)}${prevPartial ? " (partial)" : ""}`);
        }
      }
    }
    wrap.append(h("div", { class: "tile" },
      h("div", { class: "label" }, t.label, h("span", { class: "hint", title: t.hint, tabindex: 0, "aria-label": t.hint }, "?")),
      h("div", { class: "value" }, t.f(cur)), deltaEl));
  }
  const lastMonth = state.opts.months[state.opts.months.length - 1];
  $("#recent-note").hidden = state.end < shiftMonth(lastMonth, -2);
}

const MEASURE = {
  won_value: { label: "Won deal value", f: fmt.usd, short: fmt.usdShort },
  deals_won: { label: "Deals won", f: fmt.count, short: fmt.countShort },
  new_contacts: { label: "New contacts", f: fmt.count, short: fmt.countShort },
  deals_created: { label: "Deals created", f: fmt.count, short: fmt.countShort },
  sessions: { label: "Website visits", f: fmt.count, short: fmt.countShort },
};

async function seriesChart(el, measure, by, opts = {}) {
  el.classList.add("loading");
  const data = await api(`/runs/latest/explore/series?measure=${measure}&by=${by}&start=${state.start}&end=${state.end}`);
  const m = MEASURE[measure];
  lineChart(el, {
    months: data.months, format: m.f, short: m.short, ...opts,
    series: data.series.map((sr) => ({ key: sr.key, label: labelFor(data.by, sr.key), color: colorFor(data.by, sr.key), values: sr.values })),
  });
  return data;
}

async function renderTrend() {
  const measure = $("#trend-measure").value;
  let by = $("#trend-by").value;
  if (measure === "sessions" && !["none", "source"].includes(by)) by = "none";
  if (measure === "new_contacts" && by === "pipeline") by = "none";
  $("#trend-title").textContent = `${MEASURE[measure].label}, month by month`;
  $("#trend-sub").textContent = by === "none" ? "" : "Click a name in the key to hide or show it. Smaller groups are combined into \"Other\".";
  await seriesChart($("#trend-chart"), measure, by);
}

// ---------- findings ---------------------------------------------------------
const STATUS = {
  verified: ["Checked", "ok"], revised: ["Reworded by reviewer", "rev"], rejected: ["Not shown", "no"], unreviewed: ["Not yet reviewed", ""],
};
const TYPE = { pattern: "Pattern", anomaly: "Something unusual", recommendation: "Suggestion", data_quality: "Data gap" };

function findingCard(f, { compact = false } = {}) {
  const [statusLabel, statusClass] = STATUS[f.status] || [f.status, ""];
  const card = h("article", { class: "finding" },
    h("div", { class: "row" }, h("h3", {}, f.title)),
    h("div", { class: "row" }, h("span", { class: `pill ${statusClass}` }, statusLabel),
      h("span", { class: "pill" }, TYPE[f.type] || f.type), h("span", { class: "pill" }, `Confidence: ${f.confidence}`)),
    compact ? null : h("p", {}, f.claim));
  if (f.recommendation) card.append(h("p", { class: "rec" }, h("strong", {}, "Suggested next step: "), f.recommendation));
  if (compact) {
    card.append(h("details", {}, h("summary", {}, "Read the full finding"), h("p", { style: "margin-top:8px" }, f.claim)));
    return card;
  }
  const det = h("details", {}, h("summary", {}, "See the numbers and caveats"));
  const ev = (f.evidence || []).map((id) => {
    const m = state.metrics[id];
    return h("li", {}, m ? `${m.description}: ${(UNIT_FMT[m.unit] || String)(m.value)} (based on ${fmt.count(m.n)})` : id);
  });
  det.append(h("p", { class: "muted", style: "margin-top:8px" }, "Numbers this is based on"), h("ul", {}, ev));
  if (f.caveats && f.caveats.length) det.append(h("p", { class: "muted", style: "margin-top:8px" }, "Keep in mind"), h("ul", {}, f.caveats.map((c) => h("li", {}, c))));
  if (f.status_reason) det.append(h("p", { class: "muted", style: "margin-top:8px" }, f.status === "rejected" ? "Why it was held back" : "Reviewer's note"), h("ul", {}, h("li", {}, f.status_reason)));
  if (f.original_claim) det.append(h("p", { class: "muted", style: "margin-top:8px" }, "Original wording"), h("ul", {}, h("li", {}, f.original_claim)));
  card.append(det);
  return card;
}

function renderFindings() {
  const data = state.findings;
  if (!data || !data.findings.length) {
    $("#ai-summary").textContent = "No analysis yet for the latest data run.";
    $("#all-findings").replaceChildren(h("p", { class: "empty" }, "No findings yet."));
    return;
  }
  $("#ai-summary").textContent = data.summary || "";
  const published = data.findings.filter((f) => f.status === "verified" || f.status === "revised");
  $("#top-findings").replaceChildren(...published.slice(0, 4).map((f) => findingCard(f, { compact: true })));
  const which = $("#finding-filter").value;
  const list = which === "all" ? data.findings : which === "rejected" ? data.findings.filter((f) => f.status === "rejected") : published;
  $("#all-findings").replaceChildren(...(list.length ? list.map((f) => findingCard(f)) : [h("p", { class: "empty" }, "None.")]));
}

// ---------- breakdown pages --------------------------------------------------
const BREAKDOWNS = {
  campaigns: {
    dim: "campaign", prefix: "campaign.", title: "Campaigns",
    intro: "How each campaign performs. Counts the people who first came in through the campaign during the selected period, and every deal they've produced since.",
    keys: () => state.opts.campaigns.map((c) => c.id),
    cols: [
      { m: "contacts", label: "Contacts", f: fmt.count },
      { m: "contact_to_deal_rate", label: "Became deals", f: fmt.pct, kind: "pts", tested: "share of contacts becoming deals" },
      { m: "deals_won", label: "Deals won", f: fmt.count },
      { m: "win_rate", label: "Win rate", f: fmt.pct, kind: "pts", sample: true, tested: "win rate" },
      { m: "revenue_won", label: "Won value", f: fmt.usd, short: fmt.usdShort },
      { m: "revenue_per_contact", label: "Value per contact", f: fmt.usd, short: fmt.usdShort, tested: "value per contact" },
    ],
    defaultBar: "revenue_per_contact",
  },
  sources: {
    dim: "source", prefix: "source.", title: "Lead sources",
    intro: "Where contacts originally came from (HubSpot's Original source). Counts people who came in during the selected period and every deal they've produced since.",
    keys: () => state.opts.sources,
    cols: [
      { m: "contacts", label: "Contacts", f: fmt.count },
      { m: "deals_won", label: "Deals won", f: fmt.count },
      { m: "contact_to_won_rate", label: "Contacts who became wins", f: fmt.pct, kind: "pts", tested: "share of contacts becoming wins" },
      { m: "revenue_won", label: "Won value", f: fmt.usd, short: fmt.usdShort },
      { m: "revenue_per_contact", label: "Value per contact", f: fmt.usd, short: fmt.usdShort, tested: "value per contact" },
      { m: "median_days_to_won", label: "Typical days to win", f: fmt.days, lowerIsBetter: true, sample: true },
    ],
    defaultBar: "revenue_won",
  },
  industries: {
    dim: "industry", prefix: "industry.", title: "Industries",
    intro: "Results by the industry of the company on the deal. Counts deals from people who came in during the selected period.",
    keys: () => state.opts.industries,
    cols: [
      { m: "win_rate", label: "Win rate", f: fmt.pct, kind: "pts", sample: true, tested: "win rate" },
      { m: "revenue_won", label: "Won value", f: fmt.usd, short: fmt.usdShort },
    ],
    defaultBar: "revenue_won",
  },
};

/** Plain-language list of measures where this row is clearly above/below the rest. */
function standsOut(cfg, testIdx, key) {
  const out = [];
  for (const c of cfg.cols) {
    if (!c.tested) continue;
    const t = (testIdx[key] || {})[c.m];
    if (!t || !t.significant) continue;
    const up = t.value > 0;
    out.push({ up, text: `${up ? "Higher" : "Lower"} ${c.tested}` });
  }
  return out;
}

function cohortIndex(list, prefix) {
  const out = {};
  for (const m of list || []) {
    if (!m.id.startsWith(prefix)) continue;
    const rest = m.id.slice(prefix.length), dot = rest.lastIndexOf(".");
    (out[rest.slice(0, dot)] ||= {})[rest.slice(dot + 1)] = m;
  }
  return out;
}

function buildBreakdownPage(name) {
  const cfg = BREAKDOWNS[name], page = $(`#page-${name}`);
  const barSel = h("select", { "aria-label": "Compare by" }, cfg.cols.map((c) => h("option", { value: c.m, selected: c.m === cfg.defaultBar }, c.label)));
  const trendSel = h("select", { "aria-label": "Measure over time" },
    ["new_contacts", "deals_won", "won_value"].map((k) => h("option", { value: k }, MEASURE[k].label)));
  page.replaceChildren(
    h("div", { class: "page-intro" }, h("h2", {}, cfg.title), h("p", { class: "sub" }, cfg.intro)),
    h("div", { class: "note", "data-role": "cohort-note", hidden: true },
      "People who came in recently haven't had time to turn into deals yet, so recent periods will look weaker here."),
    h("div", { class: "grid-2" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Side by side"), h("div", { class: "controls" }, barSel)),
        h("div", { class: "chart", "data-role": "bars" })),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Over time"), h("div", { class: "controls" }, trendSel)),
        h("div", { class: "chart", "data-role": "trend" }))),
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", {}, h("h3", {}, "Full table"),
      h("p", { class: "muted", "data-role": "table-sub" }))), h("div", { class: "table-wrap", "data-role": "table" })));
  barSel.addEventListener("change", () => renderBreakdown(name));
  trendSel.addEventListener("change", () => renderBreakdown(name, { trendOnly: true }));
  page._barSel = barSel; page._trendSel = trendSel;
}

function renderBreakdown(name, { trendOnly = false } = {}) {
  const cfg = BREAKDOWNS[name], page = $(`#page-${name}`);
  if (!page._barSel) buildBreakdownPage(name);
  const trendEl = page.querySelector('[data-role="trend"]');
  seriesChart(trendEl, page._trendSel.value, cfg.dim, { height: 240 });
  if (trendOnly) return;

  const lastMonth = state.opts.months[state.opts.months.length - 1];
  page.querySelector('[data-role="cohort-note"]').hidden = state.end < shiftMonth(lastMonth, -3);
  const cur = cohortIndex(state.summary.cohort, cfg.prefix);
  const prev = state.compare && state.summary.previous && state.summary.previous.end >= state.opts.months[0]
    ? cohortIndex(state.summary.previous.cohort, cfg.prefix) : null;
  const keys = cfg.keys().filter((k) => cur[k]);
  const tests = cohortIndex(state.summary.cohort, `test.${cfg.dim}.`);

  // Bars
  const col = cfg.cols.find((c) => c.m === page._barSel.value);
  barChart(page.querySelector('[data-role="bars"]'), {
    format: col.f,
    rows: keys.map((k) => {
      const m = cur[k][col.m], winN = (cur[k].win_rate || {}).n;
      const n = col.sample ? (m && m.n) : winN;
      const t = col.tested && (tests[k] || {})[col.m];
      return { key: k, label: labelFor(cfg.dim, k), color: colorFor(cfg.dim, k), value: m ? m.value : null,
        lo: m ? m.ci_low : null, hi: m ? m.ci_high : null,
        flag: t && t.significant ? `${t.value > 0 ? "▲ Clearly higher" : "▼ Clearly lower"} than the rest (unlikely to be chance)` : null,
        note: n != null && n < MIN_SAMPLE ? `Small group: based on ${n} deals.` : null };
    }),
  });

  // Table
  let sortCol = page._sortCol || col.m, sortDir = page._sortDir || "descending";
  const val = (k, c) => (cur[k][c] || {}).value;
  const sorted = [...keys].sort((a, b) => {
    const va = sortCol === "_name" ? labelFor(cfg.dim, a) : val(a, sortCol), vb = sortCol === "_name" ? labelFor(cfg.dim, b) : val(b, sortCol);
    const cmp = va == null ? 1 : vb == null ? -1 : va < vb ? -1 : va > vb ? 1 : 0;
    return sortDir === "descending" ? -cmp : cmp;
  });
  const th = (key, label) => h("th", { "aria-sort": sortCol === key ? sortDir : null, tabindex: 0,
    onclick: () => { page._sortDir = sortCol === key && sortDir === "descending" ? "ascending" : "descending"; page._sortCol = key; renderBreakdown(name); } }, label);
  const table = h("table", {},
    h("thead", {}, h("tr", {}, th("_name", cfg.title.replace(/s$/, "")), ...cfg.cols.map((c) => th(c.m, c.label)),
      h("th", { style: "text-align:left", title: "Measures where this row is clearly different from all the others combined, after allowing for chance." }, "Stands out?"))),
    h("tbody", {}, ...sorted.map((k) => {
      const winN = (cur[k].win_rate || {}).n;
      const small = winN != null && winN < MIN_SAMPLE;
      return h("tr", {},
        h("td", {}, h("span", { class: "key", style: `background:${colorFor(cfg.dim, k)}` }), labelFor(cfg.dim, k),
          small ? h("span", { class: "chg" }, `small group (${winN} finished deals)`) : null),
        ...cfg.cols.map((c) => {
          const v = val(k, c.m);
          let chg = null;
          if (prev && prev[k] && prev[k][c.m]) {
            const d = change(v, prev[k][c.m].value, c.kind || "pct");
            if (d) {
              const good = c.lowerIsBetter ? d.v < 0 : d.v > 0;
              chg = h("span", { class: `chg ${Math.abs(d.v) < 0.005 ? "" : good ? "good" : "bad"}` }, d.text);
            }
          }
          const m = cur[k][c.m];
          const title = m && m.ci_low != null ? `Likely between ${c.f(m.ci_low)} and ${c.f(m.ci_high)}` : null;
          return h("td", { title }, c.f(v), chg);
        }),
        h("td", { class: "stands" }, ...(() => {
          const so = standsOut(cfg, tests, k);
          return so.length ? so.map((x) => h("span", { class: `so ${x.up ? "up" : "down"}` }, `${x.up ? "▲" : "▼"} ${x.text}`))
            : [h("span", { class: "muted" }, "No clear difference")];
        })()));
    })));
  page.querySelector('[data-role="table"]').replaceChildren(table);
  page.querySelector('[data-role="table-sub"]').textContent = prev
    ? `Small grey numbers show the change from ${rangeLabel(state.summary.previous.start, state.summary.previous.end)}. Hover a number for its likely range. Click a column to sort.`
    : "Hover a number for its likely range. Click a column to sort.";
}

// ---------- patterns ---------------------------------------------------------
function strength(r) {
  if (r == null) return "Not enough data";
  const a = Math.abs(r);
  const word = a >= 0.7 ? "Strong link" : a >= 0.4 ? "Moderate link" : a >= 0.2 ? "Weak link" : "Little or no link";
  return r < -0.2 ? `${word}, opposite direction` : word;
}

function renderPatterns() {
  const pairs = [
    ["sessions_vs_new_contacts", "Website visits → new contacts", 3],
    ["new_contacts_vs_revenue_won", "New contacts → won deal value", 4],
  ];
  const rows = [];
  for (const [id, label, n] of pairs) {
    for (let lag = 0; lag < n; lag++) {
      const m = state.metrics[`corr.${id}.lag${lag}`];
      if (!m) continue;
      const when = lag === 0 ? "same month" : `${lag} month${lag > 1 ? "s" : ""} later`;
      rows.push(h("div", { class: "lag-row" },
        h("span", {}, label, h("br"), h("span", { class: "muted" }, when)),
        h("div", { class: "lag-track", title: `Correlation ${m.value == null ? "n/a" : m.value.toFixed(2)} across ${m.n} months` },
          h("div", { class: "lag-fill", style: `width:${Math.abs(m.value || 0) * 100}%` })),
        h("span", {}, strength(m.value), h("br"), h("span", { class: "muted" },
          `${m.value == null ? "–" : m.value.toFixed(2)} · ${m.n} months · ${m.significant ? "unlikely to be chance" : "could be chance"}`))));
    }
  }
  $("#lags").replaceChildren(...(rows.length ? rows : [h("p", { class: "empty" }, "Not enough monthly data yet.")]));
  const opts = { height: 180, left: 64 };
  const a = $("#pat-a");
  a.closest(".card").hidden = !state.opts.has_traffic;
  if (state.opts.has_traffic) seriesChart(a, "sessions", "none", opts);
  seriesChart($("#pat-b"), "new_contacts", "none", opts);
  seriesChart($("#pat-c"), "won_value", "none", opts);
}

// ---------- data health ------------------------------------------------------
function renderHealth() {
  const q = (id) => state.metrics[id];
  const items = [
    ["quality.won_deals_missing_amount", "Won deals with no amount", fmt.count, "Won value is understated by these deals. Ask Sales to fill in the Amount."],
    ["quality.deals_missing_amount_rate", "Deals with no amount", fmt.pct, "Share of all deals with a blank Amount."],
    ["quality.contacts_missing_source_rate", "Contacts with no source", fmt.pct, "These can't be credited to any lead source."],
    ["quality.contacts_missing_campaign_rate", "Contacts with no campaign", fmt.pct, "Campaign results can't include these. Tagging campaigns in HubSpot fixes this going forward."],
    ["quality.contacts_missing_company_rate", "Contacts with no company", fmt.pct, "Industry results can't include these."],
    ["quality.deals_without_contact", "Deals with no contact", fmt.count, "These can't be credited to a campaign or source."],
  ];
  $("#health-tiles").replaceChildren(...items.filter(([id]) => q(id)).map(([id, label, f, hint]) => {
    const v = q(id).value;
    const ok = !v;
    return h("div", { class: "tile" }, h("div", { class: "label" }, label),
      h("div", { class: "value" }, f(v)),
      h("div", { class: `delta ${ok ? "good" : "bad"}` }, ok ? "✓ No issue" : "⚠ Needs attention"),
      h("p", { class: "muted", style: "margin-top:6px" }, hint));
  }));
  const checks = ((state.validation || {}).checks || []).filter((c) => c.level !== "info");
  const ICON = { error: "✕", warning: "⚠" };
  $("#health-checks").replaceChildren(
    h("h3", {}, "Checks on the latest data pull"),
    h("p", { class: "muted", style: "margin:4px 0 10px" }, checks.length
      ? "Things to fix in HubSpot (or in the pipeline) so the numbers are complete."
      : "✓ No problems found in the latest pull."),
    ...(checks.length ? [h("ul", { class: "checks" }, ...checks.map((c) =>
      h("li", { class: c.level }, h("span", { class: "ic", "aria-label": c.level }, ICON[c.level] || ""), c.message)))] : []));
  const man = state.manifest;
  const counts = Object.entries(man.record_counts || {}).map(([k, v]) => `${v.toLocaleString("en-US")} ${k.replace(/_/g, " ")}`).join(" · ");
  $("#run-info").replaceChildren(h("h3", {}, "About this data"),
    h("p", { class: "sub", style: "margin-top:6px" }, `Pulled ${new Date(man.finished_at || man.started_at).toLocaleString("en-US")} from ${man.source === "fixtures" ? "sample (test) data" : "HubSpot"}.`),
    h("p", { class: "muted", style: "margin-top:4px" }, counts),
    h("p", { class: "muted", style: "margin-top:4px" }, `Analysis: ${man.analysis}`));
}

// ---------- wiring -----------------------------------------------------------
async function refresh() {
  computePeriod();
  document.querySelectorAll(".chart").forEach((c) => c.classList.add("loading"));
  state.summary = await api(`/runs/latest/explore/summary?start=${state.start}&end=${state.end}&compare=${state.compare}`);
  renderPage();
}

function renderPage() {
  const p = state.page;
  if (p === "overview") { renderTiles(); renderTrend(); }
  else if (BREAKDOWNS[p]) renderBreakdown(p);
  else if (p === "patterns") renderPatterns();
  else if (p === "findings") renderFindings();
  else if (p === "health") renderHealth();
  $("#filters").style.display = ["findings", "health", "help"].includes(p) ? "none" : "";
}

function goto(page) {
  state.page = page;
  document.querySelectorAll("nav.tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.page === page)));
  document.querySelectorAll("section.page").forEach((sct) => sct.classList.toggle("active", sct.id === `page-${page}`));
  if (location.hash !== `#${page}`) history.replaceState(null, "", `#${page}`);
  if (state.summary) renderPage();
}

function initTheme() {
  let t = null;
  try { t = localStorage.getItem("dash_theme"); } catch { /* ignore */ }
  if (t) document.documentElement.dataset.theme = t;
  $("#theme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("dash_theme", document.documentElement.dataset.theme); } catch { /* ignore */ }
    if (state.summary) renderPage();
  });
}

async function init() {
  initTheme();
  try {
    const [manifest, opts, metrics, findings, validation] = await Promise.all([
      api("/runs/latest"), api("/runs/latest/explore/options"), api("/runs/latest/metrics"), api("/runs/latest/findings?status=all"),
      api("/runs/latest/validation"),
    ]);
    Object.assign(state, { manifest, opts, findings, validation, metrics: Object.fromEntries(metrics.map((m) => [m.id, m])) });
  } catch (e) {
    $("main").replaceChildren(h("div", { class: "card" }, h("h2", {}, "No data yet"),
      h("p", { class: "sub" }, "The pipeline hasn't produced a run yet, or the dashboard can't reach it."), h("p", { class: "muted" }, String(e))));
    $("#asof").textContent = "";
    return;
  }
  $("#asof").textContent = `Data as of ${new Date(state.manifest.finished_at || state.manifest.started_at)
    .toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" })}`
    + (state.manifest.source === "fixtures" ? " · SAMPLE DATA" : "");

  const monthOpts = (sel) => state.opts.months.map((m) => h("option", { value: m, selected: sel === m }, monthLabel(m, true)));
  const months = state.opts.months;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem("dash_period") || "null"); } catch { /* ignore */ }
  $("#from").replaceChildren(...monthOpts(saved && months.includes(saved.from) ? saved.from : shiftMonth(months[months.length - 1], -11)));
  $("#to").replaceChildren(...monthOpts(saved && months.includes(saved.to) ? saved.to : months[months.length - 1]));
  if (saved) { $("#preset").value = saved.p; $("#compare").checked = saved.c; }

  for (const id of ["#preset", "#from", "#to", "#compare"]) $(id).addEventListener("change", refresh);
  $("#trend-measure").addEventListener("change", renderTrend);
  $("#trend-by").addEventListener("change", renderTrend);
  $("#finding-filter").addEventListener("change", renderFindings);
  document.querySelectorAll("nav.tabs button").forEach((b) => b.addEventListener("click", () => goto(b.dataset.page)));
  addEventListener("hashchange", () => { const pg = location.hash.slice(1); if (document.getElementById(`page-${pg}`)) goto(pg); });
  document.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => goto(b.dataset.goto)));
  let rt;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => state.summary && renderPage(), 150); });

  renderFindings();
  const initial = location.hash.slice(1);
  goto(document.getElementById(`page-${initial}`) ? initial : "overview");
  await refresh();
}

init();
