"""The simulator dashboard — a single self-contained HTML page (canvas + panels +
vanilla JS/CSS, no external deps, CSP-safe) served at GET /sim.

It polls GET /sim/state ~7 Hz and shows the physical sim (top-down map, path,
sonar cone, live camera) alongside the brain's inner life (mood, gesture,
character, costmap histogram, narration) pushed via POST /sim/brain. Interactive:
click the map to drop an obstacle in the robot's path, pause/resume/reset, switch
scenario. Kept as a Python string constant so the server has zero template deps.
"""

from __future__ import annotations

DASHBOARD_HTML = r"""
<!doctype html>
<html><head><meta charset="utf-8"><title>crab · sim</title>
<style>
  :root{--bg:#0f1216;--panel:#171c22;--ink:#e6edf3;--muted:#8b98a5;--accent:#4bd;--warn:#e5533d;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.4 ui-monospace,Menlo,Consolas,monospace}
  header{padding:8px 14px;background:#0b0e11;border-bottom:1px solid #222;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
  header b{font-size:15px}
  header .sp{flex:1}
  button,select{background:#222a31;color:var(--ink);border:1px solid #333;border-radius:6px;padding:5px 9px;cursor:pointer;font:inherit}
  button:hover{border-color:var(--accent)}
  .wrap{display:grid;grid-template-columns:minmax(360px,1.4fr) 1fr;gap:12px;padding:12px;align-items:start}
  .panel{background:var(--panel);border:1px solid #232a31;border-radius:10px;padding:10px}
  .panel h3{margin:0 0 8px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
  canvas{width:100%;background:#0b0e11;border-radius:8px;display:block}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .col{flex:1;min-width:0}
  .kv{display:grid;grid-template-columns:auto 1fr;gap:2px 10px}
  .kv span:nth-child(odd){color:var(--muted)}
  .mood{font-size:22px;font-weight:bold}
  #say{min-height:40px}
  #log,#events{height:120px;overflow:auto;background:#0b0e11;border-radius:6px;padding:6px;white-space:pre-wrap}
  img{width:100%;border-radius:8px;background:#000;display:block}
  .pill{display:inline-block;padding:2px 8px;border-radius:99px;background:#222a31}
  .hint{color:var(--muted);font-size:11px}
  /* On the real robot (no 2D sim world) hide the sim-only controls/hints and show
     the "surroundings radar" hint instead. Toggled by body.no-world in tick(). */
  .worldoff{display:none}
  .no-world .worldonly{display:none}
  .no-world .worldoff{display:inline-block}
</style></head>
<body>
<header>
  <b>🦀 crab · simulator</b>
  <span id="petname" class="pill">…</span>
  <span id="mode" class="pill">…</span>
  <span class="sp"></span>
  <button class="worldonly" onclick="ctl('pause')">⏸ pause</button>
  <button class="worldonly" onclick="ctl('resume')">▶ resume</button>
  <button class="worldonly" onclick="ctl('reset')">⟲ reset</button>
  <select class="worldonly" id="scenario" onchange="ctl('scenario', this.value)">
    <option value="">scenario…</option>
    <option>poles</option><option>room</option><option>corridor</option><option>slalom</option>
  </select>
  <span class="worldoff pill">🤖 live robot</span>
</header>
<div class="wrap">
  <div class="col">
    <div class="panel">
      <h3>World map <span class="hint worldonly">— click to drop a pole · shift-click to remove</span><span class="hint worldoff">— surroundings radar (what the robot senses)</span></h3>
      <canvas id="map" width="600" height="600"></canvas>
    </div>
    <div class="panel" style="margin-top:12px">
      <h3>Costmap (what the brain sees)</h3>
      <canvas id="cost" width="600" height="90"></canvas>
    </div>
  </div>
  <div class="col">
    <div class="panel">
      <h3>Pet</h3>
      <div class="row">
        <div class="col">
          <div class="mood" id="mood">—</div>
          <div id="gesture" class="hint">—</div>
        </div>
        <div class="col kv" id="petkv"></div>
      </div>
      <div id="say" class="hint" style="margin-top:8px"></div>
      <div id="world" class="hint" style="margin-top:6px"></div>
    </div>
    <div class="panel" style="margin-top:12px">
      <h3>Camera (first-person)</h3>
      <img id="cam" alt="camera">
    </div>
    <div class="panel" style="margin-top:12px">
      <h3>Telemetry</h3>
      <div class="kv" id="telkv"></div>
      <canvas id="spark" width="600" height="46" style="margin-top:8px"></canvas>
    </div>
    <div class="row" style="margin-top:12px">
      <div class="panel col"><h3>Speech</h3><div id="log"></div></div>
      <div class="panel col"><h3>Events</h3><div id="events"></div></div>
    </div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
let W=300,H=300, dist=[], lastSay="", lastAction="", lastMood="";
$("cam").src="/camera/stream";

function ctl(action,scenario){fetch("/sim/control",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({action,scenario})});}

$("map").addEventListener("click",e=>{
  const c=$("map"), r=c.getBoundingClientRect();
  const cx=(e.clientX-r.left)/r.width*W;
  const cy=H-(e.clientY-r.top)/r.height*H;          // canvas y-down -> world y-up
  fetch("/sim/obstacle",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action:e.shiftKey?"remove":"add",cx,cy,r:14})});
});

function logLine(el,txt){const d=$(el);const at=d.scrollTop+d.clientHeight>=d.scrollHeight-4;
  d.textContent+=txt+"\n"; if(at) d.scrollTop=d.scrollHeight;}

function drawMap(s){
  const c=$("map"),g=c.getContext("2d");W=s.width;H=s.height;
  const sx=c.width/W, sy=c.height/H;
  const X=x=>x*sx, Y=y=>c.height-y*sy;               // flip y
  g.clearRect(0,0,c.width,c.height);
  g.strokeStyle="#2a333c";g.strokeRect(0,0,c.width,c.height);
  // trail
  if(s.trail&&s.trail.length>1){g.strokeStyle="#2f6d8a";g.beginPath();
    s.trail.forEach((p,i)=>{i?g.lineTo(X(p[0]),Y(p[1])):g.moveTo(X(p[0]),Y(p[1]));});g.stroke();}
  // obstacles
  (s.obstacles||[]).forEach(o=>{g.fillStyle=`rgb(${o.color[0]},${o.color[1]},${o.color[2]})`;
    g.beginPath();g.arc(X(o.cx),Y(o.cy),Math.max(3,o.r*sx),0,7);g.fill();});
  // robot + heading + sonar cone
  const rb=s.robot, th=rb.heading*Math.PI/180;
  const rx=X(rb.x),ry=Y(rb.y);
  const son=s.sonar||{}, d=(son.distance||0)*sx, cone=(son.cone||20)*Math.PI/180;
  g.fillStyle="rgba(75,200,220,.15)";g.strokeStyle="rgba(75,200,220,.5)";
  g.beginPath();g.moveTo(rx,ry);
  for(let a=-cone/2;a<=cone/2+1e-3;a+=cone/10){g.lineTo(rx+d*Math.cos(th+a),ry-d*Math.sin(th+a));}
  g.closePath();g.fill();g.stroke();
  // chosen heading arrow (brain), right-positive -> world angle th - chosen
  if(s.brain&&s.brain.heading!=null){const hd=(th-s.brain.heading*Math.PI/180);
    g.strokeStyle=s.brain.forward_clear?"#7ed957":"#e5b33d";g.lineWidth=2;g.beginPath();
    g.moveTo(rx,ry);g.lineTo(rx+40*Math.cos(hd),ry-40*Math.sin(hd));g.stroke();g.lineWidth=1;}
  // robot body
  g.fillStyle="#e6edf3";g.beginPath();
  g.moveTo(rx+9*Math.cos(th),ry-9*Math.sin(th));
  g.lineTo(rx+7*Math.cos(th+2.5),ry-7*Math.sin(th+2.5));
  g.lineTo(rx+7*Math.cos(th-2.5),ry-7*Math.sin(th-2.5));
  g.closePath();g.fill();
}

function drawRadar(b){
  // Robot-centered polar view of the costmap for the real robot (no 2D world):
  // 0deg = up/forward, +bearing = right. Wedges colored blocked/weak/free, with
  // the chosen-heading ray and the robot at center.
  const c=$("map"),g=c.getContext("2d");g.clearRect(0,0,c.width,c.height);
  const cx=c.width/2, cy=c.height/2, R=Math.min(cx,cy)-14;
  g.strokeStyle="#2a333c";
  for(let k=1;k<=3;k++){g.beginPath();g.arc(cx,cy,R*k/3,0,7);g.stroke();}
  if(!b||!b.conf||!b.centers){g.fillStyle="#8b98a5";g.fillText("waiting for brain…",cx-42,cy);return;}
  const n=b.conf.length, span=n>1?Math.abs(b.centers[1]-b.centers[0]):8;
  const pt=(deg,r)=>{const a=deg*Math.PI/180;return [cx+r*Math.sin(a), cy-r*Math.cos(a)];};
  for(let i=0;i<n;i++){
    const blk=b.blocked&&b.blocked[i];
    g.fillStyle=blk?"#e5533d":(b.conf[i]>0.15?"#e5b33d":"#2f4a3a");
    g.globalAlpha=blk?0.55:0.42;
    const rr=blk?R:R*(0.32+0.68*Math.min(1,b.conf[i]||0.12));
    g.beginPath();g.moveTo(cx,cy);
    for(let t=0;t<=1.0001;t+=0.25){const p=pt(b.centers[i]-span/2+t*span,rr);g.lineTo(p[0],p[1]);}
    g.closePath();g.fill();
  }
  g.globalAlpha=1;
  if(b.heading!=null){const p=pt(b.heading,R);g.strokeStyle=b.forward_clear?"#7ed957":"#e5b33d";
    g.lineWidth=2;g.beginPath();g.moveTo(cx,cy);g.lineTo(p[0],p[1]);g.stroke();g.lineWidth=1;}
  g.fillStyle="#e6edf3";g.beginPath();g.moveTo(cx,cy-9);g.lineTo(cx-6,cy+7);g.lineTo(cx+6,cy+7);g.closePath();g.fill();
}

function drawCost(b){
  const c=$("cost"),g=c.getContext("2d");g.clearRect(0,0,c.width,c.height);
  if(!b||!b.conf){g.fillStyle="#8b98a5";g.fillText("waiting for brain…",8,50);return;}
  const n=b.conf.length,bw=c.width/n;
  for(let i=0;i<n;i++){const h=Math.min(1,b.conf[i])*c.height;
    g.fillStyle=b.blocked&&b.blocked[i]?"#e5533d":(b.conf[i]>0.15?"#e5b33d":"#2f4a3a");
    g.fillRect(i*bw,c.height-h,bw-1,h);}
  // chosen heading marker (map bearing to bin index by center)
  if(b.centers&&b.heading!=null){let best=0,bd=1e9;
    b.centers.forEach((ct,i)=>{const dd=Math.abs(ct-b.heading);if(dd<bd){bd=dd;best=i;}});
    g.fillStyle="#7ed957";g.fillRect(best*bw,0,bw-1,6);}
}

function spark(){const c=$("spark"),g=c.getContext("2d");g.clearRect(0,0,c.width,c.height);
  g.strokeStyle="#4bd";g.beginPath();const n=dist.length;
  dist.forEach((d,i)=>{const x=i/Math.max(1,n-1)*c.width,y=c.height-Math.min(1,d/150)*c.height;
    i?g.lineTo(x,y):g.moveTo(x,y);});g.stroke();
  g.fillStyle="#8b98a5";g.fillText("forward clearance (0–150cm)",6,12);}

async function tick(){
  let s; try{s=await (await fetch("/sim/state")).json();}catch(e){return;}
  const worldOn=!!s.enabled;
  document.body.classList.toggle("no-world",!worldOn);
  const b=s.brain||{};
  // Map panel: the sim's top-down world when we have one, else a robot-centered
  // costmap radar (the honest "what it senses around it" view on real hardware).
  if(worldOn) drawMap(s); else drawRadar(b.costmap);
  drawCost(b.costmap);
  // telemetry + clearance sparkline (sim sonar, or the pet's pushed distance_cm)
  const son=s.sonar||{};
  const clr=worldOn?(son.distance||0):(b.distance_cm==null?null:b.distance_cm);
  dist.push(clr||0); if(dist.length>120)dist.shift(); spark();
  if(worldOn){
    $("telkv").innerHTML=kv({x:s.robot.x,y:s.robot.y,"θ":s.robot.heading+"°",
      clearance:(son.distance||0)+"cm", reflex:b.reflex?"YES":"no",
      battery:batt(b), paused:s.paused?"YES":"no", tick:b.tick??"—"});
  }else{
    $("telkv").innerHTML=kv({clearance:clr==null?"—":clr+"cm",
      camera:b.camera_fused?"fused":"off", forward:b.forward_clear?"clear":"blocked",
      reflex:b.reflex?"YES":"no", battery:batt(b), tick:b.tick??"—"});
  }
  $("mode").textContent=b.mode||"no brain";
  // pet
  $("petname").textContent=b.name||"—";
  $("mood").textContent=moodEmoji(b.mood)+" "+(b.mood||"—");
  $("gesture").textContent=b.gesture&&b.gesture!="none"?("*"+b.gesture+"*"):"";
  $("petkv").innerHTML=kv({memories:b.memory??"—", target:b.target||"—", place:b.place||"—"});
  if(b.character)$("say").innerHTML="<i>"+esc(b.character)+"</i>";
  $("world").textContent=b.world?("🧠 "+b.world):"";
  // logs
  if(b.say&&b.say!=lastSay){lastSay=b.say;logLine("log","🐾 "+b.say);}
  if(b.action&&b.action!=lastAction){lastAction=b.action;logLine("events","· "+b.action);}
  if(b.mood&&b.mood!=lastMood){lastMood=b.mood;logLine("events","mood → "+b.mood);}
}
function kv(o){return Object.entries(o).map(([k,v])=>`<span>${k}</span><span>${esc(String(v))}</span>`).join("");}
function esc(s){return s.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function moodEmoji(m){return {curious:"🐕",excited:"🤩",playful:"🐩",cautious:"😟",startled:"😱",bored:"😑",sleepy:"😴"}[m]||"🐾";}
function batt(b){return b.battery_v==null?"—":(b.battery_v.toFixed(1)+"V"+(b.battery_low?" ⚠":""));}
setInterval(tick,140); tick();
</script>
</body></html>
"""
