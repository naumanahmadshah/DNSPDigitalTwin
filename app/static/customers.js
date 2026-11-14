// Customers (route #/customers/<tab>). Three sub-tabs with different columns and figures:
// "Connection applications": the connection pipeline from the case management export, by stage, with what is stuck, what is blocking it,
// payment, the accredited service provider and dates; each application opens its journey through the stages.
// "Existing customers": the premises register with supply status now, life support, critical sites, distributed energy, how each is
// reached, and each premises' outages and notices in the drawer.
// "Customer notices": the wizard to design a notice (opened first, at its first step) and the notices released (notify.js; #/customers/notices/design or /released).
import { api, h, $, n0, card, table, tile, drawer, toast, modal } from "./ui.js";
import { noticesTab } from "./notify.js";

const view = $("#view");
const TABS = [["being-connected", "Connection applications"], ["existing", "Existing customers"], ["notices", "Customer notices"]];
const S = { q: "", pipe: null, search: "", sum: null, ex: null, cohort: { top: null, sub: null } };
const STAGES = ["apply", "assess", "offer", "design", "build", "commission"];   // the stages an open application can be in

export async function customers(tab, sub) {
  const t = TABS.some(([k]) => k === tab) ? tab : "being-connected";
  view.innerHTML = "";
  view.append(h("div", { class: "screen-h" }, h("h1", {}, "Customers"),
    h("span", { class: "q" }, t === "existing" ? "Every premises on the network: its supply now, its customer's needs and how the customer is reached."
      : t === "notices" ? "Notices to customers: who is told, on which channels and in what words." : "Every new connection application: its stage, what is holding it and what comes next.")),
    h("div", { class: "tabs" }, TABS.map(([k, l]) => h("a", { href: k === "notices" ? "#/customers/notices/design" : `#/customers/${k}`, class: k === t ? "on" : "" }, l))));
  const body = h("div", { class: `tabbody ${t === "notices" ? "" : "cust"}` });
  view.append(body);
  if (t === "existing") await existingTab(body);
  else if (t === "notices") await noticesTab(body, sub);
  else await connectedTab(body);
}

const kv = (rows) => h("div", { class: "kv" }, rows.map(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v ?? "-")]).flat());   // label and value pairs
const dd = (iso) => iso ? new Date(`${iso}T00:00:00`).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric" }) : "-";
const dtm = (iso) => iso ? new Date(iso).toLocaleString("en-AU", { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" }) : "-";
const chip = (text, cls = "") => h("span", { class: `chip ${cls}` }, text);
const money = (x) => (x === null || x === undefined) ? "-" : n0(x);
const noun = (n, one, many) => n === 1 ? one : many;   // the noun for a count: "1 customer", "2 customers"

// ================================================================= Connection applications
async function connectedTab(body) {
  S.pipe = await api("/api/customers/pipeline");
  S.cohort = { top: null, sub: null };
  body.append(h("div", { id: "custKpis" }), h("div", { id: "custCohorts" }), h("div", { id: "custFilters" }), h("div", { id: "custApps" }));
  await refreshApps();
}

// The four headline figures and the cohorts each breaks into. Selecting a figure lists its applications and shows its cohorts as a second
// row (for the open applications, the stage bar; for the others, a row of tiles); selecting a cohort narrows the list to it. Each cohort is worked out from the open applications themselves, so the counts
// add up to the figure above them. Selecting the same tile again steps back out.
const LATE = [["late_30", "Up to 30 days late", (d) => d <= 30], ["late_90", "31 to 90 days late", (d) => d > 30 && d <= 90], ["late_more", "Over 90 days late", (d) => d > 90]];
const COHORTS = {
  open: { is: () => true, subs: (r) => [r.stage, r.stage_label] },
  stuck: { is: (r) => r.stuck, subs: (r) => r.blocker ? [r.blocker, r.blocker_label] : ["no_blocker", "Past stage time, with no blocker recorded"] },
  payment: { is: (r) => r.payment_status === "quoted" || r.payment_status === "invoiced", subs: (r) => [r.payment_status, r.payment_label] },
  late: { is: (r) => r.days_past_target > 0, subs: (r) => { const b = LATE.find(([, , f]) => f(r.days_past_target)); return [b[0], b[1]]; } },
};
const inCohort = (r) => { const c = S.cohort.top && COHORTS[S.cohort.top]; return !c || (c.is(r) && (!S.cohort.sub || c.subs(r)[0] === S.cohort.sub)); };
const pick = (top, sub) => { S.cohort = S.cohort.top === top && sub === undefined ? { top: null, sub: null } : { top, sub: sub === S.cohort.sub ? null : (sub || null) }; refreshApps(); };

function kpiTiles(k) {
  const T = (key, label, v, note, cls = "") => {
    const t = tile(label, n0(v), note, `${cls} click ${S.cohort.top === key ? "on" : ""}`);
    t.addEventListener("click", () => pick(key));
    return t;
  };
  return h("div", { class: "cust-kpis" },
    T("open", "Open applications", k.open, `As of ${dd(S.pipe.today)}.`),
    T("stuck", "Stuck", k.stuck, `${n0(k.blocked)} ${noun(k.blocked, "application", "applications")} held by a blocker. ${n0(k.stuck - k.blocked)} ${noun(k.stuck - k.blocked, "application", "applications")} past ${noun(k.stuck - k.blocked, "its", "their")} stage time.`, k.stuck ? "amber" : ""),
    T("payment", "Awaiting payment", k.awaiting_payment, "Open applications quoted or invoiced and not yet paid."),
    T("late", "Past target date", k.over_target, "Open applications later than their promised connection date.", k.over_target ? "red" : ""));
}

function cohortTiles(all) {
  const c = S.cohort.top && COHORTS[S.cohort.top];
  if (!c) return h("p", { class: "muted small cust-hint" }, "Select a figure to break it down by cohort and list its applications.");
  if (S.cohort.top === "open") return stageStrip(S.pipe.stages);   // the open applications break down by the stage they are in
  const by = new Map();
  for (const r of all.filter(c.is)) { const [key, label] = c.subs(r); const x = by.get(key) || { key, label, n: 0 }; x.n++; by.set(key, x); }
  const order = S.cohort.top === "late" ? LATE.map(([key]) => by.get(key)).filter(Boolean) : [...by.values()].sort((a, b) => b.n - a.n);
  return h("div", { class: "cust-cohorts" }, order.map((x) => {
    const t = h("button", { class: `cust-cohort ${S.cohort.sub === x.key ? "on" : ""}`, onclick: () => pick(S.cohort.top, x.key) },
      h("div", { class: "cust-cohort-v num" }, n0(x.n)), h("div", { class: "cust-cohort-k" }, x.label));
    return t;
  }));
}

async function loadApps() {
  const qs = new URLSearchParams({ open: "true", limit: "2000" });
  if (S.q) qs.set("q", S.q);
  return api(`/api/customers/applications?${qs}`);
}

// the figures, their cohorts, the search and the list re-render together from the same state (a click on a figure or a cohort filters
// the list); the cohorts count every open application, the list also follows the search
async function refreshApps() {
  const kp = $("#custKpis"), co = $("#custCohorts"), fil = $("#custFilters"), holder = $("#custApps");
  if (!holder || !S.pipe) return;
  const [data, all] = await Promise.all([loadApps(), S.q ? api("/api/customers/applications?open=true&limit=2000") : null]);
  kp.innerHTML = ""; kp.append(kpiTiles(S.pipe.kpi));
  co.innerHTML = ""; co.append(cohortTiles((all || data).rows || []));
  fil.innerHTML = ""; fil.append(filters());
  holder.innerHTML = ""; holder.append(appsCard(data));
}

function stageStrip(stages) {
  const by = Object.fromEntries(stages.map((s) => [s.stage, s]));
  return h("div", { class: "cust-stages" }, STAGES.map((code) => {
    const s = by[code] || {};
    const open = Number(s.open_applications || 0), stuck = Number(s.stuck_applications || 0);
    const total = Math.max(1, open);
    const seg = (n, cls, lbl) => n ? h("span", { class: `seg ${cls}`, style: `width:${(100 * n) / total}%`, title: `${n} ${noun(n, "application", "applications")} in stage for ${lbl}.` }) : null;
    return h("button", { class: `cust-stage ${S.cohort.sub === code ? "on" : ""}`, onclick: () => pick("open", code) },
      h("div", { class: "cust-stage-n" }, s.stage_label || code),
      h("div", { class: "cust-stage-v num" }, n0(open)),
      h("div", { class: "cust-stage-s" }, `${n0(stuck)} stuck ${noun(stuck, "application", "applications")}. Median time in stage: ${s.median_days_in_stage == null ? "-" : `${s.median_days_in_stage} ${noun(s.median_days_in_stage, "day", "days")}`}.`),
      h("div", { class: "cust-bar", title: "Open applications by days in stage." },
        seg(Number(s.aged_0_30_days || 0), "a", "30 days or less"), seg(Number(s.aged_31_60_days || 0), "b", "31 to 60 days"),
        seg(Number(s.aged_61_90_days || 0), "c", "61 to 90 days"), seg(Number(s.aged_over_90_days || 0), "d", "over 90 days")));
  }));
}

function filters() {   // finding an application by name, address, suburb or ID; Clear also steps out of any figure or cohort
  const search = h("input", { type: "search", placeholder: "Applicant, address, suburb or application ID", value: S.q, size: 34,
    onkeydown: (e) => { if (e.key === "Enter") { S.q = e.target.value.trim(); refreshApps(); } } });
  return h("div", { class: "cust-filters" },
    h("label", {}, "Find ", search),
    h("button", { class: "btn small", onclick: () => { S.q = search.value.trim(); refreshApps(); } }, "Search"),
    h("button", { class: "btn small", onclick: () => { S.q = ""; S.cohort = { top: null, sub: null }; refreshApps(); } }, "Clear"));
}

// The list's caption as a sentence: how many applications, which figure and cohort they are, and how they are ordered
function listCaption(n, sub, shown) {
  const one = n === 1, N = n0(n), app = one ? "application" : "applications";
  const order = S.cohort.top === "stuck" ? "Listed by time in stage, longest first." : "Listed with stuck applications first, then by time in stage.";
  const what = {
    open: sub ? `${N} open ${app} in the ${sub} stage` : `${N} open ${app}`,
    stuck: !sub ? `${N} stuck ${app}` : S.cohort.sub === "no_blocker" ? `${N} ${app} past ${one ? "its" : "their"} stage time with no blocker recorded`
      : `${N} ${app} held by a blocker: ${sub.charAt(0).toLowerCase() + sub.slice(1)}`,
    payment: sub ? `${N} ${sub.toLowerCase()} ${app} awaiting payment` : `${N} ${app} awaiting payment`,
    late: sub ? `${N} ${app} ${sub.toLowerCase()}` : `${N} ${app} past ${one ? "its" : "their"} target date`,
  }[S.cohort.top] || `${N} open ${app}`;
  return `${what}${shown ? ` (the first ${n0(shown)} shown)` : ""}. ${order}`;
}

function appsCard(data) {
  const c = S.cohort.top && COHORTS[S.cohort.top];
  const rows = (data.rows || []).filter(inCohort);
  const sub = c && S.cohort.sub ? (data.rows || []).filter(c.is).map(c.subs).find(([key]) => key === S.cohort.sub)?.[1] : null;
  const caption = listCaption(c ? rows.length : data.total, sub, data.total > (data.rows || []).length ? (data.rows || []).length : null);
  return card("Applications", caption,
    rows.length ? table([
      { l: "Applicant", f: (r) => h("span", {}, r.applicant_name, h("span", { class: "muted small" }, `  ${r.application_id}`)) },
      { l: "Premises", f: (r) => h("span", {}, r.address, h("span", { class: "muted small" }, `, ${r.suburb}`)) },
      { l: "Type", f: (r) => r.connection_type_label },
      { l: "Stage", f: (r) => r.stage_label },
      { l: "Days in stage", k: "days_in_stage", r: true },
      { l: "Stuck", f: (r) => r.stuck ? h("span", { class: "chip rm-con" }, r.stuck_reason) : chip("On track", "rm-int") },
      { l: "Payment", f: (r) => r.payment_label },
      { l: "Provider", f: (r) => r.asp || "Not yet appointed" },
      { l: "Target", f: (r) => dd(r.target_date) },
    ], rows, (r) => openJourney(r.application_id), { max: 520 })
      : h("div", { class: "nt-empty" }, "No open application matches these filters."));
}

async function openJourney(appId) {
  let j;
  try { j = await api(`/api/customers/application/${encodeURIComponent(appId)}`); } catch (e) { toast(e.message, "err"); return; }
  const a = j.application;
  const steps = h("div", { class: "cust-journey" }, j.journey.map((s) => h("div", { class: `cust-step ${s.current ? "now" : ""} ${s.reached ? "" : "todo"}` },
    h("div", { class: "cust-step-n" }, s.stage_label),
    h("div", { class: "cust-step-d" }, s.reached ? (s.current ? `Current since ${dd(s.entered_date)}, for ${n0(s.days)} ${noun(s.days, "day", "days")}.` : `${dd(s.entered_date)} to ${dd(s.exited_date)}.`) : "Not yet reached.")))
  );
  drawer(h("h2", {}, a.applicant_name), h("p", { class: "muted" }, `${a.connection_type_label} at ${a.address}, ${a.suburb}.`),
    h("h4", {}, "Where it is"), steps,
    a.stuck ? h("div", { class: "note amber" }, `This application is stuck: ${a.stuck_reason}.`) : h("div", { class: "note green" }, "On track for its stage."),
    h("h4", {}, "How the premises is supplied"), supplyChain(a.supply_chain || []),
    h("h4", {}, "Details"),
    kv([["Application", a.application_id], ["Stage", a.stage_label], ["Applied", dd(a.applied_date)], ["Target", dd(a.target_date)],
      ["Days past target", a.days_past_target ? n0(a.days_past_target) : "None"], ["Payment", a.payment_label],
      ["Provider", a.asp || "Not yet appointed"], ["Blocker", a.blocker_label || "None"],
      ["Load", a.load_kva ? `${n0(a.load_kva)} kVA, ${a.phases} phase` : "-"],
      ["Solar export", a.export_kw ? `${n0(a.export_kw)} kW` : "None"], ["Battery", a.battery_kwh ? `${n0(a.battery_kwh)} kWh` : "None"],
      ["Lots", a.lots || "-"], ["NMI", a.nmi || "New premises, not yet on the register"]]));
}

// The supply path, drawn from the bulk supply point down to the premises: each step's icon, what it is, its name and a short detail,
// joined by the line the energy takes. A step's edge takes its network status colour where it has one.
const CHAIN_ICON = { bulk_supply: "stop_bulk_supply", transmission: "stop_transmission_substation", zone: "stop_zone_substation",
  feeder: "stop_hv_feeders", distribution: "stop_distribution_lv", premises: "stop_customers_der" };
function supplyChain(nodes) {
  return h("div", { class: "cust-chain" }, nodes.map((n, i) => h("div", { class: `cust-node ${n.status ? `st-${n.status}` : ""} ${i === nodes.length - 1 ? "last" : ""}` },
    h("div", { class: "cust-node-ic" }, h("img", { src: `/static/icons/${CHAIN_ICON[n.kind]}.png?v=teal`, alt: "" })),
    h("div", { class: "cust-node-tx" }, h("div", { class: "cust-node-k" }, n.label), h("div", { class: "cust-node-n" }, n.name),
      n.detail ? h("div", { class: "cust-node-d" }, n.detail) : null))));
}

// ================================================================= Existing customers
// Existing customers: six figures, each a set of premises. Selecting a figure lists its premises and shows its cohorts as a row of tiles;
// selecting a cohort narrows the list to it. The cohorts are counted on the server across the whole register, so they add up to the figure.
const FIG = {   // title, one, many, the figure's value
  all: ["Premises", "premises", "premises", (k) => k.customers],
  life_support: ["Life support", "premises with a life support customer", "premises with a life support customer", (k) => k.life_support],
  critical: ["Critical sites", "critical site", "critical sites", (k) => k.critical_sites],
  der: ["Distributed energy", "premises with distributed energy", "premises with distributed energy", (k) => k.der],
  off_supply: ["Off supply now", "premises off supply now", "premises off supply now", (k) => k.off_supply_now],
  sms: ["Reachable by text", "premises reachable by text", "premises reachable by text", (k) => k.sms],
};

async function existingTab(body) {
  S.sum = (await api("/api/customers/existing/summary")).kpi;
  S.ex = S.ex || { fig: null, cohort: null, cohorts: [] };
  body.append(h("div", { id: "exKpis" }), h("div", { id: "exCohorts" }),
    h("div", { class: "cust-filters" },
      h("label", {}, "Find ", h("input", { type: "search", placeholder: "NMI, address or suburb", value: S.search, size: 30,
        id: "custSearch", onkeydown: (e) => { if (e.key === "Enter") runSearch(); } })),
      h("button", { class: "btn small", onclick: runSearch }, "Search"),
      h("button", { class: "btn small", onclick: () => { S.search = ""; $("#custSearch").value = ""; S.ex = { fig: null, cohort: null, cohorts: [] }; drawExisting(); } }, "Clear")),
    h("div", { id: "custExisting" }));
  await drawExisting();
}

async function pickFigure(fig, cohort) {
  if (cohort === undefined) S.ex = S.ex.fig === fig ? { fig: null, cohort: null, cohorts: [] } : { fig, cohort: null, cohorts: [] };
  else S.ex.cohort = S.ex.cohort === cohort ? null : cohort;
  await drawExisting();
}

async function drawExisting() {
  const k = S.sum, kp = $("#exKpis"), co = $("#exCohorts");
  if (!kp) return;
  const T = (fig, note, cls = "") => {
    const t = tile(FIG[fig][0], n0(FIG[fig][3](k)), note, `${cls} click ${S.ex.fig === fig ? "on" : ""}`);
    t.addEventListener("click", () => pickFigure(fig));
    return t;
  };
  kp.innerHTML = "";
  kp.append(h("div", { class: "cust-kpis" },
    T("all", `Across ${n0(k.feeders)} ${noun(k.feeders, "feeder", "feeders")} and ${n0(k.zone_substations)} ${noun(k.zone_substations, "zone substation", "zone substations")}.`),
    T("life_support", `${((100 * k.life_support) / k.customers).toFixed(1)}% of all premises.`),
    T("critical", "Hospitals, aged care, schools, water and telecom sites on the register."),
    T("der", `${n0(k.solar)} with solar, ${n0(k.batteries)} with a battery and ${n0(k.ev_chargers)} with an EV charger.`),
    T("off_supply", `Across ${n0(k.outages_now)} live ${noun(k.outages_now, "outage", "outages")}.`, k.off_supply_now ? "red" : ""),
    T("sms", `${n0(k.email)} by email, ${n0(k.app_or_web)} by app or web, and ${n0(k.no_digital)} by mail only.`)));
  co.innerHTML = "";
  if (!S.ex.fig) co.append(h("p", { class: "muted small cust-hint" }, "Select a figure to break it into its cohorts and list its premises."));
  else {
    if (!S.ex.cohorts.length) S.ex.cohorts = (await api(`/api/customers/existing/cohorts?figure=${S.ex.fig}`)).cohorts;
    co.append(S.ex.cohorts.length ? h("div", { class: "cust-cohorts" }, S.ex.cohorts.map((c) => h("button", { class: `cust-cohort ${S.ex.cohort === c.key ? "on" : ""}`,
      onclick: () => pickFigure(S.ex.fig, c.key) }, h("div", { class: "cust-cohort-v num" }, n0(c.n)), h("div", { class: "cust-cohort-k" }, c.label))))
      : h("p", { class: "muted small cust-hint" }, "No premises are in this figure right now."));
  }
  await runSearch();
}

// The list's caption as a sentence: how many premises, which figure and cohort, and how they are ordered
function premisesCaption(total, shown) {
  const fig = S.ex.fig ? FIG[S.ex.fig] : null, c = S.ex.cohorts.find((x) => x.key === S.ex.cohort);
  const noun1 = fig ? (total === 1 ? fig[1] : fig[2]) : "premises";
  const what = fig ? `${n0(total)} ${noun1}${c ? `: ${c.label.charAt(0).toLowerCase() + c.label.slice(1)}` : ""}${S.search ? ", matching the search" : ""}`
    : S.search ? `${n0(total)} premises matching the search` : `${n0(total)} premises on the register`;
  return `${what}. ${shown < total ? `The first ${n0(shown)} are listed, with` : "Listed with"} life support and critical sites first.`;
}

async function runSearch() {
  const box = $("#custExisting");
  if (!box) return;
  const input = $("#custSearch");
  S.search = input ? input.value.trim() : S.search;
  box.innerHTML = "";
  box.append(h("div", { class: "muted" }, "Searching the premises register."));
  let r;
  try {
    const qs = new URLSearchParams({ q: S.search, filter: S.ex.fig || "all", limit: "60" });
    if (S.ex.cohort) qs.set("cohort", S.ex.cohort);
    r = await api(`/api/customers/existing/search?${qs}`);
  } catch (e) { box.innerHTML = ""; box.append(card("Could not search", "", h("p", { class: "muted" }, e.message))); return; }
  box.innerHTML = "";
  const rows = r.rows || [];
  box.append(card("Premises", premisesCaption(r.total, rows.length),
    rows.length ? table([
      { l: "NMI", k: "nmi" },
      { l: "Address", f: (x) => h("span", {}, x.address, h("span", { class: "muted small" }, `, ${x.suburb}`)) },
      { l: "Feeder", k: "feeder_id" },
      { l: "Customer type", f: (x) => x.customer_type_label || "-" },
      { l: "Supply", f: (x) => x.off_supply ? h("span", { class: "chip crit" }, "Off supply now") : chip("On supply", "rm-int") },
      { l: "Life support", f: (x) => x.life_support ? (x.life_support_label || "Registered") : "-" },
      { l: "Critical site", f: (x) => x.critical_site ? (x.site_name || "Yes") : "-" },
      { l: "Distributed energy", f: (x) => derText(x) },
      { l: "Reached by", f: (x) => x.channels?.primary_label || "-" },
    ], rows, (x) => openPremises(x.nmi), { max: 520 })
      : h("div", { class: "nt-empty" }, "No premises match. Try an NMI, a street or a suburb.")));
}

const derText = (x) => {
  const bits = [];
  if (x.solar_kw > 0) bits.push(`Solar ${x.solar_kw} kW`);
  if (x.has_battery) bits.push(bits.length ? "battery" : "Battery");
  if (x.has_ev_charger) bits.push("EV charger");
  return bits.length ? bits.join(", ") : "-";
};

export async function openPremises(nmi) {
  let c;
  try { c = await api(`/api/customers/existing/${encodeURIComponent(nmi)}`); } catch (e) { toast(e.message, "err"); return; }
  const p = c.premises, s = c.supply, lsLabel = c.life_support_label || "Registered";
  const route = c.route;
  drawer(h("h2", {}, p.address), h("p", { class: "muted" }, `${p.suburb} ${p.postcode}, NMI ${p.nmi}, ${p.customer_type_label || ""}`),
    s.off_supply ? h("div", { class: "note red" }, `Off supply: the device protecting this premises has a live outage (outage ${s.outage_id}${s.etr ? `, estimated restoration ${dtm(s.etr)}` : ""}).`)
      : h("div", { class: "note green" }, "On supply: no live outage on the device protecting this premises."),
    h("h4", {}, "How the premises is supplied"), supplyChain(c.supply_chain || []),
    h("h4", {}, "Calls from this premises"),
    (c.calls || []).length ? table([
      { l: "Called", f: (x) => dtm(x.ts) }, { l: "Reported", k: "reason" }, { l: "Channel", f: (x) => x.channel || "-" },
      { l: "Network check", f: (x) => h("span", {}, h("span", { class: `chip ${{ verified: "crit", resolved: "ok", attend: "amber" }[x.check] || ""}` }, x.check_label), h("div", { class: "muted small" }, x.check_note)) },
    ], c.calls, null, { noScroll: true }) : h("p", { class: "muted" }, "No call from this premises in the live record (the last six hours)."),
    h("h4", {}, "Register"),
    kv([["Feeder", p.feeder_name || p.feeder_id], ["Zone substation", p.zs_id], ["Distribution substation", p.ds_id],
      ["Protective device", p.protective_device || "Not recorded"]]),
    h("h4", {}, "Care and critical needs"),
    kv([["Life support", p.life_support ? lsLabel : "Not registered"],
      ["Critical site", p.critical_site ? `${p.site_name || p.critical_site}${p.backup_generator ? `, with a backup generator of ${n0(p.generator_kva)} kVA (${p.generator_fuel_hours} hours of fuel)` : ", with no backup generator"}` : "None"]]),
    h("h4", {}, "Distributed energy"),
    kv([["Solar", p.solar_kw ? `${p.solar_kw} kW` : "None"], ["Battery", p.has_battery ? "Yes" : "None"], ["EV charger", p.has_ev_charger ? "Yes" : "None"]]),
    h("h4", {}, "How we reach them"),
    kv([["Preferred", route.primary_label], ["Channels for the next notice", (route.channels || []).join(", ")], ["Why", route.why]]),
    h("h4", {}, "Outages on this premises' device in the last 12 months"),
    c.outages.length ? table([
      { l: "Started", f: (o) => dtm(o.start_time) }, { l: "Cause", f: (o) => o.cause_label || "-" },
      { l: "Device", f: (o) => o.device_label || "-" }, { l: "Minutes", f: (o) => n0(o.duration_min) },
      { l: "Customers interrupted", k: "customers_interrupted", r: true },
    ], c.outages, null, { noScroll: true }) : h("p", { class: "muted" }, "No outage recorded on this device in the last 12 months."),
    h("h4", {}, "Notices sent to this premises"),
    c.notices.length ? table([
      { l: "Sent", f: (x) => dtm(x.created_at) }, { l: "Notice", f: (x) => x.notice_title || "-" },
      { l: "Audience", f: (x) => x.audience_label || "-" }, { l: "Channel", f: (x) => x.channel_label || "-" },
      { l: "Status", f: (x) => x.status_label || "-" },
    ], c.notices, (x) => messageDetail(x, p), { noScroll: true }) : h("p", { class: "muted" }, "No notice has been sent to this premises."));
}

// One message sent to the premises: what it was about, how and when it went, and the words the customer received
function messageDetail(x, p) {
  const bg = modal(h("h2", {}, x.notice_title),
    kv([["Premises", `${p.nmi}${p.address ? `, ${p.address}` : ""}`], ["Notice type", x.notice_type_label], ["Outage window", x.window_label],
      ["Audience", x.audience_label], ["Channel", x.channel_label], ["Status", x.status_label],
      ["Created", dtm(x.created_at)], ["Sent", x.sent_at ? dtm(x.sent_at) : null], ["Message ID", x.msg_id]].filter(([, v]) => v)),
    h("h4", {}, "Message received"),
    h("div", { class: "nt-msg" }, x.body || "The message text was not recorded."),
    h("div", { class: "plan-actions" }, h("button", { class: "btn primary", onclick: () => bg.remove() }, "Close")));
}
