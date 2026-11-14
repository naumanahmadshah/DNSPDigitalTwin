// Network, Network Map, Operations, Plan and Invest, Customers, the asset drawer, 3D entry.
import { api, post, h, $, n0, n1, n2, pct, money, ago, hhmm, day, title, st, tile, card, table, chart, chartBox, axis, mapIn, STATUS_COLOUR, drawer,
  toast, modal, loading } from "./ui.js";
import { go, every } from "./app.js";
import * as RM from "./rm.js";
import { openLidar, warmLidar } from "./lidar.js";

const view = $("#view");
const cnt = (n, one, many = `${one}s`) => `${n0(n)} ${+n === 1 ? one : many}`;
let ZONE = null, SITES_LIST = null;   // the Network screen's zone substation filter, and the zone list behind it
const STOPS = [["bulk_supply", "Bulk supply", "stop_bulk_supply"], ["transmission_substations", "Transmission substations", "stop_transmission_substation"],
  ["zone_substations", "Zone substations", "stop_zone_substation"], ["hv_feeders", "HV feeders", "stop_hv_feeders"],
  ["distribution_lv", "Distribution and LV", "stop_distribution_lv"], ["customers_der", "Customers and DER", "stop_customers_der"]];

// ---------------------------------------------------------------- Network
export async function network(stop) {
  let d = await api(`/api/network/${stop}`);
  view.innerHTML = "";
  const zones = (SITES_LIST ||= await api("/api/scene-sites")).filter((z) => z.kind !== "transmission").map((z) => [z.zs_id, z.name.replace(" zone substation", "")]);
  const zoneOf = (r) => r.zs_id || `ZS-${(r.feeder_id || r.ds_id?.slice(3) || "").slice(0, 3)}`;
  // a transmission substation lists the zone substations it feeds; every other row belongs to one zone substation
  const inZone = (rows) => ZONE ? rows.filter((r) => (Array.isArray(r.zones) ? r.zones.includes(ZONE) : zoneOf(r) === ZONE)) : rows;
  const zsel = h("select", { class: "zone-pick", onchange: (e) => { ZONE = e.target.value || null; network(stop); } },
    h("option", { value: "" }, "All zone substations"), zones.map(([id, n]) => h("option", { value: id, selected: id === ZONE ? "" : null }, n)));
  view.append(h("div", { class: "screen-h" }, h("h1", {}, "Network"),
    h("div", { class: "right" }, stop !== "bulk_supply" ? zsel : null, h("a", { class: "btn", href: "#/map" }, "Network map"))),
    h("div", { class: "tabs" }, STOPS.map(([k, l, i]) => h("a", { href: `#/network/${k}`, class: k === stop ? "on" : "" }, h("img", { src: `/static/icons/${i}.png?v=teal` }), l))));
  const body = h("div", { class: "tabbody" });
  view.append(body);
  if (stop === "customers_der") d.dq = inZone(d.rows).reduce((a, r) => a + r.mismatched, 0);
  const why = whyRed(stop, d);
  if (why) body.append(why);
  if (stop === "bulk_supply") {
    body.append(card("Bulk supply points", "Live draw from the zone substations below, from SCADA through the live engine.",
      table([{ l: "Bulk supply point", k: "name" }, { l: "Transmission substations", k: "transmission_substations", r: 1 }, { l: "Zone substations", k: "zones", r: 1 },
        { l: "Load now MW", k: "mw_now", r: 1 }, { l: "Capacity MVA", k: "capacity_mva", r: 1 },
        { l: "Use", f: (r) => pct(r.mw_now / r.capacity_mva * 100), r: 1 }], d.rows, null, { noScroll: 1 })));
  }
  if (stop === "transmission_substations") {
    d = { ...d, rows: inZone(d.rows) };
    const red = d.rows.filter((r) => r.status === "red").length, amber = d.rows.filter((r) => r.status === "amber").length;
    const single = d.rows.reduce((a, r) => a + (r.single_transmission_zones || []).length, 0);
    body.append(h("div", { class: "glance" }, tile("Transmission substations", d.rows.length, "Steps 132 kV down to 33 or 66 kV."),
      tile("Need action", red, "Sites whose 30-day peak is over firm capacity.", red ? "red" : "green"), tile("Watch", amber, "Sites whose 30-day peak is over 85 percent of firm capacity.", amber ? "amber" : "green"),
      tile("Zones on one site", single, "Zones that lose supply if their site is lost.", single ? "amber" : "green"),
      tile("Highest 30-day use", pct(Math.max(0, ...d.rows.map((r) => r.peak_utilisation_30d || 0)) * 100), "Share of firm (N-1) capacity.")));
    body.append(card("Transmission substations", "30-day peak load against firm (N-1) capacity. Choose a site to see its transformers, circuits and zones.",
      table([{ l: "Site", k: "name" }, { l: "Status", f: (r) => st(r.status) }, { l: "Bulk supply point", k: "bulk_supply_name" },
        { l: "Voltage kV", f: (r) => `${r.kv} to ${r.secondary_kv}`, r: 1 }, { l: "Transformers", f: (r) => `${r.transformers} × ${r.transformer_mva} MVA`, r: 1 },
        { l: "Firm MVA", k: "firm_mva", r: 1 }, { l: "Peak MW 30d", f: (r) => n1(r.peak_mw_30d), r: 1 },
        { l: "Use of firm", f: (r) => pct((r.peak_utilisation_30d || 0) * 100), r: 1 }, { l: "Zone substations", k: "zone_count", r: 1 },
        { l: "Customers", k: "customers", r: 1 }, { l: "Single-site zones", f: (r) => (r.single_transmission_zones || []).length, r: 1 }],
        d.rows, (r) => transmissionDetail(r), { max: 620 })));
  }
  if (stop === "zone_substations") {
    d = { ...d, rows: inZone(d.rows) };
    const red = d.rows.filter((r) => r.status === "red").length;
    body.append(h("div", { class: "glance" }, tile("Zone transformers", d.rows.length, "Two transformers at each zone substation."), tile("Need action", red, "Transformers flagged for dissolved gas or loading.", red ? "red" : "green"),
      tile("Highest top oil now", n1(Math.max(...d.rows.map((r) => r.top_oil_live || 0))) + " °C", "Live SCADA readings."), tile("Highest 30-day use", pct(Math.max(...d.rows.map((r) => r.peak_utilisation_30d || 0)) * 100), "Share of nameplate rating.")));
    body.append(card("Zone substation transformers", "Historian data (Delta Sharing) plus live SCADA. Choose a row for its detail.",
      table([{ l: "Transformer", k: "transformer_id" }, { l: "Zone substation", f: (r) => (zones.find(([id]) => id === r.zs_id) || [0, r.zs_id])[1] },
        { l: "Status", f: (r) => st(r.status) }, { l: "Rating MVA", k: "rating_mva", r: 1 }, { l: "Built", k: "year_built", r: 1 },
        { l: "MW live", f: (r) => n1(r.mw_live), r: 1 }, { l: "Top oil", f: (r) => n1(r.top_oil_live), r: 1 }, { l: "30d use", f: (r) => pct(r.peak_utilisation_30d * 100), r: 1 },
        { l: "H2 ppm", f: (r) => n0(r.h2_now), r: 1 }, { l: "H2 rise 90d", f: (r) => n0(r.h2_rise_90d), r: 1 }], d.rows, (r) => txDetail(r), { max: 620 })));
  }
  if (stop === "hv_feeders") {
    d = { ...d, rows: inZone(d.rows) };
    const counts = { red: 0, amber: 0, green: 0 };
    d.rows.forEach((r) => counts[r.tripped ? "red" : r.status]++);
    body.append(h("div", { class: "glance" }, tile("Feeders", d.rows.length), tile("Need action", counts.red, "Feeders tripped now, or needing vegetation or reliability action.", "red"), tile("Watch", counts.amber, "Feeders to watch.", "amber"),
      tile("Tripped now", d.rows.filter((r) => r.tripped).length, "Live readings.", d.rows.some((r) => r.tripped) ? "red" : "green"),
      tile("Spur outages", d.rows.filter((r) => r.spur_outage).length, "A fuse has operated. The feeder stays in service.", d.rows.some((r) => r.spur_outage) ? "amber" : "green")));
    body.append(card("HV feeders", "12-month reliability, vegetation, defects, capacity and live load. Choose a row for its detail.",
      table([{ l: "Feeder", k: "feeder_id" }, { l: "Name", k: "feeder_name" }, { l: "Area", k: "area_label" }, { l: "Status", f: (r) => feederState(r) },
        { l: "Customers", k: "customers", r: 1 }, { l: "Life support", k: "life_support", r: 1 }, { l: "Live A", f: (r) => n0(r.amps_live), r: 1 }, { l: "30-day peak A", f: (r) => n0(r.amps_peak_30d), r: 1 },
        { l: "Rating A", k: "rating_amps", r: 1 }, { l: "Spare A", f: (r) => n0(r.spare_amps), r: 1 }, { l: "Overhead km", f: (r) => n1(r.overhead_km), r: 1 },
        { l: "Bushfire-prone", f: (r) => pct(r.bushfire_share * 100), r: 1 }, { l: "Outages 12m", k: "sustained_outages_12m", r: 1 }, { l: "Customer minutes 12m", f: (r) => n0(r.cmos_12m), r: 1 },
        { l: "Veg cat A", k: "veg_cat_a", r: 1 }, { l: "Defects", k: "open_defects", r: 1 }], d.rows, (r) => feederDetail(r), { max: 640 })));
  }
  if (stop === "distribution_lv") {
    const sm = Object.fromEntries((d.summary || []).map((x) => [x.status, x.n]));
    body.append(h("div", { class: "glance" }, tile("Distribution substations", n0((sm.red || 0) + (sm.amber || 0) + (sm.green || 0))), tile("Need action", n0(sm.red), "Substations overloaded at peak or with reverse flow.", "red"),
      tile("Watch", n0(sm.amber), "Substations to watch.", "amber")),
      card("Distribution substations to act on", "Estimated peak use (customers and EV chargers) and midday reverse flow (rooftop solar).",
        table([{ l: "Substation", k: "ds_id" }, { l: "Status", f: (r) => st(r.status) }, { l: "Type", k: "ds_type" }, { l: "kVA", k: "kva", r: 1 }, { l: "Customers", k: "customers", r: 1 },
          { l: "Solar kW", k: "solar_kw", r: 1 }, { l: "Peak use", f: (r) => pct(r.est_peak_utilisation * 100), r: 1 }, { l: "Reverse flow", f: (r) => pct(r.est_midday_reverse_flow * 100), r: 1 },
          { l: "", f: (r) => h("a", { href: `#/scene/feeder/${r.feeder_id}` }, "Site view") }], inZone(d.rows), (r) => openAsset(r.ds_id), { max: 560 })));
  }
  if (stop === "customers_der") {
    d = { ...d, rows: inZone(d.rows) };
    const c = chartBox("tall"), zname = (id) => (zones.find(([z]) => z === id) || [0, id])[1];
    body.append(h("div", { class: "glance" }, tile("Customers (NMIs)", n0(d.rows.reduce((a, r) => a + r.customers, 0))), tile("Life support", n0(d.rows.reduce((a, r) => a + r.life_support, 0))),
      tile("Rooftop solar", n1(d.rows.reduce((a, r) => a + r.solar_mw, 0)) + " MW"), tile("Records that disagree", n0(d.dq), "Meters on the wrong transformer.", d.dq ? "amber" : "green")),
      h("div", { class: "grid2" }, card("By zone substation", "Choose a row for its detail.", table([{ l: "Zone", f: (r) => zname(r.zs_id) }, { l: "Customers", k: "customers", r: 1 }, { l: "Life support", k: "life_support", r: 1 },
        { l: "Critical sites", k: "critical", r: 1 }, { l: "Solar MW", k: "solar_mw", r: 1 }, { l: "EV chargers", k: "ev_chargers", r: 1 }], d.rows, (r) => zoneDetail(r, zname(r.zs_id)), { max: 520 })), card("Rooftop solar and EV by zone", "", c)));
    chart(c, { grid: { left: 48, right: 52, top: 36, bottom: 30 }, xAxis: axis({ type: "category", data: d.rows.map((r) => r.zs_id.slice(3)) }), yAxis: [axis({ type: "value", name: "MW" }), axis({ type: "value", name: "EVs", splitLine: { show: false } })],
      series: [{ type: "bar", name: "Solar MW", data: d.rows.map((r) => r.solar_mw), color: "#c98a1e" }, { type: "line", name: "EV chargers", yAxisIndex: 1, data: d.rows.map((r) => r.ev_chargers), color: "#0c5a66" }] });
  }
}

function whyRed(stop, d) {
  const rows = d.rows || [];
  let msg = null, cls = "";
  if (stop === "transmission_substations") {
    const r = rows.filter((x) => x.status === "red"), a = rows.filter((x) => x.status === "amber");
    if (r.length) msg = `Transmission substations are red: ${r.map((x) => `${x.name} peaks at ${pct(x.peak_utilisation_30d * 100)} of its firm capacity`).join("; ")}. Highlighted below.`;
    else if (a.length) { msg = `Transmission substations are amber: ${a.map((x) => `${x.name} peaks at ${pct(x.peak_utilisation_30d * 100)} of its firm capacity`).join("; ")}.`; cls = "amber"; }
  }
  if (stop === "zone_substations") {
    const r = rows.filter((x) => x.status === "red");
    if (r.length) msg = `Zone substations are red: ${r.map((x) => `${x.name} dissolved hydrogen up ${n0(x.h2_rise_90d)} ppm in 90 days (now ${n0(x.h2_now)} ppm)`).join("; ")}. Highlighted below.`;
  }
  if (stop === "hv_feeders") {
    const t = rows.filter((x) => x.tripped), sp = rows.filter((x) => x.spur_outage), r = rows.filter((x) => x.status === "red");
    const spur = sp.length ? `outages on fused spurs of ${sp.map((x) => x.feeder_id).join(", ")}` : "";
    if (t.length || sp.length) msg = `HV feeders are red: ${[t.length ? `${t.map((x) => x.feeder_id).join(", ")} tripped now` : "", spur].filter(Boolean).join("; ")}; ${cnt(r.length, "more feeder needs", "more feeders need")} action (vegetation inside clearance or poor reliability).`;
    else if (r.length) msg = `HV feeders are red: ${cnt(r.length, "feeder needs", "feeders need")} action, led by ${r.slice(0, 3).map((x) => `${x.feeder_id} (${x.veg_cat_a} spans inside vegetation clearance, ${x.sustained_outages_12m} outages in 12 months)`).join(", ")}.`;
  }
  if (stop === "distribution_lv") {
    const sm = Object.fromEntries((d.summary || []).map((x) => [x.status, x.n]));
    if (sm.red) msg = `Distribution and LV are red: ${cnt(sm.red, "transformer is", "transformers are")} overloaded at peak or back-feeding rooftop solar at midday. Highlighted below.`;
  }
  if (stop === "customers_der" && d.dq) { msg = `Customers and DER are amber: ${cnt(d.dq, "meter's voltage follows", "meters' voltage follow")} a different transformer from the one in the records.`; cls = "amber"; }
  return msg ? h("div", { class: `banner ${cls}` }, h("span", { class: "b" }), msg) : null;
}

// a transmission substation: its transformers, the zone substations it feeds and the 132 kV supply in
async function transmissionDetail(r) {
  const zoneName = (id) => (SITES_LIST || []).find((z) => z.zs_id === id)?.name || id;
  drawer(h("h2", {}, r.name), h("p", { class: "muted" }, `${r.kv} kV supply from ${r.bulk_supply_name}, stepped down to ${r.secondary_kv} kV on ${r.transformers} × ${r.transformer_mva} MVA transformers`),
    st(r.status), " ",
    h("a", { class: "btn small", href: `#/scene/transmission/${r.ts_id}`, onclick: () => $("#drawer").classList.remove("open") }, "Site view"), " ",
    h("button", { class: "btn small", onclick: () => openAsset(r.ts_id) }, "Asset record"),
    h("div", { class: "kv", style: "margin:12px 0" }, ...[["Firm capacity (N-1)", `${n0(r.firm_mva)} MVA`], ["30-day peak", `${n1(r.peak_mw_30d)} MW, ${pct(r.peak_utilisation_30d * 100)} of firm`],
      ["Zone substations fed", n0(r.zone_count)], ["Customers in those zones", n0(r.customers)],
      ["Zones with no other site", (r.single_transmission_zones || []).map(zoneName).join(", ") || "None"]].flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)])),
    h("h4", {}, "Transformers"), table([{ l: "Transformer", k: "name" }, { l: "Status", f: (x) => st(x.status) }, { l: "Rating MVA", k: "rating_mva", r: 1 },
      { l: "Peak MW 30d", f: (x) => n1(x.peak_mw_30d), r: 1 }, { l: "Use", f: (x) => pct(x.peak_utilisation_30d * 100), r: 1 }], r.transformer_rows || [], (x) => openAsset(x.transformer_id), { noScroll: 1 }),
    h("h4", {}, "Zone substations fed"), table([{ l: "Zone substation", f: (x) => zoneName(x) }], r.zones || [], null, { noScroll: 1 }),
    h("p", { class: "small muted" }, "Load is taken from the historian's 15-minute archive for the transformers, measured against the firm (N-1) capacity of the site."));
}

async function txDetail(r) {
  const c = chartBox("tall");
  drawer(h("h2", {}, r.name), h("p", { class: "muted" }, `${r.rating_mva} MVA, built ${r.year_built || ""}`), st(r.status), " ",
    h("a", { class: "btn small", href: `#/scene/zone/${r.zs_id}`, onclick: () => $("#drawer").classList.remove("open") }, "Site view"),
    h("div", { class: "kv", style: "margin:12px 0" }, ...[["MW live", n1(r.mw_live)], ["Top oil now", `${n1(r.top_oil_live)} °C`], ["30-day peak use", pct(r.peak_utilisation_30d * 100)],
      ["Hydrogen", `${n0(r.h2_now)} ppm (90 days ago ${n0(r.h2_90d_ago)})`]].flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)])),
    h("h4", {}, "Load and top oil, last 30 days"), c, h("p", { class: "small muted" }, "Live values update every 4 seconds from SCADA. History comes from the historian archive."));
  const s = await api(`/api/series/transformer/${r.transformer_id}`);
  const mw = s.history.filter((x) => x.attribute === "MW").map((x) => [x.t, x.v]), oil = s.history.filter((x) => x.attribute === "TOP_OIL_TEMP").map((x) => [x.t, x.v]);
  const lmw = s.live.filter((x) => x.attribute === "MW").map((x) => [x.t, x.v]);
  chart(c, { grid: { left: 46, right: 50, top: 46, bottom: 30 }, legend: { textStyle: { color: "#5f7185" }, top: 0 }, xAxis: axis({ type: "time" }),
    yAxis: [axis({ type: "value", name: "MW" }), axis({ type: "value", name: "°C", splitLine: { show: false } })],
    series: [{ type: "line", name: "Load MW (history)", data: mw, showSymbol: false, color: "#24575f", lineStyle: { width: 1.8 } },
      { type: "line", name: "Top oil (°C)", yAxisIndex: 1, data: oil, showSymbol: false, color: "#e2683c", lineStyle: { width: 1.8 } },
      { type: "line", name: "Load MW (live)", data: lmw, showSymbol: false, color: "#8a63b8", lineStyle: { width: 2.2, type: "dashed" } }] });
}

// a feeder's state: tripped (its breaker or a recloser opened), an outage on a fused spur (the feeder stays in service), or its health
const feederState = (r) => r.tripped ? st("red", "Tripped") : r.spur_outage ? st("amber", "Outage on a spur") : st(r.status);

async function feederDetail(r) {
  const c = chartBox("tall"), out = h("div");
  drawer(h("h2", {}, r.feeder_name), h("p", { class: "muted" }, `${r.feeder_id}, ${r.area_label}, ${cnt(r.customers, "customer", "customers")}, ${cnt(r.life_support, "life support customer", "life support customers")}`),
    feederState(r), " ", h("a", { class: "btn small", href: `#/scene/feeder/${r.feeder_id}`, onclick: () => $("#drawer").classList.remove("open") }, "Site view"), " ",
    h("button", { class: "btn small", onclick: () => openAsset(r.feeder_id + "-CB01") }, "Feeder breaker"), " ",
    h("button", { class: "btn small", onclick: () => RM.raiseJob({ job_type: "ASSET_INSPECTION", title: `Line patrol ${r.feeder_id}`, feeder_id: r.feeder_id,
      customers: r.customers, where: `${r.feeder_name}, ${n0(r.customers)} customers` }) }, "Raise a job"),
    h("div", { class: "kv", style: "margin:12px 0" }, ...[["Overhead / underground", `${n1(r.overhead_km)} / ${n1(r.underground_km)} km`], ["Poles", n0(r.poles)], ["Bushfire-prone spans", pct(r.bushfire_share * 100)],
      ["30-day peak", `${n0(r.amps_peak_30d)} A of ${r.rating_amps} A (${n0(r.spare_amps)} A spare)`], ["Rating set by", r.limited_by || "-"], ["Customer minutes 12m", n0(r.cmos_12m)],
      ["Spans inside vegetation clearance", n0(r.veg_cat_a)], ["Open defects", n0(r.open_defects)]].flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)])),
    h("h4", {}, "Load, last 14 days"), c, out);
  const s = await api(`/api/series/feeder/${r.feeder_id}`);
  chart(c, { grid: { left: 46, right: 24, top: 46, bottom: 30 }, legend: { textStyle: { color: "#5f7185" }, top: 0 }, xAxis: axis({ type: "time" }), yAxis: axis({ type: "value", name: "A" }),
    series: [{ type: "line", name: "Amps (history)", data: s.history.map((x) => [x.t, x.amps]), showSymbol: false, color: "#24575f", lineStyle: { width: 1.8 } },
      { type: "line", name: "Amps (live)", data: s.live.map((x) => [x.t, x.amps]), showSymbol: false, color: "#e2683c", lineStyle: { width: 2.2 } },
      { type: "line", name: "Rating", markLine: { data: [{ yAxis: r.rating_amps }], lineStyle: { color: "#8a63b8", type: "dashed" }, symbol: "none", label: { formatter: "Rating" } }, data: [] }] });
  out.append(h("h4", {}, "Recent outages"), table([{ l: "Start", f: (x) => day(x.start_time) }, { l: "Cause", k: "cause_label" }, { l: "Device", k: "device_label" },
    { l: "Customers", k: "customers_interrupted", r: 1 }, { l: "Minutes", f: (x) => n0(x.duration_min), r: 1 }], s.outages, null, { max: 260 }));
}

async function zoneDetail(r, name) {
  const out = h("div", {}, loading("Loading the zone's customers."));
  drawer(h("h2", {}, name), h("p", { class: "muted" }, `${cnt(r.customers, "customer", "customers")} on ${cnt(r.feeders, "feeder", "feeders")} and ${cnt(r.ds, "distribution substation", "distribution substations")}, across ${cnt(r.suburbs, "suburb", "suburbs")}.`),
    h("a", { class: "btn small", href: `#/scene/zone/${r.zs_id}`, onclick: () => $("#drawer").classList.remove("open") }, "Site view"),
    h("div", { class: "kv", style: "margin:12px 0" }, ...[["Life support", n0(r.life_support)], ["Critical sites", n0(r.critical)], ["Rooftop solar", `${n1(r.solar_mw)} MW`],
      ["Home batteries", n0(r.batteries)], ["EV chargers", n0(r.ev_chargers)]].flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)])), out);
  const z = await api(`/api/zone-customers/${r.zs_id}`);
  out.innerHTML = "";
  out.append(h("h4", {}, "Customers by type"), table([{ l: "Type", k: "label" }, { l: "Customers", f: (x) => n0(x.customers), r: 1 }, { l: "Life support", k: "life_support", r: 1 },
      { l: "Solar MW", f: (x) => n1(x.solar_mw), r: 1 }], z.types, null, { noScroll: 1 }),
    h("h4", {}, "Largest suburbs"), table([{ l: "Suburb", k: "suburb" }, { l: "Customers", f: (x) => n0(x.customers), r: 1 }, { l: "Life support", k: "life_support", r: 1 }], z.suburbs, null, { noScroll: 1 }),
    z.sites.length ? h("h4", {}, "Critical sites") : null,
    z.sites.length ? table([{ l: "Site", k: "site_name" }, { l: "Type", k: "site_type" }, { l: "Suburb", k: "suburb" }, { l: "Backup generator", f: (x) => x.backup_generator ? "Yes" : "No" }], z.sites, null, { noScroll: 1 }) : null);
}

// ---------------------------------------------------------------- Network map
let netCache = null;
export async function networkLayer(m, highlight = []) {
  netCache = netCache || await api("/api/map/network");
  const g = L.layerGroup().addTo(m);
  for (const f of netCache.feeders) {
    const hl = highlight.includes(f.feeder_id);
    L.geoJSON(f.geo, { style: { color: hl ? "#c98a1e" : STATUS_COLOUR[f.status] || "#6f8fa8", weight: hl ? 3 : 1.6, opacity: .9 } })
      .bindTooltip(`${f.feeder_id} ${f.feeder_name}<br>${cnt(f.customers, "customer", "customers")}`).on("click", () => openAsset(f.feeder_id + "-CB01")).addTo(g);
  }
  for (const z of netCache.zones) L.circleMarker([z.lat, z.lon], { radius: 6, color: "#fff", weight: 2, fillColor: "#0c5a66", fillOpacity: 1 }).bindTooltip(z.name).addTo(g);
  transmissionLayer(g);
  return g;
}

// the 132 kV circuits from the bulk supply points and the transmission substations they feed (a layer group; fills the group it is given)
export function transmissionLayer(g) {
  const sites = Object.fromEntries((netCache?.transmission || []).map((t) => [t.ts_id, t]));
  for (const l of netCache?.tlines || []) {
    const t = sites[l.ts_id];
    L.geoJSON(l.geo, { style: { color: "#1f3b42", weight: 4.5, opacity: .85 } })
      .bindTooltip(`132 kV circuit ${l.circuit} into ${t ? t.name : l.ts_id}`).addTo(g);
  }
  for (const t of netCache?.transmission || []) {
    L.circleMarker([t.lat, t.lon], { radius: 8, color: "#fff", weight: 2, fillColor: STATUS_COLOUR[t.status] || "#1f3b42", fillOpacity: 1 })
      .bindTooltip(`${t.name}: ${t.transformers} × ${t.transformer_mva} MVA, ${cnt(t.zone_count, "zone substation", "zone substations")}`)
      .on("click", () => { location.hash = `#/scene/transmission/${t.ts_id}`; }).addTo(g);
  }
  return g;
}

export async function mapScreen() {
  // The map shows only what threatens the network: fires, satellite hotspots, storm gusts and live outages. Click a point (with a radius
  // from 10 m to 100 km) or draw an area, and a PostGIS query in Lakebase lists every customer, suburb and asset inside it on the right;
  // from there the customers can be notified on the channels they have registered for.
  view.innerHTML = "";
  const el = h("div", { class: "map tall" });
  const side = h("div", { class: "area-side" });
  const layers = { fires: true, hotspots: true, weather: true, outages: true, transmission: true, vegetation: false };
  const bar = h("div", { class: "layers" }, Object.keys(layers).map((k) => h("label", {}, h("input", { type: "checkbox", checked: layers[k] ? "" : null, onchange: (e) => toggle(k, e.target.checked) }),
    { fires: "Fires (RFS, live)", hotspots: "Satellite hotspots (DEA)", weather: "Storm gusts (forecast)", outages: "Live outages",
      transmission: "132 kV circuits and transmission substations", vegetation: "Vegetation clearance by span (LiDAR)" }[k])));
  view.append(h("div", { class: "screen-h" }, h("h1", {}, "Network map"), h("span", { class: "q" }, "Hazards near the network, and the assets and customers within their reach.")), bar,
    h("div", { style: "display:grid;grid-template-columns:1fr 420px;gap:14px" }, el, side));
  const m = mapIn(el, [-33.62, 150.85], 9);
  const groups = {};
  const load = {
    fires: async () => {
      const d = await api("/api/map/fire");
      const g = L.layerGroup();
      for (const f of d.fires) {
        if (f.perimeter_geojson) L.geoJSON(JSON.parse(f.perimeter_geojson), { style: { color: "#d2462f", fillOpacity: .35 } }).addTo(g);
        L.circleMarker([f.lat, f.lon], { radius: 8, color: "#fff", weight: 1.5, fillColor: f.alert_level === "Advice" ? "#c98a1e" : "#d2462f", fillOpacity: .95 })
          .bindTooltip(`<b>${f.title}</b><br>${f.alert_level}, ${f.status || ""}<br>${f.incident_type || ""}, ${f.size || ""}<br><span style="font-size:11px">NSW RFS, ${f.updated || ""}</span>`).addTo(g);
      }
      return g;
    },
    hotspots: async () => {
      const d = await api("/api/map/fire");
      const g = L.layerGroup();
      for (const s of d.hotspots) L.circleMarker([s.lat, s.lon], { radius: 4, color: "#dd4b1a", fillColor: "#f2a900", fillOpacity: .9, weight: 1 }).bindTooltip(`${s.satellite} hotspot, ${n1(s.hours_since)} h ago`).addTo(g);
      return g;
    },
    weather: async () => {
      const d = await api("/api/map/weather");
      const g = L.layerGroup();
      for (const c of d.cells.filter((x) => x.gust > 50)) L.geoJSON(c.geo, { style: { color: "#0000", fillColor: c.gust > 90 ? "#d2462f" : c.gust > 70 ? "#c98a1e" : "#2f7dc4", fillOpacity: .3 } })
        .bindTooltip(`Gust ${c.gust} km/h, rain ${c.rain} mm, ${hhmm(d.time)} ${day(d.time)}`).addTo(g);
      return g;
    },
    transmission: async () => {
      netCache = netCache || await api("/api/map/network");
      return transmissionLayer(L.layerGroup());
    },
    // every surveyed span, coloured by its risk tier (on a canvas, so all 20,000 draw quickly); a span opens in the LiDAR view
    vegetation: async () => {
      warmLidar();   // a span on this layer opens the LiDAR view: its models start loading now
      const d = await api("/api/vegetation/map-spans");
      const g = L.layerGroup(), canvas = L.canvas({ padding: 0.3 });
      const COL = { high_risk: "#d2462f", cut_required: "#c98a1e", planned: "#2f7dc4", no_action: "#2e9c6a" };
      for (const [id, t, c, a0, o0, a1, o1] of d.spans) {
        L.polyline([[a0, o0], [a1, o1]], { renderer: canvas, color: COL[t] || "#5f7185", weight: t === "no_action" ? 2 : 4, opacity: t === "no_action" ? .45 : .95 })
          .bindTooltip(`${id}, ${d.tiers[t] || t}: ${n2(c)} m clearance. Select the span for its LiDAR view.`)
          .on("click", (e) => { L.DomEvent.stopPropagation(e); openLidar(id); }).addTo(g);
      }
      return g;
    },
    outages: async () => {
      const d = await api("/api/map/live");
      const g = L.layerGroup();
      for (const o of d.outages) L.circleMarker([o.lat, o.lon], { radius: 10 + Math.min(14, o.customers_off / 80), color: "#d2462f", weight: 3, fillOpacity: .25 })
        .bindTooltip(`${o.feeder_id}: ${cnt(o.customers_off, "customer", "customers")} off supply. Open the situation from Alerts.`).addTo(g);
      return g;
    },
  };
  async function toggle(k, on) {
    if (groups[k]) { m.removeLayer(groups[k]); delete groups[k]; }
    if (on) { groups[k] = await load[k](); groups[k].addTo(m); }
  }
  // open framed on every hazard shown (fires, hotspots, storm cells, outages): all of them in view, and no more than that
  Promise.all(Object.keys(layers).filter((k) => layers[k]).map((k) => toggle(k, true))).then(() => {
    const b = L.latLngBounds([]);
    for (const g of Object.values(groups)) g.eachLayer((l) => { if (l.getBounds) b.extend(l.getBounds()); else if (l.getLatLng) b.extend(l.getLatLng()); });
    if (b.isValid()) m.fitBounds(b, { padding: [24, 24], maxZoom: 13 });
  });
  every(15000, async () => { if (groups.outages) await toggle("outages", true); });

  // ---- the area: a circle round a clicked point, or a drawn polygon
  const RADII = [10, 25, 50, 100, 250, 500, 1000, 2000, 5000, 10000, 25000, 50000, 100000];
  const rlabel = (r) => r >= 1000 ? `${r / 1000} km` : `${r} m`;
  const st = { mode: "circle", ri: 7, center: null, points: [], area: null, result: null, showSql: false };
  let shape = null, sketch = null, vertices = L.layerGroup().addTo(m);
  const clearShape = () => { if (shape) m.removeLayer(shape); if (sketch) m.removeLayer(sketch); shape = sketch = null; vertices.clearLayers(); };
  const slider = h("input", { type: "range", min: 0, max: RADII.length - 1, step: 1, value: st.ri, class: "area-range" });
  const rl = h("b", {}, rlabel(RADII[st.ri]));
  slider.addEventListener("input", () => { st.ri = +slider.value; rl.textContent = rlabel(RADII[st.ri]); if (shape && st.mode === "circle") shape.setRadius(RADII[st.ri]); });
  slider.addEventListener("change", () => { if (st.mode === "circle" && st.center) run({ shape: "circle", lat: st.center.lat, lon: st.center.lng, radius_m: RADII[st.ri] }); });
  const modeBtns = h("div", { class: "im-seg" }, [["circle", "Radius around a point"], ["polygon", "Draw an area"]].map(([k, l]) =>
    h("button", { class: k === st.mode ? "on" : "", onclick: (e) => { st.mode = k; st.points = []; clearShape(); [...e.target.parentNode.children].forEach((b) => b.classList.toggle("on", b === e.target)); controls(); } }, l)));
  const ctlBox = h("div", { class: "area-ctl" }), out = h("div", { class: "area-out" });
  side.append(card("Area selection", null, modeBtns, ctlBox), out);
  function controls() {
    ctlBox.innerHTML = "";
    if (st.mode === "circle") ctlBox.append(h("div", { class: "area-r" }, h("span", { class: "small muted" }, "Radius "), rl, slider,
      h("div", { class: "area-ticks small muted" }, h("span", {}, "10 m"), h("span", {}, "1 km"), h("span", {}, "100 km"))),
      h("p", { class: "small muted" }, "Click the map (a fire, a storm cell, an outage, anywhere) to centre the area there."));
    else ctlBox.append(h("p", { class: "small muted" }, `Click the map to add corners. Corners added so far: ${st.points.length}. Finish to close the shape from the last corner back to the first.`),
      h("div", { class: "row" }, h("button", { class: "btn small primary", disabled: st.points.length < 3 ? "" : null, onclick: finish }, "Finish area"),
        h("button", { class: "btn small", onclick: () => { st.points = []; clearShape(); controls(); } }, "Start again")));
  }
  function finish() {
    if (st.points.length < 3) return;
    if (sketch) m.removeLayer(sketch);
    shape = L.polygon(st.points, { color: "#24575f", weight: 2, fillColor: "#5490a8", fillOpacity: .15 }).addTo(m);
    run({ shape: "polygon", points: st.points.map((p) => [p.lat, p.lng]) });
    st.points = []; controls();
  }
  m.on("click", (e) => {
    if (st.mode === "circle") {
      clearShape(); st.center = e.latlng;
      shape = L.circle(e.latlng, { radius: RADII[st.ri], color: "#24575f", weight: 2, fillColor: "#5490a8", fillOpacity: .15 }).addTo(m);
      run({ shape: "circle", lat: e.latlng.lat, lon: e.latlng.lng, radius_m: RADII[st.ri] });
    } else {
      if (!st.points.length) clearShape();
      st.points.push(e.latlng);
      L.circleMarker(e.latlng, { radius: 4, color: "#24575f", fillColor: "#fff", fillOpacity: 1, weight: 2 }).addTo(vertices);
      if (sketch) m.removeLayer(sketch);
      sketch = L.polyline(st.points, { color: "#24575f", weight: 2, dashArray: "5 4" }).addTo(m);
      controls();
    }
  });
  m.on("dblclick", (e) => { if (st.mode === "polygon" && st.points.length >= 3) { L.DomEvent.stop(e); finish(); } });
  m.doubleClickZoom.disable();
  async function run(area) {
    st.area = area; st.showSql = false;
    out.innerHTML = ""; out.append(loading("Running the spatial query"));   // the full loader: centre of the screen, everything behind it veiled
    st.result = await post("/api/spatial/area", area).catch((e) => ({ error: e.message }));   // a 400 explains a bad area
    render();
  }
  function render() {
    const r = st.result; out.innerHTML = "";
    if (!r) return;
    if (r.error) { out.append(h("p", { class: "muted" }, r.error)); return; }
    const t = r.totals, a = r.area;
    const where = a.shape === "circle" ? `Within ${rlabel(a.radius_m)} of ${a.lat.toFixed(4)}, ${a.lon.toFixed(4)}` : `Inside the drawn area (${cnt(a.points, "corner", "corners")})`;
    const asset = (name) => (r.assets.find((x) => x.asset === name) || {}).count || 0;
    out.append(card("Inside the area", where,
      h("div", { class: "area-tiles" }, [["Customers", t.customers], ["Life support", t.life_support], ["Critical sites", t.critical_sites],
        ["without a generator", t.critical_no_generator], ["Suburbs", r.suburb_count], ["Feeders", r.feeders.length],
        ["Distribution substations", asset("Distribution substation")], ["Zone substations", r.zone_substations], ["Poles", asset("Pole")],
        ["Line spans", asset("Overhead span") + asset("Underground cable")]].map(([k, v]) => h("div", { class: "tile" }, h("div", { class: "k" }, k), h("div", { class: "v num" }, n0(v))))),
      h("div", { class: "row", style: "margin-top:10px" },
        h("button", { class: "btn primary", disabled: t.customers ? null : "", onclick: () => notifyDialog(r) }, "Notify customers"),
        h("button", { class: "btn", onclick: () => { const [lat, lon] = centre(st.area); RM.raiseJob({ job_type: "ASSET_INSPECTION", title: `Hazard patrol, ${where.toLowerCase()}`,
          lat, lon, priority: 5, emergency: true, where: `${where}; the nearest inspection crews are listed once it is raised.` }); } }, "Send a patrol"),
        h("button", { class: "btn", onclick: () => { st.showSql = !st.showSql; render(); } }, st.showSql ? "Hide query" : "Show query")),
      st.showSql ? h("div", {}, ...r.sql.map((q) => h("pre", { class: "sql" }, q))) : null));
    out.append(crewsNear(st.area));
    if (r.suburbs.length) out.append(card("Suburbs", `${cnt(r.suburb_count, "suburb", "suburbs")} in the area.`, table([{ l: "Suburb", f: (x) => `${x.suburb} ${x.postcode || ""}` },
      { l: "Customers", k: "customers", r: 1 }, { l: "Life support", k: "life_support", r: 1 }, { l: "Critical sites", k: "critical_sites", r: 1 }, { l: "Feeders", k: "feeders", r: 1 }], r.suburbs, null, { max: 220 })));
    if (r.feeders.length) out.append(card("Feeders", "Customers and lines inside the area.", table([{ l: "Feeder", f: (x) => `${x.feeder_id}` }, { l: "Status", f: (x) => st2(x.status) },
      { l: "Customers", k: "customers", r: 1 }, { l: "Life support", k: "life_support", r: 1 }, { l: "Spans", k: "spans", r: 1 }, { l: "Poles", k: "poles", r: 1 }], r.feeders, (x) => openAsset(x.feeder_id), { max: 220 })));
    if (r.distribution_substations.length) out.append(card("Distribution substations", `${cnt(asset("Distribution substation"), "distribution substation", "distribution substations")} in the area.`, table([{ l: "Substation", k: "ds_id" },
      { l: "Feeder", k: "feeder_id" }, { l: "kVA", k: "kva", r: 1 }, { l: "Customers", k: "customers", r: 1 }, { l: "Status", f: (x) => st2(x.status) }], r.distribution_substations, (x) => openAsset(x.ds_id), { max: 220 })));
    if (r.assets.length) out.append(card("All assets", "Every asset in the area, by type.", table([{ l: "Asset", k: "asset" }, { l: "Count", k: "count", r: 1 }], r.assets, null, { noScroll: 1 })));
  }
  const st2 = (s) => h("span", { class: `st ${s || ""}` }, { red: "Needs action", amber: "Watch", green: "Normal" }[s] || "");
  const centre = (a) => a.shape === "circle" ? [a.lat, a.lon] : [a.points.reduce((x, p) => x + p[0], 0) / a.points.length, a.points.reduce((x, p) => x + p[1], 0) / a.points.length];
  // Field crews in and within 5 km of the area, from resource management: who could respond, and what each is doing now.
  function crewsNear(a) {
    const box = card("Field crews nearby", "In the area or within 5 km of it. Choose a crew to see its day.", h("p", { class: "muted small" }, "Finding crews."));
    const [lat, lon] = centre(a), reach = (a.shape === "circle" ? a.radius_m / 1000 : 0) + 5;
    const km = (p) => { const r = Math.PI / 180, x = (p.lon - lon) * r * Math.cos(lat * r), y = (p.lat - lat) * r; return 6371 * Math.hypot(x, y); };
    api("/api/rm/positions").then((d) => {
      const rows = d.crews.filter((p) => !["leave", "training", "incident"].includes(p.status)).map((p) => ({ ...p, km: Math.round(km(p) * 10) / 10 }))
        .filter((p) => p.km <= reach).sort((x, y) => x.km - y.km);
      box.lastChild.replaceWith(rows.length ? table([{ l: "Crew", k: "crew_name" }, { l: "Persona", k: "persona" }, { l: "Now", k: "status_label" }, { l: "Km", k: "km", r: 1 }],
        rows, (p) => RM.crewDrawer(p.crew_id), { max: 220 }) : h("p", { class: "muted small" }, "No field crews are within 5 km right now."));
    });
    return box;
  }
  function notifyDialog(r) {
    const t = r.totals, a = r.area;
    const where = a.shape === "circle" ? `within ${rlabel(a.radius_m)} of the incident` : "in the affected area";
    const text = h("textarea", { class: "area-msg", rows: 6 },
      `Electricity network notice: there is a hazard ${where} that may affect your power supply. Crews are monitoring and power may be turned off for safety at short notice. If you rely on life support equipment, enact your backup plan now. Updates are published on our outages page.`);
    const aud = { v: "all" };
    const counts = () => aud.v === "life_support" ? { sms: t.ls_sms, email: t.ls_email, facebook: t.ls_facebook } : { sms: t.sms, email: t.email, facebook: t.facebook };
    const chosen = new Set(["sms"]);
    const chanBox = h("div", { class: "area-chans" });
    const paintChans = () => {
      const c = counts(); chanBox.innerHTML = "";
      for (const [k, l] of [["sms", "SMS"], ["email", "Email"], ["facebook", "Facebook Messenger"]]) chanBox.append(h("label", { class: "area-chan" },
        h("input", { type: "checkbox", checked: chosen.has(k) ? "" : null, onchange: (e) => { e.target.checked ? chosen.add(k) : chosen.delete(k); } }),
        h("span", {}, l), h("span", { class: "muted small" }, `${cnt(c[k], "customer", "customers")} registered`)));
    };
    paintChans();
    const audSeg = h("div", { class: "im-seg" }, [["all", `Everyone (${n0(t.customers)})`], ["life_support", `Life support only (${n0(t.life_support)})`]].map(([k, l]) =>
      h("button", { class: k === aud.v ? "on" : "", onclick: (e) => { aud.v = k; [...e.target.parentNode.children].forEach((b) => b.classList.toggle("on", b === e.target)); paintChans(); } }, l)));
    const status = h("p", { class: "small muted" }, `${cnt(t.no_channel, "customer in the area has", "customers in the area have")} no channel registered and will not receive this notice.`);
    const bg = modal(h("h2", {}, "Notify customers"), h("p", { class: "muted small" }, `${cnt(t.customers, "customer", "customers")} ${a.shape === "circle" ? `within ${rlabel(a.radius_m)}` : "in the drawn area"}. Each gets the notice on every chosen channel they have registered for.`),
      h("h4", {}, "Who"), audSeg, h("h4", {}, "Channels"), chanBox, h("h4", {}, "Message"), text, status,
      h("div", { class: "row", style: "margin-top:10px" },
        h("button", { class: "btn primary", onclick: async (e) => {
          e.target.disabled = true; e.target.textContent = "Sending";
          const res = await post("/api/notify/area", { area: st.area, body: text.value, channels: [...chosen], audience: aud.v });
          bg.remove(); toast(res.message || "Sent", res.sent ? "green" : "");
        } }, "Send notification"),
        h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel")));
  }
  controls();
  side.append(h("p", { class: "attr" }, "Fires: State of NSW (NSW RFS), CC BY 4.0. Hotspots: DEA Hotspots, Geoscience Australia, CC BY 4.0. Forecast: Open-Meteo, CC BY 4.0. Basemap: OpenStreetMap."));
}

// ---------------------------------------------------------------- the asset drawer (opened from every screen and the Asset management search)
// The work a crew does on each kind of asset, for the asset panel's Raise a job.
const JOB_FOR = { zone_substation: "SUBSTATION_MAINT", distribution_substation: "SUBSTATION_MAINT", cb: "PROTECTION_TEST", recloser: "PROTECTION_TEST",
  span: "LV_MAINT", underground_cable: "CABLE_REPAIR", pillar: "CABLE_REPAIR", pit: "CIVIL", pole: "HV_MAINT", gas_switch: "HV_MAINT", fuse: "LV_MAINT", nop: "HV_MAINT" };
const JOB_VERB = { zone_substation: "Maintain", distribution_substation: "Maintain", cb: "Test the protection on", recloser: "Test the protection on", underground_cable: "Repair",
  pole: "Inspect and repair", span: "Inspect and repair" };

// quiet: opened from the site view, where the lineworker running to the asset is the wait, so no veil over the scene
export async function openAsset(id, quiet = false) {
  drawer(quiet ? h("p", { class: "muted" }, "Loading the details.") : loading());
  let a;
  try { a = await api(`/api/asset/${encodeURIComponent(id)}`); } catch (e) {
    drawer(h("h2", {}, id), h("p", { class: "muted" }, e.status === 404 ? "This record is not in the asset register." : "The record could not be loaded. Please try again."));
    return;
  }
  const r = a.record;
  if (a.kind === "nmi") {
    const c = chartBox("short");
    drawer(h("h2", {}, `NMI ${r.nmi}`), h("p", { class: "muted" }, `${r.address || ""}, ${r.customer_type_label || ""}`),
      h("button", { class: "btn small", onclick: () => RM.raiseJob({ job_type: "SERVICE_CALL", title: `Service call, ${r.address || r.nmi}`, nmi: r.nmi, feeder_id: r.feeder_id,
        lat: r.lat, lon: r.lon, customers: 1, where: `${r.address || ""}, supplied by ${r.ds_id} on ${r.feeder_id}` }) }, "Raise a job"),
      h("div", { class: "kv" }, ...[["Location", r.lat != null ? `${(+r.lat).toFixed(5)}, ${(+r.lon).toFixed(5)}, ${n0(r.elevation_m)} m above sea level` : "-"], ["Supplied by", r.ds_id], ["Feeder", r.feeder_id], ["Life support", r.life_support ? `Yes (${r.life_support_equipment})` : "No"], ["Critical site", r.critical_site || "No"],
        ["Rooftop solar", r.solar_kw ? `${r.solar_kw} kW` : "None"], ["Battery / EV", `${r.has_battery ? "Battery" : "No battery"}; ${r.has_ev_charger ? "EV charger" : "No EV charger"}`]].flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)])),
      h("h4", {}, "Systems"), h("div", { class: "kv" }, ...a.systems.flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)])),
      a.intervals.length ? h("div", {}, h("h4", {}, "Interval energy (NEM12), last days"), c) : h("p", { class: "muted small" }, "Interval data is held only for the hero slice."));
    if (a.intervals.length) chart(c, { xAxis: axis({ type: "time" }), yAxis: axis({ type: "value", name: "kWh" }), series: ["E1", "B1"].map((reg) => ({ type: "line", name: reg === "E1" ? "Import" : "Export", showSymbol: false,
      data: a.intervals.filter((x) => x.register === reg).map((x) => [x.t, x.kwh]), color: reg === "E1" ? "#0c5a66" : "#c98a1e" })) });
    return;
  }
  const scene = r.asset_class === "zone_substation" ? `#/scene/zone/${r.asset_id}` : r.asset_class === "transmission_substation" ? `#/scene/transmission/${r.asset_id}`
    : a.transformer ? `#/scene/transmission/${a.transformer.ts_id}` : r.feeder_id ? `#/scene/feeder/${r.feeder_id}` : null;
  const kv = (rows) => h("div", { class: "kv" }, ...rows.filter(([, v]) => v !== null && v !== undefined && v !== "").flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)]));
  const sec = (title, ...body) => h("div", { class: "ad-sec" }, h("h4", {}, title), ...body);
  const jobs = a.jobs || [], live = jobs.filter((j) => j.open), done = jobs.filter((j) => !j.open);
  const reps = a.reports || [], photos = a.photos || [], sits = a.situations || [], os = a.order_summary || {};
  // the at-a-glance strip: what is happening on this asset now and what it has been through
  const chip = (n, one, many, cls = "") => h("span", { class: `ad-chip ${n ? cls : "none"}` }, h("b", {}, n0(n)), ` ${n === 1 ? one : many}`);
  const strip = h("div", { class: "ad-chips" },
    chip(live.length, "job open", "jobs open", "amber"), chip(a.defects.length, "open defect", "open defects", "red"),
    chip(sits.length, "open situation", "open situations", "red"), chip(a.outages.length, "outage on record", "outages on record"),
    chip(os.n || 0, "work order", "work orders"), chip(reps.length, "field report", "field reports"), chip(photos.length, "photo", "photos"));
  const JOB_CLS = { driving: "amber", in_progress: "amber", done: "green", parked: "red" };
  const jobCard = (j) => h("div", { class: `ad-job ${j.open ? "" : "closed"}` },
    h("div", { class: "ad-job-h" }, h("b", {}, j.title), h("span", { class: `st ${JOB_CLS[j.status] || ""}` }, j.status_label)),
    h("div", { class: "ad-job-m" }, [j.job_type_label, `priority ${j.priority}${j.emergency ? ", emergency" : ""}`, `${j.region_name} region`, day(j.when)].filter(Boolean).join(" · ")),
    h("div", { class: "ad-job-m" }, j.crew_name ? `${j.dispatched ? "Dispatched to" : "On the plan for"} ${j.crew_name}` : "No crew yet. It waits on today's plan",
      j.raised_at ? `, raised ${ago(j.raised_at)}` : "", `. ${j.origin}.`),
    h("a", { class: "ad-link", href: "#/res/control", onclick: () => $("#drawer").classList.remove("open") }, "Open in Field staff management"));
  const repRow = (x) => h("div", { class: "ad-rep" },
    h("div", { class: "ad-job-h" }, h("b", {}, `${x.crew_name || x.reported_by || "Crew"}`), h("span", { class: `st ${x.outcome === "fixed" ? "green" : "amber"}` }, x.outcome_label || "Reported")),
    h("div", { class: "ad-job-m" }, `${day(x.reported_at)} ${hhmm(x.reported_at)}`, x.customers_restored ? ` · ${n0(x.customers_restored)} customers restored` : "",
      x.access_label ? ` · access ${x.access_label.toLowerCase()}` : "", x.hazard_labels?.length ? ` · hazards: ${x.hazard_labels.join(", ")}` : ""),
    x.materials?.length ? h("div", { class: "ad-job-m" }, "Materials: " + x.materials.map((m) => `${m.label} × ${m.qty}`).join(", ")) : null,
    x.notes || x.response_note ? h("p", { class: "ad-note" }, x.notes || x.response_note) : null);
  const shown = photos.filter((p) => p.image_url);
  const photoTile = (p) => h("figure", { class: `ad-ph ${p.photo_id === a.current_photo_id ? "current" : ""}` },
    p.image_url ? h("button", { class: "ad-ph-open", type: "button", title: "Open the photo larger", onclick: () => photoViewer(shown, shown.indexOf(p), r.name || r.asset_id) },
      h("img", { src: p.image_url, loading: "lazy", alt: p.note || "Asset photo" })) : h("div", { class: "ad-ph-gone" }, "This photo has been deleted."),
    h("figcaption", {}, h("b", {}, p.photo_id === a.current_photo_id ? "Current photo" : p.status_label), h("br"), `${day(p.submitted_at)}${p.note ? ", " + p.note : ""}`,
      p.review_notes ? h("div", { class: "muted" }, `Reviewer: ${p.review_notes}`) : null));
  const nothing = !jobs.length && !reps.length && !a.outages.length && !a.defects.length && !photos.length;
  const reopen = () => openAsset(id, true);
  drawer(h("h2", {}, r.name || r.asset_id), h("p", { class: "muted" }, `${r.asset_class_label}${r.feeder_id ? " on " + r.feeder_id : ""}`),
    h("div", { class: "row" }, scene ? h("a", { class: "btn small", href: scene, onclick: () => $("#drawer").classList.remove("open") }, "Site view") : null,
      r.asset_class === "overhead_span" ? (warmLidar(), h("button", { class: "btn small", type: "button", onclick: () => openLidar(r.asset_id) }, "LiDAR view")) : null,
      r.lat != null ? h("button", { class: "btn small", onclick: () => RM.raiseJob({ job_type: JOB_FOR[r.asset_class] || "HV_MAINT", title: `${JOB_VERB[r.asset_class] || "Inspect and repair"} ${r.name || r.asset_id}`,
        asset_id: r.asset_id, feeder_id: r.feeder_id, lat: r.lat, lon: r.lon, customers: r.customers_downstream || 0, priority: r.open_defects ? 4 : 3,
        where: `${r.asset_class_label}${r.feeder_id ? " on " + r.feeder_id : ""}; ${n0(r.customers_downstream)} customers downstream`, onRaised: reopen }) }, "Raise a job") : null),
    strip,
    live.length ? sec("Work in progress", ...live.map(jobCard)) : null,
    sits.length ? sec("Open situations", table([{ l: "Situation", k: "title" }, { l: "Severity", f: (x) => title(x.severity) }, { l: "Link", f: (x) => x.how }],
      sits, (x) => { $("#drawer").classList.remove("open"); go(`#/situations/${x.situation_id}`); }, { noScroll: 1 })) : null,
    a.defects.length ? sec("Open defects", table([{ l: "Defect", k: "defect_label" }, { l: "Priority", k: "priority", r: 1 }, { l: "Age days", k: "age_days", r: 1 }], a.defects, null, { noScroll: 1 })) : null,
    reps.length ? sec("Field reports", ...reps.map(repRow)) : null,
    photos.length ? sec("Photos", h("div", { class: "ad-phs" }, ...photos.map(photoTile))) : null,
    a.outages.length ? sec("Past failures and outages", table([{ l: "Start", f: (x) => day(x.start_time) }, { l: "Cause", k: "cause_label" },
      { l: "Minutes", k: "duration_min", r: 1 }, { l: "Customers", k: "customers_interrupted", r: 1 }], a.outages, null, { noScroll: 1 })) : null,
    sec("Work orders", a.orders.length
      ? [h("p", { class: "muted small" }, `${n0(os.n)} work order${os.n === 1 ? "" : "s"}, ${money(os.total)} spent in total${a.orders.some((x) => x.asset_id !== r.asset_id) ? `, with the work on its ${r.asset_class === "pole" ? "span" : "pole"}` : ""}`),
        table([{ l: "Type", k: "order_type_label" }, { l: "On", f: (x) => x.asset_id === r.asset_id ? "This asset" : x.asset_id }, { l: "What", k: "description" },
          { l: "Created", f: (x) => day(x.created_date) }, { l: "Status", k: "status_label" }, { l: "Cost", f: (x) => money(x.cost_aud), r: 1 }], a.orders, null, { noScroll: 1 })]
      : h("p", { class: "muted small" }, "No work orders on record for this asset.")),
    done.length ? sec("Completed jobs", ...done.map(jobCard)) : null,
    nothing ? h("p", { class: "muted small ad-sec" }, "No jobs, defects, outages, field reports or photos on record for this asset.") : null,
    sec("Condition and impact", kv([["Installed", r.install_year], ["Age", r.age_years ? `${r.age_years} years` : null], ["Material / type", r.material],
      ["Rating", r.rating ? `${n1(r.rating)} ${r.rating_unit || ""}` : null], ["Open defects", a.defects.length], ["Customers downstream", r.customers_downstream != null ? n0(r.customers_downstream) : null],
      ["Life support downstream", r.life_support_downstream != null ? n0(r.life_support_downstream) : null],
      ["Fault probability (1 year)", a.fault_probability !== null && a.fault_probability !== undefined ? pct(a.fault_probability * 100) : null]])),
    a.site ? sec("Site", kv([["Supply", `${a.site.kv} kV from ${a.site.bulk_supply_name}`], ["Stepped down to", `${a.site.secondary_kv} kV, ${a.site.transformers} × ${a.site.transformer_mva} MVA`],
      ["Firm capacity (N-1)", `${n0(a.site.firm_mva)} MVA`], ["30-day peak", `${n1(a.site.peak_mw_30d)} MW, ${pct(a.site.peak_utilisation_30d * 100)} of firm`],
      ["Zone substations fed", n0(a.site.zone_count)], ["Customers in those zones", n0(a.site.customers)]])) : null,
    a.transformer ? sec("Transformer load", kv([["Rating", `${n1(a.transformer.rating_mva)} MVA`],
      ["30-day peak", `${n1(a.transformer.peak_mw_30d)} MW, ${pct(a.transformer.peak_utilisation_30d * 100)} of rating`], ["Latest reading", a.transformer.mw_now != null ? `${n1(a.transformer.mw_now)} MW` : null],
      ["Top oil, latest", a.transformer.top_oil_now != null ? `${n1(a.transformer.top_oil_now)} °C` : null]])) : null,
    a.vegetation ? sec("Vegetation (LiDAR)", h("p", {}, `Clearance ${a.vegetation.clearance_m} m, ${a.vegetation.clearance_12m_m} m in 12 months; ${a.vegetation.dominant_species}${a.vegetation.hazard_tree ? "; hazard tree" : ""}`)) : null,
    sec("One asset, every system", kv(a.systems)));
}

// a field photo large, with its caption, and the asset's other photos one step either way (arrow keys too)
function photoViewer(list, i, assetName) {
  if (!list.length) return;
  const img = h("img", { class: "pv-img", alt: "" }), cap = h("div", { class: "pv-cap" });
  const show = (k) => {
    i = (k + list.length) % list.length;
    const p = list[i];
    img.src = p.image_url; img.alt = p.note || "Asset photo";
    cap.innerHTML = "";
    cap.append(...[h("b", {}, `${assetName}: ${p.status_label || "Photo"}`), ` ${day(p.submitted_at)} ${hhmm(p.submitted_at)}${p.submitted_by ? `, sent by ${p.submitted_by.split("@")[0]}` : ""}`,
      p.note ? h("div", {}, `Note: ${p.note}`) : null, p.review_notes ? h("div", { class: "muted" }, `Reviewer: ${p.review_notes}`) : null,
      list.length > 1 ? h("div", { class: "muted" }, `${i + 1} of ${list.length}`) : null].filter(Boolean));
  };
  const nav = (d, label) => list.length > 1 ? h("button", { class: "btn pv-nav", "aria-label": label, onclick: () => show(i + d) }, d < 0 ? "‹" : "›") : null;
  const bg = modal(h("div", { class: "pv" }, nav(-1, "Previous photo"), img, nav(1, "Next photo")), cap,
    h("div", { class: "row" }, h("a", { class: "btn small", href: "#", onclick: (e) => { e.preventDefault(); window.open(list[i].image_url, "_blank"); } }, "Open full size"),
      h("button", { class: "btn small", onclick: () => close() }, "Close")));
  bg.querySelector(".modal").classList.add("pv-modal");
  const key = (e) => { if (!bg.isConnected) return document.removeEventListener("keydown", key); if (e.key === "ArrowLeft") show(i - 1); if (e.key === "ArrowRight") show(i + 1); if (e.key === "Escape") close(); };
  const close = () => { bg.remove(); document.removeEventListener("keydown", key); };
  document.addEventListener("keydown", key);
  show(i);
}

// ---------------------------------------------------------------- 3D
export async function scene3d(kind, id) {
  const mod = await import("./scene3d.js?v=2");
  return mod.show(view, kind, id);
}
