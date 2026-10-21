"""Self-contained HTML viewer for the demonstration story and the connection table.

    python -m tools.build_demo_page [out.html]

Embeds reports/demo_story.json and reports/integration_status.json; every number on the page is read
from those files at load time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEMPLATE = r"""<title>Credit Platform Simulation</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {
  --bg:#f5f6f3; --panel:#fff; --ink:#1b2320; --muted:#5b6862; --rule:#d9dfdb; --accent:#1d5f8a;
  --real:#1f6b57; --real-bg:#dcece6; --synth:#1d5f8a; --synth-bg:#dbe9f3;
  --sim:#8a5a00; --sim-bg:#f4e8c8; --none:#9b2f1d; --none-bg:#f5dfd9;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Consolas,monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#121816; --panel:#1a2220; --ink:#e4ebe7; --muted:#9aa8a2; --rule:#2c3834; --accent:#6db5e0;
  --real:#5fc2a4; --real-bg:#1f3a32; --synth:#7ec0ea; --synth-bg:#1c3347;
  --sim:#e2b44a; --sim-bg:#3a3016; --none:#f08a72; --none-bg:#3b231d; color-scheme:dark; } }
:root[data-theme="dark"] {
  --bg:#121816; --panel:#1a2220; --ink:#e4ebe7; --muted:#9aa8a2; --rule:#2c3834; --accent:#6db5e0;
  --real:#5fc2a4; --real-bg:#1f3a32; --synth:#7ec0ea; --synth-bg:#1c3347;
  --sim:#e2b44a; --sim-bg:#3a3016; --none:#f08a72; --none-bg:#3b231d; color-scheme:dark; }
body { background:var(--bg); color:var(--ink); font-family:var(--sans); line-height:1.5; padding-block:36px 64px;
  padding-left:max(16px,env(safe-area-inset-left,0px)); padding-right:max(16px,env(safe-area-inset-right,0px)); }
.wrap { max-width:1000px; margin:0 auto; display:grid; gap:32px; }
h1 { font-size:clamp(1.6rem,4vw,2.2rem); line-height:1.15; margin:0; font-weight:600; text-wrap:balance; }
h2 { font-size:1.1rem; margin:0 0 6px; font-weight:600; }
p { margin:0; max-width:68ch; } .lede { color:var(--muted); }
.legend { display:flex; flex-wrap:wrap; gap:8px; }
.chip { font-family:var(--mono); font-size:.72rem; padding:2px 9px; border-radius:99px; white-space:nowrap; }
.REAL { background:var(--real-bg); color:var(--real); } .SYNTH { background:var(--synth-bg); color:var(--synth); }
.SIM { background:var(--sim-bg); color:var(--sim); } .NONE { background:var(--none-bg); color:var(--none); }
.scroll { overflow-x:auto; } table { border-collapse:collapse; width:100%; font-size:.88rem; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--rule); vertical-align:top; }
th { color:var(--muted); font-size:.72rem; letter-spacing:.06em; text-transform:uppercase; font-weight:500; }
.tabs { display:flex; flex-wrap:wrap; gap:6px; }
.tabs button { font:inherit; font-size:.9rem; padding:6px 14px; border-radius:6px; border:1px solid var(--rule);
  background:var(--panel); color:var(--ink); cursor:pointer; }
.tabs button[aria-selected="true"] { background:var(--accent); color:var(--bg); border-color:var(--accent); }
.tl { list-style:none; margin:0; padding:0; display:grid; gap:0; border-left:2px solid var(--rule); }
.tl li { padding:10px 0 10px 18px; position:relative; display:grid; gap:4px; }
.tl li::before { content:""; position:absolute; left:-6px; top:16px; width:10px; height:10px; border-radius:50%;
  background:var(--panel); border:2px solid var(--accent); }
.tl .meta { display:flex; flex-wrap:wrap; gap:6px 10px; align-items:center; font-size:.8rem; color:var(--muted); }
.tl .meta b { color:var(--ink); font-weight:500; }
.tl .txt { max-width:80ch; }
.stats { display:flex; flex-wrap:wrap; gap:10px 24px; font-family:var(--mono); font-size:.8rem; color:var(--muted); }
.stats b { color:var(--ink); }
.note { border-left:3px solid var(--sim); background:var(--sim-bg); padding:12px 16px; border-radius:0 6px 6px 0; }
</style>
<div class="wrap">
  <header style="display:grid;gap:12px">
    <h1>What the platform does, and what is real</h1>
    <p class="lede" id="lede"></p>
    <div class="legend" id="legend"></div>
  </header>
  <section><h2>What is connected</h2><div class="scroll"><table id="conn"></table></div></section>
  <section>
    <h2>Four borrowers, replayed</h2>
    <div class="tabs" id="tabs" role="tablist"></div>
    <p class="lede" id="jnote"></p>
    <div class="stats" id="stats"></div>
    <ol class="tl" id="tl"></ol>
  </section>
  <section class="note"><h2>Say this out loud</h2>
    <ul id="limits" style="margin:0;padding-left:1.1em;display:grid;gap:4px"></ul></section>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
  var D=JSON.parse(document.getElementById("data").textContent), $=function(i){return document.getElementById(i)};
  var esc=function(s){return String(s).replace(/[&<>]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;"}[c]})};
  var cls=function(m){ if(/^NOT BUILT/.test(m)) return "NONE"; if(/SIMULATED/.test(m)) return "SIM"; if(/SYNTH/.test(m)) return "SYNTH"; return "REAL"; };
  var scls=function(s){ return s==="CONNECTED"?"REAL":s==="SIMULATED"?"SIM":s==="NOT BUILT"?"NONE":"SYNTH"; };
  $("lede").textContent="Four synthetic borrowers go from sanction to closure or default. Production code makes every decision; things outside the platform (the core banking system, the approvers, data feeds) are stand-ins and every step says which. Engine: "+D.story.engine+".";
  $("legend").innerHTML=[["REAL","REAL: production code"],["SYNTH","REAL+SYNTH: real engine on a synthetic page"],["SIM","SIMULATED: stand-in with the same interface"],["NONE","NOT BUILT: a visible gap"]].map(function(x){return '<span class="chip '+x[0]+'">'+x[1]+'</span>'}).join("");
  $("conn").innerHTML='<tr><th>Area</th><th>Component</th><th>Status</th><th>What is behind it</th><th>Evidence</th></tr>'+D.status.map(function(r){
    return "<tr><td>"+esc(r.area)+"</td><td>"+esc(r.name)+'</td><td><span class="chip '+scls(r.status)+'">'+esc(r.status)+"</span></td><td>"+esc(r.behind_it)+"</td><td>"+esc(r.evidence)+"</td></tr>"}).join("");
  var NAMES={steady:"Steady borrower",breach:"Covenant breach",default:"Stress to default",negotiated:"Negotiated agreement"};
  var NOTES={steady:"Sanction to closure with nothing going wrong: the baseline.",breach:"A covenant fails. An agent proposes; the agent's own human is refused; a second officer decides; the gate then stops new money.",default:"Early warning, SMA-2, a red-flag file, NPA, a stressed-asset clock and a settlement, with natural justice enforced in code.",negotiated:"The one place the demo shows a gap: the grammar cannot read a negotiated agreement and no model is attached."};
  var order=["steady","breach","default","negotiated"], js={}; D.story.journeys.forEach(function(j){js[j.archetype]=j});
  $("tabs").innerHTML=order.filter(function(k){return js[k]}).map(function(k,i){return '<button role="tab" data-k="'+k+'" aria-selected="'+(i===0)+'">'+NAMES[k]+"</button>"}).join("");
  function show(k){
    var j=js[k]; [].forEach.call($("tabs").children,function(b){b.setAttribute("aria-selected",b.dataset.k===k)});
    $("jnote").textContent=NOTES[k]+" ("+j.obligor+")";
    var c={REAL:0,SYNTH:0,SIM:0,NONE:0}; j.steps.forEach(function(s){c[cls(s.mode)]++});
    var st="<span><b>"+j.steps.length+"</b> steps</span><span><b>"+c.REAL+"</b> real</span><span><b>"+c.SYNTH+"</b> real engine on synthetic page</span><span><b>"+c.SIM+"</b> simulated</span><span><b>"+c.NONE+"</b> not built</span>";
    if(j.summary.ocr) st+="<span>OCR: <b>"+j.summary.ocr.straight_through_correct+"/"+j.summary.ocr.figures+"</b> straight through correct, <b>"+j.summary.ocr.straight_through_wrong+"</b> wrong</span>";
    $("stats").innerHTML=st;
    $("tl").innerHTML=j.steps.map(function(s){return '<li><div class="meta"><b>'+esc(s.when)+"</b><span>"+esc(s.component)+'</span><span class="chip '+cls(s.mode)+'">'+esc(s.mode)+'</span></div><div class="txt">'+esc(s.text)+"</div></li>"}).join("");
  }
  $("tabs").addEventListener("click",function(e){ if(e.target.dataset&&e.target.dataset.k) show(e.target.dataset.k)});
  show(order.filter(function(k){return js[k]})[0]);
  $("limits").innerHTML=["Every accuracy figure is on synthetic data with exact ground truth; none is a claim about real scans, letters or defaults.","The larger OCR release gate is failing: a second statement layout is read poorly. Safety holds (no wrong figure passed straight through); coverage does not.","No language model is attached, so letters and notes come from templates. The guards that would police a model are real and tested but untested against one.","The hosted PaddleOCR call is waiting for your key and runs on synthetic data only: the service is outside India.","Nothing is independently validated; rule packs are unverified against RBI text."].map(function(t){return "<li>"+t+"</li>"}).join("");
})();
</script>
"""


def main() -> int:
    story = json.loads((ROOT / "reports" / "demo_story.json").read_text(encoding="utf-8"))
    status = json.loads((ROOT / "reports" / "integration_status.json").read_text(encoding="utf-8"))
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports" / "demo_page.html"
    data = json.dumps({"story": story, "status": status}, default=str).replace("</", "<\\/")
    out.write_text(TEMPLATE.replace("__DATA__", data), encoding="utf-8")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
