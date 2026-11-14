// Vegetation management (feature 6), route #/veg/<tab>. Two tabs: the region summary (the latest LiDAR campaign by region, the bays on
// the map and the risk by feeder), and Vegetation Work Management with three steps as sub-tabs: create and issue work (a wizard that
// builds one work package for one contractor crew), the released work packages (whatever their status), and audit and validate (the
// post-cut clearance and the photos of the cut: accept a compliant bay, or reissue a non-compliant one). Contractors work their bays in
// the contractor's field app at /contractor. Data comes from /api/vegetation (app/routes_vegetation.py).
import { api, post, h, $, n0, n1, n2, st, tile, card, table, mapIn, drawer, toast, modal } from "./ui.js";
import { openLidar, warmLidar } from "./lidar.js";

const view = $("#view");
// two tabs: the region summary, and the vegetation work itself in four steps as sub-tabs (each step keeps its own address, #/veg/<step>)
const WORK = [["issue", "Create and issue work"], ["released", "Released work packages"], ["audit", "Audit and validate"]];
const TABS = [["capture", "Region summary", ["capture"]], ["issue", "Vegetation Work Management", WORK.map(([k]) => k)]];
const Q = {
  capture: "LiDAR survey coverage of every bay, and how the clearance to vegetation has moved between campaigns.",
  issue: "Build a work package of bays to cut, step by step, and issue it to one contractor crew.",
  released: "Every work package issued to a contractor, whether it is not started, in progress or complete.",
  audit: "Whether each cut meets the required clearance, with each bay accepted or reissued to the contractor.",
};
const S = { zones: null, result: "pending", released: "all" };
const plural = (n, one, many) => `${n0(n)} ${Number(n) === 1 ? one : many}`;
const dte = (d) => d ? new Date(d).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric" }) : "-";
const dts = (d) => d ? new Date(d).toLocaleString("en-AU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "-";
const pctTxt = (v) => (v === null || v === undefined) ? "-" : `${n1(v)}%`;
const chip = (label, cls = "") => h("span", { class: `chip veg-chip ${cls}` }, label || "-");
const tierChip = (tier, label) => chip(label, `veg-${tier || "none"}`);
// every bay is an overhead span: its id is the span's (S212149), its label BAY- and the span number
const spanOf = (r) => r.bay_id || (r.bay_label ? `S${String(r.bay_label).replace(/^BAY-/, "")}` : null);
const lidarBtn = (r, opts) => h("button", { class: "btn small veg-lidar", type: "button", "data-tip": "The span as a LiDAR survey sees it.",
  onclick: (e) => { e.stopPropagation(); openLidar(spanOf(r), opts); } }, "LiDAR view");
const lidarCol = (opts) => ({ l: "", f: (r) => lidarBtn(r, opts && opts(r)) });
const cutOf = (r) => ({ cut: { before: r.clearance_before_m, after: r.post_cut_clearance_m, attempt: r.attempt } });   // a completed cut: before and after

async function zoneList() {
  S.zones ||= (await api("/api/scene-sites")).filter((z) => z.kind === "zone").map((z) => [z.zs_id, z.name.replace(" zone substation", "")]);
  return S.zones;
}
const zoneName = (id) => (S.zones || []).find(([k]) => k === id)?.[1] || id || "-";

// ---------------------------------------------------------------- shell
export async function vegetation(tab = "capture") {
  if (!["capture", ...WORK.map(([k]) => k)].includes(tab)) tab = "capture";   // an unknown step opens the region summary
  await zoneList();
  warmLidar();   // the LiDAR view's models load in the background, so a span opens without waiting for them
  view.innerHTML = "";
  view.append(h("div", { class: "screen-h" }, h("h1", {}, "Vegetation management"), h("span", { class: "q" }, Q[tab] || Q.capture),
    h("div", { class: "right" }, tab === "capture" ? h("button", { class: "btn", type: "button", onclick: campaignHistory }, "Campaign history") : null,
      h("a", { class: "btn", href: "/contractor", target: "_blank" }, "Contractor app"))),
    h("div", { class: "tabs" }, TABS.map(([k, l, has]) => h("a", { href: `#/veg/${k}`, class: has.includes(tab) ? "on" : "" }, l))));
  const body = h("div", { class: "tabbody veg" });
  view.append(body);
  if (tab !== "capture") body.append(h("div", { class: "nt-subtabs veg-subtabs" }, WORK.map(([k, l]) => h("a", { href: `#/veg/${k}`, class: k === tab ? "on" : "" }, l))));
  const fn = { capture, issue, released, audit }[tab];
  try {
    await fn(body);
  } catch (e) {
    body.innerHTML = "";
    body.append(h("p", { class: "veg-error" }, `This step could not load: ${e.message}.`));
  }
}

// ---------------------------------------------------------------- 1 capture and model
// The latest campaign at a glance (its figures, then the bays in each risk tier), and the results by zone substation. How a bay and its
// clearance are defined is in the note on "Inside the clearance now"; every campaign's figures are behind the Campaign history button.
const TIER_TILES = [["high_risk", "red"], ["cut_required", "amber"], ["planned", ""], ["no_action", "green"]];
async function capture(body) {
  const d = await api("/api/vegetation/capture");
  S.capture = d;
  const last = d.campaigns.at(-1) || {};
  const tiers = Object.fromEntries(d.tiers.map((t) => [t.risk_tier, t]));
  const inside = tile("Inside the clearance now", n0(last.intrusions), `Required clearance is ${n0(d.required_m.general)} m on general spans and ${n0(d.required_m.bushfire_prone)} m on bushfire-prone land.`, "red");
  inside.dataset.more = ["How a bay and its clearance are defined:", ...Object.values(d.model).map((t) => `• ${t}`),
    "The demo standard is to be replaced by the distributor's own."].join("\n");
  body.append(h("div", { class: "glance" },
    tile("Overhead spans surveyed", n0(last.surveyed_spans), `${pctTxt(last.coverage_pct)} of ${n0(last.overhead_spans)} spans in the latest campaign.`),
    inside,
    tile("Vegetation grew in", n0(last.spans_grown_in), "Spans whose clearance fell since the previous campaign.", "amber"),
    tile("Cut back since then", n0(last.spans_cut_back), "Spans whose clearance rose by at least 1 m since the previous campaign.", "green")));
  // the bays in each risk tier of the latest campaign, most urgent first; a tier the data does not hold is left out
  body.append(h("div", { class: "glance" }, TIER_TILES.filter(([k]) => tiers[k]).map(([k, cls]) => {
    const t = tiers[k];
    return tile(t.risk_tier_label, n0(t.bays), `${n0(t.bushfire_bays)} of them on bushfire-prone land.`, cls);
  })));
  // results by region (how the cutting programme is planned and contracted), with the bushfire-prone split
  body.append(card("Region results from latest campaign", "Coverage and clearance by region, with bushfire-prone land shown on its own. Listed with the most high risk bays first. Select a region for its feeders.",
    table([{ l: "Region", k: "area_label" }, { l: "Feeders", f: (r) => n0(r.feeders), r: 1 }, { l: "Overhead spans", f: (r) => n0(r.overhead_spans), r: 1 },
      { l: "Coverage", f: (r) => pctTxt(r.coverage_pct), r: 1 }, { l: "Inside clearance", f: (r) => n0(r.intrusions), r: 1 },
      { l: "Bushfire-prone bays", f: (r) => n0(r.bushfire_bays), r: 1 }, { l: "Bushfire-prone inside", f: (r) => n0(r.bushfire_intrusions), r: 1 },
      { l: "High risk", f: (r) => n0(r.high_risk), r: 1 }, { l: "Grew in", f: (r) => n0(r.spans_grown_in), r: 1 }, { l: "Cut back", f: (r) => n0(r.spans_cut_back), r: 1 }],
    d.regions, (r) => regionDrawer(r), { noScroll: 1 })));
  await riskCards(body);
}

// The drill-down from a region: its figures, then its feeders (most urgent first), each opening its bays and from there the LiDAR view
async function regionDrawer(rg) {
  drawer(h("h2", {}, rg.area_label), h("p", { class: "muted" }, "Region, latest LiDAR campaign."),
    h("div", { class: "kv", style: "margin:12px 0" }, ...[["Feeders", n0(rg.feeders)], ["Overhead spans", n0(rg.overhead_spans)], ["Coverage", pctTxt(rg.coverage_pct)],
      ["Inside clearance", n0(rg.intrusions)], ["On bushfire-prone land", `${n0(rg.bushfire_bays)} bays, ${n0(rg.bushfire_intrusions)} of them inside the clearance`],
      ["High risk", n0(rg.high_risk)], ["Predicted grow-in", n0(rg.grow_in)]].flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)])),
    h("h4", {}, "Feeders"), h("p", { class: "muted" }, "Reading the feeders."));
  const f = (await api(`/api/vegetation/region/${encodeURIComponent(rg.area)}/feeders`)).feeders;
  const back = { label: `‹ Back to the ${rg.area_label} feeders`, go: () => regionDrawer(rg) };
  $("#drawerBody").lastChild.replaceWith(h("div", {}, h("p", { class: "muted small" }, `${plural(f.length, "feeder", "feeders")}, listed with high risk bays first. Select a feeder for its bays.`),
    table([{ l: "Feeder", k: "feeder_id" }, { l: "Zone substation", f: (r) => zoneName(r.zs_id) }, { l: "Bays", f: (r) => n0(r.bays), r: 1 },
      { l: "High risk", f: (r) => n0(r.high_risk), r: 1 }, { l: "Cut required", f: (r) => n0(r.cut_required), r: 1 }, { l: "Bushfire-prone", f: (r) => n0(r.bushfire_bays), r: 1 }],
    f, (r) => feederDrawer(r, back), { noScroll: 1 })));
}

// a feeder's bays, most urgent first; back returns to the region's feeders
async function feederDrawer(fd, back) {
  drawer(h("div", { class: "veg-crumb" }, h("button", { class: "btn small", type: "button", onclick: back.go }, back.label)),
    h("h2", {}, fd.feeder_id), h("p", { class: "muted" }, "Reading the feeder's bays."));
  const rows = (await api(`/api/vegetation/feeder/${encodeURIComponent(fd.feeder_id)}/spans`)).spans;
  const order = { high_risk: 1, cut_required: 2, planned: 3, no_action: 4 };
  const worst = [...rows].sort((a, b) => (order[a.risk_tier] || 5) - (order[b.risk_tier] || 5) || a.clearance_m - b.clearance_m);
  $("#drawerBody").lastChild.replaceWith(h("div", {},
    h("p", { class: "muted small" }, `${plural(rows.length, "bay", "bays")}, listed with high risk first, then by clearance, smallest first. Select a bay for its LiDAR view; inside the view the line can be walked span by span.`),
    table([{ l: "Bay", k: "bay_label" }, { l: "Risk tier", f: (r) => tierChip(r.risk_tier, r.risk_tier_label) }, { l: "Clearance m", f: (r) => n2(r.clearance_m), r: 1 },
      { l: "In 12 months m", f: (r) => n2(r.clearance_12m_m), r: 1 }, { l: "Required m", f: (r) => n1(r.required_clearance_m), r: 1 }],
    worst, (r) => openLidar(r.bay_id), { max: 560 })));
}

// Every LiDAR campaign's coverage and clearance, in a window from the button at the top right of the first step
async function campaignHistory() {
  const d = S.capture || await api("/api/vegetation/capture");
  const bg = modal(h("h2", {}, "Campaign history"), h("p", { class: "muted" }, `${plural(d.campaigns.length, "annual LiDAR campaign", "annual LiDAR campaigns")}, oldest first. The point cloud is not shown; only the clearance each campaign produced is shown.`),
    card("LiDAR survey coverage by campaign", "",
      table([{ l: "Campaign", k: "campaign_year" }, { l: "Captured", f: (r) => `${dte(r.capture_start)} to ${dte(r.capture_end)}` },
        { l: "Spans surveyed", f: (r) => n0(r.surveyed_spans), r: 1 }, { l: "Coverage", f: (r) => pctTxt(r.coverage_pct), r: 1 },
        { l: "Not surveyed", f: (r) => n0(r.not_surveyed_spans), r: 1 }, { l: "Inside clearance", f: (r) => n0(r.intrusions), r: 1 },
        { l: "Grew in", f: (r) => n0(r.spans_grown_in), r: 1 }, { l: "Cut back", f: (r) => n0(r.spans_cut_back), r: 1 },
        { l: "Average clearance m", f: (r) => n2(r.avg_clearance_m), r: 1 }], d.campaigns, null, { noScroll: 1 })),
    h("div", { class: "row" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Close")));
  bg.querySelector(".modal").classList.add("veg-history");
}

// The bays on the map (the riskiest bays, coloured by tier) and the risk by feeder, for the whole network
async function riskCards(body) {
  const d = await api("/api/vegetation/risk?limit=200");
  const mapEl = h("div", { class: "veg-map" });
  body.append(card("Bays on the map", "Each dot is a bay. Colour shows its risk tier. Select a bay for its detail.", mapEl,
    h("div", { class: "veg-legend" }, chip("High risk", "veg-high_risk"), chip("Cut required", "veg-cut_required"), chip("Planned", "veg-planned"))));
  const pts = d.bays.filter((b) => b.lat && b.lon);
  const center = pts.length ? [pts.reduce((a, b) => a + b.lat, 0) / pts.length, pts.reduce((a, b) => a + b.lon, 0) / pts.length] : undefined;
  const m = mapIn(mapEl, center || [-33.65, 150.85], 9);
  const COL = { high_risk: "#d2462f", cut_required: "#c98a1e", planned: "#2f7dc4" };
  for (const b of pts) {
    L.circleMarker([b.lat, b.lon], { radius: 5, color: "#fff", weight: 1, fillColor: COL[b.risk_tier] || "#5f7185", fillOpacity: .9 })
      .bindTooltip(`${b.bay_label}, ${b.risk_tier_label}, clearance ${n2(b.clearance_m)} m`).on("click", () => bayDrawer(b)).addTo(m);
  }
  body.append(card("Risk by feeder", "Feeders with a high risk bay are listed first.",
    table([{ l: "Feeder", k: "feeder_id" }, { l: "Zone substation", f: (r) => zoneName(r.zs_id) }, { l: "Bays", f: (r) => n0(r.bays), r: 1 },
      { l: "Inside clearance", f: (r) => n0(r.current_intrusions), r: 1 }, { l: "High risk", f: (r) => n0(r.high_risk), r: 1 },
      { l: "Cut required", f: (r) => n0(r.cut_required), r: 1 }, { l: "Planned", f: (r) => n0(r.planned), r: 1 },
      { l: "Bushfire-prone", f: (r) => n0(r.bushfire_bays), r: 1 }, { l: "Hazard trees", f: (r) => n0(r.hazard_trees), r: 1 },
      { l: "Customers at intrusion", f: (r) => n0(r.customers_at_intrusion), r: 1 }, { l: "Status", f: (r) => st(r.feeder_status) }],
    d.feeders, null, { max: 320 })));
}

function bayDrawer(b) {
  drawer(h("h4", {}, b.bay_label), h("p", { class: "muted" }, `${b.feeder_id}, ${b.classification_label}`),
    h("div", { class: "kv" }, h("div", { class: "k" }, "Risk tier"), h("div", {}, b.risk_tier_label),
      h("div", { class: "k" }, "Clearance now"), h("div", {}, `${n2(b.clearance_m)} m`),
      h("div", { class: "k" }, "In 12 months"), h("div", {}, `${n2(b.clearance_12m_m)} m`),
      h("div", { class: "k" }, "Required"), h("div", {}, `${n1(b.required_clearance_m)} m (${b.required_basis_label || b.required_basis})`),
      h("div", { class: "k" }, "Species"), h("div", {}, b.dominant_species || "-"),
      h("div", { class: "k" }, "Hazard tree"), h("div", {}, b.hazard_tree ? "Yes" : "No"),
      h("div", { class: "k" }, "Customers downstream"), h("div", {}, n0(b.customers_downstream))),
    h("div", { class: "veg-actions" }, h("button", { class: "btn primary", onclick: () => { W.sel.set(b.bay_id, b); location.hash = "#/veg/issue"; } },
      "Add to a work package"), lidarBtn(b)));
}

// ---------------------------------------------------------------- create and issue work: the package wizard
// Three steps: the bays (in seasonal plan order), the contractor crew, then the review and issue. The wizard keeps its place while the
// screen is open; leaving it part way through asks only to discard the package or to continue building it.
const STEPS = ["Choose the bays", "Choose the contractor crew", "Review and issue the package"];
const W = { step: 1, sel: new Map(), crew: null, title: "", due: "", notes: "", done: null };
const wizardBusy = () => !W.done && (W.step > 1 || W.sel.size > 0 || !!W.crew);
function resetWizard() { Object.assign(W, { step: 1, sel: new Map(), crew: null, title: "", due: "", notes: "", done: null }); }

// called by the router before it leaves #/veg/issue: true when it has asked (the address stays until the planner answers)
export function leaveVegWizard(from, to, go) {
  if (from !== "#/veg/issue" || to === from || !wizardBusy()) return false;
  const bg = modal(h("h2", {}, "Leave this work package?"),
    h("p", {}, `The package has not been issued${W.sel.size ? ` (${plural(W.sel.size, "bay is", "bays are")} selected)` : ""}. Discard it, or continue building it.`),
    h("div", { class: "veg-actions veg-end" }, h("button", { class: "btn", onclick: () => { resetWizard(); bg.remove(); go(); } }, "Discard"),
      h("button", { class: "btn primary", onclick: () => bg.remove() }, "Continue")));
  return true;
}

async function issue(body) {
  if (W.done) return issued(body);
  const box = h("div", { class: "card veg-wiz" });
  body.append(box);
  const head = h("div", { class: "veg-wiz-h" }, h("div", { class: "veg-wiz-step" }, `Step ${W.step} of ${STEPS.length}`), h("h3", {}, `${STEPS[W.step - 1]}.`));
  const foot = h("div", { class: "veg-wiz-foot" });
  const back = W.step > 1 ? h("button", { class: "btn", onclick: () => { W.step -= 1; vegetation("issue"); } }, "Back") : null;
  const discard = wizardBusy() ? h("button", { class: "btn ghost", onclick: () => { resetWizard(); vegetation("issue"); } }, "Discard") : null;
  const next = (label, ok, why) => h("button", { class: "btn primary", onclick: () => { if (!ok()) { toast(why, "bad"); return; } W.step += 1; vegetation("issue"); } }, label);
  const content = h("div", {});
  box.append(head, content, foot);
  if (W.step === 1) {
    const cands = (await api("/api/vegetation/candidates?limit=300")).bays;
    const count = h("b", {}), mix = h("span", { class: "muted small" });
    const paintSel = () => {
      const rows = [...W.sel.values()], by = (t) => rows.filter((r) => r.risk_tier === t).length;
      count.textContent = `${plural(rows.length, "bay", "bays")} selected.`;
      mix.textContent = rows.length ? ` ${n0(by("high_risk"))} high risk, ${n0(by("cut_required"))} cut required and ${n0(by("planned"))} planned.` : "";
    };
    // ticking changes only the box and the count: the list is not read again or redrawn
    const boxes = new Map();
    const tick = (r, on) => { if (on) W.sel.set(r.bay_id, r); else W.sel.delete(r.bay_id); const b = boxes.get(r.bay_id); if (b) b.checked = on; paintSel(); };
    const shown = [...W.sel.values()].filter((r) => !cands.some((c) => c.bay_id === r.bay_id)).concat(cands);   // a bay chosen elsewhere stays listed
    content.append(h("p", { class: "muted" }, `${plural(cands.length, "bay can", "bays can")} be issued, listed in the seasonal plan order: high risk first, then cut required, then planned grow-in. Bays already on an open task are not listed. Tick the bays to put in the package.`),
      h("div", { class: "veg-filters" }, h("span", {}, count, mix),
        h("button", { class: "btn small", onclick: () => cands.forEach((r) => tick(r, true)) }, "Select all shown"),
        h("button", { class: "btn small", onclick: () => shown.forEach((r) => tick(r, false)) }, "Clear the selection")),
      table([{ l: "", f: (r) => { const b = h("input", { type: "checkbox", checked: W.sel.has(r.bay_id) ? "" : null, onclick: (e) => { e.stopPropagation(); tick(r, e.target.checked); } }); boxes.set(r.bay_id, b); return b; } },
        { l: "Bay", k: "bay_label" }, { l: "Span length m", f: (r) => (r.span_length_m == null ? "-" : n0(r.span_length_m)), r: 1 },
        { l: "Feeder", k: "feeder_id" }, { l: "Spans on feeder", f: (r) => (r.feeder_spans == null ? "-" : `${n0(r.feeder_spans)}, ${n0(r.feeder_spans_to_cut)} to cut`), r: 1 },
        { l: "Risk tier", f: (r) => tierChip(r.risk_tier, r.risk_tier_label) },
        { l: "Clearance m", f: (r) => n2(r.clearance_m), r: 1 }, { l: "Required m", f: (r) => n1(r.required_clearance_m), r: 1 },
        { l: "Plan order", f: (r) => r.plan_seq ?? "-", r: 1 }, { l: "Planned month", f: (r) => r.plan_month_label || "-" }, { l: "Customers", f: (r) => n0(r.customers_downstream), r: 1 }, lidarCol()],
      shown, (r) => tick(r, !W.sel.has(r.bay_id)), { max: 460 }));
    paintSel();
    foot.append(h("span", {}, back, discard), h("span", {}, next("Next", () => W.sel.size > 0, "Tick at least one bay for the package.")));
  } else if (W.step === 2) {
    const crews = (await api("/api/vegetation/contractors")).crews, radios = new Map();   // choosing a crew sets its button, without a redraw
    content.append(h("p", { class: "muted" }, `${plural(crews.length, "contractor crew is", "contractor crews are")} listed by region and sub-region. Their open work is shown, so the package goes to a crew with room for it. Select a crew.`),
      table([{ l: "", f: (r) => { const b = h("input", { type: "radio", name: "veg-crew", checked: W.crew === r.crew_id ? "" : null, onclick: (e) => { e.stopPropagation(); W.crew = r.crew_id; } }); radios.set(r.crew_id, b); return b; } },
        { l: "Crew", f: (r) => h("span", {}, h("b", {}, r.crew_name), h("div", { class: "muted small" }, r.crew_id)) }, { l: "Sub-region", f: (r) => `${r.region_name}, ${r.area_name}` },
        { l: "Crew size", f: (r) => n0(r.crew_size), r: 1 }, { l: "Open packages", f: (r) => n0(r.open_packages), r: 1 }, { l: "Bays to do", f: (r) => n0(r.to_do), r: 1 },
        { l: "Awaiting audit", f: (r) => n0(r.awaiting_audit), r: 1 }, { l: "Next due", f: (r) => dte(r.next_due) }],
      crews, (r) => { W.crew = r.crew_id; radios.get(r.crew_id).checked = true; }, { max: 460 }));
    foot.append(h("span", {}, back, discard), h("span", {}, next("Next", () => !!W.crew, "Select the contractor crew for the package.")));
  } else {
    const crews = (await api("/api/vegetation/contractors")).crews, crew = crews.find((c) => c.crew_id === W.crew) || {};
    const rows = [...W.sel.values()], by = (t) => rows.filter((r) => r.risk_tier === t).length;
    const top = by("high_risk") ? "high_risk" : by("cut_required") ? "cut_required" : "planned";
    const dueDays = { high_risk: 30, cut_required: 60, planned: 90 }[top];
    if (!W.due) W.due = new Date(Date.now() + dueDays * 86400000).toISOString().slice(0, 10);
    if (!W.title) W.title = `${crew.region_name || ""} ${{ high_risk: "high risk", cut_required: "cut required", planned: "planned" }[top]}, ${plural(rows.length, "bay", "bays")}`.trim();
    const title = h("input", { type: "text", class: "veg-input", value: W.title, oninput: (e) => { W.title = e.target.value; } });
    const due = h("input", { type: "date", class: "veg-input", value: W.due, oninput: (e) => { W.due = e.target.value; } });
    const notes = h("textarea", { class: "veg-input", rows: "3", placeholder: "Notes for the crew, such as access, permits or school zones", oninput: (e) => { W.notes = e.target.value; } }, W.notes);
    const feeders = [...new Set(rows.map((r) => r.feeder_id))];
    content.append(h("div", { class: "veg-two" },
      h("div", {}, h("h4", {}, "The package"), h("div", { class: "kv" }, ...[["Bays", `${plural(rows.length, "bay", "bays")}: ${n0(by("high_risk"))} high risk, ${n0(by("cut_required"))} cut required and ${n0(by("planned"))} planned`],
        ["Bushfire-prone land", plural(rows.filter((r) => r.bushfire_prone).length, "bay", "bays")], ["Feeders", feeders.slice(0, 6).join(", ") + (feeders.length > 6 ? `, and ${feeders.length - 6} more` : "")],
        ["Contractor crew", `${crew.crew_name || W.crew}, ${crew.region_name || ""}`], ["Crew's open work", `${plural(crew.to_do || 0, "bay", "bays")} still to do`]].flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)]))),
      h("div", {}, h("h4", {}, "Details for the crew"), h("div", { class: "veg-form" }, h("label", {}, "Title", title), h("label", {}, "Due date", due), h("label", {}, "Notes", notes)),
        h("p", { class: "muted small" }, `The due date is set ${dueDays} days out for a package whose most urgent bay is ${{ high_risk: "high risk", cut_required: "cut required", planned: "planned" }[top]}. Change it if needed.`))));
    const go = h("button", { class: "btn primary", onclick: async () => {
      go.disabled = true;
      try {
        const r = await post("/api/vegetation/packages", { crew_id: W.crew, bay_ids: [...W.sel.keys()], title: W.title, due_date: W.due || null, notes: W.notes });
        resetWizard(); W.done = r;
        vegetation("issue");
      } catch (e) { go.disabled = false; toast(e.message, "bad"); }
    } }, "Issue the package");
    foot.append(h("span", {}, back, discard), h("span", {}, go));
  }
}

function issued(body) {   // after issuing: what went out, and where to follow it
  const r = W.done;
  body.append(h("div", { class: "card veg-wiz" }, h("div", { class: "veg-wiz-h" }, h("h3", {}, "Package issued.")),
    h("p", {}, `${r.title} is issued to ${r.crew_name}: ${plural(r.bays, "bay", "bays")}, due on ${dte(r.due_date)}. The crew sees the bays in its field app now.`),
    h("div", { class: "veg-wiz-foot" }, h("span", {}), h("span", {},
      h("button", { class: "btn", onclick: () => { resetWizard(); vegetation("issue"); } }, "Create another package"),
      h("button", { class: "btn primary", onclick: () => { resetWizard(); location.hash = "#/veg/released"; } }, "See it under Released work packages")))));
}

// ---------------------------------------------------------------- released work packages
// Every package issued to a contractor, not started, in progress or complete. The figures filter the list; a package opens its bays.
async function released(body) {
  const d = await api("/api/vegetation/released");
  const pick = (k) => { S.released = S.released === k ? "all" : k; vegetation("released"); };
  const fig = (k, cls, s) => {
    const t = tile(d.status_labels[k], n0(d.counts[k]), s, `${cls} click${S.released === k ? " on" : ""}`);
    t.addEventListener("click", () => pick(k));
    return t;
  };
  body.append(h("div", { class: "glance" },
    fig("not_started", "", "Packages whose crew has not begun a bay."),
    fig("in_progress", "amber", "Packages with bays being cut or awaiting the audit."),
    fig("complete", "green", "Packages whose every bay is accepted and closed.")));
  const rows = d.packages.filter((p) => S.released === "all" || p.status === S.released);
  const which = S.released === "all" ? "released work package" : `${d.status_labels[S.released].toLowerCase()} package`;
  body.append(card("Released work packages", `${plural(rows.length, which, `${which}s`)}, newest first.${S.released === "all" ? "" : " Select the figure again to show every package."} Select a package for its bays.`,
    table([{ l: "Package", f: (r) => h("span", {}, h("b", {}, r.title), h("div", { class: "muted small" }, r.package_id)) },
      { l: "Status", f: (r) => chip(r.status_label, `veg-pk-${r.status}`) }, { l: "Crew", f: (r) => h("span", {}, r.crew_name, h("div", { class: "muted small" }, r.region_name || r.region_id)) },
      { l: "Priority", f: (r) => tierChip(r.priority_tier, r.priority_tier_label) }, { l: "Bays", f: (r) => n0(r.bay_count), r: 1 },
      { l: "Not started", f: (r) => n0(r.issued), r: 1 }, { l: "In progress", f: (r) => n0(r.in_progress), r: 1 },
      { l: "Awaiting audit", f: (r) => n0(r.awaiting_audit), r: 1 }, { l: "Closed", f: (r) => n0(r.closed), r: 1 },
      { l: "Due", f: (r) => dte(r.due_date) }, { l: "Issued", f: (r) => dte(r.issued_at) }], rows, (r) => packageDrawer(r.package_id), { max: 520 })));
  body.append(card("Exceptions open on bays", `${plural(d.open_exceptions.length, "exception is", "exceptions are")} open: work a crew could not complete on the spot. Listed newest first.`,
    table([{ l: "Bay", k: "bay_label" }, { l: "Package", k: "package_title" }, { l: "Exception", f: (r) => chip(r.kind_label) }, { l: "Detail", k: "detail" },
      { l: "Reported by", k: "reported_by" }, { l: "When", f: (r) => dts(r.reported_at) }, lidarCol()], d.open_exceptions, null, { max: 340 })));
}

async function packageDrawer(id) {
  const d = await api(`/api/vegetation/packages/${encodeURIComponent(id)}`), p = d.package;
  drawer(h("h2", {}, p.title), h("p", { class: "muted" }, `${p.package_id}, issued to ${p.crew_name} (${p.region_name || p.region_id}).`),
    h("div", { class: "row" }, chip(p.status_label, `veg-pk-${p.status}`), " ", tierChip(p.priority_tier, p.priority_tier_label),
      h("a", { class: "btn small", href: `/contractor?crew_id=${encodeURIComponent(p.crew_id)}`, target: "_blank" }, "Open the crew's field app")),
    h("div", { class: "kv", style: "margin:12px 0" }, ...[["Bays", n0(p.bay_count)], ["Due", dte(p.due_date)], ["Issued", `${dts(p.issued_at)} by ${p.issued_by}`],
      ["Notes", p.notes || "None"]].flatMap(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)])),
    h("h4", {}, "Bays"), h("p", { class: "muted small" }, `${plural(d.bays.length, "bay", "bays")}, listed with the bays being cut first, then those awaiting the audit, not started and closed.`),
    table([{ l: "Bay", k: "bay_label" }, { l: "Status", f: (r) => chip(r.status_label) }, { l: "Attempt", k: "attempt", r: 1 },
      { l: "Required m", f: (r) => n1(r.required_clearance_m), r: 1 }, { l: "Before m", f: (r) => n2(r.clearance_before_m), r: 1 },
      { l: "After cut m", f: (r) => (r.post_cut_clearance_m === null ? "-" : n2(r.post_cut_clearance_m)), r: 1 }, { l: "Photos", f: (r) => n0(r.photos), r: 1 },
      { l: "", f: (r) => lidarBtn(r, r.post_cut_clearance_m !== null ? { cut: { before: r.clearance_before_m, after: r.post_cut_clearance_m, attempt: r.attempt } } : undefined) }],
    d.bays, null, { noScroll: 1 }));
}

// ---------------------------------------------------------------- audit and validate
async function audit(body) {
  const d = await api(`/api/vegetation/audits?result=${S.result === "all" ? "" : S.result}`);
  const cnt = Object.fromEntries(d.counts.map((r) => [r.result, r.n]));
  const pick = h("select", { class: "zone-pick", onchange: (e) => { S.result = e.target.value; vegetation("audit"); } },
    [["pending", "Awaiting decision"], ["compliant", "Compliant, closed"], ["non_compliant", "Non-compliant, reissued"], ["all", "All"]]
      .map(([k, l]) => h("option", { value: k, selected: k === S.result ? "" : null }, l)));
  body.append(h("div", { class: "veg-filters" }, pick));
  body.append(h("div", { class: "glance" },
    tile("Awaiting decision", n0(cnt.pending || 0), "Cut complete, post-cut check in progress.", "amber"),
    tile("Compliant, closed", n0(cnt.compliant || 0), "Post-cut clearance meets the requirement.", "green"),
    tile("Non-compliant, reissued", n0(cnt.non_compliant || 0), "Sent back to the contractor as a new attempt.", "red")));
  const rows = d.audits;
  body.append(card("Post-cut audits", "Clearance before and after each cut, against the required clearance, with the photos the crew took of it. Select an audit for its photos and exceptions.",
    table([{ l: "Bay", k: "bay_label" }, { l: "Crew", f: (r) => r.crew_name || r.crew_id }, { l: "Attempt", k: "attempt", r: 1 },
      { l: "Required m", f: (r) => n1(r.required_clearance_m), r: 1 }, { l: "Before m", f: (r) => n2(r.clearance_before_m), r: 1 },
      { l: "After cut m", f: (r) => n2(r.post_cut_clearance_m), r: 1 }, { l: "Gain m", f: (r) => n2(r.clearance_gain_m), r: 1 },
      { l: "Suggested", f: (r) => chip(r.suggested_result_label) }, { l: "Result", f: (r) => chip(r.result_label) },
      { l: "Photos", f: (r) => {
        const pics = (r.photos || []).filter((p) => p.image_url), gone = (r.photos || []).length - pics.length;
        if (!(r.photos || []).length) return h("span", { class: "muted" }, "None");
        return h("span", { class: "veg-thumbs" }, pics.slice(0, 3).map((p) => h("img", { src: p.image_url, alt: `${r.bay_label} photo`, loading: "lazy" })),
          pics.length > 3 ? h("span", { class: "muted small" }, `and ${n0(pics.length - 3)} more`) : null,
          gone ? h("span", { class: "muted small" }, pics.length ? `, ${n0(gone)} without a picture` : `${plural(gone, "photo", "photos")}, no picture held`) : null);
      } }, { l: "Actions", f: (r) => r.result === "pending" ? actions(r) : h("span", { class: "muted" }, r.decided_by ? `by ${r.decided_by}` : "") },
      lidarCol(cutOf)],
    rows, (r) => auditDrawer(r), { max: 560 })));
}

function actions(r) {
  return h("span", { class: "veg-actions-inline" },
    h("button", { class: "btn small primary", onclick: (e) => { e.stopPropagation(); decide(r, "compliant"); } }, "Accept and close"),
    h("button", { class: "btn small danger", onclick: (e) => { e.stopPropagation(); decide(r, "non_compliant"); } }, "Reissue"));
}

const decided = () => $("#drawer").classList.remove("open");   // a decided audit's drawer closes: the list shows its new result
function decide(r, decision) {
  if (decision === "compliant") { post(`/api/vegetation/audits/${r.audit_id}/decide`, { decision, notes: "" }).then(() => { decided(); toast(`${r.bay_label} accepted and closed.`); vegetation("audit"); }).catch((e) => toast(e.message, "bad")); return; }
  const notes = h("textarea", { class: "veg-input", rows: "3", placeholder: "Why it is being reissued (required), e.g. still inside 3 m at the midspan" });
  const crew = h("select", { class: "veg-input" }, [h("option", { value: r.crew_id }, `Same crew (${r.crew_name || r.crew_id})`)]);
  api("/api/vegetation/contractor/crews").then((cs) => {
    crew.innerHTML = "";
    for (const c of cs.crews) crew.append(h("option", { value: c.crew_id, selected: c.crew_id === r.crew_id ? "" : null }, `${c.crew_name}, ${c.region_name || c.region_id}`));
  });
  const bg = modal(h("h2", {}, `Reissue ${r.bay_label}`),
    h("p", { class: "muted" }, `After the cut, the clearance is ${n2(r.post_cut_clearance_m)} m against a required ${n1(r.required_clearance_m)} m. The bay goes back to the contractor as attempt ${r.attempt + 1}.`),
    h("div", { class: "veg-form" }, h("label", {}, "Reissue to", crew), h("label", {}, "Reason", notes)),
    h("div", { class: "veg-actions" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn danger", onclick: async () => {
        if (!notes.value.trim()) { toast("Say why the bay is being reissued.", "bad"); return; }
        try {
          await post(`/api/vegetation/audits/${r.audit_id}/decide`, { decision, notes: notes.value, crew_id: crew.value });
          bg.remove(); decided(); toast(`${r.bay_label} reissued to the contractor.`); vegetation("audit");
        } catch (e) { toast(e.message, "bad"); }
      } }, "Reissue to contractor")));
}

// A before and after strip for one bay, on a 0 to 6 m scale, with the required clearance marked.
function compareStrip(r) {
  const max = Math.max(6, (r.post_cut_clearance_m || 0) + 0.5, (r.clearance_before_m || 0) + 0.5);
  const at = (v) => `${Math.min(100, Math.max(0, (v / max) * 100))}%`;
  return h("div", { class: "veg-compare" },
    h("div", { class: "veg-bar" }, h("span", { class: "veg-fill before", style: `width:${at(r.clearance_before_m || 0)}` }), h("span", { class: "veg-lbl" }, `Before ${n2(r.clearance_before_m)} m`)),
    h("div", { class: "veg-bar" }, h("span", { class: "veg-fill after", style: `width:${at(r.post_cut_clearance_m || 0)}` }), h("span", { class: "veg-lbl" }, `After cut ${n2(r.post_cut_clearance_m)} m`)),
    h("span", { class: "veg-req", style: `left:${at(r.required_clearance_m)}` }, `Required ${n1(r.required_clearance_m)} m`));
}

async function auditDrawer(r) {
  const t = await api(`/api/vegetation/task/${r.task_id}`);
  const photos = t.photos.filter((p) => p.attempt === r.attempt).map((p) => h("div", { class: "veg-photo" },
    p.image_url ? h("a", { href: p.image_url, target: "_blank" }, h("img", { src: p.image_url, alt: `${r.bay_label}, ${p.kind_label || p.kind}` }))
      : h("div", { class: "veg-nopic" }, "The picture is no longer held."),
    h("div", {}, h("b", {}, p.kind_label || p.kind)),
    h("div", { class: "muted small" }, `${p.crew_name || p.crew_id || "The crew"}, ${dts(p.created_at)}.`),
    p.note ? h("div", { class: "muted" }, p.note) : null));
  const exc = t.exceptions.map((e) => h("li", {}, `${e.kind_label}: ${e.detail || ""} (attempt ${e.attempt})`));
  drawer(h("h4", {}, `${r.bay_label}, attempt ${r.attempt}`),
    h("p", { class: "muted" }, `${r.feeder_id}, crew ${r.crew_name || r.crew_id}. ${r.post_capture_method}.`),
    compareStrip(r), h("div", { class: "row" }, h("button", { class: "btn small", type: "button", onclick: () => openLidar(spanOf(r), cutOf(r)) }, "LiDAR view, before and after the cut")),
    h("div", { class: "kv" }, h("div", { class: "k" }, "Clearance gain"), h("div", {}, `${n2(r.clearance_gain_m)} m`),
      h("div", { class: "k" }, "Suggested"), h("div", {}, r.suggested_result_label),
      h("div", { class: "k" }, "Decision"), h("div", {}, r.result_label + (r.decided_by ? `, ${r.decided_by}` : "")),
      h("div", { class: "k" }, "Decision notes"), h("div", {}, r.decision_notes || "-")),
    h("h4", {}, "Photos of the cut"), photos.length ? h("div", { class: "veg-photos" }, photos) : h("p", { class: "muted" }, "No photos are linked to this attempt."),
    h("h4", {}, "Exceptions"), exc.length ? h("ul", { class: "veg-list" }, exc) : h("p", { class: "muted" }, "No exceptions were noted."),
    r.result === "pending" ? h("div", { class: "veg-actions" }, actions(r)) : null);
}

// ---------------------------------------------------------------- the vegetation contractors in Field staff management
// Every vegetation crew is a contractor. Field staff management shows them in their own tab, with the work packages they hold, and a
// vegetation crew's drawer there carries its packages and open bays. onCrew opens the crew's drawer (rm.js passes its own).
export async function contractorsTab(body, onCrew) {
  const crews = (await api("/api/vegetation/contractors")).crews;
  const sum = (k) => crews.reduce((a, c) => a + (c[k] || 0), 0);
  body.append(h("div", { class: "glance" },
    tile("Vegetation contractor crews", n0(crews.length), `${plural(new Set(crews.map((c) => c.region_id)).size, "sub-region", "sub-regions")} covered.`),
    tile("Bays to cut", n0(sum("to_do")), `${n0(sum("in_progress"))} of them being cut now.`, "amber"),
    tile("Awaiting the audit", n0(sum("awaiting_audit")), "Cut complete, with the post-cut check pending.", ""),
    tile("Closed in 14 days", n0(sum("closed_14d")), "Bays accepted by the post-cut audit.", "green")));
  body.append(card("Vegetation contractors", `${plural(crews.length, "contractor crew", "contractor crews")}, by region and sub-region. Their work comes from the packages issued in Vegetation management. Select a crew for its day and its packages.`,
    table([{ l: "Crew", f: (r) => h("span", {}, h("b", {}, r.crew_name), h("div", { class: "muted small" }, r.crew_id)) }, { l: "Sub-region", f: (r) => `${r.region_name}, ${r.area_name}` },
      { l: "Crew size", f: (r) => n0(r.crew_size), r: 1 }, { l: "Open packages", f: (r) => n0(r.open_packages), r: 1 }, { l: "Bays to cut", f: (r) => n0(r.to_do), r: 1 },
      { l: "Being cut", f: (r) => n0(r.in_progress), r: 1 }, { l: "Awaiting audit", f: (r) => n0(r.awaiting_audit), r: 1 }, { l: "Closed in 14 days", f: (r) => n0(r.closed_14d), r: 1 },
      { l: "Next due", f: (r) => dte(r.next_due) },
      { l: "", f: (r) => h("a", { class: "btn small", href: `/contractor?crew_id=${encodeURIComponent(r.crew_id)}`, target: "_blank", onclick: (e) => e.stopPropagation() }, "Field app") }],
    crews, (r) => onCrew(r.crew_id), { max: 640 })));
}

export function crewWorkCard(crewId) {   // a vegetation crew's packages and open bays, filled in once read
  const box = h("div", {}, h("h4", {}, "Vegetation work packages"), h("p", { class: "muted" }, "Reading the crew's packages."));
  api(`/api/vegetation/crew/${encodeURIComponent(crewId)}/work`).then((d) => {
    const open = d.bays.filter((b) => b.status === "issued" || b.status === "in_progress");
    box.replaceChildren(h("h4", {}, `Vegetation work packages (${d.packages.length})`),
      h("p", { class: "muted small" }, `${plural(open.length, "bay is", "bays are")} still to cut. The packages are listed newest first.`),
      d.packages.length ? table([{ l: "Package", k: "title" }, { l: "Status", f: (r) => chip(r.status_label, `veg-pk-${r.status}`) },
        { l: "Bays", f: (r) => n0(r.bay_count), r: 1 }, { l: "Closed", f: (r) => n0(r.closed), r: 1 }, { l: "Due", f: (r) => dte(r.due_date) }],
      d.packages, (r) => packageDrawer(r.package_id), { noScroll: 1 }) : h("p", { class: "muted" }, "No packages have been issued to this crew."));
  }).catch(() => box.replaceChildren(h("h4", {}, "Vegetation work packages"), h("p", { class: "muted" }, "The crew's packages could not be read.")));
  return box;
}
