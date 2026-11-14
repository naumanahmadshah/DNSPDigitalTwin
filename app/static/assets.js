// Asset management (#/assets/<tab>): the review queue for asset photos (what the AI check found, the photo, the tagged asset, and a person's
// decision with notes), the photo history of an asset, and one search across the asset register, the premises and the crews. Photos come from /api/assets/*; the AI findings carry no confidence number.
import { api, post, h, card, tile, table, n0, ago, toast, loading, modal } from "./ui.js";
import { openAsset } from "./screens.js";
import { crewDrawer } from "./rm.js";

const TABS = [["review", "Review queue"], ["photos", "Asset photos"], ["search", "Search"]];
const enc = encodeURIComponent;
let PHOTO_VIEW = "tiles";   // the Asset photos tab shows tiles or a list
const yesNo = (v) => (v === true ? "Yes" : v === false ? "No" : "Could not tell");

export async function assetManagement(tab = "review") {
  const view = document.querySelector("#view");
  view.innerHTML = "";
  view.append(h("div", { class: "screen-h" }, h("h1", {}, "Asset management"),
    h("span", { class: "q" }, "Field photographs verified by AI and confirmed by a reviewer, with as-built records reconciled against the design.")),
    h("div", { class: "tabs" }, TABS.map(([k, l]) => h("a", { href: `#/assets/${k}`, class: k === tab ? "on" : "" }, l))));
  const body = h("div", { class: "tabbody" }, loading("Reading the asset records"));
  view.append(body);
  await ({ review: reviewTab, photos: photosTab, search: searchTab }[tab] || reviewTab)(body);
}

// ---------------------------------------------------------------- search
// One search across the asset register, the premises (NMI or street address) and the crews; a result opens its record in the drawer
const SAMPLES = [["ZS-KNV-TX1", "Zone substation transformer"], ["KNV-F02-R01", "Recloser"], ["P204517", "Pole"], ["DS-KNV02-020", "Distribution substation"],
  ["4913476228", "Customer premises"]];
async function searchTab(body) {
  const inp = h("input", { placeholder: "Type an asset ID, pole, feeder, NMI, street address or crew name.", class: "ex-q", "aria-label": "Search" });
  const out = h("div", {});
  let seq = 0;
  const run = async () => {
    const q = inp.value.trim(), mine = ++seq;
    if (q.length < 2) { out.innerHTML = ""; return; }
    let rows;
    try { rows = await api(`/api/search?q=${enc(q)}`); } catch (e) { if (mine === seq) out.replaceChildren(h("p", { class: "muted" }, "The search could not be completed. Please try again.")); return; }
    if (mine !== seq) return;   // a newer search is on its way
    out.replaceChildren(rows.length
      ? h("div", {}, h("p", { class: "muted small" }, `${n0(rows.length)} ${rows.length === 1 ? "record matches" : "records match"} "${q}". Exact matches are listed first. Select a record to open it.`),
        table([{ l: "ID", k: "id" }, { l: "Kind", k: "kind" }, { l: "Name", k: "name" }, { l: "Feeder", f: (r) => r.feeder_id || "-" }], rows,
          (r) => r.kind === "Crew" ? crewDrawer(r.id) : openAsset(r.id)))
      : h("p", { class: "muted" }, `No record matches "${q}".`));
  };
  let t; inp.addEventListener("input", () => { clearTimeout(t); t = setTimeout(run, 220); });
  body.innerHTML = "";
  body.append(card("Search", "Searches the asset register, the premises and the crews.", inp, out),
    card("Sample records", "Select a record to open it.", h("div", { class: "row" }, SAMPLES.map(([id, what]) =>
      h("button", { class: "btn small", "data-tip": what, onclick: () => openAsset(id) }, id)))));
  inp.focus();
}

// ---------------------------------------------------------------- review queue
async function reviewTab(body) {
  const all = (await api("/api/assets/photos")).photos || [];
  const queue = all.filter((p) => p.status === "uploaded" || p.status === "ai_checked");
  const views = await Promise.all(queue.map((p) => api(`/api/assets/${enc(p.asset_id)}`).catch(() => null)));
  body.innerHTML = "";
  body.append(h("p", { class: "muted" }, `${queue.length} photo${queue.length === 1 ? " is" : "s are"} waiting for a review decision. Every decision needs a note, whichever way it goes.`));
  if (!queue.length) body.append(h("p", { class: "muted" }, "The review queue is clear. Photos sent by field crews and contractors appear here."));
  queue.forEach((p, k) => body.append(reviewCard(p, views[k], () => reviewTab(body))));
  // a photo whose AI check is still running is picked up without a reload: the list is checked quietly and the queue is drawn again only
  // when a check has finished, so a note being typed is not lost; the timer stops once the screen is left
  const pending = new Set(queue.filter((p) => p.ai_status === "pending").map((p) => p.photo_id));
  const watch = async () => {
    if (!body.isConnected || !pending.size) return;
    let now = null;
    try { now = (await api("/api/assets/photos")).photos || []; } catch (e) { /* look again */ }
    if (now && now.some((p) => pending.has(p.photo_id) && p.ai_status !== "pending")) return reviewTab(body);
    setTimeout(watch, 4000);
  };
  if (pending.size) setTimeout(watch, 4000);
}

function aiFindings(p) {
  if (p.ai_status === "pending") return h("p", { class: "muted small" }, "The AI check is running. The findings appear here in a few seconds.");
  if (p.ai_status === "failed") return h("p", { class: "muted small" }, "The AI check did not finish, so the decision rests on the photo alone.");
  const ai = p.ai_result || {};
  const defects = (ai.defects || []).length ? ai.defects.join("; ") : "No defects seen";
  const fixed = ai.looks_fixed === true ? "Yes, the defect looks repaired" : ai.looks_fixed === false ? "No, the defect is still visible" : "Not applicable";
  return h("div", { class: "ai-find" }, h("div", { class: "ai-h" }, "What the AI check found"),
    h("dl", { class: "fr-facts" },
      h("dt", {}, "Shows"), h("dd", {}, ai.asset_class_seen || "Unclear"),
      h("dt", {}, "Matches the tag"), h("dd", {}, yesNo(ai.matches_tag)),
      h("dt", {}, "Condition"), h("dd", {}, p.condition_label || "Unclear"),
      h("dt", {}, "Visible defects"), h("dd", {}, defects),
      h("dt", {}, "Recorded defect"), h("dd", {}, fixed),
      h("dt", {}, "Suggested outcome"), h("dd", {}, p.ai_outcome_label || "No outcome suggested")),
    (ai.reasons || []).length ? h("ul", { class: "ai-reasons" }, ai.reasons.map((r) => h("li", {}, r))) : null);
}

function reviewCard(p, av, redraw) {
  const notes = h("textarea", { class: "anote", rows: 2, placeholder: "Notes (required): what you checked and why", "aria-label": "Review notes" });
  const decide = async (decision, label) => {
    const text = notes.value.trim();
    if (!text) { toast("Add a note before deciding. Every decision needs one.", "red"); notes.focus(); return; }
    if (decision === "rejected" && !window.confirm("Reject this photo and delete its picture? The record stays for the audit.")) return;
    try {
      const r = await post(`/api/assets/photo/${enc(p.photo_id)}/review`, { decision, notes: text });
      toast(r.message ? `${label}. ${r.message}` : `${label}.`, "green");
      redraw();
    } catch (e) {
      toast(e.message, "red");
    }
  };
  const a = av && av.asset, defects = (av && av.defects) || [];
  return h("div", { class: "card review-card" },
    h("div", { class: "rc-top" },
      h("div", { class: "rc-photo" }, p.image_url ? h("img", { src: p.image_url, alt: `Photo tagged to ${p.asset_id}` }) : h("div", { class: "muted small" }, "Photo removed")),
      h("div", { class: "rc-meta" },
        h("div", { class: "rc-title" }, h("b", {}, p.asset_id), a ? h("span", { class: "muted" }, ` ${a.asset_class_label}, ${a.feeder_id}`) : h("span", { class: "chip" }, "Device tag")),
        h("div", { class: "muted small" }, `${p.source_label || "Field"}${p.ref_id ? `, for ${p.ref_id}` : ""}, sent ${ago(p.submitted_at)} by ${p.submitted_by || "the sender"}`),
        p.note ? h("p", { class: "rc-note" }, h("b", {}, "Sender's note: "), p.note) : null,
        h("div", { class: "rc-open" }, defects.length ? `${defects.length} open defect${defects.length === 1 ? "" : "s"} on this asset: ${defects.slice(0, 3).map((d) => d.defect_label).join(", ")}`
          : "No open defects on this asset.")),
      h("div", { class: "rc-ai" }, aiFindings(p))),
    h("div", { class: "rc-actions" }, notes,
      h("div", { class: "rc-buttons" },
        h("button", { class: "btn good", "data-tip": "Closes the open defects on this asset in the app.", onclick: () => decide("fixed", "Accepted as fixed") }, "Accept as fixed"),
        h("button", { class: "btn", "data-tip": "Makes this the asset's current picture.", onclick: () => decide("updated", "Accepted as updated photo") }, "Accept as updated photo"),
        h("button", { class: "btn small danger", "data-tip": "Deletes the picture; the record stays for the audit.", onclick: () => decide("rejected", "Rejected and deleted") }, "Reject and delete"))));
}

// ---------------------------------------------------------------- photos of one asset
function photoGrid(list) {
  if (!list.length) return h("p", { class: "muted" }, "No photos have been recorded yet.");
  return h("div", { class: "photo-grid" }, list.map((p) => h("div", { class: "pg-item click", title: "Select for the photo's details", onclick: () => photoDetail(p) },
    p.image_url ? h("img", { src: p.image_url, alt: `Photo of ${p.asset_id}`, loading: "lazy" }) : h("div", { class: "pg-gone" }, "Picture removed on rejection"),
    h("div", { class: "small" }, h("b", {}, p.status_label || p.status), h("div", { class: "muted" }, `${p.source_label || ""}, ${ago(p.submitted_at)}`)),
    p.review_notes ? h("div", { class: "muted small" }, p.review_notes) : null)));
}

// The same photos as a list: when, which asset, who uploaded it and through which app, the outcome and the notes
function photoList(list) {
  if (!list.length) return h("p", { class: "muted" }, "No photos have been recorded yet.");
  return table([
    { l: "Uploaded", f: (p) => when(p.submitted_at) }, { l: "Asset", f: (p) => h("span", {}, h("b", {}, p.asset_id), p.asset_name && p.asset_name !== p.asset_id ? h("div", { class: "muted small" }, p.asset_name) : null) },
    { l: "Uploaded by", f: (p) => p.submitted_by || "-" }, { l: "Through", f: (p) => p.source_label || "-" },
    { l: "Outcome", f: (p) => p.status_label || "-" }, { l: "Uploader's note", f: (p) => p.note || "-" }, { l: "Reviewer's note", f: (p) => p.review_notes || "-" },
  ], list, (p) => photoDetail(p), { max: 560 });
}

const when = (iso) => iso ? new Date(iso).toLocaleString("en-AU", { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" }) : "-";

// One photo in full, whatever its outcome: the picture (or the note that it was removed), who uploaded it, through which app and for which
// job or task, the asset it is tagged to, when, the uploader's note, what the AI check found, and the reviewer's decision and notes
function photoDetail(p) {
  const name = p.asset_name && p.asset_name !== p.asset_id ? p.asset_name : null;   // the register's name, when it says more than the ID
  const ai = p.ai_result || {}, list = (xs) => (xs || []).length ? h("ul", { class: "pd-list" }, xs.map((x) => h("li", {}, x))) : null;
  const kv = (rows) => h("div", { class: "kv" }, rows.filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => [h("div", { class: "k" }, k), h("div", {}, v)]).flat());
  const bg = modal(h("h2", {}, `Photo of ${p.asset_id}`), name ? h("p", { class: "muted" }, name) : null,
    p.image_url ? h("img", { class: "pd-img", src: p.image_url, alt: `Photo of ${p.asset_id}` }) : h("div", { class: "pd-gone" }, "The picture was removed when the photo was rejected. Its record is kept for audit."),
    h("h4", {}, "Upload"),
    kv([["Asset", `${p.asset_id}${name ? `, ${name}` : ""}`], ["Tagged as", p.tag_kind === "device" ? "A device not in the asset register" : "An asset in the register"],
      ["Uploaded by", p.submitted_by], ["Through", p.source_label], ["Job or task", p.ref_id], ["Uploaded", when(p.submitted_at)],
      ["Uploader's note", p.note || "No note was added."], ["Picture", p.width_px ? `${n0(p.width_px)} by ${n0(p.height_px)} pixels, ${n0(Math.round((p.bytes_len || 0) / 1024))} KB` : null]]),
    h("h4", {}, "AI check"),
    p.ai_status === "done" ? h("div", {}, kv([["Matches the tagged asset", yesNo(ai.matches_tag)], ["Looks repaired", yesNo(ai.looks_fixed)], ["Condition", p.condition_label],
      ["Suggested outcome", p.ai_outcome_label], ["Checked", when(p.ai_at)]]),
      (ai.defects || []).length ? h("div", {}, h("div", { class: "muted small" }, "Defects seen"), list(ai.defects)) : null,
      (ai.reasons || []).length ? h("div", {}, h("div", { class: "muted small" }, "Reasons"), list(ai.reasons)) : null)
      : h("p", { class: "muted" }, p.ai_status === "failed" ? `The AI check did not finish: ${p.ai_error || "no reason was recorded"}.` : "The AI check has not finished yet."),
    h("h4", {}, "Review"),
    kv([["Outcome", p.status_label], ["Reviewed by", p.reviewed_by], ["Reviewed", p.reviewed_at ? when(p.reviewed_at) : "Not yet reviewed."], ["Reviewer's notes", p.review_notes]]),
    h("div", { class: "plan-actions" }, h("button", { class: "btn primary", onclick: () => bg.remove() }, "Close")));
  bg.querySelector(".modal").classList.add("pd-window");
}

async function photosTab(body) {
  const search = h("input", { class: "fsearch", type: "search", placeholder: "Search an asset ID or name, for example P215446", "aria-label": "Search asset" });
  const results = h("div", { class: "ares" });
  const detail = h("div", {});
  body.innerHTML = "";
  body.append(search, results, detail);
  let timer = null;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = search.value.trim();
      results.innerHTML = "";
      if (q.length < 2) return;
      const r = await api(`/api/assets/search?q=${enc(q)}`).catch(() => ({ assets: [] }));
      for (const a of r.assets || []) {
        results.append(h("button", { class: "fresult", onclick: () => { results.innerHTML = ""; showAsset(a.asset_id, detail); } },
          h("b", {}, a.asset_id), h("span", { class: "muted small" }, [a.asset_class_label, a.feeder_id].filter(Boolean).join(", "))));
      }
      if (!(r.assets || []).length) results.append(h("p", { class: "muted small" }, "No asset matches that ID or name."));
    }, 250);
  });
  const recent = ((await api("/api/assets/photos")).photos || []).slice(0, 60);
  const holder = h("div");
  const view = (mode) => {   // tiles or a list, the same photos either way
    PHOTO_VIEW = mode;
    holder.replaceChildren(mode === "list" ? photoList(recent) : photoGrid(recent));
    toggle.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.v === mode));
  };
  const toggle = h("div", { class: "im-seg" }, [["tiles", "Tiles"], ["list", "List"]].map(([v, l]) => h("button", { "data-v": v, onclick: () => view(v) }, l)));
  detail.append(card("Recent photos", `The latest ${n0(recent.length)} ${recent.length === 1 ? "photo" : "photos"} across all assets, newest first. Select a photo for its details.`,
    h("div", { class: "pd-bar" }, toggle), holder));
  view(PHOTO_VIEW);
}

async function showAsset(id, el) {
  el.innerHTML = "";
  el.append(loading("Reading the asset"));
  const v = await api(`/api/assets/${enc(id)}`);
  const a = v.asset;
  el.innerHTML = "";
  if (!a) { el.append(h("p", { class: "muted" }, `${id} is not in the asset register.`)); return; }
  const cur = v.current_photo;
  el.append(card(a.asset_id, `${a.asset_class_label}, ${a.feeder_id || "No feeder recorded"}`,
    h("div", { class: "ops-tiles" },
      tile("Customers downstream", n0(a.customers_downstream), a.life_support_downstream ? `${n0(a.life_support_downstream)} on life support` : "None on life support"),
      tile("Open defects", n0(v.defects.length), "Defects not yet closed in the app"),
      tile("Zone substation", a.zs_id || "-", a.name || ""),
      tile("SAP equipment number", a.sap_equnr || "-", "From the asset register.")),
    v.defects.length ? table([{ l: "Defect", k: "defect_label" }, { l: "Priority", k: "priority", r: 1 }, { l: "Age (days)", k: "age_days", r: 1 },
      { l: "Raised", k: "created_date" }], v.defects, null, { max: 240 }) : h("p", { class: "muted small" }, "No open defects on this asset."),
    h("h4", { class: "ops-h4" }, "Current accepted photo"),
    cur && cur.image_url ? h("div", { class: "cur-photo" }, h("img", { src: cur.image_url, alt: `Current photo of ${a.asset_id}` }),
      h("div", { class: "muted small" }, `Set ${ago(cur.set_at)} by ${cur.set_by || "a reviewer"}.`)) : h("p", { class: "muted small" }, "No photo has been accepted yet."),
    h("h4", { class: "ops-h4" }, "Photo history"), photoGrid(v.photos || [])));
}
