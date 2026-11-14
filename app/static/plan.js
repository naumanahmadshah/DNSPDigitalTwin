// Plan and invest (AI aided), #/plan/<tab>[/<id>]. Two tabs:
// Design Work Programmes: the programme picker (with new and delete) and the five steps, left to right. A step opens only once the step
//   before it is accepted. The first four are drafted by AI in the background (the app is veiled while it works) and the planner edits,
//   re-runs with a note or accepts each; saving a change to an accepted step resets the steps after it, after a warning. The work bundles
//   step compares the delivery options of the packages live. The release step keeps or leaves out each bundle, prepares and schedules the
//   customer notices, saves to continue later, and releases the plan to the field.
// Released Plans: every released plan, and for one its cost and a board of its work by stage (upcoming, on the roster, in the field, done).
// The financial performance view is off the tab row for now; #/plan/finance still opens it.
import { api, post, h, $, n0, n1, money, card, table, chart, axis, modal, toast, loading } from "./ui.js";
import { financeTab } from "./finance.js";
import { noticeEditor } from "./notify.js";

const view = $("#view");
const GROUPS = [["design", "Design Work Programmes"], ["released", "Released Plans"]];
const STEP_KEYS = ["forecast", "constraints", "optimise", "bundles", "release"];
const AREA = { MET: "Metro West", RNG: "Ranges", CST: "Coast" };
const PRESSURE = ["high", "medium", "low"];
const CHECKS = [["school_zone", "School zone"], ["road_occupancy", "Road occupancy"], ["crew_hours", "Crew hours"], ["outage_duration", "Outage duration"],
  ["switching_crew", "Switching crew"]];
const NEW_PACKAGE = "__new__";
const RUN_KEY = "atlas.plan.run";   // the programme the planner has open, kept in the browser
const S = { runs: [], state: null, runId: null, tab: "forecast", timer: null };

const fy = (v) => v === null || v === undefined ? "-" : `${n1(v)}%`;
const mw = (v) => v === null || v === undefined ? "-" : `${n1(v)} MW`;
const kh = (v) => v === null || v === undefined ? "-" : `${n0(v)} h`;
const label = (v) => v === null || v === undefined || v === "" ? "-" : String(v);
const pill = (text, cls = "") => h("span", { class: `plan-pill ${cls}` }, text);
const noun = (n, one, many) => n === 1 ? one : many;   // the noun for a count: "1 bundle", "2 bundles"
const yesNo = (v) => v ? "Yes" : "No";
const dd = (iso) => iso ? new Date(`${String(iso).slice(0, 10)}T00:00:00`).toLocaleDateString("en-AU", { weekday: "short", day: "numeric", month: "short" }) : "-";
const when = (ts) => ts ? new Date(ts).toLocaleString("en-AU", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : "-";

// ---------------------------------------------------------------- the screen
export async function plan(tab = "", id = "") {
  clearTimeout(S.timer);
  S.dirty = null;
  if (tab === "released") return releasedTab(id);
  if (tab === "finance") { const body = h("div", { class: "plan-body" }); view.innerHTML = ""; view.append(header(), groupRow(""), body); return financeTab(body); }
  S.runs = (await api("/api/planning/runs")).runs;
  const want = localStorage.getItem(RUN_KEY);
  S.runId = (S.runs.find((r) => r.run_id === want) || S.runs[0])?.run_id || null;
  S.state = S.runId ? await api(`/api/planning/runs/${S.runId}`) : null;
  if (!STEP_KEYS.includes(tab) && S.state) { location.replace(`#/plan/${resumeStep()}`); return; }   // back to the step in hand
  S.tab = STEP_KEYS.includes(tab) ? tab : "forecast";
  const body = h("div", { class: "plan-body" });
  view.innerHTML = "";
  view.append(...[header(), groupRow("design"), programmeBar()].filter(Boolean));
  if (!S.runId) { view.append(body); return emptyState(body); }
  const step = stepInfo(S.tab);
  if (!step.enabled) {   // a step that is not open yet: go to the first open step not yet done
    const first = S.state.steps.find((s) => s.enabled && !s.done) || S.state.steps[0];
    const i = S.state.steps.indexOf(step);
    toast(`Step ${i + 1} is not open yet. Complete step ${i}, ${S.state.steps[i - 1].label}, first.`);
    location.replace(`#/plan/${first.key}`);
    return;
  }
  view.append(tabsRow(), body);
  if (S.tab === "release") return releaseTab(body);
  return stepTab(body, S.tab);
}

// open a programme being designed from elsewhere in the app (for example a customer notice prepared for it)
export function openProgramme(runId) { localStorage.setItem(RUN_KEY, runId); location.hash = "#/plan"; }

// the step to continue from: the one the programme is on (saved with it), if it is open, else the first open step not yet done
function resumeStep() {
  const steps = S.state.steps, cur = steps.find((x) => x.key === S.state.run.current_step);
  return (cur?.enabled ? cur : steps.find((x) => x.enabled && !x.done) || steps[0]).key;
}

function header() {
  return h("div", { class: "screen-h" }, h("h1", {}, "Plan and invest"), h("span", { class: "q" }, "Where network risk is building, and the works that address it."));
}

function groupRow(group) {
  return h("div", { class: "tabs" }, GROUPS.map(([k, l]) => h("a", { href: k === "design" ? "#/plan" : "#/plan/released", class: k === group ? "on" : "" }, l)));
}

// the programme bar: the programme open (a picker only when more than one is being designed), discarding it, and when it was last saved.
// With none yet there is no bar: the empty state below is the one place to start a programme.
function programmeBar() {
  if (!S.runs.length) return null;
  const run = S.state?.run;
  const pick = S.runs.length > 1
    ? h("select", { class: "plan-pick", onchange: (e) => { localStorage.setItem(RUN_KEY, e.target.value); location.hash = "#/plan"; plan(); } },
      S.runs.map((r) => h("option", { value: r.run_id, selected: r.run_id === S.runId ? "selected" : null }, r.title)))
    : h("b", {}, run?.title || "");
  return h("div", { class: "plan-bar" },
    h("label", { class: "plan-bar-l" }, "Programme", pick),
    run ? h("button", { class: "btn small", onclick: () => discardRun(run) }, "Discard programme") : null,
    run?.saved_at ? h("span", { class: "muted small" }, `Last saved ${when(run.saved_at)}.`) : null);
}

function tabsRow() {   // the five steps, left to right; a step opens once the one before it is accepted
  return h("div", { class: "tabs plan-steps" }, S.state.steps.map((s, i) => h("a", {
    href: s.enabled ? `#/plan/${s.key}` : null, class: [s.key === S.tab ? "on" : "", s.enabled ? "" : "off", s.done ? "done" : ""].join(" "),
    "aria-disabled": s.enabled ? null : "true", onclick: s.enabled ? null : (e) => { e.preventDefault(); blockedNote(i); },
  }, `${i + 1}. ${s.label}`, s.done ? h("span", { class: "plan-tick", "aria-label": "done" }, "✓") : null)));
}

function blockedNote(i) {
  const prev = S.state.steps[i - 1];
  const bg = modal(h("h2", {}, `Step ${i + 1} is not open yet`),
    h("p", {}, `Complete step ${i}, ${prev.label}, first: draft it with AI, review it and accept it. Each step is built from the one before it.`),
    h("div", { class: "plan-actions" }, h("button", { class: "btn primary", onclick: () => bg.remove() }, "OK")));
}

function emptyState(body) {
  const steps = [
    ["Forecast demand", "Project peak demand on each zone substation and compare it with firm capacity."],
    ["Network limitations and options", "Identify capacity, security, voltage, asset condition and reliability constraints, with the options to address each."],
    ["Design interventions", "Cost, score and rank the options to form a recommended investment portfolio."],
    ["Structure work bundles", "Group work that shares an outage, and compare delivery options against crew and switching capacity."],
    ["Release works plan", "Release the near-term works to the field as scheduled jobs, with customer notices prepared for each outage."],
  ];
  body.append(card("Start a works programme", "AI-assisted",
    h("p", {}, "A works programme is developed in five sequential steps. AI prepares a draft of each step from the outcome of the previous one. "
      + "A planner reviews and edits the draft, and the next step opens only once it is accepted."),
    h("ol", { class: "plan-flow" }, steps.map(([t, d]) => h("li", {}, h("b", {}, t), ". ", d))),
    h("p", { class: "muted" }, "All figures, including costs, scores, schedules and notice dates, are calculated from the network data. AI drafts the narrative and the recommendations only."),
    h("button", { class: "btn primary", onclick: newRun }, "Start a programme")));
}

function newRun() {
  const input = h("input", { type: "text", placeholder: "Programme name", style: "width:100%" });
  const bg = modal(h("h2", {}, "New works programme"), h("p", { class: "muted" }, "Name the programme. You can return to it at any step."), input,
    h("div", { class: "plan-actions" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn primary", onclick: async () => {
        try {
          const r = await post("/api/planning/runs", { title: input.value });
          localStorage.setItem(RUN_KEY, r.run.run_id);
          bg.remove();
          if (location.hash === "#/plan/forecast") plan("forecast"); else location.hash = "#/plan/forecast";
        } catch (e) { toast(e.message, "red"); }
      } }, "Start")));
  input.focus();
}

// discard the programme being designed and go back to the start, where a new one can begin
function discardRun(run) {
  const bg = modal(h("h2", {}, `Discard ${run.title}?`),
    h("p", {}, "This discards the programme and everything in it: its drafts, changes and decisions, its release choices, and any customer notices prepared for it that have not been sent. You return to the start, where a new programme can begin. This cannot be undone."),
    h("div", { class: "plan-actions" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn red", onclick: async () => {
        try {
          S.dirty = null;
          await api(`/api/planning/runs/${run.run_id}`, { method: "DELETE" });
          localStorage.removeItem(RUN_KEY);
          bg.remove();
          toast(`${run.title} has been discarded.`);
          if (location.hash === "#/plan") plan(); else location.hash = "#/plan";
        } catch (e) { toast(e.message, "red"); }
      } }, "Discard programme")));
}

// ---------------------------------------------------------------- one step
function stepInfo(key) { return S.state.steps.find((s) => s.key === key); }
const stepNo = (key) => STEP_KEYS.indexOf(key) + 1;

async function stepTab(body, step) {
  const v = await api(`/api/planning/runs/${S.runId}/steps/${step}`);
  body.innerHTML = "";
  if (v.status === "drafting") return drafting(body, step);
  // a step not drafted yet is drafted by AI as soon as it opens; one whose draft failed waits, so a failing draft is not retried in a loop
  if (!v.version && !v.failed) {
    body.append(loading("Drafting this step with AI"));
    try { await post(`/api/planning/runs/${S.runId}/draft`, { step, instruction: "" }); }
    catch (e) { body.innerHTML = ""; body.append(card("This step could not be drafted", "", h("p", { class: "plan-err" }, e.message))); return; }
    return plan(step);
  }
  S.dirty = v.status === "drafted" ? { step, label: v.label, pending: !!v.pending } : null;   // a draft or changes not yet accepted
  body.append(h("div", { class: "plan-actions plan-actions-r" },
    h("button", { class: "btn amber", onclick: () => makeSuggestion(step) }, "Make a suggestion")));
  if (v.failed) body.append(card("The last draft did not finish", "", h("p", { class: "plan-err" }, v.failed), h("p", { class: "muted" }, 'Select "Make a suggestion" to try again.')));
  if (!v.version) return;
  if (v.narrative) body.append(card("Background information", v.label, h("div", { class: "plan-narr" }, v.narrative)));
  if (step === "forecast") body.append(forecastView(v));
  if (step === "constraints") body.append(constraintsView(v));
  if (step === "optimise") body.append(optimiseView(v));
  if (step === "bundles") body.append(bundlesView(v));
  if (v.status === "drafted") body.append(h("div", { class: "plan-foot" },   // accepting closes the step, so it sits at the end of the page
    v.pending ? h("span", { class: "muted" }, "You changed this step after it was accepted. Save the changes to carry them forward, or discard them.") : null,
    v.pending ? h("button", { class: "btn", onclick: () => discard(step) }, "Discard changes") : null,
    h("button", { class: "btn primary", onclick: () => accept(step, v) }, v.pending ? "Save changes" : "Next")));
}

// while a draft runs the app is veiled (the lineworker at work); the step is polled until the draft is ready or fails
function drafting(body, step) {
  body.append(loading("Drafting this step with AI"));
  const poll = async () => {
    if (S.tab !== step || !location.hash.startsWith(`#/plan/${step}`)) return;
    let v;
    try { v = await api(`/api/planning/runs/${S.runId}/steps/${step}`); } catch (e) { v = null; }
    if (v && v.status === "drafting") { S.timer = setTimeout(poll, 3000); return; }
    plan(step);
  };
  S.timer = setTimeout(poll, 3000);
}

async function draft(step, instruction = "") {
  try {
    await post(`/api/planning/runs/${S.runId}/draft`, { step, instruction });
    plan(step);
  } catch (e) { toast(e.message, "red"); }
}

function makeSuggestion(step) {
  const box = h("textarea", { rows: 4, style: "width:100%", placeholder: "For example: prefer deferral where the cost is above the risk saving." });
  const bg = modal(h("h2", {}, "Make a suggestion"), h("p", { class: "muted" }, "Describe what you would like changed. The AI drafts a new version of this step from your suggestion and the accepted step before it. The new version waits for you to accept it."), box,
    h("div", { class: "plan-actions" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn primary", onclick: () => { bg.remove(); draft(step, box.value); } }, "Redraft")));
  box.focus();
}

function accept(step, v) {
  const go = async () => {
    try {
      await post(`/api/planning/runs/${S.runId}/steps/${step}/accept`, { version: v.version });
      S.dirty = null;   // accepted, and saved with the programme
      const next = STEP_KEYS[STEP_KEYS.indexOf(step) + 1] || step;   // the next step, with its address, so a reload or a poll stays on it
      if (location.hash === `#/plan/${next}`) plan(next); else location.hash = `#/plan/${next}`;
    } catch (e) { toast(e.message, "red"); }
  };
  if (!v.later_done?.length) return go();
  const bg = modal(h("h2", {}, "This changes the later steps"),
    h("p", {}, `Saving these changes to ${v.label} resets the steps after it, because they were built from the version this replaces:`),
    h("ul", { class: "plan-list" }, v.later_done.map((l) => h("li", {}, l))),
    h("p", {}, "They will need to be drafted and accepted again. Do you want to continue?"),
    h("div", { class: "plan-actions" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn amber", onclick: () => { bg.remove(); go(); } }, "Save and reset the later steps")));
}

async function discard(step) {
  try { await post(`/api/planning/runs/${S.runId}/steps/${step}/discard`, {}); plan(step); } catch (e) { toast(e.message, "red"); }
}

// Leaving a step that holds a draft or changes not yet accepted (for any other screen or step): the planner saves it, to continue from
// this step next time, or discards it. Returns true when it has taken over the move; `go` makes the move once the planner has chosen.
export function leavePlan(from, to, go) {
  const d = S.dirty;
  if (!d || !S.runId || from !== `#/plan/${d.step}` || to === from) return false;
  const runId = S.runId, n = stepNo(d.step);
  const after = (f) => async () => {
    try { await f(); S.dirty = null; bg.remove(); go(); } catch (e) { toast(e.message, "red"); }
  };
  const bg = modal(h("h2", {}, `Save step ${n}, ${d.label}?`),
    h("p", {}, d.pending
      ? "You changed this step after it was accepted, and the changes are not yet saved. Save the programme to continue from this step next time, or discard the changes."
      : "This step has a draft that has not been accepted. Save the programme to continue from this step next time, or discard the draft."),
    h("div", { class: "plan-actions" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Stay on this step"),
      h("button", { class: "btn", onclick: after(() => post(`/api/planning/runs/${runId}/steps/${d.step}/discard`, {})) }, d.pending ? "Discard changes" : "Discard draft"),
      h("button", { class: "btn primary", onclick: after(() => post(`/api/planning/runs/${runId}/save`, { step: d.step })) }, "Save")));
  return true;
}

async function edit(step, v, edits) {
  try {
    await post(`/api/planning/runs/${S.runId}/steps/${step}/edit`, { version: v.version, edits });
    plan(step);
  } catch (e) { toast(e.message, "red"); }
}

// ---------------------------------------------------------------- 1 forecast demand
const BAND = [[100, "#c8452f", "Over firm capacity"], [80, "#d39a2c", "Approaching firm capacity (80 to 100%)"], [0, "#3f8f6b", "Comfortable (under 80%)"]];
const SINGLE = "#9aa7b3";
const bandOf = (pct) => BAND.find(([lim]) => pct > lim || (lim === 0 && pct >= 0));

function forecastView(v) {
  const rows = v.rows || [];
  const firm = rows.filter((r) => r.utilisation_fy27_pct != null).sort((a, b) => b.utilisation_fy27_pct - a.utilisation_fy27_pct);
  const single = rows.filter((r) => r.utilisation_fy27_pct == null);
  const bars = [...firm, ...single];
  const name = (r) => r.name.replace(/ zone substation$/, "");
  const ch = h("div", { class: "chart tall" });
  const pctLabel = (x) => x >= 99 && x < 101 ? `${n1(x)}%` : `${Math.round(x)}%`;
  setTimeout(() => chart(ch, { grid: { left: 16, right: 64, top: 30, bottom: 44, containLabel: true },
    xAxis: axis({ type: "value", min: 0, max: Math.max(120, Math.ceil(Math.max(...firm.map((r) => r.utilisation_fy27_pct), 0) / 10) * 10 + 10),
      name: "Share of firm capacity used at the projected FY27 peak (%)", nameLocation: "middle", nameGap: 28 }),
    yAxis: { type: "category", data: bars.map(name), inverse: true, axisLabel: { color: "#5f7185" } },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, formatter: (p) => { const r = bars[p[0].dataIndex];
      return r.utilisation_fy27_pct == null ? `${r.name}<br>Single supply: no backup if its one feed is lost.<br>Projected peak: ${mw(r.peak_fy27_mw)}.`
        : `${r.name}<br>${fy(r.utilisation_fy27_pct)} of firm capacity.<br>Projected peak: ${mw(r.peak_fy27_mw)}.${r.first_over ? `<br>First over firm capacity: ${r.first_over}.` : ""}`; } },
    series: [{ type: "bar", keepColours: true, barMaxWidth: 22,
      data: bars.map((r) => r.utilisation_fy27_pct == null
        ? { value: 6, itemStyle: { color: SINGLE }, label: { show: true, position: "right", color: "#5f7185", formatter: "Single supply: no backup feed" } }
        : { value: r.utilisation_fy27_pct, itemStyle: { color: bandOf(r.utilisation_fy27_pct)[1] },
          label: { show: true, position: "right", color: "#33424f", formatter: pctLabel(r.utilisation_fy27_pct) } }),
      markLine: { symbol: "none", silent: true, data: [{ xAxis: 100 }], lineStyle: { color: "#c8452f", type: "dashed", width: 1.5 },
        label: { position: "start", formatter: "Firm capacity", color: "#c8452f" } } }] }), 0);
  const legend = h("div", { class: "plan-legend" }, BAND.map(([, c, l]) => h("span", {}, h("i", { style: `background:${c}` }), l)),
    h("span", {}, h("i", { style: `background:${SINGLE}` }), "Single supply (no backup feed)"));
  const sel = (r) => h("select", { onchange: (e) => edit("forecast", v, [{ item_id: r.id, field: "pressure", value: e.target.value }]) },
    PRESSURE.map((p) => h("option", { value: p, selected: r.pressure === p ? "selected" : null }, p[0].toUpperCase() + p.slice(1))));
  const cols = [
    { l: "Zone substation", f: (r) => h("span", {}, r.name, h("span", { class: "muted" }, `, ${AREA[r.area] || r.area}`)) },
    { l: "Single fed", f: (r) => yesNo(r.single_fed) },
    { l: "Projected peak FY27", f: (r) => mw(r.peak_fy27_mw), r: true },
    { l: "Use of N-1 FY27", f: (r) => fy(r.utilisation_fy27_pct), r: true },
    { l: "Use of N-1 FY31", f: (r) => fy(r.utilisation_fy31_pct), r: true },
    { l: "Use of N-1 FY36", f: (r) => fy(r.utilisation_fy36_pct), r: true },
    { l: "First over N-1", f: (r) => label(r.first_over) },
    { l: "Customers", f: (r) => n0(r.customers), r: true },
    { l: "Life support", f: (r) => n0(r.life_support), r: true },
    { l: "Pressure", f: (r) => sel(r) },
    { l: "Why", f: (r) => h("span", { class: "plan-why" }, label(r.why)) },
  ];
  return h("div", {}, card("How close each zone substation is to its firm capacity", "At the projected FY27 peak.",
    h("p", { class: "muted" }, "Firm capacity is what a zone substation can carry with its largest transformer out of service. A bar past the dashed line means a transformer failure at peak would leave customers without supply until load is moved or the transformer is back."),
    legend, ch), card("Zones ranked by pressure", `${n0(rows.length)} ${noun(rows.length, "zone substation", "zone substations")}, ordered by pressure and then by the first year over firm capacity.`, table(cols, rows, null, { max: 520 })));
}

// ---------------------------------------------------------------- 2 network limitations and options
function constraintsView(v) {
  const cols = [
    { l: "Selected", f: (r) => h("input", { type: "checkbox", checked: r.carry ? "checked" : null, onchange: (e) => edit("constraints", v, [{ item_id: r.id, field: "carry", value: e.target.checked }]) }) },
    { l: "Problem", f: (r) => h("span", {}, r.type, h("span", { class: "muted" }, `, ${r.zone || ""}${r.feeder ? `, ${r.feeder}` : ""}`)) },
    { l: "Measure", f: (r) => `${label(r.value)}${r.unit === "%" ? "%" : ` ${label(r.unit)}`}` },
    { l: "Years to limit", f: (r) => r.years_to_limit === null || r.years_to_limit === undefined ? "Not projected" : (r.years_to_limit === 0 ? "Now" : `${r.years_to_limit} ${noun(r.years_to_limit, "year", "years")}`), r: true },
    { l: "Severity", f: (r) => n0(r.severity), r: true },
    { l: "Customers", f: (r) => n0(r.customers), r: true },
    { l: "Details", f: (r) => h("span", { class: "plan-why" }, r.summary || "") },
    { l: "Why recommended for selection", f: (r) => h("span", { class: "plan-why" }, label(r.why)) },
  ];
  const count = (v.rows || []).length;
  return card("Network problems list to select from", `${n0(count)} ${noun(count, "network problem", "network problems")}. Tick Selected to carry a problem into the design interventions step.`, table(cols, v.rows || [], null, { max: 620 }));
}

// ---------------------------------------------------------------- 3 design interventions
const CRIT = [["network_benefit", "Network benefit"], ["asset_risk", "Asset risk"], ["cost", "Cost"], ["reliability", "Reliability"], ["sustainability", "Sustainability"], ["timing", "Timing"]];

function optimiseView(v) {
  const w = v.facts?.weights || {};
  const weights = h("div", { class: "plan-weights" }, h("div", { class: "plan-weights-t" }, "Scoring Weights"),
    h("ul", {}, CRIT.map(([k, l]) => h("li", {}, `${l} ${Math.round((w[k] || 0) * 100)}%`))));
  const blocks = (v.rows || []).map((r) => {
    const pick = h("select", { onchange: (e) => edit("optimise", v, [{ item_id: r.id, field: "recommended_option_id", value: e.target.value }]) },
      r.scores.map((s) => h("option", { value: s.option_id, selected: s.option_id === r.recommended_id ? "selected" : null }, s.option || s.option_id)));
    const scores = table([{ l: "Option", f: (s) => h("span", { class: s.option_id === r.recommended_id ? "plan-rec" : "" }, s.option || "") },
      { l: "Ten year cost", f: (s) => s.cost_10yr_aud === null || s.cost_10yr_aud === undefined ? "-" : money(s.cost_10yr_aud), r: true },
      { l: "Benefit", f: (s) => s.benefit || "-" },
      { l: "Lead time", f: (s) => s.lead_time_months === null || s.lead_time_months === undefined ? "-" : `${n0(s.lead_time_months)} ${noun(s.lead_time_months, "month", "months")}`, r: true },
      ...CRIT.map(([k, l]) => ({ l, f: (s) => n0(s[k]), r: true })),
      { l: "Total", f: (s) => h("b", {}, n1(s.total)), r: true }], r.scores);
    return h("div", { class: "plan-opt" }, h("div", { class: "plan-opt-h" }, h("b", {}, r.summary || r.id)), scores,
      h("div", { class: "plan-recbox" }, h("div", { class: "plan-recbox-t" }, "Recommendation and reasoning"),
        h("div", { class: "plan-opt-pick" }, h("span", { class: "muted" }, "Recommended:"), pick),
        h("p", { class: "plan-why" }, label(r.why))));
  });
  return h("div", { class: "card plan-optcard" }, weights, h("h3", {}, "Recommended option for each problem"),
    h("p", { class: "muted plan-optintro" }, "For each selected problem, the options that apply to it, each scored by the AI from 0 to 100 on six criteria and totalled with the weights shown. Read the scores, then the recommendation under each table."),
    h("div", { class: "plan-clear" }), ...blocks);
}

// ---------------------------------------------------------------- 4 structure work bundles, and compare how to deliver them
const costCell = (c) => money(c?.total);

function checkChip(c) { return h("span", { class: `plan-chk ${c?.status === "action" ? "action" : "ok"}`, title: c?.note || "" }, c?.label || "-"); }

function bundlesView(v) {
  const f = v.facts || {};
  const keys = f.package_keys || [];
  const cl = f.cost_labels || {};
  const parts = ["labour", "materials", "plant", "traffic", "switching", "customer", "contingency"];
  const packs = f.packages || [];
  const total = packs.reduce((a, p) => ({ bundles: a.bundles + p.bundles, items: a.items + p.items, crew_hours: a.crew_hours + p.crew_hours,
    customers: a.customers + p.customers, total: a.total + (p.cost?.total || 0) }), { bundles: 0, items: 0, crew_hours: 0, customers: 0, total: 0 });
  const packTable = table([{ l: "Package", f: (p) => h("b", {}, p.label) }, { l: "Bundles", f: (p) => n0(p.bundles), r: true },
    { l: "Work items", f: (p) => n0(p.items), r: true }, { l: "Crew hours", f: (p) => kh(p.crew_hours), r: true },
    { l: "Customers off", f: (p) => n0(p.customers), r: true }, { l: "Estimated cost", f: (p) => h("b", {}, money(p.cost?.total)), r: true }],
  [...packs, { label: "All bundles", ...total, cost: { total: total.total } }], null, { noScroll: true });
  const strong = (r, v) => r.key === "total" ? h("b", {}, v) : v;
  const breakdown = table([{ l: "Cost", f: (r) => strong(r, r.label) }, ...packs.map((p) => ({ l: p.label, f: (r) => strong(r, money(p.cost?.[r.key])), r: true })),
    { l: "Total", f: (r) => h("b", {}, money(packs.reduce((a, p) => a + (p.cost?.[r.key] || 0), 0))), r: true }],
  [...parts.map((k) => ({ key: k, label: cl[k] || k })), { key: "total", label: "Total" }], null, { noScroll: true });
  const pkgSelect = (r) => h("select", { onchange: (e) => edit("bundles", v, [{ item_id: r.id, field: "package", value: e.target.value }]) },
    keys.map((k) => h("option", { value: k, selected: r.package === k ? "selected" : null }, `Package ${k}`)),
    h("option", { value: "", selected: !r.package ? "selected" : null }, "Not in a package"),
    h("option", { value: NEW_PACKAGE }, "Create new package"));
  const cols = [
    { l: "Bundle", f: (r) => h("span", {}, r.feeder, h("span", { class: "muted" }, `, ${r.zone || ""}`)) },
    { l: "Isolation point", f: (r) => label(r.isolation) },
    { l: "Work", f: (r) => label(r.works) },
    { l: "Work items", f: (r) => n0(r.items), r: true },
    { l: "Crew hours", f: (r) => kh(r.crew_hours), r: true },
    { l: "Outage", f: (r) => r.outage_minutes ? `${(r.outage_minutes / 60).toFixed(1)} h` : "-", r: true },
    { l: "Customers off", f: (r) => n0(r.customers), r: true },
    ...CHECKS.map(([k, l]) => ({ l, f: (r) => checkChip(r.checks?.[k]) })),
    { l: "Estimated cost", f: (r) => costCell(r.cost), r: true },
    { l: "Package", f: (r) => pkgSelect(r) },
  ];
  return h("div", {}, h("div", { class: "plan-two" }, card("Packages", "What each package holds.", packTable), card("Estimated cost by package", "Amounts in AUD, 2026 dollars.", breakdown)),
    card("Bundles in the packages", "One planned outage per bundle.", table(cols, v.rows || [], null, { max: 560 })),
    deliveryOptions(v, f.options || []));
}

function deliveryOptions(v, options) {
  const ch = h("div", { class: "chart" });
  setTimeout(() => chart(ch, { grid: { left: 16, right: 90, top: 16, bottom: 40, containLabel: true },
    xAxis: axis({ type: "value", name: "Estimated cost (AUD)", nameLocation: "middle", nameGap: 28, axisLabel: { color: "#5f7185", formatter: (x) => x >= 1e6 ? `$${(x / 1e6).toFixed(1)}M` : `$${Math.round(x / 1e3)}k` } }),
    yAxis: { type: "category", data: options.map((o) => o.label), inverse: true },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, formatter: (p) => { const o = options[p[0].dataIndex];
      return `${o.label}<br>${money(o.cost_aud)}, ${n0(o.crew_hours)} crew hours<br>${n0(o.bundles)} ${noun(o.bundles, "bundle", "bundles")}, ${n0(o.weeks_needed)} ${noun(o.weeks_needed, "week", "weeks")} to finish.`; } },
    series: [{ type: "bar", barMaxWidth: 26, data: options.map((o) => ({ value: o.cost_aud, label: { show: true, position: "right", color: "#33424f", formatter: () => money(o.cost_aud) } })) }] }), 0);
  const cols = [
    { l: "Delivery option", f: (o) => h("span", {}, o.label, o.recommended ? pill("Recommended", "ok") : null) },
    { l: "Bundles", f: (o) => n0(o.bundles), r: true },
    { l: "Crew hours", f: (o) => kh(o.crew_hours), r: true },
    { l: "Estimated cost", f: (o) => money(o.cost_aud), r: true },
    { l: "Customers off", f: (o) => n0(o.customers_interrupted), r: true },
    { l: "Customer hours", f: (o) => n0(o.customer_hours), r: true },
    { l: "Weeks to finish", f: (o) => n0(o.weeks_needed), r: true },
    { l: "Locked window (weeks 1 and 2)", f: (o) => `${n0(o.locked_window_bundles)} bundles, ${kh(o.locked_window_hours)}`, r: true },
    { l: "Feasible", f: (o) => yesNo(o.feasible) },
    { l: "Select", f: (o) => h("input", { type: "radio", name: "scn", checked: o.selected ? "checked" : null, onchange: () => edit("bundles", v, [{ item_id: "__plan__", field: "scenario", value: o.id }]) }) },
  ];
  return card("Compare bundle delivery options", "Each package on its own, and all packages together. Updates as the packages change.",
    options.length ? h("div", {}, ch, table(cols, options, null, { noScroll: true })) : h("p", { class: "muted" }, "Put bundles in a package to compare how to deliver them."));
}

// ---------------------------------------------------------------- 5 release works plan
async function releaseTab(body) {
  let p;
  try { p = await api(`/api/planning/runs/${S.runId}/release-preview`); }
  catch (e) { body.append(card("Release works plan", "", h("p", { class: "muted" }, e.message))); return; }
  const keep = new Map(p.locked.map((x) => [x.bundle_id, x.included]));
  const summary = h("div", { class: "plan-sum" });
  const showSummary = () => {
    const xs = p.locked.filter((x) => keep.get(x.bundle_id));
    summary.innerHTML = "";
    summary.append(...[["Selected", `${n0(xs.length)} of ${n0(p.locked.length)} ${noun(p.locked.length, "bundle", "bundles")}`], ["Customers off", n0(xs.reduce((a, x) => a + x.customers, 0))],
      ["Life support", n0(xs.reduce((a, x) => a + x.life_support, 0))], ["Crew hours", kh(xs.reduce((a, x) => a + (x.crew_hours || 0), 0))],
      ["Estimated cost", money(xs.reduce((a, x) => a + (x.cost?.total || 0), 0))], ["Notices prepared", `${n0(xs.filter((x) => x.notice).length)} of ${n0(xs.length)}`]]
      .map(([k, val]) => h("div", {}, h("span", { class: "k" }, k), h("b", { class: "num" }, val))));
  };
  const boxes = [];
  const all = h("input", { type: "checkbox", title: "Select all", onchange: (e) => { for (const [b, x] of boxes) { b.checked = e.target.checked; keep.set(x.bundle_id, e.target.checked); } showSummary(); } });
  const tick = (x) => { const b = h("input", { type: "checkbox", checked: x.included ? "checked" : null, onchange: (e) => { keep.set(x.bundle_id, e.target.checked); showSummary(); } }); boxes.push([b, x]); return b; };
  const noticeChip = (x) => x.notice ? h("span", { class: `plan-chk ${x.notice.status === "sent" ? "ok" : "sched"}` }, x.notice.status === "scheduled" && x.notice.send_at ? `Scheduled for ${x.notice.send_at}` : x.notice.label)
    : h("span", { class: "muted small" }, "Not prepared");
  const cols = [
    { l: "", f: (x) => tick(x) },
    { l: "Outage", f: (x) => h("span", {}, dd(x.outage_date), h("div", { class: "muted small" }, x.window.replace(/^\S+ \S+ \S+ /, ""))) },
    { l: "Notice by", f: (x) => dd(x.notice_by) },
    { l: "Feeder", f: (x) => h("span", {}, x.feeder, h("div", { class: "muted small" }, `Package ${x.package}`)) },
    { l: "Isolation point", f: (x) => label(x.isolation) },
    { l: "Work", f: (x) => label(x.works) },
    { l: "Crew hours", f: (x) => kh(x.crew_hours), r: true },
    { l: "Customers off", f: (x) => n0(x.customers), r: true },
    { l: "Life support", f: (x) => n0(x.life_support), r: true },
    { l: "Estimated cost", f: (x) => money(x.cost?.total), r: true },
    { l: "Customer notice", f: (x) => noticeChip(x) },
  ];
  const chosen = () => p.locked.filter((x) => keep.get(x.bundle_id)).map((x) => x.bundle_id);
  const tbl = table(cols, p.locked, null, { max: 560 });
  tbl.querySelector("thead th")?.append(all);
  all.checked = p.locked.every((x) => x.included);
  showSummary();
  body.append(card("Works to release", `${p.scenario_label}. The locked window is weeks 1 and 2.`,
    h("p", { class: "muted" }, `Tick the bundles to send to the field. Each is raised as a field job on its outage day, crews are rostered to it seven days ahead, and its customers are notified ${p.notice_days} business days before. The first outage is ${dd(p.start)}, so every notice date is still ahead. ${n0(p.rolling_bundles)} later ${noun(p.rolling_bundles, "bundle remains", "bundles remain")} in the rolling twelve month plan.`),
    summary, tbl));
  const cl = p.cost_labels || {};
  const parts = ["labour", "materials", "plant", "traffic", "switching", "customer", "contingency"];
  body.append(card("Estimated cost of the works to release", "Amounts in AUD, 2026 dollars, for the bundles ticked when the page loaded.",
    table([{ l: "Cost", f: (r) => r.label }, { l: "Estimate", f: (r) => r.key === "total" ? h("b", {}, money(p.totals.cost.total)) : money(p.totals.cost[r.key]), r: true }],
      [...parts.map((k) => ({ key: k, label: cl[k] || k })), { key: "total", label: "Total" }], null, { noScroll: true })));
  body.append(h("div", { class: "plan-foot" },   // the step's actions close the page, at the bottom right
    p.saved_at ? h("span", { class: "muted small" }, `Last saved ${when(p.saved_at)}.`) : null,
    h("button", { class: "btn", onclick: (e) => saveLater(e.currentTarget, keep) }, "Save and continue later"),
    h("button", { class: "btn amber", onclick: () => confirmRelease(p, chosen()) }, "Release and schedule the plan")));
}

// A slow action: its button is disabled and the app is veiled (the lineworker at work) until the action returns, so it cannot be pressed twice.
async function busy(btn, msg, fn) {
  if (btn.disabled) return;
  btn.disabled = true;
  const wait = loading(msg);
  document.body.append(wait);
  try { await fn(); } finally { wait.remove(); btn.disabled = false; }
}

async function saveLater(btn, keep) {
  await busy(btn, "Saving the selection", async () => {
    try {
      await post(`/api/planning/runs/${S.runId}/selection`, { included: Object.fromEntries(keep) });
      toast("Saved. Open Plan and invest to continue from this step.");
      await plan("release");
    } catch (e) { toast(e.message, "red"); }
  });
}

function confirmRelease(p, ids) {
  if (!ids.length) return toast("Tick at least one bundle to release.");
  const xs = p.locked.filter((x) => ids.includes(x.bundle_id));
  const off = xs.reduce((a, x) => a + x.customers, 0);
  const send = h("input", { type: "checkbox", checked: "checked" });
  const bg = modal(h("h2", {}, "Release and schedule the plan?"),
    h("p", {}, `${n0(xs.length)} ${xs.length === 1 ? "bundle goes" : "bundles go"} to the field, with ${n0(off)} ${noun(off, "customer", "customers")} off across their outages, at an estimated ${money(xs.reduce((a, x) => a + (x.cost?.total || 0), 0))}. Each is raised as a field job on its outage day, and crews are rostered to it seven days ahead.`),
    h("label", { class: "plan-check" }, send, "Send customer notices"),
    h("p", { class: "muted small" }, "When ticked, each outage's customer notice is prepared and shown for review before the plan is released. Each notice is scheduled for its notice date. When cleared, the plan is released without customer notices."),
    h("div", { class: "plan-actions" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn amber", onclick: (e) => {
        if (!send.checked) return busy(e.currentTarget, "Releasing the plan", () => release(ids, false, [bg]));
        return busy(e.currentTarget, "Preparing customer notices", async () => {
          try {
            if (xs.some((x) => !x.notice)) await post(`/api/planning/runs/${S.runId}/prepare-notices`, { bundle_ids: ids });
            const q = await api(`/api/planning/runs/${S.runId}/release-preview`);
            reviewNotices(q.locked.filter((x) => ids.includes(x.bundle_id) && x.notice), ids, bg);
          } catch (err) { toast(err.message, "red"); }
        });
      } }, "Release and schedule the plan")));
}

// The customer notices of the plan, for review before it is released: one outage at a time, its messages editable. Accepting saves the
// edits, releases the plan with its notices, and closes both windows.
function reviewNotices(items, ids, releaseWin) {
  if (!items.length) return toast("No customer notice could be prepared for these outages.", "red");
  let ed = null;
  const pane = h("div", { class: "plan-review" });
  const show = async (i) => {
    if (ed) await ed.save();
    pane.innerHTML = "";
    pane.append(loading("Opening the notice"));
    try { ed = await noticeEditor(items[i].notice.id); pane.innerHTML = ""; pane.append(ed.el); }
    catch (err) { pane.innerHTML = ""; pane.append(h("p", { class: "plan-err" }, err.message)); ed = null; }
  };
  const pick = h("select", { class: "plan-pick", onchange: (e) => show(+e.target.value).catch((err) => toast(err.message, "red")) },
    items.map((x, i) => h("option", { value: i }, `${dd(x.outage_date)}, ${x.feeder}, ${n0(x.customers)} ${noun(x.customers, "customer", "customers")}`)));
  const bg = modal(h("h2", {}, "Review the customer notices"),
    h("p", { class: "muted" }, `${items.length === 1 ? "The outage has" : `Each of the ${n0(items.length)} outages has`} its own notice. Choose an outage to review or edit its messages; edits are saved with the notice.`),
    h("label", { class: "plan-bar-l" }, "Outage", pick), pane,
    h("div", { class: "plan-actions" }, h("button", { class: "btn", onclick: async () => { if (ed) await ed.save().catch(() => {}); bg.remove(); } }, "Back"),
      h("button", { class: "btn amber", onclick: (e) => busy(e.currentTarget, "Releasing the plan", async () => {
        try { if (ed) await ed.save(); } catch (err) { return toast(err.message, "red"); }
        await release(ids, true, [bg, releaseWin]);
      }) }, "Accept the notices and release")));
  bg.querySelector(".modal").classList.add("plan-review-win");
  show(0);
}

async function release(ids, notices, wins) {
  try {
    const r = await post(`/api/planning/runs/${S.runId}/release`, { bundle_ids: ids, notices });
    wins.forEach((w) => w.remove());
    localStorage.removeItem(RUN_KEY);
    toast(`Released: ${r.items} ${r.items === 1 ? "bundle" : "bundles"}${r.failed.length ? `, ${r.failed.length} could not be raised` : ""}${notices ? `, with ${r.notices} customer ${r.notices === 1 ? "notice" : "notices"} scheduled` : ", without customer notices"}.`);
    location.hash = `#/plan/released/${r.release_id}`;
  } catch (e) { toast(e.message, "red"); }
}

// ---------------------------------------------------------------- Released Plans
async function releasedTab(id) {
  const body = h("div", { class: "plan-body" });
  view.innerHTML = "";
  view.append(header(), groupRow("released"), body);
  if (id) return releasedPlan(body, id);
  const d = await api("/api/planning/released");
  const lab = d.labels || {};
  if (!d.plans.length) {
    body.append(card("Released plans", "", h("p", { class: "muted" }, "No plan has been released yet. Release one from the last step of Design Work Programmes.")));
    return;
  }
  const stages = (r) => h("span", { class: "plan-stages" }, ["upcoming", "rostered", "in_field", "complete"].filter((k) => r.stages[k])
    .map((k) => h("span", { class: `plan-stage s-${k}` }, `${n0(r.stages[k])} ${(lab[k] || k).toLowerCase()}`)));
  body.append(card("Released plans", "Listed newest first.", table([
    { l: "Plan", f: (r) => h("b", {}, r.title) },
    { l: "Released", f: (r) => h("span", {}, when(r.released_at), h("div", { class: "muted small" }, (r.released_by || "").split("@")[0])) },
    { l: "Delivery option", f: (r) => label(r.scenario_label) },
    { l: "Planned outages", f: (r) => h("span", {}, `${n0(r.items)} ${r.items === 1 ? "outage" : "outages"}`,
        h("div", { class: "muted small" }, `Across ${n0(r.feeders)} ${r.feeders === 1 ? "feeder" : "feeders"} over ${n0(r.days)} ${r.days === 1 ? "day" : "days"}.`)), r: true },
    { l: "Customers off", f: (r) => n0(r.customers), r: true },
    { l: "Estimated cost", f: (r) => money(r.cost_total_aud), r: true },
    { l: "Outage days", f: (r) => `${dd(r.first_outage)} to ${dd(r.last_outage)}` },
    { l: "Progress", f: (r) => stages(r) },
    { l: "", f: (r) => h("button", { class: "btn small", onclick: (e) => { e.stopPropagation(); deleteRelease(r); } }, "Delete") },
  ], d.plans, (r) => { location.hash = `#/plan/released/${r.release_id}`; })));
}

// Delete a released plan and everything it made (the server removes its field jobs, notices and messages, and its programme)
function deleteRelease(r) {
  const bg = modal(h("h2", {}, `Delete ${r.title}?`),
    h("p", {}, `This removes the released plan and everything behind it: the ${n0(r.items)} field ${r.items === 1 ? "job" : "jobs"} raised for its outages, `
      + "with any crew rostered to them and anything reported on them; its customer notices that have not been sent; "
      + "and the programme it was designed in, with its drafts and decisions. Notices already sent are kept as a record. It cannot be undone."),
    h("div", { class: "plan-actions" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn red", onclick: async (e) => {
        e.target.disabled = true; e.target.textContent = "Deleting";
        try {
          const x = await api(`/api/planning/released/${r.release_id}`, { method: "DELETE" });
          bg.remove();
          toast(`${r.title} has been deleted, with ${n0(x.jobs_removed)} field ${x.jobs_removed === 1 ? "job" : "jobs"} and ${n0(x.notices_removed)} unsent ${x.notices_removed === 1 ? "notice" : "notices"}.`);
          plan("released");
        } catch (err) { toast(err.message, "red"); e.target.disabled = false; e.target.textContent = "Delete plan"; }
      } }, "Delete plan")));
}

async function releasedPlan(body, id) {
  const d = await api(`/api/planning/released/${id}`);
  const p = d.plan;
  body.append(h("div", { class: "plan-actions" }, h("a", { href: "#/plan/released", class: "btn small" }, "All released plans"),
    h("span", { class: "muted" }, `${p.title}, released ${when(p.released_at)} by ${(p.released_by || "").split("@")[0]}`)));
  const tile = (k, v, s) => h("div", { class: "tile" }, h("div", { class: "k" }, k), h("div", { class: "v num" }, v), s ? h("div", { class: "s" }, s) : null);
  body.append(h("div", { class: "plan-tiles" },
    tile("Plan cost", money(d.cost.total), "Estimate in AUD, 2026 dollars."),
    tile("Planned outages", n0(p.items), `${dd(p.first_outage)} to ${dd(p.last_outage)}`),
    tile("Customers off", n0(p.customers), `${n0(p.life_support)} ${noun(p.life_support, "customer", "customers")} on life support.`),
    tile("Crew hours", kh(p.crew_hours), p.scenario_label || ""),
    tile("Customer notices", n0(d.notices), `For ${n0(p.items)} ${p.items === 1 ? "outage" : "outages"}.`)));
  body.append(h("div", { class: "plan-three" },
    card("Cost by category", "", table([{ l: "Cost", f: (r) => r.total ? h("b", {}, r.label) : r.label }, { l: "Estimate", f: (r) => r.total ? h("b", {}, money(r.aud)) : money(r.aud), r: true }],
      [...d.cost.categories, { label: "Total", aud: d.cost.total, total: true }], null, { noScroll: true })),
    card("Cost by package", "", table([{ l: "Package", k: "label" }, { l: "Estimate", f: (r) => money(r.aud), r: true }], d.cost.packages, null, { noScroll: true })),
    card("Cost by region", "", table([{ l: "Region", k: "label" }, { l: "Estimate", f: (r) => money(r.aud), r: true }], d.cost.regions, null, { noScroll: true }))));
  const col = (s) => {
    const items = d.items.filter((i) => i.stage === s.key);
    return h("div", { class: `kb-col s-${s.key}` }, h("div", { class: "kb-h" }, h("span", {}, s.label), h("b", {}, n0(items.length))),
      items.length ? items.map(kbCard) : h("div", { class: "kb-empty" }, "Nothing at this stage."));
  };
  body.append(card("The plan's work", `Grouped by stage. Crews are rostered ${d.roster_days} days ahead.`, h("div", { class: "kb" }, d.stages.map(col))));
}

function kbCard(i) {
  const crew = i.crews.length ? `${i.crews[0]}${i.crews.length > 1 ? ` and ${i.crews.length - 1} more crew${i.crews.length > 2 ? "s" : ""}` : ""}` : null;
  return h("div", { class: "kb-card", title: `${i.works}\nIsolation: ${i.isolation}` },
    h("div", { class: "kb-date" }, i.window || dd(i.outage_date)),
    h("div", { class: "kb-title" }, i.feeder),
    h("div", { class: "kb-meta" }, `${n0(i.customers)} ${noun(i.customers, "customer", "customers")}`, h("span", {}, money(i.cost))),
    h("div", { class: crew ? "kb-crew" : "kb-crew muted" }, crew || `${i.crews_needed} crew${i.crews_needed === 1 ? "" : "s"} to roster`),
    i.notice ? h("span", { class: `plan-chk ${i.notice.status === "sent" ? "ok" : "sched"}` }, i.notice.status === "scheduled" && i.notice.send_at ? `Notice scheduled for ${i.notice.send_at}` : `Notice ${i.notice.label.toLowerCase()}`) : null);
}
