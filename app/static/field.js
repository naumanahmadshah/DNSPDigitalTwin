// Field app (crew, phone first): the day as a route on real roads, drag to re-sequence, booked appointments that keep their time, start and
// complete, report a change, stand down. Live state is shared with the control room through /api/field (Lakebase), so a phone and the big
// screen always show the same day. Self-contained (no app modules), so the phone relay only has to expose this page and its API.
const root = document.getElementById("root");
const enc = encodeURIComponent;
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v; else if (k === "html") e.innerHTML = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
};
const get = (p) => fetch(p).then((r) => { if (!r.ok) throw new Error(r.statusText || `HTTP ${r.status}`); return r.json(); });
const send = (p, body) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => r.json());
const plural = (n, one, many = `${one}s`) => (n === 1 ? one : many);   // the noun for a count
const sentence = (m) => (/[.!?]$/.test(String(m || "")) ? String(m) : `${m}.`);   // a server or browser message, given a full stop when it lacks one

// Lucide line icons (lucide-static, ISC licence)
const ICONS = {
  "arrow-down": `<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>`,
  "maximize": `<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>`,
  "ambulance": `<path d="M10 10H6"/><path d="M14 18V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v11a1 1 0 0 0 1 1h2"/><path d="M19 18h2a1 1 0 0 0 1-1v-3.28a1 1 0 0 0-.684-.948l-1.923-.641a1 1 0 0 1-.578-.502l-1.539-3.076A1 1 0 0 0 16.382 8H14"/><path d="M8 8v4"/><path d="M9 18h6"/><circle cx="17" cy="18" r="2"/><circle cx="7" cy="18" r="2"/>`,
  "bed": `<path d="M2 4v16"/><path d="M2 8h18a2 2 0 0 1 2 2v10"/><path d="M2 17h20"/><path d="M6 8v9"/>`,
  "house": `<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>`,
  "truck": `<path d="M14 18V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v11a1 1 0 0 0 1 1h2"/><path d="M15 18H9"/><path d="M19 18h2a1 1 0 0 0 1-1v-3.65a1 1 0 0 0-.22-.624l-3.48-4.35A1 1 0 0 0 17.52 8H14"/><circle cx="17" cy="18" r="2"/><circle cx="7" cy="18" r="2"/>`,
  "package": `<path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z"/><path d="M12 22V12"/><polyline points="3.29 7 12 12 20.71 7"/><path d="m7.5 4.27 9 5.15"/>`,
  "construction": `<rect x="2" y="6" width="20" height="8" rx="1"/><path d="M17 14v7"/><path d="M7 14v7"/><path d="M17 3v3"/><path d="M7 3v3"/><path d="M10 14 2.3 6.3"/><path d="m14 6 7.7 7.7"/><path d="m8 6 8 8"/>`,
  "cloud-lightning": `<path d="M6 16.326A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 .5 8.973"/><path d="m13 12-3 5h4l-3 5"/>`,
  "clock": `<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>`,
  "play": `<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>`,
  "circle-check": `<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>`,
  "circle-pause": `<circle cx="12" cy="12" r="10"/><line x1="10" x2="10" y1="15" y2="9"/><line x1="14" x2="14" y1="15" y2="9"/>`,
  "route": `<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>`,
  "map-pin": `<path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/>`,
  "flag": `<path d="M4 22V4a1 1 0 0 1 .4-.8A6 6 0 0 1 8 2c3 0 5 2 7.333 2q2 0 3.067-.8A1 1 0 0 1 20 4v10a1 1 0 0 1-.4.8A6 6 0 0 1 16 16c-3 0-5-2-8-2a6 6 0 0 0-4 1.528"/>`,
  "zap": `<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>`,
  "leaf": `<path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/>`,
  "factory": `<path d="M12 16h.01"/><path d="M16 16h.01"/><path d="M3 19a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8.5a.5.5 0 0 0-.769-.422l-4.462 2.844A.5.5 0 0 1 15 10.5v-2a.5.5 0 0 0-.769-.422L9.77 10.922A.5.5 0 0 1 9 10.5V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2z"/><path d="M8 16h.01"/>`,
  "cable": `<path d="M17 19a1 1 0 0 1-1-1v-2a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2a1 1 0 0 1-1 1z"/><path d="M17 21v-2"/><path d="M19 14V6.5a1 1 0 0 0-7 0v11a1 1 0 0 1-7 0V10"/><path d="M21 21v-2"/><path d="M3 5V3"/><path d="M4 10a2 2 0 0 1-2-2V6a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2a2 2 0 0 1-2 2z"/><path d="M7 5V3"/>`,
  "utility-pole": `<path d="M12 2v20"/><path d="M2 5h20"/><path d="M3 3v2"/><path d="M7 3v2"/><path d="M17 3v2"/><path d="M21 3v2"/><path d="m19 5-7 7-7-7"/>`,
  "plug": `<path d="M12 22v-5"/><path d="M15 8V2"/><path d="M17 8a1 1 0 0 1 1 1v4a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1z"/><path d="M9 8V2"/>`,
  "gauge": `<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>`,
  "scan-eye": `<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><circle cx="12" cy="12" r="1"/><path d="M18.944 12.33a1 1 0 0 0 0-.66 7.5 7.5 0 0 0-13.888 0 1 1 0 0 0 0 .66 7.5 7.5 0 0 0 13.888 0"/>`,
  "hard-hat": `<path d="M10 10V5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v5"/><path d="M14 6a6 6 0 0 1 6 6v3"/><path d="M4 15v-3a6 6 0 0 1 6-6"/><rect x="2" y="15" width="20" height="4" rx="1"/>`,
  "lock": `<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>`,
  "chevron-up": `<path d="m18 15-6-6-6 6"/>`,
  "chevron-down": `<path d="m6 9 6 6 6-6"/>`,
  "x": `<path d="M18 6 6 18"/><path d="m6 6 12 12"/>`,
  "wrench": `<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/>`,
  "navigation": `<polygon points="3 11 22 2 13 21 11 13 3 11"/>`,
  "users": `<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><path d="M16 3.128a4 4 0 0 1 0 7.744"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><circle cx="9" cy="7" r="4"/>`,
  "camera": `<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>`,
  "clipboard": `<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="m9 14 2 2 4-4"/>`,
  "triangle-alert": `<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>`,
  "upload": `<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>`,
  "loader": `<path d="M21 12a9 9 0 1 1-6.219-8.56"/>`,
};
const svg = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`;
const ic = (name) => h("span", { class: "ic", html: svg(name) });
const glass = (name, tone, lg) => h("span", { class: `gi${tone ? " gi-" + tone : ""}${lg ? " gi-lg" : ""}` }, ic(name));
const GRIP = `<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>`;
const ARROW = `<svg viewBox="0 0 24 24" fill="none" stroke="#0c5a66" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15 L12 8 L18 15"/></svg>`;

const TYPE_ICON = { HV_MAINT: "utility-pole", LV_MAINT: "zap", SUBSTATION_MAINT: "factory", CABLE_REPAIR: "cable", PROTECTION_TEST: "gauge", FAULT_RESPONSE: "zap",
  VEGETATION: "leaf", CIVIL: "hard-hat", METERING: "gauge", CUSTOMER_CONNECTION: "plug", ASSET_INSPECTION: "scan-eye", SERVICE_CALL: "wrench" };
const TYPE_TONE = { FAULT_RESPONSE: "bad", HV_MAINT: "info", SUBSTATION_MAINT: "info", PROTECTION_TEST: "info", VEGETATION: "good", CUSTOMER_CONNECTION: "accent" };
const typeIcon = (j) => TYPE_ICON[j.type] || "wrench";
const tone = (j) => (j.is_emergency ? "bad" : TYPE_TONE[j.type]);

// What a crew can report from a stop. park: a blocker on the job (the stop goes to the dispatcher's on-hold queue, not to another crew);
// reroster: the crew cannot carry on (the rest of the day goes to the nearest qualified crews); note: running over, the stop stays in progress.
const REASONS = [
  { key: "noaccess", label: "No access to site", icon: "construction", tone: "bad", scope: "stop", act: "park" },
  { key: "customer", label: "Customer not home", icon: "house", tone: "bad", scope: "stop", act: "park" },
  { key: "material", label: "Missing material", icon: "package", tone: "warn", scope: "stop", act: "park" },
  { key: "overrun", label: "Running over", icon: "clock", tone: "warn", scope: "stop", act: "note" },
  { key: "sick", label: "Sick or unwell", icon: "bed", tone: "bad", scope: "crew", act: "reroster" },
  { key: "injury", label: "Injury", icon: "ambulance", tone: "bad", scope: "crew", act: "reroster" },
  { key: "fatigue", label: "Fatigue stop", icon: "bed", tone: "warn", scope: "crew", act: "reroster" },
  { key: "family", label: "Family emergency", icon: "users", tone: "warn", scope: "crew", act: "reroster" },
  { key: "vehicle", label: "Vehicle or plant down", icon: "truck", tone: "bad", scope: "crew", act: "reroster" },
  { key: "weather", label: "Weather stand-down", icon: "cloud-lightning", tone: "warn", scope: "crew", act: "reroster" },
];
const REASON = Object.fromEntries(REASONS.map((r) => [r.key, r]));

// A real phone (or the installed app) gets the field screen edge to edge; a big screen shows a phone frame on the Phone tab.
// ?phone=1 or ?phone=0 forces either for testing and as a demo-day fallback.
const REAL_PHONE = (() => {
  const q = new URLSearchParams(location.search).get("phone");
  if (q === "1") return true;
  if (q === "0") return false;
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const touch = matchMedia("(pointer: coarse)").matches || (navigator.maxTouchPoints || 0) > 0;
  return standalone || (touch && Math.min(innerWidth, innerHeight) <= 820);
})();

// Turn-by-turn directions to the stop: the Google Maps app on a phone (the universal link opens it when installed), a new tab elsewhere.
function directions(j) {
  const url = `https://www.google.com/maps/dir/?api=1&destination=${enc(`${j.lat},${j.lon}`)}&travelmode=driving`;
  if (REAL_PHONE) location.href = url; else window.open(url, "_blank", "noopener");
}

// ---------------------------------------------------------------- geometry and the day's timings
const SPEED = 42;
const hav = (a, b) => {
  const r = (d) => d * Math.PI / 180, dLat = r(b.lat - a.lat), dLon = r(b.lon - a.lon);
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(r(a.lat)) * Math.cos(r(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(s));
};
const hhmm = (m) => { m = Math.round(m); return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; };
const bearing = (a, b) => {
  const r = (d) => d * Math.PI / 180, y = Math.sin(r(b[1] - a[1])) * Math.cos(r(b[0]));
  const x = Math.cos(r(a[0])) * Math.sin(r(b[0])) - Math.sin(r(a[0])) * Math.cos(r(b[0])) * Math.cos(r(b[1] - a[1]));
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
};
const segKm = (a, b) => hav({ lat: a[0], lon: a[1] }, { lat: b[0], lon: b[1] });
// each waypoint (depot, stops, depot) to its index on the road path, searching forward so the closing depot lands at the path's end
function wpIndices(coords, wp) {
  const idx = []; let start = 0;
  for (let k = 0; k < wp.length; k++) {
    if (k === wp.length - 1) { idx.push(coords.length - 1); break; }
    let bi = start, bd = Infinity;
    for (let i = start; i < coords.length; i++) { const d = (coords[i][0] - wp[k].lat) ** 2 + (coords[i][1] - wp[k].lon) ** 2; if (d < bd) { bd = d; bi = i; } }
    idx.push(bi); start = bi;
  }
  return idx;
}
function midOnPath(coords, i0, i1) {   // the point half way along the leg, by distance
  if (i1 <= i0) return coords[Math.min(i0, coords.length - 1)];
  let total = 0; for (let i = i0; i < i1; i++) total += segKm(coords[i], coords[i + 1]);
  let acc = 0;
  for (let i = i0; i < i1; i++) {
    const seg = segKm(coords[i], coords[i + 1]);
    if (acc + seg >= total / 2) { const f = seg ? (total / 2 - acc) / seg : 0; return [coords[i][0] + (coords[i + 1][0] - coords[i][0]) * f, coords[i][1] + (coords[i + 1][1] - coords[i][1]) * f]; }
    acc += seg;
  }
  return coords[i1];
}
function arrowsByLeg(coords, wpIdx) {   // direction arrows in the middle of each leg only, never on a pin
  const out = [];
  for (let leg = 0; leg < wpIdx.length - 1; leg++) {
    const i0 = wpIdx[leg], i1 = wpIdx[leg + 1];
    if (i1 <= i0) continue;
    const cum = [0]; let len = 0;
    for (let i = i0; i < i1; i++) { len += segKm(coords[i], coords[i + 1]); cum.push(len); }
    if (len <= 0) continue;
    for (let d = len * 0.2; d <= len * 0.8 && out.length < 60; d += Math.max(0.45, len / 4)) {
      let s = 0; while (s < cum.length - 1 && cum[s + 1] < d) s++;
      const a = coords[i0 + s], b = coords[i0 + s + 1] || a, f = (d - cum[s]) / (segKm(a, b) || 1e-9);
      out.push({ at: [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f], deg: bearing(a, b) });
    }
  }
  return out;
}

// Times for a visiting order. legs (from the road router) is [{min, km}] per leg including the drive home, or null for a straight-line
// estimate. A booked appointment starts no earlier than its booked time; arriving after it counts as a missed appointment.
function schedule(home, js, order, shiftStart, legs) {
  let t = shiftStart, loc = home, totalKm = 0, late = 0;
  const sched = order.map((idx, k) => {
    const j = js[idx], km = legs ? legs[k].km : hav(loc, j), travel = legs ? legs[k].min : km / SPEED * 60, arrive = t + travel;
    let start = arrive, isLate = false;
    if (j.fixed) { start = Math.max(arrive, j.window_start_min); isLate = arrive > j.window_start_min + 1; }
    if (isLate) late++;
    totalKm += km; t = start + j.duration_min; loc = j;
    return { idx, arrive, start, finish: t, travelMin: travel, travelKm: km, late: isLate };
  });
  const n = order.length, homeKm = legs ? legs[n].km : (n ? hav(loc, home) : 0), homeMin = legs ? legs[n].min : homeKm / SPEED * 60;
  return { sched, freeAt: t + homeMin, totalKm: totalKm + homeKm, lateCount: late };
}

// The visiting order: stops already under way (and an emergency) stay at the front; booked appointments are anchored by their time; the
// crew's own order of the movable stops fills the gaps before each appointment, as long as the crew still arrives on time for it.
function composeDay(home, js, movable, locked, frontSeq, shiftStart) {
  const drive = (a, b) => hav(a, b) / SPEED * 60, ws = (idx) => js[idx].window_start_min || 0;
  const seq = [];
  let t = shiftStart, loc = home;
  for (const idx of frontSeq) { const j = js[idx]; t += drive(loc, j) + j.duration_min; loc = j; seq.push(idx); }
  const lk = locked.slice().sort((a, b) => ws(a) - ws(b));
  let mi = 0, li = 0;
  while (mi < movable.length || li < lk.length) {
    const m = js[movable[mi]], l = js[lk[li]];
    if (li >= lk.length || (mi < movable.length && t + drive(loc, m) + m.duration_min + drive(m, l) <= ws(lk[li]))) {
      t += drive(loc, m) + m.duration_min; loc = m; seq.push(movable[mi++]);
    } else { t = Math.max(t + drive(loc, l), ws(lk[li])) + l.duration_min; loc = l; seq.push(lk[li++]); }
  }
  return seq;
}

// ---------------------------------------------------------------- state
const S = { regions: [], region: null, workers: [], crewId: null, itin: null, order: [], statuses: {}, selected: null, offline: false, queue: [],
  route: null, routing: false, reject: null, notice: null, view: REAL_PHONE ? "phone" : "desktop", mapMax: false, issues: {}, reporting: false,
  reassignedTo: {}, parkedJobs: [], stoodDown: null, report: null, photo: null, reportQueue: [] };
let lastGood = [], roadCheck = new Map(), drag = null, reassignTimer = null, routeTimer = null, routeSeq = 0;
const timers = {};
function say(kind, msg) {   // the red "blocked" toast or the blue acknowledgement, each clearing itself
  S[kind] = msg; clearTimeout(timers[kind]);
  if (msg) timers[kind] = setTimeout(() => { S[kind] = null; render(); }, kind === "reject" ? 5200 : 6500);
}

const jobs = () => (S.itin ? S.itin.jobs : []);
const orderKey = () => S.order.join("-");
const routeValid = () => S.route && S.route.forOrder === orderKey() && S.route.legs.length === S.order.length + 1;
const plan = () => schedule(S.itin.home, jobs(), S.order, S.itin.shift_start_min, routeValid() ? S.route.legs : null);
const shiftCap = () => S.itin.shift_start_min + (S.itin.max_shift_min || 720);
const status = (idx) => S.statuses[jobs()[idx].job_id] || "planned";
const STARTED = { done: 0, in_progress: 1, driving: 2 };
const front = (idx) => status(idx) in STARTED || (jobs()[idx].is_emergency && !["reassigning", "reassigned"].includes(status(idx)));
const reported = (idx) => ["reassigning", "reassigned"].includes(status(idx));
const immovable = (idx) => jobs()[idx].fixed || front(idx) || reported(idx);
const movableSeq = () => S.order.filter((i) => !jobs()[i].fixed && !front(i));
const lockedSeq = () => S.order.filter((i) => jobs()[i].fixed && !front(i));
const frontSeq = () => S.order.filter(front);
const compose = (mov) => composeDay(S.itin.home, jobs(), mov, lockedSeq(), frontSeq(), S.itin.shift_start_min);

// ---------------------------------------------------------------- loading the day
const crewKey = (reg) => `field-crew:${reg}`;
function applyItin(d) {
  const js = d.jobs, st = { ...(d.job_status || {}) }, all = js.map((_, i) => i);
  const parked = all.filter((i) => st[js[i].job_id] === "parked"), live = all.filter((i) => !parked.includes(i));
  const rank = (i) => (st[js[i].job_id] in STARTED ? STARTED[st[js[i].job_id]] : 3);
  const isFront = (i) => st[js[i].job_id] in STARTED || js[i].is_emergency;
  const fr = live.filter(isFront).sort((a, b) => rank(a) - rank(b));
  const ord = composeDay(d.home, js, live.filter((i) => !js[i].fixed && !isFront(i)), live.filter((i) => js[i].fixed && !isFront(i)), fr, d.shift_start_min);
  clearTimeout(reassignTimer); reassignTimer = null;
  const sd = d.stood_down, rt = {};
  if (sd) js.forEach((j) => { st[j.job_id] = "reassigned"; const a = sd.jobs && sd.jobs[j.job_id]; if (a) rt[j.job_id] = { ...a, name: a.name || a.crew_name }; });
  Object.assign(S, { itin: d, order: ord, statuses: st, issues: {}, reassignedTo: rt, parkedJobs: parked, selected: null, reporting: false, route: null,
    crewId: d.crew.id, stoodDown: sd ? { reason_label: sd.reason_label, count: sd.count || js.length } : null });
  lastGood = ord.slice(); roadCheck = new Map();
  say("notice", sd ? `${d.crew.name}'s day was pulled back (${sd.reason_label}). ${S.stoodDown.count === 1 ? "The stop was" : `All ${S.stoodDown.count} stops were`} re-rostered to other crews. This stays until the dispatcher resets the roster.` : null);
  try { localStorage.setItem(crewKey(d.region), d.crew.id); } catch (_) { /* private mode */ }
}
async function loadItin(reg, cid) {
  if (!cid) { try { cid = localStorage.getItem(crewKey(reg)) || ""; } catch (_) { cid = ""; } }
  const d = await get(`/api/field/itinerary?region=${enc(reg)}${cid ? `&crew_id=${enc(cid)}` : ""}`);
  applyItin(d); render(); routeLater();
}
const loadWorkers = async (reg) => { S.workers = (await get(`/api/field/workers?region=${enc(reg)}`)).workers || []; };
async function pickRegion(reg, cid) {
  S.region = reg; S.itin = null;
  try { localStorage.setItem("field-region", reg); } catch (_) { /* private mode */ }
  render();
  await loadWorkers(reg);
  await loadItin(reg, cid);
}

// The control room can change the day (a dispatch, an emergency, a stand-down): check every 15 seconds and pick changes up.
async function poll() {
  if (!S.itin || S.offline || drag || reassignTimer || document.hidden) return;
  const d = await get(`/api/field/itinerary?region=${enc(S.region)}&crew_id=${enc(S.crewId)}`).catch(() => null);
  if (!d || d.crew.id !== S.crewId) return;
  const now = new Set(d.jobs.map((j) => j.job_id)), had = new Set(jobs().map((j) => j.job_id));
  const parkedIds = new Set(S.parkedJobs.map((i) => jobs()[i].job_id));
  const added = d.jobs.filter((j) => !had.has(j.job_id)), removed = [...had].some((id) => !now.has(id) && !parkedIds.has(id));
  if (added.length || removed || !!d.stood_down !== !!S.stoodDown) {
    const emer = added.find((j) => j.is_emergency);
    applyItin(d); routeLater(); loadWorkers(S.region).then(render).catch(() => {});
    say("notice", emer ? `Emergency added to your day: ${emer.title}. It is now your first stop.` : "The control room updated your day.");
    return render();
  }
  let changed = false;
  for (const [id, s] of Object.entries(d.job_status || {})) {
    const cur = S.statuses[id] || "planned";
    if (s !== cur && !["reassigning", "reassigned", "parked"].includes(cur) && s !== "parked") { S.statuses[id] = s; changed = true; }
  }
  if (changed) render();
}

// ---------------------------------------------------------------- the road route (public OSRM router), re-checked against the rules
function routeLater() {
  clearTimeout(routeTimer);
  if (!S.itin || !S.order.length) { S.routing = false; return; }
  const key = orderKey(), seq = ++routeSeq, wp = [S.itin.home, ...S.order.map((i) => jobs()[i]), S.itin.home];
  S.routing = true;
  routeTimer = setTimeout(() => {
    fetch(`https://router.project-osrm.org/route/v1/driving/${wp.map((p) => `${p.lon},${p.lat}`).join(";")}?overview=full&geometries=geojson`)
      .then((r) => r.json()).then((d) => {
        if (seq !== routeSeq) return;
        const r0 = d.code === "Ok" && d.routes[0];
        S.routing = false;
        S.route = r0 ? { coords: r0.geometry.coordinates.map(([lon, lat]) => [lat, lon]), legs: r0.legs.map((l) => ({ min: l.duration / 60, km: l.distance / 1000 })),
          snapped: (d.waypoints || []).map((w) => ({ lat: w.location[1], lon: w.location[0] })), forOrder: key } : null;
        enforce(); render();
      })
      .catch(() => { if (seq === routeSeq) { S.routing = false; S.route = null; render(); } });
  }, 250);
}
// The quick check before a re-order uses straight lines; on the real road times, an order that misses an appointment or runs past the
// shift limit goes back to the last order that complied.
// An order that is no worse by road than the one it replaced is kept, so a day that was already late on the road can still be improved.
function enforce() {
  if (!routeValid()) return;
  const p = plan(), over = p.freeAt > shiftCap(), breach = p.lateCount > 0 ? "appt" : over ? "shift" : null;
  roadCheck.set(orderKey(), { late: p.lateCount, over });
  const prev = roadCheck.get(lastGood.join("-"));
  if (!breach || (prev && p.lateCount <= prev.late && (!over || prev.over))) { lastGood = S.order.slice(); return; }
  if (lastGood.length && lastGood.join("-") !== orderKey()) {
    S.order = lastGood.slice(); persistOrder(S.order); routeLater();
    say("reject", breach === "appt" ? "Reverted: that order would make you miss a booked appointment."
      : `Reverted: by road your day would finish at ${hhmm(p.freeAt)}, past the ${hhmm(shiftCap())} shift limit (12-hour fatigue rule).`);
  }
}

// ---------------------------------------------------------------- changing the order (only if every appointment and the shift limit still hold)
function evalOrder(ord) {   // an order no worse than the current one is allowed, so a day that is already late can be improved
  const s = schedule(S.itin.home, jobs(), ord, S.itin.shift_start_min, null), now = schedule(S.itin.home, jobs(), S.order, S.itin.shift_start_min, null);
  const over = s.freeAt > shiftCap();
  if (s.lateCount <= now.lateCount && (!over || now.freeAt > shiftCap()) && (s.lateCount || over)) return null;
  if (s.lateCount > 0) return "That order would make you miss a booked appointment.";
  if (over) return `That order would finish at ${hhmm(s.freeAt)}, past the ${hhmm(shiftCap())} shift limit (12-hour fatigue rule).`;
  return null;
}
function persistOrder(ord) {   // saved so the same order comes back on any device
  if (!S.itin || S.stoodDown) return;
  send("/api/field/reorder", { crew_id: S.crewId, region: S.region, order: ord.map((i) => jobs()[i].job_id) }).catch(() => {});
}
function applyIfCompliant(ord) {
  const why = evalOrder(ord);
  if (why) { say("reject", `Cannot reorder: ${why[0].toLowerCase()}${why.slice(1)}`); return render(); }
  S.order = ord; say("reject", null); persistOrder(ord); routeLater(); render();
}
const canMove = (k, dir) => { if (immovable(S.order[k])) return false; const m = movableSeq(), r = m.indexOf(S.order[k]); return r >= 0 && r + dir >= 0 && r + dir < m.length; };
function move(k, dir) {
  if (!canMove(k, dir)) return;
  const m = movableSeq(), r = m.indexOf(S.order[k]);
  [m[r], m[r + dir]] = [m[r + dir], m[r]];
  applyIfCompliant(compose(m));
}
function insertAt(fromPos, gap) {   // where a dragged stop lands if dropped in the gap before tile `gap` (appointments re-anchor by time)
  const id = S.order[fromPos], m = movableSeq(), from = m.indexOf(id);
  if (from < 0) return null;
  const to = S.order.slice(0, gap).filter((x) => !jobs()[x].fixed && !front(x) && x !== id).length;
  m.splice(from, 1); m.splice(to, 0, id);
  return compose(m);
}

// ---------------------------------------------------------------- statuses and reports
function setStatus(id, s) {   // shown on every device and in the control room, not just this browser
  S.statuses[id] = s;
  send("/api/field/status", { region: S.region, job_id: id, status: s }).catch(() => {});
  render();
}
function standDown(ids, label) {   // after a few seconds the stops really are re-rostered in the control room
  clearTimeout(reassignTimer);
  reassignTimer = setTimeout(async () => {
    reassignTimer = null;
    const d = await send("/api/field/reassign", { crew_id: S.crewId, region: S.region, job_ids: ids, reason_label: label, stand_down: true }).catch(() => ({}));
    const map = Object.fromEntries(Object.entries(d.reassigned || {}).map(([id, a]) => [id, a && { ...a, name: a.name || a.crew_name }]));
    Object.assign(S.reassignedTo, map);
    ids.forEach((id) => { S.statuses[id] = "reassigned"; });
    S.stoodDown = { reason_label: label, count: ids.length };
    const named = ids.map((id) => map[id]).filter(Boolean), one = named.length === 1 && named[0];
    say("notice", `Re-roster complete. ${ids.length} stop${ids.length === 1 ? "" : "s"} reassigned${one ? ` to ${one.name} (${one.persona}, ${one.km} km away)` : " to the nearest qualified crews"}.`);
    render();
  }, 3000);
}
function report(j, key) {
  const r = REASON[key];
  S.reporting = false;
  if (S.offline) { S.queue.push([j.job_id, key]); say("notice", `Queued while offline: ${r.label} on ${j.title || j.type_label}. It syncs and takes effect when signal returns.`); return render(); }
  if (r.scope === "crew") {
    const left = S.order.filter((i) => status(i) !== "done").map((i) => jobs()[i].job_id);
    left.forEach((id) => { S.statuses[id] = "reassigning"; });
    S.issues[j.job_id] = key; S.selected = null;
    say("notice", `Logged ${r.label}. You are stood down for the rest of today. Re-rostering your ${left.length} remaining stop${left.length === 1 ? "" : "s"} to the nearest qualified crew under the safety rules.`);
    standDown(left, r.label);
  } else if (r.act === "note") {
    S.issues[j.job_id] = key;
    setStatus(j.job_id, "in_progress");
    say("notice", "Logged as running over. The dispatcher has been told. Your later stops are re-rostered if the day would breach the shift limit.");
  } else {
    const idx = jobs().indexOf(j);
    S.selected = null; S.issues[j.job_id] = key;
    if (!S.parkedJobs.includes(idx)) S.parkedJobs.push(idx);
    S.order = S.order.filter((i) => i !== idx); lastGood = S.order.slice(); persistOrder(S.order);
    setStatus(j.job_id, "parked");
    send("/api/field/park", { crew_id: S.crewId, region: S.region, job_id: j.job_id, reason: key, reason_label: r.label }).catch(() => {});
    say("notice", `Parked: ${r.label}. It has been sent to the dispatcher's on-hold queue to be re-dispatched once it is resolved.`);
    routeLater();
  }
  render();
}
function unpark(idx) {   // back onto the route at the position that finishes the day earliest
  const j = jobs()[idx];
  S.parkedJobs = S.parkedJobs.filter((i) => i !== idx);
  delete S.issues[j.job_id]; S.statuses[j.job_id] = "planned";
  send("/api/field/unpark", { region: S.region, job_id: j.job_id }).catch(() => {});
  send("/api/field/status", { region: S.region, job_id: j.job_id, status: "planned" }).catch(() => {});
  const m = movableSeq();
  let best = null, bestFree = Infinity;
  for (let p = 0; p <= m.length; p++) {
    const ord = compose([...m.slice(0, p), idx, ...m.slice(p)]), s = schedule(S.itin.home, jobs(), ord, S.itin.shift_start_min, null);
    if (s.freeAt < bestFree) { bestFree = s.freeAt; best = ord; }
  }
  S.order = best; lastGood = best.slice(); persistOrder(best); say("notice", null); routeLater(); render();
}
function restore(j) {   // reported in error: back to the plan
  clearTimeout(reassignTimer); reassignTimer = null;
  delete S.issues[j.job_id]; delete S.reassignedTo[j.job_id];
  say("notice", null);
  setStatus(j.job_id, "planned");
}
function goOnline() {
  S.offline = !S.offline;
  if (!S.offline) { const q = S.queue.splice(0); q.forEach(([id, key]) => { const j = jobs().find((x) => x.job_id === id); if (j) report(j, key); }); flushReports(); }
  render();
}

// ---------------------------------------------------------------- the map
const MAP = { el: h("div", { id: "fmap" }), map: null, layer: null, pills: null, pillData: [], line: null, set: "", routed: false, size: "" };
function ensureMap() {
  if (MAP.map) return;
  MAP.map = L.map(MAP.el, { zoomControl: true }).setView([-33.75, 150.75], 10);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" }).addTo(MAP.map);
  MAP.layer = L.layerGroup().addTo(MAP.map);
  MAP.pills = L.layerGroup().addTo(MAP.map);
  MAP.map.on("zoomend moveend", placePills);
}
const fitNow = () => { if (MAP.line && MAP.line.length) { try { MAP.map.fitBounds(MAP.line, { padding: [45, 45], maxZoom: 14 }); } catch (_) { /* not laid out yet */ } } };
function placePills() {   // per-leg drive times, nudged apart on screen so legs sharing a road don't stack their labels
  if (!MAP.map) return;
  MAP.pills.clearLayers();
  const placed = [], W = 56, H = 20, GAP = 4, slots = Math.max(2, Math.min(3, Math.floor(MAP.map.getSize().y / 120)));
  for (const p of MAP.pillData) {
    const cp = MAP.map.latLngToContainerPoint(p.at), hit = (dy) => placed.some((b) => Math.abs(cp.x - b.x) < W && Math.abs(cp.y + dy - b.y) < H + GAP);
    const steps = [0]; for (let s = 1; s <= slots; s++) steps.push(-s * (H + GAP), s * (H + GAP));
    const dy = steps.find((c) => !hit(c)) || 0;
    placed.push({ x: cp.x, y: cp.y + dy });
    L.marker(p.at, { interactive: false, keyboard: false, icon: L.divIcon({ className: "legtime", html: `<span style="transform:translateY(${dy}px)">${p.text}</span>`, iconSize: [W, H], iconAnchor: [W / 2, H / 2] }) }).addTo(MAP.pills);
  }
}
function drawMap() {
  if (!MAP.map || !S.itin) return;
  const js = jobs(), home = S.itin.home, route = routeValid() ? S.route : null, legs = route ? route.legs : null;
  MAP.layer.clearLayers();
  const snap = route && route.snapped.length === S.order.length + 2 ? route.snapped : null;
  const homePt = snap ? snap[0] : home, pts = S.order.map((idx, k) => (snap ? snap[k + 1] : js[idx])), endPt = snap ? snap[S.order.length + 1] : home;
  const wp = [homePt, ...pts, endPt], road = !!(route && route.coords.length);
  const line = road ? route.coords : wp.map((p) => [p.lat, p.lon]);
  MAP.line = line;
  L.polyline(line, { color: "#ffffff", weight: 7, opacity: 0.9, lineCap: "round", lineJoin: "round" }).addTo(MAP.layer);
  L.polyline(line, { color: "#0c5a66", weight: 4, opacity: 0.95, lineCap: "round", lineJoin: "round" }).addTo(MAP.layer);
  const wpIdx = road ? wpIndices(line, wp) : wp.map((_, i) => i);
  arrowsByLeg(line, wpIdx).forEach((a) => L.marker(a.at, { interactive: false, keyboard: false, zIndexOffset: -200,
    icon: L.divIcon({ className: "dirarrow", html: `<span style="transform:rotate(${Math.round(a.deg)}deg)">${ARROW}</span>`, iconSize: [18, 18], iconAnchor: [9, 9] }) }).addTo(MAP.layer));
  MAP.pillData = wp.slice(0, -1).map((p, k) => ({ at: midOnPath(line, wpIdx[k], wpIdx[k + 1]), text: `${Math.round(legs ? legs[k].min : hav(p, wp[k + 1]) / SPEED * 60)} min` }));
  placePills();
  L.marker([homePt.lat, homePt.lon], { icon: L.divIcon({ className: "pin pin-home", html: `<div class="pdot">${svg("house")}</div>`, iconSize: [30, 30], iconAnchor: [15, 15] }) })
    .bindTooltip("Depot and home base", { direction: "top" }).addTo(MAP.layer);
  S.order.forEach((idx, k) => {
    const j = js[idx], st = status(idx), emer = j.is_emergency && st !== "done";
    const cls = `pin${j.fixed ? " pin-fixed" : ""}${emer ? " pin-emer" : ""}${{ done: " pin-done", in_progress: " pin-active", driving: " pin-driving", reassigning: " pin-reassign", reassigned: " pin-reassigned" }[st] || ""}${S.selected === idx ? " pin-sel" : ""}`;
    const badge = emer ? `<span class="pinbadge">${svg("zap")}</span>` : j.fixed ? `<span class="pinbadge">${svg("lock")}</span>` : "";
    L.marker([pts[k].lat, pts[k].lon], { zIndexOffset: emer ? 600 : 400, icon: L.divIcon({ className: cls, html: `<div class="pinbubble">${k + 1}${badge}</div>`, iconSize: [34, 34], iconAnchor: [17, 17] }) })
      .bindTooltip(`${k + 1}. ${j.type_label}${emer ? ", emergency" : ""}`, { direction: "top", offset: [0, -6], className: "pinlabel" })
      .on("click", () => select(idx)).addTo(MAP.layer);
  });
  // Re-frame only when the set of stops changes (or the road route first arrives), so a re-order keeps the crew's own zoom.
  const set = [home.lat, home.lon, ...S.order.map((i) => js[i].job_id).sort()].join("-");
  if (MAP.set !== set) { fitNow(); MAP.set = set; MAP.routed = road; } else if (road && !MAP.routed) { fitNow(); MAP.routed = true; }
}
function layoutMap() {   // after each render: the map node has moved into the new layout
  ensureMap();
  const size = `${S.view}-${S.mapMax}`;
  MAP.map.invalidateSize();
  drawMap();
  if (MAP.size !== size) { MAP.size = size; setTimeout(() => { MAP.map.invalidateSize(); fitNow(); }, 90); }
}

// ---------------------------------------------------------------- rendering
const select = (idx) => { S.selected = idx; S.reporting = false; render(); };

function renderTiles(p) {
  const out = [];
  S.order.forEach((idx, k) => {
    const j = jobs()[idx], s = p.sched[k], st = status(idx), emer = j.is_emergency && st !== "done";
    const hint = j.fixed ? "Locked: a booked appointment, fixed in time." : emer ? "Emergency: work this first." : st === "done" ? "Completed."
      : st === "in_progress" ? "In progress, so it stays where it is." : st === "driving" ? "You are on your way to this stop."
        : st === "reassigning" ? "Re-rostering this stop to another crew." : st === "reassigned" ? "Re-rostered to another crew." : "Drag to re-sequence this stop.";
    const grip = j.fixed ? svg("lock") : emer ? svg("zap") : st === "done" ? svg("circle-check") : st === "in_progress" ? svg("circle-pause")
      : st === "driving" ? svg("navigation") : st === "reassigning" ? svg("triangle-alert") : st === "reassigned" ? svg("route") : GRIP;
    const tag = { done: ["done", "done"], in_progress: ["prog", "in progress"], driving: ["drive", "en route"], reassigning: ["reassign", "re-rostering"], reassigned: ["reassigned", "re-rostered"] }[st];
    out.push(h("div", { class: "fconn", "data-tip": "The drive to this stop (real road time and distance once routed)." }, ic("arrow-down"),
      `${s ? Math.round(s.travelMin) : 0} min drive, ${s ? s.travelKm.toFixed(1) : 0} km${k === 0 ? " from the depot" : ""}`));
    out.push(h("div", {
      class: `fstop${S.selected === idx ? " sel" : ""}${emer ? " emer" : ""}${st === "reassigning" ? " reassign" : st === "reassigned" ? " reassigned" : ""}`,
      draggable: immovable(idx) ? null : "true", onclick: () => select(idx),
      ondragstart: (e) => {
        if (immovable(idx)) return e.preventDefault();
        drag = { k, next: null };
        e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", j.job_id);
        const chip = h("div", { class: "dragchip" }, `${k + 1}. ${j.type_label}`);
        document.body.append(chip); e.dataTransfer.setDragImage(chip, 16, 14); requestAnimationFrame(() => chip.remove());
        e.currentTarget.classList.add("dragging");
      },
      ondragend: () => { if (drag) { drag = null; render(); } },
    },
      h("span", { class: `grip${immovable(idx) ? " off" : ""}`, "data-tip": hint, html: grip }),
      h("div", { class: "fseq" }, k + 1), glass(typeIcon(j), tone(j)),
      h("div", { class: "fstop-main" },
        h("div", { class: "fstop-t" }, j.title || j.type_label, emer ? h("span", { class: "tag emer" }, "Emergency") : null,
          j.fixed ? h("span", { class: "locktag", "data-tip": "Booked with the customer: it runs in its booked window and its place in the day follows that time." }, ic("lock"), "booked") : null,
          tag ? h("span", { class: `tag ${tag[0]}` }, tag[1]) : null),
        h("div", { class: "fstop-s" }, j.fixed ? `Booked ${hhmm(j.window_start_min)} to ${hhmm(j.window_end_min)}, on site ${j.duration_min} min`
          : `${s ? hhmm(s.start) : ""} to ${s ? hhmm(s.finish) : ""}, on site ${j.duration_min} min`, j.suburb ? `, ${j.suburb}` : "",
          s && s.late ? h("span", { class: "flate" }, `, arriving ${hhmm(s.arrive)}, late for the booked time`) : null)),
      h("div", { class: "fmove" },
        h("button", { disabled: !canMove(k, -1), "aria-label": "Earlier", "data-tip": "Move this stop earlier.", onclick: (e) => { e.stopPropagation(); move(k, -1); } }, ic("chevron-up")),
        h("button", { disabled: !canMove(k, 1), "aria-label": "Later", "data-tip": "Move this stop later.", onclick: (e) => { e.stopPropagation(); move(k, 1); } }, ic("chevron-down")))));
  });
  return out;
}
function dragOver(e) {   // the insertion line snaps to the gap nearest the pointer
  if (!drag) return;
  e.preventDefault();
  const tiles = [...e.currentTarget.querySelectorAll(".fstop")];
  let g = tiles.findIndex((t) => { const r = t.getBoundingClientRect(); return e.clientY < r.top + r.height / 2; });
  if (g < 0) g = tiles.length;
  drag.next = insertAt(drag.k, g);
  tiles.forEach((t, i) => { t.classList.toggle("drop-above", i === g); t.classList.toggle("drop-below", g === tiles.length && i === tiles.length - 1); });
}
function dropped(e) {
  e.preventDefault();
  const next = drag && drag.next;
  drag = null;
  if (next) applyIfCompliant(next); else render();
}

function renderItin(p) {
  return h("div", { class: "fitin" },
    S.stoodDown ? h("div", { class: "fstood", "data-tip": "Your day was pulled back after a stand-down. It stays this way until a dispatcher re-solves the roster." }, ic("triangle-alert"),
      h("span", {}, h("b", {}, "Day pulled back"), `: ${S.stoodDown.reason_label}. ${S.stoodDown.count === 1 ? "The stop was" : `All ${S.stoodDown.count} stops were`} re-rostered to other crews. This stays until the dispatcher resets the roster.`)) : null,
    h("div", { class: "fitin-head" },
      h("h3", { "data-tip": "Your stops in order. Drag a stop (or use the arrows) to set your own sequence. Booked appointments keep their time." }, "Today's route ", h("span", { class: "fsub" }, `(${S.order.length} stops)`)),
      h("button", { class: "btn nav fitin-photo", type: "button", "data-tip": "Photograph an asset, tag it and send it for review.", onclick: () => openPhoto(null) }, ic("camera"), "Asset photo")),
    S.order.length ? h("div", { class: "ftiles", ondragover: dragOver, ondrop: dropped }, ...renderTiles(p))
      : h("div", { class: "fempty" }, "No stops are on your plan today. The control room plans the day."),
    h("div", { class: "fend" }, glass("flag", "good"), h("span", {}, "Back at the depot, free at ", h("b", { class: "num" }, hhmm(p.freeAt)))),
    S.parkedJobs.length ? h("div", { class: "fparked" },
      h("div", { class: "fparked-h", "data-tip": "Stops with a blocker (no access, materials or customer). The dispatcher re-dispatches them once it clears. They do not count toward your finish time." },
        ic("clock"), "On hold Jobs (Waiting Dispatch) ", h("span", { class: "fsub" }, `(${S.parkedJobs.length})`)),
      ...S.parkedJobs.map((idx) => {
        const j = jobs()[idx], r = REASON[S.issues[j.job_id]];
        return h("div", { class: "fparked-row" }, glass(typeIcon(j), tone(j)),
          h("div", { class: "fparked-main" }, h("div", { class: "fparked-t" }, j.title || j.type_label, h("span", { class: "tag onhold" }, "on hold")),
            h("div", { class: "fsub" }, `${r ? r.label : "Blocked"}, on site ${j.duration_min} min`)),
          h("button", { class: "linkbtn", "data-tip": "Blocker cleared? Put this stop back on your route.", onclick: () => unpark(idx) }, "Restore"));
      })) : null);
}

function renderSheet(p) {
  if (S.selected == null) return null;
  const j = jobs()[S.selected], pos = S.order.indexOf(S.selected), s = pos >= 0 ? p.sched[pos] : null, st = status(S.selected), emer = j.is_emergency && st !== "done";
  const to = S.reassignedTo[j.job_id], issue = REASON[S.issues[j.job_id]];
  const banner = st === "done" ? h("div", { class: "sheetbanner" }, ic("circle-check"), "Completed")
    : st === "in_progress" ? h("div", { class: "sheetbanner prog" }, ic("circle-pause"), `In progress${S.issues[j.job_id] === "overrun" ? ", running over" : ""}`)
      : st === "driving" ? h("div", { class: "sheetbanner drive" }, ic("navigation"), "Dispatched. You are on your way to this stop.")
        : st === "reassigning" ? h("div", { class: "sheetbanner prog" }, ic("route"), `Re-rostering to another crew${issue ? `: ${issue.label}` : ""}`)
          : st === "reassigned" ? h("div", { class: "sheetbanner" }, ic("circle-check"), `Re-rostered${to ? ` to ${to.name}, ${to.persona}` : " to another crew"}`)
            : emer ? h("div", { class: "sheetbanner emer" }, ic("zap"), "Emergency: work this first") : null;
  const cell = (k, v) => h("div", {}, h("div", { class: "fsub" }, k), h("b", {}, v ?? "-"));
  const reportable = ["planned", "driving", "in_progress"].includes(st);
  return h("div", { class: "fsheet-bg", onclick: () => { S.selected = null; render(); } },
    h("div", { class: "fsheet", onclick: (e) => e.stopPropagation() }, banner,
      h("div", { class: "fsheet-head" }, glass(typeIcon(j), tone(j), true),
        h("div", { style: "flex:1;min-width:0" }, h("div", { class: "fsheet-t" }, j.type_label),
          h("div", { class: "fsub" }, `Stop ${pos + 1}, ${j.job_id}`, j.fixed ? h("span", { class: "locktag", style: "margin-left:6px" }, ic("lock"), "booked") : null)),
        h("button", { class: "iconbtn", "aria-label": "Close", onclick: () => { S.selected = null; render(); } }, ic("x"))),
      j.title ? h("div", { class: "fsheet-what" }, j.title) : null,
      h("div", { class: "fsheet-grid" },
        cell("Arrive", s ? hhmm(s.start) : null), cell("Finish", s ? hhmm(s.finish) : null),
        cell("Drive here", s ? `${Math.round(s.travelMin)} min, ${s.travelKm.toFixed(1)} km` : null), cell("On site", `${j.duration_min} min`),
        cell("Window", `${hhmm(j.window_start_min)} to ${hhmm(j.window_end_min)}`), cell("Priority", `${j.priority} of 5`),
        cell("Funded by", j.funding_label), cell("Skill and ticket", [j.required_skill_label, j.required_ticket_label].filter(Boolean).join(", ")),
        cell("Address", [j.address, j.suburb].filter(Boolean).join(", ") || null), cell("Customers", j.customers ? Number(j.customers).toLocaleString("en-AU") : "None off supply"),
        j.asset_id || j.feeder_id ? cell("Network", [j.asset_id, j.feeder_id].filter(Boolean).join(" on ")) : null),
      h("div", { class: "fsheet-actions" },
        h("button", { class: "btn big nav", "data-tip": REAL_PHONE ? "Turn-by-turn directions in the Google Maps app." : "Driving directions in Google Maps (new tab).", onclick: () => directions(j) }, ic("navigation"), "Directions"),
        st === "planned" ? h("button", { class: "btn big primary", "data-tip": "Mark this job started. The dispatcher sees it live.", onclick: () => setStatus(j.job_id, "in_progress") }, ic("play"), "Start job") : null,
        st === "driving" ? h("button", { class: "btn big primary", "data-tip": "You have arrived: start the job.", onclick: () => setStatus(j.job_id, "in_progress") }, ic("play"), "Arrived, start job") : null,
        st === "in_progress" ? h("button", { class: "btn big good", "data-tip": "Mark this job complete.", onclick: () => setStatus(j.job_id, "done") }, ic("circle-check"), "Complete job") : null,
        st === "done" ? h("button", { class: "btn big revert", "data-tip": "Marked complete by mistake? Set it back to in progress.", onclick: () => setStatus(j.job_id, "in_progress") }, ic("circle-pause"), "Mark in progress") : null,
        reportable ? h("button", { class: "btn big report", "data-tip": "Report a change: this stop cannot be done, or you cannot carry on today.", onclick: () => { S.reporting = !S.reporting; render(); } }, ic("triangle-alert"), "Report a change") : null,
        !reported(S.selected) ? h("button", { class: "btn big fr", "data-tip": "Record what the crew found and did on this job: the response, the fault, switching, customers restored, materials, photos and the outcome.", onclick: () => openFieldReport(j) }, ic("clipboard"), "File field report") : null,
        reported(S.selected) ? h("button", { class: "btn big revert", "data-tip": "Reported in error? Put this stop back on your plan.", onclick: () => restore(j) }, ic("circle-check"), "Restore to plan") : null,
        !immovable(S.selected) ? h("div", { class: "fmove big" },
          h("button", { disabled: !canMove(pos, -1), onclick: () => move(pos, -1) }, ic("chevron-up"), "Earlier"),
          h("button", { disabled: !canMove(pos, 1), onclick: () => move(pos, 1) }, ic("chevron-down"), "Later")) : null),
      S.reporting && reportable ? h("div", { class: "freasons" },
        h("div", { class: "freasons-h" }, "This stop"),
        h("div", { class: "freason-grid" }, REASONS.filter((r) => r.scope === "stop").map((r) => h("button", { class: `freason ${r.tone}`, onclick: () => report(j, r.key),
          "data-tip": r.act === "park" ? "Parks this stop for the dispatcher. You keep working your other stops; it is not handed to another crew." : "Flags that this job is running over." }, ic(r.icon), r.label))),
        h("div", { class: "freasons-h" }, "I cannot continue today"),
        h("div", { class: "freason-grid" }, REASONS.filter((r) => r.scope === "crew").map((r) => h("button", { class: `freason ${r.tone}`, onclick: () => report(j, r.key),
          "data-tip": "Stands you down for the rest of the day. Your remaining stops are re-rostered to the nearest qualified crews." }, ic(r.icon), r.label))),
        h("div", { class: "note" }, "A blocker on a stop is parked for the dispatcher. If you cannot continue, your remaining stops go to the nearest qualified crew with a valid ticket. Taps made in a black spot are queued and sent when signal returns.")) : null,
      j.fixed ? h("div", { class: "note" }, ic("lock"), "A booked appointment keeps its time. The stops around it can be re-ordered.") : null));
}

function pickers(phone) {
  const crew = h("select", { class: "fworker", "aria-label": "Crew", "data-tip": "Choose a crew to load their day.", onchange: (e) => loadItin(S.region, e.target.value) },
    S.workers.map((w) => h("option", { value: w.crew_id, selected: w.crew_id === S.crewId ? "" : null }, `${w.name}, ${w.persona}, ${w.jobs} ${plural(w.jobs, "stop")}${w.stood_down ? ", stood down" : ""}`)));
  const region = h("select", { class: "fregion", "aria-label": "Region", "data-tip": "Choose a depot region to see its crews.", onchange: (e) => pickRegion(e.target.value) },
    S.regions.map((r) => h("option", { value: r.id, selected: r.id === S.region ? "" : null }, r.name)));
  return phone ? [crew, region] : [crew, h("div", { class: "fsub fsub-row" }, region, h("span", {}, S.itin.crew.persona))];
}

function render() {
  const keep = [...root.querySelectorAll(".fitin, .pjobs, .fsheet, .freport")].map((e) => [e.className, e.scrollTop]);
  root.className = "";
  root.innerHTML = "";
  if (!S.itin) { root.append(h("div", { class: "floading" }, "Loading the field plan")); return; }
  const p = plan(), cap = shiftCap(), ok = p.freeAt <= cap, done = S.order.filter((i) => status(i) === "done").length;
  const free = (cls) => h("div", { class: `ffreev num${p.freeAt > cap ? " over" : ""}${cls || ""}` }, hhmm(p.freeAt));
  const toasts = [S.reject ? h("div", { class: "ftoast", onclick: () => { say("reject", null); render(); } }, ic("triangle-alert"), h("span", {}, S.reject)) : null,
    S.notice ? h("div", { class: "ftoast ack", onclick: () => { say("notice", null); render(); } }, ic("circle-check"), h("span", {}, S.notice)) : null];
  const compliance = ok ? `within the ${hhmm(cap)} shift limit` : `over the ${hhmm(cap)} shift limit`;
  const app = h("div", { class: `fieldapp view-${S.view}${REAL_PHONE ? " realphone" : ""}` },
    h("div", { class: "fhead" },
      h("div", { class: "fbrand" }, "Field ", h("b", {}, "Service")),
      h("div", { class: "fwho" }, glass("hard-hat", "accent"), h("div", {}, ...pickers(false))),
      h("div", { class: "ffree", "data-tip": "When your last job ends and you are back at the depot, on real road times. Updates as you re-order." },
        glass("flag", "good"), h("div", {}, h("div", { class: "fsub" }, "Free at"), free())),
      h("div", { class: "fstats", "data-tip": `Your day and shift compliance: the limit is ${hhmm(cap)} (12 hours under the enterprise agreement and fatigue rules). Re-orders that miss an appointment or run past it are blocked.` },
        h("span", {}, `${done} of ${S.order.length} ${plural(S.order.length, "stop")} done, ${Math.round(p.totalKm)} km${S.routing ? ", working out the road route" : ""}`),
        h("span", { class: `compliance ${ok ? "ok" : "bad"}` }, ic(ok ? "circle-check" : "triangle-alert"), compliance)),
      h("div", { class: "fviewtabs", "data-tip": "Tablet or computer layout, or the phone layout. Both are live." },
        h("button", { class: S.view === "desktop" ? "on" : "", onclick: () => { S.view = "desktop"; render(); } }, ic("navigation"), "Tablet"),
        h("button", { class: S.view === "phone" ? "on" : "", onclick: () => { S.view = "phone"; S.mapMax = false; render(); } }, ic("map-pin"), "Phone")),
      h("a", { class: "ghostlink", href: "/#/res/control", "data-tip": "Back to Field staff management in the control room." }, ic("navigation"), "Field staff management")),
    h("div", { class: `offline${S.offline ? " on" : ""}`, onclick: goOnline, "data-tip": "Simulate a mobile black spot. Taps queue and sync when signal returns." },
      S.offline ? `Offline (black spot): ${S.queue.length} change${S.queue.length === 1 ? "" : "s"} queued, sending when signal returns. Tap to reconnect.` : "Online: your day syncs live. Tap to simulate a black spot."),
    S.view === "desktop" ? toasts : null);
  if (S.view === "desktop") {
    app.append(h("div", { class: "fbody" },
      h("div", { class: "fmapwrap" }, MAP.el, h("button", { class: "fzoomreset", "data-tip": "Frame the whole of today's route.", onclick: fitNow }, ic("maximize"), "Reset zoom")),
      renderItin(p)), renderSheet(p) || [], renderFiling() || []);
  } else {
    app.append(h("div", { class: "phonewrap" }, h("div", { class: "phone-bezel" }, h("div", { class: "phone-notch" }),
      h("div", { class: "phone-screen" },
        h("div", { class: "phead" }, glass("hard-hat", "accent"), h("div", { class: "phead-sel" }, ...pickers(true)),
          h("div", { class: "phead-r" }, h("div", { class: "fsub" }, "Free at"), free())),
        h("div", { class: `pcompliance ${ok ? "ok" : "bad"}` }, ic(ok ? "circle-check" : "triangle-alert"), `${done} of ${S.order.length} ${plural(S.order.length, "stop")} done, ${compliance}`),
        ...toasts,
        S.mapMax ? null : h("div", { class: "pjobs" }, renderItin(p)),
        h("div", { class: `pmapwrap${S.mapMax ? " full" : ""}` }, MAP.el,
          h("button", { class: "pmaxbtn", onclick: () => { S.mapMax = !S.mapMax; render(); } }, ic("maximize"), S.mapMax ? "Show jobs" : "Full map"),
          h("button", { class: "fzoomreset phone", "aria-label": "Frame the route", onclick: fitNow }, ic("maximize"))),
        renderSheet(p), renderFiling()))));
  }
  root.append(app);
  for (const [cls, top] of keep) { const e = root.getElementsByClassName(cls.split(" ")[0])[0]; if (e) e.scrollTop = top; }
  requestAnimationFrame(layoutMap);
}

// ---------------------------------------------------------------- field status report and asset photos (filed to Lakebase; the control room reads them)
const MAX_PX = 1600;   // a phone photo is shrunk to this before it is sent
const sendOk = async (p, body) => {   // a POST that throws the server's message (a 400 carries the crew's message to read)
  const r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText || "The request failed");
  return j;
};
const nowHhmm = () => { const d = new Date(); return hhmm(d.getHours() * 60 + d.getMinutes()); };
const hhmmToIso = (v) => {   // a time picked today ("08:15") as an ISO instant, or null when none was given
  if (!v) return null;
  const [hh, mm] = v.split(":").map(Number), d = new Date();
  d.setHours(hh, mm, 0, 0);
  return d.toISOString();
};
function shrinkPhoto(file) {   // the picked photo as a JPEG data URL no wider or taller than MAX_PX
  return new Promise((resolve, reject) => {
    const img = new Image(), url = URL.createObjectURL(file);
    img.onload = () => {
      const k = Math.min(1, MAX_PX / Math.max(img.naturalWidth, img.naturalHeight));
      const c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(img.naturalWidth * k));
      c.height = Math.max(1, Math.round(img.naturalHeight * k));
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      resolve(c.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("That file is not a photo this phone can read. Try another.")); };
    img.src = url;
  });
}

// ---- the asset photo sheet: take or choose a photo, tag the asset or device, add a note, send (the AI check and review follow)
function openPhoto(jobId) {
  const j = jobId ? jobs().find((x) => x.job_id === jobId) : null;
  S.photo = { jobId: jobId || null, tag: j && j.asset_id ? { asset_id: j.asset_id, label: j.asset_id, cls: j.type_label } : null, query: "",
    dataUrl: null, note: "", busy: false, uploading: false, done: null, resultsEl: null, timer: null };   // busy: shrinking the photo
  S.selected = null;
  render();
}
function closePhoto() {
  if (S.photo && S.photo.timer) clearTimeout(S.photo.timer);
  S.photo = null;
  render();
}
function searchAssets(q) {   // only the results list is redrawn, so typing keeps its place
  const P = S.photo;
  if (!P) return;
  P.query = q;
  clearTimeout(P.timer);
  P.timer = setTimeout(async () => {
    if (!P.resultsEl) return;
    P.resultsEl.innerHTML = "";
    if (q.trim().length < 2) return;
    const r = await get(`/api/assets/search?q=${enc(q.trim())}`).catch(() => ({ assets: [] }));
    for (const a of r.assets || []) {
      P.resultsEl.append(h("button", { class: "fresult", type: "button", onclick: () => { P.tag = { asset_id: a.asset_id, label: a.name || a.asset_id, cls: a.asset_class_label }; P.query = ""; render(); } },
        h("b", {}, a.asset_id), h("span", { class: "fsub" }, [a.asset_class_label, a.feeder_id].filter(Boolean).join(", "))));
    }
    if (!(r.assets || []).length) P.resultsEl.append(h("div", { class: "fsub" }, "No asset matches that id or name."));
  }, 250);
}
function pickPhoto(e) {
  const file = e.target.files && e.target.files[0];
  if (!file || !S.photo) return;
  S.photo.busy = true; S.photo.done = null; render();
  shrinkPhoto(file).then((url) => { if (S.photo) S.photo.dataUrl = url; }).catch((err) => say("reject", err.message))
    .finally(() => { if (S.photo) S.photo.busy = false; render(); });
}
async function sendPhoto() {   // upload: the button reads "Uploading" and is disabled until the app answers, then "Upload photo" again
  const P = S.photo;
  if (!P || P.busy || P.uploading) return;
  if (!P.dataUrl) { say("reject", "Take or choose a photo first."); return render(); }
  if (!P.tag) { say("reject", "Tag the asset or device first: search its id or name and pick it from the list."); return render(); }
  if (S.offline) { say("reject", "Photos need a connection. Take the photo again once you have signal."); return render(); }
  P.uploading = true; P.done = null; render();
  try {
    const r = await sendOk("/api/assets/photo", { asset_id: P.tag.asset_id, image_b64: P.dataUrl.split(",")[1], note: P.note, source: "field", ref_id: P.jobId });
    if (S.report && P.jobId === S.report.jobId) S.report.photos.push({ photo_id: r.photo_id, asset_id: r.asset_id });
    Object.assign(P, { done: r, dataUrl: null, note: "" });
    say("notice", "Photo uploaded. A quick AI check runs now, then a reviewer confirms it in Asset management.");
  } catch (e) {
    say("reject", `The photo did not upload. ${sentence(e.message)}`);
  } finally {
    if (S.photo) S.photo.uploading = false;
  }
  render();
}
function photoSheet() {
  const P = S.photo, job = P.jobId ? jobs().find((x) => x.job_id === P.jobId) : null;
  const results = h("div", { class: "fresults" });
  P.resultsEl = results;
  const choose = h("label", { class: "fbtn" }, ic("camera"), P.dataUrl ? "Take or choose another photo" : "Take or choose a photo",
    h("input", { type: "file", accept: "image/*", capture: "environment", class: "fhidden", onchange: pickPhoto }));
  return h("div", { class: "fsheet-bg freport-bg" },
    h("div", { class: "fsheet freport", onclick: (e) => e.stopPropagation() },
      h("div", { class: "fsheet-head" }, glass("camera", "accent", true),
        h("div", { style: "flex:1;min-width:0" }, h("div", { class: "fsheet-t" }, "Asset photo"),
          h("div", { class: "fsub" }, job ? `For ${job.title || job.type_label}` : "Photograph an asset, tag it and send it for review")),
        h("button", { class: "iconbtn", "aria-label": "Close", onclick: closePhoto }, ic("x"))),
      h("div", { class: "fsec" }, h("div", { class: "fsec-h" }, "1. The photo"), choose,
        P.dataUrl ? h("img", { class: "fpreview", src: P.dataUrl, alt: "The photo to send" }) : null,
        P.busy ? h("div", { class: "fsub" }, "Working on the photo") : null),
      h("div", { class: "fsec" }, h("div", { class: "fsec-h" }, "2. The asset or device tag"),
        P.tag ? h("div", { class: "ftag" }, ic("map-pin"), h("b", {}, P.tag.asset_id), P.tag.cls ? h("span", { class: "fsub" }, P.tag.cls) : null,
          h("button", { class: "linkbtn", onclick: () => { P.tag = null; render(); } }, "Change")) : null,
        h("input", { class: "fsearch", type: "search", placeholder: "Search asset id or name, e.g. P215446", "aria-label": "Search asset",
          value: P.query, oninput: (e) => searchAssets(e.target.value) }),
        results),
      h("div", { class: "fsec" }, h("div", { class: "fsec-h" }, "3. A note (optional)"),
        h("textarea", { class: "fnote", rows: 2, placeholder: "What the photo shows", oninput: (e) => { P.note = e.target.value; } }, P.note)),
      P.done ? h("div", { class: "fdone" }, ic("circle-check"), h("span", {}, `Photo of ${P.done.asset_id || P.tag?.asset_id || "the asset"} uploaded. It is waiting for the AI check and a reviewer.`)) : null,
      h("div", { class: "fsheet-actions" },
        h("button", { class: "btn big primary", disabled: P.uploading || P.busy ? "" : null, "aria-busy": P.uploading ? "true" : null, onclick: sendPhoto },
          ic(P.uploading ? "loader" : "upload"), P.uploading ? "Uploading" : "Upload photo"),
        h("button", { class: "btn big", onclick: closePhoto }, "Done"))));
}

// ---- the field status report: opened from a job's sheet, filed to the job, read by Situations, Resources and Operations
function openFieldReport(j) {
  S.report = { jobId: j.job_id, title: j.title || j.type_label, photos: [], saving: false, error: null, meta: null,
    f: { arrived: nowHhmm(), response_note: "", patrol_complete: false, fault_located: false, fault_kind: "", fault_ref: "", segments_total: "",
      segments_cleared: "", switching_executed: false, customers_restored: "", access_status: "clear", hazards: [], materials: {}, notes: "", outcome: "" } };
  S.selected = null;
  render();
  get(`/api/fieldreport/meta?job_id=${enc(j.job_id)}`).then((m) => { if (S.report && S.report.jobId === j.job_id) { S.report.meta = m; render(); } })
    .catch(() => { say("reject", "The report form could not load its choices. Check the connection and try again."); render(); });
}
async function fileReport() {
  const R = S.report;
  if (!R || R.saving) return;
  const f = R.f;
  if (!f.outcome) { R.error = "Choose how the job is left: fixed, a temporary repair, or needs follow up."; return render(); }
  const n = (v) => (v === "" || v === null || v === undefined ? null : Number(v));
  const body = { job_id: R.jobId, crew_id: S.crewId, arrived_at: hhmmToIso(f.arrived), response_note: f.response_note, patrol_complete: f.patrol_complete,
    fault_located: f.fault_located, fault_kind: f.fault_kind || null, fault_ref: f.fault_ref || null, segments_total: n(f.segments_total),
    segments_cleared: n(f.segments_cleared), switching_executed: f.switching_executed, customers_restored: n(f.customers_restored) || 0,
    access_status: f.access_status || null, hazards: f.hazards,
    materials: Object.entries(f.materials).filter(([, q]) => Number(q) > 0).map(([code, q]) => ({ code, qty: Number(q) })),
    notes: f.notes, outcome: f.outcome, photo_ids: R.photos.map((p) => p.photo_id) };
  if (S.offline) {   // a black spot: kept on the phone, sent when signal returns (goOnline flushes the queue)
    S.reportQueue.push(body); S.report = null;
    say("notice", "No signal. The report is saved on this phone and sends when signal returns.");
    return render();
  }
  R.saving = true; R.error = null; render();
  try {
    await sendOk("/api/fieldreport/submit", body);
    S.report = null;
    say("notice", "Field report filed. The control room and the situation can now see it.");
  } catch (e) {
    R.saving = false; R.error = e.message;
  }
  render();
}
function flushReports() {
  const q = S.reportQueue.splice(0);
  q.forEach((b) => sendOk("/api/fieldreport/submit", b).catch(() => S.reportQueue.push(b)));
  if (q.length) say("notice", `${q.length} queued field report${q.length === 1 ? "" : "s"} sent now that you are online.`);
}
function reportSheet() {
  const R = S.report, f = R.f, m = R.meta, job = m && m.job;
  const chip = (on, label, onclick) => h("button", { class: `fchip${on ? " on" : ""}`, type: "button", onclick }, on ? ic("circle-check") : null, label);
  const sec = (t, ...kids) => h("div", { class: "fsec" }, h("div", { class: "fsec-h" }, t), ...kids);
  const flag = (key, label) => chip(f[key], label, () => { f[key] = !f[key]; render(); });
  const pick = (key, code, label) => chip(f[key] === code, label, () => { f[key] = f[key] === code ? "" : code; render(); });
  const num = (key, label) => h("label", { class: "fnum" }, label, h("input", { type: "number", inputmode: "numeric", min: "0", value: f[key],
    oninput: (e) => { f[key] = e.target.value; } }));
  const toggles = (list, key) => list.map((o) => chip(f[key].includes(o.code), o.label, () => {
    f[key] = f[key].includes(o.code) ? f[key].filter((x) => x !== o.code) : [...f[key], o.code]; render(); }));
  const mats = (m ? m.materials : []).map((mt) => {
    const on = f.materials[mt.code] !== undefined;
    return h("div", { class: "fmat" }, chip(on, mt.label, () => { if (on) delete f.materials[mt.code]; else f.materials[mt.code] = "1"; render(); }),
      on ? h("input", { class: "fqty", type: "number", inputmode: "numeric", min: "0", value: f.materials[mt.code], "aria-label": `Quantity of ${mt.label}`,
        oninput: (e) => { f.materials[mt.code] = e.target.value; } }) : null);
  });
  const outs = m ? m.outcomes : [];
  return h("div", { class: "fsheet-bg freport-bg" },
    h("div", { class: "fsheet freport", onclick: (e) => e.stopPropagation() },
      h("div", { class: "fsheet-head" }, glass("clipboard", "accent", true),
        h("div", { style: "flex:1;min-width:0" }, h("div", { class: "fsheet-t" }, "Field report"),
          h("div", { class: "fsub" }, `${R.title}${job && job.asset_id ? `, ${job.asset_id}${job.asset_class ? ` (${job.asset_class})` : ""}` : ""}`)),
        h("button", { class: "iconbtn", "aria-label": "Close", onclick: () => { if (!R.saving) { S.report = null; render(); } } }, ic("x"))),
      sec("How the crew responded", h("div", { class: "frow" }, h("label", { class: "fnum" }, "Arrived on site",
        h("input", { type: "time", value: f.arrived, oninput: (e) => { f.arrived = e.target.value; } }))),
        h("textarea", { class: "fnote", rows: 2, placeholder: "How you got in, and what you found on arrival", oninput: (e) => { f.response_note = e.target.value; } }, f.response_note)),
      sec("Patrol and fault", h("div", { class: "fchips" }, flag("patrol_complete", "Patrol complete"), flag("fault_located", "Fault located")),
        h("div", { class: "fchips" }, (m ? m.fault_kinds : []).map((o) => pick("fault_kind", o.code, o.label))),
        h("input", { class: "fsearch", placeholder: `Pole or span where the fault was${job && job.asset_id ? ` (plan: ${job.asset_id})` : ""}`,
          value: f.fault_ref, oninput: (e) => { f.fault_ref = e.target.value; } })),
      sec("Segments and switching", h("div", { class: "frow" }, num("segments_total", "Segments on the route"), num("segments_cleared", "Segments cleared"),
        num("customers_restored", "Customers restored")), h("div", { class: "fchips" }, flag("switching_executed", "Switching executed"))),
      sec("Access and hazards", h("div", { class: "fchips" }, (m ? m.access : []).map((o) => pick("access_status", o.code, o.label))),
        h("div", { class: "fchips" }, toggles(m ? m.hazards : [], "hazards"))),
      sec("Materials used", h("div", { class: "fmats" }, mats.length ? mats : h("div", { class: "fsub" }, "Loading the list"))),
      sec("Photos (optional)", R.photos.map((p) => h("div", { class: "fphoto" }, ic("camera"), `Photo sent for ${p.asset_id}`)),
        h("button", { class: "fbtn", type: "button", onclick: () => openPhoto(R.jobId) }, ic("camera"), "Add a photo for this job")),
      sec("Notes", h("textarea", { class: "fnote", rows: 3, placeholder: "Anything the next crew or the engineer needs to know", oninput: (e) => { f.notes = e.target.value; } }, f.notes)),
      sec("How the job is left", h("div", { class: "fouts" }, outs.map((o) => h("button", { class: `fout ${o.code}${f.outcome === o.code ? " on" : ""}`, type: "button",
        onclick: () => { f.outcome = o.code; R.error = null; render(); } }, o.label)))),
      R.error ? h("div", { class: "ferr" }, ic("triangle-alert"), R.error) : null,
      h("div", { class: "fsheet-actions" },
        h("button", { class: "btn big good", disabled: R.saving ? "" : null, onclick: fileReport }, ic("circle-check"), R.saving ? "Filing" : "File report"),
        h("button", { class: "btn big", onclick: () => { if (!R.saving) { S.report = null; render(); } } }, "Cancel"))));
}
function renderFiling() {
  if (S.photo) return photoSheet();
  if (S.report) return reportSheet();
  return null;
}


// plain-English notes after a two second hover (desktop only: a tap on a phone would leave them stuck)
function tips() {
  if (REAL_PHONE) return;
  const tip = h("div", { class: "ftip" });
  document.body.append(tip);
  let timer = null, cur = null;
  document.addEventListener("mouseover", (e) => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (!t || t === cur) return;
    cur = t; clearTimeout(timer);
    timer = setTimeout(() => { tip.textContent = t.dataset.tip; const r = t.getBoundingClientRect(); tip.style.left = `${Math.max(8, Math.min(innerWidth - 300, r.left))}px`; tip.style.top = `${r.bottom + 8}px`; tip.classList.add("on"); }, 2000);
  });
  document.addEventListener("mouseout", (e) => { if (e.target.closest && e.target.closest("[data-tip]")) { clearTimeout(timer); tip.classList.remove("on"); cur = null; } });
}

async function init() {
  tips();
  const m = await get("/api/field/meta");
  S.regions = m.regions || [];
  const valid = (r) => r && S.regions.some((x) => x.id === r);
  let saved = null;
  try { saved = localStorage.getItem("field-region"); } catch (_) { /* private mode */ }
  const q = new URLSearchParams(location.search);   // /field?region=PEN&crew_id=PEN-LN-07 opens that crew (the control room links here)
  await pickRegion([q.get("region"), saved, m.default_region].find(valid) || (S.regions[0] || {}).id, q.get("crew_id") || undefined);
  setInterval(poll, 15000);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/static/field-sw.js", { scope: "/field" }).catch(() => {});
}
init().catch((e) => { root.className = "floading"; root.textContent = `The field plan could not load. ${sentence(e.message)} Check the connection and refresh.`; });
