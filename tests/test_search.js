// node --test ui/test_search.js — offline lookup helpers.
const test = require("node:test");
const assert = require("node:assert");
const search = require("../ui/search.js");

const db = {
  meta: { places: 2, zips: 1 },
  places: {
    "atlanta,ga": [33.749, -84.388, "Atlanta"],
    "springfield,il": [39.7817, -89.6501, "Springfield"],
    "springfield,mo": [37.2089, -93.2923, "Springfield"],
  },
  zips: { "30303": [33.7519, -84.388] },
};

test("zip exact and ZIP+4", () => {
  const a = search.lookup(db, "30303");
  assert.equal(a.kind, "zip");
  assert.equal(a.label, "30303");
  assert.equal(a.lat, 33.7519);
  assert.equal(search.lookup(db, "30303-1234").kind, "zip");
  assert.equal(search.lookup(db, "99999"), null);
});

test("town with state, any case or spacing", () => {
  for (const q of ["Atlanta, GA", "  atlanta ,ga ", "ATLANTA,GA"]) {
    const hit = search.lookup(db, q);
    assert.ok(hit, q);
    assert.equal(hit.label, "Atlanta, GA");
    assert.equal(hit.lat, 33.749);
  }
});

test("bare town picks the first match and flags ambiguity", () => {
  const hit = search.lookup(db, "springfield");
  assert.ok(hit);
  assert.equal(hit.ambiguous, true);
  assert.equal(hit.label, "Springfield, IL");
});

test("bare unique town is unambiguous", () => {
  const hit = search.lookup(db, "atlanta");
  assert.equal(hit.ambiguous, false);
  assert.equal(hit.label, "Atlanta, GA");
});

test("misses and empty input", () => {
  assert.equal(search.lookup(db, "nowhere"), null);
  assert.equal(search.lookup(db, "Atlanta, ZZ"), null);
  assert.equal(search.lookup(db, ""), null);
  assert.equal(search.lookup(db, null), null);
  assert.equal(search.lookup(null, "atlanta"), null);
});

test("normalize collapses whitespace and case", () => {
  assert.equal(search.normalize("  New   York,  NY "), "new york, ny");
});
