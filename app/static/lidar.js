// LiDAR view of an overhead span: the span drawn as the classified point cloud an aerial LiDAR survey delivers.
// The measured values come from /api/vegetation/lidar/<span>: the span's length and conductor, its two poles (height, material, lean),
// and the latest survey's clearance now and in 12 months, species, growth a year, hazard tree and bushfire-prone land.
//
// How the cloud is made. The poles and the vegetation are the Meshy models; their surfaces are sampled densely (area-weighted, with the
// colour of their textures), each tree varied in size, shape, turn and lean. A simulated survey then decides which samples a laser would
// return: two flight lines 70 m up, pulses on an along-track and across-track grid, the first surface in each pulse's path returned and
// what lies behind it returned only through gaps (later returns), so the cloud is heavy on canopy tops, thin on trunks, shadowed under
// trees, striped by the scan lines, and the wires come back as broken trails. The conductors hang between the real pole heights with a
// sag for the span's length. The nearest vegetation is placed so that its gap to the nearest conductor is the surveyed clearance, and
// grown and moved so that the 12-month gap is the predicted one; the points that set the gap are always kept.
//
// The engineering overlays show the clearance zone round each conductor, the conductor at its maximum operating temperature (more sag) and
// swung by wind, and a hazard tree's fall-in reach. Inside the window the line can be walked span by span along the feeder; opened from a
// post-cut audit, the view compares the bay before and after the cut.
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { MeshoptDecoder } from "three/addons/libs/meshopt_decoder.module.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { api, h, n0, n1, n2, modal, toast } from "./ui.js";

const loader = new GLTFLoader(); loader.setMeshoptDecoder(MeshoptDecoder);
const MODELS = new Map();   // model name -> its loaded scene (shared between views)
const model = (name) => {
  if (!MODELS.has(name)) MODELS.set(name, loader.loadAsync(`/static/models_web/${name}.glb`).then((g) => g.scene).catch(() => null));
  return MODELS.get(name);
};

// the model and the usual height for each species the survey records
const SPECIES = { "Eucalyptus (fast)": ["gum_tree_tall", 18], "Eucalyptus (moderate)": ["gum_tree_tall", 15], "Exotic street tree": ["street_tree", 9],
  "Native shrub": ["native_shrub", 3.5], "Acacia": ["acacia_wattle", 7], "Casuarina": ["casuarina", 12] };
const MIX = [["gum_tree_tall", 15], ["street_tree", 9], ["acacia_wattle", 7], ["casuarina", 12], ["native_shrub", 3.5]];   // the rest of a bay
const HAZARD = ["hazard_tree", 16];
// point classes (the ASPRS LAS codes they stand for: 2 ground, 3 to 5 vegetation, 14 wire conductor, 15 transmission tower or pole)
const GROUND = 0, VEG = 1, POLE = 2, WIRE = 3;
// the colours of the points that are not vegetation, in the Clearance view
const CLASS_RGB = { [GROUND]: [0.62, 0.47, 0.30], [VEG]: [0.24, 0.72, 0.36], [POLE]: [0.95, 0.58, 0.18], [WIRE]: [0.30, 0.86, 1.0] };
const BAND = { inside: [1.0, 0.23, 0.19], near: [1.0, 0.69, 0.13], clear: [0.24, 0.80, 0.43] };
const TIER_HEX = { high_risk: "#d2462f", cut_required: "#c98a1e", planned: "#2f7dc4", no_action: "#2e9c6a" };
// the simulated survey: flight height and lines, and the pulse spacing on the ground
const FLY_H = 70, FLY_Z = [-18, 18], PULSE = 0.11, LINE_GAP = 0.33;
const HOT_SAG = 1.35, SWAY_DEG = 30;   // the conductor at maximum operating temperature sags about a third more; wind swings it 30 degrees

// a repeatable random sequence for each span, so the same span always looks the same
function rng(seed) {
  let a = [...seed].reduce((s, c) => (Math.imul(s ^ c.charCodeAt(0), 2654435761) >>> 0), 2166136261);
  return () => { a = (a + 0x6d2b79f5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

// ---------------------------------------------------------------- a model as points
// Each model is sampled once into a pool of surface points (area-weighted, with the colour of its texture), standing on y = 0, centred, one
// unit tall. A tree then takes a slice of its model's pool (the samples are in random order, so any slice is a fair sample) and turns,
// stretches, leans and scales it, so no two trees are alike and no span samples a model from scratch.
const POOL_N = 120000, POOLS = new WeakMap();
function texReader(mat) {
  const img = mat?.map?.image;
  if (!img || !img.width) { const c = mat?.color || new THREE.Color(0.5, 0.5, 0.5); return () => [c.r, c.g, c.b]; }
  const cv = document.createElement("canvas"); cv.width = Math.min(512, img.width); cv.height = Math.min(512, img.height);
  const cx = cv.getContext("2d", { willReadFrequently: true }); cx.drawImage(img, 0, 0, cv.width, cv.height);
  const px = cx.getImageData(0, 0, cv.width, cv.height).data, W = cv.width, H = cv.height;
  return (u, v) => {
    const x = Math.min(W - 1, Math.max(0, Math.floor((u - Math.floor(u)) * W))), y = Math.min(H - 1, Math.max(0, Math.floor((v - Math.floor(v)) * H)));
    const i = (y * W + x) * 4; return [px[i] / 255, px[i + 1] / 255, px[i + 2] / 255];
  };
}
function pool(scene) {
  if (POOLS.has(scene)) return POOLS.get(scene);
  // the model's triangles in world space (9 numbers each), their texture coordinates (6), their texture and their running area
  const V = [], UV = [], READ = [], meshes = [];
  scene.updateMatrixWorld(true);
  scene.traverse((m) => { if (m.isMesh) meshes.push(m); });
  const v = new THREE.Vector3();
  for (const m of meshes) {
    const g = m.geometry, pos = g.attributes.position, uv = g.attributes.uv, idx = g.index, read = texReader(Array.isArray(m.material) ? m.material[0] : m.material);
    const count = idx ? idx.count : pos.count;
    for (let i = 0; i < count; i++) {
      const j = idx ? idx.getX(i) : i;
      v.fromBufferAttribute(pos, j).applyMatrix4(m.matrixWorld); V.push(v.x, v.y, v.z);
      UV.push(uv ? uv.getX(j) : 0, uv ? uv.getY(j) : 0);
      if (i % 3 === 0) READ.push(read);
    }
  }
  const nt = READ.length, cum = new Float64Array(nt); let total = 0;
  for (let t = 0; t < nt; t++) {
    const o = t * 9, ax = V[o + 3] - V[o], ay = V[o + 4] - V[o + 1], az = V[o + 5] - V[o + 2], bx = V[o + 6] - V[o], by = V[o + 7] - V[o + 1], bz = V[o + 8] - V[o + 2];
    total += Math.hypot(ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx) / 2; cum[t] = total;
  }
  if (!nt || !total) { POOLS.set(scene, null); return null; }
  const rand = rng("pool"), p = new Float32Array(POOL_N * 3), c = new Float32Array(POOL_N * 3);
  let minY = Infinity, maxY = -Infinity, sx = 0, sz = 0;
  for (let k = 0; k < POOL_N; k++) {
    const r = rand() * total; let lo = 0, hi = nt - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (cum[mid] < r) lo = mid + 1; else hi = mid; }
    let a = rand(), b = rand(); if (a + b > 1) { a = 1 - a; b = 1 - b; }
    const w = 1 - a - b, o = lo * 9, u = lo * 6;
    for (let d = 0; d < 3; d++) p[k * 3 + d] = V[o + d] * w + V[o + 3 + d] * a + V[o + 6 + d] * b;
    const col = READ[lo](UV[u] * w + UV[u + 2] * a + UV[u + 4] * b, UV[u + 1] * w + UV[u + 3] * a + UV[u + 5] * b);
    c[k * 3] = col[0]; c[k * 3 + 1] = col[1]; c[k * 3 + 2] = col[2];
    minY = Math.min(minY, p[k * 3 + 1]); maxY = Math.max(maxY, p[k * 3 + 1]); sx += p[k * 3]; sz += p[k * 3 + 2];
  }
  const s = 1 / Math.max(1e-6, maxY - minY), mx = sx / POOL_N, mz = sz / POOL_N;
  for (let k = 0; k < POOL_N; k++) { p[k * 3] = (p[k * 3] - mx) * s; p[k * 3 + 1] = (p[k * 3 + 1] - minY) * s; p[k * 3 + 2] = (p[k * 3 + 2] - mz) * s; }
  const out = { p, c };
  POOLS.set(scene, out);
  return out;
}
// n points of a model, scaled to the given height and varied: turned, stretched across, and leaning (vary = 0 keeps it as it is, for poles)
function samplePoints(scene, n, height, rand, vary = 1) {
  const P = scene && pool(scene);
  if (!P) return null;
  const p = new Float32Array(n * 3), c = new Float32Array(n * 3), start = Math.floor(rand() * POOL_N);
  const turn = rand() * Math.PI * 2 * vary, ct = Math.cos(turn), st = Math.sin(turn);
  const wide = 1 + (rand() - 0.5) * 0.4 * vary, deep = 1 + (rand() - 0.5) * 0.4 * vary, lean = (rand() - 0.5) * 0.12 * vary, leanDir = rand() * Math.PI * 2;
  const lx = lean * Math.cos(leanDir), lz = lean * Math.sin(leanDir);
  for (let k = 0; k < n; k++) {
    const q = ((start + k) % POOL_N) * 3;
    const x0 = P.p[q] * height * wide, y = P.p[q + 1] * height, z0 = P.p[q + 2] * height * deep;
    p[k * 3] = x0 * ct - z0 * st + y * lx; p[k * 3 + 1] = y; p[k * 3 + 2] = x0 * st + z0 * ct + y * lz;
    c[k * 3] = P.c[q]; c[k * 3 + 1] = P.c[q + 1]; c[k * 3 + 2] = P.c[q + 2];
  }
  return { p, c };
}
// every model the view uses, loaded and sampled ahead of time, one at a time while the browser is idle, so opening a span does not wait for it
const ALL_MODELS = ["gum_tree_tall", "street_tree", "acacia_wattle", "casuarina", "native_shrub", "hazard_tree", "wood_pole", "concrete_pole"];
let warming = null;
export function warmLidar() {
  if (warming) return warming;
  const idle = () => new Promise((r) => (window.requestIdleCallback ? requestIdleCallback(() => r(), { timeout: 1500 }) : setTimeout(r, 50)));
  warming = (async () => { for (const n of ALL_MODELS) { const sc = await model(n); await idle(); if (sc) pool(sc); } })();
  return warming;
}

// ---------------------------------------------------------------- the conductors
// Three phases on the crossarm, hanging between the two pole tops with a parabolic sag that grows with the span length. The same line at
// maximum operating temperature sags further; swung by wind, each point turns about the straight line between its attachments.
function wires(L, h0, h1, sagK = 1, swayDeg = 0) {
  const sag = Math.min(3.5, Math.max(0.3, 0.015 * L + 0.2)) * sagK, step = 0.2, a = THREE.MathUtils.degToRad(swayDeg), out = [];
  for (const z of [-0.9, 0, 0.9]) {
    const n = Math.ceil(L / step) + 1, pts = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const x = Math.min(L, i * step), chord = h0 + (h1 - h0) * (x / L), drop = 4 * sag * x * (L - x) / (L * L);
      pts[i * 3] = x; pts[i * 3 + 1] = chord - drop * Math.cos(a); pts[i * 3 + 2] = z + drop * Math.sin(a);
    }
    out.push({ z, pts, step });
  }
  return { list: out, sag };
}
// the distance from a point to the nearest conductor (each conductor is a fine polyline; only the segments within a metre of the point's x are
// checked: a conductor's slope is gentle, so the nearest point on it is never further along the span than that)
function wireDist(W, x, y, z) {
  let best = Infinity;
  for (const w of W.list) {
    const n = w.pts.length / 3, i0 = Math.max(0, Math.floor(x / w.step) - 5), i1 = Math.min(n - 2, Math.floor(x / w.step) + 5);   // within 1 m of x
    if (i0 > i1) { const j = x < 0 ? 0 : n - 1; best = Math.min(best, Math.hypot(x - w.pts[j * 3], y - w.pts[j * 3 + 1], z - w.pts[j * 3 + 2])); continue; }
    for (let i = i0; i <= i1; i++) {
      const ax = w.pts[i * 3], ay = w.pts[i * 3 + 1], az = w.pts[i * 3 + 2], bx = w.pts[i * 3 + 3], by = w.pts[i * 3 + 4], bz = w.pts[i * 3 + 5];
      const dx = bx - ax, dy = by - ay, dz = bz - az, l2 = dx * dx + dy * dy + dz * dz;
      const t = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy + (z - az) * dz) / l2));
      best = Math.min(best, Math.hypot(x - ax - t * dx, y - ay - t * dy, z - az - t * dz));
    }
  }
  return best;
}
function wirePoint(W, v) {   // the nearest point on any conductor to a point
  let best = Infinity, out = null;
  for (const w of W.list) for (let i = 0; i < w.pts.length / 3; i++) {
    const q = new THREE.Vector3(w.pts[i * 3], w.pts[i * 3 + 1], w.pts[i * 3 + 2]), dd = v.distanceTo(q);
    if (dd < best) { best = dd; out = q; }
  }
  return out;
}

// a tree's points placed at (x, z), grown by scale about its base and then moved by an offset: [x, y, z] for point k
const NO_MOVE = [0, 0, 0];
const placed = (t, k, x, z, s, o = NO_MOVE) => [x + t.p[k * 3] * s + o[0], t.p[k * 3 + 1] * s + o[1], z + t.p[k * 3 + 2] * s + o[2]];
function minGap(t, x, z, s, W, every = 1, o = NO_MOVE) {
  let best = Infinity, at = 0;
  for (let k = 0; k < t.p.length / 3; k += every) {
    const [px, py, pz] = placed(t, k, x, z, s, o), d = wireDist(W, px, py, pz);
    if (d < best) { best = d; at = k; }
  }
  return { d: best, k: at };
}
// bisection on a monotonic gap: the value of v in [lo, hi] at which gap(v) meets the target (rising: the gap widens as v grows)
function solve(gap, lo, hi, target, rising, steps = 22) {   // 22 halvings settle 40 m to a millimetre; 8 settle a 0.8 m refinement to 3 mm
  for (let i = 0; i < steps; i++) { const m = (lo + hi) / 2, g = gap(m); if ((g < target) === rising) lo = m; else hi = m; }
  return (lo + hi) / 2;
}

// ---------------------------------------------------------------- the simulated survey
// keep[i] = 1 when a pulse from either flight line returns sample i. Each pulse footprint (about 11 cm across, on an along-track and an
// across-track grid) returns the first surface along its path, one point; what lies behind it comes back now and then as a later return
// through a gap in the canopy (the ground's last return a little more often). The along-track scan lines leave thin gaps between them, as
// a real swath does. Wire samples return sparsely (a 2 cm conductor in an 11 cm footprint), so the wires come back as broken trails.
function survey(P, cls, n, L, rand, force) {
  const keep = new Uint8Array(n), nx = Math.ceil((L + 30) / PULSE), dT = PULSE / FLY_H, nt = Math.ceil(1.6 / dT);
  for (const fz of FLY_Z) {
    const first = new Float32Array(nx * nt).fill(Infinity), firstAt = new Int32Array(nx * nt).fill(-1), bin = new Int32Array(n), range = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const x = P[i * 3], y = P[i * 3 + 1], z = P[i * 3 + 2];
      const bx = Math.floor((x + 15) / PULSE), bt = Math.floor((Math.atan2(z - fz, FLY_H - y) + 0.8) / dT);
      if (bx < 0 || bx >= nx || bt < 0 || bt >= nt || ((x + 15) % LINE_GAP) / LINE_GAP > 0.82) { bin[i] = -1; continue; }   // outside, or between scan lines
      const k = bx * nt + bt, r = Math.hypot(FLY_H - y, z - fz);
      bin[i] = k; range[i] = r;
      if (cls[i] !== WIRE && r < first[k]) { first[k] = r; firstAt[k] = i; }
    }
    for (let k = 0; k < firstAt.length; k++) if (firstAt[k] >= 0) keep[firstAt[k]] = 1;   // the first return of each pulse
    for (let i = 0; i < n; i++) {
      if (keep[i] || bin[i] < 0) continue;
      if (cls[i] === WIRE) { if (rand() < 0.08) keep[i] = 1; continue; }
      // a pole's sides face the scanner obliquely: an unobstructed side returns often, as it does on a low, oblique helicopter pass
      if (cls[i] === POLE) { if (range[i] - first[bin[i]] < 1.5 && rand() < 0.3) keep[i] = 1; continue; }
      if (range[i] - first[bin[i]] > 0.5 && rand() < (cls[i] === GROUND ? 0.09 : 0.05)) keep[i] = 1;   // a later return through a gap
    }
  }
  for (const i of force) keep[i] = 1;
  return keep;
}

// ---------------------------------------------------------------- the scene for one span
async function build(d, cut) {
  const T = { t: performance.now(), out: {}, mark(k) { const n = performance.now(); this.out[k] = Math.round(n - this.t); this.t = n; } };
  const rand = rng(d.span_id), L = d.length_m || 60;
  const ps = d.poles.find((p) => p.end_at === "start"), pe = d.poles.find((p) => p.end_at === "end");
  const H0 = ps?.height_m || 11, H1 = pe?.height_m || 11;
  const W = wires(L, H0 - 0.6, H1 - 0.6), Whot = wires(L, H0 - 0.6, H1 - 0.6, HOT_SAG), Wsway = wires(L, H0 - 0.6, H1 - 0.6, HOT_SAG, SWAY_DEG);
  const [sp, sh] = SPECIES[d.species] || SPECIES["Eucalyptus (moderate)"];
  const poleName = (p) => ((p?.material || "").toUpperCase().startsWith("CONC") ? "concrete_pole" : "wood_pole");
  const names = [...new Set([sp, ...MIX.map(([m]) => m), d.hazard_tree ? HAZARD[0] : null, poleName(ps), poleName(pe)].filter(Boolean))];
  warmLidar();   // the rest of the models carry on loading in the background
  const loaded = Object.fromEntries(await Promise.all(names.map(async (n) => [n, await model(n)])));
  const pick = (n) => loaded[n] || loaded.gum_tree_tall;
  const parts = [];   // { cls, p: positions now, q: positions in the later state (vegetation only), c: true colour, force: keep these }

  T.mark('models');
  // the ground: a gently uneven strip along the span, sampled densely (the survey thins it), grass and bare earth
  const gx0 = -14, gz = 18, gStep = 0.075, nx = Math.floor((L + 28) / gStep) + 1, nz = Math.floor((2 * gz) / gStep) + 1;
  const gp = new Float32Array(nx * nz * 3), gc = new Float32Array(nx * nz * 3);
  for (let i = 0, k = 0; i < nx; i++) for (let j = 0; j < nz; j++, k += 3) {
    const jx = gx0 + i * gStep + (rand() - 0.5) * gStep, jz = -gz + j * gStep + (rand() - 0.5) * gStep;
    gp[k] = jx; gp[k + 1] = 0.35 * Math.sin(jx * 0.07 + 1.3) * Math.cos(jz * 0.11) + 0.15 * Math.sin(jx * 0.31 + jz * 0.23) + (rand() - 0.5) * 0.04; gp[k + 2] = jz;
    const g = 0.3 + rand() * 0.2, earth = rand() < 0.18;
    gc[k] = earth ? g + 0.12 : g * 0.85; gc[k + 1] = earth ? g + 0.02 : g + 0.1; gc[k + 2] = earth ? g * 0.7 : g * 0.55;
  }
  parts.push({ cls: GROUND, p: gp, c: gc });

  T.mark('ground');
  // the two poles, the end pole leaning by the surveyed lean
  for (const [p0, x, H, lean] of [[ps, 0, H0, 0], [pe, L, H1, d.pole_lean_deg || 0]]) {
    const t = samplePoints(pick(poleName(p0)), 26000, H, rand, 0); if (!t) continue;
    // the crossarm must run across the line (along z): measure the spread of the pole's top fifth and turn the pole a quarter if needed
    let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    for (let k = 0; k < t.p.length / 3; k++) if (t.p[k * 3 + 1] > H * 0.8) {
      x0 = Math.min(x0, t.p[k * 3]); x1 = Math.max(x1, t.p[k * 3]); z0 = Math.min(z0, t.p[k * 3 + 2]); z1 = Math.max(z1, t.p[k * 3 + 2]);
    }
    if (x1 - x0 > z1 - z0) for (let k = 0; k < t.p.length / 3; k++) { const q = t.p[k * 3]; t.p[k * 3] = -t.p[k * 3 + 2]; t.p[k * 3 + 2] = q; }
    const a = THREE.MathUtils.degToRad(lean), p = new Float32Array(t.p.length);
    for (let k = 0; k < t.p.length / 3; k++) {
      const y = t.p[k * 3 + 1], z = t.p[k * 3 + 2];
      p[k * 3] = x + t.p[k * 3]; p[k * 3 + 1] = y * Math.cos(a) - z * Math.sin(a); p[k * 3 + 2] = y * Math.sin(a) + z * Math.cos(a);
    }
    parts.push({ cls: POLE, p, c: t.c });
  }
  T.mark('poles');
  // the conductors, sampled every 3 cm with a little scatter; the survey keeps about one in five
  for (const w of W.list) {
    const n = Math.ceil(L / 0.03), p = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const x = (i / (n - 1)) * L, j = Math.min(w.pts.length / 3 - 2, Math.floor(x / w.step)), f = x / w.step - j;
      for (let c = 0; c < 3; c++) p[i * 3 + c] = w.pts[j * 3 + c] * (1 - f) + w.pts[j * 3 + 3 + c] * f + (c ? (rand() - 0.5) * 0.04 : 0);
    }
    parts.push({ cls: WIRE, p, c: new Float32Array(p.length).fill(0.75) });
  }

  T.mark('wires');
  // the nearest vegetation: placed so its gap to the nearest conductor is the surveyed clearance, then a year's growth and a move along the
  // line to the conductor until the gap is the predicted one (a prediction past the conductor carries the vegetation that far beyond it)
  // the gap to draw: the surveyed clearance, or (opened from an audit) the clearance measured before the cut
  const target = Math.max(0.05, cut?.before ?? d.clearance_m), c12 = Math.min(d.clearance_12m_m ?? d.clearance_m, d.clearance_m);
  const crit = samplePoints(pick(sp), 90000, sh, rand), cx = L * (0.38 + rand() * 0.24);
  let cz, cs = 1;
  if (minGap(crit, cx, 0, 1, W, 12).d > target) {   // too short to reach the gap from the side: it stands under the line and is taller
    cz = 0.2; cs = solve((s) => minGap(crit, cx, cz, s, W, 12).d, 1, 6, target, false, 16);   // the gap shrinks as it grows
    cs = solve((s) => minGap(crit, cx, cz, s, W, 3).d, cs * 0.97, cs * 1.03, target, false, 8);   // then finely, on more of its points
  } else {
    cz = solve((z) => minGap(crit, cx, z, 1, W, 12).d, 0, 40, target, true, 16);   // the gap widens as it stands further off (to 1 cm)
    cz = solve((z) => minGap(crit, cx, z, 1, W, 3).d, cz - 0.4, cz + 0.4, target, true, 8);
  }
  const g12 = cs * (1 + (d.growth_m_per_year || 0.6) / sh), m12 = minGap(crit, cx, cz, g12, W, 1);
  const p12 = new THREE.Vector3(...placed(crit, m12.k, cx, cz, g12)), dir = wirePoint(W, p12).sub(p12).normalize();
  const along = (t) => [dir.x * t, dir.y * t, dir.z * t];
  let t12 = m12.d - c12;
  if (c12 > 0.03) { t12 = solve((t) => minGap(crit, cx, cz, g12, W, 12, along(t)).d, -8, m12.d, c12, false, 16); t12 = solve((t) => minGap(crit, cx, cz, g12, W, 3, along(t)).d, t12 - 0.4, t12 + 0.4, c12, false, 8); }
  // after a cut (opened from an audit): the vegetation within the post-cut clearance is gone
  const veg = [[crit, cx, cz, cs, cut ? cs : g12, cut ? NO_MOVE : along(t12), true]];
  T.mark('nearest_tree');
  // the rest of the bay's vegetation, a mix of species, kept well back from the line (2 m beyond the nearest, also after a year's growth)
  const others = Math.max(4, Math.min(12, Math.round(L / 11)));
  for (let i = 0; i < others; i++) {
    const [m, hh] = rand() < 0.55 ? [sp, sh] : MIX[Math.floor(rand() * MIX.length)];
    const t = samplePoints(pick(m), 42000, hh * (0.7 + rand() * 0.55), rand); if (!t) continue;
    const x = 4 + rand() * (L - 8), side = rand() < 0.5 ? -1 : 1, grow = 1 + (d.growth_m_per_year || 0.6) / hh;
    let z = side * (4 + rand() * 9);
    for (let tries = 0; tries < 14 && minGap(t, x, z, grow, W, 16).d < target + 2; tries++) z += side * 1.5;
    veg.push([t, x, z, 1, cut ? 1 : grow, NO_MOVE, false]);
  }
  for (let i = 0; i < Math.round(L / 9); i++) {   // undergrowth: low shrubs scattered through the bay
    const t = samplePoints(pick("native_shrub"), 9000, 0.8 + rand() * 1.6, rand); if (!t) continue;
    veg.push([t, rand() * L, (rand() < 0.5 ? -1 : 1) * (2.5 + rand() * 13), 1, 1, NO_MOVE, false]);
  }
  let hazard = null;
  if (d.hazard_tree) {   // the hazard tree: tall and dead, back from the line but within falling distance of it
    const t = samplePoints(pick(HAZARD[0]), 50000, HAZARD[1], rand);
    if (t) {
      const x = L * (0.15 + rand() * 0.7); let z = -(9 + rand() * 4);
      while (minGap(t, x, z, 1, W, 16).d < target + 2) z -= 1;
      veg.push([t, x, z, 1, 1, NO_MOVE, false]); hazard = { x, z, height: HAZARD[1] };
    }
  }
  for (const [t, x, z, s, s2, o2, isCrit] of veg) {
    const n = t.p.length / 3, p = new Float32Array(n * 3), q = new Float32Array(n * 3), force = [];
    for (let k = 0; k < n; k++) { p.set(placed(t, k, x, z, s), k * 3); q.set(placed(t, k, x, z, s2, o2), k * 3); }
    if (isCrit) {   // the points that set the gap, now and later, are always returned so the measured gap stays visible
      for (let k = 0; k < n; k++) {
        const dn = wireDist(W, p[k * 3], p[k * 3 + 1], p[k * 3 + 2]), dl = wireDist(W, q[k * 3], q[k * 3 + 1], q[k * 3 + 2]);
        if (dn < target + 0.6 || dl < Math.max(0.03, c12) + 0.6) force.push(k);
      }
    }
    parts.push({ cls: VEG, p, q, c: t.c, force, crit: isCrit });
  }

  T.mark('other_veg');
  // the survey: which samples come back, then one cloud ordered along the span so the survey pass can reveal it from pole to pole
  const total = parts.reduce((a, x) => a + x.p.length / 3, 0);
  const P = new Float32Array(total * 3), C = new Uint8Array(total), force = [];
  let o = 0;
  for (const x of parts) {
    P.set(x.p, o * 3); C.fill(x.cls, o, o + x.p.length / 3);
    for (const k of x.force || []) force.push(o + k);
    o += x.p.length / 3;
  }
  T.mark("pack"); const keep = survey(P, C, total, L, rand, force); T.mark("survey");
  // the returns in span order: a counting sort on 2 cm slices of the span (the order inside a slice does not matter to the pass)
  const partOf = new Uint16Array(total), kOf = new Uint32Array(total), slice = (x) => Math.max(0, Math.min(nSlice - 1, Math.floor((x + 15) / 0.02)));
  const nSlice = Math.ceil((L + 30) / 0.02), counts = new Uint32Array(nSlice + 1);
  o = 0;
  parts.forEach((x, pi) => { for (let k = 0; k < x.p.length / 3; k++) { partOf[o + k] = pi; kOf[o + k] = k; if (keep[o + k]) counts[slice(x.p[k * 3]) + 1]++; } o += x.p.length / 3; });
  for (let i = 1; i <= nSlice; i++) counts[i] += counts[i - 1];
  const n = counts[nSlice], order = new Uint32Array(n);
  for (let i = 0; i < total; i++) if (keep[i]) order[counts[slice(P[i * 3])]++] = i;
  T.mark("sort");
  const all = { n, x: new Float32Array(n), cls: new Uint8Array(n), now: new Float32Array(n * 3), later: new Float32Array(n * 3), rgb: new Float32Array(n * 3),
    dNow: new Float32Array(n), dLater: new Float32Array(n), shade: new Float32Array(n) };
  order.forEach((g, i) => {
    const x = parts[partOf[g]], k = kOf[g];
    all.cls[i] = x.cls; all.x[i] = x.p[k * 3]; all.shade[i] = 0.82 + rand() * 0.3;   // the return's intensity varies a little
    for (let j = 0; j < 3; j++) { all.now[i * 3 + j] = x.p[k * 3 + j]; all.later[i * 3 + j] = (x.q || x.p)[k * 3 + j]; all.rgb[i * 3 + j] = x.c[k * 3 + j]; }
    if (x.cls === VEG) {
      all.dNow[i] = wireDist(W, x.p[k * 3], x.p[k * 3 + 1], x.p[k * 3 + 2]);
      all.dLater[i] = cut ? all.dNow[i] : wireDist(W, x.q[k * 3], x.q[k * 3 + 1], x.q[k * 3 + 2]);
    }
  });
  T.mark('arrays_and_colour_distances');
  // the worst case the engineering overlays stand for: the nearest vegetation to the hot or wind-swung conductor (modelled, not surveyed)
  let worst = Infinity;
  for (let i = 0; i < n; i++) if (all.cls[i] === VEG && all.dNow[i] < target + 4) {
    const px = all.now[i * 3], py = all.now[i * 3 + 1], pz = all.now[i * 3 + 2];
    worst = Math.min(worst, wireDist(Whot, px, py, pz), wireDist(Wsway, px, py, pz));
  }
  T.mark("worst_case");
  return { all, W, Whot, Wsway, L, H0, H1, crit: { x: cx, z: cz }, hazard, worst, sampled: total, timings: T.out };
}

// the closest visible vegetation point to a conductor in a state, and the conductor point it is closest to (for the measurement line)
function nearest(S, later, cutAt) {
  const A = S.all, P = later ? A.later : A.now, D = later ? A.dLater : A.dNow;
  let best = Infinity, at = -1;
  for (let i = 0; i < A.n; i++) if (A.cls[i] === VEG && !(cutAt !== null && later && D[i] < cutAt) && D[i] < best) { best = D[i]; at = i; }
  if (at < 0) return null;
  const v = new THREE.Vector3(P[at * 3], P[at * 3 + 1], P[at * 3 + 2]);
  return { v, w: wirePoint(S.W, v), d: best };
}

// ---------------------------------------------------------------- drawing: round, softly lit points
const POINT_VS = `attribute vec3 color; attribute float shade; attribute float vis; uniform float size; uniform float scale; varying vec3 vColor; varying float vFog;
void main() { vColor = color * shade; vec4 mv = modelViewMatrix * vec4(position, 1.0); gl_Position = projectionMatrix * mv;
  gl_PointSize = vis < 0.5 ? 0.0 : max(1.5, size * scale / -mv.z); vFog = clamp((-mv.z - 40.0) / 220.0, 0.0, 0.65); }`;
const POINT_FS = `varying vec3 vColor; varying float vFog;
void main() { vec2 q = gl_PointCoord * 2.0 - 1.0; float r = dot(q, q); if (r > 1.0) discard;
  vec3 c = vColor * (1.05 - 0.35 * r); gl_FragColor = vec4(mix(c, vec3(0.03, 0.07, 0.09), vFog), 1.0); }`;

function overlay(S, req) {   // the engineering overlays, as one group per kind
  const g = { zone: new THREE.Group(), hot: new THREE.Group(), sway: new THREE.Group(), fall: new THREE.Group() };
  const curve = (pts) => new THREE.CatmullRomCurve3(Array.from({ length: Math.ceil(pts.length / 3 / 10) + 1 }, (_, i) => {
    const j = Math.min(pts.length / 3 - 1, i * 10); return new THREE.Vector3(pts[j * 3], pts[j * 3 + 1], pts[j * 3 + 2]);
  }));
  for (const w of S.W.list) g.zone.add(new THREE.Mesh(new THREE.TubeGeometry(curve(w.pts), 120, req, 18, false),
    new THREE.MeshBasicMaterial({ color: 0x4fd8ff, transparent: true, opacity: 0.035, depthWrite: false, side: THREE.DoubleSide })));
  const dashed = (W, col, grp) => { for (const w of W.list) {
    const ln = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve(w.pts).getPoints(200)), new THREE.LineDashedMaterial({ color: col, dashSize: 0.5, gapSize: 0.3 }));
    ln.computeLineDistances(); grp.add(ln);
  } };
  dashed(S.Whot, 0xff7a45, g.hot); dashed(S.Wsway, 0xffd23f, g.sway);
  if (S.hazard) {
    const ring = new THREE.Mesh(new THREE.RingGeometry(S.hazard.height - 0.15, S.hazard.height + 0.15, 96),
      new THREE.MeshBasicMaterial({ color: 0xff3b30, transparent: true, opacity: 0.6, side: THREE.DoubleSide, depthWrite: false }));
    ring.rotation.x = -Math.PI / 2; ring.position.set(S.hazard.x, 0.4, S.hazard.z); g.fall.add(ring);
  }
  return g;
}

// ---------------------------------------------------------------- the window
const fmtM = (v) => (v === null || v === undefined ? "-" : `${n2(v)} m`);
const dte = (d) => (d ? new Date(d).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric" }) : null);
// opts.cut = { before, after, attempt } when opened from a post-cut audit: the bay before and after the cut
export async function openLidar(spanId, opts = {}) {
  const cut = opts.cut || null;
  const st = { colour: "clearance", later: false, mix: 0, over: { zone: true, hot: false, sway: false, fall: true }, line: null, d: null, S: null };
  const stage = h("div", { class: "ld-stage" }), wait = h("div", { class: "ld-wait" }, "Running the survey over the span and building the point cloud.");
  // the measured gap: a readout in the view's top-left corner, and a small ring on the spot where the gap is measured
  const label = h("div", { class: "ld-label" }), pin = h("div", { class: "ld-pin" }), strip = h("div", { class: "ld-strip" }), title = h("h2", {}), sub = h("span", { class: "muted" });
  const side = h("div", { class: "ld-side" }), prevB = h("button", { class: "btn small", type: "button", onclick: () => step(-1) }, "Previous span");
  const nextB = h("button", { class: "btn small", type: "button", onclick: () => step(1) }, "Next span");
  const bg = modal(h("div", { class: "ld-head" }, title, sub,
      h("div", { class: "ld-views" }, prevB, nextB,
        h("button", { class: "btn small", type: "button", onclick: () => bg.remove() }, "Close"))),
    h("div", { class: "ld-body" }, h("div", { class: "ld-wrap" }, stage, wait, pin, label), side), strip);
  bg.querySelector(".modal").classList.add("ld-window");

  const renderer = new THREE.WebGLRenderer({ antialias: true }); renderer.setPixelRatio(Math.min(2, devicePixelRatio));
  renderer.setClearColor(0x081218); stage.append(renderer.domElement);
  const scene = new THREE.Scene(), cam = new THREE.PerspectiveCamera(42, 1, 0.1, 3000);
  const ctl = new OrbitControls(cam, renderer.domElement); ctl.enableDamping = true; ctl.maxPolarAngle = Math.PI * 0.49;   // never below the ground
  const mat = new THREE.ShaderMaterial({ uniforms: { size: { value: 0.085 }, scale: { value: 500 } }, vertexShader: POINT_VS, fragmentShader: POINT_FS });
  let pts = null, geo = null, pos = null, col = null, vis = null, groups = null;
  const mline = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
    new THREE.LineDashedMaterial({ color: 0xffffff, dashSize: 0.18, gapSize: 0.1, depthTest: false }));
  mline.renderOrder = 2; scene.add(mline);
  const resize = () => {
    const w = stage.clientWidth, hh = stage.clientHeight; if (!w || !hh) return;
    renderer.setSize(w, hh); cam.aspect = w / hh; cam.updateProjectionMatrix(); mat.uniforms.scale.value = hh / (2 * Math.tan(THREE.MathUtils.degToRad(cam.fov / 2)));
  };
  new ResizeObserver(resize).observe(stage);

  const seg = (opts2, isOn, on) => h("div", { class: "ld-seg" }, opts2.map(([k, l]) => h("button", { type: "button", class: isOn(k) ? "on" : "", onclick: (e) => {
    [...e.target.parentNode.children].forEach((b) => b.classList.toggle("on", b === e.target)); on(k);
  } }, l)));
  const kv = (rows) => h("dl", { class: "ld-kv" }, rows.filter(([, v]) => v !== null && v !== undefined && v !== "").flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  const dot = (c) => h("i", { style: `background:${typeof c === "string" ? c : `rgb(${c.map((x) => Math.round(x * 255)).join(",")})`}` });

  function paintSide() {
    const d = st.d, req = d.required_m, poleTxt = (p) => (p ? `${p.asset_id}, ${n1(p.height_m)} m ${String(p.material || "").toLowerCase()}` : "Not in the register");
    const legend = st.colour === "clearance"
      ? [[BAND.inside, `Vegetation inside the required ${n1(req)} m`], [BAND.near, "Vegetation within 1 m beyond the requirement"], [BAND.clear, "Vegetation clear of the line"],
        [CLASS_RGB[WIRE], "Conductor returns"], [CLASS_RGB[POLE], "Pole"], [CLASS_RGB[GROUND].map((x) => x * 0.55), "Ground"]]
      : [];
    const toggle = (k, l, c) => h("label", { class: "ld-check" }, h("input", { type: "checkbox", checked: st.over[k] ? "" : null, onchange: (e) => { st.over[k] = e.target.checked; showOverlays(); } }), dot(c), l);
    const S = st.S;
    side.replaceChildren(
      h("div", { class: "ld-h" }, "Colour by"), seg([["clearance", "Clearance"], ["rgb", "True colour"]], (k) => k === st.colour, (k) => { st.colour = k; paint(); paintSide(); }),
      h("div", { class: "ld-h" }, "Survey"),
      seg(cut ? [["now", "Before the cut"], ["later", "After the cut"]] : [["now", "Latest survey"], ["later", "In 12 months"]], (k) => (k === "later") === st.later, (k) => { st.later = k === "later"; morph(); }),
      h("div", { class: "ld-legend" }, legend.map(([c, l]) => h("div", {}, dot(c), l)),
        st.colour === "rgb" ? h("p", { class: "muted small" }, "Each point carries the colour of the surface it was sampled from, as a colourised survey does.") : null),
      h("div", { class: "ld-h" }, "Engineering overlays"),
      h("div", { class: "ld-legend" }, toggle("zone", `Clearance zone (${n1(req)} m round each conductor)`, "#4fd8ff"),
        toggle("hot", "Conductor at maximum operating temperature", "#ff7a45"), toggle("sway", "Conductor swung by wind (30 degrees)", "#ffd23f"),
        S && S.hazard ? toggle("fall", "Hazard tree fall-in reach", "#ff3b30") : null),
      h("div", { class: "ld-h" }, "Clearance"),
      kv([["Required", `${n1(req)} m (${d.basis_label || (d.bushfire_prone ? "bushfire-prone land" : "general overhead span")})`],
        ...(cut ? [["Before the cut", fmtM(cut.before ?? d.clearance_m)], ["After the cut", fmtM(cut.after)], ["Attempt", cut.attempt ? n0(cut.attempt) : null]]
          : [["Measured now", fmtM(d.clearance_m)], ["In 12 months", fmtM(d.clearance_12m_m)]]),
        ["Status", d.category_label], ["Worst case (modelled)", S && isFinite(S.worst) ? `${n2(S.worst)} m at maximum sag or in wind` : null]]),
      h("div", { class: "ld-h" }, "Span"),
      kv([["Feeder", d.feeder_id], ["Length", `${n1(d.length_m)} m`], ["Conductor", d.conductor], ["From", poleTxt(d.poles.find((p) => p.end_at === "start"))],
        ["To", poleTxt(d.poles.find((p) => p.end_at === "end"))], ["Pole lean", d.pole_lean_deg !== null ? `${n1(d.pole_lean_deg)} degrees` : null]]),
      h("div", { class: "ld-h" }, "Vegetation"),
      kv([["Dominant species", d.species], ["Growth", `${n2(d.growth_m_per_year)} m a year`], ["Hazard tree", d.hazard_tree ? "Yes, within falling distance" : "No"],
        ["Bushfire-prone land", d.bushfire_prone ? "Yes" : "No"], ["Surveyed", dte(d.capture_date)]]),
      S ? h("p", { class: "ld-note" }, `${n0(S.all.n)} returns, from ${n0(S.sampled)} surface samples, in a simulated two-line survey 70 m up.`) : null,
      h("p", { class: "ld-note" }, "This view is built from the span's survey results. The clearances, the pole heights and the span length are the measured values; the shapes of the poles and the vegetation are illustrative models, and the worst case is modelled from them."));
  }
  function paint() {
    const A = st.S.all, D = st.mix > 0.5 ? A.dLater : A.dNow, req = st.d.required_m;
    for (let i = 0; i < A.n; i++) {
      let c;
      if (st.colour === "rgb") c = [A.rgb[i * 3], A.rgb[i * 3 + 1], A.rgb[i * 3 + 2]];
      else if (A.cls[i] !== VEG) c = A.cls[i] === GROUND ? CLASS_RGB[GROUND].map((x) => x * 0.55) : CLASS_RGB[A.cls[i]];
      else c = D[i] < req ? BAND.inside : D[i] < req + 1 ? BAND.near : BAND.clear;
      col.setXYZ(i, c[0], c[1], c[2]);
    }
    col.needsUpdate = true;
  }
  function measure() {
    const d = st.d, req = d.required_m, later = st.mix > 0.5;
    const m = nearest(st.S, later, cut ? cut.after : null);
    if (!m) { label.hidden = true; pin.hidden = true; return; }
    mline.geometry.setFromPoints([m.v, m.w]); mline.computeLineDistances();
    const real = cut ? (later ? cut.after : (cut.before ?? d.clearance_m)) : later ? d.clearance_12m_m : d.clearance_m;
    bg.dataset.drawnGap = m.d.toFixed(2);   // the gap as drawn, which the tests compare with the surveyed clearance
    label.dataset.at = JSON.stringify([(m.v.x + m.w.x) / 2, (m.v.y + m.w.y) / 2, (m.v.z + m.w.z) / 2]);
    label.textContent = real <= 0 ? `Vegetation reaches the conductor${real < 0 ? ` and grows ${n2(-real)} m past it` : ""}. Required ${n1(req)} m.`
      : `${n2(real)} m to the nearest conductor${cut && later ? " after the cut" : ""}. Required ${n1(req)} m.`;
    label.className = `ld-label ${real < req ? "bad" : "ok"}`;
    pin.className = `ld-pin ${real < req ? "bad" : "ok"}`;
  }
  // the later state (12 months, or after the cut): the vegetation grows into its predicted position, or the cut corridor clears, over a second
  let anim = null;
  function morph() {
    const from = st.mix, to = st.later ? 1 : 0, t0 = performance.now(), A = st.S.all;
    cancelAnimationFrame(anim);
    const stepM = () => {
      const k = Math.min(1, (performance.now() - t0) / 900), m = from + (to - from) * (k * k * (3 - 2 * k)), a = pos.array;
      for (let i = 0; i < A.n * 3; i++) a[i] = A.now[i] + (A.later[i] - A.now[i]) * m;
      if (cut) for (let i = 0; i < A.n; i++) vis.array[i] = A.cls[i] === VEG && A.dNow[i] < cut.after && m > 0.5 ? 0 : 1;
      pos.needsUpdate = true; vis.needsUpdate = true; st.mix = m;
      if (k < 1) anim = requestAnimationFrame(stepM); else { paint(); measure(); }
    };
    stepM();
  }
  function pass() {   // as a span opens, the cloud fills in from one pole to the other, as the aircraft flies the line
    const t0 = performance.now(), S = st.S, A = S.all;
    const go = () => {
      if (st.S !== S) return;   // another span has been opened: its own pass takes over
      const k = Math.min(1, (performance.now() - t0) / 2600), xs = -14 + (S.L + 28) * k;
      let lo = 0, hi = A.n; while (lo < hi) { const mid = (lo + hi) >> 1; if (A.x[mid] <= xs) lo = mid + 1; else hi = mid; }
      geo.setDrawRange(0, lo);
      if (k < 1 && bg.isConnected) requestAnimationFrame(go); else geo.setDrawRange(0, Infinity);
    };
    go();
  }
  function sideView() {   // the view a span opens on: from the side, on the nearest tree and most of the span
    const S = st.S, L = S.L, mid = new THREE.Vector3((S.crit.x + L / 2) / 2, Math.max(S.H0, S.H1) * 0.6, S.crit.z / 2);
    ctl.target.copy(mid);
    cam.position.set(mid.x, mid.y + 5, S.crit.z + Math.max(28, L * 0.62));
    ctl.update();
  }
  function showOverlays() { if (groups) for (const [k, g] of Object.entries(groups)) g.visible = !!st.over[k]; }
  function paintStrip() {
    const line = st.line || [];
    const at = line.findIndex((r) => r.bay_id === st.d.span_id);
    prevB.disabled = at <= 0; nextB.disabled = at < 0 || at >= line.length - 1;
    strip.replaceChildren(line.length ? h("div", { class: "ld-strip-h" }, `${st.d.feeder_id}: span ${n0(at + 1)} of ${n0(line.length)}, in line order. Select a span to move to it.`) : null,
      h("div", { class: "ld-cells" }, line.map((r, i) => h("button", { type: "button", class: `ld-cell${i === at ? " on" : ""}`, style: `background:${TIER_HEX[r.risk_tier] || "#9aa8b6"}`,
        title: `${r.bay_label}: ${r.risk_tier_label || ""}, ${n2(r.clearance_m)} m clearance`, onclick: () => load(r.bay_id) }))));
  }
  async function step(k) {
    const line = st.line || [], at = line.findIndex((r) => r.bay_id === st.d.span_id), next = line[at + k];
    if (next) load(next.bay_id);
  }

  // load (or move to) a span: its measured values, the survey and the cloud
  async function load(id) {
    wait.hidden = false; label.hidden = true; pin.hidden = true;
    let d;
    try { d = await api(`/api/vegetation/lidar/${encodeURIComponent(id)}`); } catch (e) {
      wait.textContent = e.status === 404 ? `${id} has no LiDAR survey result.` : "The LiDAR view could not be loaded. Please try again."; return;
    }
    st.d = d; st.S = null; st.mix = 0; st.later = false;
    title.textContent = `LiDAR view of ${d.span_id}`;
    sub.textContent = `${n1(d.length_m)} m span on ${d.feeder_id}${cut ? `, before and after the cut` : ""}. Drag to turn, scroll to zoom.`;
    paintSide();
    if (!st.line || st.line[0]?.feeder_id !== d.feeder_id) {   // the feeder's spans for the strip load beside the survey, not before it
      st.line = [];
      api(`/api/vegetation/feeder/${encodeURIComponent(d.feeder_id)}/spans`).then((r) => { if (st.d === d) { st.line = r.spans; paintStrip(); } }).catch(() => {});
    }
    paintStrip();
    await new Promise((r) => setTimeout(r, 30));   // let the waiting note paint before the survey runs
    const S = await build(d, cut);
    if (!bg.isConnected || st.d !== d) return;
    st.S = S; bg.dataset.timings = JSON.stringify(S.timings);   // how long each stage of the build took (ms), read by the tests
    if (pts) { scene.remove(pts); geo.dispose(); }
    if (groups) Object.values(groups).forEach((g) => scene.remove(g));
    geo = new THREE.BufferGeometry();
    pos = new THREE.BufferAttribute(new Float32Array(S.all.now), 3); col = new THREE.BufferAttribute(new Float32Array(S.all.n * 3), 3);
    vis = new THREE.BufferAttribute(new Float32Array(S.all.n).fill(1), 1);
    geo.setAttribute("position", pos); geo.setAttribute("color", col); geo.setAttribute("shade", new THREE.BufferAttribute(S.all.shade, 1)); geo.setAttribute("vis", vis);
    pts = new THREE.Points(geo, mat); pts.frustumCulled = false; scene.add(pts);
    groups = overlay(S, d.required_m); Object.values(groups).forEach((g) => scene.add(g)); showOverlays();
    wait.hidden = true; label.hidden = false; pin.hidden = false;
    resize(); paint(); measure(); paintSide(); sideView(); pass();
  }

  const v3 = new THREE.Vector3();
  const loop = () => {
    if (!bg.isConnected) { renderer.dispose(); geo?.dispose(); return; }
    ctl.update(); renderer.render(scene, cam);
    const at = label.dataset.at && JSON.parse(label.dataset.at);
    if (at && st.S) { v3.set(...at).project(cam); pin.style.left = `${(v3.x * 0.5 + 0.5) * stage.clientWidth}px`; pin.style.top = `${(-v3.y * 0.5 + 0.5) * stage.clientHeight}px`; pin.hidden = v3.z > 1; }
    requestAnimationFrame(loop);
  };
  loop();
  load(spanId).catch((e) => toast(e.message, "red"));
}
