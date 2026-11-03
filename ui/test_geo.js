// node --test ui/test_geo.js — pure geometry helpers, no browser needed.
const test = require("node:test");
const assert = require("node:assert");
const geo = require("./geo.js");

const square = (w, s, e, n) => ({
  type: "Feature",
  properties: {},
  geometry: { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] },
});

test("resForZoom follows the r3..r6 ladder", () => {
  assert.equal(geo.resForZoom(4), 3);
  assert.equal(geo.resForZoom(5), 3);
  assert.equal(geo.resForZoom(6), 4);
  assert.equal(geo.resForZoom(7), 4);
  assert.equal(geo.resForZoom(8), 5);
  assert.equal(geo.resForZoom(9), 6);
  assert.equal(geo.resForZoom(13), 6);
  assert.equal(geo.resForZoom(20), 6);
});

test("no zoom yields a cell smaller than r6 (~7.4 km)", () => {
  for (let z = 0; z < 25; z++) {
    assert.ok(geo.resForZoom(z) <= 6, `zoom ${z} -> r${geo.resForZoom(z)}`);
  }
});

test("res never leaves the privacy band", () => {
  for (let z = 0; z < 25; z++) {
    const r = geo.resForZoom(z);
    assert.ok(r >= geo.RES_MIN && r <= geo.RES_MAX, `zoom ${z} -> r${r}`);
  }
});

test("pointInFeature inside and outside", () => {
  const ga = square(-85.6, 30.4, -80.8, 35.0);
  assert.equal(geo.pointInFeature(-83.0, 33.0, ga), true);
  assert.equal(geo.pointInFeature(-90.0, 33.0, ga), false);
  assert.equal(geo.pointInFeature(-83.0, 40.0, ga), false);
});

test("holes are excluded", () => {
  const withHole = {
    type: "Feature",
    properties: {},
    geometry: { type: "Polygon", coordinates: [
      [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
      [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
    ] },
  };
  assert.equal(geo.pointInFeature(1, 1, withHole), true);
  assert.equal(geo.pointInFeature(5, 5, withHole), false);
});

test("multipolygon", () => {
  const mp = {
    type: "Feature", properties: {}, geometry: { type: "MultiPolygon", coordinates: [
      [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
      [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]],
    ] },
  };
  assert.equal(geo.pointInFeature(0.5, 0.5, mp), true);
  assert.equal(geo.pointInFeature(5.5, 5.5, mp), true);
  assert.equal(geo.pointInFeature(2.5, 2.5, mp), false);
});

test("bbox helpers", () => {
  const f = square(-85.6, 30.4, -80.8, 35.0);
  const box = geo.bboxOfFeature(f);
  assert.deepEqual(box, [-85.6, 30.4, -80.8, 35.0]);
  assert.equal(geo.bboxContains(box, [-84, 32, -82, 34]), true);
  assert.equal(geo.bboxContains(box, [-84, 32, -70, 34]), false);
  assert.equal(geo.bboxIntersects(box, [-90, 20, -84, 32]), true);
  assert.equal(geo.bboxIntersects(box, [-70, 20, -60, 32]), false);
});

test("padBbox clamps to valid lat/lng", () => {
  const padded = geo.padBbox([-170, -80, 170, 80], 0.5);
  assert.ok(padded[0] >= -180 && padded[2] <= 180);
  assert.ok(padded[1] >= -85 && padded[3] <= 85);
});

const ring = geo.bboxPolygonRing([-85, 30, -80, 35]);
assert.equal(ring.length, 5);
assert.deepEqual(ring[0], ring[4]);
