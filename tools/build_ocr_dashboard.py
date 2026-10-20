"""Build a self-contained HTML accuracy dashboard from reports/ocr_accuracy.json.

    python -m tools.build_ocr_dashboard [out.html]

The page embeds the report as JSON and draws every chart from it, so the numbers on screen are
the numbers in the file. Re-run after each evaluation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEMPLATE = r"""<title>Statement OCR Accuracy</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: a ledger-style report. Prose column on the left, evidence tables and charts beside it. */
:root {
  --bg: #f6f7f5; --panel: #ffffff; --ink: #1c2421; --muted: #5d6a65; --rule: #d9dfdb;
  --accent: #1f6b57; --accent-soft: #dcece6; --bad: #b3361f; --bad-soft: #f5dfd9; --warn: #9a6b00; --warn-soft: #f4e8c8;
  --bar: #1f6b57; --bar-2: #86a99d; --bar-3: #c8d3ce; --grid: #e4e9e6;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "Cascadia Mono", Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #131917; --panel: #1b2321; --ink: #e5ece8; --muted: #9aa8a2; --rule: #2d3935;
    --accent: #5fc2a4; --accent-soft: #1f3a32; --bad: #f08a72; --bad-soft: #3b231d; --warn: #e2b44a; --warn-soft: #3a3016;
    --bar: #5fc2a4; --bar-2: #3f7a69; --bar-3: #2d3935; --grid: #25302c; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #131917; --panel: #1b2321; --ink: #e5ece8; --muted: #9aa8a2; --rule: #2d3935;
  --accent: #5fc2a4; --accent-soft: #1f3a32; --bad: #f08a72; --bad-soft: #3b231d; --warn: #e2b44a; --warn-soft: #3a3016;
  --bar: #5fc2a4; --bar-2: #3f7a69; --bar-3: #2d3935; --grid: #25302c; color-scheme: dark;
}
body { background: var(--bg); color: var(--ink); font-family: var(--sans); line-height: 1.55; padding-block: 40px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
.wrap { max-width: 1040px; margin: 0 auto; display: grid; gap: 40px; }
h1 { font-size: clamp(1.7rem, 4vw, 2.3rem); line-height: 1.15; margin: 0; font-weight: 600; letter-spacing: -0.01em; text-wrap: balance; }
h2 { font-size: 1.15rem; margin: 0 0 4px; font-weight: 600; text-wrap: balance; }
p { margin: 0; max-width: 68ch; }
.lede { color: var(--muted); font-size: 1.05rem; }
.meta { font-family: var(--mono); font-size: 0.78rem; color: var(--muted); display: flex; flex-wrap: wrap; gap: 6px 18px; }
section { display: grid; gap: 14px; min-width: 0; }
.cols { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: 28px; align-items: start; }
@media (max-width: 760px) { .cols { grid-template-columns: minmax(0, 1fr); } }
.note { color: var(--muted); font-size: 0.92rem; }
.verdict { display: flex; flex-wrap: wrap; gap: 8px; }
.pill { font-family: var(--mono); font-size: 0.78rem; padding: 3px 10px; border-radius: 99px; background: var(--accent-soft); color: var(--accent); border: 1px solid transparent; }
.pill.bad { background: var(--bad-soft); color: var(--bad); }
.pill.warn { background: var(--warn-soft); color: var(--warn); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 8px 10px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { font-weight: 500; color: var(--muted); font-size: 0.75rem; letter-spacing: 0.06em; text-transform: uppercase; }
td small { color: var(--muted); font-family: var(--mono); font-size: 0.72rem; }
.chart { background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; padding: 14px 12px 8px; }
svg { width: 100%; height: auto; display: block; }
svg text { fill: var(--ink); font-family: var(--sans); font-size: 12px; }
svg .m { fill: var(--muted); font-family: var(--mono); font-size: 11px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 0.8rem; color: var(--muted); margin: 0 4px 6px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
.caveat { border-left: 3px solid var(--warn); background: var(--warn-soft); padding: 12px 16px; border-radius: 0 6px 6px 0; }
.caveat h2 { color: var(--warn); }
ul { margin: 0; padding-left: 1.1em; display: grid; gap: 6px; max-width: 70ch; }
code { font-family: var(--mono); font-size: 0.85em; }
</style>

<div class="wrap">
  <header style="display:grid;gap:14px">
    <div class="meta" id="meta"></div>
    <h1 id="headline"></h1>
    <p class="lede" id="lede"></p>
    <div class="verdict" id="verdict"></div>
  </header>

  <section>
    <h2>What gets read, and what a person still sees</h2>
    <div class="cols">
      <p class="note" id="tiernote"></p>
      <div class="chart"><div class="legend" id="leg1"></div><svg id="c1" viewBox="0 0 560 300" role="img" aria-label="Read rate, accuracy and straight-through rate by scan quality"></svg></div>
    </div>
    <div class="scroll"><table id="tiertable"></table></div>
  </section>

  <section>
    <h2>Two statement layouts</h2>
    <div class="cols">
      <p class="note" id="laynote"></p>
      <div class="scroll"><table id="laytable"></table></div>
    </div>
  </section>

  <section>
    <h2>Why the arithmetic check matters</h2>
    <div class="cols">
      <p class="note" id="ablnote"></p>
      <div class="chart"><div class="legend" id="leg2"></div><svg id="c2" viewBox="0 0 560 190" role="img" aria-label="Wrong values reaching the ledger, two routing rules"></svg></div>
    </div>
  </section>

  <section>
    <h2>Edited documents</h2>
    <div class="cols">
      <p class="note" id="tampernote"></p>
      <div class="scroll"><table id="tampertable"></table></div>
    </div>
  </section>

  <section>
    <h2>Errors the arithmetic cannot see</h2>
    <div class="cols">
      <p class="note" id="idnote"></p>
      <div class="scroll"><table id="idtable"></table></div>
    </div>
  </section>

  <section>
    <h2>Speed and calibration</h2>
    <div class="scroll"><table id="misc"></table></div>
  </section>

  <section class="caveat">
    <h2>What this does not prove</h2>
    <ul id="caveats"></ul>
  </section>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  var R = JSON.parse(document.getElementById("data").textContent);
  var $ = function (id) { return document.getElementById(id); };
  var pct = function (x, d) { return (100 * x).toFixed(d === undefined ? 1 : d) + "%"; };
  var LV = ["clean", "mild", "moderate", "severe"];
  var NAMES = { clean: "Clean", mild: "Mild", moderate: "Moderate", severe: "Severe" };
  var o = R.overall, bl = R.by_level;

  $("meta").innerHTML = ["engine " + R.engine, R.pipeline_version, R.corpus.documents + " pages", R.corpus.test_fields + " test fields", "seed " + R.seed]
    .map(function (t) { return "<span>" + t + "</span>"; }).join("");
  $("headline").textContent = "Of " + o.stp_rate.n.toLocaleString() + " figures on unseen pages, " + pct(o.stp_rate.value, 0) +
    " passed straight through and " + (o.stp_precision.value === 1 ? "none" : pct(1 - o.stp_precision.value, 2)) + " of those were wrong.";
  $("lede").textContent = "A synthetic corpus of Schedule III statements, rendered in two layouts at four scan qualities and read by an off-the-shelf recogniser. Every figure has exact ground truth. Calibration used one half of the obligors and every number here comes from the other half. The safety property holds; coverage does not yet meet the release gate.";

  var lay = R.by_layout || {};
  $("verdict").innerHTML =
    '<span class="pill">straight-through precision ' + pct(o.stp_precision.value, 2) + " (n=" + o.stp_precision.n + "), upper bound on error " + pct(1 - o.stp_precision.ci_low, 2) + "</span>" +
    '<span class="pill bad">release gate: FAILED on coverage (clean tier ' + pct(bl.clean.accuracy_after_validation.value, 0) + " correct vs 99% required)</span>" +
    (lay.A && lay.B ? '<span class="pill warn">layout A ' + pct(lay.A.accuracy_after_validation.value, 0) + " correct, layout B " + pct(lay.B.accuracy_after_validation.value, 0) + "</span>" : "") +
    '<span class="pill bad">severe pages ' + pct(bl.severe.stp_rate.value, 1) + " straight through: rescan</span>";

  /* ---- chart 1: grouped horizontal bars by tier, page-clustered CI whiskers on accuracy ---- */
  var series = [
    { key: "recall_found", label: "Figure located", color: "var(--bar-3)" },
    { key: "accuracy_after_validation", label: "Figure correct", color: "var(--bar-2)" },
    { key: "stp_rate", label: "Passed straight through", color: "var(--bar)" }
  ];
  $("leg1").innerHTML = series.map(function (s) { return '<span><i style="background:' + s.color + '"></i>' + s.label + "</span>"; }).join("");
  var W = 560, left = 76, right = 44, top = 12, rowH = 66, bh = 14, gap = 4, plot = W - left - right;
  var svg = '';
  [0, 25, 50, 75, 100].forEach(function (t) {
    var x = left + plot * t / 100;
    svg += '<line x1="' + x + '" x2="' + x + '" y1="' + top + '" y2="' + (top + rowH * 4) + '" stroke="var(--grid)"/>';
    svg += '<text class="m" x="' + x + '" y="' + (top + rowH * 4 + 16) + '" text-anchor="middle">' + t + '%</text>';
  });
  LV.forEach(function (lv, i) {
    var y0 = top + i * rowH + 4;
    svg += '<text x="' + (left - 10) + '" y="' + (y0 + 26) + '" text-anchor="end">' + NAMES[lv] + "</text>";
    series.forEach(function (s, j) {
      var v = bl[lv][s.key].value, y = y0 + j * (bh + gap), w = plot * v;
      svg += '<rect x="' + left + '" y="' + y + '" width="' + Math.max(w, 1) + '" height="' + bh + '" rx="2" fill="' + s.color + '"/>';
      var txt = pct(v, v === 1 || v === 0 ? 0 : 1);
      svg += '<text class="m" x="' + (left + w + 6) + '" y="' + (y + 11) + '">' + txt + "</text>";
      var ci = bl[lv].page_clustered_ci95 && s.key === "accuracy_after_validation" ? bl[lv].page_clustered_ci95.accuracy_after_validation : null;
      if (ci && ci.length === 2 && ci[0] === ci[0]) {
        var xa = left + plot * ci[0], xb = left + plot * ci[1], yc = y + bh / 2;
        svg += '<line x1="' + xa + '" x2="' + xb + '" y1="' + yc + '" y2="' + yc + '" stroke="var(--ink)" stroke-width="1.2"/>' +
               '<line x1="' + xa + '" x2="' + xa + '" y1="' + (yc - 4) + '" y2="' + (yc + 4) + '" stroke="var(--ink)" stroke-width="1.2"/>' +
               '<line x1="' + xb + '" x2="' + xb + '" y1="' + (yc - 4) + '" y2="' + (yc + 4) + '" stroke="var(--ink)" stroke-width="1.2"/>';
      }
    });
  });
  $("c1").setAttribute("viewBox", "0 0 " + W + " " + (top + rowH * 4 + 28));
  $("c1").innerHTML = svg;
  $("tiernote").textContent = "Pages are graded from clean to severe. The whiskers are 95% intervals that resample whole pages, because figures on one page share the same scan damage. Moderate pages are readable but rows slip, so the platform sends more of them to a reviewer. Severe pages are rejected for rescan instead of being read badly.";

  var rows = '<tr><th>Scan quality</th><th>Pages</th><th>Located</th><th>Correct</th><th>Straight through</th><th>Wrong among straight through</th><th>Reviewer queue</th></tr>';
  LV.concat(["overall"]).forEach(function (lv) {
    var b = lv === "overall" ? o : bl[lv];
    var wrong = b.stp_precision && b.stp_precision.n ? Math.round((1 - b.stp_precision.value) * b.stp_precision.n) : 0;
    rows += "<tr><td>" + (lv === "overall" ? "<strong>All tiers</strong>" : NAMES[lv]) + "</td><td>" + (b.pages || "") + "</td><td>" + pct(b.recall_found.value) +
      "</td><td>" + pct(b.accuracy_after_validation.value) + "</td><td>" + pct(b.stp_rate.value) + " <small>n=" + b.stp_rate.n + "</small></td><td>" +
      (b.stp_precision && b.stp_precision.n ? wrong + " of " + b.stp_precision.n : "none passed") + "</td><td>" + pct(b.review_rate.value) + "</td></tr>";
  });
  $("tiertable").innerHTML = rows;

  /* ---- layouts ---- */
  if (lay.A && lay.B) {
    $("laynote").textContent = "Layout B adds a notes column and labels worded independently of the extractor's alias table. Almost every miss is a row whose label the extractor did not recognise; it is left for a reviewer rather than guessed, which is why precision is unaffected while coverage collapses. This is the weakness the harder corpus was built to expose.";
    $("laytable").innerHTML = '<tr><th>Layout</th><th>Pages</th><th>Located</th><th>Correct</th><th>Straight through</th><th>Wrong among straight through</th></tr>' +
      ["A", "B"].map(function (k) {
        var b = lay[k];
        var wrong = b.stp_precision && b.stp_precision.n ? Math.round((1 - b.stp_precision.value) * b.stp_precision.n) : 0;
        return "<tr><td>" + k + "</td><td>" + b.pages + "</td><td>" + pct(b.recall_found.value) + "</td><td>" + pct(b.accuracy_after_validation.value) + "</td><td>" + pct(b.stp_rate.value) + "</td><td>" + wrong + " of " + (b.stp_precision ? b.stp_precision.n : 0) + "</td></tr>";
      }).join("");
  }

  /* ---- identity-preserving errors ---- */
  var ip = R.identity_preserving_errors;
  if (ip) {
    $("idnote").textContent = "Swapping two sibling figures, or shifting a column by one row, keeps every total correct. A continuity check against the prior period catches most of them. The rest would pass straight through. This residual risk is measured here and is not claimed to be zero.";
    $("idtable").innerHTML = '<tr><th>Error</th><th>Pages</th><th>Stopped</th><th>Would pass straight through</th></tr>' +
      Object.keys(ip).map(function (k) {
        var t = ip[k];
        return "<tr><td>" + k + "</td><td>" + t.pages + "</td><td>" + pct(t.stopped_by_routing.value, 0) + "</td><td>" + pct(t.wrong_figures_would_pass_straight_through.value, 0) +
          " <small>[" + pct(t.wrong_figures_would_pass_straight_through.ci_low, 0) + ", " + pct(t.wrong_figures_would_pass_straight_through.ci_high, 0) + "]</small></td></tr>";
      }).join("");
  }

  /* ---- chart 2: ablation ---- */
  var ab = R.ablation_confidence_only_routing;
  var items = [
    { label: "Confidence only", sub: "straight-through rate " + pct(ab.stp_rate.value, 1) + " at the same precision target", v: ab.stp_rate.value, n: ab.stp_rate.n, color: "var(--bad)" },
    { label: "With arithmetic checks", sub: "accepted only when a statement total confirms it; " + pct(o.stp_precision.value, 2) + " correct", v: o.stp_rate.value, n: o.stp_rate.n, color: "var(--bar)" }
  ];
  $("leg2").innerHTML = "<span>Share of figures accepted without review, both rules held to the same 99.5% precision target</span>";
  var s2 = "", pl = 150, pw = 360;
  [0, 25, 50, 75, 100].forEach(function (t) {
    var x = pl + pw * t / 100;
    s2 += '<line x1="' + x + '" x2="' + x + '" y1="8" y2="132" stroke="var(--grid)"/><text class="m" x="' + x + '" y="150" text-anchor="middle">' + t + "%</text>";
  });
  items.forEach(function (it, i) {
    var y = 18 + i * 56, w = pw * it.v;
    s2 += '<text x="' + (pl - 10) + '" y="' + (y + 12) + '" text-anchor="end">' + it.label + "</text>";
    s2 += '<rect x="' + pl + '" y="' + y + '" width="' + Math.max(w, 1) + '" height="22" rx="2" fill="' + it.color + '"/>';
    s2 += '<text class="m" x="' + (pl + w + 6) + '" y="' + (y + 15) + '">' + pct(it.v, 1) + "</text>";
    s2 += '<text class="m" x="' + (pl) + '" y="' + (y + 38) + '">' + it.sub + " (" + it.n + " figures)</text>";
  });
  $("c2").innerHTML = s2;
  $("ablnote").textContent = "A recogniser's own confidence says the characters are crisp, not that a label was paired with the right number. On this corpus a confidence threshold tuned to the same 99.5% precision target on the calibration half never clears (the engine is confidently wrong too often), so it accepts nothing. The arithmetic checks are what make straight-through processing possible at all.";

  /* ---- tamper ---- */
  var td = R.tamper_detection;
  $("tampernote").textContent = "We edited one figure on a page and re-rendered it, then checked whether the platform noticed. An edit that breaks the books is caught and the cell is pointed out. A forgery that adjusts every dependent total is arithmetically consistent, so arithmetic cannot see it. That gap is real; closing it needs cross-document checks against GST and bank data, and image forensics.";
  $("tampertable").innerHTML = '<tr><th>Edit type</th><th>Pages</th><th>Flagged</th><th>Cell pinpointed</th></tr>' +
    ["inconsistent", "consistent"].map(function (k) {
      var t = td[k];
      return "<tr><td>" + (k === "inconsistent" ? "One figure changed" : "Totals adjusted to match") + "</td><td>" + t.documents + "</td><td>" + pct(t.flagged_for_review.value, 0) +
        " <small>[" + pct(t.flagged_for_review.ci_low, 0) + ", " + pct(t.flagged_for_review.ci_high, 0) + "]</small></td><td>" + pct(t.tampered_cell_pinpointed.value, 0) + "</td></tr>";
    }).join("");

  /* ---- misc ---- */
  var L = R.latency, cal = R.calibration, dc = R.documents;
  $("misc").innerHTML = '<tr><th>Measure</th><th>Value</th></tr>' +
    "<tr><td>Seconds per page, one CPU process</td><td>" + L.mean_seconds_per_page.toFixed(1) + " mean, " + L.p95_seconds_per_page.toFixed(1) + " p95</td></tr>" +
    "<tr><td>Calibration error, engine confidence alone</td><td>" + cal.ece_raw_ocr_confidence.toFixed(3) + "</td></tr>" +
    "<tr><td>Calibration error, calibrated score</td><td>" + cal.ece_calibrated.toFixed(3) + " <small>limit 0.050</small></td></tr>" +
    "<tr><td>Unit header read correctly, supported tiers</td><td>" + pct(dc.unit_detected_supported_tiers.value, 1) + " <small>n=" + dc.unit_detected_supported_tiers.n + "</small></td></tr>" +
    "<tr><td>Severe pages flagged for rescan</td><td>" + pct(dc.severe_pages_flagged_for_rescan.value, 0) + " <small>n=" + dc.severe_pages_flagged_for_rescan.n + "</small></td></tr>" +
    "<tr><td>Genuine pages wrongly flagged by the arithmetic check</td><td>" + pct(dc.genuine_documents_flagged_by_validation.value, 1) + " <small>n=" + dc.genuine_documents_flagged_by_validation.n + "</small></td></tr>";

  $("caveats").innerHTML = [
    "The pages are synthetic: one renderer, a handful of fonts, two statement layouts. Real scans vary far more. Re-run this report on consented real documents before quoting it to anyone.",
    "Damage is simulated (skew, blur, noise, compression). Stamps, handwriting, folds and photographed pages are not covered.",
    "Only financial statements are measured. Stock statements, certificates, deeds and Indic scripts are not.",
    "The release thresholds were fixed from the build guide before the first run. Thresholds were amended twice after seeing results (v2, v3); the history is in ADR 0003 and <code>ocr/eval/thresholds.yaml</code>.",
    "Straight-through precision of 100% is a bound, not a promise. The interval is the honest statement."
  ].map(function (t) { return "<li>" + t + "</li>"; }).join("");
})();
</script>
"""


def main() -> int:
    src = ROOT / "reports" / "ocr_accuracy.json"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports" / "ocr_accuracy.html"
    data = json.loads(src.read_text(encoding="utf-8"))
    html = TEMPLATE.replace("__DATA__", json.dumps(data, default=float).replace("</", "<\\/"))
    out.write_text(html, encoding="utf-8")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
