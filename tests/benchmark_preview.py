"""Controlled scheduler comparison, not camera/network/physical performance."""
import argparse
import json
from pathlib import Path
import subprocess

from robot.sim_view import DASHBOARD_HTML

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
const {performance}=require('perf_hooks');const nodes=new Map();
const document={hidden:false,getElementById(id){if(!nodes.has(id))nodes.set(id,{src:'',hidden:false});return nodes.get(id);}};
let sequence=0,active=0,peak=0,requests=0;
const Image=class{constructor(){this.naturalWidth=960;this.naturalHeight=720;}decode(){return new Promise(r=>setTimeout(r,120));}};
const URL={createObjectURL:()=> 'blob:synthetic',revokeObjectURL(){}};
const fetch=async()=>{active++;peak=Math.max(peak,active);requests++;await new Promise(r=>setTimeout(r,50));active--;
 const metadata={'X-Crab-Sequence':String(++sequence),'X-Crab-Captured-At':String(Date.now()/1000),'X-Crab-Frame-Age-Ms':'0','Content-Length':'3','X-Crab-Provenance':'simulated'};
 return {ok:true,headers:{get:k=>metadata[k]??null},blob:async()=>new Blob(['jpg']),body:{getReader(){let sent=false;return {read:async()=>sent?{done:true}:(sent=true,{done:false,value:new Uint8Array(3)}),releaseLock(){}};}}};};
'''
FOOTER = r'''
clockAnchor={wall:Date.now(),perf:performance.now(),lower:0,upper:0,boot:'fixture'};
const started=performance.now();nextCamera();
setTimeout(()=>{const seconds=(performance.now()-started)/1000;
 const result={seconds,requests,decoded_frames:cameraFrames,decoded_fps:cameraFrames/seconds,peak_requests:peak};
 if(typeof cameraDropped!=='undefined')result.replaced_pending_frames=cameraDropped;
 document.hidden=true;pauseFeeds();console.log(JSON.stringify(result));},4000);
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="8482f487ca92e78f2530dcb6ccc0ab66ad89489e")
    args = parser.parse_args()
    baseline = subprocess.check_output(["git", "show", f"{args.baseline_ref}:robot/sim_view.py"], cwd=ROOT, text=True)
    reports = {}
    for label, html in (("serial_baseline", baseline), ("bounded_overlap", DASHBOARD_HTML)):
        source = html.split("<script>", 1)[1].split('document.addEventListener("visibilitychange"', 1)[0]
        for key, value in {"MAX_AGE_MS": 5000, "MAX_RTT_MS": 200, "MAX_OFFSET_MS": 250,
                           "WALL_STEP_MS": 20, "DRIFT": .001}.items():
            source = source.replace("__CLOCK_"+key+"__", str(value))
        run = subprocess.run(["node", "-e", HARNESS+source+FOOTER], text=True, capture_output=True, timeout=6, check=True)
        reports[label] = json.loads(run.stdout)
    print(json.dumps({"scope": "Node with synthetic 50 ms fetch and 120 ms decode; four seconds per path. "
                              "Demonstrates scheduler overlap only, not real JPEG decode, Pi encoding, LAN or display speed.",
                      "baseline_ref": args.baseline_ref, **reports}, indent=2))


if __name__ == "__main__":
    main()
