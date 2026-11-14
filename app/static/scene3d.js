// Graphic-novel 3D site view, built to scale from the twin. Every model is a hand-painted Meshy GLB sized to its real-world
// dimensions; like a comic panel the scenery renders in greyscale and the network assets in full colour, under a hard sun with
// crisp shadows and an ink outline pass. Overhead lines are strung for real: each pole's crossarm turns across the line, and
// the conductors hang with sag from the insulators found on each pole model. A lineworker walks (around things, not through
// them) to whatever you click and says what he makes of it.
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { MeshoptDecoder } from "three/addons/libs/meshopt_decoder.module.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { ShaderPass } from "three/addons/postprocessing/ShaderPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import * as SkeletonUtils from "three/addons/utils/SkeletonUtils.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";
import { Line2 } from "three/addons/lines/Line2.js";
import { LineGeometry } from "three/addons/lines/LineGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import { api, post, h, n0, n1, loading } from "./ui.js";
import { openAsset } from "./screens.js";

const COL = { red: 0xe0533a, amber: 0xe0a53a, green: 0x3fb27f };
const HEX = (c) => "#" + c.toString(16).padStart(6, "0");
const SKY = 0xb7b3ab;

// model -> [real-world size in metres, which dimension that is: "h" height, "len" longest side of the footprint]
const SIZE = {
  wood_pole: [12, "h"], concrete_pole: [12, "h"], pole_transformer: [12, "h"], pole_transformer_concrete: [12, "h"], recloser: [12, "h"],
  recloser_concrete: [12, "h"], gas_switch: [12, "h"], swer_isolator: [11.5, "h"], cable_riser_pole: [12, "h"], defect_pole: [12, "h"], broken_pole: [9.5, "h"],
  power_transformer: [8.5, "len"], cb_bay: [9.5, "h"], capacitor_bank: [7, "len"], switchroom: [17, "len"], control_kiosk: [2.4, "h"], zone_sub_gate: [6.4, "len"],
  substation_fence: [2.6, "h"], house_brick: [17, "len"], house_weatherboard: [15, "len"], solar_house: [17, "len"], solar_house_2: [18, "len"],
  water_tank_house: [20, "len"], apartment_block: [17, "h"], shop_strip: [36, "len"], school: [36, "len"], farm_shed: [16, "len"],
  street_tree: [8, "h"], gum_tree_tall: [19, "h"], hazard_tree: [16, "h"], colorbond_fence: [1.8, "h"], street_light: [8.5, "h"],
  wheelie_bins: [1.4, "len"], parked_car: [4.3, "len"], network_ute: [5.4, "len"], ewp_truck: [8.6, "len"], ewp_truck_2: [8.6, "len"], crane_truck: [9.5, "len"],
  traffic_controller: [1.8, "h"], traffic_cones: [2.4, "len"], patrol_drone: [1.2, "len"], vegetation_worker: [1.8, "h"], lineworker_1: [1.8, "h"],
  fire_danger_sign: [3.8, "h"], kangaroo: [1.7, "h"], wombat: [1.6, "len"], cow: [2.4, "len"], cockatoo: [0.6, "h"],
  kiosk: [3.4, "len"], rmu: [1.7, "h"], lv_pillar: [1.1, "h"], dc_charging_hub: [12, "len"], kerbside_charger: [2.6, "h"],
};
// One scale per yard: the crew, their ute, the poles and the plant share it (S.crew), and the buildings are sized so their doors fit the
// crew. The switchroom model's door stands about 4.1 m at the switchroom's catalogue size, and a door is about 1.15 times a person.
const SWITCHROOM_DOOR_M = 4.1, PERSON_M = 1.8;
const buildingScale = (crew) => crew * PERSON_M * 1.15 / SWITCHROOM_DOOR_M;
const topOf = (o) => (o ? new THREE.Box3().setFromObject(o).max.y : 6);   // the top of a placed model, for its tag
const POLES = new Set(["wood_pole", "concrete_pole", "pole_transformer", "pole_transformer_concrete", "recloser", "recloser_concrete", "gas_switch",
  "swer_isolator", "cable_riser_pole", "defect_pole", "broken_pole"]);
// the rest of the network, and the field crews and their vehicles: always in colour, so they stand out against the grey scenery
const NETWORK = new Set([...POLES, "power_transformer", "cb_bay", "capacitor_bank", "switchroom", "control_kiosk", "kiosk", "rmu", "lv_pillar",
  "dc_charging_hub", "kerbside_charger", "street_light", "fire_danger_sign", "network_ute", "ewp_truck", "ewp_truck_2", "crane_truck", "patrol_drone",
  "traffic_controller", "traffic_cones", "vegetation_worker", "lineworker_1"]);
const STEEL = new Set(["street_light"]);
const STEEL_HEX = 0x8d989f;   // galvanised steel: natural, kept a little darker and cooler than the warm grey haze so the column reads
// where the luminaire is: the top point furthest out along the arm, in the model's own frame (found once per model)
function lampAt(t) {
  if (t.userData.lampAt !== undefined) return t.userData.lampAt;
  t.updateMatrixWorld(true);
  const inv = t.matrixWorld.clone().invert(), v = new THREE.Vector3(), pts = [];
  let top = -Infinity;
  t.traverse((m) => {
    if (!m.isMesh) return;
    const p = m.geometry.attributes.position, step = Math.max(1, Math.floor(p.count / 3000));
    for (let i = 0; i < p.count; i += step) { v.fromBufferAttribute(p, i).applyMatrix4(m.matrixWorld).applyMatrix4(inv); pts.push(v.clone()); top = Math.max(top, v.y); }
  });
  const hi = pts.filter((q) => q.y > top - (top - Math.min(...pts.map((q) => q.y))) * 0.12);
  const far = hi.reduce((a, q) => (Math.hypot(q.x, q.z) > Math.hypot(a.x, a.z) ? q : a), hi[0]);
  t.userData.lampAt = far ? new THREE.Vector3(far.x, far.y, far.z) : null;
  return t.userData.lampAt;
}
const LAMP = { lens: null };
function lampGlow(o, at) {   // the LED lens under the luminaire, lit warm and unshaded so the light reads at any distance
  if (!LAMP.lens) { LAMP.lens = new THREE.MeshBasicMaterial({ color: 0xffe9b8, fog: true }); LAMP.lens.userData.shared = true; }   // shared: survives scene teardown
  const k = 1 / (o.scale.x || 1);   // sizes in metres, whatever the model's own scale
  const lens = new THREE.Mesh(new THREE.SphereGeometry(0.34 * k, 16, 8), LAMP.lens);
  lens.scale.y = 0.32; lens.position.set(at.x, at.y - 0.1 * k, at.z); lens.userData.own = true; o.add(lens);
}
const NO_INK = new Set(["gum_tree_tall", "street_tree", "substation_fence", "colorbond_fence", "zone_sub_gate"]);
const ARM = 3.4;   // the stylised crossarms are narrowed to this (a real 11 kV crossarm is 2.4 to 3 m), the shaft with them

// ---------------------------------------------------------------- models
const loader = new GLTFLoader(); loader.setMeshoptDecoder(MeshoptDecoder);
const raw = (url) => new Promise((ok, no) => loader.load(url, ok, undefined, no));
const TPL = new Map();
function model(name) {
  if (!TPL.has(name)) TPL.set(name, raw(`/static/models_web/${name}.glb`).then((g) => prepare(name, g.scene)).catch(() => null));
  return TPL.get(name);
}
function verts(o, step = 2) {   // every vertex of o in its root's space
  o.updateMatrixWorld(true);
  const out = [], v = new THREE.Vector3();
  o.traverse((m) => { if (!m.isMesh) return; const p = m.geometry.attributes.position; for (let i = 0; i < p.count; i += step) out.push(v.fromBufferAttribute(p, i).applyMatrix4(m.matrixWorld).clone()); });
  return out;
}
const median = (a) => { const s = a.slice().sort((x, y) => x - y); return s.length ? s[Math.floor(s.length / 2)] : 0; };

// Scale a model to its real size, centre it on its footprint standing on the ground, and learn what the scene needs from its
// geometry: the footprint, the height, where a pole's conductors attach, a transformer's bushings, a tree's lean.
function prepare(name, o) {
  const [size, dim] = SIZE[name] || [4, "h"];
  const b = new THREE.Box3().setFromObject(o), z = b.getSize(new THREE.Vector3());
  o.scale.multiplyScalar(size / Math.max(1e-6, dim === "h" ? z.y : Math.max(z.x, z.z)));
  const b2 = new THREE.Box3().setFromObject(o), c = b2.getCenter(new THREE.Vector3());
  o.position.set(-c.x, -b2.min.y, -c.z);
  const sq = new THREE.Group(), t = new THREE.Group(); sq.add(o); t.add(sq);
  const info = { name };
  if (POLES.has(name)) Object.assign(info, pole(t, sq, name));
  const fb = new THREE.Box3().setFromObject(t), fz = fb.getSize(new THREE.Vector3());
  info.foot = [fz.x, fz.z]; info.h = fz.y;
  if (name === "power_transformer" || name === "cb_bay") info.bushings = bushings(t, fz.y, name === "cb_bay");
  if (name === "hazard_tree" || name === "gum_tree_tall") info.lean = lean(t, fz.y);
  t.userData.info = info;
  return t;
}
// Pole models: find the crossarms (slices much wider than the shaft), narrow the model so the top arm is ARM long, then find
// the insulator tips as peaks in the top profile of that arm: the outer two and the one nearest the pole are the three phases.
function pole(t, sq, name) {
  const P = verts(t), H = Math.max(...P.map((p) => p.y)), N = 60;
  const sl = Array.from({ length: N }, () => ({ x0: 1e9, x1: -1e9, n: 0 }));
  for (const p of P) { const s = sl[Math.min(N - 1, Math.max(0, Math.floor((p.y / H) * N)))]; s.n++; s.x0 = Math.min(s.x0, p.x); s.x1 = Math.max(s.x1, p.x); }
  const wid = (s) => (s.n ? s.x1 - s.x0 : 0);
  const shaft = median(sl.slice(Math.floor(N * 0.15), Math.floor(N * 0.45)).map(wid).filter(Boolean)) || H * 0.06;
  const arms = []; let cur = null;
  for (let k = N - 1; k >= N * 0.45; k--) {
    if (wid(sl[k]) > shaft * 2.6) { if (!cur) arms.push((cur = { k1: k, k0: k, w: 0, x0: 1e9, x1: -1e9 })); cur.k0 = k; cur.w = Math.max(cur.w, wid(sl[k])); cur.x0 = Math.min(cur.x0, sl[k].x0); cur.x1 = Math.max(cur.x1, sl[k].x1); }
    else cur = null;
  }
  const top = arms[0], W = top ? top.w : shaft * 4;
  const low = arms.find((a) => a !== top && a.w > W * 0.5);
  if (low && (name === "wood_pole" || name === "concrete_pole")) cutArm(t, P, low, N, H, shaft);
  const k = Math.min(1, Math.max(0.38, ARM / W));
  sq.scale.set(k, 1, k);
  let phases = [];
  if (top) {
    const y0 = (top.k0 / N) * H - H * 0.01, B = 28, bins = Array.from({ length: B }, () => ({ y: -1e9, z: 0 }));
    for (const p of P) {
      if (p.y < y0) continue;
      const i = Math.min(B - 1, Math.max(0, Math.floor(((p.x - top.x0) / Math.max(1e-6, top.x1 - top.x0)) * B)));
      if (p.y > bins[i].y) { bins[i].y = p.y; bins[i].z = p.z; }
    }
    const base = median(bins.map((b) => b.y).filter((y) => y > -1e9));
    const peaks = []; let run = null;
    bins.forEach((b, i) => {
      if (b.y > base + H * 0.012) { if (!run) peaks.push((run = { i0: i, i1: i, y: b.y, z: b.z })); run.i1 = i; if (b.y > run.y) { run.y = b.y; run.z = b.z; } }
      else run = null;
    });
    const px = (r) => top.x0 + (((r.i0 + r.i1 + 1) / 2) / B) * (top.x1 - top.x0);
    const pts = peaks.map((r) => new THREE.Vector3(px(r), r.y, r.z));
    if (pts.length >= 2) {
      pts.sort((a, b) => a.x - b.x);
      const mid = pts.slice(1, -1).sort((a, b) => Math.abs(a.x) - Math.abs(b.x))[0] || new THREE.Vector3(0, base + H * 0.03, 0);
      phases = [pts[0], mid, pts[pts.length - 1]];
    } else phases = [-0.42, 0, 0.42].map((f) => new THREE.Vector3(f * W, base + H * 0.02, 0));
  } else phases = [-0.4, 0, 0.4].map((f) => new THREE.Vector3(f * W, H * 0.97, 0));
  phases.forEach((p) => { p.x *= k; p.z *= k; });
  const lvArm = arms.find((a) => a !== top && a.k1 < (top ? top.k0 - 3 : N));
  const lv = new THREE.Vector3(shaft * k * 0.6 + 0.08, lvArm ? ((lvArm.k1 + 1) / N) * H : H * 0.62, 0);
  return { phases, lv, top: H };
}
// A plain pole keeps only its top crossarm: the lower arm, its braces and insulators are cut from the mesh (everything in that band
// standing off the shaft). The LV still attaches at the old arm height, on the shaft.
function cutArm(t, P, a, N, H, shaft) {
  const lo = ((a.k0 - 4) / N) * H, hi = ((a.k1 + 4) / N) * H, r = shaft * 0.62;
  const mid = P.filter((p) => p.y > H * 0.15 && p.y < H * 0.45), cx = median(mid.map((p) => p.x)), cz = median(mid.map((p) => p.z));
  const v = [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()];
  t.traverse((m) => {
    if (!m.isMesh) return;
    const g = m.geometry, pos = g.attributes.position, idx = g.index ? g.index.array : Array.from({ length: pos.count }, (_, i) => i), keep = [];
    for (let i = 0; i < idx.length; i += 3) {
      for (let j = 0; j < 3; j++) v[j].fromBufferAttribute(pos, idx[i + j]).applyMatrix4(m.matrixWorld);
      const y = (v[0].y + v[1].y + v[2].y) / 3, x = (v[0].x + v[1].x + v[2].x) / 3, z = (v[0].z + v[1].z + v[2].z) / 3;
      if (y > lo && y < hi && Math.hypot(x - cx, z - cz) > r) continue;
      keep.push(idx[i], idx[i + 1], idx[i + 2]);
    }
    g.setIndex(keep);
  });
}
function bushings(t, H, gantry) {   // the three HV connection points on a transformer's bushings or a bay's gantry
  const P = verts(t).filter((p) => p.y > H * 0.62);
  const x0 = Math.min(...P.map((p) => p.x)), x1 = Math.max(...P.map((p) => p.x));
  if (gantry) return [-1, 0, 1].map((f) => new THREE.Vector3(f * (x1 - x0) * 0.3, H * 0.93, 0));
  const B = 30, bins = Array.from({ length: B }, () => ({ y: -1e9, z: 0, x: 0 }));
  for (const p of P) { const i = Math.min(B - 1, Math.floor(((p.x - x0) / (x1 - x0)) * B)); if (p.y > bins[i].y) Object.assign(bins[i], { y: p.y, z: p.z, x: p.x }); }
  const peaks = bins.filter((b, i) => b.y > -1e9 && b.y >= (bins[i - 1]?.y ?? -1e9) && b.y >= (bins[i + 1]?.y ?? -1e9)).sort((a, b) => b.y - a.y).slice(0, 3);
  return peaks.sort((a, b) => a.x - b.x).map((b) => new THREE.Vector3(b.x, b.y, b.z));
}
function lean(t, H) {   // which way (and how far) a tree's crown sits off its base
  const P = verts(t, 3), top = P.filter((p) => p.y > H * 0.7), foot = P.filter((p) => p.y < H * 0.08);
  const c = (a) => a.reduce((s, p) => s.add(p), new THREE.Vector3()).multiplyScalar(1 / Math.max(1, a.length));
  const d = c(top).sub(c(foot)); d.y = 0; return d;
}

// One ink material per source material: the painted texture, lit softly. Scenery renders greyscale, assets in colour (saturation
// lifted so a timber pole or a green kiosk stands out of the grey). Past an ellipse round the site everything fades to the fog colour,
// so a suburb or a line leaving the site has no hard end (EDGE: its centre and radii, set per site).
const INK = [new Map(), new Map()], TINT = new Map();
const EDGE = { c: { value: new THREE.Vector2() }, r: { value: new THREE.Vector2(1e5, 1e5) } }, FADE = [0.72, 1.0];
function makeInk(src, sat, colour) {
  const m = new THREE.MeshStandardMaterial({ map: src.map || null, color: colour != null ? new THREE.Color(colour) : src.map ? 0xffffff : (src.color || new THREE.Color(0xbbbbbb)),
    roughness: 1, metalness: 0, side: THREE.DoubleSide });
  m.customProgramCacheKey = () => (sat ? "inkC" : "inkG");
  m.onBeforeCompile = (sh) => {
    sh.uniforms.edgeC = EDGE.c; sh.uniforms.edgeR = EDGE.r;
    sh.vertexShader = "varying vec2 vEdge;\n" + sh.vertexShader.replace("#include <project_vertex>", "#include <project_vertex>\nvEdge = (modelMatrix * vec4(transformed, 1.0)).xz;");
    sh.fragmentShader = "uniform vec2 edgeC, edgeR; varying vec2 vEdge;\n" + sh.fragmentShader.replace("#include <dithering_fragment>", `#include <dithering_fragment>
      vec3 c = gl_FragColor.rgb; float l = dot(c, vec3(0.299, 0.587, 0.114));
      ${sat ? "c = mix(vec3(l), c, 1.9); gl_FragColor.rgb = c * 1.3 + diffuseColor.rgb * 0.42;" : "gl_FragColor.rgb = vec3(0.10 + 0.88 * smoothstep(0.0, 0.7, l));"}
      gl_FragColor.rgb = mix(gl_FragColor.rgb, fogColor, smoothstep(${FADE[0].toFixed(2)}, ${FADE[1].toFixed(2)}, length((vEdge - edgeC) / edgeR)));`);
  };
  return m;
}
function ink(src, sat) { const c = INK[sat ? 1 : 0]; if (!c.has(src)) c.set(src, makeInk(src, sat)); return c.get(src); }
function tinted(src, hex) { const k = src.uuid + hex; if (!TINT.has(k)) TINT.set(k, makeInk(src, 1, hex)); return TINT.get(k); }

// ---------------------------------------------------------------- painted ground
function canvasTex(size, paint, rep) {
  const cv = document.createElement("canvas"); cv.width = cv.height = size; paint(cv.getContext("2d"), size);
  const t = new THREE.CanvasTexture(cv); t.wrapS = t.wrapT = THREE.RepeatWrapping; t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8; t.repeat.set(rep, rep); return t;
}
const rnd = Math.random;
const TEX = {
  grass: () => canvasTex(1024, (g, s) => {
    g.fillStyle = "#7c786f"; g.fillRect(0, 0, s, s);
    for (let i = 0; i < 900; i++) { const v = 100 + rnd() * 40 | 0; g.fillStyle = `rgba(${v},${v - 4},${v - 12},0.12)`; g.beginPath(); g.arc(rnd() * s, rnd() * s, 30 + rnd() * 90, 0, 7); g.fill(); }
    g.lineWidth = 1.3;
    for (let i = 0; i < 5200; i++) { const x = rnd() * s, y = rnd() * s, l = 4 + rnd() * 7, d = rnd() < 0.5; g.strokeStyle = d ? "rgba(40,36,30,0.30)" : "rgba(200,196,186,0.30)";
      g.beginPath(); g.moveTo(x, y); g.lineTo(x + (rnd() - 0.5) * 3, y - l); g.stroke(); }
  }, 60),
  paddock: () => canvasTex(1024, (g, s) => {
    g.fillStyle = "#6f6a61"; g.fillRect(0, 0, s, s);
    for (let i = 0; i < 700; i++) { const v = 96 + rnd() * 50 | 0; g.fillStyle = `rgba(${v},${v - 5},${v - 14},0.14)`; g.beginPath(); g.ellipse(rnd() * s, rnd() * s, 50 + rnd() * 120, 20 + rnd() * 50, rnd() * 3, 0, 7); g.fill(); }
    g.lineWidth = 1.2;
    for (let i = 0; i < 4200; i++) { const x = rnd() * s, y = rnd() * s, l = 7 + rnd() * 12; g.strokeStyle = rnd() < 0.6 ? "rgba(40,36,28,0.32)" : "rgba(196,190,174,0.26)";
      g.beginPath(); g.moveTo(x, y); g.quadraticCurveTo(x + 2, y - l / 2, x + (rnd() - 0.5) * 6, y - l); g.stroke(); }
  }, 70),
  gravel: () => canvasTex(1024, (g, s) => {
    g.fillStyle = "#6f6a63"; g.fillRect(0, 0, s, s);
    for (let i = 0; i < 1600; i++) { const v = 118 + rnd() * 22 | 0; g.fillStyle = `rgba(${v},${v - 5},${v - 10},0.10)`; g.beginPath(); g.arc(rnd() * s, rnd() * s, 30 + rnd() * 90, 0, 7); g.fill(); }
    for (let i = 0; i < 900; i++) { const x = rnd() * s, y = rnd() * s, r = 1.5 + rnd() * 3, v = 128 + rnd() * 22 | 0;
      g.fillStyle = `rgb(${v},${v - 5},${v - 11})`; g.beginPath(); g.ellipse(x, y, r * 1.4, r, rnd() * 3, 0, 7); g.fill(); g.strokeStyle = "rgba(30,26,22,0.35)"; g.lineWidth = 1; g.stroke(); }
  }, 16),
  asphalt: () => canvasTex(512, (g, s) => {
    g.fillStyle = "#57544f"; g.fillRect(0, 0, s, s);
    for (let i = 0; i < 4000; i++) { const v = 60 + rnd() * 70 | 0; g.fillStyle = `rgba(${v},${v},${v - 3},0.5)`; g.fillRect(rnd() * s, rnd() * s, 1.5, 1.5); }
    g.strokeStyle = "rgba(20,18,16,0.35)"; g.lineWidth = 1.2;
    for (let i = 0; i < 14; i++) { let x = rnd() * s, y = rnd() * s; g.beginPath(); g.moveTo(x, y); for (let k = 0; k < 6; k++) { x += (rnd() - 0.5) * 26; y += (rnd() - 0.5) * 26; g.lineTo(x, y); } g.stroke(); }
  }, 1),
  concrete: () => canvasTex(256, (g, s) => {
    g.fillStyle = "#bdb9b1"; g.fillRect(0, 0, s, s);
    for (let i = 0; i < 1400; i++) { const v = 150 + rnd() * 60 | 0; g.fillStyle = `rgba(${v},${v},${v - 4},0.35)`; g.fillRect(rnd() * s, rnd() * s, 2, 2); }
    g.strokeStyle = "rgba(40,38,34,0.45)"; g.lineWidth = 2; g.strokeRect(1, 1, s - 2, s - 2);
  }, 1),
  dirt: () => canvasTex(512, (g, s) => {
    g.fillStyle = "#9c9183"; g.fillRect(0, 0, s, s);
    for (let i = 0; i < 2600; i++) { const v = 110 + rnd() * 70 | 0; g.fillStyle = `rgba(${v},${v - 6},${v - 14},0.4)`; g.beginPath(); g.arc(rnd() * s, rnd() * s, 0.8 + rnd() * 2, 0, 7); g.fill(); }
    g.strokeStyle = "rgba(50,44,36,0.25)"; g.lineWidth = 9; for (const y of [0.3, 0.7]) { g.beginPath(); g.moveTo(0, y * s); g.lineTo(s, y * s); g.stroke(); }
  }, 1),
};

// Ink lines from depth and surface-direction breaks (second difference of depth, so flat or evenly sloping ground never inks),
// drawn over the lit scene before tone mapping. Silhouettes everywhere; interior creases only close to the camera.
const OutlineShader = {
  uniforms: { tDiffuse: { value: null }, tNormal: { value: null }, tDepth: { value: null }, res: { value: new THREE.Vector2(1, 1) }, near: { value: 0.5 }, far: { value: 3000 }, view: { value: 60 },
    projInv: { value: new THREE.Matrix4() }, camWorld: { value: new THREE.Matrix4() }, edgeC: EDGE.c, edgeR: EDGE.r },
  vertexShader: "varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
  fragmentShader: `uniform sampler2D tDiffuse, tNormal, tDepth; uniform vec2 res; uniform float near, far, view; uniform mat4 projInv, camWorld; uniform vec2 edgeC, edgeR; varying vec2 vUv;
    float lin(float d){ float z = d * 2.0 - 1.0; return (2.0 * near * far) / (far + near - z * (far - near)); }
    vec3 N(vec2 uv){ return texture2D(tNormal, uv).xyz * 2.0 - 1.0; }
    void main(){
      vec4 col = texture2D(tDiffuse, vUv); vec2 t = 1.5 / res;
      vec3 n0 = N(vUv); float d0 = lin(texture2D(tDepth, vUv).x);
      vec2 o[4]; o[0] = vec2(t.x, 0.0); o[1] = vec2(-t.x, 0.0); o[2] = vec2(0.0, t.y); o[3] = vec2(0.0, -t.y);
      float ne = 0.0, dd[4];
      for (int i = 0; i < 4; i++) { ne += 1.0 - dot(n0, N(vUv + o[i])); dd[i] = lin(texture2D(tDepth, vUv + o[i]).x); }
      float de = (abs(dd[0] + dd[1] - 2.0 * d0) + abs(dd[2] + dd[3] - 2.0 * d0)) / d0;
      float fade = 1.0 - smoothstep(view * 1.8, view * 3.4, d0);
      float crease = smoothstep(0.55, 0.95, ne) * (1.0 - smoothstep(view * 0.7, view * 1.15, d0));
      vec4 vp = projInv * vec4(vUv * 2.0 - 1.0, texture2D(tDepth, vUv).x * 2.0 - 1.0, 1.0);
      vec2 wp = (camWorld * vec4(vp.xyz / vp.w, 1.0)).xz;   // ink fades out ahead of the colour at the site's edge
      float e = max(crease, smoothstep(0.03, 0.08, de)) * fade * (1.0 - smoothstep(0.6, 0.9, length((wp - edgeC) / edgeR)));
      gl_FragColor = vec4(mix(col.rgb, vec3(0.03, 0.025, 0.02), e), col.a);
    }`,
};

// ---------------------------------------------------------------- the one renderer, scene and camera (reused across sites)
let R = null;
function engine() {
  if (R) return R;
  const r = new THREE.WebGLRenderer({ antialias: true });
  r.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  r.shadowMap.enabled = true; r.shadowMap.type = THREE.PCFSoftShadowMap;
  r.toneMapping = THREE.ACESFilmicToneMapping; r.toneMappingExposure = 1.0; r.outputColorSpace = THREE.SRGBColorSpace;
  const scene = new THREE.Scene(); scene.background = new THREE.Color(SKY); scene.fog = new THREE.Fog(SKY, 150, 560);
  const camera = new THREE.PerspectiveCamera(40, 1, 0.5, 3000);
  scene.add(new THREE.HemisphereLight(0xfff3df, 0x5a5048, 0.95));
  const sun = new THREE.DirectionalLight(0xfff1d6, 3.0); sun.position.set(-60, 90, 48); sun.castShadow = true; sun.shadow.mapSize.set(4096, 4096);
  sun.shadow.bias = -0.0004; sun.shadow.normalBias = 0.02; scene.add(sun); scene.add(sun.target);
  const world = new THREE.Group(); scene.add(world);
  const normalMat = new THREE.MeshNormalMaterial({ side: THREE.DoubleSide });
  const nrmT = new THREE.WebGLRenderTarget(1, 1, { type: THREE.HalfFloatType }); nrmT.depthTexture = new THREE.DepthTexture(1, 1);
  const composer = new EffectComposer(r);
  composer.addPass(new RenderPass(scene, camera));
  const outline = new ShaderPass(OutlineShader);
  outline.uniforms.tNormal.value = nrmT.texture; outline.uniforms.tDepth.value = nrmT.depthTexture;
  outline.uniforms.near.value = camera.near; outline.uniforms.far.value = camera.far;
  composer.addPass(outline); composer.addPass(new OutputPass());
  const controls = new OrbitControls(camera, r.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.08; controls.screenSpacePanning = false; controls.minPolarAngle = 0.22; controls.maxPolarAngle = 1.36;
  controls.minDistance = 7; controls.zoomSpeed = 0.9;
  R = { r, scene, camera, sun, world, normalMat, nrmT, composer, outline, controls, S: null, raf: 0, clock: new THREE.Clock() };
  R.route = (from, box) => route(R.S, from, box);   // the lineworker's router, for headless path checks
  window.__s3 = R;   // test hook for headless checks (camera moves, scene counts, walk routes)
  return R;
}
function clearWorld() {
  const w = R.world;
  w.traverse((o) => { if (o.userData.own) { o.geometry?.dispose(); if (o.material && !o.material.userData?.shared) { o.material.map?.dispose(); o.material.dispose(); } } });
  w.clear();
}

// ---------------------------------------------------------------- scene-building kit
function seeded(str) {
  let a = 0; for (const ch of String(str)) a = (Math.imul(a ^ ch.charCodeAt(0), 2654435761) >>> 0);
  return () => { a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
// Conductors are ink lines of constant screen width (crisp at any zoom), each with a thin invisible tube that casts its shadow.
const LINES = new Map(), WIRE = new THREE.Color(0x1b1a18), SHADOW_ONLY = new THREE.MeshBasicMaterial({ colorWrite: false, depthWrite: false });
SHADOW_ONLY.userData.shared = true;
function lineMat(px) {
  if (!LINES.has(px)) { const m = new LineMaterial({ color: 0xffffff, linewidth: px, vertexColors: true }); m.fog = true; m.userData.shared = true; if (R) R.r.getSize(m.resolution); LINES.set(px, m); }   // fog: distant conductors melt into the haze with the poles
  return LINES.get(px);
}

class Site {
  constructor(d, T, wrap) {
    this.d = d; this.T = T; this.wrap = wrap; this.r = seeded(d.seed || d.title);
    this.tags = []; this.picks = []; this.solids = []; this.anim = []; this.gy = () => 0; this.bounds = [-120, -80, 120, 80];
    this.edge = [0, 0, 1e5, 1e5];   // centre x, z and radii x, z of the ellipse past which the world fades into the fog
  }
  fadeAt(p) { const [cx, cz, rx, rz] = this.edge; return THREE.MathUtils.smoothstep(Math.hypot((p.x - cx) / rx, (p.z - cz) / rz), ...FADE); }
  info(name) { return this.T[name]?.userData.info; }
  // a model instance at (x, z) standing on the ground; sat 1 = full colour (an asset), tint = painted in a status colour
  put(name, x, z, { rot = 0, y = 0, scale = 1, sat = 0, tint = null, solid = false } = {}) {
    const t = this.T[name]; if (!t) return null;
    const o = t.clone(true), c = sat || (NETWORK.has(name) ? 1 : 0);   // network and crews: always in colour, even as scenery
    const steel = STEEL.has(name) && tint == null;
    if (steel) tint = STEEL_HEX;   // bare-metal network gear reads as grey on grey: natural steel, darker, and its lamp lit (below)
    o.traverse((m) => { if (m.isMesh) { m.material = tint != null ? tinted(m.material, tint) : ink(m.material, c); m.castShadow = true; m.receiveShadow = true; } });
    o.position.set(x, this.gy(x, z) + y, z); o.rotation.y = rot; if (scale !== 1) o.scale.multiplyScalar(scale);
    if (steel && name === "street_light") { const at = lampAt(t); if (at) lampGlow(o, at); }
    if (NO_INK.has(name)) o.userData.noInk = true;
    o.userData.info = t.userData.info;
    R.world.add(o);
    if (solid) this.block(o, solid === true ? 0 : solid);
    return o;
  }
  block(o, shrink = 0) {   // its footprint is in the lineworker's way
    o.updateMatrixWorld(true); const b = new THREE.Box3().setFromObject(o);
    this.solids.push([b.min.x + shrink, b.min.z + shrink, b.max.x - shrink, b.max.z - shrink]);
  }
  blockAt(x, z, r) { this.solids.push([x - r, z - r, x + r, z + r]); }
  // something clickable: an invisible box over it, its tag, and what the lineworker is told about it
  asset(o, item, { tagY = null, pri = 1, text = null } = {}) {
    if (!o || !item) return;
    o.updateMatrixWorld(true);
    const b = new THREE.Box3().setFromObject(o), sz = b.getSize(new THREE.Vector3()), c = b.getCenter(new THREE.Vector3());
    const px = new THREE.Mesh(new THREE.BoxGeometry(Math.max(1, sz.x), Math.max(1, sz.y), Math.max(1, sz.z)), new THREE.MeshBasicMaterial({ visible: false }));
    px.position.copy(c); px.layers.set(1); px.userData.own = true; px.userData.item = item; px.userData.root = o; R.world.add(px); this.picks.push(px);
    item._box = b;
    if (text !== false) this.tag(text || item.label, item.status, new THREE.Vector3(c.x, tagY ?? b.max.y + 1.2, c.z), { pri, item, root: o });
  }
  tag(text, status, pos, { pri = 1, item = null, root = null, cls = "" } = {}) {
    const el = h("div", { class: `s3-tag ${status || ""} ${cls}` }, status ? h("i", { style: `background:${HEX(COL[status] || 0x999999)}` }) : null, h("span", {}, text));
    if (item) { el.classList.add("click"); el.addEventListener("click", (e) => { e.stopPropagation(); visit(item, root); }); }
    this.wrap.append(el);
    this.tags.push({ el, pos, pri: pri + ({ red: 4, amber: 2 }[status] || 0), w: 0, h: 0 });
    return el;
  }
  own(mesh) { mesh.userData.own = true; mesh.castShadow = true; mesh.receiveShadow = true; R.world.add(mesh); return mesh; }
  // flat painted strips: road, kerb, footpath, driveway, slab (repeat = metres per texture tile)
  strip(tex, x0, z0, x1, z1, y = 0.03, tile = 4) {
    const w = x1 - x0, d = z1 - z0, t = TEX[tex](); t.repeat.set(w / tile, d / tile);
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, d), makeInk({ map: t }, 0));
    m.rotation.x = -Math.PI / 2; m.position.set((x0 + x1) / 2, y, (z0 + z1) / 2); m.castShadow = false;
    return this.own(m);
  }
  slab(x0, z0, x1, z1, hgt, grey = 0xbcb8b0) {
    const m = new THREE.Mesh(new THREE.BoxGeometry(x1 - x0, hgt, z1 - z0), makeInk({ color: new THREE.Color(grey) }, 0));
    m.position.set((x0 + x1) / 2, this.gy((x0 + x1) / 2, (z0 + z1) / 2) + hgt / 2, (z0 + z1) / 2); return this.own(m);
  }
  ground(tex, size = 1400, flat = true) {
    const g = new THREE.PlaneGeometry(size, size, flat ? 1 : 220, flat ? 1 : 220); g.rotateX(-Math.PI / 2);
    if (!flat) { const p = g.attributes.position; for (let i = 0; i < p.count; i++) p.setY(i, this.gy(p.getX(i), p.getZ(i))); g.computeVertexNormals(); }
    const t = TEX[tex](); t.repeat.set(size / 22, size / 22);
    const m = new THREE.Mesh(g, makeInk({ map: t }, 0)); m.castShadow = false; return this.own(m);
  }
  // a road along x with kerb and gutter both sides
  road(x0, x1, zc, width) {
    this.strip("asphalt", x0, zc - width / 2, x1, zc + width / 2, 0.04, 6);
    for (const s of [-1, 1]) this.slab(x0, zc + s * (width / 2) - (s > 0 ? 0 : 0.45), x1, zc + s * (width / 2) + (s > 0 ? 0.45 : 0), 0.16, 0xc4c0b8);
  }
  // a street along x (axis "x") or z, centred on c, from a0 to a1: asphalt, kerbs and footpaths, the kerbs and paths broken at `gaps`
  // (the centres of the streets that cross it)
  street(axis, a0, a1, c, width = 8, gaps = []) {
    const rect = (tex, u0, v0, u1, v1, y, tile) => axis === "x" ? this.strip(tex, u0, v0, u1, v1, y, tile) : this.strip(tex, v0, u0, v1, u1, y, tile);
    const box = (u0, v0, u1, v1, hgt, g) => axis === "x" ? this.slab(u0, v0, u1, v1, hgt, g) : this.slab(v0, u0, v1, u1, hgt, g);
    rect("asphalt", a0, c - width / 2, a1, c + width / 2, axis === "x" ? 0.04 : 0.042, 6);
    const cuts = [a0, ...gaps.filter((g) => g > a0 && g < a1).sort((p, q) => p - q).flatMap((g) => [g - width / 2 - 1.6, g + width / 2 + 1.6]), a1];
    for (let i = 0; i + 1 < cuts.length; i += 2) {
      const u0 = cuts[i], u1 = cuts[i + 1]; if (u1 - u0 < 1) continue;
      for (const sd of [-1, 1]) {
        box(u0, c + sd * (width / 2) - (sd > 0 ? 0 : 0.45), u1, c + sd * (width / 2) + (sd > 0 ? 0.45 : 0), 0.16, 0xc4c0b8);
        rect("concrete", u0, sd > 0 ? c + width / 2 + 2.4 : c - width / 2 - 3.8, u1, sd > 0 ? c + width / 2 + 3.8 : c - width / 2 - 2.4, 0.05, 1.6);
      }
    }
  }
  // a conductor hanging between two points with sag (parabola); r is its weight class (HV 0.05, LV bundle 0.085, service 0.03)
  wire(a, b, sag, r = 0.055) {
    const pts = []; for (let i = 0; i <= 24; i++) { const t = i / 24, p = a.clone().lerp(b, t); p.y -= 4 * sag * t * (1 - t); pts.push(p); }
    const g = new LineGeometry(); g.setPositions(pts.flatMap((p) => [p.x, p.y, p.z]));
    g.setColors(pts.flatMap((p) => WIRE.clone().lerp(R.scene.fog.color, this.fadeAt(p)).toArray()));
    const l = new Line2(g, lineMat(Math.round(THREE.MathUtils.clamp(r * 32, 1.1, 3) * 10) / 10)); l.computeLineDistances();
    l.userData.own = true; l.userData.noInk = true; R.world.add(l);
    const sh = new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts), 24, Math.max(0.035, r * 0.8), 4, false), SHADOW_ONLY);
    sh.castShadow = true; sh.userData.own = true; sh.userData.noInk = true; R.world.add(sh);
    return l;
  }
  // a pole standing on the line, its crossarm across the line direction (dir = unit vector along the line, in the ground plane)
  linePole(name, x, z, dir, opt = {}) {
    const n = new THREE.Vector3(-dir.z, 0, dir.x);   // across the line
    const o = this.put(name, x, z, { ...opt, rot: Math.atan2(-n.z, n.x) });
    if (o && opt.lean) o.rotateOnWorldAxis(dir.clone().normalize(), THREE.MathUtils.degToRad(opt.lean));
    if (o) { this.blockAt(x, z, 0.8); o.updateMatrixWorld(true); }
    return o;
  }
  attach(o) {   // world positions of a placed pole's phase attachments (sorted across the line) and its LV point
    const i = o?.userData.info; if (!i?.phases) return null;
    o.updateMatrixWorld(true);
    return { hv: i.phases.map((p) => o.localToWorld(p.clone())), lv: o.localToWorld(i.lv.clone()), top: o.localToWorld(new THREE.Vector3(0, i.top, 0)) };
  }
  // string the line: phases i to i between each pair of poles (sag grows with span length); ABC is one bundled cable
  string(poles, { abc = false, phases = 3, lv = false, fault = null } = {}) {
    const att = poles.map((o) => this.attach(o));
    for (let i = 0; i + 1 < att.length; i++) {
      const a = att[i], b = att[i + 1]; if (!a || !b) continue;
      const L = a.hv[1].distanceTo(b.hv[1]), sag = 0.35 + L * 0.012;
      const sel = abc ? [1] : phases === 1 ? [1] : phases === 2 ? [0, 2] : [0, 1, 2];
      if (fault && fault.span === i) { fault.draw(a, b, sel, sag); }
      else for (const k of sel) this.wire(a.hv[k], b.hv[k], sag, abc ? 0.09 : 0.05);
      if (lv && !(fault && fault.span === i && fault.lvDown)) this.wire(a.lv, b.lv, sag * 1.25, 0.085);
    }
    return att;
  }
}

// ---------------------------------------------------------------- the four kinds of site
const POLE_MODEL = (p) => {
  const conc = (p.material || "").toUpperCase() === "CONCRETE", kinds = (p.devices || []).map((d) => d.kind);
  if (p.ds) return conc ? "pole_transformer_concrete" : "pole_transformer";
  if (kinds.includes("recloser")) return conc ? "recloser_concrete" : "recloser";
  if (kinds.includes("swer_isolator")) return "swer_isolator";
  if (kinds.includes("gas_switch")) return "gas_switch";
  if (p.status === "red" && !conc) return "defect_pole";
  return conc ? "concrete_pole" : "wood_pole";
};
const majority = (spans) => { const c = {}; for (const s of spans) c[s.conductor] = (c[s.conductor] || 0) + 1; return Object.entries(c).sort((a, b) => b[1] - a[1])[0]?.[0] || ""; };
const poleItem = (p) => ({ asset_id: p.asset_id, label: p.asset_id, status: p.status, facts: p.facts, open: p.asset_id });
function poleTag(p) {
  if (p.ds) return { ...p.ds, label: `${p.ds.label}, ${n0(p.ds.facts.kva)} kVA`, open: p.ds.asset_id };
  const d = (p.devices || [])[0];
  if (d) return { asset_id: d.asset_id, label: `${d.label} ${d.asset_id}`, status: p.status === "green" ? "green" : p.status, facts: { ...p.facts, device: d.label }, open: d.asset_id };
  return null;
}
function needs(d) {
  const base = ["network_ute", "patrol_drone", "traffic_cones", "traffic_controller", "ewp_truck_2"];
  const k = d.kind;
  if (k === "zone") return [...base, "power_transformer", "cb_bay", "capacitor_bank", "switchroom", "control_kiosk", "zone_sub_gate", "substation_fence",
    "cable_riser_pole", "wood_pole", "concrete_pole", "gum_tree_tall", "street_tree", "house_brick", "house_weatherboard", "street_light"];
  if (k === "transmission") return [...base, "power_transformer", "cb_bay", "capacitor_bank", "switchroom", "control_kiosk", "zone_sub_gate", "substation_fence",
    "cable_riser_pole", "concrete_pole", "gum_tree_tall", "street_tree"];
  const poles = [...new Set((d.poles || []).map(POLE_MODEL))];
  if (k === "street") return [...base, ...poles, "wood_pole", "house_brick", "house_weatherboard", "solar_house", "solar_house_2", "colorbond_fence", "street_tree",
    "street_light", "wheelie_bins", "parked_car", "hazard_tree", "broken_pole", "defect_pole", "cockatoo", "crane_truck"];
  if (k === "rural") return [...base, ...poles, "wood_pole", "gum_tree_tall", "hazard_tree", "farm_shed", "water_tank_house", "kangaroo", "wombat", "cow",
    "fire_danger_sign", "ewp_truck", "vegetation_worker", "broken_pole", "defect_pole", "cockatoo"];
  return [...base, "kiosk", "rmu", "lv_pillar", "solar_house", "solar_house_2", "house_brick", "apartment_block", "shop_strip", "school", "street_tree",
    "street_light", "dc_charging_hub", "kerbside_charger", "wheelie_bins", "parked_car", "colorbond_fence"];
}

// A suburban lot row along a road on one side: houses facing the street, driveways, side fences, bins and front-yard trees.
function lots(S, side, x0, x1, { front, frontage = 20, solarShare = 0, models = ["house_brick", "house_weatherboard"], solarModels = ["solar_house", "solar_house_2"],
  bins = true, trees = true, fences = true, kerb = 4.2, zr = 0 } = {}) {
  const n = Math.max(1, Math.floor((x1 - x0) / frontage)), r = S.r, out = [];
  const solar = new Set(); const want = Math.round(n * solarShare);
  while (solar.size < Math.min(n, want)) solar.add(Math.floor(r() * n));
  for (let i = 0; i < n; i++) {
    const lx = x0 + (i + 0.5) * ((x1 - x0) / n), isSolar = solar.has(i);
    const name = isSolar ? solarModels[Math.floor(r() * solarModels.length)] : models[Math.floor(r() * models.length)];
    const inf = S.info(name); if (!inf) continue;
    const depth = inf.foot[1], set = 4.5 + r() * 2.5, zc = zr + side * (front + set + depth / 2), hx = lx + (r() - 0.5) * 1.6;
    const o = S.put(name, hx, zc, { rot: side < 0 ? 0 : Math.PI, scale: 0.95 + r() * 0.1, solid: true });
    if (o && r() < 0.5) o.scale.x *= -1;   // mirrored, so a street of four house designs doesn't read as one house repeated
    S.strip("concrete", lx + 4.5, zr + Math.min(side * kerb, side * (front + set)), lx + 7.7, zr + Math.max(side * kerb, side * (front + set)), 0.05, 3.2);
    const fz = S.info("colorbond_fence");
    if (fz && fences) for (let k = 0; k < 4; k++) { const z = zr + side * (front + set + 1.5 + k * fz.foot[0]); const f = S.put("colorbond_fence", x0 + i * ((x1 - x0) / n), z, { rot: Math.PI / 2 }); if (f) S.block(f); }
    if (bins && r() < 0.35) S.put("wheelie_bins", lx + 2.4, zr + side * (kerb + 0.7), { rot: side < 0 ? 0 : Math.PI });
    if (trees && r() < 0.45) S.put("street_tree", lx - 6 + r() * 2, zr + side * (front + 2.2), { rot: r() * 6, scale: 0.7 + r() * 0.4, solid: 2.5 });
    if (trees && r() < 0.6) S.put("street_tree", lx + (r() - 0.5) * 10, zr + side * (front + set + depth + 6 + r() * 6), { rot: r() * 6, scale: 0.8 + r() * 0.5, solid: 2.5 });
    out.push({ o, x: hx, z: zc, side, solar: isSolar, name, svc: new THREE.Vector3(hx - 2, Math.min(inf.h * 0.55, 4.2), zr + side * (front + set + 0.4)) });
  }
  return out;
}

async function buildStreet(S) {
  const d = S.d, P = d.poles || [], N = P.length, SP = 36, L = Math.max(1, N - 1) * SP, ZP = -5.0, X = (i) => (i - (N - 1) / 2) * SP;
  const fault = d.fault, cause = fault?.cause_code;
  S.bounds = [-L / 2 - 70, -90, L / 2 + 70, 90];   // the shadowed heart of the scene; the suburb carries on into the haze
  S.edge = [0, 0, L / 2 + 170, 130];
  S.ground("grass");
  // a suburban grid: this street, a parallel street behind the back fences on each side, and a cross street at each end of the run
  const XC = [-L / 2 - 52, L / 2 + 52], ZB = 80, FAR = L / 2 + 230;
  S.street("x", -FAR, FAR, 0, 8, XC);
  for (const z of [-ZB, ZB]) S.street("x", -FAR, FAR, z, 8, XC);
  for (const x of XC) S.street("z", -ZB - 150, ZB + 150, x, 8, [-ZB, 0, ZB]);
  const c = d.customers || {}, share = c.n ? c.solar / c.n : 0;
  const blocks = [[-L / 2 - 40, L / 2 + 40], [XC[1] + 9, XC[1] + 110], [XC[0] - 110, XC[0] - 9]];
  const houses = [...lots(S, -1, ...blocks[0], { front: 8, solarShare: share }), ...lots(S, 1, ...blocks[0], { front: 8, solarShare: share })];
  for (const [x0, x1] of blocks.slice(1)) for (const sd of [-1, 1]) lots(S, sd, x0, x1, { front: 8, solarShare: share, fences: false, bins: false });
  for (const z of [-ZB, ZB]) for (const [x0, x1] of blocks) for (const sd of [-1, 1]) lots(S, sd, x0, x1, { front: 8, solarShare: share, zr: z, fences: false, bins: false });
  // the next streets have their own low-voltage pole lines
  for (const z of [-ZB, ZB]) {
    const row = []; for (let x = -L / 2 - 36; x <= L / 2 + 36; x += 36) row.push(S.linePole("wood_pole", x, z - 5, new THREE.Vector3(1, 0, 0)));
    S.string(row, { lv: true });
    for (let x = -L / 2 - 30; x <= L / 2 + 30; x += 40) S.put("street_light", x + 6, z + 5.6, { rot: -Math.PI / 2 });
  }
  const dir = new THREE.Vector3(1, 0, 0), poles = [];
  P.forEach((p, i) => {
    let name = POLE_MODEL(p);
    if (fault && i === fault.pole_index + 1 && cause === "VEH") name = "broken_pole";
    if (fault && i === fault.pole_index && ["EQF", "LTN"].includes(cause) && !p.ds) name = "defect_pole";
    const o = S.linePole(name, X(i), ZP, dir, { sat: 1 });
    poles.push(o);
    const it = poleItem(p), tg = poleTag(p);
    S.asset(o, tg ? { ...it, ...tg } : it, { text: tg ? tg.label : p.status !== "green" ? p.asset_id : false, pri: tg ? 3 : 2 });
  });
  const abc = (d.spans || []).length > 0 && d.spans.every((x) => /^ABC/.test(x.conductor || ""));   // bundled HV only when the whole run is ABC
  const fd = fault && faultDraw(S, fault, cause, ZP);
  const att = S.string(poles, { abc, lv: true, fault: fd });
  // services: every house takes its supply off the LV at the nearest pole
  for (const hs of houses) {
    const k = att.reduce((b, a, i) => (a && Math.abs(a.lv.x - hs.svc.x) < Math.abs((att[b]?.lv.x ?? 1e9) - hs.svc.x) ? i : b), 0);
    if (att[k] && Math.abs(att[k].lv.x - hs.svc.x) < 30) S.wire(att[k].lv, hs.svc, 0.35, 0.03);
  }
  // spans the LiDAR flags: tag them mid-span
  (d.spans || []).forEach((s, i) => {
    if (s.status === "green" && !s.fault) return;
    const a = att[i], b = att[i + 1]; if (!a || !b) return;
    const m = a.top.clone().lerp(b.top, 0.5); m.y += 1.6;
    const item = { asset_id: s.span_id, label: s.fault ? `Fault: ${fault.cause_label}` : `${s.span_id}, ${n1(s.clearance_m)} m to vegetation`, status: s.fault ? "red" : s.status, facts: s.facts, open: s.span_id };
    S.tag(item.label, item.status, m, { pri: s.fault ? 6 : 2, item });
    spanPick(S, a.top, b.top, item);
  });
  // the street furniture: lights on the far nature strip, street trees, a parked car or two
  for (let x = -L / 2 - 30; x <= L / 2 + 30; x += 40) S.put("street_light", x + 6, 5.6, { rot: -Math.PI / 2 });
  for (let x = -L / 2 - 20; x <= L / 2 + 20; x += 40) S.put("street_tree", x + 24, 5.4, { rot: S.r() * 6, scale: 0.8 + S.r() * 0.3, solid: 2.5 });
  S.put("parked_car", -L / 2 + 14, 2.6, { rot: 0 }); S.put("parked_car", L / 2 - 30, -2.6, { rot: Math.PI });
  if (fault && cause === "VEH" && N > fault.pole_index + 1) S.put("parked_car", X(fault.pole_index + 1) + 2.2, ZP + 2.4, { rot: 0.95, solid: true });   // the car that hit it
  const dsI = Math.max(0, P.findIndex((p) => p.ds));
  if (fault) await faultCrew(S, fault, X(fault.span_index ?? fault.pole_index) + SP / 2, ZP, d.outage);
  else { S.put("network_ute", X(dsI) - 8, -2.4, { rot: 0, tint: COL[d.status] || COL.green, solid: true }); }
  S.start = new THREE.Vector3(X(dsI) - 2, 0, -7.1);
  // start at street level beside the pole-mounted substation, looking down the street; zoom out to see the suburb
  S.view = { target: new THREE.Vector3(X(dsI) + 16, 2, 0), dist: 58, theta: Math.PI - 0.3, phi: 1.04, max: 300 };
  S.fog = [220, 560];
}

// a thin invisible box along a span, so the span itself is clickable
function spanPick(S, a, b, item) {
  const len = a.distanceTo(b), m = new THREE.Mesh(new THREE.BoxGeometry(len, 3, 3), new THREE.MeshBasicMaterial({ visible: false }));
  m.position.copy(a).lerp(b, 0.5); m.position.y -= 0.6; m.lookAt(b.x, m.position.y, b.z); m.rotateY(Math.PI / 2);
  m.layers.set(1); m.userData.own = true; m.userData.item = item; m.userData.root = m; R.world.add(m); S.picks.push(m);
  item._box = new THREE.Box3().setFromObject(m);
}

// How each cause looks on the faulted span: a tree down on the conductors, a pole hit by a car, a cracked crossarm, a cockatoo.
function faultDraw(S, fault, cause, zLine) {
  const fd = { span: fault.span_index, lvDown: ["VEG", "WND", "VEH"].includes(cause) };
  fd.draw = (a, b, sel, sag) => {
    const mid = a.hv[1].clone().lerp(b.hv[1], 0.5);
    if (["VEG", "WND", "VEH"].includes(cause)) {   // conductors down: two phases broken and lying on the ground, one sagging hard
      const g1 = new THREE.Vector3(mid.x - 3, S.gy(mid.x - 3, mid.z + 2) + 0.15, mid.z + 2.5), g2 = new THREE.Vector3(mid.x + 4, S.gy(mid.x + 4, mid.z + 3) + 0.15, mid.z + 3.2);
      sel.forEach((k, j) => {
        if (j === 0) { S.wire(a.hv[k], g1, 0.8); S.wire(g2, b.hv[k], 0.8); }
        else S.wire(a.hv[k], b.hv[k], sag * (j === 1 ? 6 : 1));
      });
      if (cause !== "VEH") {   // the tree: a dead gum fallen from the front yard across the line
        const inf = S.info("hazard_tree"), lz = zLine - 10;
        const t = S.put("hazard_tree", mid.x, lz, { sat: 1 });
        if (t && inf) {
          const lv = inf.lean.clone(), want = new THREE.Vector3(0, 0, 1);
          t.rotation.y = Math.atan2(want.x, want.z) - Math.atan2(lv.x, lv.z);   // its natural lean toward the line
          t.rotateOnWorldAxis(new THREE.Vector3(1, 0, 0), 0.72);                 // and fallen until the trunk rests on the conductors
          S.tag("Tree on the line", "red", new THREE.Vector3(mid.x, mid.y - 3, zLine - 3), { pri: 5 });
        }
      }
    } else for (const k of sel) S.wire(a.hv[k], b.hv[k], sag);
    if (cause === "ANM" && S.T.cockatoo) S.put("cockatoo", a.hv[0].x, a.hv[0].z, { y: a.hv[0].y - S.gy(a.hv[0].x, a.hv[0].z) + 0.05, sat: 1 });
  };
  return fd;
}
async function faultCrew(S, fault, x, zLine, out) {
  const drone = S.put("patrol_drone", x, zLine + 2, { y: 15, sat: 1 });
  if (drone) S.anim.push((t) => { drone.position.y = 15 + Math.sin(t * 1.7) * 0.4; drone.rotation.y = Math.sin(t * 0.4) * 0.5; });
  if (out?.crew_id) {
    S.put("ewp_truck_2", x - 9, zLine + 2.6, { sat: 1, solid: true });
    S.put("traffic_controller", x - 24, 1.5, { rot: Math.PI / 2, sat: 1 });
    for (const dx of [-17, -13, 6]) S.put("traffic_cones", x + dx, zLine + 4.2, { rot: 0.2 });
  } else S.put("network_ute", x - 10, zLine + 2.6, { rot: 0, tint: COL.red, solid: true });
}

async function buildRural(S) {
  const d = S.d, P = d.poles || [], N = P.length;
  // terrain: rolling hills, a little flatter along the easement
  // rolling ground that steepens away from the easement, rising behind the bush to a timbered ridge (the Ranges) in the haze
  const ss = (a, b, v) => { const t = THREE.MathUtils.clamp((v - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
  S.gy = (x, z) => {
    const away = 0.55 + 0.45 * ss(25, 90, Math.abs(z));
    const roll = (4.2 * Math.sin(x * 0.016 + 1.3) * Math.cos(z * 0.012) + 2.4 * Math.sin(x * 0.037 + z * 0.021) + 1.3 * Math.cos(z * 0.045 - x * 0.011)) * away;
    const ridge = 46 * ss(-140, -330, z) * (0.78 + 0.22 * Math.sin(x * 0.011 + 0.8) + 0.08 * Math.sin(x * 0.043));
    return roll + 0.004 * Math.max(0, -z - 30) ** 1.6 + ridge - 1.6 * Math.exp(-(z * z) / 900);
  };
  // the real run, turned to lie along x and compressed to about 52 m spans, keeping its bends
  const pts = P.map((p) => new THREE.Vector2(p.x, p.z));
  const ang = pts.length > 1 ? Math.atan2(pts[N - 1].y - pts[0].y, pts[N - 1].x - pts[0].x) : 0;
  const rot = pts.map((p) => new THREE.Vector2(p.x * Math.cos(-ang) - p.y * Math.sin(-ang), p.x * Math.sin(-ang) + p.y * Math.cos(-ang)));
  const real = rot.slice(1).reduce((s, p, i) => s + p.distanceTo(rot[i]), 0) / Math.max(1, N - 1), k = 52 / Math.max(1, real);
  const cx = (rot[0].x + rot[N - 1].x) / 2;
  const XY = rot.map((p) => new THREE.Vector2((p.x - cx) * k, THREE.MathUtils.clamp(p.y * k, -14, 14)));
  const zMid = XY.reduce((s, p) => s + p.y, 0) / Math.max(1, N); XY.forEach((p) => { p.y -= zMid; });
  const L = Math.max(60, XY[N - 1].x - XY[0].x);
  S.bounds = [-L / 2 - 90, -90, L / 2 + 90, 90];
  S.edge = [0, -40, L / 2 + 160, 520];   // deep, so the ridge stays in the haze behind the bush
  S.ground("paddock", 1600, false);
  // the maintenance track along the easement (south of every pole), following the ground
  const lineZ = (x) => { if (x <= XY[0].x) return XY[0].y; for (let i = 1; i < N; i++) if (x <= XY[i].x) { const t = (x - XY[i - 1].x) / Math.max(1e-6, XY[i].x - XY[i - 1].x); return XY[i - 1].y + (XY[i].y - XY[i - 1].y) * t; } return XY[N - 1].y; };
  const trackZ = (x) => lineZ(x) + 9 + 1.5 * Math.sin(x * 0.03);
  track(S, -L / 2 - 120, L / 2 + 120, trackZ);
  const poles = [], dirs = XY.map((p, i) => { const a = XY[Math.max(0, i - 1)], b = XY[Math.min(N - 1, i + 1)]; return new THREE.Vector3(b.x - a.x, 0, b.y - a.y).normalize(); });
  const spans = d.spans || [];
  P.forEach((p, i) => {
    const lean = spans[i]?.lean_deg ?? spans[i - 1]?.lean_deg ?? 0;
    const o = S.linePole(POLE_MODEL(p), XY[i].x, XY[i].y, dirs[i], { sat: 1, lean: lean > 1.5 ? lean : 0 });
    poles.push(o);
    const it = poleItem(p), tg = poleTag(p);
    if (lean > 4) it.label = `${p.asset_id}, leaning ${n1(lean)} degrees`;
    S.asset(o, tg ? { ...it, ...tg } : it, { text: tg ? tg.label : (p.status !== "green" || lean > 4) ? it.label : false, pri: 2 });
  });
  const cond = majority(spans), fault = d.fault, wires = { abc: spans.length > 0 && spans.every((x) => /^ABC/.test(x.conductor || "")), phases: /^SC\/GZ/.test(cond) ? (d.swer ? 1 : 2) : 3 };
  const att = S.string(poles, { ...wires, fault: fault && faultDraw(S, fault, fault.cause_code, 0) });
  for (const [i, s] of [[0, -1], [N - 1, 1]]) {   // the line carries on past the site both ways, into the haze
    const dr = dirs[i].clone().multiplyScalar(s), run = [poles[i]];
    for (let k = 1; k <= 3; k++) run.push(S.linePole("wood_pole", XY[i].x + dr.x * 52 * k, XY[i].y + dr.z * 52 * k, dirs[i], {}));
    S.string(run, wires);
  }
  // vegetation where the LiDAR says it is: a gum beside each tight span at its measured clearance, a hazard tree leaning in
  const gum = S.info("gum_tree_tall"), haz = S.info("hazard_tree"), r = S.r, keep = [];
  spans.forEach((s, i) => {
    const a = att[i], b = att[i + 1]; if (!a || !b) return;
    const mid = a.top.clone().lerp(b.top, 0.5), dir = b.top.clone().sub(a.top).setY(0).normalize(), n = new THREE.Vector3(-dir.z, 0, dir.x);
    const item = { asset_id: s.span_id, label: `${s.span_id}, ${n1(s.clearance_m)} m to vegetation`, status: s.status, facts: s.facts, open: s.span_id };
    if (s.clearance_m != null && s.clearance_m < 3.5 && gum) {
      const off = 1.8 + s.clearance_m + gum.foot[0] * 0.32, p = mid.clone().addScaledVector(n, -off);
      S.put("gum_tree_tall", p.x, p.z, { rot: r() * 6, scale: 0.9 + r() * 0.15, solid: 4 }); keep.push(p);
    }
    if (s.hazard_tree && haz) {
      const p = mid.clone().addScaledVector(n, 9.5).addScaledVector(dir, 6), t = S.put("hazard_tree", p.x, p.z, { sat: 1, solid: 4 });
      if (t) { const want = n.clone().negate(); t.rotation.y = Math.atan2(want.x, want.z) - Math.atan2(haz.lean.x, haz.lean.z); }
      S.tag("Hazard tree", "red", new THREE.Vector3(p.x, S.gy(p.x, p.z) + 15, p.z), { pri: 3 }); keep.push(p);
    }
    if (s.status !== "green" || s.fault) {
      const m = mid.clone(); m.y += 1.8;
      const it2 = s.fault ? { ...item, label: `Fault: ${fault.cause_label}`, status: "red" } : item;
      S.tag(it2.label, it2.status, m, { pri: s.fault ? 6 : s.status === "red" ? 4 : 2, item: it2 });
    }
    spanPick(S, a.top, b.top, item);
  });
  // the bush behind the easement, the paddock in front of it
  for (let i = 0, tries = 0; i < 84 && tries < 1200; tries++) {
    const x = -L / 2 - 100 + r() * (L + 200), z = lineZ(x) - 17 - r() * 95, young = r() < 0.32;
    if (keep.some((p) => Math.hypot(p.x - x, p.z - z) < (young ? 6 : 9))) continue;
    S.put(!young && r() < 0.1 ? "hazard_tree" : "gum_tree_tall", x, z, { rot: r() * 6, scale: young ? 0.4 + r() * 0.2 : 0.7 + r() * 0.45, solid: young ? 1.5 : 4 });
    keep.push(new THREE.Vector3(x, 0, z)); i++;
  }
  const zp = (x, dz) => trackZ(x) + dz;   // the paddock, measured out from the track
  for (let i = 0; i < 34; i++) {   // timber on the ridge slopes (scenery only, beyond the walkable site)
    const x = -L / 2 - 260 + r() * (L + 520), z = -150 - r() * 170;
    S.put("gum_tree_tall", x, z, { rot: r() * 6, scale: 0.9 + r() * 0.5 });
  }
  for (let i = 0; i < 9; i++) { const x = -L / 2 - 60 + r() * (L + 120), z = zp(x, 34 + r() * 60); S.put("gum_tree_tall", x, z, { rot: r() * 6, scale: 0.7 + r() * 0.4, solid: 4 }); }
  S.put("farm_shed", L * 0.22, zp(L * 0.22, 44), { rot: Math.PI + 0.3, solid: true });
  S.put("water_tank_house", -L * 0.28, zp(-L * 0.28, 50), { rot: Math.PI - 0.2, solid: true });
  for (const [x, z, a] of [[18, 22, 1.2], [25, 26, 2.6], [12, 29, 0.4]]) S.put("cow", x, zp(x, z), { rot: a });
  for (const [x, z, a] of [[-32, 14, 0.9], [-27, 18, 2.1], [-37, 12, 1.6]]) S.put("kangaroo", x, zp(x, z), { rot: a });
  S.put("wombat", 8, zp(8, 6), { rot: 2.2 });
  // the fire danger sign at the track gate, and the vegetation crew when a span is red
  const fx = XY[0].x + 6, fz = trackZ(fx) + 5;
  const sign = S.put("fire_danger_sign", fx, fz, { rot: 0.15, sat: 1, solid: true });
  const fr = { EXTREME: "red", CATASTROPHIC: "red", HIGH: "amber" }[d.fire_rating] || "green";
  S.asset(sign, { asset_id: "fire-danger", label: `Fire danger today: ${d.fire_rating_label}`, status: fr, facts: { rating: d.fire_rating_label, district: d.fire_district } }, { pri: 3 });
  const red = spans.findIndex((s) => s.status === "red");
  if (fault) await faultCrew(S, fault, (att[fault.span_index]?.top.x + att[fault.span_index + 1]?.top.x) / 2 || 0, 0, d.outage);
  else if (d.veg_crew && red >= 0 && att[red + 1]) {
    const m = att[red].top.clone().lerp(att[red + 1].top, 0.5);
    S.put("ewp_truck", m.x - 6, trackZ(m.x - 6), { sat: 1, solid: true });
    const vw = S.put("vegetation_worker", m.x + 2, lineZ(m.x) - 6, { rot: Math.PI, sat: 1 });
    if (vw) S.tag("Vegetation crew", null, new THREE.Vector3(m.x + 2, vw.position.y + 2.8, lineZ(m.x) - 6), { pri: 2 });
  }
  S.put("network_ute", fx + 9, fz - 2, { rot: 0.1, tint: COL[d.status] || COL.green, solid: true });
  S.start = new THREE.Vector3(fx + 5, 0, fz - 4);
  S.view = { target: new THREE.Vector3(fx, 5, (lineZ(fx) + fz) / 2), dist: 78, theta: 1.35, phi: 1.18, max: Math.max(350, L * 1.5) };   // the zone substation's height and zoom
}
function track(S, x0, x1, zf, w = 3.6) {   // a dirt ribbon draped over the terrain
  const seg = 120, pos = [], uv = [], idx = [];
  for (let i = 0; i <= seg; i++) {
    const x = x0 + ((x1 - x0) * i) / seg, z = zf(x);
    for (const s of [-1, 1]) { pos.push(x, S.gy(x, z + s * w / 2) + 0.06, z + s * w / 2); uv.push(((x - x0) / 6), s > 0 ? 1 : 0); }
    if (i < seg) { const a = i * 2; idx.push(a, a + 2, a + 1, a + 1, a + 2, a + 3); }
  }
  const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3)); g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
  g.setIndex(idx); g.computeVertexNormals();
  const t = TEX.dirt(); t.repeat.set(1, 1);
  const m = new THREE.Mesh(g, makeInk({ map: t }, 0)); m.castShadow = false; S.own(m);
}

async function buildZone(S) {
  const d = S.d, TX = d.transformers || [], FD = d.feeders || [];
  const XF = 40, Z0 = -34, Z1 = 28;   // the fenced yard
  S.bounds = [-110, -90, 110, 100];
  S.edge = [0, 0, 160, 140];
  S.ground("grass");
  S.strip("gravel", -XF, Z0, XF, Z1, 0.03, 9);
  S.strip("concrete", -3.5, 4, 3.5, 38, 0.05, 3.5);
  S.road(-200, 200, 42, 8);
  S.strip("concrete", -200, 35.2, 200, 36.6, 0.05, 1.6);
  // the security fence with the gate on the street side
  const fw = S.info("substation_fence")?.foot[0] || 4;
  const run = (ax, az, bx, bz, gap) => {
    const len = Math.hypot(bx - ax, bz - az), n = Math.max(1, Math.round(len / fw)), step = len / n, rot = Math.atan2(-(bz - az), bx - ax);
    for (let i = 0; i < n; i++) {
      const t = (i + 0.5) / n, x = ax + (bx - ax) * t, z = az + (bz - az) * t;
      if (gap && Math.abs(x) < gap) continue;
      const f = S.put("substation_fence", x, z, { rot }); if (f) { f.scale.x *= step / fw; S.block(f); }
    }
  };
  run(-XF, Z0, XF, Z0); run(-XF, Z1, XF, Z1, 3.6); run(-XF, Z0, -XF, Z1); run(XF, Z0, XF, Z1);
  S.put("zone_sub_gate", 0, Z1, { rot: 0 });
  // transformers in their bays with firewalls between, each fed from its subtransmission bay behind
  const n = Math.max(1, TX.length), gapX = 22, xs = TX.map((_, i) => (i - (n - 1) / 2) * gapX), zT = -6, zB = -22;
  const att = [];
  TX.forEach((t, i) => {
    const o = S.put("power_transformer", xs[i], zT, { sat: 1, solid: true }), b = S.put("cb_bay", xs[i], zB, { sat: 1, solid: true });
    S.asset(o, { ...t, open: t.asset_id }, { pri: 3 });
    S.tag(`Subtransmission bay ${i + 1}`, null, new THREE.Vector3(xs[i], 11.5, zB), { pri: 0 });
    const ti = o?.userData.info, bi = b?.userData.info;
    if (ti?.bushings?.length === 3 && bi?.bushings) {
      const tb = ti.bushings.map((p) => o.localToWorld(p.clone())), bb = bi.bushings.map((p) => b.localToWorld(p.clone()));
      tb.forEach((p, k) => S.wire(bb[k], p, 0.5, 0.06));
      att.push(bb);
    }
    if (i < n - 1) S.slab(xs[i] + gapX / 2 - 0.25, zT - 5, xs[i] + gapX / 2 + 0.25, zT + 5, 7.5, 0xa8a49c);
  });
  // the subtransmission line comes in over the back fence
  for (const [i, bb] of att.entries()) {
    const dir = new THREE.Vector3(0, 0, 1), sub = [14, 80, 146, 212].map((dz) => S.linePole("concrete_pole", xs[i], Z0 - dz, dir, { scale: 1.3 }));
    const a1 = S.attach(sub[0]);
    if (a1) a1.hv.forEach((p, k) => S.wire(p, bb[2 - k], 0.6, 0.06));
    S.string(sub);
  }
  for (const x of xs) S.strip("concrete", Math.min(x, -14) - 0.6, zT + 4.6, Math.max(x, -14) + 0.6, zT + 5.8, 0.05, 1.2);   // trench covers
  for (const x of xs) S.strip("concrete", x - 0.6, zT + 3.6, x + 0.6, zT + 5.8, 0.05, 1.2);
  S.strip("concrete", -14.6, zT + 4.6, -13.4, 8, 0.05, 1.2);
  S.strip("concrete", -22.6, 22.6, -21.4, 26.2, 0.05, 1.2); S.strip("concrete", -XF + 1, 25.4, XF - 1, 26.6, 0.05, 1.2);
  S.crew = 1;   // the zone yard's plant is life size: so are the crew and the ute, and the switchroom is sized to fit them
  const sw = S.put("switchroom", -22, 15, { solid: true, scale: buildingScale(S.crew) });
  S.tag("11 kV switchroom", null, new THREE.Vector3(-22, topOf(sw) + 1, 15), { pri: 1 });
  S.put("control_kiosk", -8.5, 20, { sat: 1, solid: true });
  const cap = S.put("capacitor_bank", 24, 14, { sat: 1, solid: true });
  if (cap) S.tag("Capacitor bank", null, new THREE.Vector3(24, 6.5, 14), { pri: 1 });
  // feeders leave the switchroom underground and rise up riser poles along the street, then head off overhead
  const left = FD.slice(0, Math.ceil(FD.length / 2)), right = FD.slice(Math.ceil(FD.length / 2));
  const place = (list, s) => list.map((f, j) => ({ f, x: s * (12 + j * 9) }));
  for (const { f, x } of [...place(left, -1).reverse(), ...place(right, 1)]) {
    // each feeder rises up its riser pole and heads straight out overhead, on into the haze
    const far = new THREE.Vector3(x * 1.7, 0, 70), dir = far.clone().sub(new THREE.Vector3(x, 0, 32)).setY(0).normalize();
    const rp = S.linePole("cable_riser_pole", x, 32, dir, { sat: 1 });
    S.string([rp, ...[0, 1, 2, 3, 4].map((k) => S.linePole("wood_pole", far.x + dir.x * 42 * k, far.z + dir.z * 42 * k, dir, {}))]);
    S.asset(rp, { ...f, open: `${f.asset_id}-CB01` }, { pri: f.status === "green" ? 1 : 2 });
  }
  // gums round the yard, and an open reserve across the road that the feeders fan out over
  const r = S.r;
  for (let i = 0, tries = 0; i < 26 && tries < 600; tries++) {
    const a = r() * Math.PI * 2, rr = 54 + r() * 70, x = Math.cos(a) * rr, z = Math.sin(a) * rr * 0.85 - 6;
    if (z > 26 && z < 52) continue;
    if (z >= 52 && FD.some((_, j) => Math.abs(x) < 8)) continue;
    S.put(r() < 0.65 ? "gum_tree_tall" : "street_tree", x, z, { rot: r() * 6, scale: 0.7 + r() * 0.4, solid: 3 }); i++;
  }
  S.put("network_ute", 6, 20, { rot: -0.5, tint: COL[d.status] || COL.green, solid: true, scale: S.crew });
  S.start = new THREE.Vector3(2, 0, 24);
  S.view = { target: new THREE.Vector3(6, 2, -2), dist: 125, theta: 1.45, phi: 1.2 };   // from across the street, the feeders in front, the bays behind
  S.fog = [120, 250];   // a thick haze: the yard is clear, the lines leaving it fade out within a few spans
}

// ---------------------------------------------------------------- the transmission substation (132 kV down to 33 or 66 kV)
// The yard reuses the zone kit (transformers, bays, switchroom, fence, poles). The steelwork is drawn in code: a lattice gantry where the
// 132 kV circuits land, a lattice double-circuit tower line from the bulk supply point, and a rigid busbar on post insulators. The steel
// carries no ink (its many faces would otherwise outline it black from a distance) and is a dark weathered steel, the same tone as the yard equipment, so it stands out against the haze.
const TS = { GALV: 0x4f585d, PORC: 0x8a6a57, ALU: 0xb4b9b7 };
const TS_UP = new THREE.Vector3(0, 1, 0);
const TS_MAT = new Map();
function tsMat(hex) {
  if (!TS_MAT.has(hex)) { const m = makeInk({ color: new THREE.Color(hex) }, 1); m.userData.shared = true; TS_MAT.set(hex, m); }
  return TS_MAT.get(hex);
}
// a square steel section from a to b
function tsMember(a, b, t) {
  const d = b.clone().sub(a), L = d.length(), g = new THREE.BoxGeometry(t, L, t);
  g.applyMatrix4(new THREE.Matrix4().compose(a.clone().add(b).multiplyScalar(0.5), new THREE.Quaternion().setFromUnitVectors(TS_UP, d.normalize()), new THREE.Vector3(1, 1, 1)));
  return g;
}
// a square lattice built up +y from 0 to L, tapering from w0 to w1, n panels, X-braced on all four faces
function tsLattice(L, w0, w1, n, t = 0.16, tb = 0.07) {
  const lv = Array.from({ length: n + 1 }, (_, i) => { const y = (L * i) / n, w = (w0 + ((w1 - w0) * y) / L) / 2; return [[-w, -w], [w, -w], [w, w], [-w, w]].map(([x, z]) => new THREE.Vector3(x, y, z)); });
  const gs = [];
  for (let i = 0; i < n; i++) for (let k = 0; k < 4; k++) {
    const a = lv[i][k], b = lv[i + 1][k], a2 = lv[i][(k + 1) % 4], b2 = lv[i + 1][(k + 1) % 4];
    gs.push(tsMember(a, b, t), tsMember(a, b2, tb), tsMember(a2, b, tb));
  }
  for (const ring of lv) for (let k = 0; k < 4; k++) gs.push(tsMember(ring[k], ring[(k + 1) % 4], tb));
  return mergeGeometries(gs);
}
// lay a +y geometry from point `from` towards `to`
function tsAlong(g, from, to) {
  g.applyMatrix4(new THREE.Matrix4().compose(from, new THREE.Quaternion().setFromUnitVectors(TS_UP, to.clone().sub(from).normalize()), new THREE.Vector3(1, 1, 1)));
  return g;
}
// an insulator string: a rod with porcelain sheds, built along +y
function tsDiscs(L, r = 0.15) {
  const gs = [new THREE.CylinderGeometry(0.03, 0.03, L, 6).translate(0, L / 2, 0)];
  for (let y = 0.12; y < L - 0.05; y += 0.16) gs.push(new THREE.CylinderGeometry(r, r * 0.8, 0.05, 12).translate(0, y, 0));
  return mergeGeometries(gs);
}
// a steel or porcelain mesh in the scene: outside the outline pass (steel and porcelain are picked, not inked)
function tsMesh(S, g, hex) {
  const m = new THREE.Mesh(g, tsMat(hex)); m.userData.noInk = true; return S.own(m);
}
// a strain gantry: lattice columns at xs, a lattice beam across the top, three strain strings per bay pulling towards dz; returns the
// string ends per bay and the steel (for picking)
function tsGantry(S, xs, z, H, bays, dz = -1) {
  const col = [], W = 0.9;
  for (const x of xs) {
    col.push(tsAlong(tsLattice(H, W * 1.3, W, Math.round(H / 1.5)), new THREE.Vector3(x, 0, z), new THREE.Vector3(x, 1, z)));
    col.push(tsMember(new THREE.Vector3(x, H, z), new THREE.Vector3(x, H + 2.2, z), 0.14));   // earth wire peak
  }
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  col.push(tsAlong(tsLattice(x1 - x0, W, W, Math.round((x1 - x0) / 1.5)), new THREE.Vector3(x0, H - W / 2, z), new THREE.Vector3(x1, H - W / 2, z)));
  const steel = tsMesh(S, mergeGeometries(col), TS.GALV);
  for (const x of xs) S.blockAt(x, z, 1);
  const ends = bays.map((bx) => [-4, 0, 4].map((o) => {
    const a = new THREE.Vector3(bx + o, H - 0.9, z + dz * 0.4), b = new THREE.Vector3(bx + o, H - 1.3, z + dz * 2.4);
    S.own(new THREE.Mesh(tsAlong(tsDiscs(a.distanceTo(b)), a, b), tsMat(TS.PORC))).userData.noInk = true;
    return b;
  }));
  return { ends, steel };
}
// a 132 kV double-circuit lattice suspension tower: three arms a side, strings hanging from the arm tips; returns the arm tips per side
// (L and R, three phases each), the earth wire peak and the steel (for picking)
function tsTower(S, x, z, rot, H = 34) {
  const gp = new THREE.Group(); gp.position.set(x, 0, z); gp.rotation.y = rot;
  const gs = [tsLattice(H - 8, 7, 2.4, 9, 0.22, 0.08), tsAlong(tsLattice(8, 2.4, 1.6, 4, 0.18, 0.07), new THREE.Vector3(0, H - 8, 0), new THREE.Vector3(0, H, 0))];
  gs.push(tsAlong(tsLattice(3.5, 1.6, 0.2, 2, 0.14, 0.06), new THREE.Vector3(0, H, 0), new THREE.Vector3(0, H + 3.5, 0)));   // earth wire peak
  const arms = [[H - 12, 5.6], [H - 7, 4.8], [H - 2, 5.6]], tips = { L: [], R: [] };
  const strings = [];
  for (const [y, len] of arms) for (const s of [-1, 1]) {
    const root = new THREE.Vector3(s * 1.0, y, 0), tip = new THREE.Vector3(s * (1.0 + len), y, 0);
    gs.push(tsAlong(tsLattice(len, 1.5, 0.35, 4, 0.14, 0.06), root.clone().add(new THREE.Vector3(0, 0.5, 0)), tip.clone().add(new THREE.Vector3(0, 0.15, 0))));
    gs.push(tsMember(root.clone().add(new THREE.Vector3(0, 2.4, 0)), tip.clone().add(new THREE.Vector3(0, 0.5, 0)), 0.1));
    const bot = tip.clone().add(new THREE.Vector3(0, -2.0, 0));
    strings.push(tsAlong(tsDiscs(2.0), bot, tip));
    tips[s < 0 ? "L" : "R"].push(bot);
  }
  const steelM = new THREE.Mesh(mergeGeometries(gs), tsMat(TS.GALV)), insM = new THREE.Mesh(mergeGeometries(strings), tsMat(TS.PORC));
  for (const m of [steelM, insM]) { m.castShadow = true; m.receiveShadow = true; m.userData.own = true; m.userData.noInk = true; gp.add(m); }
  R.world.add(gp); gp.updateMatrixWorld(true);
  S.blockAt(x, z, 4);
  const w = (p) => gp.localToWorld(p.clone());
  return { L: tips.L.map(w), R: tips.R.map(w), peak: w(new THREE.Vector3(0, H + 3.5, 0)), steel: steelM };
}
// the rigid 132 kV busbar: three aluminium tubes at height Hb on post insulators standing on steel pedestals; returns the tubes (for picking)
function tsBusbar(S, x0, x1, zs, Hb = 10, step = 15) {
  const steel = [], ins = [], tube = [];
  for (const z of zs) {
    tube.push(tsAlong(new THREE.CylinderGeometry(0.075, 0.075, x1 - x0, 8).translate(0, (x1 - x0) / 2, 0), new THREE.Vector3(x0, Hb, z), new THREE.Vector3(x1, Hb, z)));
    for (let x = x0 + 2; x <= x1 - 1; x += step) {
      steel.push(new THREE.CylinderGeometry(0.16, 0.2, Hb - 2.2, 10).translate(x, (Hb - 2.2) / 2, z), new THREE.BoxGeometry(0.7, 0.12, 0.7).translate(x, Hb - 2.2, z));
      ins.push(tsAlong(tsDiscs(2.1, 0.13), new THREE.Vector3(x, Hb - 2.2, z), new THREE.Vector3(x, Hb - 0.1, z)));
      S.blockAt(x, z, 0.6);
    }
  }
  tsMesh(S, mergeGeometries(steel), TS.GALV); tsMesh(S, mergeGeometries(ins), TS.PORC);
  return tsMesh(S, mergeGeometries(tube), TS.ALU);
}

async function buildTransmission(S) {
  const d = S.d, TX = d.transformers || [], IN = (d.incoming || []).slice(0, 2), OUT = d.circuits || [];
  const n = Math.max(1, TX.length), xs = TX.map((_, i) => 24 + (i - (n - 1) / 2) * 28);   // transformers in a row, centred on x = 24
  const XF = 72, Z0 = -86, Z1 = 44;   // a fenced yard about 145 by 130 m
  const site = { asset_id: d.ts_id, label: d.title, status: d.status || "green", facts: d.facts || {}, open: d.ts_id };
  S.bounds = [-140, -150, 140, 120];
  S.edge = [0, -40, 240, 300];
  S.ground("grass");
  S.strip("gravel", -XF, Z0, XF, Z1, 0.03, 9);
  S.strip("concrete", -4, 6, 4, 54, 0.05, 3.5);
  S.strip("concrete", -XF + 4, 2, XF - 4, 8, 0.051, 3.5);   // the internal road across the yard
  S.road(-260, 260, 62, 9);
  S.strip("concrete", -260, 54.5, 260, 56.2, 0.05, 1.6);
  const fw = S.info("substation_fence")?.foot[0] || 4;
  const run = (ax, az, bx, bz, gap) => {
    const len = Math.hypot(bx - ax, bz - az), k = Math.max(1, Math.round(len / fw)), step = len / k, rot = Math.atan2(-(bz - az), bx - ax);
    for (let i = 0; i < k; i++) {
      const t = (i + 0.5) / k, x = ax + (bx - ax) * t, z = az + (bz - az) * t;
      if (gap && Math.abs(x) < gap) continue;
      const f = S.put("substation_fence", x, z, { rot }); if (f) { f.scale.x *= step / fw; S.block(f); }
    }
  };
  run(-XF, Z0, XF, Z0); run(-XF, Z1, XF, Z1, 4); run(-XF, Z0, -XF, Z1); run(XF, Z0, XF, Z1);
  S.put("zone_sub_gate", 0, Z1, { rot: 0 });

  // 132 kV: the incoming circuits land on the line gantry at the back of the yard, one bay each
  const bayX = IN.length > 1 ? [-49.5, -32.5] : [-41], zG = -72, HG = 17;
  const gantry = tsGantry(S, [-58, -41, -24], zG, HG, bayX.slice(0, Math.max(1, IN.length)));
  S.asset(gantry.steel, site, { pri: 2, text: "132 kV line gantry" });
  // the double-circuit tower line from the bulk supply point, out through the back fence into the haze
  const towers = [0, 1, 2, 3, 4].map((k) => tsTower(S, -41 - k * 14, -112 - k * 270, Math.atan2(14, 270) * (k ? 1 : 0.5)));
  towers.forEach((t, k) => {
    const nx = towers[k + 1]; if (!nx) return;
    for (const side of ["L", "R"]) for (let p = 0; p < 3; p++) S.wire(t[side][p], nx[side][p], 7.5, 0.07);
    S.wire(t.peak, nx.peak, 6, 0.04);
  });
  // the first tower's steel is the line: a click on it opens the incoming circuit (the first one on the left arms, the second on the right)
  towers.forEach((t) => {
    if (IN[0]) S.asset(t.steel, { ...IN[0], open: IN[0].asset_id }, { pri: 1, text: false });
  });
  const incomingLabel = IN.length ? `132 kV from ${String(IN[0].bsp_name || "the bulk supply point").replace(/ bulk supply point$/i, "")}` : "132 kV double-circuit line";
  S.tag(incomingLabel, null, towers[0].peak.clone().add(new THREE.Vector3(0, 4, 0)), { pri: 3 });
  for (let c = 0; c < IN.length; c++) {
    const side = c === 0 ? "L" : "R";
    for (let p = 0; p < 3; p++) S.wire(towers[0][side][2 - p], gantry.ends[c][side === "L" ? p : 2 - p], 2.2, 0.07);
  }
  for (const x of [-58, -41, -24]) S.wire(towers[0].peak, new THREE.Vector3(x, HG + 2.2, zG), 1.6, 0.04);

  // line bays: circuit breaker and isolators under each landing, down onto the busbar
  const zL = -58, zBus = [-46.5, -44, -41.5], HB = 10;
  const bus = (k, x) => new THREE.Vector3(x, HB, zBus[k]);
  const bb = (o) => (o?.userData.info?.bushings || []).map((p) => o.localToWorld(p.clone()));
  bayX.slice(0, IN.length).forEach((x, c) => {
    const b = S.put("cb_bay", x, zL, { sat: 1, solid: true, scale: 1.3 }), p = bb(b);
    if (p.length === 3) p.forEach((q, k) => { S.wire(gantry.ends[c][k], q, 0.8, 0.07); S.wire(q, bus(k, q.x), 0.5, 0.07); });
    S.tag(`132 kV circuit ${c + 1} bay`, null, new THREE.Vector3(x, 15, zL), { pri: 1 });
  });
  const x0 = Math.min(-60, Math.min(...xs) - 12), x1 = Math.max(62, Math.max(...xs) + 12);
  const busTube = tsBusbar(S, x0, x1, zBus, HB);
  S.asset(busTube, site, { pri: 2, text: "132 kV busbar" });

  // transformer bays: 132 kV breaker off the busbar, then the transformers with firewalls between them
  xs.forEach((x, i) => {
    const b = S.put("cb_bay", x, -30, { sat: 1, solid: true, scale: 1.3 }), o = S.put("power_transformer", x, -12, { sat: 1, solid: true, scale: 1.6 });
    const p = bb(b), q = bb(o);
    if (p.length === 3) p.forEach((pt, k) => S.wire(bus(k, pt.x), pt, 0.5, 0.07));
    if (p.length === 3 && q.length === 3) q.forEach((pt, k) => S.wire(p[k], pt, 0.7, 0.07));
    if (TX[i]) S.asset(o, { ...TX[i], open: TX[i].asset_id }, { pri: 3 });
    if (i < n - 1) S.slab(x + 14 - 0.3, -18, x + 14 + 0.3, -6, 8.5, 0xa8a49c);   // firewall
    S.strip("concrete", x - 0.7, 7, x + 0.7, 12, 0.05, 1.2);   // cable trench to the switchroom
  });
  S.strip("concrete", Math.min(...xs) - 0.7, 11, Math.max(...xs) + 0.7, 12.4, 0.05, 1.2);

  // the lower-voltage side: switchroom, control building, a capacitor bank, and the control kiosk between the transformers
  const kv2 = d.facts?.secondary_kv || (OUT[0]?.facts?.kv) || 33;
  S.crew = 1.3;   // the yard's bays and poles are drawn at 1.3 times life size to read from the pulled-back view: the crew and ute match them
  const swr = S.put("switchroom", -36, 26, { solid: true, scale: buildingScale(S.crew) });   // both buildings sized so their doors fit the crew
  S.tag(`${kv2} kV switchroom`, null, new THREE.Vector3(-36, topOf(swr) + 1, 26), { pri: 2 });
  const cpb = S.put("switchroom", 40, 30, { solid: true, scale: buildingScale(S.crew) });
  S.tag("Control and protection building", null, new THREE.Vector3(40, topOf(cpb) + 1, 30), { pri: 1 });
  S.put("control_kiosk", xs[0] + 18, 24, { sat: 1, solid: true });
  S.put("capacitor_bank", -54, -14, { sat: 1, solid: true, scale: 1.3 });
  S.tag(`${kv2} kV capacitor bank`, null, new THREE.Vector3(-54, 8, -14), { pri: 1 });

  // the outgoing circuits: cable to a riser pole at the street, then overhead on tall concrete poles into the haze (labels staggered in height)
  const half = Math.ceil(OUT.length / 2);
  OUT.forEach((f, j) => {
    const s = j < half ? -1 : 1, k = j < half ? half - 1 - j : j - half, x = s * (14 + k * 12);
    const far = new THREE.Vector3(x * 1.9, 0, 100), dir = far.clone().sub(new THREE.Vector3(x, 0, 50)).setY(0).normalize();
    const rp = S.linePole("cable_riser_pole", x, 50, dir, { sat: 1, scale: 1.3 });
    S.string([rp, ...[0, 1, 2, 3, 4].map((m) => S.linePole("concrete_pole", far.x + dir.x * 70 * m, far.z + dir.z * 70 * m, dir, { scale: 1.3 }))]);
    const zone = String(f.zone_name || f.label || "").replace(/ zone substation$/i, "");
    S.asset(rp, { ...f, open: f.asset_id }, { pri: 1, tagY: 18 + (j % 3) * 4, text: `${f.facts?.kv || kv2} kV to ${zone}` });
  });

  // trees round the yard (kept out of the tower line's easement), and the network ute at the gate
  const r = S.r;
  for (let i = 0, tries = 0; i < 34 && tries < 900; tries++) {
    const a = r() * Math.PI * 2, rr = 95 + r() * 80, x = Math.cos(a) * rr, z = Math.sin(a) * rr * 0.9 - 20;
    if (z > 40 && z < 75) continue;
    if (z > 75 && Math.abs(x) < 120) continue;
    if (z < -90 && Math.abs(x + 41 + (-(z + 112) / 270) * 14) < 18) continue;
    S.put(r() < 0.65 ? "gum_tree_tall" : "street_tree", x, z, { rot: r() * 6, scale: 0.7 + r() * 0.4, solid: 3 }); i++;
  }
  S.put("network_ute", 16, 36, { rot: -0.2, tint: COL[d.status] || COL.green, solid: true, scale: S.crew });   // parked beside the gate road
  S.start = new THREE.Vector3(0, 0, 36);
  S.view = { target: new THREE.Vector3(0, 4, -22), dist: 190, theta: 1.45, phi: 1.12, max: 420 };   // the zone substation's start, pulled back for the bigger yard
  S.fog = [170, 420];
}

async function buildEstate(S) {
  const d = S.d, K = d.kiosks || [], L = 180;
  S.bounds = [-L / 2 - 60, -70, L / 2 + 60, 70];
  S.edge = [0, 0, L / 2 + 190, 140];
  S.ground("grass");
  // an estate grid: this street, a parallel street behind the back fences on each side, and a cross street past the shops and the school
  const XC = [-L / 2 - 70, L / 2 + 70], ZB = 80, FAR = L / 2 + 300;
  S.street("x", -FAR, FAR, 0, 8, XC);
  for (const z of [-ZB, ZB]) S.street("x", -FAR, FAR, z, 8, XC);
  for (const x of XC) S.street("z", -ZB - 160, ZB + 160, x, 8, [-ZB, 0, ZB]);
  const c = d.customers || {}, share = c.n ? c.solar / c.n : 0, sm = ["solar_house", "solar_house_2"];
  lots(S, -1, -L / 2, L / 2, { front: 8, frontage: 18, solarShare: share, models: ["house_brick"], solarModels: sm });
  lots(S, 1, -L / 2 + 30, L / 2, { front: 8, frontage: 18, solarShare: share, models: ["house_brick"], solarModels: sm });
  const more = (sd, x0, x1, zr) => lots(S, sd, x0, x1, { front: 8, frontage: 18, solarShare: share, models: ["house_brick", "house_weatherboard"], solarModels: sm, fences: false, bins: false, zr });
  for (const z of [-ZB, ZB]) {
    more(Math.sign(z), XC[0] + 10, XC[1] - 10, z);                            // the far side of each parallel street
    more(-Math.sign(z), z < 0 ? -L / 2 - 10 : XC[0] + 10, XC[1] - 10, z);     // backing onto this street (clear of the school)
    for (const x of [XC[0] - 9, XC[1] + 9]) for (const sd of [-1, 1]) more(sd, x < 0 ? x - 100 : x, x < 0 ? x : x + 100, z);
  }
  for (const x of [XC[0] - 9, XC[1] + 9]) for (const sd of [-1, 1]) more(sd, x < 0 ? x - 100 : x, x < 0 ? x : x + 100, 0);
  for (const z of [-ZB, ZB]) for (let x = -L / 2 - 50; x <= L / 2 + 50; x += 40) S.put("street_light", x, z + 5.3, { rot: -Math.PI / 2 });
  // padmount substations on the nature strip, the ring main unit beside the first, LV pillars at the lot boundaries
  const kx = [-34, 38], kz = [-5.6, 5.6];
  K.forEach((k, i) => {
    const o = S.put("kiosk", kx[i], kz[i], { rot: i ? Math.PI : 0, sat: 1, solid: true });
    S.asset(o, { ...k, label: `${k.label}, ${n0(k.facts.kva)} kVA`, open: k.asset_id }, { pri: 3 });
  });
  const rmu = S.put("rmu", -27.5, -5.6, { sat: 1, solid: true });
  if (rmu) S.tag("Ring main unit", null, new THREE.Vector3(-27.5, 3.2, -5.6), { pri: 1 });
  for (let x = -L / 2 + 18; x < L / 2; x += 36) for (const s of [-1, 1]) if (Math.abs(x - kx[0]) > 8 && Math.abs(x - kx[1]) > 8) S.put("lv_pillar", x, s * 6.0, { rot: s < 0 ? 0 : Math.PI, sat: 1 });
  for (let x = -L / 2 - 10; x <= L / 2 + 10; x += 34) S.put("street_light", x, (Math.round(x / 34) % 2 ? 1 : -1) * 5.3, { rot: (Math.round(x / 34) % 2 ? -1 : 1) * Math.PI / 2 });
  for (let x = -L / 2 + 6; x <= L / 2; x += 28) S.put("street_tree", x, 5.0, { rot: S.r() * 6, scale: 0.75, solid: 2.5 });
  // the shops and apartments at the end of the street, the school behind, the public charging hub if the register has one
  S.put("shop_strip", L / 2 + 34, -30, { rot: 0, solid: true });
  S.put("apartment_block", -L / 2 + 8, 22, { rot: Math.PI, solid: true });
  S.put("school", -L / 2 - 40, -36, { rot: Math.PI / 2, solid: true });
  if (d.hub) {
    const hb = S.put("dc_charging_hub", L / 2 + 30, 22, { rot: Math.PI, sat: 1, solid: true });
    S.asset(hb, { asset_id: "charging-hub", label: `Public charging hub, ${d.hub.evse_count} charger${d.hub.evse_count === 1 ? "" : "s"}`, status: "green", facts: { chargers: d.hub.evse_count } }, { pri: 2 });
    S.put("kerbside_charger", L / 2 + 16, 5.2, {});
  }
  S.put("parked_car", -10, 2.6, { rot: 0 }); S.put("parked_car", 60, -2.6, { rot: Math.PI });
  S.put("network_ute", kx[0] + 8, -2.4, { rot: 0, tint: COL[d.status] || COL.green, solid: true });
  S.start = new THREE.Vector3(kx[0] + 4, 0, -7.1);
  S.view = { target: new THREE.Vector3(kx[0] - 6, 2, 2), dist: 72, theta: 1.12, phi: 1.15, max: 300 };   // the zone substation's height and zoom
}

// ---------------------------------------------------------------- the lineworker
let HERO = null;
function hero() {
  if (!HERO) HERO = (async () => {
    const base = await raw("/static/character/engineer_rigged.glb");
    const clips = {};
    await Promise.all(["idle", "run", "head_scratch", "point_up", "angry_stomp"].map(async (n) => {
      try { const a = await raw(`/static/character/anim_${n}.glb`); const c = a.animations[0]; if (c) { const hp = c.tracks.find((t) => /Hips\.position$/.test(t.name)); if (hp) { const v = hp.values; for (let i = 3; i < v.length; i += 3) { v[i] = v[0]; v[i + 2] = v[2]; } } clips[n] = c; } } catch (e) { /* move unavailable */ }
    }));
    return { base, clips };
  })().catch(() => null);
  return HERO;
}
async function addHero(S) {
  const H = await hero(); if (!H) return;
  const o = SkeletonUtils.clone(H.base.scene);
  o.traverse((m) => { if (m.isMesh) { m.material = ink(m.material, 1); m.castShadow = true; m.frustumCulled = false; } });
  const mixer = new THREE.AnimationMixer(o), acts = {};
  for (const [k, c] of Object.entries(H.clips)) acts[k] = mixer.clipAction(c);
  for (const k of ["angry_stomp", "point_up"]) if (acts[k]) { acts[k].setLoop(THREE.LoopOnce, 1); acts[k].clampWhenFinished = true; }
  acts.idle?.play(); mixer.update(0.01);
  o.updateMatrixWorld(true);
  const hgt = new THREE.Box3().setFromObject(o, true).getSize(new THREE.Vector3()).y;
  o.scale.multiplyScalar(THREE.MathUtils.clamp(PERSON_M * (S.crew || 1) / Math.max(1e-3, hgt), 1e-4, 1e4));   // life size, or the scene's crew scale
  const p = openGround(S, S.start || new THREE.Vector3());   // he starts on open ground, clear of the ute and everything else
  o.position.set(p.x, S.gy(p.x, p.z), p.z); o.rotation.y = Math.PI * 0.2;
  R.world.add(o);
  S.hero = { o, mixer, acts, cur: acts.idle, trip: null, head: o.getObjectByName("Head") };
}
function play(Hr, name, fade = 0.25, speed = 1) {
  const a = Hr.acts[name]; if (!a) return;
  a.timeScale = speed; if (a === Hr.cur) return;
  a.reset().fadeIn(fade).play(); Hr.cur?.fadeOut(fade); Hr.cur = a;
}

// The walk map: the site in 0.8 m cells, every solid footprint blocked with a clearance that grows with the crew's build (built once a site).
function walkGrid(S) {
  if (S.walk) return S.walk;
  const C = 0.8, [x0, z0, x1, z1] = S.bounds, nx = Math.ceil((x1 - x0) / C), nz = Math.ceil((z1 - z0) / C), k = S.crew || 1;
  const g = new Uint8Array(nx * nz), m = 0.35 + 0.25 * k;   // clearance: his half-width (0.25 m a life size) and a little room, so it grows with his build, not his height
  for (const s of S.solids) {
    for (let i = Math.max(0, Math.floor((s[0] - m - x0) / C)); i <= Math.min(nx - 1, Math.floor((s[2] + m - x0) / C)); i++)
      for (let j = Math.max(0, Math.floor((s[1] - m - z0) / C)); j <= Math.min(nz - 1, Math.floor((s[3] + m - z0) / C)); j++) g[j * nx + i] = 1;
  }
  const at = (i, j) => (i < 0 || j < 0 || i >= nx || j >= nz ? 1 : g[j * nx + i]);
  S.walk = { C, x0, z0, nx, nz, k, at, wx: (i) => x0 + (i + 0.5) * C, wz: (j) => z0 + (j + 0.5) * C };
  return S.walk;
}
// the open ground nearest a point (where he stands, or is placed): never inside anything
function openGround(S, p) {
  const { C, x0, z0, at, wx, wz } = walkGrid(S);
  const si = Math.floor((p.x - x0) / C), sj = Math.floor((p.z - z0) / C);
  if (!at(si, sj)) return p.clone();
  for (let r = 1; r < 60; r++) for (let i = si - r; i <= si + r; i++) for (let j = sj - r; j <= sj + r; j++)
    if ((Math.abs(i - si) === r || Math.abs(j - sj) === r) && !at(i, j)) return new THREE.Vector3(wx(i), p.y, wz(j));
  return p.clone();
}

// A* over a coarse walk map: everything solid is blocked with a margin; the route is then pulled taut.
function route(S, from, box) {
  const { C, x0, z0, nx, nz, k, at, wx, wz } = walkGrid(S);
  let si = Math.floor((from.x - x0) / C), sj = Math.floor((from.z - z0) / C);
  if (at(si, sj)) { outer: for (let r = 1; r < 14; r++) for (let i = si - r; i <= si + r; i++) for (let j = sj - r; j <= sj + r; j++) if (!at(i, j)) { si = i; sj = j; break outer; } }
  const reach = 2.2 * k, cx = (box.min.x + box.max.x) / 2, cz = (box.min.z + box.max.z) / 2;
  const near = (i, j) => { const x = wx(i), z = wz(j); return Math.hypot(Math.max(box.min.x - x, 0, x - box.max.x), Math.max(box.min.z - z, 0, z - box.max.z)) <= reach; };
  const hcost = (i, j) => Math.hypot(wx(i) - cx, wz(j) - cz) / C;
  const gs = new Float32Array(nx * nz).fill(Infinity), came = new Int32Array(nx * nz).fill(-1), heap = [];
  const push = (f, id) => { heap.push([f, id]); let i = heap.length - 1; while (i) { const p = (i - 1) >> 1; if (heap[p][0] <= heap[i][0]) break; [heap[p], heap[i]] = [heap[i], heap[p]]; i = p; } };
  const pop = () => { const top = heap[0], last = heap.pop(); if (heap.length) { heap[0] = last; let i = 0; for (;;) { const l = 2 * i + 1, r = l + 1; let m = i; if (l < heap.length && heap[l][0] < heap[m][0]) m = l; if (r < heap.length && heap[r][0] < heap[m][0]) m = r; if (m === i) break; [heap[m], heap[i]] = [heap[i], heap[m]]; i = m; } } return top; };
  const s0 = sj * nx + si; gs[s0] = 0; push(hcost(si, sj), s0);
  let end = -1, guard = 0, best = s0, bestH = hcost(si, sj);
  const done = new Uint8Array(nx * nz);
  while (heap.length && guard < nx * nz) {   // each cell is settled once, so the whole walk map can be searched and a far target is still found
    const [, id] = pop(); if (done[id]) continue;   // a stale entry: the cell was already settled by a shorter way
    done[id] = 1; guard++;
    const i = id % nx, j = (id / nx) | 0;
    if (near(i, j)) { end = id; break; }
    const hc = hcost(i, j); if (hc < bestH) { bestH = hc; best = id; }
    for (let di = -1; di <= 1; di++) for (let dj = -1; dj <= 1; dj++) {
      if (!di && !dj) continue; const a = i + di, b = j + dj; if (at(a, b) || (di && dj && (at(i + di, j) || at(i, j + dj)))) continue;
      const ng = gs[id] + (di && dj ? 1.414 : 1), nid = b * nx + a;
      if (ng < gs[nid]) { gs[nid] = ng; came[nid] = id; push(ng + hcost(a, b), nid); }
    }
  }
  if (end < 0) end = best;   // fenced off or unreachable: he goes as close as the open ground allows, never through anything
  const cells = []; for (let c = end; c >= 0; c = came[c]) cells.push(c); cells.reverse();
  const pts = cells.map((c) => new THREE.Vector3(wx(c % nx), 0, wz((c / nx) | 0))); pts[0] = from.clone().setY(0);
  const clear = (a, b) => { const k = Math.ceil(a.distanceTo(b) / (C * 0.5)); for (let q = 1; q < k; q++) { const t = q / k; if (at(Math.floor((a.x + (b.x - a.x) * t - x0) / C), Math.floor((a.z + (b.z - a.z) * t - z0) / C))) return false; } return true; };
  const out = [pts[0]]; let i0 = 0;
  while (i0 < pts.length - 1) { let j = pts.length - 1; while (j > i0 + 1 && !clear(pts[i0], pts[j])) j--; out.push(pts[j]); i0 = j; }
  return out;
}

function visit(item, root) {
  const S = R.S; if (!S) return;
  S.sel = item; S.say = null; S.bub?.classList.remove("on");
  if (item.open) openAsset(item.open, true);   // the details open at once, while he runs over to it
  post("/api/scene/speech", { asset_id: item.asset_id || item.label, facts: { ...(item.facts || { label: item.label }), status: item.status || "green" } })
    .then((r) => { if (S.sel === item) say(S, r.text, item); }).catch(() => { if (S.sel === item) say(S, "Looks fine from here, boss.", item); });
  const Hr = S.hero; if (!Hr) return;
  const box = item._box || new THREE.Box3().setFromObject(root);
  let path = route(S, Hr.o.position.clone().setY(0), box);
  if (!path || path.length < 2) path = [Hr.o.position.clone().setY(0), Hr.o.position.clone().setY(0)];   // already as close as he can get: he turns to it
  const segs = path.slice(1).map((p, i) => p.distanceTo(path[i])), dist = segs.reduce((a, b) => a + b, 0);
  const T = THREE.MathUtils.clamp(dist / 4.6, 1.2, 9), speed = dist / T;
  play(Hr, "run", 0.2, speed / (4.6 * (S.crew || 1)));   // a bigger crew takes longer strides
  Hr.trip = { path, segs, dist, t: 0, T, item, box };
}
function updateHero(S, dt) {
  const Hr = S.hero; if (!Hr) return;
  Hr.mixer.update(dt);
  const tr = Hr.trip; if (!tr) return;
  if (tr.t < tr.T) {
    tr.t = Math.min(tr.T, tr.t + dt);
    let d = (tr.dist * tr.t) / tr.T, i = 0; while (i < tr.segs.length - 1 && d > tr.segs[i]) { d -= tr.segs[i]; i++; }
    const a = tr.path[i], b = tr.path[i + 1], k = tr.segs[i] ? Math.min(1, d / tr.segs[i]) : 1;
    Hr.o.position.lerpVectors(a, b, k); Hr.o.position.y = S.gy(Hr.o.position.x, Hr.o.position.z);
    let dr = Math.atan2(b.x - a.x, b.z - a.z) - Hr.o.rotation.y; dr = Math.atan2(Math.sin(dr), Math.cos(dr)); Hr.o.rotation.y += dr * Math.min(1, dt * 12);
    if (tr.t >= tr.T) {
      const c = tr.box.getCenter(new THREE.Vector3()); Hr.o.rotation.y = Math.atan2(c.x - Hr.o.position.x, c.z - Hr.o.position.z);
      const st = tr.item.status;
      play(Hr, st === "red" && Hr.acts.angry_stomp ? "angry_stomp" : st === "amber" && Hr.acts.head_scratch ? "head_scratch" : Hr.acts.point_up ? "point_up" : "idle", 0.25);
      tr.since = 0;
      bubble(S, S.say ? S.say.text : "...", S.say?.item);   // he speaks once he has arrived, not on the run
    }
  } else if (tr.since != null) {
    tr.since += dt;
    if (tr.since > (tr.item.status === "amber" ? 3.2 : 2.6)) { play(Hr, "idle", 0.4); Hr.trip = null; }
  }
}
function say(S, text, item) {   // the answer is held until he has arrived
  S.say = { text, item };
  const tr = S.hero?.trip;
  if (!tr || tr.t >= tr.T) bubble(S, text, item);
}
function bubble(S, text, item) {
  if (!S.bub) { S.bub = h("div", { class: "s3-bubble" }); S.wrap.append(S.bub); }
  S.bub.innerHTML = ""; S.bub.append(h("span", {}, text));
  if (item?.open) S.bub.append(h("a", { href: "#", onclick: (e) => { e.preventDefault(); openAsset(item.open, true); } }, "Open the asset details"));
  S.bub.classList.remove("on"); void S.bub.offsetWidth; S.bub.classList.add("on");
}

// ---------------------------------------------------------------- render loop, tags, picking
function renderInk() {
  const { r, scene, camera, normalMat, nrmT, composer } = R;
  const hidden = [];
  R.world.traverse((o) => { if (o.userData.noInk && o.visible) { o.visible = false; hidden.push(o); } });
  const bg = scene.background; scene.background = null; scene.overrideMaterial = normalMat;
  r.setRenderTarget(nrmT); r.clear(); r.render(scene, camera);
  scene.overrideMaterial = null; scene.background = bg; r.setRenderTarget(null);
  hidden.forEach((o) => { o.visible = true; });
  composer.render();
}
const _v = new THREE.Vector3();
function placeTags(S) {
  const w = S.wrap.clientWidth, hh = S.wrap.clientHeight, taken = [];
  const order = S.tags.slice().sort((a, b) => b.pri - a.pri);
  for (const t of order) {
    _v.copy(t.pos).project(R.camera);
    const x = (_v.x * 0.5 + 0.5) * w, y = (-_v.y * 0.5 + 0.5) * hh;
    if (!t.w) { t.w = t.el.offsetWidth; t.h = t.el.offsetHeight; }
    const box = [x - t.w / 2 - 3, y - t.h - 3, x + t.w / 2 + 3, y + 3];
    const off = _v.z > 1 || x < -40 || x > w + 40 || y < -20 || y > hh + 40;
    const clash = taken.some((b) => !(box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]));
    t.el.style.transform = `translate(${x - t.w / 2}px, ${y - t.h}px)`;
    t.el.classList.toggle("hid", off || clash);
    if (!off && !clash) taken.push(box);
  }
  if (S.bub && S.hero?.head) {
    S.hero.head.getWorldPosition(_v); _v.y += 0.6 * (S.crew || 1); _v.project(R.camera);
    S.bub.style.left = `${(_v.x * 0.5 + 0.5) * w}px`; S.bub.style.top = `${(-_v.y * 0.5 + 0.5) * hh}px`; S.bub.style.display = _v.z < 1 ? "" : "none";
  }
}
function loop() {
  R.raf = requestAnimationFrame(loop);
  const S = R.S;
  if (!S || !R.r.domElement.isConnected) { cancelAnimationFrame(R.raf); R.raf = 0; return; }
  const dt = Math.min(0.05, R.clock.getDelta()), t = R.clock.elapsedTime;
  R.controls.update();
  const [x0, z0, x1, z1] = S.bounds, tg = R.controls.target;   // keep the view over the site
  tg.x = THREE.MathUtils.clamp(tg.x, x0 * 0.8, x1 * 0.8); tg.z = THREE.MathUtils.clamp(tg.z, z0 * 0.8, z1 * 0.8);
  R.outline.uniforms.view.value = R.camera.position.distanceTo(tg);
  R.outline.uniforms.projInv.value.copy(R.camera.projectionMatrixInverse); R.outline.uniforms.camWorld.value.copy(R.camera.matrixWorld);
  for (const f of S.anim) f(t);
  updateHero(S, dt);
  renderInk();
  placeTags(S);
}
function resize(wrap) {
  const w = wrap.clientWidth, hh = wrap.clientHeight; if (!w || !hh) return;
  R.r.setSize(w, hh, false); R.camera.aspect = w / hh; R.camera.updateProjectionMatrix();
  const pr = R.r.getPixelRatio();
  R.composer.setSize(w, hh); R.nrmT.setSize(w * pr, hh * pr); R.outline.uniforms.res.value.set(w * pr, hh * pr);
  for (const m of LINES.values()) m.resolution.set(w, hh);
}

// ---------------------------------------------------------------- the screen
let SITES = null;
export async function show(view, kind, id) {
  engine();
  view.innerHTML = "";
  const wrap = h("div", { class: "s3-stage" }), side = h("div", { class: "s3-side" }), hud = h("div", { class: "s3-hud" });
  const shell = h("div", { class: "s3" }, side, wrap);
  view.append(shell);
  wrap.append(R.r.domElement, hud);
  R.r.domElement.style.width = "100%"; R.r.domElement.style.height = "100%";
  const ld = h("div", { class: "s3-loading" }, loading("Building the site from the twin"));
  wrap.append(ld);
  if (R.ro) R.ro.disconnect();
  R.ro = new ResizeObserver(() => resize(wrap)); R.ro.observe(wrap); resize(wrap);
  R.S = null; clearWorld();
  const [d] = await Promise.all([api(`/api/scene/${kind}/${encodeURIComponent(id || "ZS-KNV")}`), SITES ? null : api("/api/scene-sites").then((s) => { SITES = s; })]);
  siteList(side, d, kind, id);
  const names = needs(d), T = {};
  (await Promise.all(names.map(model))).forEach((t, i) => { T[names[i]] = t; });
  if (!wrap.isConnected) return;
  const S = new Site(d, T, wrap);
  await ({ zone: buildZone, transmission: buildTransmission, street: buildStreet, rural: buildRural, estate: buildEstate }[d.kind] || buildZone)(S);
  // sun and shadow box sized to the site, haze beyond it
  const [x0, z0, x1, z1] = S.bounds, ext = Math.max(x1 - x0, z1 - z0) / 2 + 20, sc = R.sun.shadow.camera;
  sc.left = -ext; sc.right = ext; sc.top = ext; sc.bottom = -ext; sc.near = 1; sc.far = 600; sc.updateProjectionMatrix();
  R.sun.position.set((x0 + x1) / 2 - 120, 118, (z0 + z1) / 2 + 95);   // a lowish afternoon sun: long, graphic shadows
  R.sun.target.position.set((x0 + x1) / 2, 0, (z0 + z1) / 2);
  [R.scene.fog.near, R.scene.fog.far] = S.fog || [ext * 0.85, ext * 3.1];
  EDGE.c.value.set(S.edge[0], S.edge[1]); EDGE.r.value.set(S.edge[2], S.edge[3]);
  const v = S.view, c = R.camera;
  R.controls.target.copy(v.target);
  c.position.set(v.target.x + v.dist * Math.sin(v.phi) * Math.cos(v.theta), v.target.y + v.dist * Math.cos(v.phi), v.target.z + v.dist * Math.sin(v.phi) * Math.sin(v.theta));
  c.lookAt(v.target); R.controls.maxDistance = v.max || v.dist * 2.0; R.controls.update();
  hudChips(hud, d);
  await addHero(S);
  if (!wrap.isConnected) return;
  ld.remove();
  R.S = S;
  picking(S);
  if (!R.raf) { R.clock.getDelta(); loop(); }
}

function picking(S) {
  const el = R.r.domElement, ray = new THREE.Raycaster(), m = new THREE.Vector2();
  ray.layers.set(1);
  let down = null;
  const pick = (e) => { const b = el.getBoundingClientRect(); m.set(((e.clientX - b.left) / b.width) * 2 - 1, -((e.clientY - b.top) / b.height) * 2 + 1); ray.setFromCamera(m, R.camera); return ray.intersectObjects(S.picks, false)[0]?.object; };
  el.onpointerdown = (e) => { down = [e.clientX, e.clientY]; };
  el.onpointermove = (e) => { el.style.cursor = pick(e) ? "pointer" : "grab"; };
  el.onclick = (e) => {
    if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return;   // that was a drag
    const o = pick(e);
    if (o) visit(o.userData.item, o.userData.root); else if (S.bub) { S.bub.classList.remove("on"); S.sel = null; }
  };
}

function hudChips(hud, d) {
  hud.innerHTML = "";
  const chip = (t, cls = "") => h("span", { class: `chip3 ${cls}` }, t);
  const out = [chip(d.title, "ttl")];
  if (d.kind === "street" && d.customers?.ds_id) out.push(chip(`${d.customers.ds_id} supplies ${n0(d.customers.n)} ${d.customers.n === 1 ? "customer" : "customers"}, ${n0(d.customers.solar)} with rooftop solar`));
  if (d.kind === "estate" && d.kiosks?.length) out.push(chip(`${d.kiosks.length} ${d.kiosks.length === 1 ? "padmount substation supplies" : "padmount substations supply"} ${n0(d.customers.n)} ${d.customers.n === 1 ? "customer" : "customers"}, ${n0(d.customers.solar)} with rooftop solar`));
  if (d.kind === "zone") out.push(chip(`${d.transformers.length} ${d.transformers.length === 1 ? "transformer" : "transformers"}, ${d.feeders.length} ${d.feeders.length === 1 ? "feeder" : "feeders"}`));
  if (d.kind === "transmission") {
    const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
    out.push(chip(`${plural(d.transformers.length, "transformer")}, ${plural(d.circuits.length, "outgoing circuit")}, ${plural(d.incoming.length, "incoming 132 kV circuit")}`));
  }
  if (d.kind === "rural") out.push(chip(`Fire danger today: ${d.fire_rating_label}${d.fire_district ? ` (${d.fire_district})` : ""}`, { EXTREME: "red", CATASTROPHIC: "red", HIGH: "amber" }[d.fire_rating] || ""));
  if (d.outage) out.push(chip(`Fault from ${d.outage.cause_label}: ${n0(d.outage.customers_off)} ${d.outage.customers_off === 1 ? "customer" : "customers"} off supply`, "red"));
  const tripped = (d.feeders || []).filter((f) => f.tripped);
  if (tripped.length) out.push(chip(`Tripped now: ${tripped.map((f) => f.asset_id).join(", ")}`, "red"));
  hud.append(...out, h("span", { style: "flex:1" }), h("a", { class: "btn small", href: "#/map" }, "Back to the map"));
}

function siteList(side, d, kind, id) {
  side.innerHTML = "";
  const zsOf = kind === "zone" ? id : (SITES || []).find((z) => z.feeders.some((f) => f.feeder_id === id))?.zs_id;
  const dot = (s) => h("i", { class: "d", style: `background:${HEX(COL[s] || COL.green)}` });
  for (const z of SITES || []) {
    if (z.kind === "transmission") {   // a transmission substation, then the zone substations it feeds
      const open = kind === "transmission" && id === z.ts_id;
      const zoneDot = (zid) => { const zi = (SITES || []).find((x) => x.kind === "zone" && x.zs_id === zid); return dot(zi?.feeders?.some((f) => f.status === "red") ? "amber" : "green"); };
      const sec = h("div", { class: `s3-grp ${open ? "open" : ""}` },
        h("button", { class: "s3-gh", onclick: () => sec.classList.toggle("open") }, h("span", {}, z.name.replace(/ transmission substation$/i, "")), h("small", {}, `${z.zones.length} zone substation${z.zones.length === 1 ? "" : "s"}`)),
        h("div", { class: "s3-items" },
          h("a", { href: `#/scene/transmission/${z.ts_id}`, class: open ? "on" : "" }, h("span", {}, "Transmission substation", h("small", {}, z.name)), dot(z.status)),
          ...z.zones.map((zid) => h("a", { href: `#/scene/zone/${zid}`, class: kind === "zone" && id === zid ? "on" : "" },
            h("span", {}, "Zone substation", h("small", {}, (SITES || []).find((x) => x.zs_id === zid)?.name || zid)), zoneDot(zid)))));
      side.append(sec);
      continue;
    }
    const open = z.zs_id === zsOf;
    const sec = h("div", { class: `s3-grp ${open ? "open" : ""}` },
      h("button", { class: "s3-gh", onclick: () => sec.classList.toggle("open") }, h("span", {}, z.name.replace(/ zone substation$/i, "")), h("small", {}, `${z.feeders.length} feeder${z.feeders.length === 1 ? "" : "s"}`)),
      h("div", { class: "s3-items" },
        h("a", { href: `#/scene/zone/${z.zs_id}`, class: kind === "zone" && id === z.zs_id ? "on" : "" }, h("span", {}, "Zone substation", h("small", {}, z.name)), dot(z.feeders.some((f) => f.status === "red") ? "amber" : "green")),
        ...z.feeders.map((f) => h("a", { href: `#/scene/feeder/${f.feeder_id}`, class: id === f.feeder_id ? "on" : "" }, h("span", {}, f.feeder_id, h("small", {}, f.scene)), dot(f.status)))));
    side.append(sec);
  }
  setTimeout(() => side.querySelector("a.on")?.scrollIntoView({ block: "center" }), 0);
}
