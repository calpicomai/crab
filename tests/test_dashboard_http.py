"""Dashboard delivery contracts without starting hardware or an HTTP listener."""
import asyncio
import json
import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, Mock, patch

from robot import config
from robot.camera import CameraFrame
from robot.telemetry import TelemetryStore
from robot.sim_view import DASHBOARD_HTML


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        with patch.object(config, "SIMULATE", True), patch.object(config, "ACTUATORS_ENABLED", False):
            from robot import server
        self.server = server
        self.now = 100.0
        self.store = TelemetryStore(clock=lambda: self.now)
        for target, name, value in ((server, "_telemetry", self.store),
                                    (server, "sim_world", None)):
            active = patch.object(target, name, value)
            active.start()
            self.addCleanup(active.stop)

    def publish(self, sequence):
        return self.server.sim_brain({"_publisher_id": "fixture", "_sequence": sequence,
                                      "_publisher_started_at_ns": 1, "tick": sequence})

    def test_latest_jpeg_headers_match_returned_frame(self):
        camera = Mock()
        camera.get_snapshot.return_value = CameraFrame(b"jpeg1", 98.75, 42, "simulated")
        with patch.object(self.server, "camera", camera), patch.object(self.server.time, "time", return_value=100):
            response = self.server.camera_frame()
        self.assertEqual(response.body, b"jpeg1")
        self.assertEqual(response.headers["X-Crab-Sequence"], "42")
        self.assertEqual(response.headers["X-Crab-Captured-At"], "98.75")
        self.assertEqual(float(response.headers["X-Crab-Frame-Age-Ms"]), 1250)
        self.assertEqual(response.headers["X-Crab-Provenance"], "simulated")
        self.assertIn("no-store", response.headers["Cache-Control"])
        camera.get_snapshot.assert_called_once_with(timeout=0)
        camera.health.assert_not_called()

    def test_missing_or_stalled_camera_returns_uncacheable_failure(self):
        for camera in (None, Mock(get_snapshot=Mock(return_value=None))):
            with self.subTest(camera=camera), patch.object(self.server, "camera", camera):
                response = self.server.camera_frame()
                self.assertEqual(response.status_code, 503)
                self.assertIn("no-store", response.headers["Cache-Control"])

    def test_receipt_age_advances_without_new_telemetry(self):
        self.assertTrue(self.publish(1)["ok"])
        with patch.object(self.server.time, "time", return_value=101.5):
            response = self.server.sim_state()
        state = json.loads(response.body)
        self.assertEqual(state["telemetry_age_ms"], 1500)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["brain"]["tick"], 1)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_slow_event_consumer_gets_newest_snapshot_without_replay(self):
        async def consume():
            request = Mock(is_disconnected=AsyncMock(side_effect=[False, False, True]))
            self.publish(1)
            stream = self.server._dashboard_events(request)
            first = await anext(stream)
            for sequence in range(2, 51):
                self.publish(sequence)
            latest = await anext(stream)
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
            return first, latest

        with patch.object(self.server.asyncio, "sleep", new=AsyncMock()):
            first, latest = asyncio.run(consume())
        self.assertTrue(first.startswith("event: state\ndata: "))
        self.assertEqual(json.loads(first.split("data: ", 1)[1])["brain"]["tick"], 1)
        self.assertEqual(json.loads(latest.split("data: ", 1)[1])["brain"]["tick"], 50)


@unittest.skipUnless(shutil.which("node"), "Node is required to execute the dashboard timing code")
class BrowserTimingTests(unittest.TestCase):
    def js(self, check):
        source = DASHBOARD_HTML.split("<script>", 1)[1].split("function connectState(){", 1)[0]
        harness = """
const assert=require('assert');
let wall=1000000,mono=10000;
const Date={now:()=>wall},performance={now:()=>mono};
function calibrate(offset=30,boot='pi'){
  const sentWall=wall,sentMono=mono;wall+=12;mono+=12;
  measureClock({server_at:(sentWall+6+offset)/1000,server_boot_id:boot},sentWall,sentMono);
}
"""
        result = subprocess.run(["node", "-e", harness+source+check], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_both_skew_directions_and_buffered_capture_ages_use_upper_bounds(self):
        self.js("""
for(const offset of [-30,30]){
  clockAnchor=null;wall=1000000;mono=10000;calibrate(offset);
  const stamp=(1000000+offset)/1000;
  assert(Math.abs(stampAge(stamp)-18)<.001);
  const s={server_at:stamp,server_boot_id:'pi',receivedPerf:mono,
    serverAgeAtReceipt:stampAge(stamp),sourceAgeAtReceipt:stampAge(stamp-.01),telemetry_age_ms:100,
    brain:{_received_at:stamp-.1,_produced_at_pi_lower:stamp-.01}};
  const h={available:true,fresh:true,age_s:.2,max_age_s:1,captured_at:stamp};
  const first=inputTiming(s,h);assert(first.fresh);assert(first.age>=.318);
  assert(!inputTiming(s,{...h,clock:{},captured_at:1005}).fresh);
  wall+=900;mono+=900;assert(!inputTiming(s,h).fresh);
  // Delivery of the same retained capture cannot reset source age.
  s.receivedPerf=mono;s.serverAgeAtReceipt=stampAge(stamp);
  s.sourceAgeAtReceipt=stampAge(stamp-.01);assert(!inputTiming(s,h).fresh);
}
""")

    def test_bad_probe_future_clock_steps_and_expiration_are_unknown(self):
        self.js("""
calibrate();assert.equal(stampAge(1001),Infinity);
wall+=100;assert.equal(stampAge(1000),Infinity);wall-=100;
wall+=5001;mono+=5001;assert.equal(stampAge(1000),Infinity);
for(const offset of [-500,500]){clockAnchor=null;assert.throws(()=>calibrate(offset));}
clockAnchor=null;wall=1000000;mono=10000;
assert.throws(()=>measureClock({server_at:true,server_boot_id:'pi'},wall,mono));
wall+=250;mono+=250;assert.throws(()=>measureClock({server_at:1000.1,server_boot_id:'pi'},1000000,10000));
clockAnchor=null;calibrate();assert.throws(()=>calibrate(130));
""")

    def test_sse_replay_and_boot_changes_cannot_retime_old_state(self):
        source = DASHBOARD_HTML.split("function connectState(){", 1)[1].split("async function nextCamera(){", 1)[0]
        self.js("""
const document={hidden:false};let handler=null,probes=0;
const EventSource=class{addEventListener(type,fn){handler=fn;}};
probeClock=()=>{probes++;};
function connectState(){"""+source+"""
calibrate();connectState();
function deliver(stamp,boot='pi',revision=1){handler({data:JSON.stringify({server_at:stamp,
  server_boot_id:boot,revision,brain:{_produced_at_pi_lower:stamp}})});}
deliver(1000.03);const receipt=latestState.receivedPerf;
wall+=100;mono+=100;deliver(1000.03);assert.equal(latestState.receivedPerf,receipt);
deliver(1000.02);assert.equal(latestState.server_at,1000.03);
deliver(1005);assert.equal(latestState.server_at,1000.03);
deliver(1000.04,'pi',0);assert.equal(latestState.revision,1);
deliver(1000.13,'reboot');assert.equal(probes,1);assert.equal(latestState.server_boot_id,'pi');
calibrate(30,'reboot');deliver(1000.142,'reboot');assert.equal(latestState.server_boot_id,'reboot');
""")

    def test_clock_probe_has_one_timer_and_restarts_after_visibility_generation(self):
        self.js("""
const document={hidden:false},timers=new Map();let timerId=0;
function setTimeout(fn,delay){const id=++timerId;timers.set(id,delay);return id;}
function clearTimeout(id){timers.delete(id);}
let changeGeneration=false;
const fetch=async()=>{if(changeGeneration)clockGeneration++;return {ok:true,json:async()=>({server_at:1000,server_boot_id:'pi'})};};
(async()=>{
  await probeClock();await probeClock();assert.equal(timers.size,1);
  changeGeneration=true;await probeClock();assert.equal(timers.size,1);
  assert.equal([...timers.values()][0],0); // old completion requests a fresh probe
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


if __name__ == "__main__":
    unittest.main()
