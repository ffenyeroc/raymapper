/* raymap — Downloads page logic (pure helpers + a browser-only render).
 * Loaded as a plain script in the browser; require()d by node --test. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.raymapDownloads = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const REQUIRED = ["platform", "file", "url", "sha256", "size"];

  function formatBytes(n) {
    if (typeof n !== "number" || !isFinite(n) || n < 0) return "—";
    if (n < 1024) return `${n} B`;
    const mb = n / (1024 * 1024);
    return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(n / 1024).toFixed(0)} KB`;
  }

  // A wrong manifest should say so on the page, not render an empty card list.
  function validate(manifest) {
    const errors = [];
    if (!manifest || typeof manifest !== "object") {
      return { ok: false, artifacts: [], errors: ["manifest is not an object"] };
    }
    const artifacts = Array.isArray(manifest.artifacts) ? manifest.artifacts : [];
    if (!Array.isArray(manifest.artifacts)) errors.push("artifacts is not an array");

    const good = [];
    artifacts.forEach((a, i) => {
      const errs = [];
      for (const k of REQUIRED) {
        if (a[k] === undefined || a[k] === null) errs.push(`missing ${k}`);
      }
      if (a.url && !/^https?:\/\//.test(a.url)) errs.push("url is not http(s)");
      if (a.sha256 && !/^[0-9a-f]{64}$/i.test(a.sha256)) errs.push("sha256 is not 64 hex chars");
      if (a.size !== undefined && (typeof a.size !== "number" || a.size < 0)) {
        errs.push("size is not a number");
      }
      if (errs.length) errors.push(`artifact ${i}: ${errs.join("; ")}`);
      else good.push(a);
    });
    return { ok: errors.length === 0, artifacts: good, errors };
  }

  function render(doc, manifest) {
    const box = doc.getElementById("artifacts");
    const note = doc.getElementById("manifest-note");
    const { ok, artifacts, errors } = validate(manifest);

    if (!ok && errors.length) {
      note.textContent = `The download manifest is malformed: ${errors[0]}`;
      note.className = "callout";
      return;
    }
    if (!artifacts.length) {
      note.textContent =
        "No installers are published yet — the collector is still being packaged. " +
        "This page will list Windows, macOS and Linux builds here once they exist.";
      return;
    }

    note.textContent =
      manifest.version ? `Collector ${manifest.version}` : "Available builds";
    box.innerHTML = "";
    for (const a of artifacts) {
      const card = doc.createElement("div");
      card.className = "card";
      card.innerHTML =
        `<h3>${a.platform} <span class="muted">${a.arch || ""}</span></h3>` +
        `<p><a class="download" href="${a.url}">Download</a>` +
        ` <span class="muted">${formatBytes(a.size)}</span></p>` +
        `<p class="muted">SHA-256 <code>${a.sha256}</code></p>`;
      box.appendChild(card);
    }
  }

  return { formatBytes, validate, render, REQUIRED };
});

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    fetch("data/downloads.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((m) => window.raymapDownloads.render(document, m))
      .catch((e) => {
        const note = document.getElementById("manifest-note");
        if (note) note.textContent = `Could not load the download list (${e.message}).`;
      });
  });
}
