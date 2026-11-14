// "Behind the scenes": the orange question mark at the top right of the app. Hover it (or tap it) for how the screen in view is built:
// the platform path its data takes (source systems, Lakeflow, Unity Catalog, the synced tables, Lakebase, the FastAPI app and the
// screen), with the parts this screen uses highlighted and named, the AI, Genie and change data feed where they play a part, and the
// API requests the screen has just made (recorded by ui.js). The text follows the route (the longest matching "#/screen/tab" key below);
// a screen can add a note for what it shows now (setBehind), for example the spatial query behind an alert.
import { h, API_LOG } from "./ui.js";

// The platform path, left to right, and the services beside it
const FLOW = [
  ["sources", "Source systems", "SAP, GIS, SCADA, market and CRM exports, and the live streams."],
  ["lakeflow", "Lakeflow", "Declarative pipelines: bronze, silver and gold."],
  ["uc", "Unity Catalog", "Governed gold marts, ML models and lineage."],
  ["sync", "Synced tables", "Gold copied into Lakebase and kept in step."],
  ["lakebase", "Lakebase", "Postgres: the served marts and the app's own records."],
  ["api", "FastAPI", "The Databricks App's API, signed in with the user's identity."],
  ["ui", "This screen", "The browser, calling the API."],
];
const SIDE = [
  ["jobs", "Jobs", "Background jobs: the live engine, the detector and the ML scoring."],
  ["ai", "AI models", "Foundation Model API endpoints, called by the app."],
  ["genie", "Genie", "AI/BI Genie answers questions in plain English over the gold marts (the Genie button)."],
  ["cdf", "Change data feed", "The app's records in Lakebase flow back to the lakehouse as Unity Catalog tables."],
];

const MARTS_API = "FastAPI reads Lakebase only; it never queries the lakehouse directly while a screen loads.";
const TEXT = {
  "#/cc": { title: "Command Centre", what: "The executive view of the network now: supply, reliability against target, fire danger and the open alerts. It refreshes every 15 seconds.",
    uses: { sources: "SCADA and the historian, outage records (OMS), the NSW RFS fire feed and the Open-Meteo forecast.",
      lakeflow: "The medallion pipeline builds the reliability, feeder and network marts.", uc: "Gold marts (gld_reliability, gld_feeder_state, gld_bulk_supply) and the storm projection model (ml_storm_projection).",
      sync: "The gold marts the screen needs, synced into the serving schema.", lakebase: "serving.gld_* for history; atlas.live_outage and atlas.situation for what is happening now.",
      api: "GET /api/cc assembles every figure in one call. " + MARTS_API, jobs: "The live engine writes outages and SCADA readings; the detector raises the alerts.",
      cdf: "Outages and alerts flow back to the lakehouse (schema lb_atlas)." } },
  "#/situations": { title: "Alerts", what: "The detector raises an alert when the network crosses a rule: a trip, a fire near the lines, a storm, a hot transformer or a crew that has gone quiet. Each alert carries recommendations, and every decision is recorded.",
    uses: { lakeflow: "Gold marts the detector reads: the network, the forecast and the ML scores.", uc: "Gold marts and the span risk and storm models (ml_span_risk, ml_storm_projection).",
      lakebase: "atlas.situation, atlas.situation_event, atlas.recommendation and atlas.notification_outbox.",
      api: "GET /api/situations lists the alerts; GET /api/situation/{id} opens one; POST .../plan, .../decide and .../undo act on it.",
      jobs: "The detector (Spark rules over gold) writes the alerts; the live engine moves the outages and crews.", ai: "The fast model drafts customer SMS text for an alert.",
      cdf: "Every alert, decision and message flows back to the lakehouse (schema lb_atlas)." } },
  "#/network": { title: "Network", what: "The supply chain from the bulk supply points to the customers, stop by stop, from the asset register and the connectivity model, with live load where it is measured.",
    uses: { sources: "SAP asset register, GIS (CIM), SCADA and the historian (Delta Sharing).", lakeflow: "Bronze, silver and gold for every asset class and the connectivity model.",
      uc: "gld_transmission_substation, gld_zone_transformer_state, gld_feeder_state, gld_ds_state and gld_customer_impact.", sync: "Each stop's marts synced into the serving schema.",
      lakebase: "serving.gld_* plus the live readings the live engine writes.", api: "GET /api/network/{stop} for the stop's rows; GET /api/asset/{id} for the drawer. " + MARTS_API,
      jobs: "The live engine streams SCADA readings into Lakebase." } },
  "#/map": { title: "Network map", what: "Hazards near the network (fires, hotspots and the wind forecast), and the assets and customers within their reach, found with spatial queries.",
    uses: { sources: "NSW RFS fires, DEA hotspots and the Open-Meteo forecast, beside the GIS network.", uc: "gld_asset with its geometry, and gld_customer_impact for who is behind each device.",
      lakebase: "PostGIS in Lakebase runs the spatial queries (ST_DWithin, ST_Distance) over serving.gld_asset.", api: "GET /api/map/network, /live, /fire and /weather for the layers; POST /api/spatial/area for what an area holds; POST /api/notify/area to message it; GET /api/vegetation/map-spans for the vegetation clearance layer." } },
  "#/ops": { title: "Operations: restoration and switching", what: "Restoration in progress (each figure opens what is behind it), the live events on the network with every customer call checked against the network, and how the crews are performing.",
    uses: { sources: "Meter alarms, customer calls and switch positions as live streams.", uc: "gld_nmi, gld_isolation_ds and gld_outage, with the network marts for a premises' supply chain.",
      lakebase: "The live schema (meter alarms, calls, switching), atlas.live_outage, rm.crew_live and rm.field_report.",
      api: "GET /api/ops/restoration, /api/ops/live (refreshed every 15 seconds) and /api/ops/learn; GET /api/ops/off-supply behind the customer figures and GET /api/ops/outage/{id} for an outage's detail; a call opens GET /api/customers/existing/{nmi}.",
      jobs: "The live engine writes the streams and moves restoration forward.", cdf: "The live streams flow back to the lakehouse (schema lb_live)." } },
  "#/res": { title: "Field staff management", what: "Today's work orders and the jobs raised at run time, rostered by the solver against each crew's skills, tickets, hours and the enterprise agreement. A supervisor's change is kept as an override on the solved roster.",
    uses: { sources: "SAP work orders and the workforce system (crews, skills, tickets, leave).", lakeflow: "The workforce and work order marts.", uc: "gld_crew, gld_crew_skill, gld_certification, gld_field_job and gld_rm_feeder.",
      sync: "The workforce marts synced into the serving schema.", lakebase: "serving.gld_crew*, gld_field_job and gld_rm_*, and the rm schema: raised jobs, overrides, field status, holds and crew positions.",
      api: "GET /api/rm/roster solves the region's plan in the app; POST /api/rm/dispatch, /api/rm/apply-assign and /api/rm/hold change it.",
      jobs: "The live engine drives dispatched crews to their jobs.", cdf: "Every override, hold and field status flows back to the lakehouse (schema lb_rm)." } },
  "#/customers/being-connected": { title: "Connection applications", what: "Every new connection application from the case management export: its stage, what is holding it, and how the premises will be supplied.",
    uses: { sources: "The connections case management export.", lakeflow: "Bronze to gold for applications, their stage history and blockers.", uc: "gld_connection_application and gld_connection_stage, with the network marts for the supply chain.",
      sync: "The connection marts synced into the serving schema.", lakebase: "serving.gld_connection_* and the network marts (gld_feeder_state, gld_zone_supply, gld_ds_state).",
      api: "GET /api/customers/pipeline for the figures; GET /api/customers/applications for the list; GET /api/customers/application/{id} for the drawer and its supply chain." } },
  "#/customers/existing": { title: "Existing customers", what: "The premises register with supply now, life support and critical needs, distributed energy and how each customer is reached.",
    uses: { sources: "MSATS standing data, GIS service points, the life support register and the CRM contact preferences.", lakeflow: "Bronze, silver and gold into one premises mart.", uc: "gld_nmi, with gld_isolation_ds for which premises sit behind each device.",
      sync: "gld_nmi and gld_isolation_ds synced into the serving schema.", lakebase: "serving.gld_nmi and serving.gld_isolation_ds, atlas.live_outage for supply now and atlas.notification_outbox for past notices.",
      api: "GET /api/customers/existing/summary, /cohorts and /search; GET /api/customers/existing/{nmi} for the drawer." } },
  "#/customers/notices": { title: "Customer notices", what: "Who a notice reaches is worked out live in Lakebase: the customers behind the isolation point of a planned outage, behind the tripped device of a fault, or inside the forecast cells of a weather hazard. The business rules are applied in code, the AI drafts the text, and messages are written only when the notice is released.",
    uses: { uc: "gld_nmi, gld_isolation_ds and the weather forecast cells.", lakebase: "atlas.notice and atlas.notice_exclusion for the decision trail; atlas.notification_outbox for the messages; PostGIS for the hazard areas.",
      api: "GET /api/notify/candidates; POST /api/notify/notices to create; POST .../drafts and .../release; GET .../customers for the recipients.",
      ai: "The planning model drafts each audience's text for the channels chosen.", cdf: "Notices and messages flow back to the lakehouse (schema lb_atlas)." } },
  "#/plan": { title: "Plan and invest", what: "A works programme in five steps: forecast demand, network limitations and options, interventions, work bundles and the release. AI drafts each step from the step before; every number comes from the marts.",
    uses: { lakeflow: "The planning marts: zone demand, constraints, interventions, unit costs and bundles.", uc: "gld_plan_* marts and the zone load forecast model (ml_zone_load_forecast).",
      sync: "The planning marts synced into the serving schema.", lakebase: "atlas.planning_run, planning_step, planning_selection and the release tables; edits and approvals go to atlas.audit_log.",
      api: "POST /api/planning/runs/{id}/draft starts the AI draft in the background; .../accept, .../edit and .../release record the planner's decisions.",
      ai: "The planning model drafts the narrative and the choices; it never computes a figure.", cdf: "Programmes and releases flow back to the lakehouse (schema lb_atlas)." } },
  "#/veg": { title: "Vegetation management", what: "The latest LiDAR campaign by region, with the bays on the map and the risk by feeder; then the work itself: a wizard that builds a work package for one contractor crew, the released packages whatever their status, and the post-cut audit with the photos of each cut. Contractors work their bays in the contractor's field app.",
    uses: { sources: "Annual LiDAR clearance campaigns, and the contractors' field app (start, photos, exceptions, cut complete).", lakeflow: "The vegetation marts.", uc: "gld_veg_survey_coverage, gld_veg_bay, gld_veg_feeder, gld_veg_plan_baseline and gld_vegetation_risk.",
      lakebase: "serving.gld_veg_*, and the rm schema's vegetation tables (packages, bay tasks, photos, exceptions, completions, audits); the photo pictures in media.photo_bytes.",
      api: "GET /api/vegetation/capture and /risk for the region summary; /plan, /candidates and /contractors for the wizard, then POST /api/vegetation/packages; GET /api/vegetation/released and /packages/{id}; GET /api/vegetation/audits and POST .../decide. The contractor's field app reports through /api/vegetation/contractor/*. The LiDAR view reads GET /api/vegetation/lidar/{span} and walks the line with /api/vegetation/feeder/{id}/spans; the point cloud itself is built in the browser.",
      cdf: "Packages, task progress, photos and audits flow back to the lakehouse (schema lb_rm)." } },
  "#/assets": { title: "Asset management", what: "Field photos checked by the vision model against the asset register; a reviewer decides what changes.",
    uses: { sources: "Photos from the field app and the contractor app.", uc: "gld_asset and gld_open_defect.", lakebase: "atlas.asset_photo, media.photo_bytes, atlas.defect_closure and the as-built records.",
      api: "POST /api/assets/photo uploads; GET /api/assets/photos lists the queue; POST /api/assets/photo/{id}/review records the decision.", ai: "The vision model reads each photo; a person decides." } },
  "#/assets/search": { title: "Asset management: search", what: "One search across the asset register, the premises and the crews. Exact matches are listed first, and a result opens its record.",
    uses: { uc: "gld_asset and gld_nmi, synced from gold; the crews from gld_crew.", sync: "gld_asset and gld_nmi synced into the serving schema.",
      lakebase: "serving.gld_asset and serving.gld_nmi, searched with ILIKE.", api: "GET /api/search, then the drawer endpoints (/api/asset/{id} and /api/rm/crew/{id})." } },
  "#/scene": { title: "Site view", what: "A 3D view of the site built to scale from the twin, with the lineworker who walks to an asset and explains it.",
    uses: { uc: "The network marts for the site, its assets and their state.", lakebase: "The served marts in Lakebase.", api: "GET /api/scene/{kind}/{id} for the site; POST /api/scene/speech for what the lineworker says.",
      ai: "The fast model writes the lineworker's lines from the asset's facts." } },
};

let extra = { hash: null, info: null };
export function setBehind(info) { extra = { hash: location.hash, info }; }   // what the screen shows now; cleared by the next route

function current() {
  const parts = (location.hash || "#/cc").split("/");
  for (let n = parts.length; n >= 2; n--) { const t = TEXT[parts.slice(0, n).join("/")]; if (t) return t; }
  return TEXT["#/cc"];
}

// the requests this screen has made, one row per endpoint: how many times, the latest status and time
function requests() {
  const screen = location.hash || "#/cc", by = new Map();
  for (const r of API_LOG.filter((x) => x.screen === screen)) {
    const key = `${r.method} ${r.path}`, x = by.get(key) || { ...r, n: 0 };
    by.set(key, { ...r, n: x.n + 1 });
  }
  return [...by.values()].sort((a, b) => b.at - a.at).slice(0, 12);
}

let panel = null, pinned = false, hideT = 0;
function show(btn) {
  clearTimeout(hideT);
  const t = current(), uses = t.uses || {}, note = extra.info && extra.hash === location.hash ? extra.info : null;
  if (!panel) {
    panel = h("div", { class: "behind-panel", onmouseenter: () => clearTimeout(hideT), onmouseleave: () => { if (!pinned) hideSoon(); } });
    document.body.append(panel);
  }
  const node = ([k, l, d]) => h("div", { class: `behind-node ${uses[k] || k === "ui" ? "on" : ""}`, title: d }, h("b", {}, l));
  const reqs = requests();
  panel.innerHTML = "";
  panel.append(...[h("div", { class: "behind-k" }, "Behind the scenes"), h("div", { class: "behind-t" }, t.title), h("p", {}, t.what),
    h("div", { class: "behind-flow" }, FLOW.map(node).flatMap((n, i) => i ? [h("span", { class: "behind-arrow" }, "›"), n] : [n])),
    h("div", { class: "behind-side" }, SIDE.map(node)),
    h("div", { class: "behind-uses" }, [...FLOW, ...SIDE].filter(([k]) => uses[k]).map(([k, l]) => h("div", {}, h("b", {}, l), " ", uses[k]))),
    note ? h("div", { class: "behind-note" }, h("div", { class: "behind-t" }, note.title), h("p", {}, note.what),
      (note.sources || []).length ? h("div", { class: "behind-src" }, note.sources.map(([k, v]) => h("div", {}, h("b", {}, k), " ", v))) : null,
      note.sql ? h("pre", { class: "behind-sql" }, note.sql) : null) : null,
    h("div", { class: "behind-k behind-k2" }, "Requests from this screen"),
    reqs.length ? h("div", { class: "behind-reqs" }, reqs.map((r) => h("div", {}, h("span", { class: `behind-m ${r.status < 400 ? "" : "bad"}` }, r.method),
      h("span", { class: "behind-p" }, r.path), h("span", { class: "behind-ms" }, `${r.status}, ${r.ms} ms${r.n > 1 ? `, ${r.n} calls` : ""}`))))
      : h("p", { class: "behind-none" }, "No request has been recorded for this screen yet.")].filter(Boolean));
  const r = btn.getBoundingClientRect();
  panel.style.top = `${r.bottom + 8}px`;
  panel.style.right = `${Math.max(12, window.innerWidth - r.right)}px`;
  panel.classList.add("on");
}
function hide() { pinned = false; panel?.classList.remove("on"); }
function hideSoon() { clearTimeout(hideT); hideT = setTimeout(hide, 180); }

export function behindButton() {
  const b = h("button", { class: "behind-q", id: "behindBtn", "aria-label": "Behind the scenes", type: "button" }, "?");
  b.addEventListener("mouseenter", () => show(b));
  b.addEventListener("mouseleave", () => { if (!pinned) hideSoon(); });
  b.addEventListener("focus", () => show(b));
  b.addEventListener("blur", () => { if (!pinned) hideSoon(); });
  b.addEventListener("click", (e) => { e.stopPropagation(); if (pinned) hide(); else { pinned = true; show(b); } });   // a tap pins it (touch screens)
  document.addEventListener("click", (e) => { if (pinned && !panel?.contains(e.target)) hide(); });
  window.addEventListener("hashchange", hide);
  return b;
}
