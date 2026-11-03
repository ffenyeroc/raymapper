/* raymap — MapLibre GL map rendering aggregated GeoJSON from the raymap API.
 *
 * Five display states (see app_api.classify):
 *   detected_corroborated  solid red    >=2 devices reported a detection
 *   detected_single        hollow red   1 device reported a detection (unconfirmed)
 *   clear_multi            solid green  >=2 devices, no detections
 *   clear_single           hollow green 1 device, no detections
 *   (no marker)            no reports in the window — NOT evidence of safety
 */
const API_BASE = window.RAYMAP_API || "http://localhost:8000";

const DETECTED = ["in", ["get", "status"], ["literal", ["detected_corroborated", "detected_single"]]];
const SINGLE = ["==", ["get", "confidence"], "single"];

const STATUS_LABEL = {
  detected_corroborated: "possible interference (2+ devices)",
  detected_single: "possible interference (1 device — unconfirmed)",
  clear_multi: "no detections (2+ devices)",
  clear_single: "no detections (1 device — low confidence)",
};

const map = new maplibregl.Map({
  container: "map",
  style: "https://tiles.openfreemap.org/styles/liberty", // free vector basemap, no key
  center: [-98.5, 39.5], // continental US
  zoom: 3.5,
  attributionControl: false,
});

// OpenFreeMap / OpenMapTiles / OpenStreetMap attribution is required (ODbL).
map.addControl(new maplibregl.AttributionControl({
  compact: true,
  customAttribution: '© <a href="https://openfreemap.org">OpenFreeMap</a> · © <a href="https://openmaptiles.org">OpenMapTiles</a> · © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}), "bottom-right");

const daysEl = document.getElementById("days");
const detOnlyEl = document.getElementById("detections-only");
const refreshBtn = document.getElementById("refresh");
const statusEl = document.getElementById("status");

async function loadData() {
  const days = daysEl.value;
  const detOnly = detOnlyEl.checked ? "&include_detections_only=true" : "";
  const url = `${API_BASE}/geojson?days=${days}${detOnly}`;
  statusEl.textContent = "loading…";
  statusEl.classList.remove("err");
  try {
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const fc = await resp.json();
    const count = fc.features ? fc.features.length : 0;
    updateSource(fc);
    // Absence of markers means absence of reports, not absence of interference.
    statusEl.textContent = count === 0
      ? "no reports in this window — not evidence of safety"
      : `${count} cell${count === 1 ? "" : "s"}`;
  } catch (err) {
    statusEl.textContent = `error: ${err.message}`;
    statusEl.classList.add("err");
  }
}

function updateSource(fc) {
  const src = map.getSource("cells");
  if (src) {
    src.setData(fc);
    const halo = map.getSource("halo");
    if (halo) halo.setData(fc);
    return;
  }

  // --- Main (clusters + cells) source -----------------------------------
  map.addSource("cells", {
    type: "geojson",
    data: fc,
    cluster: true,
    clusterMaxZoom: 6, // uncluster at state/regional zoom so dots appear
    clusterRadius: 40,
    clusterProperties: {
      // A cluster is marked detected if any child has a detection.
      any_detected: ["max", ["case", [">", ["get", "detection_device_count"], 0], 1, 0]],
      max_det_devices: ["max", ["get", "detection_device_count"]],
    },
  });

  // --- Halos only for corroborated detections (never amplify 1-device claims)
  map.addSource("halo", { type: "geojson", data: fc, cluster: false });
  map.addLayer({
    id: "halo-red",
    type: "heatmap",
    source: "halo",
    filter: ["all", [">", ["get", "detection_device_count"], 1], ["==", ["get", "confidence"], "multi"]],
    paint: {
      "heatmap-weight": 1,
      "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 4, 16, 6, 34, 8, 70, 10, 130, 12, 240],
      "heatmap-opacity": 0.6,
      "heatmap-color": [
        "interpolate", ["linear"], ["heatmap-density"],
        0.0, "rgba(224,57,62,0)",
        0.4, "rgba(224,57,62,0.22)",
        1.0, "rgba(224,57,62,0.55)",
      ],
    },
  });

  // Clustered circles (on top of the halos).
  map.addLayer({
    id: "clusters",
    type: "circle",
    source: "cells",
    filter: ["has", "point_count"],
    paint: {
      "circle-color": ["case", ["==", ["get", "any_detected"], 1], "#e0393e", "#2e7d32"],
      "circle-radius": ["step", ["get", "point_count"], 14, 20, 18, 100, 24],
      "circle-opacity": 0.85,
    },
  });
  map.addLayer({
    id: "cluster-count",
    type: "symbol",
    source: "cells",
    filter: ["has", "point_count"],
    layout: { "text-field": ["get", "point_count"], "text-size": 12 },
    paint: { "text-color": "#ffffff" },
  });

  // Individual cells: solid = corroborated, hollow = single device.
  map.addLayer({
    id: "cells",
    type: "circle",
    source: "cells",
    filter: ["!", ["has", "point_count"]],
    paint: {
      "circle-color": ["case", DETECTED, "#e0393e", "#2e9e4f"],
      "circle-opacity": ["case", SINGLE, 0.10, 0.5],
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 3, 4, 4, 5, 6, 6, 9, 8, 16, 11, 33],
      "circle-stroke-width": ["case", SINGLE, 2.5, 1.5],
      "circle-stroke-color": ["case", DETECTED, "#e0393e", "#2e9e4f"],
      "circle-stroke-opacity": ["case", SINGLE, 1.0, 0.5],
    },
  });

  map.on("click", "cells", (e) => {
    const p = e.features[0].properties;
    const label = STATUS_LABEL[p.status] || p.status;
    const stale = p.last_ts ? `last report: ${new Date(p.last_ts).toLocaleString()}` : "";
    showPopup(
      `<strong>${label}</strong>` +
        `cell ${p.cell} · day ${p.day}<br>` +
        `reports: ${p.report_count} · devices: ${p.device_count} · ` +
        `detections: ${p.detection_count}` +
        (p.confidence === "single" ? `<br><em>Single device — not corroborated.</em>` : "") +
        (stale ? `<br>${stale}` : "")
    );
  });
  map.on("click", "clusters", async (e) => {
    const features = map.queryRenderedFeatures(e.point, { layers: ["clusters"] });
    const clusterId = features[0].properties.cluster_id;
    const src = map.getSource("cells");
    const zoom = await src.getClusterExpansionZoom(clusterId);
    map.easeTo({ center: features[0].geometry.coordinates, zoom });
  });
  map.on("mouseenter", "cells", () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", "cells", () => (map.getCanvas().style.cursor = ""));
}

function showPopup(html) {
  const popup = document.getElementById("popup");
  popup.innerHTML = html;
  popup.hidden = false;
  setTimeout(() => { popup.hidden = true; }, 8000);
}

map.on("load", loadData);
refreshBtn.addEventListener("click", loadData);
daysEl.addEventListener("change", loadData);
detOnlyEl.addEventListener("change", loadData);

// Auto-refresh: the API reads only aggregates, so this is a cheap query.
const AUTO_REFRESH_S = 60;
setInterval(loadData, AUTO_REFRESH_S * 1000);
