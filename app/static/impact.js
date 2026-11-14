// Impact analysis: who depends on what, three ways (the second with two views).
//   Flow view (Sankey): bulk supply point > transmission substation > zone substation > feeder > suburb for one region, counting a chosen
//     cohort with its true numbers.
//   Network spread, 3D: the network as floors over real geography, bulk supply at the top, transmission substations, zone substations,
//     feeders and suburbs below, threads between them.
//   Network spread, what if it fails (map): fail any bulk supply point, 132 kV circuit, transmission substation, subtransmission circuit,
//     zone substation or feeder and see who stays off and who is back-fed.
//   Share at every level (sunburst), two views: the asset hierarchy (bulk supply point > transmission substation > zone substation >
//     feeder > distribution substation) and customers by suburb (region > zone substation > feeder > suburb), zoomable.
// One payload (/api/impact) drives all four; the what-if rules live in simulate() and are shared by the map and the 3D view.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { api, h, n0, chart, mapIn, STATUS_COLOUR } from "./ui.js";

const MEASURES = [["customers", "All customers"], ["life_support", "Life support"], ["critical_sites", "Critical sites"],
  ["critical_no_generator", "Critical sites without a generator"], ["solar", "Rooftop solar"]];
const TEAL = ["#24575f", "#3c7c88", "#5490a8", "#83b8bf", "#b3d9d4"];
const STATE = { off: { c: "#d2462f", l: "Off until repaired" }, faulted: { c: "#8f2a1c", l: "On the faulted feeder" },
  partial: { c: "#e27b54", l: "Partly back-fed (tie short of capacity)" }, backfed: { c: "#c98a1e", l: "Back-fed through a tie" }, ok: { c: "#5490a8", l: "Unaffected" } };
const TOP_SUBURBS = 25;
const cnt = (n, one, many = `${one}s`) => `${n0(n)} ${+n === 1 ? one : many}`;
const SPUR = "#e27b54";   // an outage on a fused spur: a fuse has operated, the feeder itself stays in service
const legend = (items, inline = false) => h("div", { class: `im-key${inline ? " inline" : ""}` }, items.map(([c, l]) => h("span", { style: `color:${c}` }, l)));
const keyInControls = (el) => document.querySelector(".modal.im .im-ctl")?.append(el);   // charts keep their key in the controls row, top right

// ---------------------------------------------------------------- the payload, shaped once
function shape(d) {
  const zones = new Map(d.zones.map((z) => [z.zs_id, z]));
  const feeders = new Map(d.feeders.map((f) => [f.feeder_id, f]));
  const regions = [...new Map(d.feeders.map((f) => [f.area, f.area_label])).entries()].map(([code, label]) => ({ code, label }));
  const subs = new Map();   // suburb in a region: its rows by feeder and a weighted centroid
  for (const r of d.suburbs) {
    const k = `${r.suburb}|${r.area}`;
    if (!subs.has(k)) subs.set(k, { key: k, name: r.suburb, postcode: r.postcode, area: r.area, rows: [], lat: 0, lon: 0, w: 0 });
    const s = subs.get(k); s.rows.push(r);
    s.lat += r.lat * r.customers; s.lon += r.lon * r.customers; s.w += r.customers;
  }
  for (const s of subs.values()) {
    s.lat /= s.w; s.lon /= s.w;
    s.feeders = [...new Set(s.rows.map((r) => r.feeder_id))];
    const untied = s.rows.filter((r) => !(feeders.get(r.feeder_id)?.ties || []).length).reduce((a, r) => a + r.customers, 0);
    s.backup = untied === 0 ? "all" : untied === s.w ? "none" : "part";   // can its customers be back-fed if their feeder is lost?
  }
  for (const f of d.feeders) {   // a feeder's centre: where its customers are
    const rows = d.suburbs.filter((r) => r.feeder_id === f.feeder_id), w = rows.reduce((a, r) => a + r.customers, 0) || 1;
    f.lat = rows.reduce((a, r) => a + r.lat * r.customers, 0) / w; f.lon = rows.reduce((a, r) => a + r.lon * r.customers, 0) / w;
  }
  return { ...d, zoneMap: zones, feederMap: feeders, tsMap: new Map((d.transmission || []).map((t) => [t.ts_id, t])), regions, subs: [...subs.values()] };
}
const val = (r, m) => +(r[m] || 0);

// ---------------------------------------------------------------- what if it fails
// Lose a bulk supply point (its circuits go), a circuit, a zone substation or a feeder. A zone substation is off when every circuit into
// it is down. Its feeders, and a faulted feeder, lose supply; a feeder that has a tie to a feeder still in supply can be back-fed through
// it up to that feeder's spare capacity, otherwise it stays off until repaired. The faulted feeder itself is only partly restorable.
export function simulate(D, sel) {
  // the transmission level: a 132 kV circuit fails with its bulk supply point or on its own; a transmission substation is off when every
  // 132 kV circuit into it is down (or it is the one failed), and then so are its subtransmission circuits out to the zone substations
  const tlinesDown = new Set(D.tlines.filter((l) => (sel?.kind === "bsp" && l.bsp_id === sel.id) || (sel?.kind === "tline" && l.line_id === sel.id))
    .map((l) => l.line_id));
  const tsDown = new Set(D.transmission.filter((t) => (sel?.kind === "ts" && t.ts_id === sel.id) ||
    (D.tlines.some((l) => l.ts_id === t.ts_id) && D.tlines.filter((l) => l.ts_id === t.ts_id).every((l) => tlinesDown.has(l.line_id)))).map((t) => t.ts_id));
  const linesDown = new Set(D.lines.filter((l) => tsDown.has(l.ts_id) || (sel?.kind === "line" && l.line_id === sel.id)).map((l) => l.line_id));
  const zoneDown = new Set(D.zones.filter((z) => (sel?.kind === "zone" && z.zs_id === sel.id) ||
    ((z.line_ids || []).length && z.line_ids.every((id) => linesDown.has(id)))).map((z) => z.zs_id));
  const down = new Set(D.feeders.filter((f) => zoneDown.has(f.zs_id) || (sel?.kind === "feeder" && f.feeder_id === sel.id)).map((f) => f.feeder_id));
  // Back-feeding is limited by capacity: each feeder still in supply has its emergency rating less its own peak to spare (capped by the
  // tie), and the lost feeders share it, biggest first. A lost feeder gets back the share of its peak load its ties can carry.
  const spare = {}; for (const f of D.feeders) spare[f.feeder_id] = Math.max(0, (f.emergency_amps || 0) - (f.amps_peak_30d || 0));
  const feeder = {}, share = {};
  const lost = D.feeders.filter((f) => down.has(f.feeder_id)).sort((a, b) => (b.amps_peak_30d || 0) - (a.amps_peak_30d || 0));
  for (const f of D.feeders) if (!down.has(f.feeder_id)) { feeder[f.feeder_id] = "ok"; share[f.feeder_id] = 1; }
  for (const f of lost) {
    if (sel?.kind === "feeder" && f.feeder_id === sel.id) { feeder[f.feeder_id] = "faulted"; share[f.feeder_id] = 0; continue; }
    const need = Math.max(1, f.amps_peak_30d || 1); let got = 0;
    for (const t of [...(f.ties || [])].sort((a, b) => (spare[b.to] || 0) - (spare[a.to] || 0))) {
      if (down.has(t.to) || got >= need) continue;
      const take = Math.min(need - got, spare[t.to] || 0, 420); spare[t.to] -= take; got += take;
    }
    share[f.feeder_id] = got / need;
    feeder[f.feeder_id] = share[f.feeder_id] >= 0.99 ? "backfed" : share[f.feeder_id] > 0.01 ? "partial" : "off";
  }
  // customers on a partly back-fed feeder split between back-fed and off, in proportion
  const split = (fid, v) => { const st = feeder[fid]; if (st !== "partial") return { [st]: v }; return { backfed: v * share[fid], off: v * (1 - share[fid]) }; };
  const tally = {}; for (const k of Object.keys(STATE)) { tally[k] = {}; for (const [m] of MEASURES) tally[k][m] = 0; }
  for (const r of D.suburbs) for (const [m] of MEASURES) for (const [k, v] of Object.entries(split(r.feeder_id, val(r, m)))) tally[k][m] += v;
  const suburb = {};
  for (const s of D.subs) {
    const byState = {}; for (const r of s.rows) for (const [k, v] of Object.entries(split(r.feeder_id, r.customers))) byState[k] = (byState[k] || 0) + v;
    const worst = ["off", "faulted", "backfed", "ok"].find((k) => byState[k] >= 0.5) || "ok";
    suburb[s.key] = { worst, byState };
  }
  return { tlinesDown, tsDown, linesDown, zoneDown, down, feeder, share, tally, suburb, any: down.size > 0 };
}

function assetLabel(D, sel) {
  if (!sel) return "";
  if (sel.kind === "bsp") return D.bsps.find((b) => b.bsp_id === sel.id)?.name;
  if (sel.kind === "ts") return D.tsMap.get(sel.id)?.name;
  if (sel.kind === "tline") { const l = D.tlines.find((x) => x.line_id === sel.id); return `132 kV circuit ${l.circuit} from ${D.bsps.find((b) => b.bsp_id === l.bsp_id)?.name}`; }
  if (sel.kind === "line") { const l = D.lines.find((x) => x.line_id === sel.id); return `Supply circuit ${l.circuit} into ${D.zoneMap.get(l.zs_id)?.zs_name}`; }
  if (sel.kind === "zone") return D.zoneMap.get(sel.id)?.zs_name;
  return `${D.feederMap.get(sel.id)?.feeder_name} (${sel.id})`;
}

function pickerOptions(D) {
  const g = (label, items) => h("optgroup", { label }, items.map(([v, t]) => h("option", { value: v }, t)));
  return [h("option", { value: "" }, "Nothing failed"),
    g("Bulk supply points", D.bsps.map((b) => [`bsp:${b.bsp_id}`, b.name])),
    g("132 kV circuits into transmission substations", D.tlines.map((l) => [`tline:${l.line_id}`,
      `${D.tsMap.get(l.ts_id)?.name}, circuit ${l.circuit} from ${D.bsps.find((b) => b.bsp_id === l.bsp_id)?.name}`])),
    g("Transmission substations", D.transmission.map((t) => [`ts:${t.ts_id}`, t.name])),
    g("Subtransmission circuits into zone substations", D.lines.map((l) => [`line:${l.line_id}`,
      `${D.zoneMap.get(l.zs_id)?.zs_name}, circuit ${l.circuit} from ${D.tsMap.get(l.ts_id)?.name}${D.zoneMap.get(l.zs_id)?.single_fed ? " (its only supply)" : ""}`])),
    g("Zone substations", D.zones.map((z) => [`zone:${z.zs_id}`, z.zs_name])),
    g("Feeders", D.feeders.map((f) => [`feeder:${f.feeder_id}`, `${f.feeder_id}, ${f.feeder_name}${(f.ties || []).length ? "" : " (no backup tie)"}`]))];
}
const parseSel = (v) => { if (!v) return null; const [kind, id] = v.split(":"); return { kind, id }; };

function outcomePanel(D, sel, sim, m) {
  const box = h("div", { class: "im-out" });
  if (!sel) { box.append(h("p", { class: "muted small" }, "Choose an asset to fail, or click one.")); return box; }
  box.append(h("div", { class: "im-sel" }, assetLabel(D, sel)));
  const rows = [["customers", "Customers"], ["life_support", "Life support"], ["critical_sites", "Critical sites"], ["critical_no_generator", "  without a generator"], ["solar", "Rooftop solar"]];
  const t = h("table", { class: "t im-tally" }, h("thead", {}, h("tr", {}, h("th", {}, ""), ["off", "faulted", "backfed"].map((k) => h("th", { class: "r" }, h("i", { class: "im-dot", style: `background:${STATE[k].c}` }), STATE[k].l)))),
    h("tbody", {}, rows.map(([k, l]) => h("tr", { class: k === m ? "on" : "" }, h("td", {}, l), ["off", "faulted", "backfed"].map((s) => h("td", { class: "r num" }, n0(sim.tally[s][k])))))));
  box.append(t);
  const hit = D.subs.map((s) => ({ s, n: (sim.suburb[s.key].byState.off || 0) + (sim.suburb[s.key].byState.faulted || 0) + (sim.suburb[s.key].byState.backfed || 0),
    worst: sim.suburb[s.key].worst })).filter((x) => x.n >= 0.5).sort((a, b) => b.n - a.n).slice(0, 15);
  if (hit.length) box.append(h("h4", {}, "Suburb impact"), h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Suburb"), h("th", { class: "r" }, "Customers"))),
    h("tbody", {}, hit.map((x) => h("tr", {}, h("td", {}, h("i", { class: "im-dot", style: `background:${STATE[x.worst].c}` }), x.s.name), h("td", { class: "r num" }, n0(x.n)))))));
  return box;
}

// ---------------------------------------------------------------- tab 1: dependency Sankey
// Bulk supply point > zone substation > feeder > suburb. A zone fed from two bulk supply points is shown drawing on each equally (its
// normal running). Hover a node to see everything it depends on and everything that depends on it, all the way up and down; click to
// keep that view, click empty space to go back to the whole region.
const COLS = ["Bulk supply point", "Transmission substation", "Zone substation", "Feeder", "Suburb"];
function sankeyTab(D, root, ctl) {
  const region = ctl.region, m = ctl.measure;
  const fs = D.feeders.filter((f) => f.area === region);
  const subs = D.subs.filter((s) => s.area === region).map((s) => ({ s, v: s.rows.reduce((a, r) => a + val(r, m), 0) })).filter((x) => x.v > 0)
    .sort((a, b) => b.v - a.v);
  const top = new Set(subs.slice(0, TOP_SUBURBS).map((x) => x.s.key));
  const other = `Other suburbs (${Math.max(0, subs.length - TOP_SUBURBS)})`;
  const nodes = [], seen = new Set(), agg = new Map();
  const node = (name, depth, color, extra = {}) => { if (!seen.has(name)) { seen.add(name); nodes.push({ name, depth, itemStyle: { color }, ...extra }); } };
  const link = (a, b, v) => { const k = `${a}\u0000${b}`; agg.set(k, (agg.get(k) || 0) + v); };
  const fval = {}, zval = {};
  for (const s of D.subs.filter((x) => x.area === region)) for (const r of s.rows) {
    const v = val(r, m); if (!v) continue;
    const sub = top.has(s.key) ? `${s.name}${s.feeders.length === 1 ? " \u2022 One feeder" : ""}` : other;
    node(sub, 4, s.backup === "none" && top.has(s.key) ? "#c98a1e" : TEAL[3]);
    link(r.feeder_id, sub, v);
    fval[r.feeder_id] = (fval[r.feeder_id] || 0) + v;
  }
  for (const f of fs) if (fval[f.feeder_id]) {
    const z = D.zoneMap.get(f.zs_id);
    node(f.feeder_id, 3, f.tripped ? STATUS_COLOUR.red : f.spur_outage ? SPUR : STATUS_COLOUR[f.status],
      { label: { formatter: `${f.feeder_id}${(f.ties || []).length ? "" : "  No backup tie"}${f.tripped ? "  Tripped" : f.spur_outage ? "  Spur outage" : ""}` } });
    node(z.zs_name, 2, z.single_fed ? "#b4552d" : TEAL[1], { label: { formatter: `${z.zs_name.replace(" zone substation", "")}${z.single_fed ? "  Single supply" : ""}` } });
    link(z.zs_name, f.feeder_id, fval[f.feeder_id]);
    zval[z.zs_id] = (zval[z.zs_id] || 0) + fval[f.feeder_id];
  }
  for (const [zs, v] of Object.entries(zval)) {
    const z = D.zoneMap.get(zs), circuits = D.lines.filter((l) => l.zs_id === zs);
    for (const l of circuits) {   // each circuit carries an equal share: bulk supply point to transmission substation to zone substation
      const b = D.bsps.find((x) => x.bsp_id === l.bsp_id), bn = b.name.replace(" bulk supply point", " bulk supply");
      const t = D.tsMap.get(l.ts_id), tn = t.name.replace(" transmission substation", " transmission");
      node(bn, 0, "#1f3b42");
      node(tn, 1, "#2e6470");
      link(bn, tn, v / circuits.length);
      link(tn, z.zs_name, v / circuits.length);
    }
  }
  const links = [...agg.entries()].map(([k, value]) => { const [source, target] = k.split("\u0000"); return { source, target, value }; });
  // the dependency chain of a node: everything upstream of it and everything downstream of it
  const ups = new Map(), downs = new Map();
  for (const l of links) { (ups.get(l.target) || ups.set(l.target, []).get(l.target)).push(l.source); (downs.get(l.source) || downs.set(l.source, []).get(l.source)).push(l.target); }
  const walk = (start, next) => { const out = new Set(), q = [start]; while (q.length) for (const n of next.get(q.shift()) || []) if (!out.has(n)) { out.add(n); q.push(n); } return out; };
  const paint = (name) => {
    const up = name ? walk(name, ups) : null, down = name ? walk(name, downs) : null;
    const onNode = (n) => !name || n === name || up.has(n) || down.has(n);
    const onLink = (l) => !name || ((l.target === name || up.has(l.target)) && up.has(l.source)) || ((l.source === name || down.has(l.source)) && down.has(l.target));
    c.setOption({ series: [{ data: nodes.map((n) => ({ ...n, itemStyle: { ...n.itemStyle, opacity: onNode(n.name) ? 1 : .15 }, label: { ...(n.label || {}), color: onNode(n.name) ? "#1c2b3a" : "rgba(28,43,58,.25)", fontWeight: n.name === name ? 700 : 400 } })),
      links: links.map((l) => ({ ...l, lineStyle: { opacity: !name ? .28 : onLink(l) ? .6 : .03 } })) }] }, { lazyUpdate: false });
  };
  const head = h("div", { class: "im-cols" }, COLS.map((t) => h("span", {}, t)));
  const box = h("div", { class: "im-chart" });
  keyInControls(legend([["#b4552d", "Single supply circuit"], [STATUS_COLOUR.red, "Feeder needs action or tripped"], [SPUR, "Outage on a fused spur"], [STATUS_COLOUR.amber, "Feeder to watch"],
    [STATUS_COLOUR.green, "Feeder normal"], ["#c98a1e", "Suburb cannot be back-fed"], ["#3c6c78", "\u2022 One feeder"]], true));
  root.append(head, box);
  const L0 = 10, R0 = 190, NW = 11;
  const c = chart(box, { animation: false, tooltip: { trigger: "item", formatter: (p) => p.dataType === "edge" ? `${p.data.source} \u2192 ${p.data.target}: ${n0(p.data.value)}` : `${p.name}: ${n0(p.value)}` },
    series: [{ type: "sankey", data: nodes, links, emphasis: { disabled: true }, nodeGap: 5, nodeWidth: NW, layoutIterations: 64, nodeAlign: "justify", draggable: false,
      lineStyle: { color: "gradient", opacity: .28, curveness: .5 }, label: { color: "#1c2b3a", fontSize: 10.5 }, left: L0, right: R0, top: 4, bottom: 8 }] });
  const place = () => {   // the column headings sit over the four columns of nodes
    const w = box.clientWidth, step = (w - L0 - R0 - NW) / (COLS.length - 1);
    [...head.children].forEach((el, i) => { el.style.left = `${L0 + i * step}px`; });
  };
  const ro = new ResizeObserver(() => { c.resize(); place(); }); ro.observe(box); place();
  let locked = null;
  c.on("mouseover", (p) => { if (!locked) paint(p.dataType === "node" ? p.name : p.data.target); });
  c.on("mouseout", () => { if (!locked) paint(null); });
  c.on("click", (p) => { locked = p.dataType === "node" ? p.name : p.data.target; paint(locked); });
  c.getZr().on("click", (e) => { if (!e.target) { locked = null; paint(null); } });
}

// ---------------------------------------------------------------- tab 4: sunburst
const SUN_VIEWS = [["assets", "Asset hierarchy"], ["suburbs", "Customers by suburb"]];
const feederColour = (f) => f.tripped ? STATUS_COLOUR.red : f.spur_outage ? SPUR : (f.ties || []).length ? undefined : "#8f6a1c";
const ring = (r0, r, label, extra = {}) => ({ r0, r, label, ...extra });
// Labels follow the circle wherever they fit: a segment whose arc is long enough for its name, in a ring deep enough for the text, writes
// its label along the ring (tangential); the rest keep the ring's radial labels, cut to the ring's depth so every label stays inside the
// donut. Sized from the chart box, as at the first view.
function orient(data, levels, box) {
  const R = Math.min(box.clientWidth, box.clientHeight) / 2, px = (p) => parseFloat(p) / 100 * R;
  const sum = (n) => n.value ?? (n.children || []).reduce((t, c) => t + sum(c), 0);
  const total = data.reduce((t, n) => t + sum(n), 0) || 1;
  const walk = (nodes, depth) => {
    const L = levels[depth]; if (!L) return;
    const lab = L.label || {}, fs = lab.fontSize || 10, r0 = px(L.r0), r1 = px(L.r);
    if (lab.rotate === "radial" && lab.position !== "outside") lab.width = Math.max(24, r1 - r0 - 8);   // a radial label stays inside its ring
    for (const n of nodes) {
      const arc = (sum(n) / total) * Math.PI * (r0 + r1), need = String(n.name).length * fs * 0.55 + 6;
      if (lab.position !== "outside" && arc >= need && r1 - r0 >= fs + 4) n.label = { ...(n.label || {}), rotate: "tangential", width: arc };
      walk(n.children || [], depth + 1);
    }
  };
  walk(data, 1);
  return levels;
}

async function sunburstTab(D, root, ctl) {
  if (ctl.view === "suburbs") return suburbSunburst(D, root, ctl);
  D.ds ||= (await api("/api/impact/assets")).ds;   // the distribution substations load once, the first time the asset view opens
  if (root.isConnected) assetSunburst(D, root, ctl);
}

// Asset hierarchy: every level from where the transmission network hands over down to the distribution substation, sized by the chosen
// measure. A zone substation fed from two transmission substations sits under the one its first circuit comes from, marked as dual fed.
function assetSunburst(D, root, ctl) {
  const m = ctl.measure, dsBy = new Map();
  for (const d of D.ds) (dsBy.get(d.feeder_id) || dsBy.set(d.feeder_id, []).get(d.feeder_id)).push(d);
  const mainTs = (z) => (D.lines.filter((l) => l.zs_id === z.zs_id).sort((a, b) => a.circuit - b.circuit)[0] || {}).ts_id;
  const sources = (z) => [...new Set(D.lines.filter((l) => l.zs_id === z.zs_id).map((l) => l.ts_id))];
  const zoneNode = (z) => ({
    name: z.zs_name.replace(" zone substation", ""), zone: z,
    itemStyle: z.single_fed ? { color: "#b4552d" } : sources(z).length > 1 ? { color: "#2e7d6b" } : undefined,
    children: D.feeders.filter((f) => f.zs_id === z.zs_id).map((f) => ({
      name: f.feeder_id, itemStyle: feederColour(f) ? { color: feederColour(f) } : undefined,
      children: (dsBy.get(f.feeder_id) || []).filter((d) => val(d, m) > 0).map((d) => ({ name: d.ds_id, value: val(d, m), ds: d,
        itemStyle: d.customers_off ? { color: STATUS_COLOUR.red } : undefined })),
    })).filter((f) => f.children.length),
  });
  const data = D.bsps.map((b, bi) => ({
    name: b.name.replace(" bulk supply point", ""), itemStyle: { color: TEAL[bi % TEAL.length] },
    children: D.transmission.filter((t) => t.bsp_id === b.bsp_id).map((t) => ({
      name: t.name.replace(" transmission substation", ""),
      children: D.zones.filter((z) => mainTs(z) === t.ts_id).map(zoneNode).filter((z) => z.children.length),
    })).filter((t) => t.children.length),
  })).filter((b) => b.children.length);
  const box = h("div", { class: "im-chart" });
  keyInControls(legend([["#b4552d", "Single supply circuit"], ["#2e7d6b", "Fed from two transmission substations"], ["#8f6a1c", "No backup tie"],
    [STATUS_COLOUR.red, "Tripped, or behind a device that has operated"], [SPUR, "Outage on a fused spur"], ["#5f7185", "Click a ring to zoom in"]], true));
  root.append(box);
  const tip = (p) => {
    const path = p.treePathInfo.slice(1).map((x) => x.name).join(" › "), d = p.data || {};
    const z = d.zone && sources(d.zone).length > 1 ? `<br>Also fed from ${sources(d.zone).filter((t) => t !== mainTs(d.zone)).map((t) => D.tsMap.get(t)?.name).join(", ")}` : "";
    const off = d.ds?.customers_off ? `<br>${cnt(d.ds.customers_off, "customer", "customers")} behind a device that has operated` : "";
    return `${path}: ${n0(p.value)}${z}${off}`;
  };
  chart(box, { tooltip: { formatter: tip },
    series: [{ type: "sunburst", data, radius: ["4%", "86%"], sort: null, nodeClick: "rootToNode", emphasis: { focus: "ancestor" },
      itemStyle: { borderColor: "#fff", borderWidth: 1 }, label: { fontSize: 10, color: "#fff", minAngle: 5, overflow: "truncate" },
      levels: orient(data, [{}, ring("5%", "17%", { rotate: "tangential", fontSize: 11 }), ring("17%", "31%", { rotate: "radial", width: 70 }),
        ring("31%", "47%", { rotate: "radial", width: 80, minAngle: 6 }), ring("47%", "63%", { rotate: "radial", fontSize: 9, width: 80 }),
        ring("63%", "86%", { rotate: "radial", fontSize: 8, color: "#1c2b3a", minAngle: 2.5, width: 110 }, { itemStyle: { opacity: .9, borderWidth: 0.15 } })], box) }] });
}

// Customers by suburb: where the customers live, by the zone substation and feeder that supply them. A suburb served by several feeders
// appears under each of them (it is a grouping of customers, not an asset).
function suburbSunburst(D, root, ctl) {
  const m = ctl.measure;
  const data = D.regions.map((rg, ri) => ({
    name: rg.label, itemStyle: { color: TEAL[ri % TEAL.length] },
    children: D.zones.filter((z) => z.area === rg.code).map((z) => ({
      name: z.zs_name.replace(" zone substation", ""), itemStyle: z.single_fed ? { color: "#b4552d" } : undefined,
      children: D.feeders.filter((f) => f.zs_id === z.zs_id).map((f) => ({
        name: f.feeder_id, itemStyle: feederColour(f) ? { color: feederColour(f) } : undefined,
        children: D.suburbs.filter((r) => r.feeder_id === f.feeder_id && val(r, m) > 0).map((r) => ({ name: r.suburb, value: val(r, m) })),
      })).filter((f) => f.children.length),
    })).filter((z) => z.children.length),
  }));
  const box = h("div", { class: "im-chart" });
  keyInControls(legend([["#b4552d", "Single supply circuit"], ["#8f6a1c", "No backup tie"], [STATUS_COLOUR.red, "Tripped now"], [SPUR, "Outage on a fused spur"],
    ["#5f7185", "Click a ring to zoom in"]], true));
  root.append(box);
  chart(box, { tooltip: { formatter: (p) => `${p.treePathInfo.slice(1).map((x) => x.name).join(" › ")}: ${n0(p.value)}` },
    series: [{ type: "sunburst", data, radius: ["5%", "80%"], sort: null, nodeClick: "rootToNode", emphasis: { focus: "ancestor" },
      itemStyle: { borderColor: "#fff", borderWidth: 1 }, label: { fontSize: 10, color: "#fff", minAngle: 4, overflow: "truncate" },
      levels: orient(data, [{}, ring("6%", "22%", { rotate: "tangential", fontSize: 12 }), ring("22%", "42%", { rotate: "radial", width: 90, minAngle: 6 }),
        ring("42%", "60%", { rotate: "radial", fontSize: 9 }),
        ring("60%", "80%", { position: "outside", rotate: "radial", fontSize: 8.5, color: "#1c2b3a", minAngle: 3 }, { itemStyle: { opacity: .85 } })], box) }] });
}

// ---------------------------------------------------------------- tab 3: what if it fails (map)
function mapTab(D, root, ctl, net) {
  const wrap = h("div", { class: "im-split" }), mapBox = h("div", { class: "im-map" }), side = h("div", { class: "im-side" });
  const pick = h("select", { class: "im-pick" }, ...pickerOptions(D));
  pick.value = ctl.fail ? `${ctl.fail.kind}:${ctl.fail.id}` : "";
  wrap.append(mapBox, side); root.append(wrap);
  const m = mapIn(mapBox, [-33.66, 150.85], 9);
  mapBox.append(legend(Object.values(STATE).map((x) => [x.c, x.l])));
  const layers = L.layerGroup().addTo(m);
  const draw = () => {
    layers.clearLayers();
    const sim = simulate(D, ctl.fail);
    for (const f of net.feeders) {
      const s = sim.feeder[f.feeder_id];
      L.geoJSON(f.geo, { style: { color: STATE[s].c, weight: s === "ok" ? 1.8 : 3.2, opacity: s === "ok" ? .7 : 1 } })
        .on("click", () => select({ kind: "feeder", id: f.feeder_id })).bindTooltip(`${f.feeder_id}, ${f.feeder_name}`).addTo(layers);
    }
    for (const l of D.tlines) {
      const dn = sim.tlinesDown.has(l.line_id) || sim.tsDown.has(l.ts_id);
      L.geoJSON(l.geo, { style: { color: dn ? "#d2462f" : "#1f3b42", weight: 5, opacity: .85 } })
        .on("click", () => select({ kind: "tline", id: l.line_id })).bindTooltip(`132 kV circuit ${l.circuit} into ${D.tsMap.get(l.ts_id)?.name}`).addTo(layers);
    }
    for (const l of D.lines) {
      const dn = sim.linesDown.has(l.line_id);
      L.geoJSON(l.geo, { style: { color: dn ? "#d2462f" : "#24575f", weight: 3.4, dashArray: D.zoneMap.get(l.zs_id).single_fed ? null : "6 5", opacity: .9 } })
        .on("click", () => select({ kind: "line", id: l.line_id })).bindTooltip(`${l.kv} kV circuit ${l.circuit} into ${D.zoneMap.get(l.zs_id).zs_name}`).addTo(layers);
    }
    for (const t of D.transmission) {
      const dn = sim.tsDown.has(t.ts_id);
      L.circleMarker([t.lat, t.lon], { radius: 8, color: "#fff", weight: 2, fillColor: dn ? "#d2462f" : "#1f3b42", fillOpacity: 1 })
        .on("click", () => select({ kind: "ts", id: t.ts_id })).bindTooltip(`${t.name}: ${cnt(t.transformers, "transformer", "transformers")}, ${cnt(t.zone_count, "zone substation", "zone substations")}`).addTo(layers);
    }
    for (const s of D.subs) {
      const st = sim.suburb[s.key];
      L.circleMarker([s.lat, s.lon], { radius: 3 + Math.sqrt(s.w) / 9, color: "#fff", weight: 1, fillColor: STATE[st.worst].c, fillOpacity: st.worst === "ok" ? .55 : .9 })
        .bindTooltip(`${s.name} ${s.postcode}: ${cnt(s.w, "customer", "customers")} on ${cnt(s.feeders.length, "feeder", "feeders")}`).addTo(layers);
    }
    for (const z of D.zones) L.circleMarker([z.lat, z.lon], { radius: 9, color: "#fff", weight: 2, fillColor: sim.zoneDown.has(z.zs_id) ? "#d2462f" : "#3c6c78", fillOpacity: 1 })
      .on("click", () => select({ kind: "zone", id: z.zs_id })).bindTooltip(`${z.zs_name}: ${z.supply_circuits} supply circuit${z.supply_circuits > 1 ? "s" : ""}`).addTo(layers);
    for (const b of D.bsps) L.marker([b.lat, b.lon], { icon: L.divIcon({ className: "im-bsp", html: "", iconSize: [18, 18] }) })
      .on("click", () => select({ kind: "bsp", id: b.bsp_id })).bindTooltip(b.name).addTo(layers);
    side.innerHTML = "";
    side.append(h("label", { class: "small muted" }, "Fail an asset"), pick, outcomePanel(D, ctl.fail, sim, ctl.measure));
  };
  const select = (sel) => { ctl.fail = sel; pick.value = sel ? `${sel.kind}:${sel.id}` : ""; draw(); };
  pick.addEventListener("change", () => select(parseSel(pick.value)));
  draw();
  setTimeout(() => { m.invalidateSize(); m.fitBounds(L.latLngBounds(D.subs.map((x) => [x.lat, x.lon])).pad(0.06)); }, 50);
}

// ---------------------------------------------------------------- tab 2: layered network (3D)
const LEVEL = { suburb: 0, feeder: 15, zone: 30, ts: 38, bsp: 45 };
function layeredTab(D, root, ctl) {
  const wrap = h("div", { class: "im-split" }), stage = h("div", { class: "im-3d" }), side = h("div", { class: "im-side" });
  const pick = h("select", { class: "im-pick" }, ...pickerOptions(D));
  wrap.append(stage, side); root.append(wrap);
  const lat0 = D.zones.reduce((a, z) => a + z.lat, 0) / D.zones.length, lon0 = D.zones.reduce((a, z) => a + z.lon, 0) / D.zones.length;
  // Spread the floors evenly: positions keep their east-west and north-south order, but are spaced by rank rather than by distance, so
  // the dense metro area opens up and the empty country between regions closes in.
  const kx = 111.32 * Math.cos(lat0 * Math.PI / 180);
  const rawX = (lon) => (lon - lon0) * kx, rawZ = (lat) => (lat0 - lat) * 110.54;
  const sx = [...new Set(D.subs.map((s) => rawX(s.lon)))].sort((a, b) => a - b), sz = [...new Set(D.subs.map((s) => rawZ(s.lat)))].sort((a, b) => a - b);
  const even = (v, sorted, width) => {
    if (v <= sorted[0]) return -width / 2; if (v >= sorted.at(-1)) return width / 2;
    let lo = 0, hi = sorted.length - 1; while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (sorted[mid] <= v) lo = mid; else hi = mid; }
    const t = (lo + (v - sorted[lo]) / (sorted[hi] - sorted[lo] || 1)) / (sorted.length - 1);
    return (t - 0.5) * width;
  };
  const P = (lat, lon, y) => new THREE.Vector3(even(rawX(lon), sx, 130), y, even(rawZ(lat), sz, 90));
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, devicePixelRatio));
  stage.append(renderer.domElement);
  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0xb8c6c9, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.4); sun.position.set(-40, 80, 60); scene.add(sun);
  const camera = new THREE.PerspectiveCamera(38, 1, 0.5, 3000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.maxPolarAngle = Math.PI * 0.49;
  // floors: a faint sheet per level, named at the left
  const labels = [];
  const tag = (text, pos, cls) => { const el = h("div", { class: `im-tag ${cls || ""}` }, text); stage.append(el); labels.push({ el, pos }); return el; };
  const xs = D.subs.map((s) => P(s.lat, s.lon, 0).x), zsx = D.subs.map((s) => P(s.lat, s.lon, 0).z);
  const minX = Math.min(...xs) - 6, maxX = Math.max(...xs) + 6, minZ = Math.min(...zsx) - 6, maxZ = Math.max(...zsx) + 6;
  {   // frame the whole network: look from the south, a little west, high enough to see every floor
    const cx = (minX + maxX) / 2, cz = (minZ + maxZ) / 2, span = Math.max(maxX - minX, (maxZ - minZ) * 1.6);
    controls.target.set(cx, LEVEL.feeder, cz);
    camera.position.set(cx - span * 0.12, LEVEL.bsp + span * 0.42, cz + span * 0.78);
  }
  for (const [k, y] of Object.entries(LEVEL)) {
    const g = new THREE.PlaneGeometry(maxX - minX, maxZ - minZ); g.rotateX(-Math.PI / 2);
    const sheet = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: k === "suburb" ? 0xdfe8e6 : 0x9ccccc, transparent: true, opacity: k === "suburb" ? .55 : .07, depthWrite: false, side: THREE.DoubleSide }));
    sheet.position.set((minX + maxX) / 2, y - 0.02, (minZ + maxZ) / 2); scene.add(sheet);
    tag({ suburb: "Suburbs", feeder: "Feeders", zone: "Zone substations", ts: "Transmission", bsp: "Bulk supply" }[k], new THREE.Vector3(minX, y, maxZ), "lvl");
  }
  // things
  const pickables = [], objs = { suburb: new Map(), feeder: new Map(), zone: new Map(), ts: new Map(), bsp: new Map() };
  const mat = (c) => new THREE.MeshStandardMaterial({ color: c, roughness: .7, metalness: .05, transparent: true });
  const maxW = Math.max(...D.subs.map((s) => s.w));
  for (const s of D.subs) {
    const ht = 0.3 + 7 * Math.sqrt(s.w / maxW);
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(1.1, ht, 1.1), mat(0x5490a8));
    mesh.position.copy(P(s.lat, s.lon, ht / 2)); mesh.userData = { kind: "suburb", id: s.key, s };
    scene.add(mesh); pickables.push(mesh); objs.suburb.set(s.key, mesh);
  }
  for (const f of D.feeders) {
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(0.55, 16, 12), mat(0x3c7c88));
    mesh.position.copy(P(f.lat, f.lon, LEVEL.feeder)); mesh.userData = { kind: "feeder", id: f.feeder_id };
    scene.add(mesh); pickables.push(mesh); objs.feeder.set(f.feeder_id, mesh);
  }
  for (const z of D.zones) {
    const mesh = new THREE.Mesh(new THREE.CylinderGeometry(1.4, 1.4, 1.2, 24), mat(0x24575f));
    mesh.position.copy(P(z.lat, z.lon, LEVEL.zone)); mesh.userData = { kind: "zone", id: z.zs_id };
    scene.add(mesh); pickables.push(mesh); objs.zone.set(z.zs_id, mesh);
    tag(z.zs_name.replace(" zone substation", ""), mesh.position.clone().add(new THREE.Vector3(0, 1.6, 0)), "zone");
  }
  // sites that sit close together on the map (Nepean and Bringelly are a few kilometres apart) are pushed apart on their floor, so the
  // boxes and names never overlap; the threads follow the moved positions
  const tsAt = spreadApart(D.transmission.map((t) => P(t.lat, t.lon, LEVEL.ts)), 15);
  D.transmission.forEach((t, i) => {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(2.4, 1.1, 2.4), mat(0x2e6470));
    mesh.position.copy(tsAt[i]); mesh.userData = { kind: "ts", id: t.ts_id };
    scene.add(mesh); pickables.push(mesh); objs.ts.set(t.ts_id, mesh);
    tag(t.name.replace(" transmission substation", ""), mesh.position.clone().add(new THREE.Vector3(0, 1.5, 0)), "ts");
  });
  for (const b of D.bsps) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(3, 1.6, 3), mat(0x1f3b42));
    mesh.position.copy(P(b.lat, b.lon, LEVEL.bsp)); mesh.userData = { kind: "bsp", id: b.bsp_id };
    scene.add(mesh); pickables.push(mesh); objs.bsp.set(b.bsp_id, mesh);
    tag(b.name.replace(" bulk supply point", ""), mesh.position.clone().add(new THREE.Vector3(0, 2, 0)), "bsp");
  }
  // threads: suburb to feeder, feeder to zone, zone to transmission substation (one per circuit), transmission substation to bulk supply (one
  // per 132 kV circuit), and the backup ties between feeders
  const threads = [];
  const thread = (a, b, info, dashed = false) => {
    const g = new THREE.BufferGeometry().setFromPoints([a, b]);
    const m = dashed ? new THREE.LineDashedMaterial({ color: 0xc98a1e, dashSize: .8, gapSize: .6, transparent: true }) : new THREE.LineBasicMaterial({ color: 0x5490a8, transparent: true });
    const line = new THREE.Line(g, m); if (dashed) line.computeLineDistances();
    line.userData = info; scene.add(line); threads.push(line); return line;
  };
  for (const s of D.subs) for (const r of s.rows) {
    const f = D.feederMap.get(r.feeder_id);
    thread(objs.suburb.get(s.key).position.clone().setY(objs.suburb.get(s.key).geometry.parameters.height), objs.feeder.get(r.feeder_id).position, { t: "sf", suburb: s.key, feeder: f.feeder_id, w: r.customers });
  }
  for (const f of D.feeders) thread(objs.feeder.get(f.feeder_id).position, objs.zone.get(f.zs_id).position, { t: "fz", feeder: f.feeder_id, zone: f.zs_id });
  for (const l of D.lines) {
    const a = objs.zone.get(l.zs_id).position.clone(), b = objs.ts.get(l.ts_id).position.clone();
    if (D.zoneMap.get(l.zs_id).supply_circuits > 1) { const off = new THREE.Vector3(l.circuit === 1 ? .5 : -.5, 0, 0); a.add(off); b.add(off); }
    thread(a, b, { t: "zt", line: l.line_id, zone: l.zs_id, ts: l.ts_id });
  }
  for (const l of D.tlines) {
    const a = objs.ts.get(l.ts_id).position.clone(), b = objs.bsp.get(l.bsp_id).position.clone();
    if (D.tlines.filter((x) => x.ts_id === l.ts_id).length > 1) { const off = new THREE.Vector3(l.circuit === 1 ? .5 : -.5, 0, 0); a.add(off); b.add(off); }
    thread(a, b, { t: "bt", line: l.line_id, ts: l.ts_id, bsp: l.bsp_id });
  }
  const tieSeen = new Set();
  for (const f of D.feeders) for (const t of f.ties || []) {
    const k = [f.feeder_id, t.to].sort().join("|"); if (tieSeen.has(k) || !objs.feeder.get(t.to)) continue; tieSeen.add(k);
    thread(objs.feeder.get(f.feeder_id).position, objs.feeder.get(t.to).position, { t: "tie", a: f.feeder_id, b: t.to, other: t.other_zone }, true);
  }
  // colour everything for the current state: a failure if one is chosen, else each suburb's backup (blue: all can be back-fed, amber:
  // part, orange: none); a focused suburb dims everything it does not depend on
  let focus = null;
  const paint = () => {
    const sim = simulate(D, ctl.fail);
    const chain = focus ? (() => {
      const s = D.subs.find((x) => x.key === focus), fs = new Set(s.feeders), zs = new Set([...fs].map((f) => D.feederMap.get(f).zs_id));
      const ls = new Set(D.lines.filter((l) => zs.has(l.zs_id)).map((l) => l.line_id)), bs = new Set(D.lines.filter((l) => zs.has(l.zs_id)).map((l) => l.bsp_id));
      const ts = new Set(D.lines.filter((l) => zs.has(l.zs_id)).map((l) => l.ts_id));
      const ties = new Set(); for (const f of fs) for (const t of D.feederMap.get(f).ties || []) ties.add(t.to);
      return { s: new Set([focus]), fs, zs, ls, bs, ts, ties };
    })() : null;
    const dim = (o, keep) => { o.material.opacity = !chain || keep ? 1 : .12; };
    for (const [k, mesh] of objs.suburb) {
      const st = sim.suburb[k].worst, s = mesh.userData.s;
      mesh.material.color.set(sim.any ? STATE[st].c : { all: "#5490a8", part: "#d9a441", none: "#c96f2d" }[s.backup]); dim(mesh, chain?.s.has(k));
    }
    for (const [k, mesh] of objs.feeder) {
      const f = D.feederMap.get(k);
      mesh.material.color.set(sim.any ? STATE[sim.feeder[k]].c : f.tripped ? STATUS_COLOUR.red : f.spur_outage ? SPUR : (f.ties || []).length ? "#3c7c88" : "#8f6a1c");
      dim(mesh, chain?.fs.has(k) || chain?.ties.has(k));
    }
    for (const [k, mesh] of objs.zone) { mesh.material.color.set(sim.zoneDown.has(k) ? "#d2462f" : D.zoneMap.get(k).single_fed ? "#b4552d" : "#24575f"); dim(mesh, chain?.zs.has(k)); }
    for (const [k, mesh] of objs.ts) { mesh.material.color.set(sim.tsDown.has(k) ? "#d2462f" : "#2e6470"); dim(mesh, chain?.ts.has(k)); }
    for (const [k, mesh] of objs.bsp) { mesh.material.color.set(ctl.fail?.kind === "bsp" && ctl.fail.id === k ? "#d2462f" : "#1f3b42"); dim(mesh, chain?.bs.has(k)); }
    for (const l of threads) {
      const u = l.userData; let c = 0x5490a8, keep = false, op = .35;
      if (u.t === "sf") { c = sim.any ? new THREE.Color(STATE[sim.feeder[u.feeder]].c).getHex() : 0x83b8bf; keep = chain?.s.has(u.suburb); op = .28; }
      if (u.t === "fz") { c = sim.down.has(u.feeder) ? 0xd2462f : 0x5490a8; keep = chain?.fs.has(u.feeder); op = .5; }
      if (u.t === "zt") { c = sim.linesDown.has(u.line) ? 0xd2462f : D.zoneMap.get(u.zone).single_fed ? 0xb4552d : 0x24575f; keep = chain?.ls.has(u.line); op = .85; }
      if (u.t === "bt") { c = sim.tlinesDown.has(u.line) || sim.tsDown.has(u.ts) ? 0xd2462f : 0x1f3b42; keep = chain?.ts.has(u.ts); op = .85; }
      if (u.t === "tie") { c = 0xc98a1e; keep = chain && (chain.fs.has(u.a) || chain.fs.has(u.b)); op = u.other ? .95 : .6; }
      l.material.color.set(c); l.material.opacity = !chain ? op : keep ? 1 : .05;
    }
    side.innerHTML = "";
    side.append(h("label", { class: "small muted" }, "Fail an asset (or click one)"), pick);
    if (focus && !ctl.fail) side.append(dependencyPanel(D, focus));
    else side.append(outcomePanel(D, ctl.fail, sim, ctl.measure));
    stage.querySelector(".im-key")?.remove();
    stage.append(legend(ctl.fail ? Object.values(STATE).map((x) => [x.c, x.l])
      : [["#5490a8", "All customers can be back-fed"], ["#d9a441", "Some can be back-fed"], ["#c96f2d", "None can be back-fed"], ["#b4552d", "Single supply circuit"], ["#c98a1e", "Backup tie (dashed)"]]));
  };
  pick.value = ctl.fail ? `${ctl.fail.kind}:${ctl.fail.id}` : "";
  pick.addEventListener("change", () => { ctl.fail = parseSel(pick.value); focus = null; paint(); });
  // picking
  const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
  let downAt = null;
  renderer.domElement.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener("pointerup", (e) => {
    if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5) return;   // a drag, not a click
    const r = renderer.domElement.getBoundingClientRect();
    mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(mouse, camera);
    const hit = ray.intersectObjects(pickables)[0]?.object.userData;
    if (!hit) { focus = null; ctl.fail = null; pick.value = ""; }
    else if (hit.kind === "suburb") { focus = hit.id; ctl.fail = null; pick.value = ""; }
    else { focus = null; ctl.fail = { kind: hit.kind, id: hit.id }; pick.value = `${hit.kind}:${hit.id}`; }
    paint();
  });
  const tip = h("div", { class: "im-tip" }); stage.append(tip);
  renderer.domElement.addEventListener("pointermove", (e) => {
    const r = renderer.domElement.getBoundingClientRect();
    mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(mouse, camera);
    const u = ray.intersectObjects(pickables)[0]?.object.userData;
    if (!u) { tip.style.display = "none"; return; }
    tip.textContent = u.kind === "suburb" ? `${u.s.name} ${u.s.postcode}: ${cnt(u.s.w, "customer", "customers")} on ${cnt(u.s.feeders.length, "feeder", "feeders")}`
      : u.kind === "feeder" ? `${u.id}, ${D.feederMap.get(u.id).feeder_name}${(D.feederMap.get(u.id).ties || []).length ? "" : " (no backup tie)"}`
        : u.kind === "zone" ? `${D.zoneMap.get(u.id).zs_name}: ${D.zoneMap.get(u.id).supply_circuits} supply circuit${D.zoneMap.get(u.id).supply_circuits > 1 ? "s" : ""}`
          : u.kind === "ts" ? D.tsMap.get(u.id).name
            : D.bsps.find((b) => b.bsp_id === u.id).name;
    tip.style.display = "block"; tip.style.left = `${e.clientX - r.left + 12}px`; tip.style.top = `${e.clientY - r.top + 12}px`;
  });
  const size = () => { const w = stage.clientWidth, hh = stage.clientHeight; if (!w || !hh) return; renderer.setSize(w, hh); camera.aspect = w / hh; camera.updateProjectionMatrix(); };
  const ro = new ResizeObserver(size); ro.observe(stage); size();
  const v = new THREE.Vector3();
  const loop = () => {
    if (!stage.isConnected) { ro.disconnect(); renderer.dispose(); return; }
    controls.update(); renderer.render(scene, camera);
    const w = stage.clientWidth, hh = stage.clientHeight;
    const placed = [];
    for (const { el, pos } of labels) {
      v.copy(pos).project(camera);
      const x = (v.x + 1) / 2 * w, y = (1 - v.y) / 2 * hh, lw = el.offsetWidth || 60, lh = 14;
      const box = [x - lw / 2, y - lh, x + lw / 2, y];
      const on = v.z < 1 && Math.abs(v.x) < 1.05 && Math.abs(v.y) < 1.05 &&
        !placed.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1]);   // never let two names overlap
      el.style.display = on ? "block" : "none";
      if (on) { placed.push(box); el.style.transform = `translate(${x}px, ${y}px) translate(-50%, -100%)`; }
    }
    requestAnimationFrame(loop);
  };
  const rank = (el) => el.classList.contains("lvl") ? 0 : el.classList.contains("bsp") ? 1 : 2;
  labels.sort((a, b) => rank(a.el) - rank(b.el));
  paint(); loop();
}

function dependencyPanel(D, key) {
  const s = D.subs.find((x) => x.key === key);
  const box = h("div", { class: "im-out" }, h("div", { class: "im-sel" }, `${s.name} ${s.postcode}`),
    h("p", { class: "muted small" }, `${cnt(s.w, "customer", "customers")}. ${{ all: "Every feeder here has a backup tie.", part: "Some of its customers are on a feeder with no backup tie.", none: "None of its feeders has a backup tie: losing one leaves its customers off until repaired." }[s.backup]}`));
  const t = h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Feeder", "Customers", "Zone substation", "Supply circuits", "Backup tie"].map((x, i) => h("th", { class: i === 1 || i === 3 ? "r" : "" }, x)))),
    h("tbody", {}, s.rows.sort((a, b) => b.customers - a.customers).map((r) => {
      const f = D.feederMap.get(r.feeder_id), z = D.zoneMap.get(f.zs_id);
      return h("tr", {}, h("td", {}, r.feeder_id), h("td", { class: "r num" }, n0(r.customers)), h("td", {}, z.zs_name.replace(" zone substation", "")),
        h("td", { class: "r num" }, z.supply_circuits), h("td", {}, (f.ties || []).map((t) => `${t.to}${t.other_zone ? " (other zone)" : ""}`).join(", ") || "None"));
    })));
  box.append(t);
  return box;
}

// Push points on one floor apart until every pair is at least min apart (a few relaxation passes, each pair moved equally along the line
// between them); points already far enough apart stay where the map puts them.
function spreadApart(points, min) {
  const p = points.map((v) => v.clone());
  for (let pass = 0; pass < 20; pass++) {
    let moved = false;
    for (let i = 0; i < p.length; i++) for (let j = i + 1; j < p.length; j++) {
      const dx = p[j].x - p[i].x, dz = p[j].z - p[i].z, d = Math.hypot(dx, dz);
      if (d >= min) continue;
      const ux = d ? dx / d : 1, uz = d ? dz / d : 0, push = (min - d) / 2;
      p[i].x -= ux * push; p[i].z -= uz * push; p[j].x += ux * push; p[j].z += uz * push; moved = true;
    }
    if (!moved) break;
  }
  return p;
}

// ---------------------------------------------------------------- the dialog
export async function impactAnalysis() {
  const bg = h("div", { class: "modal-bg", onclick: (e) => { if (e.target === bg) bg.remove(); } });
  const dlg = h("div", { class: "modal im" });
  bg.append(dlg); document.body.append(bg);
  dlg.append(h("div", { class: "im-head" }, h("h2", {}, "Impact analysis"), h("button", { class: "btn small", onclick: () => bg.remove() }, "Close")),
    h("p", { class: "muted small" }, "Loading the network's dependencies."));
  const [raw, net] = await Promise.all([api("/api/impact"), api("/api/map/network")]);
  const D = shape(raw);
  const ctl = { tab: "sankey", sub: "layers", view: "assets", region: D.regions[0].code, measure: "customers", fail: (() => { const z = D.zones.find((x) => x.single_fed && x.area === "CST") || D.zones.find((x) => x.single_fed); return z ? { kind: "line", id: z.line_ids[0] } : null; })() };
  const TABS = [["sankey", "Flow view"], ["spread", "Network spread"], ["sunburst", "Share at every level"]];
  const SUBS = [["layers", "Network layers"], ["whatif", "Network map"]];
  const body = h("div", { class: "im-body" });
  const render = () => {
    dlg.innerHTML = "";
    const tabs = h("div", { class: "im-tabs" }, TABS.map(([k, l]) => h("button", { class: k === ctl.tab ? "on" : "", onclick: () => { ctl.tab = k; render(); } }, l)));
    const seg = (items, cur, set) => h("div", { class: "im-seg" }, items.map(([k, l]) => h("button", { class: k === cur ? "on" : "", onclick: () => { set(k); render(); } }, l)));
    const controls = h("div", { class: "im-ctl" },
      ctl.tab === "spread" ? seg(SUBS, ctl.sub, (k) => { ctl.sub = k; }) : null,
      ctl.tab === "sunburst" ? seg(SUN_VIEWS, ctl.view, (k) => { ctl.view = k; }) : null,
      ctl.tab === "sankey" ? seg(D.regions.map((r) => [r.code, r.label]), ctl.region, (k) => { ctl.region = k; }) : null,
      ctl.tab === "sankey" || ctl.tab === "sunburst" ? seg(MEASURES, ctl.measure, (k) => { ctl.measure = k; }) : null);
    body.innerHTML = "";
    dlg.append(h("div", { class: "im-head" }, h("h2", {}, "Impact analysis"), h("button", { class: "btn small", onclick: () => bg.remove() }, "Close")), tabs, controls, body);
    const view = ctl.tab === "spread" ? ctl.sub : ctl.tab;
    ({ sankey: () => sankeyTab(D, body, ctl), sunburst: () => sunburstTab(D, body, ctl), whatif: () => mapTab(D, body, ctl, net), layers: () => layeredTab(D, body, ctl) })[view]();
  };
  render();
}
