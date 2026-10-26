"""Mission-control timing/history and host metrics; fake sources only."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from robot.camera import PiCamera, _FrameBuffer
from robot.sim_view import DASHBOARD_HTML
from shared.vitals import SystemVitals
from brain.preview_probe import probe, LIMIT


class PreviewProbeTests(unittest.TestCase):
    def test_read_only_requests_bound_body_and_keep_each_matching_stamp(self):
        response = Mock(status=200)
        response.read.return_value = b"jpeg"
        response.getheader.side_effect = lambda name: {"X-Crab-Sequence": "9", "X-Crab-Captured-At": "1000"}.get(name)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("brain.preview_probe.http.client.HTTPConnection", return_value=connection), patch("brain.preview_probe.time.sleep"):
            report = probe("http://127.0.0.1:8000", samples=2)
        self.assertEqual(connection.request.call_count, 2)
        for call in connection.request.call_args_list:
            self.assertEqual(call.args, ("GET", "/camera/frame"))
        response.read.assert_called_with(LIMIT+1)
        self.assertEqual(report["samples"][0]["encoded_at_pi"], "1000")
        connection.close.assert_called_once()

    def test_redirect_or_public_target_is_rejected_without_following_it(self):
        connection = Mock()
        connection.getresponse.return_value = Mock(status=302)
        with patch("brain.preview_probe.http.client.HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "no redirect"):
                probe("http://localhost:8000", samples=2)
            with self.assertRaises(ValueError):
                probe("https://example.com", samples=2)
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()


class VitalsTests(unittest.TestCase):
    def setUp(self):
        self.now, self.cpu = 10., 1.
        self.files = {"/proc/stat": "cpu 100 0 50 800 50 0 0 0\n",
                      "/proc/meminfo": "MemTotal: 8192000 kB\nMemAvailable: 4096000 kB\n",
                      "/proc/self/status": "Name: fixture\nVmRSS: 20480 kB\n"}
        self.reader = Mock(side_effect=lambda path: self.files[path])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vitals = SystemVitals("pi", clock=lambda: self.now, wall=lambda: 1000+self.now,
                                  process_clock=lambda: self.cpu, reader=self.reader, thermal_root=self.temp.name)

    def test_one_hz_cache_cpu_warmup_source_and_actual_memory_units(self):
        first = self.vitals.snapshot()
        self.assertIsNone(first["cpu_percent"])
        self.assertEqual(first["memory_used_mib"], 4000)
        self.assertEqual(first["rss_mib"], 20)
        calls = self.reader.call_count
        self.now += .2
        self.assertAlmostEqual(self.vitals.snapshot()["age_s"], .2)
        self.assertEqual(self.reader.call_count, calls)
        self.files["/proc/stat"] = "cpu 150 0 50 850 50 0 0 0\n"
        self.now += .8
        self.cpu += .1
        second = self.vitals.snapshot()
        self.assertEqual(second["cpu_percent"], 50)
        self.assertAlmostEqual(second["process_cpu_percent"], 10)
        self.assertEqual(second["host"], "pi")

    def test_missing_or_reset_metrics_are_unknown_and_snapshot_cannot_mutate_cache(self):
        first = self.vitals.snapshot()
        first["reasons"]["temperature_c"] = "modified"
        self.assertNotEqual(self.vitals.snapshot()["reasons"]["temperature_c"], "modified")
        self.reader.side_effect = OSError("unavailable")
        self.now += 2
        result = self.vitals.snapshot()
        for key in ("cpu_percent", "rss_mib", "memory_used_mib", "temperature_c"):
            self.assertIsNone(result[key])
        json.dumps(result, allow_nan=False)

    def test_only_readable_cpu_temperature_is_reported(self):
        for name, kind, value in (("thermal_zone0", "gpu-thermal", "55000"),
                                  ("thermal_zone1", "cpu-thermal", "43000")):
            zone = Path(self.temp.name)/name
            zone.mkdir()
            self.files[str(zone/"type")] = kind
            self.files[str(zone/"temp")] = value
        result = self.vitals.snapshot()
        self.assertEqual(result["temperature_c"], 43)
        self.assertEqual(result["temperature_source"], "cpu-thermal")


class CameraInstrumentationTests(unittest.TestCase):
    def test_sensor_cadence_and_encoded_cadence_are_separate_and_expire(self):
        camera = PiCamera(simulate=True)
        camera._failure = None
        with patch("robot.camera.time.monotonic", return_value=10):
            camera._capture_metadata(SimpleNamespace(get_metadata=lambda: {"SensorTimestamp": 1_000_000_000}))
            camera._buffer.write(b"encoded")
        with patch("robot.camera.time.monotonic", return_value=10.1):
            camera._capture_metadata(SimpleNamespace(get_metadata=lambda: {"SensorTimestamp": 1_100_000_000}))
            camera._buffer.write(b"newest")
            stats = camera.stats()
            self.assertEqual(stats["capture_fps"], 10)
            self.assertEqual(stats["encoded_fps"], 10)
            self.assertEqual(stats["jpeg_bytes"], 6)
            self.assertIn("encoded-output", stats["timestamp_kind"])
        with patch("robot.camera.time.monotonic", return_value=13):
            self.assertIsNone(camera.stats()["capture_fps"])
            self.assertIsNone(camera.stats()["encoded_fps"])
        camera.stop()


@unittest.skipUnless(shutil.which("node"), "Node executes the real preview/history code")
class MissionBrowserTests(unittest.TestCase):
    def js(self, check):
        source = DASHBOARD_HTML.split("<script>", 1)[1].split('document.addEventListener("visibilitychange"', 1)[0]
        harness = """
const assert=require('assert');let mono=10000,wall=1000000;
const performance={now:()=>mono};const Date=class extends globalThis.Date{static now(){return wall;}};
const nodes=new Map();const document={hidden:false,getElementById(id){if(!nodes.has(id))nodes.set(id,{textContent:'',innerHTML:'',hidden:false,disabled:false,src:'',setAttribute(){},removeAttribute(){}});return nodes.get(id);}};
const window={devicePixelRatio:1};let timerId=0;const timers=new Map();
function setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms});return id;}function clearTimeout(id){timers.delete(id);}
"""
        result = subprocess.run(["node", "-e", harness+source+check], text=True, capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_decode_and_request_overlap_with_one_replaceable_pending_frame(self):
        self.js("""
let seq=0,decodeCount=0,release=null,liveURLs=new Set();
const URL={createObjectURL(){const s='blob:'+Math.random();liveURLs.add(s);return s;},revokeObjectURL(s){liveURLs.delete(s);}};
const Image=class{constructor(){this.naturalWidth=960;this.naturalHeight=720;}decode(){decodeCount++;return decodeCount===1?new Promise(r=>release=r):Promise.resolve();}};
postJSON=async()=>({ok:true});
const fetch=async()=>{const sequence=String(++seq);return {ok:true,headers:{get(k){return {'X-Crab-Sequence':sequence,'X-Crab-Captured-At':String(1000+seq/1000),'X-Crab-Frame-Age-Ms':'0','Content-Length':'3'}[k]??null;}},body:{getReader(){let sent=false;return {async read(){if(sent)return {done:true};sent=true;return {done:false,value:new Uint8Array(3)};},releaseLock(){}};}}};};
(async()=>{
  await nextCamera();assert(!cameraRunning&&cameraDecoding);assert.equal(decodeCount,1);
  await stopCompanion();assert(document.getElementById('input-result').textContent.includes('STOP acknowledged'));assert(cameraDecoding);
  await nextCamera();const second=cameraPending.sequence;
  await nextCamera();assert.notEqual(cameraPending.sequence,second);assert.equal(cameraDropped,1);
  release();for(let i=0;i<20;i++)await Promise.resolve();
  assert.equal(decodeCount,2);assert.equal(cameraPending,null);assert.equal(liveURLs.size,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_replayed_or_out_of_order_preview_never_replaces_newest_slot(self):
        self.js("""
let stamp=1000,seq=5;const URL={createObjectURL:()=> 'blob:test',revokeObjectURL(){}};
const Image=class{decode(){return Promise.resolve();}};
const fetch=async()=>({ok:true,headers:{get:k=>({'X-Crab-Sequence':String(seq),'X-Crab-Captured-At':String(stamp),'X-Crab-Frame-Age-Ms':'0','Content-Length':'1'}[k]??null)},body:{getReader(){let sent=false;return {async read(){if(sent)return {done:true};sent=true;return {done:false,value:new Uint8Array(1)};},releaseLock(){}};}}});
(async()=>{await nextCamera();for(let i=0;i<15;i++)await Promise.resolve();assert.equal(cameraSequence,'5');
 seq=4;stamp+=.01;await nextCamera();assert.equal(cameraFetchedSequence,'5');
 seq=6;stamp=999;await nextCamera();assert.equal(cameraFetchedSequence,'5');assert.equal(cameraReceivedFrames,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_stalled_decode_times_out_and_releases_slot_and_url(self):
        self.js("""
let revoked=0;const URL={createObjectURL:()=> 'blob:test',revokeObjectURL:()=>revoked++};
const Image=class{decode(){return new Promise(()=>{});}};
cameraPending={blob:new Blob(['x']),sequence:'1',stamp:1000,age:0,session:0,started:mono,received:mono};
(async()=>{const decoding=decodeCamera();const timeout=[...timers.values()].find(t=>t.ms===1000);assert(timeout);timeout.fn();await decoding;
 assert(!cameraDecoding);assert.equal(cameraDecoder,null);assert.equal(revoked,1);assert(cameraReason.includes('timed out'));
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_hidden_generation_discards_pending_decode_and_releases_blob(self):
        self.js("""
let release,revoked=0;const URL={createObjectURL:()=> 'blob:test',revokeObjectURL:()=>revoked++};
const Image=class{decode(){return new Promise(r=>release=r);}};
cameraPending={blob:new Blob(['x']),sequence:'1',stamp:1000,age:0,session:0,started:mono,received:mono};
(async()=>{decodeCamera();pauseFeeds();release();for(let i=0;i<10;i++)await Promise.resolve();assert.equal(cameraFrames,0);assert.equal(revoked,1);assert.equal(cameraPending,null);})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_preview_body_is_bounded_before_decode(self):
        self.js("""
(async()=>{
  await assert.rejects(()=>readJpeg({headers:{get:()=>String(5*1024*1024)}}));
  let cancelled=false;
  await assert.rejects(()=>readJpeg({headers:{get:()=>null},body:{getReader:()=>({read:async()=>({done:false,value:new Uint8Array(3*1024*1024)}),cancel:async()=>cancelled=true,releaseLock(){}})}}));
  assert(cancelled);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

    def test_graph_history_is_bounded_with_unknown_gaps_and_distinct_host_sources(self):
        self.js("""
drawGraphs=()=>{};
const s={server_boot_id:'pi',receivedPerf:mono,server_at:1000,serverAgeAtReceipt:0,sourceAgeAtReceipt:0,telemetry_age_ms:0,
 brain:{_received_at:1000,_produced_at_pi_lower:1000,inputs:{battery:{available:true,fresh:true,captured_at:1000,age_s:0,max_age_s:2}},battery_v:7.4,
 host_vitals:{host:'pi',age_s:0,cpu_percent:50}},host_vitals:{host:'pi',age_s:0,cpu_percent:10}};
for(let i=0;i<200;i++){mono+=1000;wall+=1000;clockAnchor={wall,perf:mono,lower:0,upper:0,boot:'pi'};
 s.receivedPerf=mono;s.server_at=wall/1000;s.brain._received_at=wall/1000;s.brain._produced_at_pi_lower=wall/1000;sampleGraphs(s,mono);}
assert.equal(graphHistory.length,121);assert.equal(graphHistory.at(-1).pi_cpu_percent,10);assert.equal(graphHistory.at(-1).jetson_cpu_percent,null);
s.brain.inputs.battery.available=false;mono+=1000;wall+=1000;sampleGraphs(s,mono);assert.equal(graphHistory.at(-1).battery,null);
""")

    def test_stale_state_cannot_publish_old_capture_rates_with_live_jpeg(self):
        self.js("""
drawGraphs=()=>{};clockAnchor={wall,perf:mono,lower:0,upper:0,boot:'pi'};
cameraReceivedAt=mono;cameraAgeAtReceipt=0;cameraCapturedAt=wall/1000;cameraHz=8;
const s={server_boot_id:'pi',receivedPerf:mono-10000,server_at:(wall-10000)/1000,serverAgeAtReceipt:0,sourceAgeAtReceipt:0,
 camera_capture:{capture_fps:15,encoded_fps:14},brain:{}};
sampleGraphs(s,mono);const p=graphHistory.at(-1);assert.equal(p.capture,null);assert.equal(p.encoded,null);assert.equal(p.preview,8);
""")

    def test_pending_stop_keeps_priority_over_later_ordinary_input(self):
        self.js("""
let release;const calls=[];postJSON=async(path,payload)=>{calls.push(payload);if(path==='__STOP_PATH__')return new Promise(r=>release=r);return {ok:true};};
(async()=>{const stopping=stopCompanion();await Promise.resolve();assert(stopPending);await sendText('hello');assert.equal(calls.length,1);
 release({ok:true});await stopping;assert(document.getElementById('input-result').textContent.includes('STOP acknowledged'));assert(!stopPending);
})().catch(e=>{console.error(e);process.exitCode=1;});
""".replace('__STOP_PATH__', '/stop'))

    def test_old_pending_input_cannot_overwrite_new_stop_acknowledgement(self):
        self.js("""
let release=null;const calls=[];postJSON=async(path,payload)=>{calls.push([path,payload]);if(payload.text==='hello')return new Promise(r=>release=r);return {ok:true};};
(async()=>{sendText('hello');await Promise.resolve();await stopCompanion();const ack=document.getElementById('input-result').textContent;
 release({ok:true});for(let i=0;i<10;i++)await Promise.resolve();assert.equal(document.getElementById('input-result').textContent,ack);assert(ack.includes('STOP acknowledged'));assert(!calls.some(([,p])=>p.text==='resume'));assert(!stopPending);})().catch(e=>{console.error(e);process.exitCode=1;});
""")


if __name__ == "__main__":
    unittest.main()
