
const $=id=>document.getElementById(id);
let W=300,H=300,battHist=[];
// Keep one latest state and one camera request/decode in flight. Neither path
// queues historical frames; hidden tabs release their streams and connections.
let latestState=null,observedState=null,stateStream=null,streamConnected=false;
let stateReconnectTimer=null;
let stateObservedAt=0,stateObservedMessages=0,stateRejected=0,stateReason="Waiting for a Pi state sample";
let clockReason="Waiting for browser–Pi calibration",clockProbeResult=null,renderError=null;
let stateMessages=0,stateHz=0,cameraFrames=0,cameraHz=0,metricStart=performance.now(),metricsReady=false;
let cameraAbort=null, cameraTimer=null, cameraRunning=false, cameraURL=null;
let cameraSequence=null, cameraReceivedAt=0, cameraAgeAtReceipt=0, cameraRTT=null;
let cameraCapturedAt=null;
let cameraReason="waiting for camera", cameraProvenance="unknown", cameraSession=0;
let clockAnchor=null, clockProbeRunning=false, clockProbeTimer=null;
let clockGeneration=0;
const CLOCK_MAX_AGE_MS=__CLOCK_MAX_AGE_MS__,CLOCK_MAX_RTT_MS=__CLOCK_MAX_RTT_MS__;
const CLOCK_MAX_OFFSET_MS=__CLOCK_MAX_OFFSET_MS__,CLOCK_WALL_STEP_MS=__CLOCK_WALL_STEP_MS__;
const CLOCK_DRIFT=__CLOCK_DRIFT__;

function nonnegative(value){return typeof value==="number"&&Number.isFinite(value)&&value>=0;}
function remoteBounds(anchor=clockAnchor){
  if(!anchor)return null;
  const elapsed=performance.now()-anchor.perf;
  if(!nonnegative(elapsed)||elapsed>CLOCK_MAX_AGE_MS||Math.abs(Date.now()-anchor.wall-elapsed)>CLOCK_WALL_STEP_MS)return null;
  const remote=anchor.wall+elapsed,drift=elapsed*CLOCK_DRIFT;
  return [remote+anchor.lower-drift,remote+anchor.upper+drift];
}
function stampAge(stamp){
  const bounds=remoteBounds();
  if(!bounds||typeof stamp!=="number"||!Number.isFinite(stamp)||stamp<=0)return Infinity;
  const age=bounds[1]-stamp*1000;
  return nonnegative(age)?age:Infinity;
}
function measureClock(state,wall,perf){
  const end=performance.now(),elapsed=end-perf,now=Date.now();
  if(!nonnegative(elapsed)||elapsed>CLOCK_MAX_RTT_MS)throw Error(`Clock probe round trip ${Math.round(elapsed)} ms exceeds ${CLOCK_MAX_RTT_MS} ms bound`);
  if(Math.abs(now-wall-elapsed)>CLOCK_WALL_STEP_MS)throw Error("Browser wall clock stepped during calibration");
  if(typeof state.server_at!=="number"||!Number.isFinite(state.server_at)||state.server_at<=0||
     typeof state.server_boot_id!=="string"||!state.server_boot_id)throw Error("Pi clock reply is malformed");
  const lower=state.server_at*1000-(wall+elapsed),upper=state.server_at*1000-wall;
  if(lower < -CLOCK_MAX_OFFSET_MS||upper > CLOCK_MAX_OFFSET_MS)throw Error(`Browser–Pi offset [${lower.toFixed(1)}, ${upper.toFixed(1)}] ms exceeds ±${CLOCK_MAX_OFFSET_MS} ms bound`);
  const previous=remoteBounds();
  if(clockAnchor&&clockAnchor.boot===state.server_boot_id&&
     (!previous||wall+elapsed+lower>previous[1]||wall+elapsed+upper<previous[0]))throw Error("Pi or browser wall clock stepped between probes");
  clockAnchor={wall:wall+elapsed,perf:end,lower,upper,boot:state.server_boot_id};
}
async function probeClock(){
  clearTimeout(clockProbeTimer);clockProbeTimer=null;
  if(document.hidden||clockProbeRunning)return;
  clockProbeRunning=true;
  const generation=clockGeneration,wall=Date.now(),perf=performance.now();
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),1000);
  try{
    const response=await fetch("/sim/clock",{cache:"no-store",signal:controller.signal});
    if(!response.ok)throw Error(`Pi clock HTTP ${response.status}; check matching server/dashboard versions`);
    const state=await response.json();
    if(!state||typeof state!=="object"||Array.isArray(state))throw Error("Pi clock reply is malformed");
    if(generation===clockGeneration){
      const elapsed=performance.now()-perf;
      clockProbeResult={rtt_ms:elapsed,offset_lower_ms:typeof state.server_at==="number"?state.server_at*1000-wall-elapsed:null,
        offset_upper_ms:typeof state.server_at==="number"?state.server_at*1000-wall:null,at:performance.now()};
      measureClock(state,wall,perf);clockReason="Browser–Pi clock calibrated";
    }
  }catch(e){if(generation===clockGeneration){clockAnchor=null;clockReason=e.name==="AbortError"?"Pi clock probe timed out":String(e.message).slice(0,220);}}
  finally{clearTimeout(timer);clockProbeRunning=false;
    if(!document.hidden){clearTimeout(clockProbeTimer);
      clockProbeTimer=setTimeout(probeClock,generation===clockGeneration?1000:0);}}
}
function serverAge(s){
  const elapsed=performance.now()-s.receivedPerf;
  if(!clockAnchor||clockAnchor.boot!==s.server_boot_id||!nonnegative(elapsed)||!nonnegative(s.serverAgeAtReceipt))return Infinity;
  return Math.max(stampAge(s.server_at),s.serverAgeAtReceipt+elapsed);
}
function receiptAge(s){
  // Server receipt and browser delivery are separate events. Include the time
  // the SSE frame spent buffered in the transport instead of resetting its age.
  if(!nonnegative(s.telemetry_age_ms))return Infinity;
  return Math.max(stampAge(s.brain?._received_at),s.telemetry_age_ms+serverAge(s));
}
function telemetryAge(s){
  const elapsed=performance.now()-s.receivedPerf;
  if(!nonnegative(elapsed)||!nonnegative(s.sourceAgeAtReceipt))return Infinity;
  return Math.max(receiptAge(s),stampAge(s.brain?._produced_at_pi_lower),s.sourceAgeAtReceipt+elapsed);
}
function inputTiming(s,h){
  if(!h||!nonnegative(h.age_s)||!nonnegative(h.max_age_s)||!nonnegative(h.captured_at)||h.captured_at===0)return {age:Infinity,fresh:false};
  // age_s is already the coordinator's conservative capture age in its source
  // clock domain. Add all subsequent source/transport delay; do not compare a
  // Jetson timestamp directly with the browser or Pi wall clock.
  const age=Math.max(h.clock?stampAge(h.captured_at):0,h.age_s*1000+telemetryAge(s))/1000;
  return {age,fresh:!!h.available&&!!h.fresh&&Number.isFinite(age)&&age<=h.max_age_s&&telemetryAge(s)<=3000};
}
let cameraFetchedSequence=null,cameraFetchedStamp=null,cameraPending=null,cameraDecoding=false,cameraReceivedFrames=0,cameraReceivedHz=0;
let cameraHeaderMs=null,cameraTransferMs=null,cameraDecodeMs=null,cameraQueueMs=null,cameraBytes=null,cameraDropped=0;
let cameraNaturalWidth=0,cameraNaturalHeight=0,lastHistoryAt=0;
let cameraDecoder=null;
let stopPending=false,messagePending=false,controlSequence=0,lastRenderedRevision=null;
const graphHistory=[];
let previewBoot=null;
function observePreviewBoot(boot){
  if(previewBoot===boot)return;
  previewBoot=boot;cameraSession++;cameraPending=null;
  cameraSequence=cameraFetchedSequence=cameraFetchedStamp=cameraCapturedAt=null;
  cameraReceivedAt=0;cameraAgeAtReceipt=Infinity;
  if(cameraDecoder)cameraDecoder.src="";
  if(cameraAbort)cameraAbort.abort();
  cameraReason="Waiting for preview from the observed Pi session";
}

function connectState(){
  if(document.hidden||stateStream)return;
  if(!clockAnchor&&!clockProbeRunning&&clockProbeTimer===null)probeClock();
  clearTimeout(stateReconnectTimer);stateReconnectTimer=null;
  const stream=new EventSource("/sim/events");stateStream=stream;
  stream.onopen=()=>{if(stateStream===stream)streamConnected=true;};
  stream.addEventListener("state",event=>{
    if(stateStream!==stream)return;
    try{
      const state=JSON.parse(event.data);
      if(!state||typeof state!=="object"||Array.isArray(state)||typeof state.server_boot_id!=="string"||!state.server_boot_id||
         !nonnegative(state.server_at)||state.server_at===0||!Number.isSafeInteger(state.revision)||state.revision<0)throw Error("Malformed Pi state envelope");
      stateObservedAt=performance.now();stateObservedMessages++;observePreviewBoot(state.server_boot_id);
      // Retain displayable diagnostics even when timing is untrusted. These
      // infinite ages never establish freshness or authorize Resume.
      observedState={...state,receivedPerf:stateObservedAt,serverAgeAtReceipt:Infinity,sourceAgeAtReceipt:Infinity};
      stateReason="Pi sample received; waiting for verified timing";
      if(clockAnchor&&clockAnchor.boot===state.server_boot_id&&!Number.isFinite(stampAge(state.server_at)))return;
      if(latestState&&latestState.server_boot_id===state.server_boot_id&&state.revision<latestState.revision)return;
      if(latestState&&latestState.server_boot_id===state.server_boot_id&&state.server_at<=latestState.server_at)return;
      if(!clockAnchor||clockAnchor.boot!==state.server_boot_id){
        if(clockAnchor){clockAnchor=null;clearTimeout(clockProbeTimer);probeClock();}
        return;
      }
      state.receivedPerf=performance.now();
      state.serverAgeAtReceipt=stampAge(state.server_at);
      state.sourceAgeAtReceipt=stampAge(state.brain?._produced_at_pi_lower);
      latestState=state;stateMessages++;stateReason="Pi state timing verified";
    }catch(e){stateRejected++;stateReason="Rejected Pi state: "+String(e.message).slice(0,180);}
  });
  stream.onerror=()=>{
    if(stateStream!==stream)return;
    streamConnected=false;stateReason="Pi state stream disconnected; retrying";
    // Native EventSource retries transient loss. Some failures close it
    // permanently; keep one bounded retry, without a historical event queue.
    if(stream.readyState===2){
      stream.close();stateStream=null;clearTimeout(stateReconnectTimer);
      if(!document.hidden)stateReconnectTimer=setTimeout(connectState,1000);
    }
  };
}

async function nextCamera(){
  if(document.hidden||cameraRunning)return;
  cameraRunning=true;
  const session=cameraSession,controller=new AbortController();cameraAbort=controller;
  const started=performance.now(),timeout=setTimeout(()=>controller.abort(),1500);
  let retry=100;
  try{
    const response=await fetch("/camera/frame",{cache:"no-store",signal:controller.signal});
    const headersAt=performance.now();
    if(!response.ok)throw Error(response.status===503?"Camera unavailable · inspect capture health":"Camera HTTP "+response.status);
    const sequence=response.headers.get("X-Crab-Sequence"),stamp=Number(response.headers.get("X-Crab-Captured-At"));
    const ageText=response.headers.get("X-Crab-Frame-Age-Ms"),age=ageText===null?NaN:Number(ageText);
    if(!sequence||!/^\d+$/.test(sequence)||!Number.isSafeInteger(Number(sequence))||Number(sequence)<=0||
       !nonnegative(age)||!Number.isFinite(stamp)||stamp<=0)throw Error("Camera timing metadata missing or malformed");
    const blob=await readJpeg(response),received=performance.now();
    if(session!==cameraSession||document.hidden)return;
    if(blob.size===0||blob.size>4*1024*1024)throw Error("JPEG outside preview size bound");
    if(cameraFetchedSequence!==null&&(Number(sequence)<=Number(cameraFetchedSequence)||stamp<=cameraFetchedStamp))return;
    {
      cameraFetchedSequence=sequence;cameraFetchedStamp=stamp;cameraReceivedFrames++;
      if(cameraPending)cameraDropped++;
      cameraPending={blob,sequence,stamp,age,session,started,received,headerMs:headersAt-started,
        transferMs:received-headersAt,provenance:response.headers.get("X-Crab-Provenance")||"unknown"};
      decodeCamera();
    }
    cameraReason="";
  }catch(error){if(session===cameraSession&&!document.hidden)cameraReason=error.name==="AbortError"?"Camera request timed out":String(error.message);retry=700;}
  finally{
    clearTimeout(timeout);cameraAbort=null;cameraRunning=false;
    if(!document.hidden)cameraTimer=setTimeout(nextCamera,Math.max(0,retry-(performance.now()-started)));
  }
}

async function readJpeg(response){
  const limit=4*1024*1024,length=response.headers.get("Content-Length");
  if(length!==null&&(!/^\d+$/.test(length)||Number(length)>limit))throw Error("JPEG outside preview size bound");
  if(!response.body?.getReader)throw Error("Browser does not support bounded preview reads");
  const reader=response.body.getReader(),chunks=[];let total=0;
  try{
    while(true){const {value,done}=await reader.read();if(done)break;total+=value.byteLength;
      if(total>limit||chunks.length>=2048){await reader.cancel();throw Error("JPEG outside preview size bound");}chunks.push(value);}
  }finally{reader.releaseLock();}
  return new Blob(chunks,{type:"image/jpeg"});
}

async function decodeCamera(){
  if(cameraDecoding)return;
  cameraDecoding=true;
  try{
    while(cameraPending&&!document.hidden){
      const frame=cameraPending;cameraPending=null;
      const url=URL.createObjectURL(frame.blob),decodeStarted=performance.now();let retained=false;
      try{
        const decoded=new Image();cameraDecoder=decoded;decoded.src=url;
        let decodeTimer;
        try{await Promise.race([decoded.decode(),new Promise((_,reject)=>{
          decodeTimer=setTimeout(()=>{decoded.src="";reject(Error("decode timed out"));},1000);
        })]);}finally{clearTimeout(decodeTimer);}
        if(frame.session!==cameraSession||document.hidden)continue;
        const now=performance.now(),oldURL=cameraURL;cameraURL=url;retained=true;
        $("cam").src=url;if(oldURL)URL.revokeObjectURL(oldURL);
        cameraNaturalWidth=decoded.naturalWidth;cameraNaturalHeight=decoded.naturalHeight;
        cameraSequence=frame.sequence;cameraReceivedAt=now;cameraCapturedAt=frame.stamp;
        cameraAgeAtReceipt=Math.max(frame.age+now-frame.started,stampAge(frame.stamp));
        cameraRTT=frame.received-frame.started;cameraHeaderMs=frame.headerMs;cameraTransferMs=frame.transferMs;
        cameraDecodeMs=now-decodeStarted;cameraQueueMs=decodeStarted-frame.received;cameraBytes=frame.blob.size;
        cameraProvenance=frame.provenance;cameraFrames++;
        $("camera-empty").hidden=true;
      }catch(error){if(frame.session===cameraSession&&!document.hidden)cameraReason="JPEG decode failed: "+String(error.message);}
      finally{cameraDecoder=null;if(!retained)URL.revokeObjectURL(url);}
    }
  }finally{cameraDecoding=false;}
}

function pauseFeeds(){
  if(stateStream)stateStream.close();stateStream=null;streamConnected=false;
  clearTimeout(stateReconnectTimer);stateReconnectTimer=null;
  cameraSession++;cameraPending=null;cameraFetchedSequence=null;cameraFetchedStamp=null;
  if(cameraDecoder)cameraDecoder.src="";
  if(cameraAbort)cameraAbort.abort();clearTimeout(cameraTimer);
  stateMessages=cameraFrames=cameraReceivedFrames=0;metricStart=performance.now();metricsReady=false;
  clockGeneration++;clockAnchor=null;clearTimeout(clockProbeTimer);clockProbeTimer=null;
}
function resumeFeeds(){if(!document.hidden){probeClock();connectState();nextCamera();}}
function numeric(value){return typeof value==="number"&&Number.isFinite(value)?value:null;}
function fmt(value,digits=0,suffix=""){return numeric(value)===null?"—":value.toFixed(digits)+suffix;}
function esc(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function text(id,value){const node=$(id),str=String(value??"");if(node&&node.textContent!==str)node.textContent=str;}
function html(id,value){const node=$(id);if(node&&node.innerHTML!==value)node.innerHTML=value;}
function list(value){return Array.isArray(value)?value:[];}
function clockWidth(link){const c=link?.clock;return c&&numeric(c.offset_lower_s)!==null&&numeric(c.offset_upper_s)!==null&&c.offset_upper_s>=c.offset_lower_s?(c.offset_upper_s-c.offset_lower_s)*1000:null;}
function frameAge(now=performance.now()){return cameraReceivedAt?Math.max(cameraAgeAtReceipt+now-cameraReceivedAt,stampAge(cameraCapturedAt)):Infinity;}
function hostValue(s,host,key){
  const sample=host==="pi"?s?.host_vitals:s?.brain?.host_vitals;
  const delivery=host==="pi"?serverAge(s):telemetryAge(s);
  return sample?.host===host&&nonnegative(sample.age_s)&&sample.age_s*1000+delivery<=3000?numeric(sample[key]):null;
}
function sampleGraphs(s,now){
  if(!s||now-lastHistoryAt<1000)return;
  lastHistoryAt=now;
  const b=s.brain||{},live=telemetryAge(s)<=3000,link=b.pipeline?.transport;
  const cameraLive=frameAge(now)<=2000,piFresh=serverAge(s)<=2000,previewRecent=cameraReceivedAt>0&&now-cameraReceivedAt<=2000;
  const battery=inputTiming(s,b.inputs?.battery),sonar=inputTiming(s,b.inputs?.ultrasonic);
  const point={at:now,boot:s.server_boot_id,battery:battery.fresh?numeric(b.battery_v):null,
    sonar:sonar.fresh?numeric(b.distance_cm):null,capture:cameraLive&&piFresh?numeric(s.camera_capture?.capture_fps):null,
    encoded:cameraLive&&piFresh?numeric(s.camera_capture?.encoded_fps):null,preview:previewRecent&&metricsReady?numeric(cameraHz):null,
    age:cameraLive?frameAge(now):null,rtt:previewRecent?numeric(cameraRTT):null,
    ping:live&&link?.ready?numeric(link.last_rtt_ms):null,uncertainty:live&&link?.ready?clockWidth(link):null};
  for(const host of ["pi","jetson"])for(const key of ["cpu_percent","memory_used_mib","temperature_c"])
    point[host+"_"+key]=hostValue(s,host,key);
  graphHistory.push(point);while(graphHistory.length>121)graphHistory.shift();
  drawGraphs(now);
}
function drawGraph(id,series,range,now){
  const canvas=$(id);if(!canvas)return;
  const width=canvas.clientWidth||360,height=canvas.clientHeight||150,dpr=Math.min(window.devicePixelRatio||1,2);
  if(canvas.width!==Math.round(width*dpr)||canvas.height!==Math.round(height*dpr)){canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);}
  const g=canvas.getContext("2d");g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,width,height);
  let [low,high]=range;
  if(high===null){const values=graphHistory.flatMap(p=>series.map(line=>p[line[0]])).filter(v=>numeric(v)!==null);high=Math.max(1,...values)*1.15;}
  const left=36,right=9,top=11,bottom=25,w=width-left-right,h=height-top-bottom;
  g.font="10px system-ui";g.fillStyle="#91a6b5";g.strokeStyle="#263642";g.lineWidth=1;
  for(const ratio of [0,.5,1]){const y=top+h*ratio;g.beginPath();g.moveTo(left,y);g.lineTo(width-right,y);g.stroke();g.fillText((high-(high-low)*ratio).toFixed(high-low<5?1:0),0,y+3);}
  g.fillText("−120s",left,height-5);g.fillText("now",width-28,height-5);
  for(const [key,color] of series){
    g.strokeStyle=color;g.lineWidth=1.8;g.beginPath();let active=false,previous=null;
    for(const p of graphHistory){
      const value=p[key],x=left+w*(1-(now-p.at)/120000);
      if(x<left||numeric(value)===null){active=false;previous=p;continue;}
      const y=top+h*(1-Math.max(0,Math.min(1,(value-low)/(high-low))));
      if(!active||!previous||p.at-previous.at>2200||p.boot!==previous.boot)g.moveTo(x,y);else g.lineTo(x,y);
      active=true;previous=p;
    }g.stroke();
  }
  if(!graphHistory.some(p=>series.some(line=>numeric(p[line[0]])!==null))){g.fillStyle="#9aafbd";g.font="12px system-ui";g.fillText("Waiting for valid samples",left+8,top+h*.6);}
}
function drawGraphs(now){
  const cyan="#79d8dc",violet="#baa9ef",amber="#edbf79",green="#8bd4b3";
  for(const [id,series,range] of [
    ["battery-graph",[["battery",amber]],[6,8.5]],["sonar-graph",[["sonar",cyan]],[0,200]],
    ["fps-graph",[["capture",green],["encoded",cyan],["preview",violet]],[0,30]],
    ["latency-graph",[["age",amber],["rtt",violet]],[0,null]],
    ["network-graph",[["ping",cyan],["uncertainty",amber]],[0,null]],
    ["cpu-graph",[["pi_cpu_percent",cyan],["jetson_cpu_percent",violet]],[0,100]],
    ["memory-graph",[["pi_memory_used_mib",cyan],["jetson_memory_used_mib",violet]],[0,null]],
    ["temperature-graph",[["pi_temperature_c",cyan],["jetson_temperature_c",violet]],[0,100]]])drawGraph(id,series,range,now);
}
function intentLabel(intent){
  const result=intent.result||{},response=result.response||{},motion=response.motion;
  if(intent.status==="blocked"||intent.status==="failed"||intent.status==="cancelled")return intent.status;
  if(motion){if(response.status?.simulate)return "simulated result";return motion.completed_cycles?"vendor result · unmeasured":"no motion executed";}
  if(intent.kind==="acknowledge"&&result.sound_queued===false&&result.speech_queued===false)return "output not emitted";
  return intent.status==="completed"?"handled":intent.status||"unknown";
}
function systemCard(title,value,status,kind,health,use,reason,next){
  return `<article class="system-card"><div class="system-title"><h3>${esc(title)}</h3><span class="badge ${esc(kind)}">${esc(status)}</span></div><strong>${esc(value)}</strong><p>${esc(reason)}</p><dl><dt>Source</dt><dd>${esc(health?.provenance||"unknown")}</dd><dt>Current use</dt><dd>${esc(use)}</dd><dt>Verification</dt><dd>${esc(health?.provenance==="simulated"?"simulation only":"not hardware acceptance")}</dd></dl><p class="next-action">${esc(next)}</p></article>`;
}
function renderSystems(s,b,live){
  const inputs=b.inputs||{},cards=[];
  for(const [name,title,value] of [["ultrasonic","Sonar",numeric(b.distance_cm)===null?"No reading":fmt(b.distance_cm,1," cm")],
    ["camera","Camera",cameraNaturalWidth?`${cameraNaturalWidth} × ${cameraNaturalHeight}`:"No frame"],
    ["microphone","Microphone",b.hearing?.available?"STT available":"Not listening"],
    ["speaker","Speaker",b.audio?.effects_enabled?"Effects configured":"Output disabled"]]){
    let h=inputs[name]||{},timing=inputTiming(s,h),sourceLive=live;
    if(name==="camera"){h={...h,available:s.camera_capture?.available===true,provenance:s.camera_capture?.provenance||"unknown"};timing={fresh:frameAge()<=2000};sourceLive=serverAge(s)<=2000;}
    const failed=!!h.failure_reason;
    const status=!sourceLive?"UNKNOWN":!h.available?"UNAVAILABLE":!timing.fresh?"STALE / IDLE":failed?"FAILED":"AVAILABLE";
    const reason=(name==="camera"?s.camera_capture?.reason:null)||h.failure_reason||h.ignored_reason||(!h.available?"No health evidence":!timing.fresh?"No fresh capture":"Capture timing valid");
    const use=h.used&&timing.fresh&&live?"used":"not used";
    let next=name==="ultrasonic"?"Missing echoes do not mean clear space.":name==="camera"?"Preview does not imply detector use.":name==="microphone"?"STT and capture are separate opt-in checks.":"Process success does not prove audible sound.";
    if(name==="speaker"&&b.audio?.last_result?.ok===false)next=b.audio.last_result.reason||next;
    cards.push(systemCard(title,value,status,status==="AVAILABLE"?"neutral":status==="FAILED"?"bad":"warn",h,use,reason,next));
  }
  const perception=b.pipeline?.streams?.perception||{},interpretationFresh=inputTiming(s,inputs.interpretation).fresh;
  cards.push(systemCard("Vision",b.inputs?.interpretation?.used?"Attention labels":"Not interpreting",live&&perception.available&&interpretationFresh?"AVAILABLE":"NOT RUNNING","neutral",inputs.interpretation,
    inputs.interpretation?.used&&live&&interpretationFresh?"attention only":"not used",perception.failure_reason||"No fresh detector output", "A preview is not world understanding."));
  cards.push(systemCard("Language",b.mind?.available?"Local mind":"Not running",live&&b.mind?.available?(b.mind.busy?"WORKING":"AVAILABLE"):"NOT RUNNING","neutral",{provenance:b.mind?.provenance},
    b.mind?.busy&&live?"interpreting":"not used",b.mind?.failure_reason||"Optional local model", "Typed interaction works without a language model."));
  html("system-cards",cards.join(""));
}
function renderState(s){
  const b=s.brain&&typeof s.brain==="object"&&!Array.isArray(s.brain)?s.brain:{};
  const sourceAge=telemetryAge(s),live=sourceAge<=3000,link=b.pipeline?.transport,piFresh=serverAge(s)<=2000;
  const runtime=s.robot_runtime||{},sim=typeof runtime.simulate==="boolean"?runtime.simulate:typeof b.simulate==="boolean"?b.simulate:null;
  const motion=runtime.motion||{};const stopped=motion.stop_latched===true||b.stopped===true;
  const hasCompanion=Object.keys(b).length>0;
  let phase;
  if(!piFresh)phase="Pi timing unverified";
  else if(!hasCompanion)phase="Companion not publishing";
  else if(!Number.isFinite(sourceAge))phase="Companion timing unverified";
  else if(!live)phase="Companion telemetry stale";
  else if(stopped)phase="Stopped · fresh resume required";
  else phase=b.episode?.phase||"Waiting for companion";
  let blocked;
  if(live)blocked=b.blocked_reason||"Stillness is intentional. No autonomous roaming.";
  else if(!piFresh)blocked="Pi packets are displayable diagnostics; capture age is unverified. Resume stays disabled.";
  else if(!hasCompanion)blocked="Pi is connected. Start the Jetson companion with dashboard publishing enabled.";
  else blocked="Companion samples have no verified fresh timing. Inspect Pi–Jetson transport and clock details.";
  text("petname",b.name||"Crab");text("episode",phase);text("blocked",blocked);
  text("provenance",sim===true?(piFresh?"EXPLICIT SIMULATION":"REPORTED SIMULATION"):sim===false?(piFresh?"REAL INPUTS · UNVERIFIED":"REPORTED REAL · AGE UNKNOWN"):"SOURCE UNKNOWN");
  $("provenance").className="badge "+(sim===true?"sim":"neutral");
  text("motion-status",!piFresh?"MOTION UNKNOWN":sim===true?"VIRTUAL MOTION ONLY":motion.enabled===true?"ACTUATORS ENABLED":motion.enabled===false?"ACTUATORS DISABLED":"MOTION UNKNOWN");
  $("motion-status").className="badge "+(!piFresh?"warn":sim===true?"sim":motion.enabled===true?"bad":"neutral");
  text("pi-link",piFresh&&streamConnected?"State feed live":piFresh?"State fresh · reconnecting":"No fresh Pi state");text("pi-link-detail",`SSE ${stateHz.toFixed(1)} updates/s · ${fmt(serverAge(s),0," ms")} upper age`);
  text("jetson-link",live&&link?.ready?"WebSocket ready":live?"Link unavailable":hasCompanion?"Samples received · timing unverified":"No companion samples");
  text("jetson-link-detail",link?.failure_reason||`Status age ${fmt(link?.status_capture_age_s,2," s")} · ping ${fmt(link?.last_rtt_ms,1," ms")}`);
  const c=link?.clock,width=clockWidth(link);
  text("clock-value",live&&width!==null?`[${fmt(c.offset_lower_s*1000,1)}, ${fmt(c.offset_upper_s*1000,1)}] ms`:"Unmeasured");
  text("clock-detail",live&&width!==null?`Pi − Jetson · probe width ${fmt(width,1," ms")}`:"No fresh Pi–Jetson clock evidence");
  $("resume-review").disabled=!!renderError||stopPending||!(piFresh&&streamConnected&&live&&link?.ready&&stopped);text("mode",b.mode||"stationary");
  const active=list(b.intentions).find(i=>i&&["active","pending"].includes(i.status));
  text("current-intent",!live?"Waiting for fresh evidence":active?String(active.kind).replaceAll("_"," "):stopped?"Remain stopped":b.episode?.phase==="listen"?"Listening window":"Settle nearby");
  text("intent-reason",!live?"No behavior is inferred from stale telemetry.":active?.reason||b.blocked_reason||"No movement request active. Stillness is intentional.");
  text("mood",b.mood?String(b.mood).slice(0,1).toUpperCase():"·");
  text("last-response",b.last_say||"No response recorded.");
  const audio=b.audio?.last_result;text("response-outcome",!audio?"Output is not verified audible.":audio.ok===true?"Playback request succeeded · audibility unverified":audio.reason||"Output was not emitted or not confirmed");
  const intentions=list(b.intentions).filter(i=>i&&typeof i==="object").slice(-8).reverse();
  html("timeline",intentions.length?intentions.map(i=>`<li><strong>${esc(String(i.kind||"intention").replaceAll("_"," "))}</strong><span class="result-state">${esc((live?"":"last reported · ")+intentLabel(i))}</span><p>${esc(i.result?.reason||i.reason||"No outcome detail")}</p><time>${esc(i.source||"unknown source")}${i.created_at?" · "+esc(new Date(i.created_at*1000).toLocaleTimeString()):""}</time></li>`).join(""):'<li class="empty">No intentions recorded. Start the companion and send a message.</li>');text("timeline-count",intentions.length);
  const battery=inputTiming(s,b.inputs?.battery),sonar=inputTiming(s,b.inputs?.ultrasonic);
  text("battery-value",battery.fresh?fmt(b.battery_v,2," V"):"Unknown / stale");text("sonar-value",sonar.fresh?fmt(b.distance_cm,1," cm"):"No fresh reading");
  const cam=s.camera_capture||{},age=frameAge();
  text("capture-rate",`${fmt(piFresh?cam.capture_fps:null,1)} / ${fmt(piFresh?cam.encoded_fps:null,1)} fps`);
  text("latency-value",Number.isFinite(age)?`${fmt(age,0)} / ${fmt(cameraRTT,0)} ms`:"Unknown / stale");
  text("network-value",live&&link?.ready?`${fmt(link.last_rtt_ms,1)} / ${fmt(width,1)} ms`:"Unknown");
  for(const [id,key,unit,digits] of [["cpu-value","cpu_percent","%",1],["memory-value","memory_used_mib"," MiB",0],["temperature-value","temperature_c"," °C",1]])
    text(id,`${fmt(hostValue(s,"pi",key),digits,unit)} / ${fmt(hostValue(s,"jetson",key),digits,unit)}`);
  const revision=s.server_boot_id+":"+s.revision;
  if(lastRenderedRevision!==revision){if(battery.fresh&&numeric(b.battery_v)!==null){battHist.push(b.battery_v);if(battHist.length>120)battHist.shift();}lastRenderedRevision=revision;}
  renderSystems(s,b,live);
  html("inputs",Object.entries(b.inputs||{}).map(([name,h])=>{const timing=inputTiming(s,h);return `<tr><td>${esc(name)}</td><td>${esc(h?.provenance||"unknown")} · ${h?.available?(timing.fresh?"fresh":Number.isFinite(timing.age)?"stale":"timing unknown"):"unavailable"}${Number.isFinite(timing.age)?" · "+timing.age.toFixed(1)+"s":""}</td><td>${esc(h?.used&&timing.fresh?"used":h?.ignored_reason||h?.failure_reason||"not used")}</td></tr>`;}).join(""));
  text("evidence",sim===true?"Explicit simulation · no physical acceptance":"Hardware acceptance remains separate from input availability");
  $("simulator").hidden=!s.enabled;if(s.enabled&&$("simulator").open&&s.robot)drawMap(s);
  if($("diagnostics").open){text("saved-narrative",b.character||"No saved narrative");text("intentions",JSON.stringify(b.intentions||[],null,2));text("audio-health",JSON.stringify({output:b.audio,hearing:b.hearing,mind:b.mind},null,2));text("raw-state",JSON.stringify(s,null,2));}
  text("feed-stats",`Pi WebSocket ${link?.ready?"ready":link?.connected?"waiting for status":"disconnected"} · round trip ${link?.last_rtt_ms??"—"}ms · source age ${Number.isFinite(sourceAge)?Math.round(sourceAge)+"ms":"unknown"}${link?.failure_reason?" · "+link.failure_reason:""}`);
}

async function postJSON(path,payload,timeout=1800){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
  try{const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),signal:controller.signal});
    const result=await response.json();if(!response.ok)throw Error(result.reason||result.detail||"HTTP "+response.status);return result;
  }finally{clearTimeout(timer);}
}
async function sendText(value){
  const message=value||$("engagement").value.trim();if(!message||messagePending||stopPending)return;
  const sequence=++controlSequence;messagePending=true;$("send-control").disabled=true;
  try{const result=await postJSON("__EVENT_PATH__",{text:message});
    if(sequence===controlSequence){text("input-result",result.ok?"Input delivered locally · awaiting behavior outcome":result.reason||"Input rejected");if(result.ok&&!value)$("engagement").value="";}
  }catch(error){if(sequence===controlSequence)text("input-result","Input not confirmed: "+error.message);}
  finally{messagePending=false;$("send-control").disabled=stopPending;}
}
async function stopCompanion(){
  if(stopPending)return;stopPending=true;$("send-control").disabled=true;const sequence=++controlSequence;
  $("stop-control").setAttribute("aria-busy","true");$("resume-confirm").hidden=true;
  text("input-result","STOP requested · awaiting Pi acknowledgement");
  let acknowledged=false;
  try{const result=await postJSON("__STOP_PATH__",{});acknowledged=result.ok===true;
    if(sequence===controlSequence)text("input-result",result.ok?"STOP acknowledged by Pi · wait for current status":result.detail||"Stop not confirmed");
    // Preserve the existing explicit stop input as a coordinator boundary too.
    if(result.ok){const delivered=await postJSON("__EVENT_PATH__",{text:"stop"});if(!delivered.ok&&sequence===controlSequence)text("input-result","Pi STOP acknowledged · companion input rejected: "+(delivered.reason||"unknown reason"));}
  }catch(error){if(sequence===controlSequence)text("input-result",acknowledged?"Pi STOP acknowledged · companion input delivery unconfirmed. "+error.message:"STOP unconfirmed · use the physical cutoff if needed. "+error.message);}
  finally{stopPending=false;$("send-control").disabled=messagePending;$("stop-control").removeAttribute("aria-busy");}
}
function ctl(action){if(latestState?.enabled)postJSON("/sim/control",{action}).catch(error=>text("input-result","Simulator control failed: "+error.message));}

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


function renderTransport(now){
  const clockValid=remoteBounds()!==null,observedRecent=stateObservedAt>0&&now-stateObservedAt<1500;
  const acceptedFresh=latestState&&serverAge(latestState)<=2000;
  const issue=renderError?`Dashboard display error: ${renderError}. STOP remains available.`:
    !clockValid?`Timing unverified: ${clockReason==="Browser–Pi clock calibrated"?"clock anchor expired":clockReason}. Capture ages and Resume remain unavailable.`:
    !streamConnected?"Pi state stream disconnected. Reconnecting automatically; cached values are not current health.":
    stateReason.startsWith("Rejected")?stateReason:null;
  $("runtime-alert").hidden=!issue;text("runtime-alert",issue||"");
  $("runtime-alert").className="runtime-alert "+(renderError?"error":"warning");
  text("feed-state",!streamConnected?"Pi stream disconnected":observedRecent?(clockValid?"live connection":"Pi stream connected · timing unverified"):"Pi stream open · waiting for sample");
  if(!acceptedFresh||!clockValid||!streamConnected||renderError)$("resume-review").disabled=true;
  if(!acceptedFresh){
    text("pi-link",observedRecent?"Pi samples arriving · timing unverified":streamConnected?"Pi stream open · waiting":"No fresh Pi state");
    text("pi-link-detail",`${stateObservedMessages} packets received · ${stateRejected} rejected · ${stateReason}`);
  }
  text("browser-clock-detail",clockValid?`Browser–Pi: calibrated · ${fmt(clockProbeResult?.rtt_ms,1)} ms round trip`:`Browser–Pi: ${clockReason}`);
  if(!latestState&&!observedState){text("episode",observedRecent?"Verifying Pi timing":"Waiting for Pi state");text("blocked",issue||"Waiting for the first Pi state sample; companion health is not yet known.");}
  if($("diagnostics").open)text("frontend-health",JSON.stringify({clock_valid:clockValid,clock_reason:clockReason,
    probe:clockProbeResult,stream_connected:streamConnected,packets_received:stateObservedMessages,rejected:stateRejected,
    last_packet_age_ms:stateObservedAt?Math.round(now-stateObservedAt):null,state_reason:stateReason,display_error:renderError},null,2));
}
function renderPreviewRates(now){
  const state=latestState||observedState,piFresh=state&&serverAge(state)<=2000;
  const recent=cameraReceivedAt>0&&now-cameraReceivedAt<=2000;
  text("preview-rate",`${fmt(metricsReady?cameraReceivedHz:null,1)} / ${fmt(metricsReady?cameraHz:null,1)} fps`);
  text("fps-value",`${fmt(piFresh?state.camera_capture?.capture_fps:null,1)} / ${fmt(recent&&metricsReady?cameraHz:null,1)} fps`);
}
let lastPaint=0;
function paint(now){
  try{
  if(now-lastPaint>=250&&!document.hidden){
    lastPaint=now;
    if(now-metricStart>=1000){const seconds=(now-metricStart)/1000;stateHz=stateMessages/seconds;cameraHz=cameraFrames/seconds;cameraReceivedHz=cameraReceivedFrames/seconds;stateMessages=cameraFrames=cameraReceivedFrames=0;metricStart=now;metricsReady=true;}
    const displayState=latestState&&Number.isFinite(serverAge(latestState))?latestState:(observedState||latestState);
    if(displayState){renderState(displayState);sampleGraphs(displayState,now);}else if(now-lastHistoryAt>=1000){drawGraphs(now);lastHistoryAt=now;}
    renderPreviewRates(now);renderTransport(now);
    const age=frameAge(now),recent=cameraReceivedAt>0&&now-cameraReceivedAt<=2000;$("cam").style.opacity=recent?"1":".4";
    text("camera-age",Number.isFinite(age)?Math.round(age)+" ms":"Unknown");
    text("camera-source",`${cameraProvenance.toUpperCase()} / ${!Number.isFinite(age)?"TIMING UNVERIFIED":age>2000?"STALE":"ENCODED FRAME"}`);
    const displayWidth=$("cam").getBoundingClientRect().width,scale=cameraNaturalWidth?displayWidth/cameraNaturalWidth:null;
    text("camera-dimensions",cameraNaturalWidth?`${cameraNaturalWidth} × ${cameraNaturalHeight} · ${fmt(scale,2)}×`:"—");
    text("camera-stats",cameraReason||(!Number.isFinite(age)?"JPEG preview received · capture timing unverified; inspect the timing banner":age>2000?"Last frame is stale · inspect capture and transport":"Newest decoded preview · encoded age excludes exposure and encode delay"));
    html("camera-timing",[["Headers / first-byte wait",fmt(cameraHeaderMs,1," ms")],["Body transfer",fmt(cameraTransferMs,1," ms")],["JPEG decode",fmt(cameraDecodeMs,1," ms")],["Decode-slot wait",fmt(cameraQueueMs,1," ms")],["JPEG payload",cameraBytes===null?"—":fmt(cameraBytes/1024,1," KiB")],["Replaced before decode",String(cameraDropped)]].map(([label,value])=>`<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join(""));
    const config=latestState?.camera_capture;
    const compression=config?.encoder_requested==="mjpeg_hw"&&config.quality_applied===false?`${fmt(numeric(config.requested_bitrate_bps)===null?null:config.requested_bitrate_bps/1e6,2)} Mbps target · JPEG quality not applied`:`JPEG quality ${config?.requested_quality??"—"}${config?.quality_applied===false?" (not applied by this encoder API)":""}`;
    text("camera-config",config?`${config.sensor_model||"Sensor model unreported"} · ${config.encoder||"encoder unknown"} · requested ${config.requested_width??"—"}×${config.requested_height??"—"} @ ${config.requested_fps??"—"} fps · ${compression}. Capture cadence: ${config.capture_fps_source||"unmeasured"}.`:"Capture configuration has not arrived.");
    renderError=null;
  }
  }catch(error){
    renderError=String(error.message).slice(0,220);$("resume-review").disabled=true;$("resume-confirm").hidden=true;
    renderTransport(now);
  }finally{requestAnimationFrame(paint);}
}

document.addEventListener("visibilitychange",()=>document.hidden?pauseFeeds():resumeFeeds());
window.addEventListener("pagehide",pauseFeeds);window.addEventListener("pageshow",resumeFeeds);
window.addEventListener("resize",()=>drawGraphs(performance.now()));
function syncSafetyHeight(){document.documentElement.style.setProperty("--safety-height",document.querySelector(".safety-bar").offsetHeight+"px");}
if(typeof ResizeObserver!=="undefined")new ResizeObserver(syncSafetyHeight).observe(document.querySelector(".safety-bar"));
window.addEventListener("resize",syncSafetyHeight);syncSafetyHeight();
$("engagement-form").addEventListener("submit",event=>{event.preventDefault();sendText();});
document.querySelectorAll("[data-message]").forEach(button=>button.addEventListener("click",()=>sendText(button.dataset.message)));
document.querySelectorAll("[data-stop]").forEach(button=>button.addEventListener("click",stopCompanion));
$("resume-review").addEventListener("click",()=>{if(stopPending)return;$("resume-confirm").hidden=false;$("resume-confirm-button").focus();});
$("resume-cancel").addEventListener("click",()=>{$("resume-confirm").hidden=true;$("resume-review").focus();});
$("resume-confirm-button").addEventListener("click",()=>{$("resume-confirm").hidden=true;if(renderError||stopPending||!streamConnected||!latestState||serverAge(latestState)>2000||telemetryAge(latestState)>3000||!latestState.brain?.pipeline?.transport?.ready){text("input-result","Resume not sent · fresh connected status required");return;}sendText("resume");});
$("camera-size").addEventListener("click",()=>{const native=$("camera-panel").classList.toggle("native");$("camera-size").setAttribute("aria-pressed",String(native));text("camera-size",native?"Fit · no upscale":"Native 1:1");});
$("camera-expand").addEventListener("click",()=>{const expanded=$("camera-panel").classList.toggle("expanded");document.body.classList.toggle("expanded-view",expanded);$("camera-expand").setAttribute("aria-expanded",String(expanded));text("camera-expand",expanded?"Close view":"Expand");});
document.addEventListener("keydown",event=>{if(event.key==="Escape"&&$("camera-panel").classList.contains("expanded"))$("camera-expand").click();});
document.querySelectorAll("[data-world]").forEach(button=>button.addEventListener("click",()=>ctl(button.dataset.world)));
$("map").addEventListener("click",event=>{if(!latestState?.enabled)return;const r=$("map").getBoundingClientRect();postJSON("/sim/obstacle",{action:event.shiftKey?"remove":"add",cx:(event.clientX-r.left)/r.width*W,cy:H-(event.clientY-r.top)/r.height*H,r:14}).catch(error=>text("input-result",error.message));});
renderSystems({brain:{}},{},false);resumeFeeds();requestAnimationFrame(paint);
