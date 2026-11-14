// Operations (#/ops): restoration and switching. Restoration in progress: the headline tiles (each opens its detail: the outages, the
// crews, the customers off supply, the life support customers off, the field reports), the outages (each opens its full detail) and the
// crews deployed. The timeline of the live streams: meter alarms, customer calls (each with its customer and a check of the problem
// against the network) and switch positions; a call or an alarm opens the customer's record, a switch its device. Then the crews'
// performance and damage by cause.
// Everything is read from Lakebase by /api/ops/*.
import { api, h, card, tile, table, st, n0, hhmm, chart, chartBox, loading, toast, modal } from "./ui.js";
import { openPremises } from "./customers.js";
import { openAsset } from "./screens.js";
import { crewDrawer } from "./rm.js";

const cnt = (n, one, many = `${one}s`) => `${n0(n)} ${+n === 1 ? one : many}`;
const POLL_MS = 15000;   // restoration and the timeline refresh this often while the screen is open

const pctOf = (a, b) => (b ? Math.round((100 * a) / b) : 0);
const bar = (share, cls = "") => h("div", { class: `ops-bar ${cls}`, role: "img", "aria-label": `${share} percent` }, h("span", { style: `width:${Math.max(0, Math.min(100, share))}%` }));
const minutes = (v) => (v === null || v === undefined ? "-" : `${n0(v)} min`);
const when = (ts) => (ts ? `${hhmm(ts)}` : "-");
const CHECK_TONE = { verified: "red", resolved: "green", not_verified: "", attend: "amber" };
const toAlert = (o, bg) => { if (!o.situation_id) return; bg?.remove(); location.hash = `#/situations/${o.situation_id}`; };
const offFor = (o) => {   // how long the outage has had customers off, or took to restore
  const end = o.status === "active" ? Date.now() : Date.parse(o.restored_at || "") || Date.now(), m = Math.max(0, Math.round((end - Date.parse(o.start_time)) / 60000));
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
};

// One outage in full: what happened stage by stage, the feeder and device, the crew and the repair job, the field report, and the
// customers it has off; its alert is a click away.
async function outageDetail(id) {
  const box = h("div", {}, loading("Reading the outage."));
  const bg = detail(`Outage on ${id}`, "Everything recorded for this outage.", () => box);
  let d;
  try { d = await api(`/api/ops/outage/${encodeURIComponent(id)}`); } catch (e) { box.replaceChildren(h("p", { class: "muted" }, `The outage could not be read. ${e.message}`)); return; }
  const o = d.outage, rp = d.report;
  const kv = (rows) => h("div", { class: "kv" }, ...rows.filter(([, v]) => v !== null && v !== undefined && v !== "").flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)]));
  bg.querySelector("h2").textContent = `${o.feeder_name || o.feeder_id}: ${o.cause_label.toLowerCase()}`;
  bg.querySelector("p.muted").textContent = `Outage ${o.outage_id}, ${o.status === "active" ? `customers off for ${offFor(o)}` : `restored after ${offFor(o)}`}.`;
  box.replaceChildren(
    h("div", { class: "grid2e" },
      h("div", {}, h("h4", { class: "ops-h4" }, "The outage"),
        kv([["Status", st(o.status === "active" ? "red" : "green", o.status_label)], ["Started", `${when(o.start_time)}, ${offFor(o)} ${o.status === "active" ? "ago" : "to restore"}`],
          ["Feeder", `${o.feeder_name || o.feeder_id} (${o.feeder_id}), ${o.zs_name || ""}`], ["Device", `${o.device_label || "Device"} ${o.device_id}`],
          ["Cause", o.cause_label], ["Customers", `${n0(o.customers_restored)} of ${n0(o.customers_initial)} restored, ${n0(o.customers_off)} still off`],
          ["Life support off", n0(o.life_support_off)], ["Restored automatically", `${n0(o.flisr_customers)} by FLISR`],
          ["Estimated back", o.status === "active" && o.etr ? `${when(o.etr)}` : null], ["Latest stage", o.stage_label]])),
      h("div", {}, h("h4", { class: "ops-h4" }, "The crew and the repair"),
        kv([["Crew", o.crew_name || "Not yet sent"], ["Repair job", o.job_status_label || "Not raised"],
          ["Field report", rp ? `${rp.outcome_label}, filed by ${rp.crew_name || "the crew"} at ${when(rp.reported_at)}` : "None yet"],
          ["Arrived", rp && rp.arrived_at ? when(rp.arrived_at) : null], ["Fault found", rp && rp.fault_kind_label ? `${rp.fault_kind_label}${rp.fault_ref ? ` ${rp.fault_ref}` : ""}` : null],
          ["Hazards", rp && rp.hazards.length ? rp.hazards.join(", ") : null], ["Customers restored by the crew", rp ? n0(rp.customers_restored) : null],
          ["Crew's notes", rp ? [rp.response_note, rp.notes].filter(Boolean).join(" ") || null : null]]))),
    h("h4", { class: "ops-h4" }, "What happened, stage by stage"),
    table([{ l: "Time", f: (x) => when(x.ts), r: 1 }, { l: "Stage", k: "action_label" }, { l: "Device", f: (x) => x.device || "-" },
      { l: "Customers", f: (x) => (x.customers === null || x.customers === undefined ? "-" : n0(x.customers)), r: 1 }, { l: "Note", f: (x) => x.note || "-" }], d.stages, null, { noScroll: 1 }),
    h("h4", { class: "ops-h4" }, `Customers off in this outage (${n0(d.premises.length)})`),
    d.premises.length ? table([{ l: "Address", f: (p) => p.address || "-" }, { l: "NMI", k: "nmi" }, { l: "Life support", f: (p) => (p.life_support ? "Yes" : "No") },
      { l: "Critical site", f: (p) => p.critical_site || "No" }], d.premises, (p) => openPremises(p.nmi), { max: 260 }) : h("p", { class: "muted" }, "No customer is off in this outage now."),
    h("div", { class: "row" }, o.situation_id ? h("button", { class: "btn primary", onclick: () => toAlert(o, bg) }, "Open the outage's alert") : null,
      o.crew_id ? h("button", { class: "btn", onclick: () => crewDrawer(o.crew_id) }, "The crew's day") : null));
}

// ---------------------------------------------------------------- the tiles' detail
// Each tile opens a window with what is behind its figure; a row leads on to the outage's alert, the crew or the customer.
function detail(titleText, sub, body) {
  const bg = modal(h("h2", {}, titleText), h("p", { class: "muted" }, sub), body(() => bg), h("div", { class: "row ops-end" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Close")));
  bg.querySelector(".modal").classList.add("ops-detail");
  return bg;
}
const outageCols = [
  { l: "Feeder", k: "feeder_id" }, { l: "Status", f: (x) => st(x.status === "active" ? "red" : "green", x.status_label) },
  { l: "Started", f: (x) => when(x.start_time) }, { l: "Restored of total", f: (x) => `${n0(x.customers_restored)} of ${n0(x.customers_initial)}`, r: 1 },
  { l: "Still off", f: (x) => n0(x.customers_off), r: 1 }, { l: "Cause", k: "cause_label" },
];
function tileDetail(kind, r) {
  const t = r.totals, active = r.outages.filter((o) => o.status === "active");
  if (kind === "restored") return detail("Customers restored", `${n0(t.customers_restored)} of ${n0(t.customers_total)} customers are back on supply across the outages active now or restored in the last six hours. Select an outage for its detail.`,
    () => table([...outageCols, { l: "Restored automatically (FLISR)", f: (x) => n0(x.flisr_customers), r: 1 }, { l: "Restored by the crew", f: (x) => n0(x.field_customers), r: 1 }],
      r.outages, (o) => outageDetail(o.outage_id), { max: 480 }));
  if (kind === "active") return detail("Active outages", `${cnt(active.length, "outage is", "outages are")} active now, and ${cnt(t.restored_recent, "outage was", "outages were")} restored in the last six hours. Select an outage for its detail.`,
    () => table([...outageCols, { l: "Life support off", f: (x) => n0(x.life_support_off), r: 1 }, { l: "Estimated back", f: (x) => (x.etr ? hhmm(x.etr) : "-") },
      { l: "Crew", f: (x) => x.crew_name || "Not yet sent" }, { l: "Repair job", f: (x) => x.job_status_label || "-" }], active, (o) => outageDetail(o.outage_id), { max: 480 }));
  if (kind === "crews") return detail("Crews deployed", `${cnt(t.crews_deployed, "crew is", "crews are")} travelling to or working on the outages, ${n0(t.crews_on_site)} of them on site. Select a crew for its day.`,
    () => table([{ l: "Crew", f: (c) => c.crew_name || c.crew_id }, { l: "Role", k: "persona" }, { l: "Region", k: "region_name" },
      { l: "Status", f: (c) => st(c.status === "on_site" ? "green" : c.status === "travelling" ? "amber" : "", c.status_label) }, { l: "Job", k: "job_id" }],
    r.crews, (c) => crewDrawer(c.crew_id), { max: 480 }));
  if (kind === "off" || kind === "life") {
    const ls = kind === "life";
    const box = h("div", {}, loading("Reading the customers off supply."));
    detail(ls ? "Life support customers off supply" : "Customers off supply",
      ls ? "Life support customers are restored first. Select a customer for their record, their calls and how they are supplied." : "Listed with life support customers first, then by outage. Select a customer for their record, their calls and how they are supplied.", () => box);
    // the list is worked out from the connectivity model (the premises behind each device off supply); the tile counts the outage records,
    // so where the two disagree on which premises a device supplies, the gap is said
    const counted = ls ? r.totals.life_support_off : r.totals.customers_off;
    api(`/api/ops/off-supply${ls ? "?life_support=true" : ""}`).then((d) => box.replaceChildren(h("p", { class: "muted small" },
      `${cnt(d.premises.length, ls ? "life support customer is" : "customer is", ls ? "life support customers are" : "customers are")} behind the devices off supply now, by the network connectivity model.`
      + (d.premises.length !== counted ? ` The outage records count ${n0(counted)}; the two differ where the records and the model disagree on which premises a device supplies.` : "")),
      table([{ l: "Address", f: (p) => p.address || "-" }, { l: "NMI", k: "nmi" }, { l: "Feeder", k: "feeder_id" }, { l: "Outage", k: "outage_id" },
        { l: "Estimated back", f: (p) => (p.etr ? hhmm(p.etr) : "-") }, { l: "Life support", f: (p) => (p.life_support ? "Yes" : "No") },
        { l: "Critical site", f: (p) => p.critical_site || "No" }], d.premises, (p) => openPremises(p.nmi), { max: 480 })))
      .catch((e) => box.replaceChildren(h("p", { class: "muted" }, `The customers could not be read. ${e.message}`)));
    return null;
  }
  const filed = r.outages.filter((o) => o.field_outcome);
  return detail("Field reports on these outages", `${cnt(filed.length, "outage has", "outages have")} a field report, together restoring ${cnt(t.field_customers_reported, "customer", "customers")} as the crews counted them. Select a report for the crew's day and its reports.`,
    () => table([{ l: "Feeder", k: "feeder_id" }, { l: "Outcome", k: "field_outcome_label" }, { l: "Customers restored", f: (x) => n0(x.field_customers), r: 1 },
      { l: "Crew", f: (x) => x.crew_name || "-" }, { l: "Reported", f: (x) => (x.field_reported_at ? when(x.field_reported_at) : "-") }, { l: "Cause", k: "cause_label" }],
    filed, (x) => { const crew = x.field_crew_id || x.crew_id; if (crew) crewDrawer(crew); }, { max: 480 }));
}

function restorationPanel(r) {
  const t = r.totals;
  const share = pctOf(t.customers_restored, t.customers_total);
  const open = (kind) => () => tileDetail(kind, r);
  const click = (el, kind) => { el.classList.add("click"); el.addEventListener("click", open(kind)); return el; };
  const outageRows = table([
    { l: "Feeder", f: (x) => h("span", {}, h("b", {}, x.feeder_id), h("div", { class: "muted small" }, x.feeder_name || "")) },
    { l: "Device", f: (x) => h("span", {}, x.device_id, h("div", { class: "muted small" }, x.device_label || "")) },
    { l: "Status", f: (x) => st(x.status === "active" ? "red" : "green", x.status_label) },
    { l: "Started", f: (x) => h("span", {}, when(x.start_time), h("div", { class: "muted small" }, x.status === "active" ? `${offFor(x)} off` : `${offFor(x)} to restore`)) },
    { l: "Restored of total", f: (x) => `${n0(x.customers_restored)} of ${n0(x.customers_initial)}`, r: 1 },
    { l: "Still off", f: (x) => n0(x.customers_off), r: 1 }, { l: "Life support off", f: (x) => n0(x.life_support_off), r: 1 },
    { l: "Automatic (FLISR)", f: (x) => n0(x.flisr_customers), r: 1 },
    { l: "Cause", k: "cause_label" }, { l: "Latest stage", f: (x) => x.stage_label || "-" },
    { l: "Estimated back", f: (x) => (x.status === "active" && x.etr ? hhmm(x.etr) : "-") },
    { l: "Crew", f: (x) => x.crew_name || "Not yet sent" },
    { l: "Repair job", f: (x) => x.job_status_label || "-" },
    { l: "Field report", f: (x) => (x.field_outcome_label ? `${x.field_outcome_label}, ${n0(x.field_customers)} restored` : "None yet") },
  ], r.outages, (o) => outageDetail(o.outage_id), { max: 360 });
  const crewRows = table([
    { l: "Crew", f: (c) => h("span", {}, h("b", {}, c.crew_name || c.crew_id), h("div", { class: "muted small" }, c.crew_id)) }, { l: "Role", k: "persona" },
    { l: "Employment", f: (c) => c.employment_type || "-" }, { l: "Region", k: "region_name" }, { l: "Crew size", f: (c) => n0(c.crew_size), r: 1 },
    { l: "Vehicle", f: (c) => c.vehicle_type_label || "-" },
    { l: "Status", f: (c) => st(c.status === "on_site" ? "green" : c.status === "travelling" ? "amber" : "", c.status_label) },
    { l: "Since", f: (c) => (c.since ? when(c.since) : "-") }, { l: "Working on", f: (c) => c.working_on || "Other work" }, { l: "Job", k: "job_id" },
  ], r.crews, (c) => crewDrawer(c.crew_id), { max: 300 });
  const restored = h("div", { class: "tile ops-wide" }, h("div", { class: "k" }, "Customers restored"),
    h("div", { class: "v num" }, `${n0(t.customers_restored)} / ${n0(t.customers_total)}`), bar(share), h("div", { class: "s" }, `${share}% back on supply.`));
  return card("Restoration in progress", "Customers restored out of the total, updated every 15 seconds from Lakebase. Select a figure for what is behind it.",
    h("div", { class: "ops-tiles" },
      click(restored, "restored"),
      click(tile("Active outages", n0(t.active_outages), `${cnt(t.restored_recent, "outage", "outages")} restored in the last six hours.`), "active"),
      click(tile("Crews deployed", n0(t.crews_deployed), `${cnt(t.crews_on_site, "crew", "crews")} on site.`), "crews"),
      click(tile("Customers off supply", n0(t.customers_off), "Customers still without power."), "off"),
      click(tile("Life support still off", n0(t.life_support_off), "Life support customers still without power."), "life"),
      click(tile("Field reports, customers", n0(t.field_customers_reported), "Customers restored, as the crews counted them."), "reports")),
    h("h4", { class: "ops-h4" }, "Outages"), h("p", { class: "muted small" }, "Select an outage for everything recorded on it."), outageRows,
    h("h4", { class: "ops-h4" }, "Crew deployment"), h("p", { class: "muted small" }, "Select a crew for its day."), crewRows);
}

// ---------------------------------------------------------------- the timeline
function livePanel(v) {
  const open = (e) => (e.kind === "switch" ? openAsset(e.ref) : openPremises(e.ref));
  return card("Live switching and events", "The latest meter alarms, customer calls and switch positions from the live streams in Lakebase. Each call is checked against the network. Select a row for the customer's record or the device.",
    h("div", { class: "ops-live" },
      h("div", {}, h("h4", { class: "ops-h4" }, "Timeline"),
        table([{ l: "Time", f: (e) => when(e.ts), r: 1 }, { l: "Event", k: "kind_label" },
          { l: "Customer or device", f: (e) => h("span", {}, e.address || e.ref, e.address ? h("div", { class: "muted small" }, `NMI ${e.ref}${e.life_support ? ", life support" : ""}`) : null) },
          { l: "Feeder", f: (e) => e.feeder_id || "-" }, { l: "Reported", k: "detail" },
          { l: "Network check", f: (e) => (e.check ? h("span", {}, st(CHECK_TONE[e.check], e.check_label), h("div", { class: "muted small" }, e.check_note)) : "-") }],
        v.timeline || [], open, { max: 520 }))));
}

// ---------------------------------------------------------------- resources and damage
function learnPanels(L) {
  const d = L.damage;
  const crews = table([{ l: "Crew", f: (c) => c.crew_name || c.crew_id }, { l: "Role", k: "persona" }, { l: "Region", k: "region" },
    { l: "Reports", k: "reports", r: 1 }, { l: "Fixed", f: (c) => `${n0(c.fixed)} of ${n0(c.reports)}`, r: 1 }, { l: "Follow-up", k: "follow_up", r: 1 },
    { l: "Customers restored", k: "customers_restored", r: 1 }, { l: "Average arrival", f: (c) => minutes(c.avg_arrival_min), r: 1 }],
    L.resources.by_crew, (c) => crewDrawer(c.crew_id), { max: 300 });
  const damageTable = table([{ l: "Cause", k: "cause" }, { l: "Outages", k: "events", r: 1 }, { l: "Customers", f: (x) => n0(x.customers), r: 1 },
    { l: "Customer minutes", f: (x) => n0(x.customer_minutes), r: 1 }], d.by_cause, null, { max: 300 });
  const chartEl = chartBox("short");
  requestAnimationFrame(() => {
    const causes = d.by_cause.slice(0, 8).reverse();
    if (!causes.length) return;
    chart(chartEl, { grid: { left: 150, right: 24, top: 10, bottom: 24 }, xAxis: { type: "value", axisLabel: { color: "#5f7185" } },
      yAxis: { type: "category", data: causes.map((c) => c.cause), axisLabel: { color: "#5f7185" } },
      series: [{ type: "bar", data: causes.map((c) => Math.round(c.customer_minutes || 0)), name: "Customer minutes" }] });
  });
  return h("div", {},
    card("Resource performance by crew", "Field reports filed, with outcomes and arrival time measured against when the job was created. Select a crew for its day.", crews),
    card("Damage insights by cause", `Sustained outages in the 12 months to ${d.since}.`, chartEl, damageTable));
}

export async function operations() {
  const view = document.querySelector("#view");
  let timer = null;
  const stop = () => clearInterval(timer);
  window.addEventListener("hashchange", stop, { once: true });
  const head = h("div", { class: "screen-h" }, h("h1", {}, "Restoration and switching"),
    h("span", { class: "q" }, "Restoration in progress, the live events on the network, and how the crews are performing."));

  view.innerHTML = "";
  view.append(loading("Reading the restoration board."));
  let rest, live, learn;
  try {
    [rest, live, learn] = await Promise.all([api("/api/ops/restoration"), api("/api/ops/live"), api("/api/ops/learn")]);
  } catch (e) {
    view.innerHTML = "";
    view.append(h("div", { class: "card" }, h("h3", {}, "Operations could not load"), h("p", { class: "muted" }, e.message)));
    return;
  }
  const learnNode = learnPanels(learn);   // built once: the damage chart does not redraw every 15 seconds
  const paint = (r, v) => {
    view.innerHTML = "";
    view.append(head, restorationPanel(r), livePanel(v), learnNode);
  };
  paint(rest, live);
  timer = setInterval(async () => {
    if (document.hidden || location.hash.split("/")[1] !== "ops") return stop();
    if (document.querySelector(".modal-bg")) return;   // a detail window is open: the board waits so it does not move under it
    try {
      const [r, v] = await Promise.all([api("/api/ops/restoration"), api("/api/ops/live")]);
      paint(r, v);
    } catch (e) {
      toast(`Operations could not refresh: ${e.message}`, "red");
    }
  }, POLL_MS);
}
