/* raymap — offline place/ZIP lookup (no network, no geocoding service).
 * Loaded as a plain script in the browser; require()d by node --test. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.raymapSearch = factory();
})(typeof self !== "undefined" ? self : this, function () {
  function normalize(query) {
    return String(query || "")
      .trim()
      .toLowerCase()
      .replace(/\s+/g, " ");
  }

  // Accepts "30303", "30303-1234", "Atlanta, GA" or a bare "atlanta".
  function lookup(db, raw) {
    const q = normalize(raw);
    if (!db || !q) return null;

    const zip = q.split("-")[0];
    if (/^\d{5}$/.test(zip)) {
      const hit = db.zips && db.zips[zip];
      return hit ? { kind: "zip", label: zip, lat: hit[0], lng: hit[1] } : null;
    }

    const withState = q.match(/^(.+?)\s*,\s*([a-z]{2})$/);
    if (withState) {
      const [, name, state] = withState;
      const hit = db.places && db.places[`${name},${state}`];
      return hit
        ? { kind: "place", label: `${hit[2]}, ${state.toUpperCase()}`, lat: hit[0], lng: hit[1] }
        : null;
    }

    // Bare name: prefix match across "name,state" keys; report if ambiguous.
    let match = null;
    let count = 0;
    for (const key in db.places) {
      if (key.startsWith(q + ",")) {
        if (match === null) match = key;
        if (++count > 1) break;
      }
    }
    if (match === null) return null;
    const hit = db.places[match];
    const state = match.slice(match.lastIndexOf(",") + 1).toUpperCase();
    return {
      kind: "place",
      label: `${hit[2]}, ${state}`,
      lat: hit[0],
      lng: hit[1],
      ambiguous: count > 1,
    };
  }

  return { normalize, lookup };
});
