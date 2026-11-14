// The loading scene: a comic lineworker up a pole, his hands at work on the crossarm while the app loads. The sky is white and the picture goes
// out of focus towards its edges (a sharp layer over a softer one over a blurred one), so it melts into the app's veil.
// His hands and the gear swinging off his belt are short looping sprites laid exactly over the still picture, in every layer.
//
// loading() leaves a placeholder in the page (a screen, a panel or a drawer); while any placeholder is on screen the app is veiled (out of
// focus) and the scene plays over it, always in the centre of the screen.
const ART = "/static/loader8/";
const V = "?v=7";
// each moving part's box as [left, top, right, bottom] in percent of the picture
const SPRITES = [["hands", [44.244, 23.509, 63.883, 59.625]], ["hip", [28.781, 57.07, 45.711, 95.4]]];
const FRAMES = 16, FPS = 12;

const div = (cls, style = {}, parent) => { const d = document.createElement("div"); d.className = cls; Object.assign(d.style, style); if (parent) parent.append(d); return d; };
const img = (src, cls, parent) => { const i = document.createElement("img"); i.src = ART + src + V; i.className = cls; i.alt = ""; i.draggable = false; parent.append(i); return i; };

function build(stage) {
  img("worker.webp", "ls-pic", stage);
  const parts = SPRITES.map(([name, [l, t, r, b]]) => div("lw-hands", { left: l + "%", top: t + "%", width: (r - l) + "%", height: (b - t) + "%",
    backgroundImage: `url(${ART}${name}.webp${V})`, backgroundSize: `${FRAMES * 100}% 100%` }, stage));
  return { parts };
}

function frame(P, t) {
  const k = Math.floor(t * FPS) % FRAMES;
  for (const el of P.parts) el.style.backgroundPosition = `${(k / (FRAMES - 1)) * 100}% 0`;
}

function lampScene(msg) {
  const box = document.createElement("div");
  box.className = "ls-scene lw";
  const frameBox = div("ls-frame", {}, box);
  const hazeWrap = div("ls-hazewrap", {}, frameBox);   // a blurred copy behind, so the edges dissolve out of focus
  const midWrap = div("lw-midwrap", {}, frameBox);    // a lightly blurred copy between, so the focus falls off gradually
  const haze = div("ls-stage ls-haze", {}, hazeWrap), mid = div("ls-stage lw-mid", {}, midWrap), stage = div("ls-stage ls-sharp", {}, frameBox);
  const cap = div("ls-cap", {}, box); cap.textContent = msg;
  box.caption = (m) => { cap.textContent = m; };
  const Hz = build(haze), Hm = build(mid), P = build(stage), t0 = performance.now();
  frame(P, 0);
  const tick = () => {
    if (!box.isConnected && performance.now() - t0 > 500) return;
    const t = box.dataset.t != null ? +box.dataset.t : (performance.now() - t0) / 1000;   // data-t freezes a moment (tests)
    for (const L of [P, Hm, Hz]) frame(L, t);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return box;
}

// ---------------------------------------------------------------- the app-wide veil
let veil = null, scene = null, seenAt = 0, hideTimer = 0;
function show(msg) {
  if (!veil) veil = div("ls-veil", {}, document.body);
  clearTimeout(hideTimer);
  if (!scene) { scene = lampScene(msg); veil.innerHTML = ""; veil.append(scene); }
  else scene.caption(msg);
  veil.classList.add("on");
}
function hide() {
  if (!veil || !veil.classList.contains("on")) return;
  veil.classList.remove("on");
  hideTimer = setTimeout(() => { if (veil && !veil.classList.contains("on")) { veil.innerHTML = ""; scene = null; } }, 400);
}
export function waitMarker(msg) {   // what loading() puts in the page: invisible, it just holds the space and the message
  const d = document.createElement("div");
  d.className = "ll-wait"; d.dataset.msg = msg;
  return d;
}
setInterval(() => {
  const w = [...document.querySelectorAll(".ll-wait")].pop();
  if (w) { if (!seenAt) seenAt = performance.now(); if (performance.now() - seenAt > 160) show(w.dataset.msg); }   // quick loads never flash the veil
  else { seenAt = 0; hide(); }
}, 80);

// Warm the browser cache so the first veil paints at once.
for (const f of ["worker.webp", "hands.webp", "hip.webp"]) { const i = new Image(); i.src = ART + f + V; }
