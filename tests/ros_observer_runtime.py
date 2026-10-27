"""Actual Humble ROSIDL + two-process DDS test, using only loopback fake HTTP."""
from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

import rclpy
from rclpy.duration import Duration
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from crab_observation_msgs.msg import ObservationSnapshot


def main():
    lock = threading.Lock()
    fixture = dict(boot="fixture-pi-one", sequence=0, offset=.03, failure=False,
                   freeze=False, missing=False, brain=None, requests=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            with lock:
                fixture["requests"].append(("GET", self.path))
                if fixture["failure"]:
                    self.send_error(503); return
                now = time.time(); pi_now = now + fixture["offset"]
                boot = fixture["boot"]
                if self.path == "/sim/clock":
                    value = dict(server_at=pi_now, server_boot_id=boot)
                elif self.path == "/sim/state":
                    if not fixture["freeze"] or fixture["brain"] is None:
                        fixture["sequence"] += 1
                        health = dict(available=True, provenance="simulated", captured_at=pi_now-.1,
                                      received_at=now-.09, age_s=.1, max_age_s=1., fresh=True,
                                      used=True, source="explicit synthetic input")
                        fixture["brain"] = dict(
                            _publisher_id="fixture-jetson-"+boot, _publisher_started_at_ns=100,
                            _sequence=fixture["sequence"], _produced_at=now-.02,
                            _produced_at_pi_lower=pi_now-.03, _received_at=pi_now-.01,
                            inputs={"battery":health, "ultrasonic":dict(available=False,
                                provenance="simulated", captured_at=None, fresh=False, used=False,
                                failure_reason="synthetic missing echo")},
                            battery_v=7.4, distance_cm=None, stopped=True, motion_enabled=False,
                            pipeline={"transport":{"boot_id":boot,"session_id":"fixture-session"}},
                            last_heard="PRIVATE FIXTURE TEXT MUST NOT BE PUBLISHED")
                    value = dict(server_at=pi_now, server_boot_id=boot,
                        revision=fixture["sequence"], brain=copy.deepcopy(fixture["brain"]),
                        robot_runtime={"simulate":False,"motion":{"enabled":False,"stop_latched":True}},
                        host_vitals={"host":"pi","captured_at":pi_now-.05,"age_s":.05,
                                     "cpu_percent":12.,"source":"synthetic vitals"})
                    if fixture["missing"]:
                        value["brain"] = {}
                        value["host_vitals"] = {"host":"pi","captured_at":None,
                                                "age_s":None,"cpu_percent":None}
                else:
                    self.send_error(404); return
            data = json.dumps(value).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(data))); self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            with lock:
                fixture["requests"].append(("POST", self.path))
            self.send_error(405)

    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    rclpy.init(args=[])
    from brain.ros_observer.core import HTTPObserver
    from brain.ros_observer.node import create_node
    # FastDDS endpoint discovery omits history/depth (UNKNOWN/0). Check those
    # on real local Publisher objects; separately inspect advertised policies.
    declaration_worker = HTTPObserver(f"http://127.0.0.1:{server.server_port}")
    declaration = create_node(declaration_worker)
    try:
        for publisher in (declaration.observations, declaration.diagnostics):
            assert publisher.qos_profile.depth==1
            assert publisher.qos_profile.history==HistoryPolicy.KEEP_LAST
    finally:
        declaration.destroy_node(); declaration_worker.close()
    probe = rclpy.create_node("crab_observer_probe",start_parameter_services=False,enable_rosout=False)
    qos = QoSProfile(depth=1,history=HistoryPolicy.KEEP_LAST,reliability=ReliabilityPolicy.BEST_EFFORT,
                     durability=DurabilityPolicy.VOLATILE,lifespan=Duration(seconds=2))
    snapshots, diagnostics = [], []
    probe.create_subscription(ObservationSnapshot,"/crab/observations",snapshots.append,qos)
    probe.create_subscription(DiagnosticArray,"/crab/diagnostics",diagnostics.append,qos)
    child = None
    report = {"scope":"Generated ROSIDL and real two-process Humble/FastDDS on amd64 Jammy; fake loopback HTTP only, no hardware acceptance."}
    with tempfile.TemporaryFile(mode="w+") as output:
        try:
            child = subprocess.Popen(["bash","ros2/run-observer.sh",
                "--base-url",f"http://127.0.0.1:{server.server_port}","--poll-hz","5"],
                env={**os.environ,"CRAB_OBSERVER_PYTHON":sys.executable},
                stdout=output,stderr=subprocess.STDOUT)

            def until(predicate, timeout=8):
                deadline = time.monotonic()+timeout
                while time.monotonic()<deadline:
                    if child.poll() is not None:
                        output.seek(0); raise AssertionError("observer exited: "+output.read()[-3000:])
                    rclpy.spin_once(probe,timeout_sec=.05)
                    if predicate(): return
                output.seek(0); raise AssertionError("DDS condition timed out: "+output.read()[-2000:])

            until(lambda:snapshots and snapshots[-1].companion_fresh and diagnostics)
            first = snapshots[-1]
            assert first.context.robot_boot_id == "fixture-pi-one"
            assert first.context.offset_bounds_valid and first.companion_context.sequence_valid
            assert first.companion_context.session_id == "fixture-session"
            assert first.battery_v_valid and not first.sonar_cm_valid
            assert first.motion_enabled_valid and not first.motion_enabled and first.stop_latched
            assert not next(x for x in first.inputs if x.name=="ultrasonic").available
            assert not next(x for x in first.inputs if x.name=="battery").physically_verified
            until(lambda:("crab_observer","/") in probe.get_node_names_and_namespaces())
            assert probe.get_subscriber_names_and_types_by_node("crab_observer","/") == []
            assert probe.get_service_names_and_types_by_node("crab_observer","/") == []
            for topic in ("/crab/observations", "/crab/diagnostics"):
                endpoints = probe.get_publishers_info_by_topic(topic)
                endpoint = next(x for x in endpoints if x.node_name=="crab_observer")
                policy = endpoint.qos_profile
                assert policy.reliability==ReliabilityPolicy.BEST_EFFORT
                assert policy.durability==DurabilityPolicy.VOLATILE
                assert policy.lifespan.nanoseconds==2_000_000_000
            report["publisher_qos_latest_volatile_two_second_lifespan"] = True
            report["typed_delivery_and_no_control_interfaces"] = True

            with lock: fixture["missing"] = True
            until(lambda:snapshots and not snapshots[-1].companion_context.publisher_id_valid)
            missing = snapshots[-1]
            assert not missing.companion_fresh and not missing.pi.cpu_percent_valid
            assert not missing.pi.context.fresh and not missing.inputs
            assert not missing.battery_v_valid and not missing.sonar_cm_valid
            with lock: fixture["missing"] = False
            until(lambda:snapshots[-1].companion_fresh)
            report["missing_null_payload_recovers_without_false_health"] = True

            with lock: fixture["freeze"] = True; frozen_stamp=fixture["brain"]["_produced_at"]
            until(lambda:snapshots and not snapshots[-1].companion_fresh,timeout=5)
            assert snapshots[-1].companion_context.captured_at == frozen_stamp
            assert not next(x for x in snapshots[-1].inputs if x.name=="battery").context.fresh
            report["repeated_receipt_does_not_refresh_capture"] = True
            with lock: fixture["freeze"] = False; fixture["offset"] = .5
            until(lambda:not snapshots[-1].context.offset_bounds_valid)
            assert not snapshots[-1].companion_fresh
            report["excessive_skew_published_as_untrusted"] = True
            with lock:
                fixture.update(boot="fixture-pi-two",sequence=0,offset=.03,brain=None)
            until(lambda:snapshots[-1].context.robot_boot_id=="fixture-pi-two" and snapshots[-1].companion_fresh)
            report["boot_change_recovers_without_old_identity"] = True

            with lock: fixture["failure"] = True
            until(lambda:any(x.name=="crab/observer" and x.level==DiagnosticStatus.ERROR
                             for x in diagnostics[-1].status))
            seen=len(snapshots)
            deadline=time.monotonic()+.8
            while time.monotonic()<deadline:rclpy.spin_once(probe,timeout_sec=.05)
            assert len(snapshots)==seen,"HTTP failure republished an old snapshot"
            report["failure_diagnostics_without_old_snapshot_republication"] = True
            with lock: fixture["failure"] = False
            until(lambda:len(snapshots)>seen and snapshots[-1].companion_fresh)
            before=snapshots[-1].companion_context.sequence
            time.sleep(.8)  # Deliberately stall only the test subscriber.
            until(lambda:snapshots[-1].companion_context.sequence>before+1)
            assert snapshots[-1].context.age_upper_s < 2
            report["stalled_subscriber_recovers_to_newest"] = True

            started=time.monotonic();child.send_signal(signal.SIGTERM);code=child.wait(timeout=3)
            assert code==0,code
            report["sigterm_exit_s"]=round(time.monotonic()-started,3)
            report["http_requests"]=len(fixture["requests"])
            assert set(fixture["requests"]) == {("GET","/sim/clock"),("GET","/sim/state")}
            report["get_only_allowlisted_paths"] = True
            print("CRAB_ROS_RUNTIME_RESULT="+json.dumps(report,sort_keys=True))
        finally:
            if child and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:child.kill();child.wait()
            probe.destroy_node();rclpy.shutdown()
            server.shutdown();server.server_close();thread.join(timeout=1)


if __name__ == "__main__":
    main()
