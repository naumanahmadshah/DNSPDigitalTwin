// Field staff management: field resource management (see docs/architecture.md). The Control
// Room shows each region's plan, the same plan the field works; dispatch, holds, stand-downs, region moves and planning exceptions work on
// it; Re-rostered and Overrides follow every change; the cross-region summary covers every region; the vegetation contractors tab shows the
// vegetation crews (all contractors) with the work packages they hold.
// The dialogs here (assign, raise a job, a crew's day) are also opened from situations, the asset panel, maps and the Asset management search.
import { api, post, h, $, n0, n1, pct, money, ago, day, card, table, chart, chartBox, axis, mapIn, drawer, toast, modal, loading } from "./ui.js";
import { every } from "./app.js";
import { fieldReportsCard } from "./fieldreports.js";
import { contractorsTab, crewWorkCard } from "./vegetation.js";

const view = $("#view");
const TABS = [["control", "Control Room"], ["rerostered", "Re-rostered"], ["overrides", "Overrides"], ["summary", "Cross-region summary"],
  ["vegetation", "Vegetation contractors"]];
const S = { meta: null, region: localStorage.getItem("rm_region"), data: null, prev: null };
const big$ = (v) => Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(2).replace(/\.?0+$/, "")}M` : Math.abs(v) >= 1e3 ? `$${Math.round(v / 1e3)}k` : `$${n0(v)}`;
const plural = (n, one, many = `${one}s`) => (n === 1 ? one : many);   // the noun for a count
const agoS = (ts) => ts ? ago(new Date(ts * 1000).toISOString()) : "";
const typeChip = (t) => h("span", { class: `chip ${t === "Contractor" ? "rm-con" : "rm-int"}` }, t);
const regionName = (id) => (S.meta?.regions.find((r) => r.id === id) || {}).name || id;

async function meta() {
  S.meta ||= await api("/api/rm/meta");
  if (!S.meta.regions.some((r) => r.id === S.region)) S.region = S.meta.regions[0]?.id;
  return S.meta;
}

export async function screen(tab) {
  await meta();
  view.innerHTML = "";
  view.append(h("div", { class: "screen-h" }, h("h1", {}, "Field staff management"),
    h("div", { class: "right" }, bell(), h("a", { class: "btn", href: "/field", target: "_blank" }, "Field app"),
      h("button", { class: "btn", onclick: phoneDemo }, "Phone demo"))),
    h("div", { class: "tabs" }, TABS.map(([k, l]) => h("a", { href: `#/res/${k}`, class: k === tab ? "on" : "" }, l))));
  const body = h("div", { class: "tabbody" });
  view.append(body);
  if (tab === "vegetation") return contractorsTab(body, crewDrawer);   // the vegetation contractors and their work packages (vegetation.js)
  await ({ control, rerostered, overrides, summary }[tab] || control)(body);
}

// ---------------------------------------------------------------- Control Room
async function control(body) {
  const region = S.region;
  S.prev = null;
  body.append(loading("Solving the roster"));
  S.data = await api(`/api/rm/roster?region=${region}`);
  body.innerHTML = "";
  const top = h("div"), kpis = h("div"), grid = h("div", { class: "rm-grid" }), rost = h("div");
  body.append(top, kpis, grid, rost);
  const mapEl = h("div", { class: "map rm-map" }), safety = h("div"), right = h("div");
  grid.append(card("Crews and network", "Jobs on today's plan. Click a job to assign it.", mapEl, h("div", { class: "legend" },
    h("span", {}, h("i", { style: "background:#2f7dc4" }), "Planned job"), h("span", {}, h("i", { style: "background:#d2462f" }), "Emergency"),
    h("span", {}, h("i", { style: "background:#6a5cf0" }), "Override"), h("span", {}, h("i", { style: "background:#0c5a66" }), "Crew now"),
    h("span", {}, h("i", { style: "background:#c98a1e" }), "Stressed feeder"))), right);
  const crewSum = await api(`/api/rm/crew-summary?region=${region}`);
  const feeders = (await api(`/api/rm/feeders?region=${region}`)).feeders;   // loaded before the map draws them
  let m = null, layer = null;
  setTimeout(() => { m = mapIn(mapEl); layer = L.layerGroup().addTo(m); drawMap(); }, 20);

  async function drawMap() {
    if (!layer) return;
    layer.clearLayers();
    const pts = [];
    for (const f of feeders) {
      const s = f.stress_level || 0, c = s > 0.6 ? "#d2462f" : s > 0.35 ? "#c98a1e" : "#2e9c6a";
      L.circleMarker([f.lat, f.lon], { radius: 6 + s * 12, color: c, fillColor: c, fillOpacity: .22, weight: 1 })
        .bindTooltip(`${f.feeder_name || f.feeder_id}<br>stress ${pct(s * 100)} · ${n0(f.customers)} customers · ${f.life_support} on life support`).addTo(layer);
      pts.push([f.lat, f.lon]);
    }
    for (const a of S.data.assignments.slice(0, 450)) {
      const c = a.override ? "#6a5cf0" : a.is_emergency ? "#d2462f" : "#2f7dc4";
      L.circleMarker([a.lat, a.lon], { radius: a.is_emergency ? 7 : 5, color: "#fff", weight: 1, fillColor: c, fillOpacity: .95 })
        .bindTooltip(`${a.title}<br>${a.crew_name} (${a.employment_type})<br><i>click to assign</i>`).on("click", () => assignDialog(region, a, false, refresh)).addTo(layer);
    }
    const pos = (await api(`/api/rm/positions?region=${region}`)).crews;
    for (const p of pos.filter((x) => ["travelling", "on_site"].includes(x.status)))
      L.marker([p.lat, p.lon], { icon: L.divIcon({ className: "rm-crewpin", html: `<span class="${p.status}"></span>`, iconSize: [14, 14] }) })
        .bindTooltip(`${p.crew_name} (${p.persona})<br>${p.status_label}${p.job_title ? ": " + p.job_title : ""}`).on("click", () => crewDrawer(p.crew_id)).addTo(layer);
    if (pts.length && !drawMap.fitted) { m.fitBounds(pts, { padding: [24, 24], maxZoom: 12 }); drawMap.fitted = true; }
  }

  async function refresh() {   // solved again after a change (an assignment, an exception, a crew move); the tiles show what moved
    S.prev = S.data ? S.data.kpis : null;
    S.data = await api(`/api/rm/roster?region=${region}`);
    paint();
  }

  function paint() {
    const perJob = (x) => x && x.jobs_assigned ? Math.round(x.total_cost_aud / x.jobs_assigned) : null;
    const k = { ...S.data.kpis, cost_per_job: perJob(S.data.kpis) }, p = S.prev ? { ...S.prev, cost_per_job: perJob(S.prev) } : {};
    const ovr = S.data.assignments.filter((a) => a.override);
    const ovrTicket = ovr.filter((a) => /qualif|ticket|skill/i.test(a.breach || "")).length, ovrShift = ovr.filter((a) => /shift|hour|fatigue|limit/i.test(a.breach || "")).length;
    const eba = (k.eba_breaches || 0) + ovrShift;
    top.innerHTML = "";
    const regionSel = h("select", { onchange: (e) => { S.region = e.target.value; localStorage.setItem("rm_region", S.region); screen("control"); } },
      S.meta.regions.map((r) => h("option", { value: r.id, selected: r.id === region ? "" : null }, r.name)));
    top.append(h("div", { class: "rm-ctl" }, regionSel,
      h("button", { class: "btn", onclick: () => regionManager(region, refresh) }, "Manage crew regions"),
      h("span", { class: "muted small" }, `${crewSum.available} ${plural(crewSum.available, "crew")} available, ${crewSum.contractors} of them ${plural(crewSum.contractors, "contractor")}.`)));
    kpis.innerHTML = "";
    const K = (l, key, opts = {}) => {
      const v = k[key], pv = p[key], d = pv != null && v !== pv ? (v - pv) / (Math.abs(pv) || 1) * 100 : null;
      return h("div", { class: `tile ${opts.tone || ""}` }, h("div", { class: "k" }, l), h("div", { class: "v num" }, opts.money ? money(v) : n0(v),
        d != null && Math.round(d) !== 0 ? h("span", { class: `rm-delta ${(d > 0) === !opts.lower ? "up" : "down"}` }, `${d > 0 ? "▲" : "▼"} ${Math.abs(Math.round(d))}%`) : null));
    };
    kpis.append(h("div", { class: "rm-ops" },
      K("Jobs assigned", "jobs_assigned", { tone: "green" }), K("Deferred", "jobs_deferred", { tone: "amber", lower: 1 }), K("Emergency jobs", "emergency_jobs"),
      K("Crews deployed", "crew_deployed"), K("Roster cost", "total_cost_aud", { money: 1, lower: 1 }),
      K("Customer minutes at risk", "deferred_customer_minutes", { tone: "red", lower: 1 }),
      h("div", { class: `tile ${eba ? "red" : "green"}` }, h("div", { class: "k" }, "Fatigue breaches"), h("div", { class: "v num" }, n0(eba))),
      K("Crews blocked by an expired ticket", "expired_ticket_blocks", { tone: k.expired_ticket_blocks ? "amber" : "green" }),
      K("Travel km", "total_travel_km", { lower: 1 }), K("Travel emissions kg CO2", "scope3_co2_kg", { lower: 1 }), K("Cost per job", "cost_per_job", { money: 1, lower: 1 })));
    safety.replaceChildren(card("Safety and compliance", "",
      h("div", { class: "kv rm-kv" },
        h("div", { class: "k" }, "Fatigue breaches"), h("b", { class: eba ? "rm-bad" : "rm-good" }, eba ? `${eba} ${plural(eba, "breach", "breaches")}, authorised and logged for audit.` : "None. The solver guards it."),
        h("div", { class: "k" }, "Ticket and qualification breaches"), h("b", { class: ovrTicket ? "rm-bad" : "rm-good" }, ovrTicket ? `${ovrTicket} ${plural(ovrTicket, "override", "overrides")}, recorded as ${plural(ovrTicket, "a breach", "breaches")}.` : "None. The solver guards it."),
        h("div", { class: "k" }, "Crews with an open safety incident"), h("b", { class: k.open_incident_crew ? "rm-bad" : "rm-good" }, k.open_incident_crew ? `${k.open_incident_crew} ${plural(k.open_incident_crew, "crew", "crews")} allowed back into planning.` : "None. The solver guards it."),
        h("div", { class: "k" }, "Manual overrides"), h("b", { class: ovr.length ? "rm-bad" : "rm-good" }, ovr.length ? `${ovr.length} ${plural(ovr.length, "override", "overrides")}, each logged for audit.` : "None.")),
      h("button", { class: "btn", style: "width:100%;margin-top:12px", onclick: () => guardsDialog(region, refresh) }, "Guards and exceptions"),
      h("div", { class: "note" }, "The solver cannot break a hard guard. A supervisor can relax one for planning (Guards and exceptions) or override a single job; every exception is flagged as a breach and written to the audit log.")));
    rost.innerHTML = "";
    const rank = (a) => a.override ? 0 : a.redispatched_from ? 1 : a.reassigned_from ? 2 : a.is_emergency ? 3 : 9;
    const rows = S.data.assignments.slice().sort((a, b) => rank(a) - rank(b)).slice(0, 400);
    rost.append(card(`Roster, ${n0(S.data.assignments_total)} ${plural(S.data.assignments_total, "job", "jobs")} assigned in ${regionName(region)}`, `${n0(S.data.deferred_total)} ${plural(S.data.deferred_total, "job", "jobs")} deferred. Click a row to assign, hold or override.`,
      table([{ l: "Job", f: (a) => h("span", {}, a.title, a.is_emergency ? h("span", { class: "chip crit", style: "margin-left:6px" }, "Emergency") : null,
          a.override ? h("span", { class: "chip rm-ovr", style: "margin-left:6px", title: a.breach || "" }, "Override") : null) },
        { l: "Type", k: "job_type_label" }, { l: "Priority", k: "priority", r: 1 }, { l: "Crew", k: "crew_name" }, { l: "Crew type", f: (a) => typeChip(a.employment_type) },
        { l: "Travel km", k: "travel_km", r: 1 }, { l: "Minutes on site", k: "duration_min", r: 1 }, { l: "Cost", f: (a) => money(a.cost_aud), r: 1 }],
        rows, (a) => assignDialog(region, a, false, refresh), { max: 560 })));
    drawMap();
  }

  function holdCard() {
    const box = h("div"), head = h("span", { class: "chip" });
    const fill = (items) => {
      head.textContent = items.length;
      box.innerHTML = "";
      if (!items.length) box.append(h("p", { class: "muted small" }, "Nothing is on hold. Stops that a crew parks from the field (no access, missing material, customer not home), or that you put on hold, appear here to re-dispatch or allocate."));
      for (const p of items) box.append(h("div", { class: "rm-hold" }, h("div", {}, h("b", {}, p.title || p.job_type_label),
        h("div", { class: "muted small" }, `${p.reason_label} · from ${p.from_crew_name || "the plan"} · ${p.duration_min} min on site`)),
        h("div", { class: "row" },
          h("button", { class: "btn small primary", onclick: async (e) => { e.target.disabled = true; const r = await post("/api/rm/redispatch", { region, job_id: p.job_id });
            toast(r.ok ? `Re-dispatched to ${r.assigned_to.crew_name} (${r.assigned_to.persona}, ${r.assigned_to.km} km).` : r.error, r.ok ? "green" : "red"); refresh(); } }, "Re-dispatch"),
          h("button", { class: "btn small", onclick: () => assignDialog(region, { ...p, crew_name: p.from_crew_name, crew_id: p.from_crew_id }, true, refresh) }, "Allocate"))));
    };
    const tick = () => api(`/api/rm/parked?region=${region}`).then((d) => fill(d.parked)).catch(() => {});
    tick(); every(4000, tick);
    return card(h("span", {}, "On hold Jobs (Waiting Dispatch) ", head), "", box);
  }
  right.append(holdCard(), safety);
  paint();
}


// ---------------------------------------------------------------- assign: a crew for a job, with the rule check, hand-offs and override
async function assignDialog(region, a, fromHold, onDone) {
  const out = h("div"), pick = h("select", { style: "width:100%" }, h("option", { value: "" }, "Loading crews"));
  const actions = h("div", { class: "row", style: "justify-content:flex-end;margin-top:14px" });
  const bg = modal(h("h2", {}, a.title || a.job_type_label), h("p", { class: "muted" }, `Priority ${a.priority}/5 · ${a.duration_min} min on site · currently `,
    h("b", {}, a.crew_name || "unassigned"), fromHold ? h("span", { class: "chip amber", style: "margin-left:8px" }, "On hold") : null),
    h("div", { class: "k muted small" }, "Assign to"), pick, out, actions);
  const c = await api(`/api/rm/candidates?region=${region}&job_id=${encodeURIComponent(a.job_id)}`);
  pick.innerHTML = "";
  pick.append(h("option", { value: "" }, "Choose a crew"), ...(c.candidates || []).map((x) =>
    h("option", { value: x.crew_id }, `${x.crew_name}, ${x.persona}: ${x.load_pct}% loaded, ${x.km} km away${x.eligible ? "" : ", not qualified"}`)));
  let plan = null;
  const close = () => bg.remove();
  const done = (msg, cls = "green") => { toast(msg, cls); close(); onDone && onDone(); };
  const draw = () => {
    actions.innerHTML = "";
    actions.append(h("button", { class: "btn", onclick: close }, "Cancel"));
    if (!fromHold) actions.append(h("button", { class: "btn", onclick: async () => {
      await post("/api/rm/hold", { region, job_id: a.job_id, crew_id: a.crew_id, reason_label: "On hold (dispatcher)" });
      done(`${a.title || a.job_id} is now on hold, in the dispatch queue to be re-allocated.`); } }, "Put on hold"));
    if (!plan) return;
    const apply = async (override, handoffs = []) => {
      const reason = override ? (plan.reason || "Assigned past the shift limit (hand-offs skipped)") : null;
      const r = await post("/api/rm/apply-assign", { region, job_id: a.job_id, crew_id: pick.value, handoffs, override, breach_reason: reason });
      if (r.ok) done(override ? `Override: ${a.title || a.job_id} was force-assigned to ${plan.target.crew_name}. The breach is recorded in the audit log.`
        : `Assigned to ${plan.target.crew_name}${handoffs.length ? `, with ${handoffs.length} ${plural(handoffs.length, "stop", "stops")} handed on` : ""}.`, override ? "red" : "green");
    };
    if (plan.ok) actions.append(h("button", { class: "btn primary", onclick: () => apply(false, plan.kind === "handoffs" ? plan.handoffs.map((x) => ({ job_id: x.job_id, to_crew_id: x.to.crew_id })) : []) },
      plan.kind === "handoffs" ? "Approve and assign" : "Assign"));
    if (!plan.ok || plan.kind === "handoffs") actions.append(h("button", { class: "btn danger", onclick: () => apply(true) },
      plan.kind === "handoffs" ? "Override (skip hand-offs)" : "Override and assign (breach)"));
  };
  pick.addEventListener("change", async () => {
    plan = null; out.innerHTML = ""; draw();
    if (!pick.value) return;
    out.append(h("p", { class: "muted small" }, "Checking the rules"));
    plan = await post("/api/rm/plan-assign", { region, job_id: a.job_id, crew_id: pick.value });
    out.innerHTML = "";
    if (plan.kind === "direct") out.append(h("div", { class: "note green" }, `Fits inside ${plan.target.crew_name}'s day. It finishes about ${plan.finish}, against a shift limit of ${plan.limit}.`));
    else if (plan.kind === "handoffs") out.append(h("div", { class: "note amber" }, h("b", {}, `${plan.target.crew_name}'s day would run past the ${plan.limit} limit. `), "To make room, these stops go to other crews:",
      ...plan.handoffs.map((x) => h("div", { class: "rm-hand" }, h("span", {}, x.title || x.job_type_label), h("span", {}, `→ ${x.to.crew_name}, ${x.to.persona}, ${x.to.km} km`))),
      h("div", { class: "small" }, `Then ${plan.target.crew_name} finishes about ${plan.finish}.`)));
    else out.append(h("div", { class: "note red" }, plan.reason, h("div", { class: "small", style: "margin-top:4px" }, "You can still force it with an override, which is flagged as a breach.")));
    draw();
  });
  draw();
}

// ---------------------------------------------------------------- guards and exceptions
async function guardsDialog(region, onDone) {
  const g = await api(`/api/rm/policy?region=${region}`), pol = g.policy;
  const sec = (title, sub, allKey, listKey, crews, line) => {
    const all = h("input", { type: "checkbox", checked: pol[allKey] ? "" : null, onchange: (e) => { pol[allKey] = e.target.checked; boxes.forEach((b) => { b.disabled = pol[allKey]; b.checked = pol[allKey] || (pol[listKey] || []).includes(b.value); }); } });
    const boxes = crews.map((c) => h("input", { type: "checkbox", value: c.crew_id, disabled: pol[allKey] ? "" : null, checked: pol[allKey] || (pol[listKey] || []).includes(c.crew_id) ? "" : null,
      onchange: (e) => { const s = new Set(pol[listKey] || []); e.target.checked ? s.add(c.crew_id) : s.delete(c.crew_id); pol[listKey] = [...s]; } }));
    return h("div", { class: "rm-sec" }, h("div", { class: "row" }, h("b", {}, title), h("label", { class: "small", style: "margin-left:auto" }, all, " Allow all")),
      h("div", { class: "muted small" }, sub), crews.length ? h("div", { class: "rm-pol" }, crews.map((c, i) => h("label", { class: "rm-polrow" }, boxes[i], h("b", {}, c.crew_name), h("span", { class: "muted small" }, line(c)))))
        : h("p", { class: "muted small" }, "No crews in this region."));
  };
  const bg = modal(h("h2", {}, "Guards and exceptions"), h("p", { class: "muted" }, `${regionName(region)}. Relax a safety guard for planning. Exceptions apply only to the crews you tick and are flagged as breaches in the audit log.`),
    sec("Open safety incidents", "Crews off the roster because of an open safety event. Tick to authorise them back into planning.", "incident_all", "incident_allow", g.incident_crew,
      (c) => `${c.persona} · ${c.event_type}${c.severity ? `, severity ${c.severity}` : ""}${c.equipment ? ` · ${c.equipment}` : ""}`),
    sec("Fatigue (overtime)", "The enterprise agreement caps a day at 12 hours. Tick a crew to authorise up to 4 hours more (each one counts as a fatigue breach).", "fatigue_all", "fatigue_allow", g.fatigue_crew,
      (c) => `${c.persona} · ${c.load_pct}% of the ${c.eba_hours} hour cap`),
    h("div", { class: "row", style: "justify-content:flex-end;margin-top:14px" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn danger", onclick: async () => { await post("/api/rm/policy", { region, ...pol }); bg.remove(); toast("Exceptions applied. The plan is re-solved with them.", "green"); onDone && onDone(); } }, "Apply exceptions")));
}

// ---------------------------------------------------------------- manage crew regions
async function regionManager(region, onDone) {
  const box = h("div"), msg = h("div");
  const bg = modal(h("h2", {}, "Manage crew regions"), h("p", { class: "muted" }, `${regionName(region)}. Move a crew to another region: its stops today go to other qualified crews where possible, the rest go on hold with the reason.`), msg, box);
  bg.querySelector(".modal").classList.add("wide");
  const load = async () => {
    const d = await api(`/api/rm/crew-roster?region=${region}`);
    box.innerHTML = "";
    box.append(table([{ l: "Crew", f: (c) => h("span", {}, c.crew_name, c.moved ? h("span", { class: "chip amber", style: "margin-left:6px" }, "Moved") : null) }, { l: "Persona", k: "persona" },
      { l: "Type", f: (c) => typeChip(c.employment_type) }, { l: "Jobs today", k: "jobs_today", r: 1 },
      { l: "Move to region", f: (c) => { const sel = h("select", {}, h("option", { value: "" }, "Choose a region"), S.meta.regions.filter((r) => r.id !== region).map((r) => h("option", { value: r.id }, r.name)));
        return h("div", { class: "row" }, sel, c.moved ? h("button", { class: "btn small", onclick: async () => { const r = await post("/api/rm/reset-region", { crew_id: c.crew_id }); msg.replaceChildren(h("div", { class: "note green" }, `Restored to ${r.restored_to}.`)); load(); onDone && onDone(); } }, "Undo") : null,
          h("button", { class: "btn small primary", onclick: async () => { if (!sel.value) return; const r = await post("/api/rm/move-region", { crew_id: c.crew_id, to_region: sel.value });
            msg.replaceChildren(h("div", { class: `note ${r.ok ? "green" : "red"}` }, r.ok ? `${r.crew_name} moved to ${r.to_region}. Of ${r.stops} ${plural(r.stops, "stop", "stops")}, ${r.reassigned} ${r.reassigned === 1 ? "was" : "were"} re-rostered and ${r.parked} ${r.parked === 1 ? "is" : "are"} on hold with a reason.` : r.error));
            load(); onDone && onDone(); } }, "Move")); } }], d.crew, null, { max: 520 }));
  };
  load();
}

// ---------------------------------------------------------------- Re-rostered and Overrides (every region, most recent first)
async function rerostered(body) {
  const box = h("div");
  body.append(h("p", { class: "muted" }, "Crews who stood themselves down in the field, or were stood down, in every region, and where each of their stops went."), box);
  const fill = (d) => {
    box.innerHTML = "";
    if (!d.rerostered.length) box.append(h("p", { class: "muted" }, "No stand-downs. When a crew reports it cannot continue (sick, injury, family emergency, vehicle, weather) it shows here with its reason and where every stop went."));
    for (const it of d.rerostered) box.append(card(h("span", {}, it.crew_name, " ", typeChip(it.employment_type)), `${it.persona} · ${it.region_name} · ${agoS(it.ts)}`,
      h("div", { class: "row" }, h("span", { class: "chip amber" }, it.reason_label), h("span", {}, h("b", {}, it.count), ` ${plural(it.count, "stop", "stops")} pulled back`), h("span", { class: "rm-good" }, h("b", {}, it.reassigned), ` ${plural(it.reassigned, "stop", "stops")} re-rostered`),
        it.unplaced ? h("span", { class: "rm-bad" }, h("b", {}, it.unplaced), ` ${plural(it.unplaced, "stop", "stops")} awaiting a crew`) : null,
        h("button", { class: "btn small", style: "margin-left:auto", onclick: async () => { const r = await post("/api/rm/restore-crew", { region: it.region, crew_id: it.crew_id });
          toast(r.ok ? `${it.crew_name} is back on the roster with ${r.restored} ${plural(r.restored, "stop", "stops")}.` : r.error, r.ok ? "green" : "red"); rerostered(clear(body)); } }, "Restore crew")),
      h("div", { class: "rm-moves" }, it.moves.map((m) => h("div", { class: "rm-hand" }, h("span", {}, m.title || m.job_id),
        m.to ? h("span", {}, `→ ${m.to.crew_name}, ${m.to.persona}, ${m.to.km} km`) : h("span", { class: "rm-bad" }, "→ awaiting re-dispatch"))))));
  };
  fill(await api("/api/rm/rerostered"));
  every(6000, () => api("/api/rm/rerostered").then(fill).catch(() => {}));
}

async function overrides(body) {
  const box = h("div");
  body.append(h("p", { class: "muted" }, "Supervisor breach overrides currently on the roster, in every region. Each is a job force-assigned past a hard guard (the shift limit, or a crew that is not qualified)."), box);
  const fill = (d) => {
    box.innerHTML = "";
    if (!d.overrides.length) box.append(h("p", { class: "muted" }, "There are no active overrides. A job force-assigned past a hard guard appears here with its breach. A re-solve that clears the breach removes it."));
    for (const it of d.overrides) box.append(card(h("span", {}, it.crew_name, " ", typeChip(it.employment_type)), `${it.persona || ""} · ${it.region_name} · ${agoS(it.ts)}`,
      h("div", { class: "row" }, h("span", { class: "chip crit" }, it.kind), h("span", {}, it.title || it.job_id), it.from_crew_name ? h("span", { class: "muted" }, `From ${it.from_crew_name}`) : null),
      h("div", { class: "note red" }, it.breach)));
  };
  fill(await api("/api/rm/overrides"));
  every(6000, () => api("/api/rm/overrides").then(fill).catch(() => {}));
}

const clear = (b) => { b.innerHTML = ""; return b; };

// ---------------------------------------------------------------- Cross-region summary: every region's actual field plan
async function summary(body) {
  body.append(loading("Solving every region"));
  const [e, x] = await Promise.all([api("/api/rm/exec"), api("/api/rm/expiring")]);
  body.innerHTML = "";
  const t = e.totals, c1 = chartBox(), c2 = chartBox();
  const T = (k, v, s, cls = "") => h("div", { class: `tile ${cls}` }, h("div", { class: "k" }, k), h("div", { class: "v num" }, v), s ? h("div", { class: "s" }, s) : null);
  body.append(h("div", { class: "glance" }, T("Jobs scheduled today, all regions", n0(t.jobs_assigned), `${n0(t.jobs_deferred)} ${plural(t.jobs_deferred, "job", "jobs")} deferred.`, "green"),
    T("Crews deployed, all regions", n0(t.crew_deployed), "Crews with work on today's plans."), T("Customer minutes at risk, all regions", n0(t.deferred_customer_minutes), "Customer minutes in the deferred work.", "red")),
    h("div", { class: "rm-fin", style: "margin-bottom:16px" }, h("div", { class: "rm-fin-h" }, `Cost, all regions, today (${day(e.day)})`), h("div", { class: "rm-fin-g" },
      T("Roster cost", money(t.total_cost_aud)), T("Cost per job", money(t.jobs_assigned ? t.total_cost_aud / t.jobs_assigned : 0)), T("Travel km", n0(t.total_travel_km)))),
    card(`By region (${e.regions.length})`, "Each region's field plan for today.", table([{ l: "Region", k: "region_name" },
      { l: "Jobs", k: "jobs_assigned", r: 1 }, { l: "Deferred", k: "jobs_deferred", r: 1 }, { l: "Crews", k: "crew_deployed", r: 1 },
      { l: "Travel km", k: "total_travel_km", r: 1 }, { l: "Cost", f: (r) => money(r.total_cost_aud), r: 1 }, { l: "Cost per job", f: (r) => money(r.jobs_assigned ? r.total_cost_aud / r.jobs_assigned : 0), r: 1 }],
      e.regions, null, { noScroll: 1 })),
    h("div", { class: "grid2e" }, card("Crew cost by region", "On today's plans.", c1), card("Network crew cost, last 30 days", "Today is the solved plan. Earlier days are modelled around it, with weekends lighter.", c2)),
    card("Team workload", "Trades ranked by jobs per crew across the network today, with what the work costs.",
      table([{ l: "Team (trade)", k: "persona" }, { l: "Crews", k: "crews", r: 1 }, { l: "Jobs", k: "jobs", r: 1 }, { l: "Jobs per crew", f: (r) => h("b", {}, n1(r.jobs_per_crew)), r: 1 },
        { l: "Hours per crew", f: (r) => n1(r.hours_per_crew), r: 1 }, { l: "Cost", f: (r) => money(r.cost_aud), r: 1 }, { l: "Cost per job", f: (r) => money(r.cost_per_job), r: 1 }], e.by_persona, null, { noScroll: 1 })),
    complianceCard(x));
  const regions = e.regions.slice().sort((a, b) => a.total_cost_aud - b.total_cost_aud);
  chart(c1, { grid: { left: 90, right: 70, top: 10, bottom: 20 }, yAxis: axis({ type: "category", data: regions.map((r) => r.region_name) }), xAxis: axis({ type: "value", axisLabel: { formatter: (v) => big$(v) } }),
    series: [{ type: "bar", data: regions.map((r) => r.total_cost_aud), label: { show: true, position: "right", formatter: (p) => big$(p.value) } }] });
  chart(c2, { grid: { left: 60, right: 18, top: 24, bottom: 30 }, xAxis: axis({ type: "category", data: e.cost_history.map((d) => day(d.date)) }), yAxis: axis({ type: "value", axisLabel: { formatter: (v) => big$(v) } }),
    series: [{ type: "line", data: e.cost_history.map((d) => d.cost_aud), showSymbol: false, areaStyle: {}, color: "#24575f",
      markPoint: { symbolSize: 36, label: { formatter: (p) => big$(p.value), fontSize: 10 }, data: [{ type: "max", name: "Peak" }, { type: "min", name: "Low" }] } }] });
}

function complianceCard(x) {
  const total = x.by_ticket.reduce((a, r) => a + r.n, 0), shown = x.by_ticket.slice(0, 6);
  const line = (label, v, cls, onclick) => h("div", { class: "rm-line click", onclick }, h("span", {}, label), h("b", { class: cls }, n0(v), " ›"));
  return card("Compliance and licence risk", "Crews whose tickets have expired or will expire soon. Click any figure to see the crews behind it.",
    line(h("span", {}, h("b", {}, "Certifications expired now"), " (already lapsed)"), x.expired_total, "rm-bad", () => drill("Certifications expired now, all tickets", "expired", null)),
    line(h("span", {}, h("b", {}, "At risk"), ` (expired or expiring within ${x.window_days} days)`), total, "rm-warn", () => drill(`At risk within ${x.window_days} days, all tickets`, "at_risk", null)),
    h("div", { class: "muted small", style: "margin:10px 0 4px" }, `At risk by ticket${x.by_ticket.length > shown.length ? `, showing the top ${shown.length} of ${x.by_ticket.length}` : ""}`),
    ...shown.map((tk) => line(tk.ticket, tk.n, "", () => drill(`${tk.ticket}, at risk within ${x.window_days} days`, "at_risk", tk.ticket_code))),
    h("div", { class: "note" }, "An expired ticket is a hard guard: a crew cannot be rostered to work that needs it. The breakdown lists the same crews by ticket, so it does not add up to the totals."));
}

async function drill(titleText, scope, ticket) {
  const tb = h("tbody"), more = h("div", { class: "muted small", style: "padding:8px" }), sub = h("span", { class: "muted small" });
  const wrap = h("div", { class: "scroll", style: "max-height:62vh" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Crew", "Persona", "Region", "Type", "Ticket", "Expiry", "Status"].map((x) => h("th", {}, x)))), tb), more);
  modal(h("h2", {}, titleText), sub, wrap);
  let offset = 0, busy = false, has = true;
  const page = async () => {
    if (busy || !has) return;
    busy = true; more.textContent = "Loading more";
    const d = await api(`/api/rm/expiring/detail?scope=${scope}&offset=${offset}&limit=200${ticket ? `&ticket_code=${encodeURIComponent(ticket)}` : ""}`);
    for (const r of d.items) tb.append(h("tr", {}, h("td", {}, r.crew_name), h("td", {}, r.persona), h("td", {}, r.region), h("td", {}, typeChip(r.employment_type)),
      h("td", {}, r.ticket), h("td", {}, fullDay(r.expiry_date)), h("td", {}, r.is_expired ? h("span", { class: "chip crit" }, "Expired") : h("span", { class: "chip" }, `in ${r.days_to_expiry} ${plural(r.days_to_expiry, "day")}`))));
    offset += d.items.length; has = d.has_more; busy = false;
    sub.textContent = `${n0(d.total)} ${plural(d.total, "crew", "crews")}, showing ${n0(offset)}, most urgent first.`;
    more.textContent = has ? "" : `All ${n0(d.total)} ${plural(d.total, "crew", "crews")} loaded.`;
  };
  wrap.addEventListener("scroll", () => { if (wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 120) page(); });
  page();
}

// ---------------------------------------------------------------- stand-down bell
function bell() {
  const badge = h("span", { class: "rm-bellbadge" }), pop = h("div", { class: "rm-bellpop" });
  const btn = h("button", { class: "btn rm-bell", title: "Stand-down alerts", onclick: () => pop.classList.toggle("open") }, "Stand-downs ", badge);
  const seen = new Set(JSON.parse(localStorage.getItem("rm_seen_standdowns") || "[]"));
  const key = (it) => `${it.region}:${it.crew_id}:${Math.round(it.ts)}`;
  let items = [];
  const markSeen = () => { items.forEach((it) => seen.add(key(it))); localStorage.setItem("rm_seen_standdowns", JSON.stringify([...seen])); fill(); };
  const fill = () => {
    const unseen = items.filter((it) => !seen.has(key(it)));
    badge.textContent = unseen.length || ""; btn.classList.toggle("new", unseen.length > 0);
    pop.replaceChildren(h("div", { class: "row" }, h("b", {}, "Stand-down alerts"), unseen.length ? h("button", { class: "btn small", style: "margin-left:auto", onclick: markSeen }, "Mark all seen") : null),
      ...(items.length ? items.slice(0, 8).map((it) => h("div", { class: `rm-bellrow ${seen.has(key(it)) ? "" : "new"}`, onclick: () => { markSeen(); pop.classList.remove("open"); location.hash = "#/res/rerostered"; } },
        h("div", {}, h("b", {}, it.crew_name), ` · ${it.region_name}`), h("div", { class: "muted small" }, `${it.reason_label} · ${it.count} ${plural(it.count, "stop", "stops")} re-rostered · ${agoS(it.ts)}`)))
        : [h("p", { class: "muted small" }, "No stand-downs right now.")]));
  };
  const tick = () => api("/api/rm/rerostered").then((d) => { items = d.rerostered; fill(); }).catch(() => {});
  tick(); every(6000, tick);
  return h("div", { class: "rm-bellwrap" }, btn, pop);
}

// ---------------------------------------------------------------- phone demo: the field app on the audience's phones
async function phoneDemo() {
  const d = await api("/api/rm/phone-link");
  modal(h("h2", {}, "Field app on phones"), d.url
    ? h("div", { class: "rm-qr" }, d.qr ? h("img", { src: `data:image/png;base64,${d.qr}`, alt: "QR code for the field app" }) : null,
        h("p", {}, "Scan to open the field app. ", h("a", { href: d.url, target: "_blank" }, "Open the link"), h("span", { class: "muted small" }, ` (shared ${ago(d.at)})`)),
        h("p", { class: "muted small" }, "On iPhone, tap Share, then Add to Home Screen. On Android, accept the install prompt. Phones and this screen share the same live plan."))
    : h("div", {}, h("p", {}, "Phones open the field app through a relay on the presenter's laptop (a workspace sign-in is needed otherwise). Start it with:"),
        h("pre", { class: "sql" }, "tools/field_demo.sh"), h("p", { class: "muted small" }, "It opens a public link to the field app only, and shares the QR code here.")));
}

// ---------------------------------------------------------------- a crew's day (the Asset management search, maps, situations)
export async function crewDrawer(crewId) {
  const d = await api(`/api/rm/crew/${encodeURIComponent(crewId)}`), c = d.crew, p = d.position || {};
  const veg = c.persona === "Vegetation Crew";   // a vegetation contractor: its field app is the contractor's, and its packages are shown
  drawer(h("h2", {}, c.crew_name), h("p", { class: "muted" }, `${c.crew_id} · ${c.persona} · ${c.region_name} · `, typeChip(c.employment_type)),
    h("div", { class: "kv" }, ...[["Now", p.status_label + (p.job_title ? `: ${p.job_title}` : "")], ["Hours on shift", n1(d.hours_on_shift)], ["Crew size", c.size], ["Vehicle", c.vehicle_type_label],
      ["Skills", (c.skills || []).join(", ")], ["Rate", `${money(c.hourly_cost_aud)} an hour`], d.on_leave ? ["Leave", "On leave today"] : null, d.in_training ? ["Training", "In training today"] : null,
      d.open_incident ? ["Safety", "Open safety incident (off the roster unless authorised)"] : null, d.stood_down ? ["Stood down", d.stood_down.reason_label] : null]
      .filter(Boolean).flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v ?? "-")])),
    h("div", { class: "row", style: "margin:12px 0" }, h("a", { class: "btn small primary", target: "_blank", href: veg ? `/contractor?crew_id=${encodeURIComponent(c.crew_id)}` : `/field?region=${c.region_id}&crew_id=${encodeURIComponent(c.crew_id)}` }, "Open the crew's field app"),
      d.stood_down ? h("button", { class: "btn small", onclick: async () => { await post("/api/rm/restore-crew", { region: c.region_id, crew_id: c.crew_id }); toast(`${c.crew_name} is back on the roster.`, "green"); crewDrawer(crewId); } }, "Restore crew")
        : h("button", { class: "btn small danger", onclick: async () => { const why = prompt("Why is the crew standing down?", "Fatigue"); if (!why) return;
          const r = await post("/api/rm/stand-down", { region: c.region_id, crew_id: c.crew_id, reason_label: why }); toast(`${c.crew_name} stood down. ${r.count} ${plural(r.count, "stop", "stops")} re-rostered.`, "red"); crewDrawer(crewId); } }, "Stand the crew down")),
    h("h4", {}, `Today's stops (${d.stops.length})`), d.stops.length ? table([{ l: "Stop", f: (s) => h("span", {}, s.title, s.is_emergency ? h("span", { class: "chip crit", style: "margin-left:6px" }, "Emergency") : null) },
      { l: "Type", k: "job_type_label" }, { l: "Start", k: "start" }, { l: "Finish", k: "finish" }, { l: "Status", f: (s) => statusLabel(s.status) }], d.stops, null, { noScroll: 1 })
      : h("p", { class: "muted" }, "No stops on today's plan."),
    h("h4", {}, "Tickets"), table([{ l: "Ticket", k: "ticket" }, { l: "Expiry", f: (t) => fullDay(t.expiry_date) }, { l: "Status", f: (t) => t.is_expired ? h("span", { class: "chip crit" }, "Expired")
      : t.days_to_expiry <= 45 ? h("span", { class: "chip amber" }, `in ${t.days_to_expiry} ${plural(t.days_to_expiry, "day")}`) : "Current" }], d.tickets, null, { noScroll: 1 }),
    veg ? crewWorkCard(c.crew_id) : null,   // a vegetation contractor's packages and open bays
    fieldReportsCard({ crewId }));   // what this crew has reported back from the field
}
const fullDay = (d) => d ? new Date(d).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric" }) : "-";
const statusLabel = (s) => ({ planned: "Planned", driving: "En route", in_progress: "In progress", done: "Done", parked: "On hold", reassigning: "Re-rostering", reassigned: "Re-rostered" }[s] || s);

// ---------------------------------------------------------------- raise a job on an asset, feeder, customer or point, and send a crew
export async function raiseJob(spec) {
  const types = (await api("/api/rm/job-types")).types;
  const ttl = h("input", { value: spec.title || "", style: "width:100%" });
  const type = h("select", {}, types.map((t) => h("option", { value: t.code, selected: t.code === spec.job_type ? "" : null }, t.label)));
  const prio = h("select", {}, [5, 4, 3, 2, 1].map((p) => h("option", { value: p, selected: p === (spec.priority || 4) ? "" : null }, `${p}${p === 5 ? " (urgent)" : p === 1 ? " (low)" : ""}`)));
  const emer = h("input", { type: "checkbox", checked: spec.emergency ? "" : null });
  const out = h("div");
  const bg = modal(h("h2", {}, "Raise a job"), h("p", { class: "muted" }, spec.where || ""),
    h("div", { class: "kv", style: "align-items:center" }, h("div", { class: "k" }, "Work"), type, h("div", { class: "k" }, "Title"), ttl, h("div", { class: "k" }, "Priority"), prio,
      h("div", { class: "k" }, "Emergency"), h("label", {}, emer, " Do it now (first stop on the crew's day)")), out,
    h("div", { class: "row", style: "justify-content:flex-end;margin-top:14px" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn primary", onclick: async (e) => {
        e.target.disabled = true;
        const { onRaised, ...body } = spec;
        const r = await post("/api/rm/job", { ...body, job_type: type.value, title: ttl.value || spec.title, priority: +prio.value, emergency: emer.checked });
        out.replaceChildren(h("div", { class: "note green" }, `Job raised in ${r.region_name}. It is on today's plan for the region's crews.`));
        e.target.closest(".row").replaceChildren(h("button", { class: "btn", onclick: () => bg.remove() }, "Leave it for the plan"));
        if (onRaised) onRaised(r);   // e.g. the asset drawer reloads to show the new job
        out.append(await sendCrew(r.job_id, () => { bg.remove(); if (onRaised) onRaised(r); }));
      } }, "Raise the job")));
}

// The nearest qualified crews for a job with room today, each with a dispatch button (the crew drives there on the live engine).
async function sendCrew(jobId, onSent) {
  const d = await api(`/api/rm/nearest?job_id=${encodeURIComponent(jobId)}`);
  return h("div", {}, h("h4", {}, "Send a crew now"), d.crews.length ? table([{ l: "Crew", k: "crew_name" }, { l: "Persona", k: "persona" }, { l: "Km", k: "km", r: 1 }, { l: "Minutes", k: "eta_min", r: 1 },
    { l: "Now", k: "status" }, { l: "", f: (c) => c.blocked ? h("span", { class: "muted small" }, c.blocked) : h("button", { class: "btn small primary", onclick: async () => {
      const r = await post("/api/rm/dispatch", { job_id: jobId, crew_id: c.crew_id });
      toast(r.ok ? `${r.crew_name} dispatched, ${r.km} km away. The job is first on their field app.` : r.error, r.ok ? "green" : "red"); onSent && onSent(); } }, "Dispatch") }], d.crews, null, { noScroll: 1 })
    : h("p", { class: "muted" }, "No qualified crew in the region is available right now. The job waits on the plan."));
}
