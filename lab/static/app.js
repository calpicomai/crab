"use strict";
const $=id=>document.getElementById(id),canvas=$("scene"),ctx=canvas.getContext("2d");
let state=null,busy=false,received=0,lastRevision=-1,lastStatus="",lastTime=-1,rtt=0,linkError=false;
const history=[],events=[];
let yaw=-1.03,pitch=.59,distance=.47,drag=null;
const finite=x=>typeof x==="number"&&Number.isFinite(x),fmt=(x,n=2)=>finite(x)?x.toFixed(n):"—";
const set=(id,value)=>{$(id).textContent=value;};
function error(message){$("error").hidden=!message;set("error",message||"");}
function event(message){events.unshift({at:state?.sim_time_s??0,message});events.length=Math.min(events.length,20);const list=$("events");list.replaceChildren();for(const row of events){const li=document.createElement("li"),time=document.createElement("time");time.textContent=`${fmt(row.at,2)} s`;li.append(time,document.createTextNode(row.message));list.append(li);}}
function renderState(value){
  state=value;received=performance.now();
  set("connection","LOCAL PHYSICS");$("connection").classList.remove("offline");
  set("scene-name",{flat:"Flat ground",low_friction:"Low friction",sonar_dropout:"Sonar dropout"}[value.scenario]||value.scenario);
  set("world-time",`T + ${fmt(value.sim_time_s,3)} s`);
  const running=value.runtime?.running===true,status=value.status||{},metrics=value.metrics||{};
  set("scene-status",`${running?"RUNNING":"PAUSED"} / ${(status.state||value.mode||"unknown").toUpperCase()}`);
  set("mode-state",value.mode?.toUpperCase()||"—");
  set("play",running?"Pause simulation":"Run simulation");
  set("transport-note",status.reason||"Fixed timestep · no hardware connection");
  for(const button of document.querySelectorAll("[data-mode]"))button.classList.toggle("selected",button.dataset.mode===value.mode);
  set("height",`${fmt(metrics.root_height_m,3)} m`);set("tilt",`${fmt(finite(metrics.tilt_rad)?metrics.tilt_rad*180/Math.PI:null,1)}°`);
  set("travel",`${fmt(metrics.displacement_m,3)} m`);set("contacts",String(metrics.contacts??"—"));
  set("constraints",`Joint limits ${metrics.joint_limit_count??"—"} · target clips ${metrics.target_saturation_count??"—"}`);set("rtt",`RTT ${fmt(rtt,0)} ms`);
  const sonar=value.policy_observation?.sonar;
  set("sonar",sonar?.valid&&finite(sonar.distance_m)?`Stock-like simulated sonar: ${fmt(sonar.distance_m,3)} m. Capture age ${fmt(sonar.age_s,3)} s.`:"Simulated sonar unavailable / no echo. Unknown is not clear space.");
  set("policy",JSON.stringify(value.policy_observation,null,2));set("decision",value.controller_decision?JSON.stringify(value.controller_decision,null,2):"No physics step yet");set("truth",JSON.stringify(value.evaluation_truth,null,2));
  const description=`${status.state||value.mode}: ${status.reason||"no constraint reported"}`;
  if(description!==lastStatus){lastStatus=description;event(description);}
  if(value.sim_time_s<lastTime){history.length=0;event(`World reset · seed ${value.seed} · ${value.scenario}`);}
  if(value.runtime?.revision!==lastRevision){history.push({at:performance.now(),height:metrics.root_height_m});lastRevision=value.runtime?.revision;}
  while(history.length>240||history.length&&performance.now()-history[0].at>24000)history.shift();
  lastTime=value.sim_time_s;
  if(value.runtime?.error)error(value.runtime.error);
  if(value.runtime?.error){set("connection","PHYSICS ERROR");$("connection").classList.add("offline");set("scene-status","PHYSICS ERROR · LAST VALID STATE");}
  updateButtons();drawPlot();
}
function updateButtons(){const stale=!state||performance.now()-received>1500,fault=Boolean(state?.runtime?.error);for(const button of document.querySelectorAll("[data-mode],#play,#step"))button.disabled=busy||stale||fault;$("reset").disabled=busy||stale;$("step").disabled=busy||stale||fault||state?.runtime?.running;}
async function request(path,options={}){const result=await fetch(path,{cache:"no-store",...options});if(!result.ok){let detail;try{detail=(await result.json()).detail;}catch{}throw new Error(detail||`Local server HTTP ${result.status}`);}return result.json();}
async function control(action,extra={}){if(busy)return;busy=true;updateButtons();try{const next=await request("/api/control",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,...extra})});error("");renderState(next);}catch(e){error(e.message);}finally{busy=false;updateButtons();}}
$("play").onclick=()=>control(state?.runtime?.running?"pause":"play");
$("step").onclick=()=>control("step",{steps:10});
$("reset").onclick=()=>{const seed=Number($("seed").value);if(!Number.isInteger(seed)||seed<0||seed>2147483647){error("Seed must be an integer from 0 to 2147483647.");return;}control("reset",{seed,scenario:$("scenario").value});};
for(const button of document.querySelectorAll("[data-mode]"))button.onclick=()=>control("mode",{mode:button.dataset.mode});
$("reset-view").onclick=()=>{yaw=-1.03;pitch=.59;distance=.47;};
$("expand").onclick=()=>{$("scene").parentElement.requestFullscreen?.().catch(e=>error(e.message));};
canvas.onpointerdown=e=>{drag={x:e.clientX,y:e.clientY};canvas.setPointerCapture(e.pointerId);};
canvas.onpointermove=e=>{if(!drag)return;yaw-=(e.clientX-drag.x)*.007;pitch=Math.max(.15,Math.min(1.4,pitch+(e.clientY-drag.y)*.006));drag={x:e.clientX,y:e.clientY};};
canvas.onpointerup=canvas.onpointercancel=()=>{drag=null;};
canvas.addEventListener("wheel",e=>{e.preventDefault();distance=Math.max(.23,Math.min(1.4,distance*Math.exp(e.deltaY*.001)));},{passive:false});
canvas.onkeydown=e=>{if(e.key==="ArrowLeft")yaw-=.1;else if(e.key==="ArrowRight")yaw+=.1;else if(e.key==="ArrowUp")pitch=Math.min(1.4,pitch+.1);else if(e.key==="ArrowDown")pitch=Math.max(.15,pitch-.1);else if(e.key==="+")distance=Math.max(.23,distance-.03);else if(e.key==="-")distance=Math.min(1.4,distance+.03);else return;e.preventDefault();};
const dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0),add=(a,b)=>a.map((v,i)=>v+b[i]),scale=(a,s)=>a.map(v=>v*s);
function frame(){
  const rect=canvas.getBoundingClientRect(),dpr=Math.min(2,devicePixelRatio||1),w=rect.width,h=rect.height;
  if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);}
  ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
  const root=state?.evaluation_truth?.root_position_m||[0,0,.09],target=[root[0],root[1],.045];
  const right=[-Math.sin(yaw),Math.cos(yaw),0],up=[-Math.cos(yaw)*Math.sin(pitch),-Math.sin(yaw)*Math.sin(pitch),Math.cos(pitch)],forward=[-Math.cos(yaw)*Math.cos(pitch),-Math.sin(yaw)*Math.cos(pitch),-Math.sin(pitch)];
  const eye=add(target,scale(forward,-distance)),focal=Math.min(w,h)*1.2;
  function project(p){const q=p.map((v,i)=>v-eye[i]),z=dot(q,forward);return z>.015?{x:w/2+focal*dot(q,right)/z,y:h*.55-focal*dot(q,up)/z,z}:null;}
  function line(a,b,color,width=1){a=project(a);b=project(b);if(!a||!b)return;ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
  for(let i=-20;i<=20;i++){const v=i*.025;line([v,-.5,0],[v,.5,0],i%4===0?"#2a3942":"#1c2b33");line([-.5,v,0],[.5,v,0],i%4===0?"#2a3942":"#1c2b33");}
  line([0,0,.0005],[.07,0,.0005],"#b47966",2);line([0,0,.0005],[0,.07,.0005],"#79ae9a",2);
  const geoms=[...(state?.render_geoms||[])].filter(g=>g.type!=="plane").sort((a,b)=>dot(b.position,forward)-dot(a.position,forward));
  for(const g of geoms){
    if(!g.position?.every(finite)||!g.rotation?.every(finite))continue;
    const p=project(g.position);if(!p)continue;const r=g.rotation,s=g.size,c=g.rgba||[.5,.7,.65,1];
    const color=(shade=1)=>`rgba(${c.slice(0,3).map(v=>Math.round(Math.min(255,v*255*shade))).join(",")},${c[3]??1})`;
    const transform=v=>add(g.position,[dot(r.slice(0,3),v),dot(r.slice(3,6),v),dot(r.slice(6,9),v)]);
    if(g.type==="capsule"||g.type==="cylinder"){
      const a=project(transform([0,0,-s[1]])),b=project(transform([0,0,s[1]]));if(!a||!b)continue;
      ctx.lineCap="round";ctx.strokeStyle=color(.63);ctx.lineWidth=Math.max(2,2*s[0]*focal/p.z);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
      ctx.strokeStyle=color(1.1);ctx.lineWidth=Math.max(1,s[0]*focal/p.z);ctx.stroke();ctx.lineCap="butt";
    }else if(g.type==="sphere"){
      ctx.fillStyle=color();ctx.beginPath();ctx.arc(p.x,p.y,Math.max(1,s[0]*focal/p.z),0,Math.PI*2);ctx.fill();
    }else if(g.type==="box"){
      const vertices=[];for(let i=0;i<8;i++)vertices.push(project(transform([i&1?s[0]:-s[0],i&2?s[1]:-s[1],i&4?s[2]:-s[2]])));
      const faces=[[0,1,3,2],[4,6,7,5],[0,4,5,1],[2,3,7,6],[0,2,6,4],[1,5,7,3]].map((ids,i)=>({ids,i,z:ids.reduce((sum,k)=>sum+(vertices[k]?.z||0),0)/4})).sort((a,b)=>b.z-a.z);
      for(const face of faces){const points=face.ids.map(k=>vertices[k]);if(points.some(x=>!x))continue;ctx.beginPath();points.forEach((x,i)=>i?ctx.lineTo(x.x,x.y):ctx.moveTo(x.x,x.y));ctx.closePath();ctx.fillStyle=color([.55,1.05,.8,.7,.65,.9][face.i]);ctx.fill();ctx.strokeStyle="#263b40";ctx.lineWidth=.7;ctx.stroke();}
    }
  }
  const contacts=state?.evaluation_truth?.contacts||[];for(const contact of contacts){const p=project(contact.position_m);if(!p)continue;ctx.strokeStyle="#f1ca84";ctx.lineWidth=1.5;ctx.beginPath();ctx.arc(p.x,p.y,4,0,Math.PI*2);ctx.stroke();}
  if(!state){ctx.fillStyle="#728697";ctx.font="13px system-ui";ctx.textAlign="center";ctx.fillText("Connecting to the local physics engine…",w/2,h/2);}
  if(state&&performance.now()-received>1500){set("connection","DISCONNECTED");$("connection").classList.add("offline");set("scene-status","LAST RECEIVED STATE · STALE");updateButtons();}
  requestAnimationFrame(frame);
}
function drawPlot(){const plot=$("plot"),c=plot.getContext("2d"),w=plot.width,h=plot.height; c.clearRect(0,0,w,h);c.strokeStyle="#273640";for(const y of [25,60,95]){c.beginPath();c.moveTo(30,y);c.lineTo(w-20,y);c.stroke();}const values=history.map(x=>x.height).filter(finite);if(!values.length)return;const lo=Math.min(.03,...values),hi=Math.max(.13,...values),now=performance.now();c.strokeStyle="#9ee8cb";c.lineWidth=2;c.beginPath();let open=false;for(const row of history){if(!finite(row.height)){open=false;continue;}const x=30+(w-50)*(1-(now-row.at)/24000),y=h-15-(row.height-lo)/(hi-lo)*(h-30);if(open)c.lineTo(x,y);else c.moveTo(x,y);open=true;}c.stroke();c.fillStyle="#7d94a4";c.font="10px monospace";c.fillText(`${hi.toFixed(2)}`,4,13);c.fillText(`${lo.toFixed(2)}`,4,h-4);}
async function poll(){const started=performance.now();try{const value=await request("/api/state");rtt=performance.now()-started;if(linkError){error("");linkError=false;}renderState(value);}catch(e){linkError=true;error(`Physics link: ${e.message}. Showing last received state.`);}setTimeout(poll,Math.max(30,100-(performance.now()-started)));}
request("/api/specs").then(value=>set("specs",JSON.stringify(value,null,2))).catch(e=>set("specs",e.message));
frame();poll();
