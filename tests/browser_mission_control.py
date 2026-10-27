"""Synthetic operations UI states/screenshots; explicit loopback simulation only."""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
from PIL import Image, ImageDraw

from browser_live_feed import CDP, ROOT, port, wait_url


def pattern():
    image = Image.new("RGB", (960, 720), "#132431")
    draw = ImageDraw.Draw(image)
    for x in range(0, 960, 40):
        draw.line((x, 0, x, 720), fill="#2d485a", width=1)
    for y in range(0, 720, 40):
        draw.line((0, y, 960, y), fill="#2d485a", width=1)
    colors = ["#c1d6e0", "#719ba6", "#c5a46e", "#9f7773", "#788293", "#456c66"]
    for i, color in enumerate(colors):
        draw.rectangle((80+i*130, 100, 200+i*130, 300), fill=color)
    for step in range(1, 30):
        draw.line((80, 370+step*6, 880, 370+step*6), fill="#d9e8ee", width=1 if step%3 else 2)
    draw.rectangle((80, 585, 880, 650), fill="#09151e")
    draw.text((110, 610), "SYNTHETIC CAMERA TEST PATTERN / 960 x 720 / NO PHYSICAL CAMERA", fill="#d6e5ec")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=80)
    return base64.b64encode(output.getvalue()).decode()


FIXTURE_JS = r"""
stateStream.close();stateStream=null;
window.fixtureBase=JSON.parse(JSON.stringify(latestState));window.fixtureMode='real';window.fixtureStopped=false;
window.fixtureHardware=false;window.fixtureStopError=false;window.fixtureFailure=false;window.fixturePosts=[];window.fixtureDelay=0;window.fixtureDecodeDelay=0;
window.fixtureActive=0;window.fixturePeak=0;window.fixtureSequence=1000;
const originalFetch=window.fetch.bind(window),NativeImage=window.Image;
window.Image=function(){const image=new NativeImage(),decode=image.decode.bind(image);image.decode=async()=>{if(fixtureDecodeDelay)await new Promise(r=>setTimeout(r,fixtureDecodeDelay));return decode();};return image;};
const jpeg=Uint8Array.from(atob('__PATTERN__'),c=>c.charCodeAt(0));
window.fetch=async(url,options={})=>{
  if(String(url)==='/camera/frame'){
    fixtureActive++;fixturePeak=Math.max(fixturePeak,fixtureActive);
    try{if(fixtureDelay)await new Promise(r=>setTimeout(r,fixtureDelay));
      return new Response(jpeg,{headers:{'Content-Type':'image/jpeg','Content-Length':String(jpeg.length),
        'X-Crab-Sequence':String(++fixtureSequence),'X-Crab-Captured-At':String(realDateNow()/1000-.025),
        'X-Crab-Frame-Age-Ms':'25','X-Crab-Provenance':'simulated'}});
    }finally{fixtureActive--;}
  }
  if(options.method==='POST'&&(String(url)==='/stop'||String(url)==='/companion/events')){
    const body=JSON.parse(options.body);fixturePosts.push({url:String(url),body,at:performance.now()});
    if(String(url)==='/stop'&&fixtureStopError)return new Response(JSON.stringify({reason:'Synthetic stop transport failure'}),{status:503});
    if(String(url)==='/stop'||body.text==='stop')fixtureStopped=true;
    if(body.text==='resume')fixtureStopped=false;
    return new Response(JSON.stringify({ok:true,detail:'Synthetic Pi stop acknowledged'}));
  }
  return originalFetch(url,options);
};
document.querySelector('main').insertAdjacentHTML('afterbegin','<p id="fixture-label" style="margin:0 0 16px;color:#edbf79;font-size:12px;letter-spacing:.1em">SYNTHETIC UI FIXTURE · NO PHYSICAL ACCEPTANCE</p>');
window.refreshFixture=()=>{
  const now=realDateNow()/1000,perf=performance.now(),sim=fixtureMode==='sim';
  const s=JSON.parse(JSON.stringify(fixtureBase));s.server_at=now;s.receivedPerf=perf;s.serverAgeAtReceipt=stampAge(now);
  s.enabled=sim;s.robot_runtime={simulate:sim,motion:{enabled:sim,stop_latched:fixtureStopped}};
  s.camera_capture={available:!fixtureFailure,sensor_model:'OV5647 · fixture',encoder:'JPEG fixture · no hardware benchmark',
    requested_width:960,requested_height:720,requested_quality:80,requested_fps:15,capture_fps:14.9,encoded_fps:14.7,
    capture_fps_source:'synthetic SensorTimestamp fixture',provenance:'simulated'};
  const h=(available=true,used=false,reason=null)=>({available,used,fresh:available,captured_at:now-.05,age_s:.05,max_age_s:2,
    provenance:sim?'simulated':'real',failure_reason:reason,ignored_reason:used?null:'not used by stationary behavior'});
  const host=(name,cpu,memory,temp)=>({host:name,captured_at:now,age_s:.1,cpu_percent:cpu,memory_used_mib:memory,memory_total_mib:name==='pi'?3900:7800,temperature_c:temp,rss_mib:120,source:'synthetic host fixture'});
  s.host_vitals=host('pi',22,740,45);
  if(fixtureHardware)Object.assign(s.camera_capture,{encoder_requested:'mjpeg_hw',encoder:fixtureHardware==='simulated'?'Pillow synthetic; hardware profile not applied':'V4L2 MJPEG · synthetic capability fixture',requested_bitrate_bps:4000000,quality_applied:fixtureHardware==='simulated',hardware_benchmark_verified:false});
  s.brain={name:'Crab',mode:'interaction',simulate:sim,episode:{phase:'listen'},stopped:fixtureStopped,motion_enabled:false,
    blocked_reason:fixtureStopped?'User requested stop · explicit fresh resume required':'',mood:'curious',battery_v:7.42,distance_cm:74,
    last_say:'Hello. I’m here.',character:'Generated, saved identity narrative · not a live observation.',
    inputs:{battery:h(true,true),ultrasonic:h(!fixtureFailure,!fixtureFailure,fixtureFailure?'No echo · do not treat as clear space':null),
      camera:h(true,false),microphone:h(false,false,'STT disabled for this session'),speaker:h(false,false,fixtureFailure?'Amplifier disable unconfirmed':'Audio output disabled'),interpretation:h(false,false,'Vision model not running')},
    hearing:{available:false},audio:{effects_enabled:false,enabled:false,last_result:{ok:false,played:false,reason:fixtureFailure?'Amplifier cleanup failed · inspect output':'Audio output disabled'}},
    mind:{available:false,busy:false,provenance:'real',failure_reason:fixtureFailure?'Local model unavailable':null},
    pipeline:{transport:{transport:'websocket',connected:true,ready:true,last_rtt_ms:12.1,status_capture_age_s:.07,
      clock:{offset_lower_s:.024,offset_upper_s:.036}},streams:{perception:{available:false,failure_reason:'Camera interpretation disabled'}}},
    host_vitals:host('jetson',18,2350,49),_received_at:now-.005,_produced_at:now-.04,_produced_at_pi_lower:now-.02,
    intentions:[{kind:'notice',status:'completed',source:'human',reason:'Typed hello received',created_at:now-4,result:{reason:'Fresh typed engagement'}},
      {kind:'acknowledge',status:'completed',source:'episode',created_at:now-3,reason:'Acknowledge once, then listen',result:{reason:'Audio disabled · no output emitted',sound_queued:false,speech_queued:false}},
      {kind:'listen',status:'active',source:'episode',created_at:now-2,reason:'Open a bounded listening window',result:{reason:'Awaiting explicit engagement'}}]};
  if(fixtureFailure){s.brain.inputs.battery.available=false;s.brain.battery_v=null;cameraReason='Synthetic camera transfer failure';}
  s.telemetry_age_ms=5;s.sourceAgeAtReceipt=stampAge(s.brain._produced_at_pi_lower);
  latestState=s;streamConnected=true;stateMessages++;
};
window.fixtureTick=setInterval(refreshFixture,100);refreshFixture();
window.seedGraphs=()=>{graphHistory.length=0;const now=performance.now();for(let i=0;i<=120;i++){
  const wave=Math.sin(i/13);graphHistory.push({at:now-(120-i)*1000,boot:latestState.server_boot_id,battery:7.5-i*.0006,sonar:70+wave*12,
    capture:14.9+wave*.2,encoded:14.7+wave*.2,preview:9.8+wave*.1,age:90+Math.sin(i/8)*18,rtt:35+Math.sin(i/7)*7,ping:12+wave,uncertainty:12+wave*.5,
    pi_cpu_percent:22+wave*8,jetson_cpu_percent:18+Math.cos(i/10)*6,pi_memory_used_mib:740+wave*20,jetson_memory_used_mib:2350+wave*50,
    pi_temperature_c:45+wave*2,jetson_temperature_c:49+Math.cos(i/20)*2});}drawGraphs(now);};seedGraphs();
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/crab-mission-browser"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if not chromium:
        parser.error("installed Chromium required")
    server_port, browser_port = port(), port()
    base = f"http://127.0.0.1:{server_port}"
    env = {**os.environ, "PICRAWLER_SIMULATE": "1", "PICRAWLER_SIM_WORLD": "1", "PICRAWLER_ACTUATORS_ENABLED": "0",
           "PICRAWLER_HOME_ON_START": "none", "PICRAWLER_AUDIO_ENABLED": "0", "PICRAWLER_HOST": "127.0.0.1", "PICRAWLER_PORT": str(server_port)}
    server = browser = cdp = None
    report = {"scope": "Synthetic browser fixtures on explicit local simulation; no physical camera/network/host performance claim", "screenshots": []}
    with tempfile.TemporaryDirectory(prefix="crab-mission-profile-") as profile, \
         open(args.output_dir/"server.log", "w") as server_log, open(args.output_dir/"chromium.log", "w") as browser_log, \
         httpx.Client(timeout=2, trust_env=False) as http:
        try:
            server = subprocess.Popen([sys.executable, "-m", "robot.server"], cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
            wait_url(http, base+"/health", server)
            browser = subprocess.Popen([chromium, "--headless", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking", "--no-first-run",
                f"--remote-debugging-port={browser_port}", f"--user-data-dir={profile}", "about:blank"], stdout=browser_log, stderr=subprocess.STDOUT)
            targets = wait_url(http, f"http://127.0.0.1:{browser_port}/json/list", browser).json()
            cdp = CDP(next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page"))
            for method in ("Runtime.enable", "Network.enable", "Page.enable"):
                cdp.call(method)
            cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": "window.realDateNow=Date.now.bind(Date);Date.now=()=>realDateNow()+30;"})
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 1050, "deviceScaleFactor": 1, "mobile": False})
            cdp.call("Page.navigate", {"url": base+"/sim"})
            cdp.wait("typeof latestState!=='undefined' && latestState!==null && clockAnchor!==null")
            cdp.value(FIXTURE_JS.replace("__PATTERN__", pattern()))
            cdp.wait("cameraNaturalWidth===960 && document.querySelector('#clock-value').textContent.includes('24.0')")

            def screenshot(name, full=True):
                if full:
                    size = cdp.call("Page.getLayoutMetrics")["cssContentSize"]
                    params = {"format": "png", "captureBeyondViewport": True, "clip": {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1}}
                else:
                    params = {"format": "png", "captureBeyondViewport": False}
                output = cdp.call("Page.captureScreenshot", params)
                (args.output_dir/name).write_bytes(base64.b64decode(output["data"]))
                report["screenshots"].append(name)

            cdp.wait("cameraNaturalWidth===960 && cameraHz>0")
            cdp.value("fixtureHardware=true;refreshFixture();true")
            cdp.wait("document.querySelector('#camera-config').textContent.includes('4.00 Mbps target') && document.querySelector('#camera-config').textContent.includes('JPEG quality not applied')")
            report["hardware_bitrate_distinct_from_jpeg_quality"] = True
            cdp.value("fixtureHardware='simulated';refreshFixture();true")
            cdp.wait("document.querySelector('#camera-config').textContent.includes('Pillow synthetic') && document.querySelector('#camera-config').textContent.includes('JPEG quality 80') && !document.querySelector('#camera-config').textContent.includes('Mbps target')")
            report["simulation_reports_applied_jpeg_quality"] = True
            cdp.value("fixtureHardware=false;refreshFixture();true")
            cdp.value("seedGraphs();true")
            screenshot("real-connected-desktop.png")
            report["native_no_upscale"] = cdp.value("document.querySelector('#cam').getBoundingClientRect().width<=cameraNaturalWidth+1")
            assert report["native_no_upscale"]
            cdp.value("window.scrollTo(0,document.querySelector('#vitals-heading').getBoundingClientRect().top+window.scrollY-document.querySelector('.safety-bar').offsetHeight-16);true")
            screenshot("vitals-desktop.png", False)
            cdp.value("window.scrollTo(0,0);document.querySelector('#camera-expand').click();true")
            report["stop_visible_in_expanded_camera"] = cdp.value("document.querySelector('#stop-control').getBoundingClientRect().bottom<window.innerHeight && document.querySelector('#stop-control').getBoundingClientRect().top>=0")
            assert report["stop_visible_in_expanded_camera"]
            cdp.value("document.querySelector('#camera-expand').click();fixtureMode='sim';refreshFixture();true")
            cdp.wait("document.querySelector('#provenance').textContent.includes('SIMULATION')")
            screenshot("simulation-desktop.png")

            cdp.value("fixtureMode='real';fixtureStopped=true;refreshFixture();true")
            cdp.wait("!document.querySelector('#resume-review').disabled")
            cdp.value("document.querySelector('#resume-review').click();true")
            report["resume_requires_confirmation"] = cdp.value("!document.querySelector('#resume-confirm').hidden && !fixturePosts.some(p=>p.body.text==='resume')")
            assert report["resume_requires_confirmation"]
            cdp.value("document.querySelector('#resume-confirm-button').click();true")
            cdp.wait("fixturePosts.some(p=>p.body.text==='resume') && !fixtureStopped")
            cdp.value("fixtureDecodeDelay=180;fixtureDelay=30;true")
            cdp.wait("cameraDropped>0", timeout=4)
            cdp.value("document.querySelector('#stop-control').click();true")
            cdp.wait("document.querySelector('#input-result').textContent.includes('STOP acknowledged')")
            report["stop_during_slow_decode"] = True
            report["peak_preview_requests"] = cdp.value("fixturePeak")
            report["replaced_pending_frames"] = cdp.value("cameraDropped")
            assert report["peak_preview_requests"] == 1

            cdp.value("fixtureDelay=fixtureDecodeDelay=0;true")
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
            cdp.wait("document.documentElement.scrollWidth<=window.innerWidth")
            screenshot("real-connected-mobile.png")
            report["mobile_no_overflow"] = True
            cdp.value("fixtureFailure=true;refreshFixture();true")
            cdp.wait("document.querySelector('#system-cards').textContent.includes('No echo')")
            screenshot("failure-mobile.png")
            cdp.value("fixtureStopError=true;document.querySelector('#stop-control').click();true")
            cdp.wait("document.querySelector('#input-result').textContent.includes('STOP unconfirmed')")
            report["stop_failure_honest"] = True
            cdp.value("clearInterval(fixtureTick);pauseFeeds();latestState.receivedPerf-=10000;latestState.sourceAgeAtReceipt+=10000;true")
            cdp.wait("!streamConnected && document.querySelector('#feed-state').textContent.includes('disconnected') && document.querySelector('#resume-review').disabled")
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 1050, "deviceScaleFactor": 1, "mobile": False})
            screenshot("disconnected-desktop.png")
            report["stale_resume_disabled"] = True
            report["javascript_errors"] = cdp.errors
            assert not cdp.errors, cdp.errors
            (args.output_dir/"report.json").write_text(json.dumps(report, indent=2)+"\n")
            print(json.dumps(report, indent=2))
        finally:
            if cdp:
                cdp.close()
            for process in (browser, server):
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)


if __name__ == "__main__":
    main()
