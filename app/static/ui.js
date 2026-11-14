import { waitMarker } from "./lamplight.js";
// Shared UI helpers: API, DOM, formatting, charts, maps, drawer, toasts, the loading veil.
// Every request a screen makes to the app's API, kept for the behind-the-scenes panel (behind.js): the screen, the method and endpoint,
// the status and how long it took. The latest 300 are kept.
export const API_LOG = [];
export const api = async (path, opts = {}) => {
  const t0 = performance.now(), screen = location.hash || "#/cc";
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  API_LOG.push({ screen, method: opts.method || "GET", path: String(path).split("?")[0], status: r.status, ms: Math.round(performance.now() - t0), at: Date.now() });
  if (API_LOG.length > 300) API_LOG.shift();
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.detail || j.error || r.statusText), { status: r.status });   // the status lets a screen tell "not found" from a failure
  return j;
};
export const post = (path, body = {}) => api(path, { method: "POST", body });

export const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v; else if (k === "html") e.innerHTML = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
};
export const $ = (s, root = document) => root.querySelector(s);

export const n0 = (v) => (v === null || v === undefined || isNaN(v)) ? "-" : Math.round(+v).toLocaleString("en-AU");
export const n1 = (v) => (v === null || v === undefined || isNaN(v)) ? "-" : (+v).toLocaleString("en-AU", { maximumFractionDigits: 1, minimumFractionDigits: 1 });
export const n2 = (v) => (v === null || v === undefined || isNaN(v)) ? "-" : (+v).toLocaleString("en-AU", { maximumFractionDigits: 2 });
export const money = (v) => (v === null || v === undefined) ? "-" : "$" + Math.round(+v).toLocaleString("en-AU");
export const pct = (v) => (v === null || v === undefined) ? "-" : Math.round(+v) + "%";
export const ago = (ts) => {
  if (!ts) return "never";
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (s < 60) return Math.max(0, Math.round(s)) + " s ago";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
};
export const hhmm = (ts) => ts ? new Date(ts).toLocaleTimeString("en-AU", { hour: "2-digit", minute: "2-digit" }) : "-";
export const day = (ts) => ts ? new Date(ts).toLocaleDateString("en-AU", { weekday: "short", day: "numeric", month: "short" }) : "-";
export const title = (s) => (s || "").toLowerCase().replace(/(^|\s)\S/g, (t) => t.toUpperCase());

export const st = (s, label) => h("span", { class: `st ${s || ""}` }, label ?? ({ red: "Needs action", amber: "Watch", green: "Normal" }[s] || s || ""));

export function tile(k, v, s, cls = "", how = null) {
  return h("div", { class: `tile ${cls}` }, h("div", { class: "k" }, k), h("div", { class: "v num" }, v), s ? h("div", { class: "s" }, s) : null);   // `how` is kept in the signature; the link is no longer shown
}

export function card(titleText, sub, ...body) {
  return h("div", { class: "card" }, h("h3", {}, titleText, sub ? h("span", { class: "sub" }, sub) : null), ...body);
}

export function table(cols, rows, onRow, opts = {}) {
  const t = h("table", { class: "t" });
  t.append(h("thead", {}, h("tr", {}, cols.map((c) => h("th", { class: c.r ? "r" : "" }, c.l)))));
  const tb = h("tbody");
  for (const r of rows) {
    const tr = h("tr", { class: onRow ? "click" : "" }, cols.map((c) => {
      const v = c.f ? c.f(r) : r[c.k];
      return h("td", { class: c.r ? "r num" : "" }, v === null || v === undefined ? "-" : v);
    }));
    if (onRow) tr.addEventListener("click", () => onRow(r));
    tb.append(tr);
  }
  t.append(tb);
  return opts.noScroll ? t : h("div", { class: "scroll", style: opts.max ? `max-height:${opts.max}px` : "" }, t);
}

// Every table header sorts its column (click again to reverse): numbers by value (1,858; $93k; 6%), anything else as text.
const sortKey = (td) => {
  const t = td.textContent.trim(), m = t.replace(/,/g, "").match(/^-?\$?(-?\d+(?:\.\d+)?)\s*(k|M)?(?:\s*[%A-Za-z]{0,4})?$/);
  return m ? +m[1] * ({ k: 1e3, M: 1e6 }[m[2]] || 1) : t === "-" ? null : t;
};
document.addEventListener("click", (e) => {
  const th = e.target.closest?.("table.t > thead th"); if (!th) return;
  const t = th.closest("table"), tb = t.tBodies[0], i = [...th.parentNode.children].indexOf(th); if (!tb) return;
  const dir = th.dataset.sort === "asc" ? -1 : 1;
  t.querySelectorAll("thead th").forEach((x) => delete x.dataset.sort);
  th.dataset.sort = dir === 1 ? "asc" : "desc";
  const rows = [...tb.rows].map((r) => [r, r.cells[i] ? sortKey(r.cells[i]) : null]);
  rows.sort(([, a], [, b]) => a === null ? 1 : b === null ? -1 : dir * (typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b), "en", { numeric: true })));
  tb.append(...rows.map(([r]) => r));
});

// ---------------- charts (ECharts)
const charts = new Set();
// Every chart draws in shades of one teal-green (deep teal to mint) like layered paper strips: each bar is a strip with a crisp edge, a faint
// shaded lip and a soft shadow cast on its neighbour; lines carry a soft drop shadow over a fading fill. Screens pass their old
// series colours; they are mapped here in one place. Single-series bar charts take a different strip colour per bar.
const BAND = ["#24575f", "#2f6a75", "#3c7c88", "#48849c", "#5490a8", "#6aa5b5", "#83b8bf", "#9ccccc", "#b3d9d4", "#c8e4df"];
const SERIES = ["#3c6c78", "#5490a8", "#9ccccc", "#24575f", "#83b8bf", "#48849c", "#b3d9d4", "#6aa5b5"];
const TO_BAND = { "#0c5a66": "#3c6c78", "#c98a1e": "#5490a8", "#2f7dc4": "#48849c", "#d2462f": "#24575f", "#dd4b1a": "#6aa5b5", "#5f7185": "#9ccccc",
  "#6a5cf0": "#83b8bf", "#2e9c6a": "#5490a8" };
const mapCol = (c) => (typeof c === "string" && TO_BAND[c.toLowerCase()]) || c;
function shade(hex, k) {   // k < 0 darker, k > 0 lighter
  const n = parseInt(hex.slice(1), 16), f = (v) => Math.round(Math.max(0, Math.min(255, k < 0 ? v * (1 + k) : v + (255 - v) * k)));
  return "#" + [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => f(v).toString(16).padStart(2, "0")).join("");
}
const strip = (c, horizontal) => new echarts.graphic.LinearGradient(0, horizontal ? 1 : 0, horizontal ? 0 : 1, 0,
  [{ offset: 0, color: shade(c, -0.14) }, { offset: 0.1, color: c }, { offset: 0.86, color: c }, { offset: 1, color: shade(c, -0.07) }]);
const SHADOW = { shadowColor: "rgba(20, 60, 70, 0.28)", shadowBlur: 7, shadowOffsetX: 3, shadowOffsetY: 1 };
const bandPick = (i, n) => BAND[n <= BAND.length ? Math.round((i * (BAND.length - 1)) / Math.max(1, n - 1)) : Math.min(BAND.length - 1, Math.floor((i * BAND.length) / n))];   // long charts walk the band
function paperSeries(s, i, all, option) {
  const base = mapCol(s.color) || SERIES[i % SERIES.length];
  if (s.lineStyle?.color) s.lineStyle.color = mapCol(s.lineStyle.color);
  if (s.markLine?.lineStyle?.color) s.markLine.lineStyle.color = mapCol(s.markLine.lineStyle.color);
  if (s.type === "bar") {
    const horizontal = [].concat(option.yAxis || [])[0]?.type === "category";
    const bars = all.filter((x) => x.type === "bar").length;
    if (s.keepColours && Array.isArray(s.data)) {   // the screen colours each bar itself (a status scale): keep it, as a paper strip
      s.data = s.data.map((v) => v && typeof v === "object" && v.itemStyle?.color ? { ...v, itemStyle: { ...v.itemStyle, color: strip(v.itemStyle.color, horizontal) } } : v);
    } else if (bars === 1 && Array.isArray(s.data) && s.data.length > 1) {   // one series: every bar its own strip of the band
      const n = s.data.length;
      s.data = s.data.map((v, k) => ({ ...(v && typeof v === "object" && !Array.isArray(v) ? v : { value: v }), itemStyle: { color: strip(bandPick(k, n), horizontal) } }));
    } else s.color = base;
    s.itemStyle = Object.assign({ color: strip(base, horizontal), borderRadius: 0, ...SHADOW }, s.itemStyle || {});
    s.emphasis = Object.assign({ itemStyle: { shadowBlur: 12, shadowColor: "rgba(20, 60, 70, 0.38)" } }, s.emphasis || {});
  } else if (s.type === "line") {
    s.color = base;
    s.lineStyle = Object.assign({ width: 2.4, shadowColor: "rgba(20, 60, 70, 0.22)", shadowBlur: 6, shadowOffsetY: 3 }, s.lineStyle || {});
    if (s.areaStyle) s.areaStyle = Object.assign({}, s.areaStyle, { opacity: 1, color: new echarts.graphic.LinearGradient(0, 0, 0, 1,
      [{ offset: 0, color: base + "66" }, { offset: 1, color: base + "08" }]) });
  } else if (s.type === "pie") {
    s.itemStyle = Object.assign({ borderColor: "#ffffff", borderWidth: 3, ...SHADOW, shadowOffsetX: 2, shadowOffsetY: 2 }, s.itemStyle || {});
    if (!s.color) s.color = BAND.filter((_, k) => k % 2 === 0).concat(BAND.filter((_, k) => k % 2));
  } else if (s.color) s.color = base;
  return s;
}
export function chart(el, option) {
  const c = echarts.init(el, null, { renderer: "canvas" });
  if (option.series) { const all = [].concat(option.series); option.series = all.map((x, i) => paperSeries(x, i, all, option)); }
  c.setOption(Object.assign({
    backgroundColor: "transparent", textStyle: { color: "#5f7185", fontFamily: "Inter, system-ui" },
    grid: { left: 48, right: 18, top: 28, bottom: 30 }, tooltip: { trigger: "axis", backgroundColor: "#ffffff", borderColor: "#d9e0ea", textStyle: { color: "#1c2b3a" } }, color: SERIES,
  }, option));
  charts.add(c);
  return c;
}
export const axis = (extra = {}) => Object.assign({ axisLine: { lineStyle: { color: "#aab6c8" } }, splitLine: { lineStyle: { color: "#e3e8f0" } }, axisLabel: { color: "#5f7185" } }, extra);
window.addEventListener("resize", () => charts.forEach((c) => c.resize()));
export function disposeCharts() { charts.forEach((c) => { try { c.dispose(); } catch (e) {} }); charts.clear(); }
export const chartBox = (cls = "") => h("div", { class: `chart ${cls}` });

// ---------------- maps (Leaflet over OpenStreetMap)
export function mapIn(el, center = [-33.65, 150.85], zoom = 9) {
  const m = L.map(el, { zoomControl: true, attributionControl: true, preferCanvas: true }).setView(center, zoom);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "&copy; OpenStreetMap contributors" }).addTo(m);
  return m;
}
export const STATUS_COLOUR = { red: "#d2462f", amber: "#c98a1e", green: "#2e9c6a" };

// ---------------- drawer, toasts, modal
export function drawer(...content) {
  const b = $("#drawerBody");
  b.innerHTML = "";
  b.append(...content.filter((c) => c !== null && c !== undefined && c !== false));
  // opened from a pop-up (a customer in a notice's recipients, say), the drawer sits above it; otherwise at its usual layer
  const top = [...document.querySelectorAll(".modal-bg")].reduce((a, m) => Math.max(a, +m.style.zIndex || 3000), 0);
  $("#drawer").style.zIndex = top ? String(top + 5) : "";
  $("#drawer").classList.add("open");
}
// A press anywhere outside the open drawer closes it. It acts on the press, before the click, so clicking another row or asset closes this
// drawer and the click then opens that one. Pop-ups, notifications and hover panels opened over the drawer do not count as outside.
document.addEventListener("pointerdown", (e) => {
  const d = document.getElementById("drawer");
  if (!d?.classList.contains("open") || d.contains(e.target)) return;
  if (e.target.closest?.(".modal-bg, #toasts, .behind-panel, .infotip, .s3-bubble")) return;
  d.classList.remove("open");
}, true);
export function toast(msg, cls = "") {
  const t = h("div", { class: `toast ${cls}` }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), 6500);
}
export function modal(...content) {
  const bg = h("div", { class: "modal-bg", onclick: (e) => { if (e.target === bg) bg.remove(); } }, h("div", { class: "modal" }, ...content.filter((c) => c !== null && c !== undefined)));
  const open = [...document.querySelectorAll(".modal-bg")].length, dz = +$("#drawer")?.style.zIndex || 0;
  bg.style.zIndex = String(Math.max(3000 + 10 * open, dz + 5));   // each pop-up above what is already open, the drawer included
  document.body.append(bg);
  return bg;
}
export function loading(msg = "Loading") {   // veil the app and play the waiting scene in the centre of the screen (lamplight.js)
  return waitMarker(msg);
}
