// The small "i" beside every tile, column, panel title and headline figure: hover (or tap) it to read what the number means and how it
// is counted. Any element with the class "has-tip" and a data-info text shows the same note on hover (a figure explaining itself). The texts live in glossary.js, keyed by the label as shown; a key "Panel title / Label" (or "#/screen / Label") wins over a
// plain "Label", so the same word can mean different things in different places. Screens don't add the dots themselves: anything rendered is decorated here.
// A screen can add to a note without replacing it: a data-more text on the tile (or any element around the label) follows the glossary text.
import { INFO } from "./glossary.js";

const LABELS = [".tile .k", ".statrow .k", ".hero .kh", "table.t th", ".card > h3", "#drawer h4"].join(",");
const own = (el) => [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim();   // the label, not its sub-title
const panel = (el) => { const h3 = el.closest(".card")?.querySelector(":scope > h3"); return h3 && h3 !== el ? own(h3) : ""; };

function lookup(el) {
  const label = own(el); if (!label) return null;
  const p = panel(el), parts = location.hash.split("/"), at = (k) => INFO[`${k} / ${label}`];
  return (p && at(p)) || (el.closest("#drawer") && at("#drawer")) || at(parts.slice(0, 3).join("/")) || at(parts.slice(0, 2).join("/")) || INFO[label] || null;   // "#/res/control", then "#/res"
}

function decorate(root) {
  const els = root.matches?.(LABELS) ? [root] : [...(root.querySelectorAll?.(LABELS) || [])];
  for (const el of els) {
    if (el.querySelector(":scope > .info")) continue;
    const more = el.closest("[data-more]")?.dataset.more, base = lookup(el), text = [base, more].filter(Boolean).join("\n\n");
    if (!text) continue;
    const dot = document.createElement("span");
    dot.className = "info"; dot.textContent = "i"; dot.tabIndex = 0; dot.dataset.info = text; dot.setAttribute("aria-label", `About ${own(el)}`);
    if (more) dot.dataset.wide = "1";   // a longer note gets a wider box
    const sub = el.querySelector(":scope > .sub");
    sub ? el.insertBefore(dot, sub) : el.append(dot);
  }
}

// one floating note for the whole app
let tip = null, pinned = null;
function show(dot) {
  if (!tip) { tip = document.createElement("div"); tip.className = "infotip"; document.body.append(tip); }
  tip.textContent = dot.dataset.info; tip.classList.add("on");
  const r = dot.getBoundingClientRect(), w = Math.min(dot.dataset.wide ? 440 : 300, window.innerWidth - 24);
  tip.style.maxWidth = w + "px";
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  const left = Math.max(12, Math.min(window.innerWidth - tw - 12, r.left + r.width / 2 - tw / 2));
  const below = r.bottom + 8 + th < window.innerHeight;
  tip.style.left = left + "px"; tip.style.top = (below ? r.bottom + 8 : r.top - th - 8) + "px";
}
function hide() { pinned = null; tip?.classList.remove("on"); }

document.addEventListener("mouseover", (e) => { const d = e.target.closest?.(".info, .has-tip"); if (d) show(d); });
document.addEventListener("mouseout", (e) => { const d = e.target.closest?.(".info, .has-tip"); if (d && d !== pinned) hide(); });
document.addEventListener("focusin", (e) => { if (e.target.classList?.contains("info") || e.target.classList?.contains("has-tip")) show(e.target); });
document.addEventListener("focusout", (e) => { if (e.target.classList?.contains("info") || e.target.classList?.contains("has-tip")) hide(); });
document.addEventListener("click", (e) => {   // a tap pins the note (touch screens); the click never reaches the row or tile behind it
  const d = e.target.closest?.(".info");   // only the "i" is pinned on a tap; a figure with a note still passes its click on
  if (d) { e.stopPropagation(); e.preventDefault(); if (pinned === d) hide(); else { show(d); pinned = d; } }
  else if (pinned) hide();
}, true);
window.addEventListener("scroll", hide, true);

new MutationObserver((muts) => { for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1) decorate(n); })
  .observe(document.body, { childList: true, subtree: true });
decorate(document.body);
