// Asset Atlas, Electricity Distribution: shell, router, Command Centre and Situations.
import { api, post, h, $, n0, n1, n2, pct, ago, hhmm, day, title, tile, card, table, chart, chartBox, axis, disposeCharts, mapIn,
  toast, modal, loading } from "./ui.js";
import { behindButton, setBehind } from "./behind.js";
import * as S2 from "./screens.js";
import * as RM from "./rm.js";
import "./infotips.js";
import { impactAnalysis } from "./impact.js";
import { operations } from "./ops.js";
import { plan, leavePlan } from "./plan.js";
import { customers } from "./customers.js";
import { vegetation, leaveVegWizard } from "./vegetation.js";
import { assetManagement } from "./assets.js";
import { fieldReportsCard } from "./fieldreports.js";

const view = $("#view");
let timers = [];
const every = (ms, fn) => timers.push(setInterval(fn, ms));
const TYPE_ICON = { storm: "\u{1F32C}", fire: "\u{1F525}", trip: "⚡", heat: "\u{1F321}", min_load: "☀", vegetation: "\u{1F333}",
  drone_defect: "\u{1F6F8}", fatigue: "⏱", records: "\u{1F5C2}", overdue: "⌛" };
const ctx = { meta: null };
const cnt = (n, one, many = `${one}s`) => `${n0(n)} ${+n === 1 ? one : many}`;

// ---------------------------------------------------------------- router
const routes = {
  cc: () => commandCentre(),
  situations: (id) => situationsScreen(id),
  network: (stop) => S2.network(stop || "zone_substations"),
  map: () => S2.mapScreen(),
  ops: () => operations(),
  plan: (tab, id) => plan(tab, id),   // no step in the address: the programme opens at the step in hand
  res: (tab) => RM.screen(tab || "control"),
  customers: (tab, sub) => customers(tab || "being-connected", sub),
  veg: (tab) => vegetation(tab || "capture"),
  assets: (tab) => assetManagement(tab || "review"),
  scene: (kind, id) => S2.scene3d(kind, id),
};

// One screen renders at a time. A hash change mid-render clears the view at once (an in-flight 3D build sees its stage detached
// and stops) and the latest hash renders as soon as the current one finishes, so a slow screen can never paint over a newer one.
let inflight = null, again = false;
function route() {
  if (inflight) { again = true; view.innerHTML = ""; view.append(loading()); return inflight; }
  inflight = (async () => { do { again = false; await render(); } while (again); inflight = null; })();
  return inflight;
}
// Operations holds five screens as tabs: restoration and switching, the network (with its map and site views), field staff, vegetation
// and asset management. Each keeps its own address and its own tabs; the Operations rail icon stays lit on all of them.
const OPS_TABS = [["ops", "#/ops", "Restoration and switching", "tab_ops.png?v=1"], ["network", "#/network/zone_substations", "Network", "tab_network.png?v=1"],
  ["res", "#/res/control", "Field staff management", "tab_res.png?v=1"], ["veg", "#/veg/capture", "Vegetation management", "tab_veg.png?v=1"],
  ["assets", "#/assets/review", "Asset management", "tab_assets.png?v=1"]];
const OPS_OF = { map: "network", scene: "network" };   // the routes shown under another screen's tab
function sectionTabs(r) {
  const nav = $("#section"), tab = OPS_OF[r] || r, on = OPS_TABS.some(([k]) => k === tab);
  nav.classList.toggle("on", on);
  nav.replaceChildren(...(on ? OPS_TABS.map(([k, href, l, icon]) => h("a", { href, class: k === tab ? "on" : "" }, h("img", { src: `/static/icons/${icon}`, alt: "" }), l)) : []));
  return on ? "ops" : r;
}
async function render() {
  timers.forEach(clearInterval); timers = [];
  disposeCharts();
  const [, r, a, b] = (location.hash || "#/cc").split("/");
  const lit = sectionTabs(r);
  document.querySelectorAll("#rail a").forEach((x) => x.classList.toggle("on", x.dataset.r === lit));
  const on = document.querySelector("#rail a.on"), fr = $("#frame");
  if (on && fr) { const a = on.getBoundingClientRect(), f = fr.getBoundingClientRect(); fr.style.setProperty("--tail-y", `${a.top + a.height / 2 - f.top}px`); }
  document.body.classList.remove("sit-focus", "rail-peek");
  document.querySelectorAll(".modal-bg").forEach((m) => m.remove());   // a dialog belongs to the screen that opened it
  view.innerHTML = "";
  view.append(loading());
  try {
    await (routes[r] || routes.cc)(decodeURIComponent(a || ""), decodeURIComponent(b || ""));
  } catch (e) {
    view.innerHTML = "";
    view.append(h("div", { class: "card" }, h("h3", {}, "Something went wrong"), h("p", { class: "muted" }, e.message)));
    console.error(e);
  }
}
// moving to another screen closes the side drawer (a redraw of the same screen, such as the Command Centre's refresh, keeps it open)
window.addEventListener("hashchange", (e) => {
  const from = new URL(e.oldURL).hash, to = location.hash;
  // leaving a plan step with work not yet accepted, or a vegetation work package not yet issued, asks first: the address goes back until
  // the user has answered
  const proceed = () => { location.hash = to; };
  if (leavePlan(from, to, proceed) || leaveVegWizard(from, to, proceed)) { history.replaceState(null, "", from); return; }
  $("#drawer").classList.remove("open"); route();
});
export const go = (hash) => { location.hash = hash; };
export { every };

// ---------------------------------------------------------------- top bar
async function topbar() {
  try {
    const m = await api("/api/meta");
    ctx.meta = m;
    paintLive(m.engine);
  } catch (e) { /* keep last state */ }
  try {
    const s = await api("/api/situations");
    const open = s.length;   // the server lists the open alerts, the same ones the Alerts list and the Command Centre count
    $("#sitBadge").hidden = !open; $("#sitBadge").textContent = open;
    // new since the last poll: mark them seen first (two overlapping polls must not both toast), stay quiet on the first load
    const fresh = s.filter((x) => !seen.has(x.situation_id));
    fresh.forEach((x) => seen.add(x.situation_id));
    if (primed) fresh.slice(0, 3).forEach((x) => toast(`${TYPE_ICON[x.type] || ""} ${x.title}`, x.severity === "critical" ? "red" : ""));
    primed = true;
  } catch (e) {}
}
const seen = new Set();
let primed = false;


// ---------------------------------------------------------------- Command Centre
// The comic exclamation mark that flags a stop needing attention (orange with a dark cast shadow, gently bouncing).
const BANG = `<svg viewBox="0 0 40 80" aria-hidden="true"><g transform="translate(3 3)"><path d="M12 2 L30 4 L22 50 L14 50 Z" fill="#1f2a30"/>` +
  `<ellipse cx="17" cy="64" rx="7.5" ry="6.5" fill="#1f2a30"/></g><path d="M12 2 L30 4 L22 50 L14 50 Z" fill="#e2683c" stroke="#1f2a30" stroke-width="2.4" stroke-linejoin="round"/>` +
  `<ellipse cx="17" cy="64" rx="7.5" ry="6.5" fill="#e2683c" stroke="#1f2a30" stroke-width="2.4"/><path d="M15.5 8 L19 8.6 L16.8 30" stroke="#f6a37f" stroke-width="2" fill="none" stroke-linecap="round"/></svg>`;
const big$cc = (v) => v < 0 ? `-${big$cc(-v)}` : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `$${Math.round(v / 1e3)}k` : `$${n0(v)}`;
async function commandCentre() {
  const d = await api("/api/cc");
  view.innerHTML = "";
  const rel = d.reliability || {};
  const saidi = rel.saidi_ex_med, tgt = rel.saidi_target_to_date;
  const fd = d.fire_danger || {};
  const onSupply = (d.spine.find((s) => s.stop === "customers_der")?.total ?? 0) - d.customers_off;   // every connection point, from the spine
  const diff = (tgt || 0) - (saidi || 0);
  const risk = d.at_risk || {}, late = d.long_outages || {};
  const tile = (href, ...kids) => h("div", href ? { class: "cc-go", onclick: () => go(href) } : {}, ...kids);
  view.append(h("div", { class: "hero" },
    tile("#/ops", h("div", { class: "kh", style: d.customers_off ? "color:#d2462f" : "" }, d.customers_off ? "Customers off supply now" : "Customers on supply"),
      h("div", { class: "bigv num" }, d.customers_off ? n0(d.customers_off) : n0(onSupply)),
      h("div", { class: "lbl" }, d.customers_off ? `${n0(d.life_support_off)} of them are on life support.` : "No customers are off supply right now.")),
    tile("#/ops", h("div", { class: "kh", style: diff < 0 ? "color:#d2462f" : "" }, "Reliability against target"),
      h("div", { class: "midv num", style: `color:${diff < 0 ? "#d2462f" : "#2e9c6a"}` }, `${diff < 0 ? "▲" : "▼"} ${n1(Math.abs(diff))} min ${diff < 0 ? "over" : "under"}`),
      h("div", { class: "lbl" }, "Average minutes off supply per customer (SAIDI).")),
    tile("#/situations", h("div", { class: "kh", style: "color:#d2462f" }, "Revenue at risk"), h("div", { class: "midv num" }, big$cc(risk.revenue || 0)),
      h("div", { class: "lbl" }, `Across ${n0(risk.situations)} open ${risk.situations === 1 ? "alert" : "alerts"}.`)),
    tile("#/situations", h("div", { class: "kh", style: late.customers ? "color:#d2462f" : "" }, "Off supply over 12 hours"),
      h("div", { class: "midv num", style: late.customers ? "color:#d2462f" : "" }, n0(late.customers)),
      h("div", { class: "lbl" }, late.customers ? `The guaranteed service level is breached. ${n0(late.life_support)} of them are on life support.` : "The guaranteed service level is not breached.")),
    h("div", { class: "ic", onclick: () => go("#/map") }, h("img", { src: "/static/icons/network.png?v=teal" }), "Hazard impact and notifications"),
    h("div", { class: "ic", onclick: impactAnalysis }, h("img", { src: "/static/icons/plan_invest.png?v=teal" }), "Network impact")));
  // The spine: one stop per part of the network, flagged when something there needs attention.
  const spine = h("div", { class: "spine" });
  const icons = { bulk_supply: "stop_bulk_supply", transmission_substations: "stop_transmission_substation", zone_substations: "stop_zone_substation",
    hv_feeders: "stop_hv_feeders", distribution_lv: "stop_distribution_lv", customers_der: "stop_customers_der" };
  d.spine.forEach((s, i) => {
    if (i) spine.append(h("div", { class: "link" }));
    const cls = s.red + (s.live_red || 0) ? "red" : s.amber ? "amber" : "";
    spine.append(h("div", { class: `stop ${cls}`, onclick: () => go(`#/network/${s.stop}`) }, cls ? h("span", { class: "bang", html: BANG }) : null,
      h("img", { src: `/static/icons/${icons[s.stop]}.png?v=teal` }), h("div", { class: "n" }, s.stop_label)));
  });
  view.append(spine);
  const sev = d.situations;
  view.append(h("div", { class: "statrow" },
    tile("#/situations", h("div", { class: "v num", style: (sev.critical || sev.high) ? "color:#b23321" : "" }, n0(Object.values(sev).reduce((a, b) => a + b, 0))),
      h("div", { class: "k" }, "Open situations"), h("div", { class: "s" }, `${n0(sev.critical || 0)} critical and ${n0(sev.high || 0)} high-severity alerts.`)),
    tile("#/res/control", h("div", { class: "v num" }, n0(d.crews.deployed)), h("div", { class: "k" }, "Crews out"), h("div", { class: "s" }, `${n0(d.crews.available)} ${d.crews.available === 1 ? "crew" : "crews"} available. ${d.crews.stood_down ? `${n0(d.crews.stood_down)} stood down.` : "None stood down."}`)),
    tile("#/map", h("div", { class: "v", style: ["EXTREME", "CATASTROPHIC"].includes(fd.today) ? "color:#b23321" : "" }, title(fd.today || "-")),
      h("div", { class: "k" }, "Fire danger today"), h("div", { class: "s" }, fd.district ? `The NSW RFS rating for the ${fd.district} district.` : "The NSW RFS rating.")),
    tile("#/ops", h("div", { class: "v num" }, n0(rel.major_event_days)), h("div", { class: "k" }, "Major event days"), h("div", { class: "s" }, "This regulatory year (2.5 beta method)."))));
  // Reliability against target this regulatory year.
  const rc = chartBox("tall");
  view.append(h("div", { class: "card" },
    h("h3", {}, "Customer minutes off supply against target", h("span", { class: "sub" }, "Cumulative SAIDI this regulatory year, with major event days excluded.")), rc));
  let cum = 0, cum2 = 0;
  const days = d.reliability_days, prev = d.reliability_prev;
  const actual = days.map((x) => { if (!x.is_major_event_day) cum += x.saidi; return [x.day, +cum.toFixed(2)]; });
  const target = days.map((x, i) => [x.day, +(92 * (i + 1) / 365).toFixed(2)]);
  const last = prev.slice(0, days.length).map((x, i) => { if (!x.is_major_event_day) cum2 += x.saidi; return [days[i] ? days[i].day : x.day, +cum2.toFixed(2)]; });
  chart(rc, { legend: { right: 10, top: 0, textStyle: { color: "#5f7185" } }, xAxis: axis({ type: "time" }), yAxis: axis({ type: "value", name: "min" }),
    series: [{ type: "line", name: "actual", data: actual, showSymbol: false, lineStyle: { width: 2.4, color: "#0c5a66" }, color: "#0c5a66" },
      { type: "line", name: "target", data: target, showSymbol: false, lineStyle: { type: "dashed", color: "#5f7185" }, color: "#5f7185" },
      { type: "line", name: "last year", data: last, showSymbol: false, lineStyle: { type: "dotted", color: "#dd4b1a" }, color: "#dd4b1a" }] });
  const open = Object.values(sev).reduce((a, b) => a + b, 0);
  every(15000, async () => { if (location.hash.startsWith("#/cc") || location.hash === "") { const d2 = await api("/api/cc"); if (d2.customers_off !== d.customers_off || Object.values(d2.situations).reduce((a, b) => a + b, 0) !== open) route(); } });
}


// ---------------------------------------------------------------- Situations
// Situations: every open situation as a slim ranked row with an impact bar, most urgent first, one folded group per kind (the medium ones in a group of their own). Opening a situation
// slides the menu away, turns the list into an out-of-focus column on the left and shows the situation in a speech-bubble panel.
const SEVW = { critical: 3, high: 2, medium: 1 };
const big$ = (v) => v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `$${Math.round(v / 1e3)}k` : `$${n0(v)}`;
// every kind of situation is a group in the list, folded at first (opening a situation unfolds its group): the group's name for one and for many
const GROUP_LABEL = { trip: ["outage in progress", "outages in progress"], fire: ["fire or hotspot near the network", "fires and hotspots near the network"],
  storm: ["strong wind forecast", "strong wind forecasts"], fatigue: ["crew fatigue alert", "crew fatigue alerts"], heat: ["transformer alert", "transformer alerts"],
  drone_defect: ["drone-spotted defect", "drone-spotted defects"], vegetation: ["vegetation alert", "vegetation alerts"],
  min_load: ["minimum load alert", "minimum load alerts"], records: ["records mismatch", "records mismatches"],
  overdue: ["crew overdue on an emergency job", "crews overdue on emergency jobs"] };
const groupName = (s, n) => (GROUP_LABEL[s.type] || [(s.type_label || s.type).toLowerCase(), (s.type_label || s.type).toLowerCase()])[n === 1 ? 0 : 1];
function impactText(s) {   // what the situation means in a few words: who is off, who is at risk, or what is wrong
  const p = s.payload || {};
  if (s.type === "trip") return `${cnt(s.customers_impacted, "customer", "customers")} off supply`;
  if (s.customers_impacted) return `${cnt(s.customers_impacted, "customer", "customers")} at risk`;
  if (s.type === "heat") return `hydrogen up ${n0(p.h2_rise)} ppm, no outage yet`;
  if (s.type === "drone_defect") return `${title((p.defect || "defect").replace("_", " "))} on ${p.pole_id || "a pole"}`;
  if (s.type === "vegetation") return `${cnt(p.red, "span", "spans")} to cut now`;
  return s.type_label;
}
function rank(list) {
  return [...list].sort((a, b) => (SEVW[b.severity] - SEVW[a.severity]) || ((b.type === "trip") - (a.type === "trip")) ||
    (b.revenue_at_risk - a.revenue_at_risk) || (b.customers_impacted - a.customers_impacted));
}
async function situationsScreen(id) {
  const list = rank(await api("/api/situations"));
  view.innerHTML = "";
  const total = list.reduce((a, s) => a + (s.revenue_at_risk || 0), 0), cust = list.reduce((a, s) => a + (s.customers_impacted || 0), 0);
  const ls = list.reduce((a, s) => a + (s.life_support_impacted || 0), 0);
  view.append(h("div", { class: "screen-h sq-h" }, h("h1", {}, "Alerts"),
    h("div", { class: "right sq-sum" }, h("span", {}, h("b", {}, list.length), list.length === 1 ? " open alert" : " open alerts"), h("span", {}, h("b", {}, n0(cust)), " customers impacted"),
      h("span", {}, h("b", {}, n0(ls)), " on life support"), h("span", {}, h("b", {}, big$(total)), " revenue at risk"))));
  if (!list.length) { view.append(card("No open alerts", "", h("p", { class: "muted" }, "No alert is open. Switch on live data ingestion, at the top right of the screen, to stream the network's live data."))); return; }
  const wrap = h("div", { class: "sq" }), main = h("div", { class: "sq-main" });
  const bubble = h("div", { class: "sitx-bubble" }), body = h("div", { class: "sitx-body" });
  bubble.append(h("button", { class: "sitx-x", title: "Back to all situations", onclick: () => unfocus() }, "×"), body);
  wrap.append(main, bubble); view.append(wrap);
  // the queue: one group per kind, most urgent first, every group folded at first; the medium ones in one group of their own
  const queue = h("div", { class: "sq-queue" });
  main.append(h("div", { class: "sq-qh" }, h("span", {}, "Most urgent first."), h("span", { class: "muted small" }, "revenue at risk")), queue);
  const maxRisk = Math.max(1, ...list.map((s) => s.revenue_at_risk || 0), ...Object.values(list.reduce((g, s) => { const k = s.type; g[k] = (g[k] || 0) + (s.revenue_at_risk || 0); return g; }, {})));
  const bar = (v, tip) => h("div", { class: "sq-bar has-tip", "data-info": tip }, h("i", { style: `width:${Math.max(v ? 3 : 0, Math.round(100 * v / maxRisk))}%` }));
  const money$ = (v, tip) => h("span", { class: "rv num has-tip", "data-info": tip }, v ? big$(v) : "");
  const row = (s, child = false) => h("div", { class: `sq-row sev-${s.severity}${child ? " child" : ""}`, "data-sid": s.situation_id, onclick: (e) => { e.stopPropagation(); focus(s.situation_id); } },
    h("span", { class: "ico" }, TYPE_ICON[s.type] || "•"), h("span", { class: "tt" }, s.title, h("span", { class: `stx ${s.status} has-tip`, "data-info": s.status_note }, s.status_label)),
    h("span", { class: "imp" }, impactText(s)), h("span", { class: "ls" }, s.life_support_impacted ? `${n0(s.life_support_impacted)} on life support` : ""),
    bar(s.revenue_at_risk || 0, s.risk_basis), money$(s.revenue_at_risk, s.risk_basis), h("span", { class: "ag" }, ago(s.updated_at)));
  const group = (label, items, open, sev) => {
    const sum = (k) => items.reduce((a, s) => a + (s[k] || 0), 0);
    const kids = h("div", { class: "sq-kids" + (open ? " open" : "") }, items.map((s) => row(s, true)));
    const head = h("div", { class: `sq-row group sev-${sev}`, onclick: () => { kids.classList.toggle("open"); head.classList.toggle("open"); } },
      h("span", { class: "chev" }, "›"), h("span", { class: "tt" }, `${items.length} ${label}`),
      h("span", { class: "imp" }, sum("customers_impacted") ? `${cnt(sum("customers_impacted"), "customer", "customers")} ${items.every((s) => s.type === "trip") ? "off supply" : "at risk"}` : ""),
      h("span", { class: "ls" }, sum("life_support_impacted") ? `${n0(sum("life_support_impacted"))} on life support` : ""),
      ...(() => { const tip = sum("revenue_at_risk") ? `The revenue at risk of the ${items.length} situation${items.length === 1 ? "" : "s"} in this group, added together. Each one is customers off or at risk × hours × the cost of a customer hour (reliability incentive plus network revenue on energy not delivered). Open the group and hover over a row to see its own sum.` : "No revenue at risk: these situations take no customers off supply.";
        return [bar(sum("revenue_at_risk"), tip), money$(sum("revenue_at_risk"), tip)]; })(), h("span", { class: "ag" }, ""));
    if (open) head.classList.add("open");
    return [head, kids];
  };
  const urgent = list.filter((s) => s.severity !== "medium"), medium = list.filter((s) => s.severity === "medium");
  const byType = {}; for (const s of urgent) (byType[s.type] = byType[s.type] || []).push(s);
  const done = new Set();
  for (const s of urgent) {
    if (done.has(s.type)) continue;
    const items = byType[s.type];
    done.add(s.type);
    queue.append(...group(groupName(s, items.length), items, false, items[0].severity));
  }
  if (medium.length) queue.append(...group(`Medium severity: ${[...new Set(medium.map((s) => (s.type_label || s.type).toLowerCase()))].join(", ")}`, medium, false, "medium"));
  // focus: the menu goes, the list becomes an out-of-focus column, the situation opens in the bubble
  const tail = () => {
    const on = main.querySelector(".sq-row.on"); if (!on) return;
    const y = on.getBoundingClientRect().top + Math.min(on.offsetHeight, 60) / 2 - bubble.getBoundingClientRect().top;
    bubble.style.setProperty("--tail", `${Math.max(24, Math.min(bubble.clientHeight - 24, y))}px`);
  };
  async function focus(sid) {
    document.body.classList.add("sit-focus"); wrap.classList.add("focus");
    main.querySelectorAll(".sq-row[data-sid]").forEach((r) => r.classList.toggle("on", r.dataset.sid === sid));
    main.querySelectorAll(".sq-row.on").forEach((r) => { const k = r.closest(".sq-kids"); if (k && !k.classList.contains("open")) { k.classList.add("open"); k.previousElementSibling?.classList.add("open"); } });
    history.replaceState(null, "", `#/situations/${encodeURIComponent(sid)}`);
    setTimeout(() => { main.querySelector(".on")?.scrollIntoView({ block: "nearest", behavior: "smooth" }); tail(); }, 380);
    body.scrollTop = 0;
    await situationDetail(sid, body);
    tail();
  }
  function unfocus() {
    document.body.classList.remove("sit-focus", "rail-peek"); wrap.classList.remove("focus");
    main.querySelectorAll(".on").forEach((r) => r.classList.remove("on"));
    body.innerHTML = "";   // nothing of the closed situation stays on the page
    history.replaceState(null, "", "#/situations");
  }
  main.addEventListener("scroll", tail);
  wrap.addEventListener("click", (e) => { if (wrap.classList.contains("focus") && !bubble.contains(e.target) && !e.target.closest(".sq-row")) unfocus(); });
  document.addEventListener("keydown", function esc(e) { if (!wrap.isConnected) return document.removeEventListener("keydown", esc); if (e.key === "Escape") unfocus(); });
  if (id && list.some((s) => s.situation_id === id)) focus(id);
}

// A sent notice can be taken back only for a short window: the button counts down, then closes for good (the server enforces it too).
function undoCountdown(btn, left, onSent) {   // onSent: redraw once the messages have gone (the outbox then reads sent)
  const end = Date.now() + left * 1000;
  let shown = false;   // the first tick runs before the box is on the page; stop only once it has been shown and then left
  const tick = () => {
    const s = Math.ceil((end - Date.now()) / 1000);
    if (btn.isConnected) shown = true; else if (shown) return;
    if (btn.disabled) return;   // clicked: the button now says "Undoing"
    if (s <= 0) { btn.disabled = true; btn.textContent = "Sent"; btn.title = "The messages have gone out"; if (onSent) setTimeout(() => { if (btn.isConnected) onSent(); }, 1200); return; }
    btn.textContent = `Undo (${s}s)`;
    setTimeout(tick, 250);
  };
  tick();
}

async function situationDetail(sid, root) {
  root.innerHTML = "";
  root.append(loading("Reading the twin."));
  let s = await api(`/api/situation/${encodeURIComponent(sid)}`);
  if (!s.recommendations.length && s.status !== "closed") {
    root.innerHTML = ""; root.append(loading("Projecting impact and building the plan."));
    s = await post(`/api/situation/${encodeURIComponent(sid)}/plan`);
  }
  root.innerHTML = "";
  root.append(h("div", { class: "screen-h" }, h("h1", {}, s.title), h("span", { class: `sevx ${s.severity}` }, title(s.severity)),
    h("div", { class: "right" }, h("span", { class: "fresh" }, `Detected ${ago(s.detected_at)}.`))));
  const V = s.view || {};
  const steps = h("div", { class: "steps" });
  const [happen, affects, recommend] = ["Situation", "Impact", "Recommendation"].map((t) => h("div", { class: "step" }, h("div", { class: "h" }, t)));
  steps.append(happen, affects, recommend);
  root.append(steps);
  const renderer = TYPES[s.type] || TYPES.generic;
  renderer(s, V, happen, affects);
  // Recommendations (shared).
  for (const r of s.recommendations) {
    const box = h("div", { class: `rec ${r.status}` }, h("div", { class: "tt" }, r.title), h("div", { class: "dd" }, r.detail));
    if (r.status === "proposed") {
      box.append(h("div", { class: "row" },
        h("button", { class: "btn primary small", onclick: (e) => decide(sid, r, "accept", root, e) }, "Accept"),
        r.kind === "dispatch" ? h("button", { class: "btn small", onclick: () => otherCrew(sid, r, root) }, "Another crew")
          : r.kind === "notify" ? h("button", { class: "btn small", onclick: () => editNotice(sid, s, r, root) }, "Adjust")
          : null,   // only a crew choice or a notice's wording can be changed; elsewhere Adjust would just be Accept
        h("button", { class: "btn small danger", onclick: (e) => decide(sid, r, "reject", root, e) }, "Reject")));
    } else {
      const undo = h("button", { class: "btn small", onclick: async () => {
        undo.disabled = true; undo.textContent = "Undoing";
        try {
          const res = await post(`/api/situation/${encodeURIComponent(sid)}/undo`, { rec_id: r.rec_id });
          toast(`Undone: ${(res.effects || []).join("; ") || r.title}`, "green");
        } catch (e) { toast(e.message, "red"); }
        situationDetail(sid, root);
      } }, "Undo");
      box.append(h("div", { class: "row" }, h("span", { class: "small muted" }, `${title(r.status)} by ${r.decided_by || ""} ${ago(r.decided_at)}${r.reason ? ": " + r.reason : ""}`), undo));
      if (r.undo_left != null) undoCountdown(undo, r.undo_left, r.undo_left > 0 ? () => situationDetail(sid, root) : null);
    }
    recommend.append(box);
  }
  if (!s.recommendations.length) recommend.append(h("p", { class: "muted" }, "No action proposed."));
  // Audit trail and outbox.
  const ev = s.events.map((e) => h("div", { class: "small" }, h("span", { class: "muted" }, `${hhmm(e.ts)} ${e.actor}: `), title(e.action.replace("_", " ")),
    e.detail && e.detail.effects ? h("div", { class: "muted" }, e.detail.effects.join("; ")) : null));
  root.append(h("div", { class: "grid2e", style: "margin-top:16px" }, card("Decision trail", "Each change, who made it and when, held in Lakebase.", ...ev),
    card("Customer notifications", "Drafted by GenAI and queued in the Lakebase outbox. Delivery is simulated.",
      s.outbox.length ? table([{ l: "Audience", k: "audience" }, { l: "Status", k: "status" }, { l: "Messages", k: "n", r: 1 }], s.outbox, null, { noScroll: 1 }) : h("p", { class: "muted" }, "None queued yet."),
      s.outbox[0] ? h("p", { class: "small", style: "margin-top:8px" }, h("b", {}, "SMS: "), s.outbox[0].sample) : null)));
  root.append(h("div", { style: "margin-top:16px" }, fieldReportsCard({ situationId: sid })));   // what the crews reported back
}

async function decide(sid, r, action, root, ev) {
  let reason = null;
  if (action === "reject") { reason = prompt("Why are you rejecting this?"); if (reason === null) return; }
  // answer the click at once: the box's buttons lock and the pressed one says what is happening, until the server replies
  const btn = ev && ev.currentTarget, row = btn && btn.parentElement;
  if (row) { row.querySelectorAll("button").forEach((b) => { b.disabled = true; }); btn.textContent = action === "reject" ? "Rejecting" : "Accepting"; }
  try {
    const res = await post(`/api/situation/${encodeURIComponent(sid)}/decide`, { rec_id: r.rec_id, action, reason });
    toast(`${title(res.status)}: ${(res.effects || []).join("; ") || r.title}`, "green");
  } catch (e) { toast(e.message, "red"); }
  situationDetail(sid, root);
}

// A customer notice adjusted before it goes: the wording and who receives it.
function editNotice(sid, s, r, root) {
  const etr = s.view?.outage?.etr ? ` We expect power back by about ${hhmm(s.view.outage.etr)}.` : "";
  const text = h("textarea", { rows: 5, style: "width:100%" }, r.draft ||
    `Power update: we know about the outage affecting your address (${s.title.replace(/ tripped$/, "")}). Crews are working on it.${etr} If you rely on life support equipment, use your backup plan now.`);
  const who = h("select", {}, [["affected", "Every customer on the affected feeders"], ["life_support", "Life support customers only"]].map(([v, l]) =>
    h("option", { value: v, selected: v === (r.payload.audience === "life_support" ? "life_support" : "affected") ? "" : null }, l)));
  const bg = modal(h("h2", {}, "Adjust the customer notice"), h("p", { class: "muted" }, "Edit the SMS and choose who receives it. It goes to the outbox when you send it."),
    h("div", { class: "kv", style: "align-items:center" }, h("div", { class: "k" }, "Send to"), who, h("div", { class: "k" }, "Message"), text),
    h("div", { class: "row", style: "justify-content:flex-end;margin-top:12px" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn primary", onclick: async () => {
        bg.remove();
        const res = await post(`/api/situation/${encodeURIComponent(sid)}/decide`, { rec_id: r.rec_id, action: "adjust", reason: "Edited the message", message: text.value, audience: who.value });
        toast(`${title(res.status)}: ${(res.effects || []).join("; ")}`, "green"); situationDetail(sid, root);
      } }, "Send")));
}

// A dispatch adjusted to another crew: the nearest qualified crews with room today, from resource management.
async function otherCrew(sid, r, root) {
  const d = await api(`/api/rm/nearest?job_id=${encodeURIComponent(r.payload.job_id)}`);
  const bg = modal(h("h2", {}, "Send another crew"), h("p", { class: "muted" }, "The nearest qualified crews, with the hours each has left today and any guard that stops them."),
    table([{ l: "Crew", k: "crew_name" }, { l: "Persona", k: "persona" }, { l: "Km", k: "km", r: 1 }, { l: "Minutes", k: "eta_min", r: 1 }, { l: "Now", k: "status" },
      { l: "", f: (c) => c.blocked ? h("span", { class: "muted small" }, c.blocked) : h("button", { class: "btn small primary", onclick: async () => {
        bg.remove();
        const res = await post(`/api/situation/${encodeURIComponent(sid)}/decide`, { rec_id: r.rec_id, action: "adjust", reason: `Dispatched ${c.crew_name} instead`, crew_id: c.crew_id });
        toast(`${title(res.status)}: ${(res.effects || []).join("; ")}`, "green"); situationDetail(sid, root);
      } }, "Send this crew") }], d.crews, null, { noScroll: 1 }),
    d.crews.length ? null : h("p", { class: "muted" }, "No qualified crew is free in the region right now."));
}

const kv = (pairs) => h("div", { class: "kv" }, ...pairs.flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v ?? "-")]));
const bandColour = { "0-1 km": "#d2462f", "1-3 km": "#dd4b1a", "3-5 km": "#e3b341", "5-10 km": "#8aa0b8" };

const TYPES = {
  storm(s, V, happen, affects) {
    const days = V.days || [];
    const c1 = chartBox("short");
    happen.append(h("div", { class: "timeline" }, ["Predict", "Mobilise", "Restore", "Recover"].map((p, i) => h("div", { class: `ph ${i === 0 ? "on" : ""}` }, p))),
      h("p", { class: "small muted" }, `Forecast peak gust of ${n0(Math.max(...days.map((d) => d.peak_gust), 0))} km/h on ${day(V.peak_day)}.`), c1);
    chart(c1, { grid: { left: 40, right: 40, top: 20, bottom: 26 }, xAxis: axis({ type: "category", data: (V.windows || []).map((w) => `${day(w.window_start)} ${hhmm(w.window_start)}`) }),
      yAxis: [axis({ type: "value", name: "km/h" }), axis({ type: "value", name: "faults", splitLine: { show: false } })],
      series: [{ type: "line", name: "Gust", data: (V.windows || []).map((w) => w.gust), color: "#2f7dc4", showSymbol: false, smooth: true },
        { type: "bar", name: "Expected faults", yAxisIndex: 1, data: (V.windows || []).map((w) => w.faults), color: "#c98a1e" }] });
    const mapEl = h("div", { class: "map", style: "height:230px;margin-top:8px" });
    happen.append(mapEl);
    setTimeout(() => {
      const m = mapIn(mapEl, [-33.6, 150.8], 8);
      for (const c of V.cells || []) {
        const g = typeof c.geo === "string" ? JSON.parse(c.geo) : c.geo;
        L.geoJSON(g, { style: { color: "#0000", fillColor: c.gust > 90 ? "#d2462f" : c.gust > 70 ? "#c98a1e" : c.gust > 50 ? "#2f7dc4" : "#2e9c6a", fillOpacity: .38 } })
          .bindTooltip(`H3 cell, gust ${c.gust} km/h, rain ${c.rain} mm`).addTo(m);
      }
    }, 50);
    const tf = V.top_feeders || [];
    affects.append(h("div", { class: "big num" }, n0(days.reduce((a, d) => a + d.customers, 0))), h("div", { class: "muted small" }, "Customers expected to be off supply over 72 hours (P50 estimate)."),
      h("div", { class: "sep" }),
      table([{ l: "Feeder", k: "feeder_id" }, { l: "Faults", f: (r) => n2(r.faults_p50), r: 1 }, { l: "Life support", k: "life_support", r: 1 },
        { l: "Why", f: (r) => (r.drivers || []).map((d) => d.label).join(", ") }], tf.slice(0, 8), (r) => S2.openAsset(r.feeder_id), { max: 260 }),
      h("p", { class: "small muted" }, `Model ${V.model_version || ""} (MLflow, Unity Catalog). Drivers are measured against a calm-weather baseline.`));
  },
  fire(s, V, happen, affects) {
    const f = V.fire || {};
    const mapEl = h("div", { class: "map", style: "height:300px" });
    happen.append(kv([["Source", f.source], ["Alert level", f.alert_level], ["Status", f.status], ["Type", f.incident_type], ["Size", f.size], ["Updated", f.updated || ago(f.captured_at)]]), mapEl,
      h("p", { class: "attr" }, `Real public data from ${f.source || "the public fire feeds"}, CC BY 4.0.`));
    setTimeout(async () => {
      const m = mapIn(mapEl, [f.lat || -33.7, f.lon || 150.4], 12);
      if (f.lat == null) { await S2.networkLayer(m, (V.feeders || []).map((x) => x.feeder_id)); return; }   // the fire has left the live feed
      if (f.perimeter_geojson) L.geoJSON(JSON.parse(f.perimeter_geojson), { style: { color: "#d2462f", fillOpacity: .3 } }).addTo(m);
      if (f.radius_m) L.circle([f.lat, f.lon], { radius: f.radius_m, color: "#d2462f", fillOpacity: .3 }).addTo(m);
      [1000, 3000, 5000].forEach((r, i) => L.circle([f.lat, f.lon], { radius: r + (f.radius_m || 0), color: ["#d2462f", "#dd4b1a", "#e3b341"][i], weight: 1, fill: false, dashArray: "4" }).addTo(m));
      L.circleMarker([f.lat, f.lon], { radius: 7, color: "#fff", fillColor: "#d2462f", fillOpacity: 1 }).addTo(m);
      await S2.networkLayer(m, (V.feeders || []).map((x) => x.feeder_id));
    }, 50);
    affects.append(h("div", { class: "big num" }, n0((V.feeders || []).reduce((a, x) => a + (x.customers || 0), 0))), h("div", { class: "muted small" }, "Customers on feeders with spans within 3 km."),
      table([{ l: "Feeder", k: "feeder_id" }, { l: "Band", f: (r) => h("span", { style: `color:${bandColour[r.band]}` }, r.band) }, { l: "Spans", k: "spans", r: 1 },
        { l: "Nearest m", k: "nearest_m", r: 1 }], V.bands || [], null, { max: 220 }),
      null);
    if (V.spatial_sql) setBehind({ title: "The spatial query", what: "The spatial query is computed live in Lakebase with PostGIS, using geography distances in metres (ST_DWithin and ST_Distance) over the GIS span shapes synced from gold. The connectivity trace then finds the customers.",
      sources: [["Feed", f.source], ["Lakebase", "serving.gld_asset (GIS and CIM, synced from gold)"]], sql: V.spatial_sql });
  },
  trip(s, V, happen, affects) {
    const o = V.outage || {};
    const stages = (o.stages || []).map((x) => h("div", { class: "small trip-step" }, h("b", {}, hhmm(x.ts)), " ", x.label, x.device ? ` ${x.device}` : "",
      x.customers ? `, ${n0(x.customers)} customers` : "", x.note ? h("div", { class: "muted" }, x.note) : null));
    happen.append(kv([["Feeder", o.feeder_id], ["Device", o.device_id], ["Started", hhmm(o.start_time)], ["Cause (suspected)", o.cause_label], ["ETR", hhmm(o.etr)]]), h("div", { class: "sep" }), ...stages,
      h("p", { class: "small muted" }, `${cnt((V.meter_alarms || {}).POWER_OUTAGE, "meter outage alarm", "meter outage alarms")} and ${cnt(V.calls, "call", "calls")} in the last 2 hours.`),
      ...(V.job?.crew_name && V.job.field_status ? [h("div", { class: "note green" }, `Field crew: `, h("a", { href: "#", onclick: (e) => { e.preventDefault(); RM.crewDrawer(V.job.crew_id); } }, V.job.crew_name),
        `, ${{ driving: "en route", in_progress: "on site", done: "repair complete" }[V.job.field_status] || V.job.field_status}`)] : []));
    const bk = V.backup || {}, ties = bk.ties || [], z = bk.zone;
    const best = ties[0];
    const backup = bk.spur
      ? h("div", { class: "note red" }, "A fused spur has no alternative supply: the customers still off stay off until the repair is done, unless a mobile generator carries them.")
      : !ties.length
      ? h("div", { class: "note red" }, "No backup tie on this feeder: the customers still off stay off until the fault is found and repaired.")
      : !best.transfer_amps
        ? h("div", { class: "note red" }, `${ties.map((t) => t.tie_feeder).join(" and ")} ${ties.length > 1 ? "have" : "has"} no spare capacity at peak, so the tie cannot pick anyone up.`)
        : h("div", { class: `note ${best.transfer_share >= 0.99 ? "green" : "amber"}` },
          `${best.transfer_share >= 0.99 ? "All" : `About ${n0(best.customers_carried)} of the ${n0(o.customers_off)}`} customers still off can be back-fed through ${best.nop} from ${best.tie_feeder}`,
          `${best.other_zone ? " (another zone substation)" : ""}: it has ${n0(best.transfer_amps)} A to spare (${n0(best.tie_peak_amps)} A peak on a ${n0(best.tie_rating_amps)} A rating).`);
    affects.append(...[h("div", { class: "big num" }, n0(o.customers_off)), h("div", { class: "muted small" }, `Still off, out of ${n0(o.customers_initial)} at the trip; ${o.life_support_off} on life support.`), backup,
      z && z.single_fed ? h("div", { class: "note red" }, `${z.zs_name} has a single supply circuit: losing it would take every feeder there off.`) : null].filter(Boolean),
      h("div", { class: "sep" }), table([{ l: "Device", k: "asset_id" }, { l: "Type", k: "asset_class_label" }, { l: "Customers behind", f: (r) => r.asset_class_label === "Normally open point" ? "Not applicable (normally open)" : n0(r.customers_downstream), r: 1 }], V.devices || [], (r) => S2.openAsset(r.asset_id), { max: 200 }));
  },
  heat(s, V, happen, affects) {
    const c = chartBox();
    const st0 = V.state || {};
    happen.append(kv([["Transformer", st0.transformer_id], ["Live load", `${n1((V.live || {}).mw)} MW of ${st0.rating_mva} MVA`], ["Top oil now", `${n1((V.live || {}).top_oil)} °C`],
      ["Hydrogen", `${n0(st0.h2_now)} ppm, up ${n0(st0.h2_rise_90d)} in 90 days`]]), c);
    chart(c, { xAxis: axis({ type: "time" }), yAxis: axis({ type: "value", name: "H2 ppm" }), series: [{ type: "line", data: (V.dga || []).map((d) => [d.d, d.h2]), color: "#d2462f", showSymbol: false, areaStyle: { opacity: .15 } }] });
    const c2 = chartBox("short");
    affects.append(h("div", { class: "small muted" }, "Peak load forecast, next 14 days (MW)"), c2);
    chart(c2, { xAxis: axis({ type: "category", data: (V.forecast || []).map((f) => day(f.day)) }), yAxis: axis({ type: "value" }),
      series: [{ type: "line", data: (V.forecast || []).map((f) => f.peak_mw_forecast ?? f.peak_mw_forecast), color: "#c98a1e" },
        { type: "line", data: (V.forecast || []).map((f) => f.peak_mw_upper), color: "#c98a1e66", lineStyle: { type: "dashed" } }] });
  },
  min_load(s, V, happen, affects) {
    const c = chartBox();
    happen.append(h("div", { class: "small muted" }, "Solar radiation forecast across the network, W/m² (Open-Meteo)."), c);
    chart(c, { xAxis: axis({ type: "category", data: (V.solar || []).map((m) => `${day(m.t)} ${hhmm(m.t)}`) }), yAxis: axis({ type: "value" }),
      series: [{ type: "line", data: (V.solar || []).map((m) => m.rad), color: "#c98a1e", showSymbol: false }] });
    affects.append(table([{ l: "Transformer", k: "ds_id" }, { l: "Solar kW", k: "solar_kw", r: 1 }, { l: "Reverse flow", f: (r) => pct(r.est_midday_reverse_flow * 100), r: 1 }, { l: "Sites", k: "sites", r: 1 }],
      V.transformers || [], (r) => S2.openAsset(r.ds_id), { max: 300 }));
  },
  vegetation(s, V, happen, affects) {
    happen.append(table([{ l: "Feeder", k: "feeder_id" }, { l: "Cut now", k: "red", r: 1 }, { l: "Within 12 months", k: "amber", r: 1 }, { l: "Bushfire land", f: (r) => pct(r.bushfire * 100), r: 1 }],
      V.packages || [], null, { max: 320 }));
    affects.append(table([{ l: "Span", k: "span_id" }, { l: "Clearance m", k: "clearance_m", r: 1 }, { l: "In 12 months", k: "clearance_12m_m", r: 1 }, { l: "Species", k: "dominant_species" }],
      V.spans || [], (r) => S2.openAsset(r.span_id), { max: 320 }));
  },
  drone_defect(s, V, happen, affects) {
    const det = V.detection || {}, p = V.pole || {};
    happen.append(h("img", { src: V.image, style: "width:100%;border-radius:8px;border:1px solid #d9e0ea" }),
      kv([["Detection", title((det.defect_class || "").replaceAll("_", " "))], ["Score", n2(det.score)], ["Captured", day(det.captured_at)], ["Pole match", `${p.asset_id}, ${n1(det.distance_m)} m (spatial join)`]]));
    affects.append(kv([["Pole", p.asset_id], ["Material", p.material], ["Age", `${p.age_years} years`], ["Open defects", p.open_defects], ["Customers behind", n0(p.customers_downstream)], ["Isolation point", p.protective_device]]));
  },
  fatigue(s, V, happen, affects) {
    happen.append(table([{ l: "Crew", k: "crew_name" }, { l: "Region", k: "region" }, { l: "Planned hours", f: (r) => n1(r.hours), r: 1 }, { l: "Now", k: "status" }], V.crews || [],
      (r) => RM.crewDrawer(r.crew_id), { noScroll: 1 }));
    affects.append(h("p", {}, "Crews planned past the enterprise agreement's 12 hour cap must stand down; their remaining stops go to the nearest rested, qualified crews, and anything nobody can take goes on hold with the reason."),
      h("a", { class: "btn small", href: "#/res/control" }, "Open Field staff management"));
  },
  records(s, V, happen, affects) {
    happen.append(h("p", { class: "small" }, "Each meter's voltage (metering coordinator voltage service) is correlated with the transformers within 800 m (spatial join), after removing the network-wide daily profile."),
      table([{ l: "NMI", k: "nmi" }, { l: "Recorded", k: "recorded_ds" }, { l: "Follows", k: "suggested_ds" }, { l: "r", k: "best_correlation", r: 1 }, { l: "r recorded", k: "recorded_correlation", r: 1 }],
        V.meters || [], (r) => S2.openAsset(r.nmi), { max: 320 }));
    affects.append(h("p", {}, "Outage notifications and life support checks use the connectivity model. A meter on the wrong transformer means the wrong customers hear about an outage."));
  },
  overdue(s, V, happen, affects) {   // a crew gone quiet on an emergency: the watch, the job and the situation they were sent to
    const w = V.watch || {}, j = V.job || {}, o = V.outage, c = V.crew;
    happen.append(h("p", {}, s.headline),
      kv([["Crew", w.crew_name], ["Sent", w.dispatched_at ? `${hhmm(w.dispatched_at)} by ${(w.dispatched_by || "").split("@")[0]}` : null],
        ["Job", j.title || w.job_id], ["Expected done by", w.expected_by ? hhmm(w.expected_by) : null], ["Allowed beyond that", w.grace_min != null ? `${w.grace_min} minutes` : null],
        ["Last heard", w.last_update_at ? `${ago(w.last_update_at)}: ${V.last_status_label || w.last_status}` : "Not heard from since dispatch."],
        ["Welfare calls", w.welfare_checks ? n0(w.welfare_checks) : "None yet"]]),
      c ? h("button", { class: "btn small", onclick: () => RM.crewDrawer(c.crew_id) }, `Where ${c.crew_name} is now: ${c.status_label}`) : null);
    affects.append(V.linked ? h("p", {}, "Sent to ", h("a", { href: `#/situations/${encodeURIComponent(V.linked.situation_id)}` }, V.linked.title),
        `, ${title((V.linked.status || "").replace("_", " ")).toLowerCase()}.`) : null,
      o ? h("p", {}, o.status === "active" ? `${n0(o.customers_off)} customers still off, ${n0(o.life_support_off)} on life support, waiting on this crew.` : "The outage is no longer active.") : null,
      h("p", { class: "muted small" }, "The alert closes on its own when the crew reports (on site, done or parked), another crew takes over, or the dispatch is recalled."));
  },
  generic(s, V, happen) { happen.append(h("p", {}, s.headline)); },
};

// ---------------------------------------------------------------- live data ingestion and the demo reset (top right)
// The switch starts a 60-minute live run, which switches itself off at the end; switching it off sooner ends the run. Its note says where the
// run is: off, starting (about a minute), on until hh:mm, or switching off.
const LIVE_NOTE = { idle: () => "Off", starting: () => "Starting, about a minute", stopping: () => "Switching off",
  live: (e) => (e.until ? `On until ${hhmm(e.until)}` : "On") };
const liveSwitch = h("input", { type: "checkbox", role: "switch", id: "liveSwitch", "aria-label": "Live data ingestion" });
const liveNote = h("span", { class: "live-note" });
const liveCtl = h("label", { class: "live-ctl", for: "liveSwitch",
  "data-tip": "Streams live network data for 60 minutes, then switches off on its own. Switch it off sooner to end the run." },
  liveSwitch, h("span", { class: "live-track" }), h("span", { class: "live-txt" }, h("b", {}, "Live data ingestion"), liveNote));
let livePoll = 0;
function paintLive(e) {
  const ph = (e && e.phase) || "idle";
  liveSwitch.checked = ph === "live" || ph === "starting";
  liveSwitch.disabled = ph === "stopping";
  liveCtl.className = `live-ctl ${ph}`;
  liveNote.textContent = LIVE_NOTE[ph](e || {});
  clearTimeout(livePoll);
  if (ph === "starting" || ph === "stopping") livePoll = setTimeout(topbar, 3000);   // follow the change closely until it settles
}
liveSwitch.addEventListener("change", async () => {
  const on = liveSwitch.checked;
  paintLive({ phase: on ? "starting" : "stopping" });
  try {
    const r = await post(`/api/presenter/${on ? "go_live" : "stop_live"}`, on ? { minutes: 60 } : {});
    toast(on ? (r.already ? "Live data ingestion is already on." : "Live data ingestion is starting. It switches off on its own after 60 minutes.")
      : "Live data ingestion is switching off.", "green");
  } catch (e) { toast(e.message, "red"); }
  setTimeout(topbar, 1500);
});
const resetBtn = h("button", { class: "btn small", type: "button", "data-tip": "Clears the live outages, raised jobs, crew changes and customer messages, and restores the snapshot's outages in progress.",
  onclick: async () => {
    if (!window.confirm("Reset the demo? The live outages, raised jobs, crew changes and customer messages are cleared, and the snapshot's outages in progress are restored.")) return;
    try { await post("/api/presenter/reset", {}); toast("The demo has been reset.", "green"); topbar(); route(); } catch (e) { toast(e.message, "red"); }
  } }, "Reset demo");

// With a situation open the menu is tucked away; it slides back out over the screen while the mouse is at the left edge or on the menu.
document.addEventListener("mousemove", (e) => {
  if (!document.body.classList.contains("sit-focus")) return;
  const on = e.clientX < 14 || (document.body.classList.contains("rail-peek") && e.target.closest?.("#rail"));
  document.body.classList.toggle("rail-peek", !!on);
});

// ---------------------------------------------------------------- boot
$("#askBtn").addEventListener("click", () => { if (ctx.meta?.genie) window.open(ctx.meta.genie, "_blank", "noopener"); else toast("The Genie space is not connected yet.", "red"); });
$("#demoCtl").append(liveCtl, resetBtn);
if (!location.hash) location.hash = "#/cc";   // the app opens on the Command Centre
$("#top").append(behindButton());   // the orange question mark: what happens behind the screen in view
topbar(); setInterval(topbar, 12000);
setTimeout(route);   // after every module in the import cycle (app, screens, resources) has finished loading
