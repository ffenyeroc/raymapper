const test = require("node:test");
const assert = require("node:assert");
const dl = require("./downloads.js");

const good = () => ({
  version: "1.0.0",
  artifacts: [
    {
      platform: "macOS",
      arch: "arm64",
      file: "raymapper-collector-1.0.0-macos-arm64.tar.gz",
      size: 41_234_567,
      sha256: "a".repeat(64),
      url: "https://github.com/ffenyeroc/raymapper/releases/download/v1.0.0/x.tar.gz",
    },
  ],
});

test("formatBytes", () => {
  assert.equal(dl.formatBytes(0), "0 B");
  assert.equal(dl.formatBytes(2048), "2 KB");
  assert.equal(dl.formatBytes(41_234_567), "39.3 MB");
  assert.equal(dl.formatBytes(-1), "—");
  assert.equal(dl.formatBytes("nope"), "—");
});

test("a well-formed manifest validates", () => {
  const r = dl.validate(good());
  assert.equal(r.ok, true);
  assert.equal(r.artifacts.length, 1);
  assert.deepEqual(r.errors, []);
});

test("an empty manifest is valid but offers nothing", () => {
  const r = dl.validate({ version: null, artifacts: [] });
  assert.equal(r.ok, true);
  assert.equal(r.artifacts.length, 0);
});

test("missing fields, bad url and bad hash are reported", () => {
  const m = good();
  delete m.artifacts[0].size;
  m.artifacts[0].url = "ftp://example.com/x";
  m.artifacts[0].sha256 = "short";
  const r = dl.validate(m);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.includes("missing size")));
  assert.ok(r.errors.some((e) => e.includes("url is not http")));
  assert.ok(r.errors.some((e) => e.includes("sha256")));
  // A bad artifact is not rendered.
  assert.equal(r.artifacts.length, 0);
});

test("garbage does not throw", () => {
  for (const bad of [null, undefined, 42, "x", { artifacts: "no" }]) {
    const r = dl.validate(bad);
    assert.equal(r.ok, false);
    assert.equal(r.artifacts.length, 0);
  }
});
