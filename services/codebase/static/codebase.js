// 3D codebase map: three.js scene + squarified-treemap layout per repo.
//
// Each repo becomes a tile on the ground plane. Files within a repo are
// packed via squarified treemap (Bruls/Huijing/van Wijk 2000) weighted by
// sqrt(loc+1) so even zero-loc files have a visible footprint. Each file
// is a box; height is log-scaled by loc, colour is mtime recency.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

// ---------------------------------------------------------------------------
// Scene bootstrap.
// ---------------------------------------------------------------------------
const canvas = document.getElementById("scene");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setClearColor(0x06080d, 1.0);

const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0x06080d, 200, 1400);

const camera = new THREE.PerspectiveCamera(
  55, window.innerWidth / window.innerHeight, 0.5, 4000
);
camera.position.set(380, 240, 380);
camera.lookAt(0, 0, 0);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = Math.PI * 0.49; // don't dip below ground
controls.minDistance = 60;
controls.maxDistance = 1800;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.35;

scene.add(new THREE.AmbientLight(0x99aacc, 0.55));
const sun = new THREE.DirectionalLight(0xffe6c0, 1.05);
sun.position.set(400, 700, 250);
scene.add(sun);
const fill = new THREE.DirectionalLight(0x4477ff, 0.35);
fill.position.set(-300, 200, -400);
scene.add(fill);

// Ground.
const ground = new THREE.Mesh(
  new THREE.PlaneGeometry(3000, 3000),
  new THREE.MeshStandardMaterial({ color: 0x0c1218, roughness: 0.95, metalness: 0.1 })
);
ground.rotation.x = -Math.PI / 2;
ground.position.y = -0.05;
scene.add(ground);

// Subtle grid.
const grid = new THREE.GridHelper(3000, 60, 0x16202c, 0x10161e);
grid.position.y = 0;
scene.add(grid);

window.addEventListener("resize", () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// ---------------------------------------------------------------------------
// Squarified treemap.
//
// Standard Bruls/Huijing/van Wijk algorithm. Input: list of {weight, ...}
// sorted descending. Output: same items mutated with {x, y, w, h} where
// (x,y) is the rectangle's top-left and (w,h) its size, all in the
// rectangle space (0..rectW, 0..rectH).
// ---------------------------------------------------------------------------
function squarify(items, rect) {
  const work = items.slice();
  const totalW = work.reduce((s, it) => s + it.weight, 0) || 1;
  // Scale weights to area of rect.
  const area = rect.w * rect.h;
  const scaleW = area / totalW;
  for (const it of work) it._a = it.weight * scaleW;
  layoutRow(work, { ...rect });

  function layoutRow(remaining, r) {
    if (remaining.length === 0) return;
    let row = [];
    let bestRatio = Infinity;
    let i = 0;
    while (i < remaining.length) {
      const trial = row.concat([remaining[i]]);
      const ratio = worstRatio(trial, Math.min(r.w, r.h));
      if (ratio > bestRatio && row.length > 0) break;
      row = trial;
      bestRatio = ratio;
      i += 1;
    }
    placeRow(row, r);
    const consumed = row.reduce((s, it) => s + it._a, 0);
    const newR = shrink(r, consumed);
    layoutRow(remaining.slice(row.length), newR);
  }

  function worstRatio(row, side) {
    const rowSum = row.reduce((s, it) => s + it._a, 0);
    if (rowSum === 0 || side === 0) return Infinity;
    let max = 0, min = Infinity;
    for (const it of row) {
      if (it._a > max) max = it._a;
      if (it._a < min) min = it._a;
    }
    const sq = side * side;
    return Math.max((sq * max) / (rowSum * rowSum), (rowSum * rowSum) / (sq * min));
  }

  function placeRow(row, r) {
    const rowSum = row.reduce((s, it) => s + it._a, 0);
    if (rowSum === 0) return;
    const horizontal = r.w >= r.h;
    if (horizontal) {
      const rowH = rowSum / r.w;
      let cx = r.x;
      for (const it of row) {
        const itW = it._a / rowH;
        it.x = cx; it.y = r.y; it.w = itW; it.h = rowH;
        cx += itW;
      }
    } else {
      const rowW = rowSum / r.h;
      let cy = r.y;
      for (const it of row) {
        const itH = it._a / rowW;
        it.x = r.x; it.y = cy; it.w = rowW; it.h = itH;
        cy += itH;
      }
    }
  }

  function shrink(r, consumed) {
    const horizontal = r.w >= r.h;
    if (horizontal) {
      const taken = consumed / r.w;
      return { x: r.x, y: r.y + taken, w: r.w, h: r.h - taken };
    } else {
      const taken = consumed / r.h;
      return { x: r.x + taken, y: r.y, w: r.w - taken, h: r.h };
    }
  }
}

// ---------------------------------------------------------------------------
// Colour: file mtime → THREE.Color via piecewise gradient.
// ---------------------------------------------------------------------------
const COLOR_STOPS = [
  { age: 0,        color: new THREE.Color(0xff7a3d) }, // < 1 day → orange
  { age: 86400,    color: new THREE.Color(0xffa84a) },
  { age: 7 * 86400,  color: new THREE.Color(0xd29922) }, // ~1 week → yellow
  { age: 30 * 86400, color: new THREE.Color(0x58a6ff) }, // ~1 month → blue
  { age: 180 * 86400, color: new THREE.Color(0x2a4365) },
  { age: 365 * 86400 * 2, color: new THREE.Color(0x1a2238) },
];

function colorForMtime(mtime, nowSec) {
  const age = Math.max(0, nowSec - mtime);
  for (let i = 0; i < COLOR_STOPS.length - 1; i++) {
    const a = COLOR_STOPS[i], b = COLOR_STOPS[i + 1];
    if (age <= b.age) {
      const t = (age - a.age) / (b.age - a.age);
      return a.color.clone().lerp(b.color, Math.max(0, Math.min(1, t)));
    }
  }
  return COLOR_STOPS[COLOR_STOPS.length - 1].color;
}

// ---------------------------------------------------------------------------
// Scene state for the rendered city.
// ---------------------------------------------------------------------------
const REPO_TILE_SIZE = 220;
const REPO_GAP = 30;
const HEIGHT_SCALE = 8.0;        // world units per log(loc)
const MIN_HEIGHT = 0.6;
const MAX_FOOTPRINT = REPO_TILE_SIZE; // safety clamp

let cityGroup = null;
let pickables = []; // {mesh, file, repo}

const repoLabels = []; // {el, worldPos}

function buildCity(payload) {
  if (cityGroup) {
    scene.remove(cityGroup);
    disposeGroup(cityGroup);
    cityGroup = null;
    pickables = [];
  }
  for (const lbl of repoLabels) lbl.el.remove();
  repoLabels.length = 0;

  cityGroup = new THREE.Group();
  scene.add(cityGroup);

  const repos = payload.repos || [];
  const nowSec = payload.now || (Date.now() / 1000);
  const totalWidth = repos.length * REPO_TILE_SIZE + Math.max(0, (repos.length - 1)) * REPO_GAP;

  let cursorX = -totalWidth / 2;
  let totalFiles = 0;
  let totalLoc = 0;

  for (const repo of repos) {
    const tileX = cursorX + REPO_TILE_SIZE / 2;
    const tileZ = 0;

    // Tile platform (subtle base under each repo).
    const plat = new THREE.Mesh(
      new THREE.BoxGeometry(REPO_TILE_SIZE + 8, 0.4, REPO_TILE_SIZE + 8),
      new THREE.MeshStandardMaterial({ color: 0x101820, roughness: 0.95, metalness: 0.05 })
    );
    plat.position.set(tileX, 0.2, tileZ);
    cityGroup.add(plat);

    // Layout files via squarified treemap.
    const files = (repo.files || []).map(f => ({
      ...f,
      weight: Math.sqrt((f.loc || 0) + 1.0),
    }));
    if (files.length > 0) {
      // Squarify mutates with x,y,w,h relative to (0,0)..(TILE,TILE).
      squarify(files, { x: 0, y: 0, w: REPO_TILE_SIZE, h: REPO_TILE_SIZE });
    }

    for (const f of files) {
      if (!f.w || !f.h) continue;
      const w = Math.max(0.4, Math.min(MAX_FOOTPRINT, f.w * 0.96)); // 4% margin
      const d = Math.max(0.4, Math.min(MAX_FOOTPRINT, f.h * 0.96));
      const h = Math.max(MIN_HEIGHT, Math.log10((f.loc || 0) + 1.0) * HEIGHT_SCALE);

      const geo = new THREE.BoxGeometry(w, h, d);
      const color = colorForMtime(f.mtime || 0, nowSec);
      const mat = new THREE.MeshStandardMaterial({
        color, roughness: 0.55, metalness: 0.15,
        emissive: color.clone().multiplyScalar(0.05),
      });
      const mesh = new THREE.Mesh(geo, mat);
      // (f.x, f.y) is top-left in tile space; centre = +w/2,+d/2.
      const cx = (f.x + f.w / 2) - REPO_TILE_SIZE / 2 + tileX;
      const cz = (f.y + f.h / 2) - REPO_TILE_SIZE / 2 + tileZ;
      mesh.position.set(cx, h / 2 + 0.4, cz);
      cityGroup.add(mesh);
      pickables.push({ mesh, file: f, repo: repo.name });

      totalFiles += 1;
      totalLoc += f.loc || 0;
    }

    // Floating label per repo (HTML overlay; positioned each frame).
    const label = document.createElement("div");
    label.className = "repo-label";
    label.textContent = `${repo.name} · ${repo.file_count} files`;
    document.body.appendChild(label);
    repoLabels.push({
      el: label,
      worldPos: new THREE.Vector3(tileX, 38, tileZ - REPO_TILE_SIZE / 2 - 6),
    });

    cursorX += REPO_TILE_SIZE + REPO_GAP;
  }

  // Update HUD totals.
  const tot = document.getElementById("totals");
  if (tot) {
    tot.innerHTML = "";
    for (const repo of repos) {
      const div = document.createElement("div");
      div.textContent = `${repo.name}: ${repo.file_count} files`;
      tot.appendChild(div);
    }
    const div = document.createElement("div");
    div.className = "muted";
    div.textContent = `total: ${totalFiles} files · ${totalLoc.toLocaleString()} loc`;
    tot.appendChild(div);
  }
}

function disposeGroup(group) {
  group.traverse((obj) => {
    if (obj.geometry) obj.geometry.dispose();
    if (obj.material) {
      if (Array.isArray(obj.material)) obj.material.forEach(m => m.dispose());
      else obj.material.dispose();
    }
  });
}

// ---------------------------------------------------------------------------
// Picking + panel.
// ---------------------------------------------------------------------------
const raycaster = new THREE.Raycaster();
const mouseNDC = new THREE.Vector2();
let hovered = null;
let hoveredOriginalEmissive = null;

canvas.addEventListener("pointermove", (ev) => {
  const r = canvas.getBoundingClientRect();
  mouseNDC.x = ((ev.clientX - r.left) / r.width) * 2 - 1;
  mouseNDC.y = -((ev.clientY - r.top) / r.height) * 2 + 1;
});

canvas.addEventListener("click", () => {
  const hit = pickFirst();
  if (!hit) return;
  showPanel(hit.file, hit.repo);
});

function pickFirst() {
  if (!pickables.length) return null;
  raycaster.setFromCamera(mouseNDC, camera);
  const meshes = pickables.map(p => p.mesh);
  const hits = raycaster.intersectObjects(meshes, false);
  if (hits.length === 0) return null;
  const m = hits[0].object;
  return pickables.find(p => p.mesh === m) || null;
}

function showPanel(file, repo) {
  document.getElementById("panel").classList.remove("hidden");
  document.getElementById("panel-path").textContent = file.path;
  document.getElementById("panel-repo").textContent = repo;
  document.getElementById("panel-loc").textContent = (file.loc || 0).toLocaleString();
  document.getElementById("panel-size").textContent = humanSize(file.size || 0);
  document.getElementById("panel-mtime").textContent = file.mtime
    ? new Date(file.mtime * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC"
    : "—";
  document.getElementById("panel-ext").textContent = file.ext || "—";
}

document.getElementById("panel-close").addEventListener("click", () => {
  document.getElementById("panel").classList.add("hidden");
});

function humanSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MiB`;
}

// ---------------------------------------------------------------------------
// HUD wiring.
// ---------------------------------------------------------------------------
document.getElementById("toggle-rotate").addEventListener("change", (ev) => {
  controls.autoRotate = ev.target.checked;
});
document.getElementById("toggle-labels").addEventListener("change", (ev) => {
  for (const lbl of repoLabels) lbl.el.style.display = ev.target.checked ? "" : "none";
});
document.getElementById("refresh").addEventListener("click", async () => {
  document.getElementById("status").textContent = "scanning…";
  try {
    const r = await fetch("/api/codebase/refresh", { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    await loadCity();
  } catch (err) {
    document.getElementById("status").textContent = "scan failed: " + err.message;
  }
});

// ---------------------------------------------------------------------------
// Data load.
// ---------------------------------------------------------------------------
async function loadCity() {
  document.getElementById("status").textContent = "loading…";
  let r;
  try {
    r = await fetch("/api/codebase/snapshot");
  } catch (err) {
    document.getElementById("status").textContent = "fetch failed: " + err.message;
    return;
  }
  if (!r.ok) {
    document.getElementById("status").textContent = `error ${r.status}`;
    return;
  }
  const data = await r.json();
  buildCity(data);
  const ts = new Date((data.scanned_at || 0) * 1000).toLocaleTimeString();
  document.getElementById("status").textContent = `scanned ${ts}`;
}

// ---------------------------------------------------------------------------
// Render loop.
// ---------------------------------------------------------------------------
const tmpV = new THREE.Vector3();
function tick() {
  controls.update();

  // Hover highlight.
  const hit = pickFirst();
  if (hovered && hit?.mesh !== hovered) {
    hovered.material.emissive.copy(hoveredOriginalEmissive);
    hovered = null;
  }
  if (hit && hit.mesh !== hovered) {
    hovered = hit.mesh;
    hoveredOriginalEmissive = hovered.material.emissive.clone();
    hovered.material.emissive.setHex(0x9fc8ff);
  }

  // Project repo labels to screen space.
  const w = window.innerWidth, h = window.innerHeight;
  for (const lbl of repoLabels) {
    tmpV.copy(lbl.worldPos).project(camera);
    const sx = (tmpV.x * 0.5 + 0.5) * w;
    const sy = (-tmpV.y * 0.5 + 0.5) * h;
    const inFront = tmpV.z < 1.0;
    lbl.el.style.left = `${sx}px`;
    lbl.el.style.top = `${sy}px`;
    lbl.el.style.opacity = inFront ? "1" : "0";
  }

  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}

tick();
loadCity();
