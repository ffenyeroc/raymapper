/* raymap — geometry helpers, no dependencies.
 * Loaded as a plain script in the browser; require()d by node --test. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.raymapGeo = factory();
})(typeof self !== "undefined" ? self : this, function () {
  // Hex "across" in km, for the legend and the zoom ladder.
  const RES_KM = { 3: 138, 4: 52, 5: 20, 6: 7.4, 7: 2.8, 8: 1.06 };
  const RES_MIN = 3;
  const RES_MAX = 8;

  // ~25-40px hexes at every zoom; r8 is the privacy floor.
  function resForZoom(z) {
    if (z <= 5) return 3;
    if (z <= 7) return 4;
    if (z <= 8) return 5;
    if (z <= 10) return 6;
    if (z <= 12) return 7;
    return 8;
  }

  function pointInRing(lng, lat, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const xi = ring[i][0], yi = ring[i][1];
      const xj = ring[j][0], yj = ring[j][1];
      const crosses = (yi > lat) !== (yj > lat);
      if (crosses && lng < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  // Polygon coordinates: [outerRing, ...holes]
  function pointInPolygonCoords(lng, lat, coords) {
    if (!coords.length || !pointInRing(lng, lat, coords[0])) return false;
    for (let i = 1; i < coords.length; i++) {
      if (pointInRing(lng, lat, coords[i])) return false;
    }
    return true;
  }

  function pointInFeature(lng, lat, feature) {
    const g = feature.geometry;
    if (!g) return false;
    if (g.type === "Polygon") return pointInPolygonCoords(lng, lat, g.coordinates);
    if (g.type === "MultiPolygon") {
      return g.coordinates.some((poly) => pointInPolygonCoords(lng, lat, poly));
    }
    return false;
  }

  // bbox = [west, south, east, north]
  function expand(box, coords) {
    if (typeof coords[0] === "number") {
      const [x, y] = coords;
      if (x < box[0]) box[0] = x;
      if (y < box[1]) box[1] = y;
      if (x > box[2]) box[2] = x;
      if (y > box[3]) box[3] = y;
      return;
    }
    for (const c of coords) expand(box, c);
  }

  function bboxOfFeature(feature) {
    const box = [Infinity, Infinity, -Infinity, -Infinity];
    expand(box, feature.geometry.coordinates);
    return box;
  }

  function bboxIntersects(a, b) {
    return !(a[2] < b[0] || b[2] < a[0] || a[3] < b[1] || b[3] < a[1]);
  }

  function bboxContains(outer, inner) {
    return inner[0] >= outer[0] && inner[2] <= outer[2] && inner[1] >= outer[1] && inner[3] <= outer[3];
  }

  function padBbox(box, frac) {
    const dx = (box[2] - box[0]) * frac;
    const dy = (box[3] - box[1]) * frac;
    return [
      Math.max(-180, box[0] - dx),
      Math.max(-85, box[1] - dy),
      Math.min(180, box[2] + dx),
      Math.min(85, box[3] + dy),
    ];
  }

  function bboxPolygonRing(box) {
    const [w, s, e, n] = box;
    return [[w, s], [e, s], [e, n], [w, n], [w, s]];
  }

  return {
    RES_KM, RES_MIN, RES_MAX, resForZoom, pointInRing, pointInPolygonCoords,
    pointInFeature, bboxOfFeature, bboxIntersects, bboxContains, padBbox, bboxPolygonRing,
  };
});
