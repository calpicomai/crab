"""Input-specific dashboard observations; fake I/O or explicit loopback simulation."""
import json
import queue
import shutil
import subprocess
import time
import unittest

from brain.pet.coordinator import Coordinator
from brain.realtime import RealtimeClient
from brain.realtime_benchmark import SimulatedServer
from robot.sim_view import DASHBOARD_HTML
from shared import COMPANION_EVENTS_PATH
from shared.realtime import InputEvent
from websockets.sync.client import connect
from test_coordinator import Clock, Robot, Voice


class OutcomeTransportTests(unittest.TestCase):
    def test_http_receipt_identifies_the_exact_queued_input(self):
        with SimulatedServer() as server:
            first = server.http.post(COMPANION_EVENTS_PATH, json={"text": "hello"}).json()
            second = server.http.post(COMPANION_EVENTS_PATH, json={"text": "hello"}).json()
            self.assertIn("event_id", first)
            self.assertNotEqual(first["event_id"], second["event_id"])
            self.assertEqual(first["boot_id"], server.leases.boot_id)
            self.assertIsNone(first["session_id"])
            events = server.http.get(COMPANION_EVENTS_PATH).json()["events"]
            self.assertEqual([e["event_id"] for e in events], [first["event_id"], second["event_id"]])
            self.assertEqual(events[0]["boot_id"], first["boot_id"])

    def test_websocket_preserves_receipt_and_original_epoch(self):
        events = queue.Queue()
        with SimulatedServer() as server:
            client = RealtimeClient(server.base_url, on_event=events.put_nowait)
            client.start()
            try:
                deadline = time.monotonic()+3
                while not client.health()["ready"] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(client.health()["ready"])
                response = server.http.post(COMPANION_EVENTS_PATH, json={"text": "resume"}).json()
                self.assertIn("event_id", response)
                event = events.get(timeout=2)
                for key in ("event_id", "boot_id", "session_id"):
                    self.assertEqual(event[key], response[key])
                self.assertEqual(event["control_epoch"], response["control_epoch"])
                self.assertEqual(response["session_id"], client.health()["session_id"])
            finally:
                client.close()

    def test_legacy_event_without_id_remains_valid(self):
        event = InputEvent(text="hello", timestamp=1.0)
        self.assertEqual(event.text, "hello")

    def test_old_companion_receives_original_event_shape_without_opt_in(self):
        with SimulatedServer() as server, connect(server.ws_url+"/companion/ws", proxy=None) as socket:
            self.assertEqual(json.loads(socket.recv(timeout=2))["type"], "hello")
            receipt = server.http.post(COMPANION_EVENTS_PATH, json={"text": "hello"}).json()
            self.assertFalse(receipt.get("correlation_available", True))
            for _ in range(10):
                message = json.loads(socket.recv(timeout=2))
                if message["type"] == "event":
                    self.assertEqual(set(message["event"]), {"text", "timestamp", "source"})
                    break
            else:
                self.fail("event was not delivered")


class CoordinatorOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.robot, self.voice = Robot(), Voice()
        self.coordinator = Coordinator(self.robot, self.voice, clock=self.clock, wall_clock=self.clock)
        self.coordinator.observe({"boot_id": "pi", "pose": "unknown"})
        self.addCleanup(self.coordinator.close)

    def hear(self, text, event_id):
        self.assertIn("input_events", self.coordinator.snapshot())
        return self.coordinator.hear(text, source="dashboard", event_id=event_id,
                                     event_boot_id="pi", event_session_id="session")

    def observation(self, event_id):
        return next(e for e in self.coordinator.snapshot()["input_events"] if e["event_id"] == event_id)

    def tick_until(self, predicate):
        deadline = time.monotonic()+1
        while not predicate() and time.monotonic() < deadline:
            self.coordinator.tick()
            time.sleep(.001)
        self.assertTrue(predicate())

    def test_greeting_links_its_actual_acknowledgement(self):
        self.hear("hello", "hello1")
        self.assertEqual(self.observation("hello1")["state"], "queued")
        # A camera/safety/mind intention is not this input's acknowledgement.
        self.coordinator.request("orient", source="mind")
        self.coordinator.tick()
        self.assertEqual(self.observation("hello1")["state"], "queued")
        for _ in range(5):
            self.clock.now += .6
            self.coordinator.observe({"boot_id": "pi"})
            self.coordinator.tick()
        observed = self.observation("hello1")
        self.assertEqual(observed["state"], "completed")
        self.assertEqual(observed["kind"], "acknowledge")
        self.assertEqual(observed["session_id"], "session")
        self.assertIn("audio requested", observed["reason"])

    def test_stop_and_resume_have_independent_results(self):
        self.hear("hello", "hello1")
        self.hear("stop", "stop1")
        self.assertEqual(self.observation("hello1")["state"], "cancelled")
        self.tick_until(lambda: self.observation("stop1")["state"] == "completed")
        self.hear("resume", "resume1")
        self.assertEqual(self.observation("resume1")["state"], "queued")
        self.tick_until(lambda: self.observation("resume1")["state"] == "completed")
        self.assertFalse(self.coordinator.stopped)
        self.assertEqual(self.observation("stop1")["kind"], "stop")
        self.assertEqual(self.observation("resume1")["kind"], "resume")

    def test_repeat_is_processed_without_claiming_a_second_acknowledgement(self):
        self.hear("hello", "hello1")
        self.hear("hello", "hello2")
        self.assertEqual(self.observation("hello2")["state"], "processed")
        self.assertIsNone(self.observation("hello2")["intention_id"])
        self.assertEqual(self.observation("hello1")["state"], "queued")

    def test_observation_window_is_bounded(self):
        for index in range(100):
            self.hear("hello", f"event{index}")
        self.assertLessEqual(len(self.coordinator.snapshot()["input_events"]), 32)
        self.assertEqual(self.coordinator.snapshot()["input_events"][-1]["event_id"], "event99")

    def test_unconfirmed_command_is_unknown_without_retry(self):
        def unavailable():
            self.robot.calls.append("resume")
            raise OSError("lost reply")
        self.robot.resume = unavailable
        self.hear("resume", "resume1")
        self.tick_until(lambda: self.observation("resume1")["state"] == "unknown")
        self.assertIn("lost reply", self.observation("resume1")["reason"])
        for _ in range(5):
            self.coordinator.tick()
        self.assertEqual(self.robot.calls.count("resume"), 1)

    def test_blocked_posture_reports_its_own_reason(self):
        self.hear("stand", "stand1")
        self.coordinator.tick()
        observed = self.observation("stand1")
        self.assertEqual(observed["state"], "blocked")
        self.assertIn("body expression disabled", observed["reason"])
        self.assertNotIn("stand", self.robot.calls)


@unittest.skipUnless(shutil.which("node"), "Node executes the actual dashboard script")
class BrowserOutcomeTests(unittest.TestCase):
    def js(self, check):
        source = DASHBOARD_HTML.split("<script>", 1)[1].split("function drawMap(s){", 1)[0]
        harness = r"""
const assert=require('assert');let mono=10000,wall=1000000;
const performance={now:()=>mono},Date={now:()=>wall};
const nodes=new Map(),document={getElementById:id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',value:'hello',setAttribute(){},removeAttribute(){}});return nodes.get(id);}};
function fresh(sequence=1,events=[]){
  return {server_boot_id:'pi',server_at:wall/1000,revision:sequence,receivedPerf:mono,
    serverAgeAtReceipt:0,sourceAgeAtReceipt:0,telemetry_age_ms:0,
    brain:{_publisher_id:'publisher',_publisher_started_at_ns:1,_sequence:sequence,
      _produced_at_pi_lower:wall/1000,_received_at:wall/1000,input_events:events,
      pipeline:{transport:{session_id:'session',ready:true}}}};
}
function receipt(id='event1'){return {ok:true,event_id:id,boot_id:'pi',session_id:'session'};}
function observed(id='event1',state='completed'){return {event_id:id,boot_id:'pi',session_id:'session',
  state,kind:'acknowledge',reason:'audio requested; inspect playback health'};}
"""
        setup = """
clockAnchor={wall,perf:mono,lower:0,upper:0,boot:'pi'};
latestState=fresh();streamConnected=true;
postJSON=async()=>receipt();
"""
        program = harness+source+"\n(async()=>{\n"+setup+check+"\n})().catch(e=>{console.error(e);process.exit(1);});"
        result = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_matching_later_observation_replaces_awaiting_label(self):
        self.js("""
await sendText('hello');latestState=fresh(2,[observed()]);
assert.equal(typeof renderInputOutcome,'function','no event-specific telemetry reconciliation');
renderInputOutcome(mono);assert($('input-result').textContent.includes('completed'));
assert(!$('input-result').textContent.includes('awaiting'));
assert(!$('input-result').textContent.includes('audible sound'));
""")

    def test_telemetry_can_overtake_http_acknowledgement(self):
        self.js("""
let release;postJSON=()=>new Promise(r=>release=r);const input=sendText('hello');
latestState=fresh(2,[observed()]);release(receipt());await input;
assert($('input-result').textContent.includes('completed'));
""")

    def test_old_result_and_wrong_session_cannot_complete_newer_input(self):
        self.js("""
await sendText('hello');postJSON=async()=>receipt('event2');await sendText('hello again');
latestState=fresh(2,[observed()]);renderInputOutcome(mono);
assert(!$('input-result').textContent.includes('completed'));
latestState=fresh(3,[{...observed('event2'),session_id:'old'}]);renderInputOutcome(mono);
assert(!$('input-result').textContent.includes('completed'));
latestState=fresh(4,[observed('event2')]);renderInputOutcome(mono);
assert($('input-result').textContent.includes('completed'));
""")

    def test_stale_telemetry_waits_and_missing_ids_are_uncorrelated(self):
        self.js("""
await sendText('hello');latestState=fresh(2,[observed()]);latestState.sourceAgeAtReceipt=4000;
renderInputOutcome(mono);assert(!$('input-result').textContent.includes('completed'));
postJSON=async()=>({ok:true});await sendText('hello again');renderInputOutcome(mono);
assert($('input-result').textContent.includes('uncorrelated'));
postJSON=async()=>({...receipt(),correlation_available:false});await sendText('hello again');
assert($('input-result').textContent.includes('uncorrelated'));
""")

    def test_loss_timeout_and_publisher_replacement_are_explicit_unknown(self):
        self.js("""
await sendText('hello');streamConnected=false;renderInputOutcome(mono);
assert($('input-result').textContent.includes('unknown'));
streamConnected=true;latestState=fresh(2,[observed()]);renderInputOutcome(mono);
assert($('input-result').textContent.includes('completed'));
await sendText('hello');latestState=fresh(3,[observed()]);latestState.brain._publisher_id='replacement';
renderInputOutcome(mono);assert($('input-result').textContent.includes('unknown'));
latestState=fresh(4,[observed()]);renderInputOutcome(mono);
assert(!$('input-result').textContent.includes('completed'));
await sendText('hello');mono+=31000;wall+=31000;renderInputOutcome(mono);
assert($('input-result').textContent.includes('unknown'));
""")

    def test_reverse_http_ack_does_not_replace_stop(self):
        self.js("""
let release;postJSON=()=>new Promise(r=>release=r);const hello=sendText('hello');
postJSON=async(path)=>path==='/stop'?{ok:true}:receipt('stop1');await stopCompanion();
release(receipt('hello1'));await hello;
latestState=fresh(2,[{...observed('stop1'),kind:'stop'}]);renderInputOutcome(mono);
assert($('input-result').textContent.includes('STOP acknowledged'));
assert($('input-result').textContent.includes('completed'));
""")

    def test_wrong_boot_and_reversed_telemetry_do_not_complete_input(self):
        self.js("""
await sendText('hello');latestState=fresh(3,[{...observed(),boot_id:'older-pi'}]);renderInputOutcome(mono);
assert(!$('input-result').textContent.includes('completed'));
latestState=fresh(2,[observed()]);renderInputOutcome(mono);
assert(!$('input-result').textContent.includes('completed'));
latestState=fresh(4,[observed()]);latestState.server_boot_id='replacement-pi';
clockAnchor.boot='replacement-pi';
renderInputOutcome(mono);assert($('input-result').textContent.includes('Pi restarted'));
latestState=fresh(5,[observed()]);renderInputOutcome(mono);
assert(!$('input-result').textContent.includes('completed'));
""")

    def test_session_replacement_cannot_reuse_old_session_result(self):
        self.js("""
await sendText('hello');latestState=fresh(2,[observed()]);
latestState.brain.pipeline.transport.session_id='replacement';renderInputOutcome(mono);
assert($('input-result').textContent.includes('session changed'));
latestState=fresh(3,[observed()]);renderInputOutcome(mono);
assert(!$('input-result').textContent.includes('completed'));
""")

    def test_rejected_and_failed_posts_keep_the_error_and_typed_message(self):
        self.js("""
postJSON=async()=>({ok:false,reason:'input queue full'});await sendText();
assert.equal($('input-result').textContent,'input queue full');assert.equal($('engagement').value,'hello');
postJSON=async()=>{throw Error('connection reset');};await sendText();
assert($('input-result').textContent.includes('Input not confirmed: connection reset'));
renderInputOutcome(mono);assert.equal($('engagement').value,'hello');
assert(!messagePending&&!$('send-control').disabled);
""")

    def test_stop_ack_survives_companion_delivery_failure(self):
        self.js("""
postJSON=async path=>{if(path==='/stop')return {ok:true};throw Error('connection reset');};
await stopCompanion();renderInputOutcome(mono);
assert($('input-result').textContent.includes('Pi STOP acknowledged'));
assert($('input-result').textContent.includes('delivery unconfirmed'));
assert(!stopPending&&!$('send-control').disabled);
""")


if __name__ == "__main__":
    unittest.main()
