// Field reports: what the crew found and did on a job, filed from the field app. One card, for the situation detail (by situation) and the
// Resources job and crew drawers (by job or by crew). Every code is shown as its label (the backend returns labels).
import { api, h, card, ago, hhmm } from "./ui.js";

const yesNo = (v) => (v ? "Yes" : "No");

// The facts of one report as label and value pairs, in the order a supervisor reads them.
function facts(r) {
  const seg = r.segments_total || r.segments_cleared ? `${r.segments_cleared ?? 0} of ${r.segments_total ?? 0} cleared` : null;
  const mats = (r.materials || []).map((m) => `${m.label} ${m.qty}`).join(", ") || null;
  return [
    ["Arrived on site", r.arrived_at ? hhmm(r.arrived_at) : null],
    ["Patrol complete", yesNo(r.patrol_complete)],
    ["Fault located", r.fault_located ? [r.fault_label, r.fault_ref].filter(Boolean).join(", ") : "No"],
    ["Segments", seg],
    ["Switching executed", yesNo(r.switching_executed)],
    ["Customers restored", r.customers_restored ? Number(r.customers_restored).toLocaleString("en-AU") : "0"],
    ["Access", r.access_label],
    ["Hazards on site", (r.hazard_labels || []).join(", ") || "None reported"],
    ["Materials used", mats || "None"],
    ["Photos", r.photo_ids && r.photo_ids.length ? `${r.photo_ids.length} ${r.photo_ids.length === 1 ? "photo" : "photos"} attached` : "None"],
  ];
}

function reportBlock(r) {
  const tone = { fixed: "good", temporary_repair: "amber", follow_up: "red" }[r.outcome] || "";
  return h("div", { class: "fr-item" },
    h("div", { class: "fr-head" },
      h("span", { class: `fr-out ${tone}` }, r.outcome_label || "No outcome recorded"),
      h("b", {}, r.crew_name || r.crew_id || "Crew"),
      h("span", { class: "muted small" }, `${ago(r.reported_at)}${r.job_id ? ` · ${r.job_id}` : ""}`)),
    h("dl", { class: "fr-facts" }, facts(r).flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v === null || v === undefined || v === "" ? "-" : String(v))])),
    r.response_note ? h("p", { class: "fr-note" }, h("b", {}, "How they responded: "), r.response_note) : null,
    r.notes ? h("p", { class: "fr-note" }, h("b", {}, "Notes: "), r.notes) : null);
}

// A card that fills itself from the API: it returns at once, so a screen can place it straight away.
export function fieldReportsCard({ situationId, jobId, crewId } = {}) {
  const body = h("div", { class: "fr-list" }, h("p", { class: "muted small" }, "Loading field reports"));
  const q = situationId ? `situation_id=${encodeURIComponent(situationId)}` : jobId ? `job_id=${encodeURIComponent(jobId)}`
    : crewId ? `crew_id=${encodeURIComponent(crewId)}` : "";
  api(`/api/fieldreport/list?${q}`).then((res) => {
    const rs = res.reports || [];
    body.innerHTML = "";
    if (!rs.length) {
      body.append(h("p", { class: "muted small" }, situationId ? "No crew has filed a field report on this situation yet."
        : jobId ? "No field report on this job yet. Crews file one from the field app once they have been on site."
          : "No field reports from this crew yet."));
      return;
    }
    body.append(...rs.slice(0, 12).map(reportBlock));
    const older = rs.length - 12;
    if (older > 0) body.append(h("p", { class: "muted small" }, `${older} older ${older === 1 ? "report" : "reports"} not shown.`));
  }).catch((e) => {
    body.innerHTML = "";
    body.append(h("p", { class: "muted small" }, `Field reports could not load (${e.message}).`));
  });
  return card("Field reports", "What the crew found and did, filed from the field app.", body);
}
