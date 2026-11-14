// Customer notices: the "Customer notices" tab of Customers (noticesTab(root, sub) is embedded there). Two sub-tabs:
// Design customer notices, a wizard: 1 choose what to tell customers about (the released plans' planned work, network faults, weather
//   hazards or a custom notice), 2 choose the channels and draft with AI (the app is veiled while it writes), 3 review the draft: the
//   rules applied, the drafted text and a sample of each channel, who it reaches (click a count for the customers), then release it,
//   now or scheduled for its notice date. Notices still in draft are listed to pick up again.
// Released customer notices: the notices scheduled or sent, each opening read only (a scheduled planned-work notice is changed from its
//   planned outage in the wizard).
// Nothing reaches a customer until the notice is released.
import { api, post, h, n0, card, table, toast, modal, loading } from "./ui.js";
import { openProgramme } from "./plan.js";
import { openPremises } from "./customers.js";

// The works plan a notice belongs to, linked: the released plan on its board, or the programme being designed at its step in hand
function planCell(r) {
  const p = r.plan;
  if (!p) return h("span", { class: "muted" }, r.notice_type === "planned_work" ? "Not from a works plan" : "-");
  if (p.state === "Removed") return h("span", { class: "muted" }, "Works plan removed from registry");
  const when = p.released_at ? new Date(p.released_at).toLocaleDateString("en-AU", { day: "numeric", month: "short" }) : "";
  return h("span", {}, h("a", { href: p.release_id ? `#/plan/released/${p.release_id}` : "#/plan", onclick: (e) => {
    e.stopPropagation();
    if (!p.release_id) { e.preventDefault(); openProgramme(p.run_id); }
  } }, p.title), h("div", { class: "muted small" }, p.release_id ? `Released ${when}` : "In design"));
}

// what a notice can be about: the first step of the wizard, each with what it covers
const KINDS = [["planned_work", "Planned work", "A planned outage from a released works plan. Customers must be notified four business days before."],
  ["network_fault", "Network faults", "A live outage. Customers behind the tripped device are notified at once."],
  ["severe_weather", "Weather hazard", "Forecast strong winds over the network. Customers in the affected forecast cells are warned ahead."],
  ["custom", "Custom notice", "Any other work. Describe it and choose the feeders it interrupts."]];
const CHOOSE = [["sms", "SMS", "A text message to the customer's mobile."], ["email", "Email", "Sent to the registered email address."],
  ["app", "App or web", "A card in the app and on the outage website."], ["mail", "Mail", "A letter, for customers with no digital channel."]];
const S = { root: null, sub: "design", step: 1, kind: "planned_work", cand: null, preview: null, channels: new Set(["sms", "email", "app", "mail"]),
  nid: null, view: null, poll: null };
const LABELS = { channel: {}, audience: {} };   // filled from the API (ref_label), never a code table in the page
const setLabels = (l) => { if (l) { Object.assign(LABELS.channel, l.channel || {}); Object.assign(LABELS.audience, l.audience || {}); } };
const chName = (ch) => LABELS.channel[ch] || ch;
const audName = (a) => LABELS.audience[a] || a;
const noun = (n, one, many) => n === 1 ? one : many;   // the noun for a count: "1 customer", "2 customers"
const dayLabel = (iso) => iso ? new Date(`${iso}T00:00:00`).toLocaleDateString("en-AU", { weekday: "short", day: "numeric", month: "short" }) : "-";
const chip = (text, cls = "") => h("span", { class: `chip ${cls}` }, text);
const statusChip = (s, l) => h("span", { class: `nt-status nt-st-${s || "none"}` }, l || s || "");
const stop = () => { if (S.poll) { clearTimeout(S.poll); S.poll = null; } };

// One notice's messages, editable: a text box for each audience and channel it uses. save() stores the boxes that changed.
export async function noticeEditor(nid) {
  const v = await api(`/api/notify/notices/${nid}`);
  setLabels(v.labels);
  const n = v.notice, drafts = n.drafts || {}, boxes = [];
  const el = h("div", {}, Object.entries(n.draft_channels || {}).filter(([, chans]) => chans.length).map(([aud, chans]) => h("div", { class: "nt-group" },
    h("h4", {}, audName(aud)), chans.map((ch) => {
      const t = h("textarea", { rows: ch === "email" || ch === "mail" ? 7 : 3 }, (drafts[aud] || {})[ch] || "");
      boxes.push([aud, ch, t]);
      return h("label", { class: "nt-draft" }, h("span", { class: "nt-draft-h" }, chName(ch)), t);
    }))));
  const save = async () => {
    const edits = boxes.filter(([a, c, t]) => t.value !== ((drafts[a] || {})[c] || "")).map(([a, c, t]) => ({ audience: a, channel: c, text: t.value }));
    if (edits.length) await post(`/api/notify/notices/${nid}/drafts`, { edits });
  };
  return { el, save, notice: n };
}

export async function noticesTab(root, sub) {
  stop();
  S.root = root;
  S.sub = sub === "released" ? "released" : "design";
  // arriving at the design tab (from the Customers tabs, the menu or the Released tab) starts the wizard afresh at its first step; the
  // wizard's own steps redraw it in place, so they keep their state
  if (S.sub === "design") Object.assign(S, S.openDraft ? { step: 4, nid: S.openDraft } : { step: 1, kind: "planned_work", nid: null, cand: null, preview: null });
  S.openDraft = null;
  root.innerHTML = "";
  const kpis = h("div", { class: "nt-kpis" });
  const body = h("div", { class: "nt-body" });
  S.pickBox = h("div", { class: "nt-pick" });
  root.append(h("div", { class: "nt" }, kpis, S.pickBox,
    h("div", { class: "nt-subtabs" }, [["design", "Design customer notices"], ["released", "Released customer notices"]].map(([k, l]) =>
      h("a", { href: `#/customers/notices/${k}`, class: k === S.sub ? "on" : "", onclick: (e) => {
        // choosing a tab closes a figure's list, so the tabs return to their place under the figures
        S.pick = null;
        if (location.hash === `#/customers/notices/${k}`) { e.preventDefault(); noticesTab(S.root, k); }
      } }, l))), body));
  loadOverview(kpis);
  if (S.sub === "released") return releasedList(body);
  return wizard(body);
}

// The three figures each open their list of notices under them; selecting the same figure again closes it
async function loadOverview(el) {
  const o = await api("/api/notify/overview");
  const kpi = (key, k, v, s) => {
    const t = h("div", { class: `tile click ${S.pick === key ? "on" : ""}` }, h("div", { class: "k" }, k), h("div", { class: "v num" }, v), h("div", { class: "s" }, s));
    t.addEventListener("click", async () => {
      S.pick = S.pick === key ? null : key;
      // a draft open in the design tab is saved and closed, so the design tab starts afresh under the list
      if (S.sub === "design" && S.step === 4 && S.nid && S.view) {
        try { toast(await saveQuiet() ? "Your edits to the draft were saved. It is listed under Drafts." : "The draft was closed. It is listed under Drafts."); }
        catch (e) { return toast(e.message, "red"); }
        return noticesTab(S.root, "design");
      }
      loadOverview(el);
    });
    return t;
  };
  el.innerHTML = "";
  el.append(kpi("draft", "Drafts", n0(o.draft), "Notices being designed."),
    kpi("scheduled", "Scheduled", n0(o.scheduled), o.next_send ? `The next notice is sent on ${o.next_send}.` : "No notice is waiting to be sent."),
    kpi("sent", "Sent this week", n0(o.sent_week), `${n0(o.reached_week)} ${noun(o.reached_week, "customer", "customers")} reached.`));
  pickList();
}

// The notices behind the figure selected: drafts open in the wizard to carry on; scheduled and sent notices open in full
async function pickList() {
  const box = S.pickBox;
  if (!box) return;
  box.innerHTML = "";
  if (!S.pick) return;
  const d = await api(`/api/notify/notices?status=${S.pick === "draft" ? "draft" : "released"}&limit=200`);
  const week = Date.now() - 7 * 86400000;
  const rows = (d.notices || []).filter((r) => S.pick === "draft" || (S.pick === "scheduled" ? r.status === "scheduled" : r.status === "sent" && r.sent_at && Date.parse(r.sent_at) >= week));
  const what = { draft: ["Drafts", "draft", "drafts", "Select a draft to continue designing it."],
    scheduled: ["Scheduled notices", "scheduled notice", "scheduled notices", "Listed newest first. Select a notice to see its messages and recipients."],
    sent: ["Sent this week", "notice sent this week", "notices sent this week", "Listed newest first. Select a notice to see what was sent and to whom."] }[S.pick];
  box.append(card(what[0], `${n0(rows.length)} ${rows.length === 1 ? what[1] : what[2]}. ${what[3]}`, rows.length ? table([
    { l: "Notice", f: (r) => h("span", {}, r.title, h("div", { class: "muted small" }, r.feeder_label || r.description || "")) },
    { l: "Type", f: (r) => r.notice_type_label },
    { l: "Window", f: (r) => r.window_label },
    S.pick === "draft" ? { l: "Started by", f: (r) => (r.created_by || "").split("@")[0] } : { l: "Goes or went", f: (r) => r.send_at_label || "-" },
    { l: "Customers reached", f: (r) => n0(r.reached), r: true },
    { l: "Channels", f: (r) => (r.channel_labels || []).join(", ") },
    { l: "Works plan", f: planCell },
  ], rows, (r) => {
    if (S.pick !== "draft") return noticeWindow(r);
    S.openDraft = r.notice_id;
    if (location.hash === "#/customers/notices/design") noticesTab(S.root, "design"); else location.hash = "#/customers/notices/design";
  }, { max: 420 }) : h("div", { class: "nt-empty" }, `There are no ${what[2]}.`)));
}

// ---------------------------------------------------------------- the wizard
function wizard(body) {
  body.innerHTML = "";   // one step at a time; each step's Back button returns to the one before
  const pane = h("div", { class: "nt-pane" });
  body.append(pane);
  if (S.step === 1) return kindStep(pane, body);
  if (S.step === 2) return listStep(pane, body);
  if (S.step === 3) return channelStep(pane, body);
  return reviewStep(pane, body);
}

// 1: what to tell customers about
// 1: what the notice is about, then Next; the drafts to pick up again are listed under the choices
function kindStep(pane, body) {
  const next = h("button", { class: "btn primary", onclick: () => {
    if (S.kind === "custom") { S.cand = { custom: true }; S.preview = null; S.step = 3; } else S.step = 2;
    wizard(body);
  } }, "Next");
  pane.append(h("h3", { class: "nt-q" }, "Choose what the notice is about."),
    h("div", { class: "nt-kinds" }, KINDS.map(([k, l, d]) => h("button", { class: `nt-kind ${S.kind === k ? "on" : ""}`, onclick: (e) => {
      S.kind = k;
      e.currentTarget.parentElement.querySelectorAll(".nt-kind").forEach((b) => b.classList.toggle("on", b === e.currentTarget));
    } }, h("b", {}, l), h("span", {}, d)))),
    h("div", { class: "nt-foot" }, next));
  const drafts = h("div", {});
  pane.append(drafts);
  draftsList(drafts, body);
}

// 2: the outages, faults or hazards of the kind chosen; choosing one goes on to the channels (or opens the notice it already has)
async function listStep(pane, body) {
  const kind = KINDS.find(([k]) => k === S.kind) || KINDS[0];
  const list = h("div", {}, h("div", { class: "muted small" }, "Loading the outages."));
  pane.append(h("h3", { class: "nt-q" }, S.kind === "planned_work" ? "Choose the planned outage." : S.kind === "network_fault" ? "Choose the outage." : "Choose the weather hazard."),
    list, h("div", { class: "nt-foot" }, h("button", { class: "btn", onclick: () => { S.step = 1; wizard(body); } }, "Back")));
  let c;
  try { c = await api(`/api/notify/candidates?kind=${S.kind}`); } catch (e) { list.innerHTML = ""; list.append(h("p", { class: "muted" }, e.message)); return; }
  const items = c.items || [];
  list.innerHTML = "";
  list.append(h("p", { class: "muted small" }, c.kind === "planned_work"
    ? `The planned outages of the released works plans. Customers must be notified ${c.notice_days} business days before. Select an outage to design its notice.`
    : `${kind[2]} Select one to design its notice.`));
  if (!items.length) {
    list.append(h("div", { class: "nt-empty" }, c.kind === "planned_work" ? "No released plan has planned outages ahead. Release a works plan in Plan and invest."
      : c.kind === "network_fault" ? "No outage is live right now." : "No forecast cell is at or above the threshold in the next four days."));
    return;
  }
  const pickRow = (r) => {
    if (r.notice) { S.nid = r.notice.id; S.step = 4; return wizard(body); }
    if (r.eligible === false) return toast(r.blocked || "This cannot be notified at present.");
    S.cand = r; S.preview = null; S.step = 3; wizard(body);
  };
  if (c.kind === "planned_work") {
    list.append(table([
      { l: "Feeder", f: (r) => h("span", {}, r.feeder_name, h("div", { class: "muted small" }, r.plan_title)) },
      { l: "Day", f: (r) => r.date_label },
      { l: "Window", f: (r) => r.window_label.replace(/^\S+ \S+ \S+ /, "") },
      { l: "Work items", f: (r) => n0(r.jobs), r: true },
      { l: "Customers impacted", f: (r) => n0(r.customers), r: true },
      { l: "Notice period", f: (r) => h("span", { class: "nt-np" }, r.eligible ? chip("Met", "rm-int") : chip("Too soon", "rm-con"),
        r.notice ? statusChip(r.notice.status, r.notice.label) : null) },
    ], items, pickRow, { max: 460 }));
  } else if (c.kind === "network_fault") {
    list.append(table([
      { l: "Feeder", f: (r) => r.feeder_name }, { l: "Started", f: (r) => r.window_label },
      { l: "Customers off", f: (r) => n0(r.customers_off), r: true }, { l: "Life support off", f: (r) => n0(r.life_support_off), r: true },
    ], items, pickRow, { max: 460 }));
  } else {
    list.append(table([
      { l: "Window", f: (r) => r.window_label }, { l: "Cells", f: (r) => n0(r.cell_count), r: true },
      { l: "Max gust, km/h", f: (r) => Math.round(r.max_gust_kmh), r: true },
    ], items, pickRow, { max: 220 }));
  }
}

async function draftsList(el, body) {
  const d = await api("/api/notify/notices?status=draft&limit=40");
  const rows = d.notices || [];
  if (!rows.length) return;
  el.append(card("Drafts to pick up again", `${n0(rows.length)} ${noun(rows.length, "draft", "drafts")} not yet released.`, table([
    { l: "Notice", f: (r) => h("span", {}, r.title, h("div", { class: "muted small" }, r.feeder_label || r.description || "")) },
    { l: "Type", f: (r) => r.notice_type_label },
    { l: "Window", f: (r) => r.window_label },
    { l: "Channels", f: (r) => (r.channel_labels || []).join(", ") },
    { l: "Customers", f: (r) => n0(r.reached), r: true },
    { l: "Works plan", f: planCell },
    { l: "Started by", f: (r) => (r.created_by || "").split("@")[0] },
    { l: "", f: (r) => h("button", { class: "btn small", onclick: (e) => { e.stopPropagation(); deleteDraft(r, body); } }, "Delete") },
  ], rows, (r) => { S.nid = r.notice_id; S.step = 4; wizard(body); }, { max: 300 })));
}

// Delete a draft and everything about it; the server refuses a notice that has been released
function deleteDraft(r, body) {
  const bg = modal(h("h2", {}, `Delete the draft "${r.title}"?`),
    h("p", {}, "This deletes the draft notice and everything about it: its drafted messages, the customers it would reach and those left out, "
      + "and its AI drafting record. If it was prepared for an outage on a works plan, the plan shows that outage as having no notice. It cannot be undone."),
    h("div", { class: "plan-actions" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn red", onclick: async (e) => {
        e.target.disabled = true;
        try {
          await api(`/api/notify/notices/${r.notice_id}`, { method: "DELETE" });
          bg.remove();
          if (S.nid === r.notice_id) { S.nid = null; S.step = 1; }
          toast(`The draft "${r.title}" has been deleted.`);
          wizard(body);
        } catch (err) { toast(err.message, "red"); e.target.disabled = false; }
      } }, "Delete draft")));
}

// 2: the channels, then draft with AI
async function channelStep(pane, body) {
  const c = S.cand || {};
  const reach = h("div", { class: "nt-reach muted" }, c.custom ? "" : "Working out who this reaches.");
  let custom = null;
  if (c.custom) custom = await customForm();
  const boxes = CHOOSE.map(([k, l, hint]) => h("label", { class: `nt-ch ${S.channels.has(k) ? "on" : ""}` },
    h("input", { type: "checkbox", checked: S.channels.has(k) ? "checked" : null, onchange: (e) => {
      e.target.checked ? S.channels.add(k) : S.channels.delete(k); e.target.closest(".nt-ch").classList.toggle("on", e.target.checked); refreshReach(); } }),
    h("span", { class: "nt-ch-l" }, l), h("span", { class: "nt-ch-h" }, hint)));
  const refreshReach = async () => {
    if (c.custom) return;
    reach.textContent = "Working out who this reaches.";
    try {
      const p = await api(`/api/notify/candidate?id=${encodeURIComponent(c.id)}&channels=${[...S.channels].join(",")}`);
      S.preview = p; setLabels(p.labels);
      if (p.notice_id) { S.nid = p.notice_id; S.step = 4; return wizard(body); }
      const x = p.counts || {};
      reach.innerHTML = "";
      reach.append(h("b", { class: "num" }, n0(x.reached)), ` ${noun(x.reached, "customer", "customers")} reached on these channels, ${n0(x.life_support)} on life support (they also get a contact centre call)`,
        x.excluded?.no_chosen_channel ? h("span", { class: "nt-warn" }, `. ${n0(x.excluded.no_chosen_channel)} ${noun(x.excluded.no_chosen_channel, "customer has", "customers have")} none of these channels: add Mail to reach them.`) : ".");
    } catch (e) { reach.textContent = e.message; }
  };
  const what = c.custom ? card("Custom notice", "Describe the work and choose the feeders it interrupts.", custom.el)
    : card(c.kind === "planned_work" ? `Planned work on ${c.feeder_name}` : c.kind === "network_fault" ? `Network fault on ${c.feeder_name}` : "Weather hazard",
      c.window_label || "", h("p", { class: "muted" }, c.kind === "planned_work" ? `${c.works}, ${c.isolation}. ${n0(c.customers)} ${noun(c.customers, "customer", "customers")} off supply. Customers must be told by ${dayLabel(c.notice_by)}.`
        : c.kind === "network_fault" ? `${n0(c.customers_off)} ${noun(c.customers_off, "customer", "customers")} off supply, ${n0(c.life_support_off)} on life support.` : `Gusts up to ${Math.round(c.max_gust_kmh || 0)} km/h over ${n0(c.cell_count)} ${noun(c.cell_count, "forecast cell", "forecast cells")}.`));
  pane.append(what, card("Choose the channels", "Select one or more channels.", h("div", { class: "nt-chs" }, boxes),
    h("div", { class: "nt-ch-fixed" }, "Life support customers always receive a contact centre call, on top of their chosen channel."), reach,
    h("div", { class: "row nt-go" }, h("button", { class: "btn", onclick: () => { S.step = S.cand?.custom ? 1 : 2; wizard(body); } }, "Back"),
      h("button", { class: "btn primary", onclick: () => draftWithAI(body, custom) }, "Draft with AI"))));
  refreshReach();
}

async function customForm() {
  let feeders = [];
  try { feeders = (await api("/api/notify/feeders")).feeders || []; } catch (e) { toast(e.message, "red"); }
  const chosen = new Set();
  const list = h("div", { class: "nt-feeders" }, feeders.map((f) => h("label", { class: "nt-feeder" },
    h("input", { type: "checkbox", onchange: (e) => (e.target.checked ? chosen.add(f.feeder_id) : chosen.delete(f.feeder_id)) }),
    ` ${f.feeder_name} (${n0(f.customers)} ${noun(f.customers, "customer", "customers")})`)));
  const title = h("input", { placeholder: "Feeder reconductor, Ironbark Hill" });
  const desc = h("textarea", { rows: 3, placeholder: "What the work is, in plain words" });
  const start = h("input", { type: "datetime-local" }), end = h("input", { type: "datetime-local" });
  return { el: h("div", { class: "nt-form" }, h("label", {}, "Title", title), h("label", {}, "Description", desc),
    h("div", { class: "nt-two" }, h("label", {}, "Start", start), h("label", {}, "End", end)), h("label", {}, "Feeders", list),
    h("p", { class: "muted small" }, "A planned interruption needs four business days' notice.")),
    body: () => ({ notice_type: "project_work", title: title.value, description: desc.value, feeders: [...chosen], start: start.value, end: end.value }) };
}

async function draftWithAI(body, custom) {
  if (!S.channels.size) return toast("Choose at least one channel.");
  try {
    const req = custom ? { ...custom.body(), channels: [...S.channels] } : { candidate: S.cand.id, channels: [...S.channels] };
    const n = await post("/api/notify/notices", req);
    S.nid = n.notice.notice_id; S.step = 4;
    wizard(body);
  } catch (e) { toast(e.message, "red"); }
}

// 3: review the draft and release it
async function reviewStep(pane, body) {
  let v;
  try { v = await api(`/api/notify/notices/${S.nid}`); setLabels(v.labels); } catch (e) { pane.append(card("Could not open the notice", "", h("p", { class: "muted" }, e.message))); return; }
  if (v.ai.status === "running") {   // the AI is writing: one veil (the lineworker at work) while the draft is checked quietly every few seconds
    pane.append(loading("Drafting this notice with AI"));
    const nid = v.notice.notice_id;
    const poll = async () => {
      if (!S.root?.isConnected || S.nid !== nid) return;
      let x = null;
      try { x = await api(`/api/notify/notices/${nid}`); } catch (e) { /* look again */ }
      if (!x || x.ai.status === "running") { S.poll = setTimeout(poll, 3000); return; }
      wizard(body);   // drafted (or failed): draw the review once
    };
    S.poll = setTimeout(poll, 3000);
    return;
  }
  S.view = v;
  const n = v.notice, editable = n.status !== "sent";
  pane.append(h("div", { class: "nt-head-row" },
    h("div", {}, h("h2", { class: "nt-title" }, n.title), h("div", { class: "nt-meta" }, chip(n.notice_type_label), statusChip(n.status, n.status_label),
      h("span", { class: "muted small" }, n.window_label), n.send_at_label && n.status !== "draft" ? h("span", { class: "muted small" }, n.status === "sent" ? `Sent ${n.send_at_label}` : `Scheduled for ${n.send_at_label}`) : null,
      h("span", { class: "muted small" }, `Channels: ${n.channels.map(chName).join(", ")}`)))));
  if (v.ai.status === "failed") pane.append(h("div", { class: "note amber" }, `The AI draft did not finish: ${v.ai.error || "an unknown error"}. Redraft to try again.`));
  pane.append(h("div", { class: "nt-review" },
    h("div", { class: "nt-review-main" }, draftsCard(v, editable)),
    h("div", { class: "nt-review-side" }, reachCard(v), rulesCard(v.rules))));
  if (editable) pane.append(h("div", { class: "nt-foot" },   // the notice's actions close the page, at the bottom right
    h("button", { class: "btn", onclick: () => { S.step = 1; S.nid = null; wizard(body); } }, "Back"),
    h("button", { class: "btn", onclick: () => redraft(body) }, "Redraft with AI"),
    h("button", { class: "btn", onclick: () => saveEdits(body) }, "Save edits"),
    h("button", { class: "btn amber", disabled: Object.keys(n.drafts || {}).length ? null : "true", onclick: () => confirmRelease(body) }, n.status === "scheduled" ? "Change the release" : "Release notice")));
}

function draftsCard(v, editable) {
  const n = v.notice, drafts = n.drafts || {};
  const groups = Object.entries(n.draft_channels || {}).filter(([, chans]) => chans.length).map(([aud, chans]) => h("div", { class: "nt-group" }, h("h4", {}, audName(aud)),
    chans.map((ch) => h("label", { class: "nt-draft" }, h("span", { class: "nt-draft-h" }, chName(ch)),
      h("textarea", { "data-draft": `${aud}|${ch}`, rows: ch === "email" || ch === "mail" ? 7 : 3, readonly: editable ? null : "true" },
      (drafts[aud] || {})[ch] || "")))));
  return card("Drafted message", editable ? "Edit before releasing." : "Shown as released.", groups.length ? groups : h("p", { class: "muted" }, "No drafts yet. Redraft with AI."),
    (n.edits || []).length ? h("p", { class: "muted small" }, `Edited ${n.edits.length} ${noun(n.edits.length, "time", "times")}. Last edit by ${(n.edits[n.edits.length - 1].by || "").split("@")[0]}.`) : null);
}

function reachCard(v) {
  const c = v.counts || {}, src = v.messages || c.messages || {};
  const rows = [];
  for (const aud of Object.keys(src)) for (const ch of Object.keys(src[aud])) { const x = src[aud][ch]; rows.push({ aud, ch, n: typeof x === "number" ? x : x.messages }); }
  const msgTotal = c.messages_total ?? rows.reduce((a, r) => a + r.n, 0);
  const link = (val, text) => h("button", { class: "nt-count", onclick: () => customersList(v) }, h("b", { class: "num" }, n0(val)), ` ${text}`);
  return card("Who it reaches", "Select a count to see the customers.", h("div", { class: "nt-kpis2" }, link(c.reached ?? c.in_scope, "customers"), link(c.life_support, "on life support"),
    h("div", {}, h("b", { class: "num" }, n0(msgTotal)), ` ${noun(msgTotal, "message", "messages")}`)),
  table([{ l: "Audience", f: (r) => audName(r.aud) }, { l: "Channel", f: (r) => chName(r.ch) }, { l: "Messages", f: (r) => n0(r.n), r: true }], rows, null, { noScroll: true }),
  v.exclusions?.total ? h("details", { class: "nt-excl" }, h("summary", {}, `${n0(v.exclusions.total)} ${noun(v.exclusions.total, "customer", "customers")} left out`),
    h("ul", { class: "nt-list" }, (v.exclusions.by_reason || []).map((r) => h("li", {}, `${n0(r.n)} ${r.label}`)))) : null);
}

async function customersList(v) {
  const box = h("div", {}, h("p", { class: "muted" }, "Loading the customers."));
  const bg = modal(h("h2", {}, "Customers this notice reaches"), box, h("div", { class: "row" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Close")));
  bg.querySelector(".modal").classList.add("wide");
  try {
    const d = await api(`/api/notify/notices/${v.notice.notice_id}/customers`);
    box.innerHTML = "";
    box.append(h("p", { class: "muted" }, `${n0(d.total)} ${noun(d.total, "customer", "customers")}, ${n0(d.life_support)} on life support${d.shown < d.total ? `. The first ${n0(d.shown)} are listed, with life support customers first` : ""}.`),
      table([{ l: "NMI", k: "nmi" }, { l: "Address", f: (r) => r.address || "-" }, { l: "Suburb", f: (r) => r.suburb || "-" },
        { l: "Feeder", f: (r) => r.feeder || "-" }, { l: "Customer", f: (r) => r.customer_type || "-" },
        { l: "Life support", f: (r) => r.life_support ? h("span", { class: "chip rm-con" }, r.equipment || "Yes") : "No" },
        { l: "Reached by", f: (r) => r.channels.join(", ") }], d.customers, null, { max: 460 }));
  } catch (e) { box.innerHTML = ""; box.append(h("p", { class: "muted" }, e.message)); }
}

function rulesCard(rules) {
  return card("Business rules applied", "The number of customers or messages each rule changed.", table([{ l: "Rule", f: (r) => r.label }, { l: "Detail", f: (r) => r.detail }, { l: "Changed", k: "changed", r: true }],
    rules || [], null, { noScroll: true }));
}

function redraft(body) {
  const box = h("textarea", { rows: 3, style: "width:100%", placeholder: "For example: make it shorter and mention the street names" });
  const bg = modal(h("h2", {}, "Redraft with AI"), h("p", { class: "muted" }, "The AI writes the text again for the chosen channels. Your edits are replaced (they stay in the edit log)."), box,
    h("div", { class: "row" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn primary", onclick: async () => {
        try { await post(`/api/notify/notices/${S.nid}/redraft`, { note: box.value }); bg.remove(); wizard(body); } catch (e) { toast(e.message, "red"); }
      } }, "Redraft")));
}

async function saveEdits(body) {
  const v = S.view, edits = [];
  for (const [aud, chans] of Object.entries(v.notice.draft_channels || {})) for (const ch of chans) {
    const el = document.querySelector(`[data-draft="${aud}|${ch}"]`);
    if (el && el.value !== ((v.notice.drafts || {})[aud] || {})[ch]) edits.push({ audience: aud, channel: ch, text: el.value });
  }
  if (!edits.length) return toast("There are no changes to save.");
  try { await post(`/api/notify/notices/${S.nid}/drafts`, { edits }); toast("Edits saved."); wizard(body); } catch (e) { toast(e.message, "red"); }
}

function confirmRelease(body) {
  const v = S.view, n = v.notice, c = v.counts || {};
  const now = h("input", { type: "radio", name: "when", value: "now", checked: n.schedulable ? null : "checked" });
  const later = h("input", { type: "radio", name: "when", value: "scheduled", checked: n.schedulable ? "checked" : null });
  const bg = modal(h("h2", {}, "Release this notice?"),
    h("p", {}, `It reaches ${n0(c.reached)} ${noun(c.reached, "customer", "customers")} (${n0(c.life_support)} on life support) with ${n0(c.messages_total)} ${noun(c.messages_total, "message", "messages")}. Once a message goes, it cannot be taken back.`),
    h("div", { class: "plan-keep" }, n.schedulable ? h("label", {}, later, ` Schedule it for 9:00am on its notice date (${dayLabel(n.notice_by)})`) : null,
      h("label", {}, now, " Send it now (SMS and calls wait for 8:00am in quiet hours)")),
    h("div", { class: "row" }, h("button", { class: "btn", onclick: () => bg.remove() }, "Cancel"),
      h("button", { class: "btn amber", onclick: async () => {
        await saveQuiet();
        try {
          const r = await post(`/api/notify/notices/${S.nid}/release`, { when: later.checked ? "scheduled" : "now", confirm: true });
          bg.remove();
          toast(r.notice.status === "sent" ? "Notice sent." : `Notice scheduled for ${r.notice.send_at_label}.`);
          S.step = 1; S.nid = null;
          location.hash = "#/customers/notices/released";
        } catch (e) { toast(e.message, "red"); }
      } }, "Release notice")));
}

async function saveQuiet() {   // keep any edits made before releasing
  const v = S.view, edits = [];
  for (const [aud, chans] of Object.entries(v.notice.draft_channels || {})) for (const ch of chans) {
    const el = document.querySelector(`[data-draft="${aud}|${ch}"]`);
    if (el && el.value !== ((v.notice.drafts || {})[aud] || {})[ch]) edits.push({ audience: aud, channel: ch, text: el.value });
  }
  if (edits.length) await post(`/api/notify/notices/${S.nid}/drafts`, { edits });
  return edits.length;   // how many messages were changed
}

// ---------------------------------------------------------------- released notices
async function releasedList(body) {
  const d = await api("/api/notify/notices?status=released&limit=100");
  const rows = d.notices || [];
  body.append(card("Released customer notices", "Scheduled or sent, newest first.", rows.length ? table([
    { l: "Notice", f: (r) => h("span", {}, r.title, h("div", { class: "muted small" }, r.feeder_label || r.description || "")) },
    { l: "Type", f: (r) => r.notice_type_label },
    { l: "Window", f: (r) => r.window_label },
    { l: "Status", f: (r) => statusChip(r.status, r.status_label) },
    { l: "Goes or went", f: (r) => r.send_at_label || "-" },
    { l: "Customers reached", f: (r) => n0(r.reached), r: true },
    { l: "Channels", f: (r) => (r.channel_labels || []).join(", ") },
    { l: "Works plan", f: planCell },
  ], rows, (r) => noticeWindow(r), { max: 520 })
    : h("div", { class: "nt-empty" }, "No notice has been released yet.")));
}

// One released notice in full, in a window: what it is about, when it was released and when it goes (or went), who released it, how many
// it reaches and who was left out, the words on each channel, and the customers it was sent to (or will be)
async function noticeWindow(r) {
  const wait = loading("Opening the notice");
  document.body.append(wait);
  let v, c;
  try { [v, c] = await Promise.all([api(`/api/notify/notices/${r.notice_id}`), api(`/api/notify/notices/${r.notice_id}/customers`)]); }
  catch (e) { toast(e.message, "red"); return; }
  finally { wait.remove(); }
  setLabels(v.labels);
  const n = v.notice, k = v.counts || {}, sent = n.status === "sent", who = (x) => (x || "").split("@")[0] || "-";
  const when = (iso) => iso ? new Date(iso).toLocaleString("en-AU", { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : null;
  const kv = (rows) => h("div", { class: "kv" }, rows.filter(([, x]) => x !== null && x !== undefined && x !== "").map(([l, x]) => [h("div", { class: "k" }, l), h("div", {}, x)]).flat());
  const left = v.exclusions || {};
  const texts = Object.entries(n.draft_channels || {}).filter(([, chans]) => chans.length).map(([aud, chans]) => h("div", { class: "nt-group" },
    h("h4", {}, audName(aud)), chans.map((ch) => h("div", { class: "nt-draft" }, h("span", { class: "nt-draft-h" }, chName(ch)),
      h("div", { class: "nt-msg" }, ((n.drafts || {})[aud] || {})[ch] || "No text was written for this channel.")))));
  const people = (c.customers || []).length ? table([   // select a customer for their premises: details, past notices and outages, and supply
    { l: "NMI", k: "nmi" }, { l: "Address", f: (x) => [x.address, x.suburb].filter(Boolean).join(", ") || "-" }, { l: "Feeder", f: (x) => x.feeder || "-" },
    { l: "Customer type", f: (x) => x.customer_type || "-" }, { l: "Life support", f: (x) => x.life_support ? (x.equipment || "Yes") : "No" },
    { l: "Channels", f: (x) => (x.channels || []).join(", ") },
  ], c.customers, (x) => openPremises(x.nmi), { max: 320 }) : h("p", { class: "muted" }, "No customer is reached by this notice.");
  const bg = modal(h("h2", {}, n.title), h("p", { class: "muted" }, r.feeder_label || n.description || ""),
    kv([["Type", n.notice_type_label], ["Outage window", n.window_label], ["Status", n.status_label],
      ["Released", n.released_at ? `${when(n.released_at)} by ${who(n.released_by)}` : null],
      [sent ? "Sent" : "Goes", sent ? `${when(n.sent_at) || n.send_at_label || "-"}${n.sent_by ? ` by ${who(n.sent_by)}` : ""}` : n.send_at_label],
      ["Customers reached", `${n0(k.reached ?? c.total)}${(k.life_support ?? c.life_support) ? `, ${n0(k.life_support ?? c.life_support)} on life support` : ""}`],
      ["Left out", left.total ? (left.by_reason || []).map((x) => `${n0(x.n)} ${x.label.toLowerCase()}`).join("; ") : "None"],
      ["Channels", (r.channel_labels || []).join(", ")], ["Works plan", r.plan ? (r.plan.state === "Removed" ? "Works plan removed from registry" : `${r.plan.title} (${r.plan.state.toLowerCase()})`) : null],
      ["Notice ID", n.notice_id]]),
    h("h4", {}, sent ? "What was sent" : "What will be sent"), ...texts,
    h("h4", {}, sent ? `Who it was sent to (${n0(c.total)})` : `Who it will go to (${n0(c.total)})`),
    h("p", { class: "muted small" }, "Select a customer to open their premises: details, past notices and outages, and how they are supplied."),
    c.shown < c.total ? h("p", { class: "muted small" }, `Showing the first ${n0(c.shown)} of ${n0(c.total)} customers, life support first.`) : null,
    people,
    h("div", { class: "plan-actions" }, h("button", { class: "btn primary", onclick: () => bg.remove() }, "Close")));
  bg.querySelector(".modal").classList.add("nt-window");
}
