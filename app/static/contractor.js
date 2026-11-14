// The vegetation contractor's field app: the field app's look and layout (tablet or phone, a map with the route, the stops, a sheet for
// the stop in hand) showing only what a vegetation contractor does. Its stops are the bays issued to the crew, in a route from the depot
// that takes the most urgent bays first; on a bay the crew starts the cut, takes photos (before, after, or of an exception), notes an
// exception and marks the cut complete. The photos are kept with the bay for the post-cut audit in Vegetation management. Every write goes
// into a queue kept on the device and is sent to /api/vegetation/contractor/sync, so a bay can be worked with no signal (each change
// carries its own id, so a repeat is ignored by the server). Self-contained (no app modules), like the field app.
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
const plural = (n, one, many = `${one}s`) => `${Number(n).toLocaleString("en-AU")} ${n === 1 ? one : many}`;
const sentence = (m) => (/[.!?]$/.test(String(m || "")) ? String(m) : `${m}.`);   // a server or browser message, given a full stop when it lacks one
const m1 = (v) => (v === null || v === undefined ? "-" : `${(+v).toFixed(1)} m`);
const m2 = (v) => (v === null || v === undefined ? "-" : `${(+v).toFixed(2)} m`);
const dte = (d) => (d ? new Date(d).toLocaleDateString("en-AU", { day: "numeric", month: "short" }) : "-");
const api = async (path, body) => {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.detail || j.error || r.statusText); e.status = r.status; throw e; }
  return j;
};

// Lucide line icons (lucide-static, ISC licence), the ones this view uses
const ICONS = {
  "arrow-down": `<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>`,
  "maximize": `<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>`,
  "house": `<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>`,
  "play": `<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>`,
  "circle-check": `<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>`,
  "circle-pause": `<circle cx="12" cy="12" r="10"/><line x1="10" x2="10" y1="15" y2="9"/><line x1="14" x2="14" y1="15" y2="9"/>`,
  "map-pin": `<path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/>`,
  "flag": `<path d="M4 22V4a1 1 0 0 1 .4-.8A6 6 0 0 1 8 2c3 0 5 2 7.333 2q2 0 3.067-.8A1 1 0 0 1 20 4v10a1 1 0 0 1-.4.8A6 6 0 0 1 16 16c-3 0-5-2-8-2a6 6 0 0 0-4 1.528"/>`,
  "leaf": `<path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/>`,
  "hard-hat": `<path d="M10 10V5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v5"/><path d="M14 6a6 6 0 0 1 6 6v3"/><path d="M4 15v-3a6 6 0 0 1 6-6"/><rect x="2" y="15" width="20" height="4" rx="1"/>`,
  "x": `<path d="M18 6 6 18"/><path d="m6 6 12 12"/>`,
  "navigation": `<polygon points="3 11 22 2 13 21 11 13 3 11"/>`,
  "camera": `<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>`,
  "triangle-alert": `<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>`,
  "clock": `<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>`,
  "upload": `<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>`,
};
const svg = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`;
const ic = (name) => h("span", { class: "ic", html: svg(name) });
const glass = (name, tone, lg) => h("span", { class: `gi${tone ? " gi-" + tone : ""}${lg ? " gi-lg" : ""}` }, ic(name));
const TONE = { high_risk: "bad", cut_required: "warn", planned: "info" };
const KINDS = { before: "Before the cut", after: "After the cut", exception: "An exception" };
const STATUS_TAG = { in_progress: ["prog", "cutting"], awaiting_audit: ["drive", "awaiting audit"], closed: ["done", "closed"] };
const SPEED = 42;   // km/h for the straight-line drive estimate when no road route is to hand
const REAL_PHONE = matchMedia("(max-width: 640px)").matches;

const S = { crews: [], crew: null, bays: [], selected: null, detail: null, view: REAL_PHONE ? "phone" : "desktop", mapMax: false, offline: false,
  busy: false, notice: null, route: null };

// ---------------------------------------------------------------- the queue (per crew, kept on the device)
const qKey = () => `veg-queue:${S.crew}`;
const queue = () => { try { return JSON.parse(localStorage.getItem(qKey()) || "[]"); } catch (e) { return []; } };
const saveQueue = (q) => {
  try { localStorage.setItem(qKey(), JSON.stringify(q)); return true; } catch (e) { say("This device is out of space for photos. Reconnect to send the waiting changes, then add the photo again."); return false; }
};
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);
const online = () => navigator.onLine && !S.offline;
function say(msg) { S.notice = msg; render(); clearTimeout(say.t); say.t = setTimeout(() => { S.notice = null; render(); }, 4500); }

async function flush() {
  const q = queue();
  if (!q.length || !S.crew || !online() || S.busy) { render(); return; }
  S.busy = true;
  const sent = new Set(q.map((o) => o.client_op_id));
  const dropSent = () => saveQueue(queue().filter((o) => !sent.has(o.client_op_id)));   // a change made while sending stays for the next go
  try {
    const r = await api("/api/vegetation/contractor/sync", { crew_id: S.crew, ops: q });
    dropSent();
    const bad = r.results.filter((x) => !x.ok);
    if (bad.length) say(`A change was not saved. ${sentence(bad[0].error)}`);
  } catch (e) {
    if (e.status && e.status >= 400 && e.status < 500) { dropSent(); say(`A change was not saved. ${sentence(e.message)}`); }   // refused: not retried forever
  } finally { S.busy = false; }
  await refresh();
}
async function act(op, done) {
  const q = queue();
  q.push({ ...op, client_op_id: uid() });
  if (!saveQueue(q)) return;
  if (done) say(online() ? done : `${done.replace(/\.$/, "")} once the signal returns.`);
  await flush();
}

// ---------------------------------------------------------------- data
async function loadCrews() {
  S.crews = (await api("/api/vegetation/contractor/crews")).crews;
  const want = new URLSearchParams(location.search).get("crew_id") || localStorage.getItem("veg-crew");
  S.crew = S.crews.some((c) => c.crew_id === want) ? want : (S.crews[0] || {}).crew_id;
}
async function loadBays() {
  if (!S.crew) return;
  try { S.bays = (await api(`/api/vegetation/contractor/bays?crew_id=${enc(S.crew)}`)).bays; } catch (e) { say(`The bays could not be loaded. ${sentence(e.message)}`); }
}
async function loadDetail(taskId) {
  S.detail = await api(`/api/vegetation/contractor/task/${enc(taskId)}?crew_id=${enc(S.crew)}`);
}
async function refresh() {
  await loadBays();
  if (S.selected) { try { await loadDetail(S.selected); } catch (e) { S.selected = null; S.detail = null; } }
  plan(); render();
}
const crew = () => S.crews.find((c) => c.crew_id === S.crew) || {};

// ---------------------------------------------------------------- the route: the open bays from the depot, most urgent first, then nearest next
const hav = (a, b) => {
  const R = 6371, dLa = (b.lat - a.lat) * Math.PI / 180, dLo = (b.lon - a.lon) * Math.PI / 180;
  const x = Math.sin(dLa / 2) ** 2 + Math.cos(a.lat * Math.PI / 180) * Math.cos(b.lat * Math.PI / 180) * Math.sin(dLo / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
};
const RANK = { in_progress: 0, issued: 1 };
const TIER_RANK = { high_risk: 0, cut_required: 1, planned: 2 };
function plan() {
  const c = crew(), home = { lat: c.depot_lat, lon: c.depot_lon };
  const open = S.bays.filter((b) => b.status === "issued" || b.status === "in_progress");
  const order = [];
  let at = home;
  const left = [...open];
  while (left.length) {   // a bay being cut first, then the most urgent tier, the nearest bay of it next
    left.sort((a, b) => (RANK[a.status] - RANK[b.status]) || (TIER_RANK[a.risk_tier_at_issue] ?? 3) - (TIER_RANK[b.risk_tier_at_issue] ?? 3)
      || (a.lat && at.lat ? hav(at, a) - hav(at, b) : 0));
    const next = left.shift();
    order.push(next);
    if (next.lat) at = next;
  }
  const legs = order.map((b, i) => { const from = i ? order[i - 1] : home; const km = b.lat && from.lat ? hav(from, b) * 1.3 : 0; return { km, min: km / SPEED * 60 }; });
  S.route = { home, order, legs, done: S.bays.filter((b) => b.status === "awaiting_audit" || b.status === "closed") };
}

// ---------------------------------------------------------------- the map
const MAP = { el: h("div", { id: "fmap" }), map: null, layer: null, set: "", size: "" };
function ensureMap() {
  if (MAP.map) return;
  MAP.map = L.map(MAP.el, { zoomControl: true }).setView([-33.75, 150.75], 10);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" }).addTo(MAP.map);
  MAP.layer = L.layerGroup().addTo(MAP.map);
}
const fitNow = () => {
  const pts = [S.route.home, ...S.route.order].filter((p) => p && p.lat).map((p) => [p.lat, p.lon]);
  if (pts.length) { try { MAP.map.fitBounds(pts, { padding: [45, 45], maxZoom: 15 }); } catch (_) { /* not laid out yet */ } }
};
function drawMap() {
  if (!MAP.map || !S.route) return;
  const { home, order } = S.route;
  MAP.layer.clearLayers();
  const line = [home, ...order, home].filter((p) => p && p.lat).map((p) => [p.lat, p.lon]);
  if (line.length > 1) {
    L.polyline(line, { color: "#ffffff", weight: 7, opacity: 0.9 }).addTo(MAP.layer);
    L.polyline(line, { color: "#0c5a66", weight: 4, opacity: 0.95, dashArray: "8 6" }).addTo(MAP.layer);
  }
  if (home.lat) L.marker([home.lat, home.lon], { icon: L.divIcon({ className: "pin pin-home", html: `<div class="pdot">${svg("house")}</div>`, iconSize: [30, 30], iconAnchor: [15, 15] }) })
    .bindTooltip(crew().depot_name || "Depot", { direction: "top" }).addTo(MAP.layer);
  order.forEach((b, k) => {
    if (!b.lat) return;
    const cls = `pin pin-${b.risk_tier_at_issue}${b.status === "in_progress" ? " pin-active" : ""}${S.selected === b.task_id ? " pin-sel" : ""}`;
    L.marker([b.lat, b.lon], { zIndexOffset: 400, icon: L.divIcon({ className: cls, html: `<div class="pinbubble">${k + 1}</div>`, iconSize: [34, 34], iconAnchor: [17, 17] }) })
      .bindTooltip(`${k + 1}. ${b.bay_label}, ${b.risk_tier_at_issue_label}`, { direction: "top", offset: [0, -6], className: "pinlabel" })
      .on("click", () => select(b.task_id)).addTo(MAP.layer);
  });
  const set = [S.crew, ...order.map((b) => b.task_id)].join("-");
  if (MAP.set !== set) { fitNow(); MAP.set = set; }
}
function layoutMap() {
  ensureMap();
  const size = `${S.view}-${S.mapMax}`;
  MAP.map.invalidateSize();
  drawMap();
  if (MAP.size !== size) { MAP.size = size; setTimeout(() => { MAP.map.invalidateSize(); fitNow(); }, 90); }
}

// ---------------------------------------------------------------- the bays list
async function select(taskId) {
  S.selected = taskId; S.detail = null; render();
  try { await loadDetail(taskId); } catch (e) { S.selected = null; say(`The bay could not be opened. ${sentence(e.message)}`); }
  render();
}
function renderItin() {
  const { order, legs, done } = S.route;
  const tiles = [];
  order.forEach((b, k) => {
    const tag = STATUS_TAG[b.status];
    tiles.push(h("div", { class: "fconn" }, ic("arrow-down"), `About ${Math.round(legs[k].min)} min drive, ${legs[k].km.toFixed(1)} km${k === 0 ? " from the depot" : ""}`));
    tiles.push(h("div", { class: `fstop${S.selected === b.task_id ? " sel" : ""}`, onclick: () => select(b.task_id) },
      h("div", { class: "fseq" }, k + 1), glass("leaf", TONE[b.risk_tier_at_issue]),
      h("div", { class: "fstop-main" },
        h("div", { class: "fstop-t" }, b.bay_label, h("span", { class: `tag veg-${b.risk_tier_at_issue}` }, (b.risk_tier_at_issue_label || "").toLowerCase()),
          tag ? h("span", { class: `tag ${tag[0]}` }, tag[1]) : null, b.attempt > 1 ? h("span", { class: "tag onhold" }, `attempt ${b.attempt}`) : null),
        h("div", { class: "fstop-s" }, `${b.feeder_id}. Needs ${m1(b.required_clearance_m)} clear and has ${m2(b.clearance_before_m)}. Due ${dte(b.due_date)}.`))));
  });
  return h("div", { class: "fitin" },
    h("div", { class: "fitin-head" }, h("h3", {}, "Your bays ", h("span", { class: "fsub" }, `(${plural(order.length, "to cut", "to cut")})`))),
    order.length ? h("div", { class: "ftiles" }, ...tiles) : h("div", { class: "fempty" }, "No bays are issued to this crew. New packages appear here as soon as the office issues them."),
    order.length ? h("div", { class: "fend" }, glass("flag", "good"), h("span", {}, "Then back to the depot.")) : null,
    done.length ? h("div", { class: "fparked" },
      h("div", { class: "fparked-h" }, ic("clock"), "Cut and with the office ", h("span", { class: "fsub" }, `(${done.length})`)),
      ...done.map((b) => h("div", { class: "fparked-row ct-done", onclick: () => select(b.task_id) }, glass("leaf", "good"),
        h("div", { class: "fparked-main" }, h("div", { class: "fparked-t" }, b.bay_label, h("span", { class: `tag ${STATUS_TAG[b.status][0]}` }, STATUS_TAG[b.status][1])),
          h("div", { class: "fsub" }, b.status === "closed" ? `Accepted and closed${b.closed_at ? ` on ${dte(b.closed_at)}` : ""}.` : "Cut complete. The post-cut check is with the office."))))) : null);
}

// ---------------------------------------------------------------- the bay sheet
function renderSheet() {
  if (!S.selected) return null;
  const close = () => { S.selected = null; S.detail = null; render(); };
  if (!S.detail) return h("div", { class: "fsheet-bg", onclick: close }, h("div", { class: "fsheet", onclick: (e) => e.stopPropagation() }, h("p", { class: "fsub" }, "Opening the bay.")));
  const t = S.detail.task, b = S.detail.bay || {}, photos = S.detail.photos || [], exc = S.detail.exceptions || [];
  const open = t.status === "issued" || t.status === "in_progress";
  const waiting = queue().filter((o) => o.task_id === t.task_id);
  const photosNow = photos.filter((p) => p.attempt === t.attempt).length + waiting.filter((o) => o.type === "photo").length;
  const last = (S.detail.reissues || []).slice(-1)[0];
  const banner = t.status === "closed" ? h("div", { class: "sheetbanner" }, ic("circle-check"), "Accepted and closed. Thank you.")
    : t.status === "awaiting_audit" ? h("div", { class: "sheetbanner drive" }, ic("clock"), "Cut complete. The post-cut check is with the office.")
      : t.status === "in_progress" ? h("div", { class: "sheetbanner prog" }, ic("circle-pause"), "Cutting in progress.")
        : t.attempt > 1 && last ? h("div", { class: "sheetbanner emer" }, ic("triangle-alert"), `Sent back for another cut (attempt ${t.attempt}). ${last.reason ? sentence(last.reason) : ""}`) : null;
  const cell = (k, v) => h("div", {}, h("div", { class: "fsub" }, k), h("b", {}, v ?? "-"));
  const pos = S.route.order.findIndex((x) => x.task_id === t.task_id);
  const sections = [];
  if (open) {
    const kind = h("select", { class: "ct-input" }, Object.entries(KINDS).map(([k, l]) => h("option", { value: k, selected: k === (t.status === "issued" ? "before" : "after") ? "" : null }, l)));
    const note = h("input", { type: "text", class: "ct-input", placeholder: "A note on the photo (optional)" });
    const file = h("input", { type: "file", accept: "image/*", capture: "environment", class: "ct-file" });
    file.addEventListener("change", async () => {
      const f = file.files && file.files[0];
      if (!f) return;
      try { await act({ type: "photo", task_id: t.task_id, kind: kind.value, note: note.value, image_b64: await shrink(f) }, "Photo added."); }
      catch (e) { say(`The photo was not added. ${sentence(e.message)}`); }
    });
    sections.push(h("div", { class: "ct-sec" }, h("div", { class: "ct-sec-h" }, ic("camera"), "Photos of the bay"),
      h("div", { class: "ct-row" }, kind, note), h("label", { class: "btn big nav ct-pick" }, ic("camera"), "Take or choose a photo", file)));
    const ek = h("select", { class: "ct-input" }, (S.detail.exception_kinds || []).map((k) => h("option", { value: k.code }, k.label)));
    const ed = h("textarea", { class: "ct-input", rows: "2", placeholder: "What you found, in your own words" });
    sections.push(h("div", { class: "ct-sec" }, h("div", { class: "ct-sec-h" }, ic("triangle-alert"), "Something stopping the cut?"),
      h("div", { class: "ct-row" }, ek), ed,
      h("button", { class: "btn big report", onclick: () => { if (!ed.value.trim()) { say("Describe what you found first."); return; } act({ type: "exception", task_id: t.task_id, kind: ek.value, detail: ed.value.trim() }, "Exception noted."); } },
        ic("triangle-alert"), "Note the exception")));
  }
  return h("div", { class: "fsheet-bg", onclick: close },
    h("div", { class: "fsheet", onclick: (e) => e.stopPropagation() }, banner,
      h("div", { class: "fsheet-head" }, glass("leaf", TONE[t.risk_tier_at_issue], true),
        h("div", { style: "flex:1;min-width:0" }, h("div", { class: "fsheet-t" }, b.bay_label || t.bay_label),
          h("div", { class: "fsub" }, `${pos >= 0 ? `Stop ${pos + 1}, ` : ""}${t.package_title}`)),
        h("button", { class: "iconbtn", "aria-label": "Close", onclick: close }, ic("x"))),
      h("div", { class: "fsheet-what" }, `${t.risk_tier_at_issue_label}. ${t.status_label}${t.attempt > 1 ? `, attempt ${t.attempt}` : ""}.`),
      h("div", { class: "fsheet-grid" },
        cell("Clear space needed", m1(t.required_clearance_m)), cell("Clearance now", m2(b.clearance_m)),
        cell("In 12 months", m2(Math.max(0, b.clearance_12m_m || 0))), cell("Short by", m2(Math.max(0, (t.required_clearance_m || 0) - (b.clearance_m || 0)))),
        cell("Species", b.dominant_species), cell("Bushfire-prone land", b.bushfire_prone ? "Yes" : "No"),
        cell("Hazard tree", b.hazard_tree ? "Yes, within falling distance" : "No"), cell("Due", dte(t.due_date)),
        cell("Feeder", b.feeder_id || t.feeder_id), cell("Pole lean", b.pole_lean_deg ? `${(+b.pole_lean_deg).toFixed(1)} degrees` : "None recorded")),
      h("div", { class: "fsheet-actions" },
        b.lat ? h("button", { class: "btn big nav", onclick: () => window.open(`https://www.google.com/maps/dir/?api=1&destination=${b.lat},${b.lon}`, "_blank", "noopener") }, ic("navigation"), "Directions") : null,
        t.status === "issued" ? h("button", { class: "btn big primary", onclick: () => act({ type: "start", task_id: t.task_id }, "Bay started.") }, ic("play"), "Start the bay") : null,
        t.status === "in_progress" ? h("button", { class: "btn big good", disabled: photosNow ? null : "", onclick: () => {
          if (!confirm("Mark this bay cut complete? It goes to the office for the post-cut check.")) return;
          act({ type: "complete", task_id: t.task_id, notes: "" }, "Cut marked complete.");
        } }, ic("circle-check"), "Mark the cut complete") : null),
      t.status === "in_progress" && !photosNow ? h("div", { class: "note" }, ic("camera"), "Add a photo of the cut before marking it complete.") : null,
      ...sections,
      h("div", { class: "ct-sec" }, h("div", { class: "ct-sec-h" }, ic("camera"), `Photos taken (${photos.length + waiting.filter((o) => o.type === "photo").length})`),
        photos.length ? h("div", { class: "ct-photos" }, photos.map((p) => h("figure", {}, p.image_url ? h("img", { src: p.image_url, alt: `${t.bay_label}, ${p.kind_label || p.kind}`, loading: "lazy" })
          : h("div", { class: "ct-nopic" }, "Picture no longer held"),
          h("figcaption", {}, `${p.kind_label || KINDS[p.kind] || ""}, attempt ${p.attempt}`)))) : h("p", { class: "fsub" }, "No photos have been taken of this bay yet."),
        waiting.filter((o) => o.type === "photo").length ? h("p", { class: "fsub" }, `${plural(waiting.filter((o) => o.type === "photo").length, "photo is", "photos are")} waiting to send.`) : null),
      exc.length ? h("div", { class: "ct-sec" }, h("div", { class: "ct-sec-h" }, ic("triangle-alert"), "Exceptions noted"),
        h("ul", { class: "ct-list" }, exc.map((e) => h("li", {}, `${e.kind_label}, attempt ${e.attempt}: ${e.detail || ""}`)))) : null));
}

// ---------------------------------------------------------------- the page
function pickers() {
  return h("select", { class: "fworker", "aria-label": "Crew", onchange: async (e) => {
    S.crew = e.target.value; localStorage.setItem("veg-crew", S.crew); S.selected = null; S.detail = null; MAP.set = "";
    await loadBays(); plan(); render(); flush();
  } }, S.crews.map((c) => h("option", { value: c.crew_id, selected: c.crew_id === S.crew ? "" : null }, `${c.crew_name}, ${c.region_name}`)));
}
function render() {
  if (!S.route) { root.className = "floading"; root.textContent = "Loading your bays"; return; }
  const keep = [...root.querySelectorAll(".fitin, .pjobs, .fsheet")].map((e) => [e.className, e.scrollTop]);
  root.className = ""; root.innerHTML = "";
  const c = crew(), q = queue().length, { order, done } = S.route;
  const due = order.map((b) => b.due_date).filter(Boolean).sort()[0];
  const status = `${plural(order.length, "bay", "bays")} to cut, ${plural(done.filter((b) => b.status === "awaiting_audit").length, "bay", "bays")} with the office`;
  const toast = S.notice ? h("div", { class: "ftoast ack", onclick: () => { S.notice = null; render(); } }, ic("circle-check"), h("span", {}, S.notice)) : null;
  const offlineBar = h("div", { class: `offline${online() ? "" : " on"}`, onclick: () => { S.offline = !S.offline; if (online()) flush(); else render(); } },
    online() ? (q ? `Sending ${plural(q, "change", "changes")}.` : "Online: your bays sync live. Tap to simulate a black spot.")
      : `Offline (black spot): ${plural(q, "change", "changes")} waiting, sent when the signal returns. Tap to reconnect.`);
  const app = h("div", { class: `fieldapp view-${S.view}${REAL_PHONE ? " realphone" : ""}` },
    h("div", { class: "fhead" },
      h("div", { class: "fbrand" }, "Field ", h("b", {}, "Service"), h("span", { class: "ct-badge" }, "Vegetation contractor")),
      h("div", { class: "fwho" }, glass("hard-hat", "accent"), h("div", {}, pickers(), h("div", { class: "fsub" }, `${c.region_name || ""}, ${c.depot_name || ""}. Contractor crew of ${c.crew_size || "-"}.`))),
      h("div", { class: "ffree" }, glass("clock", "good"), h("div", {}, h("div", { class: "fsub" }, "Next due"), h("div", { class: "ffreev num" }, due ? dte(due) : "None"))),
      h("div", { class: "fstats" }, h("span", {}, `${status}.`)),
      h("div", { class: "fviewtabs" },
        h("button", { class: S.view === "desktop" ? "on" : "", onclick: () => { S.view = "desktop"; render(); } }, ic("navigation"), "Tablet"),
        h("button", { class: S.view === "phone" ? "on" : "", onclick: () => { S.view = "phone"; S.mapMax = false; render(); } }, ic("map-pin"), "Phone"))),
    offlineBar, S.view === "desktop" ? toast : null);
  if (S.view === "desktop") {
    app.append(h("div", { class: "fbody" },
      h("div", { class: "fmapwrap" }, MAP.el, h("button", { class: "fzoomreset", onclick: fitNow }, ic("maximize"), "Reset zoom")), renderItin()), renderSheet() || []);
  } else {
    app.append(h("div", { class: "phonewrap" }, h("div", { class: "phone-bezel" }, h("div", { class: "phone-notch" }),
      h("div", { class: "phone-screen" },
        h("div", { class: "phead" }, glass("hard-hat", "accent"), h("div", { class: "phead-sel" }, pickers()),
          h("div", { class: "phead-r" }, h("div", { class: "fsub" }, "Next due"), h("div", { class: "ffreev num" }, due ? dte(due) : "None"))),
        h("div", { class: "pcompliance ok" }, ic("leaf"), `${status}.`), toast,
        S.mapMax ? null : h("div", { class: "pjobs" }, renderItin()),
        h("div", { class: `pmapwrap${S.mapMax ? " full" : ""}` }, MAP.el,
          h("button", { class: "pmaxbtn", onclick: () => { S.mapMax = !S.mapMax; render(); } }, ic("maximize"), S.mapMax ? "Show bays" : "Full map")),
        renderSheet()))));
  }
  root.append(app);
  for (const [cls, top] of keep) { const e = root.getElementsByClassName(cls.split(" ")[0])[0]; if (e) e.scrollTop = top; }
  requestAnimationFrame(layoutMap);
}

// ---------------------------------------------------------------- photos: shrunk to a JPEG the device can keep and the server reads
function shrink(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, 1600 / Math.max(img.width, img.height)), c = document.createElement("canvas");
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(img.src);
      resolve(c.toDataURL("image/jpeg", 0.82));
    };
    img.onerror = () => reject(new Error("The picture could not be read. Choose it again."));
    img.src = URL.createObjectURL(file);
  });
}

window.addEventListener("online", () => flush());
window.addEventListener("offline", () => render());
(async function start() {
  try {
    await loadCrews();
    await loadBays();
    plan(); render(); flush();
    setInterval(() => { if (online() && !S.busy) refresh(); }, 30000);   // packages issued in the office appear without a reload
  } catch (e) {
    root.className = "floading";
    root.textContent = `The app could not start. ${sentence(e.message)} Check the connection and reload.`;
  }
})();
