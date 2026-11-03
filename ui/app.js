/* raymap — H3 hex mesh over the US, coloured by reported status.
 *
 * Six display states:
 *   detected_corroborated  solid red    >=2 devices reported a detection
 *   detected_single        hollow red   1 device (unconfirmed)
 *   clear_multi            solid green  >=2 devices, no detections
 *   clear_single           hollow green 1 device, no detections
 *   no_data                grey         no reports here — NOT evidence of safety
 *
 * The mesh covers land at the current resolution; the API only returns cells
 * that have reports. Resolution follows zoom (r3 national .. r6 street); r6 is
 * the minimum cell size (~7.4 km) regardless of how far the user zooms in.
 */
const API_BASE = window.RAYMAP_API || "";
const geo = window.raymapGeo;

const MAX_MESH_CELLS = 8000;
const HEX_AREA = { 3: 12393, 4: 1770, 5: 253, 6: 36.1, 7: 5.16, 8: 0.737 };
const US_BBOX = [-125.5, 23.5, -65.5, 50.5];
const RED = "#e0393e";
const GREEN = "#2e9e4f";
const GREY = "#9aa3af";

const STATUS_LABEL = {
  detected_corroborated: "possible interference (2+ devices)",
  detected_single: "possible interference (1 device — unconfirmed)",
  clear_multi: "no detections (2+ devices)",
  clear_single: "no detections (1 device — low confidence)",
  no_data: "no reports here — not evidence of safety",
};

const map = new maplibregl.Map({
  container: "map",
  style: "https://tiles.openfreemap.org/styles/liberty",
  center: [-98.5, 39.5],
  zoom: 5,
  minZoom: 4,
  maxZoom: 14,
  attributionControl: false,
});

map.addControl(new maplibregl.AttributionControl({
  compact: true,
  customAttribution: '© <a href="https://openfreemap.org">OpenFreeMap</a> · © <a href="https://openmaptiles.org">OpenMapTiles</a> · © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}), "bottom-right");
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

const daysEl = document.getElementById("days");
const detOnlyEl = document.getElementById("detections-only");
const refreshBtn = document.getElementById("refresh");
const statusEl = document.getElementById("status");
const resBadge = document.getElementById("res-badge");

let states = [];              // [{feature, bbox}]
let dataByCell = new Map();   // h3 -> properties
let meshCells = [];           // h3 ids for the current view
let meshBBox = null;
let currentRes = null;
const boundaryCache = new Map();

// --- geometry / mesh -------------------------------------------------------
function currentBBox() {
  const b = map.getBounds();
  return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
}

function clampToUS(box) {
  return [
    Math.max(box[0], US_BBOX[0]), Math.max(box[1], US_BBOX[1]),
    Math.min(box[2], US_BBOX[2]), Math.min(box[3], US_BBOX[3]),
  ];
}

function estimateCells(box, res) {
  const midLat = (box[1] + box[3]) / 2;
  const w = (box[2] - box[0]) * 111.32 * Math.max(0.1, Math.cos((midLat * Math.PI) / 180));
  const h = (box[3] - box[1]) * 111.32;
  return (w * h) / HEX_AREA[res];
}

function isOnLand(lat, lng) {
  for (const s of states) {
    const [w, so, e, n] = s.bbox;
    if (lng < w || lng > e || lat < so || lat > n) continue;
    if (geo.pointInFeature(lng, lat, s.feature)) return true;
  }
  return false;
}

function buildMesh(res) {
  const box = clampToUS(geo.padBbox(currentBBox(), 0.2));
  if (box[0] >= box[2] || box[1] >= box[3]) return { cells: 0 };
  // Estimate before polyfilling: a continental bbox at r6 is ~220k cells.
  if (estimateCells(box, res) > MAX_MESH_CELLS) return { tooMany: true };
  const cells = h3.polygonToCells([geo.bboxPolygonRing(box)], res, true);
  const onLand = cells.filter((c) => {
    const [lat, lng] = h3.cellToLatLng(c);
    return isOnLand(lat, lng);
  });
  if (onLand.length > MAX_MESH_CELLS) return { tooMany: true };
  meshCells = onLand;
  meshBBox = box;
  return { cells: onLand.length };
}

function boundary(cell) {
  let ring = boundaryCache.get(cell);
  if (!ring) {
    ring = h3.cellToBoundary(cell, true);
    ring.push(ring[0]);
    boundaryCache.set(cell, ring);
  }
  return ring;
}

function featureFor(cell, props) {
  const properties = props || {
    h3: cell, res: currentRes, status: "no_data", confidence: "none",
    device_count: 0, report_count: 0, detection_count: 0,
    detection_device_count: 0, day: null, last_ts: null,
  };
  return {
    type: "Feature",
    properties,
    geometry: { type: "Polygon", coordinates: [boundary(cell)] },
  };
}

function render() {
  const features = [];
  const seen = new Set();
  for (const cell of meshCells) {
    features.push(featureFor(cell, dataByCell.get(cell)));
    seen.add(cell);
  }
  // Never hide a reported cell because the mesh missed it (e.g. coastlines),
  // but keep the extra polygons inside the viewport.
  for (const [cell, props] of dataByCell) {
    if (seen.has(cell)) continue;
    const [lat, lng] = h3.cellToLatLng(cell);
    if (meshBBox && (lng < meshBBox[0] || lng > meshBBox[2] || lat < meshBBox[1] || lat > meshBBox[3])) continue;
    features.push(featureFor(cell, props));
  }
  map.getSource("hexes").setData({ type: "FeatureCollection", features });
  updateStatus(features.length);
}

function updateStatus(shown) {
  const reported = dataByCell.size;
  statusEl.textContent = reported === 0
    ? "no reports in this window — not evidence of safety"
    : `${shown} cells · ${reported} with reports`;
  if (resBadge) resBadge.textContent = `hex ≈ ${geo.RES_KM[currentRes]} km`;
}

// --- data ------------------------------------------------------------------
async function loadStates() {
  const resp = await fetch("data/us_states.geojson");
  if (!resp.ok) throw new Error(`states HTTP ${resp.status}`);
  const gj = await resp.json();
  states = gj.features.map((f) => ({ feature: f, bbox: geo.bboxOfFeature(f) }));
}

async function loadData(res) {
  const days = daysEl.value;
  const detOnly = detOnlyEl.checked ? "&include_detections_only=true" : "";
  // Only the viewport is needed; at r6 the national set is tens of thousands
  // of cells. Pad so panning does not refetch immediately.
  const box = clampToUS(geo.padBbox(currentBBox(), 0.2));
  const url = `${API_BASE}/geojson?days=${days}&res=${res}&bbox=${box.join(",")}${detOnly}`;
  statusEl.textContent = "loading…";
  statusEl.classList.remove("err");
  try {
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const fc = await resp.json();
    dataByCell = new Map(fc.features.map((f) => [f.properties.h3, f.properties]));
    currentRes = fc.res;
  } catch (err) {
    statusEl.textContent = `error: ${err.message}`;
    statusEl.classList.add("err");
  }
}

function rebuild() {
  let res = currentRes;
  let result = buildMesh(res);
  // Pathological viewport (or a very wide zoom): step down a level.
  while (result.tooMany && res > geo.RES_MIN) {
    res -= 1;
    result = buildMesh(res);
  }
  render();
  return res;
}

async function refreshView({ force = false } = {}) {
  const res = geo.resForZoom(map.getZoom());
  if (force || res !== currentRes) {
    await loadData(res);
    rebuild();
  } else if (!meshBBox || !geo.bboxContains(meshBBox, currentBBox())) {
    rebuild();
  }
}

// --- layers ----------------------------------------------------------------
function addLayers() {
  map.addSource("hexes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

  map.addLayer({
    id: "hex-fill",
    type: "fill",
    source: "hexes",
    paint: {
      "fill-color": [
        "match", ["get", "status"],
        "detected_corroborated", RED,
        "detected_single", RED,
        "clear_multi", GREEN,
        "clear_single", GREEN,
        GREY,
      ],
      "fill-opacity": [
        "match", ["get", "status"],
        "detected_corroborated", 0.55,
        "detected_single", 0.10,
        "clear_multi", 0.50,
        "clear_single", 0.10,
        // no_data: visible enough to read as a mesh, light enough not to
        // drown the red/green signal or hide basemap labels.
        0.22,
      ],
    },
  });

  map.addLayer({
    id: "hex-line",
    type: "line",
    source: "hexes",
    paint: {
      "line-color": [
        "match", ["get", "status"],
        "detected_corroborated", RED,
        "detected_single", RED,
        "clear_multi", GREEN,
        "clear_single", GREEN,
        GREY,
      ],
      "line-width": [
        "match", ["get", "status"],
        "detected_single", 2.2,
        "clear_single", 2.2,
        "no_data", 0.8,
        1.2,
      ],
      "line-opacity": ["match", ["get", "status"], "no_data", 0.6, 0.9],
    },
  });

  const popup = new maplibregl.Popup({ closeButton: true, maxWidth: "300px" });
  map.on("click", "hex-fill", (e) => {
    const p = e.features[0].properties;
    let html = `<strong>${STATUS_LABEL[p.status] || p.status}</strong>`;
    if (p.status === "no_data") {
      html += "Cell has no reports in the selected window. Grey does <em>not</em> mean safe.";
    } else {
      html +=
        `reports: ${p.report_count} · devices: ${p.device_count}<br>` +
        `detections: ${p.detection_count} · detecting devices: ${p.detection_device_count}<br>` +
        `day: ${p.day}` +
        (p.last_ts ? `<br>last report: ${new Date(p.last_ts).toLocaleString()}` : "");
      if (p.confidence === "single") html += "<br><em>Single device — not corroborated.</em>";
    }
    html += `<br><span class="mono">${p.h3}</span>`;
    popup.setLngLat(e.lngLat).setHTML(html).addTo(map);
  });
  map.on("mousemove", "hex-fill", (e) => {
    map.getCanvas().style.cursor = e.features.length ? "pointer" : "";
  });
}

// --- boot ------------------------------------------------------------------
async function init() {
  addLayers();
  try {
    await loadStates();
  } catch (err) {
    statusEl.textContent = `error loading boundaries: ${err.message}`;
    statusEl.classList.add("err");
    return;
  }
  await refreshView({ force: true });
}

map.on("load", init);
map.on("moveend", () => refreshView());
refreshBtn.addEventListener("click", () => refreshView({ force: true }));
daysEl.addEventListener("change", () => refreshView({ force: true }));
detOnlyEl.addEventListener("change", () => refreshView({ force: true }));

// Keep the current view fresh; the API reads only aggregates.
setInterval(() => {
  if (currentRes !== null) {
    loadData(currentRes).then(render);
  }
}, 60000);
