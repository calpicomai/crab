"""Explicitly launched, publisher-only ROS adapter. No hardware/control imports."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, fields, is_dataclass
import os
import signal
import sys
import threading
import time

from .core import HTTPObserver, age_for_publish


@dataclass(frozen=True)
class Diagnostic:
    name: str
    level: int  # diagnostic_msgs: OK=0, WARN=1, ERROR=2
    message: str
    values: tuple[tuple[str, str], ...] = ()


def diagnostics_for(snapshot, health, error):
    """Bounded diagnostic descriptions, independently testable without ROS.

    OK describes an observation's transport/timing, never hardware acceptance.
    The original snapshot may be retained here for diagnostics only.
    """
    counters = tuple((key, str(health.get(key, "unknown"))) for key in
                     ("running", "completed", "failed", "replaced", "calibration_failed", "poll_hz"))
    if error or not health.get("running"):
        level, message = 2, error or health.get("failure_reason") or "HTTP observer is not running"
    elif snapshot is None:
        level, message = 1, "Waiting for the first read-only observation"
    elif not snapshot.context.timing_verified:
        level, message = 1, "Observation received; clock/age is unverified"
    elif not snapshot.context.fresh:
        level, message = 1, "Observation is stale"
    else:
        level, message = 0, "Current HTTP observation; no command authority or hardware acceptance"
    result = [Diagnostic("crab/observer", level, str(message)[:300], counters + (
        ("command_authority", "false"),
        ("calibration_reason", str(health.get("calibration_error", ""))[:300]),))]
    if snapshot is None:
        return result
    context = snapshot.companion_context
    result.append(Diagnostic("crab/companion", 0 if snapshot.companion_fresh else 1,
        "Current companion observation" if snapshot.companion_fresh else "Companion timing is stale or unknown",
        (("boot", context.robot_boot_id), ("session", context.session_id if context.session_id_valid else "unknown"),
         ("sequence", str(context.sequence) if context.sequence_valid else "unknown"),
         ("age_upper_s", str(context.age_upper_s) if context.age_upper_valid else "unknown"))))
    for item in snapshot.inputs:
        if item.failure_reason:
            level, message = 2, item.failure_reason
        elif not item.available_valid or not item.available:
            level, message = 1, "Input availability is unknown or unavailable"
        elif not item.context.fresh:
            level, message = 1, "Input age is stale or unknown"
        else:
            level, message = 0, "Current available observation; verification is reported separately"
        result.append(Diagnostic("crab/input/" + item.name, level, message,
            (("source", item.source if item.source_valid else "unknown"), ("provenance", item.provenance),
             ("available", str(item.available) if item.available_valid else "unknown"),
             ("used", str(item.used) if item.used_valid else "unknown"),
             ("physically_verified", str(item.physically_verified) if item.physically_verified_valid else "unknown"),
             ("age_upper_s", str(item.context.age_upper_s) if item.context.age_upper_valid else "unknown"),
             ("ignored_reason", item.ignored_reason))))
    for name, host in (("pi", snapshot.pi), ("jetson", snapshot.jetson)):
        current = host.host_valid and host.context.fresh
        result.append(Diagnostic("crab/host/" + name, 0 if current else 1,
            "Current host observation; individual metric validity is separate" if current else
            "Host observation is missing, stale or unverified",
            (("source", host.source), ("sampling_state", host.sampling_state or "unknown"),
             ("sampling_reason", host.sampling_reason),
             ("age_upper_s", str(host.age_s) if host.age_valid else "unknown"))))
    return result


def to_ros(value, message_types):
    """Only the four explicit dataclass contracts can cross this adapter."""
    if is_dataclass(value):
        message = message_types[type(value).__name__]()
        for field in fields(value):
            setattr(message, field.name, to_ros(getattr(value, field.name), message_types))
        return message
    if isinstance(value, tuple):
        return [to_ros(item, message_types) for item in value]
    return value


def create_node(observer):
    # Lazy imports keep --help and pure diagnostic tests usable without ROS.
    from rclpy.node import Node
    from rclpy.clock import Clock, ClockType
    from rclpy.duration import Duration
    from rclpy.qos import QoSProfile, HistoryPolicy, ReliabilityPolicy, DurabilityPolicy
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
    from crab_observation_msgs import msg

    message_types = {name: getattr(msg, name) for name in
                     ("ObservationSnapshot", "ObservationContext", "HostVitals", "InputObservation")}

    class ObservationNode(Node):
        def __init__(self):
            super().__init__("crab_observer", start_parameter_services=False, enable_rosout=False)
            qos = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST,
                reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE,
                lifespan=Duration(seconds=2))
            self.observations = self.create_publisher(msg.ObservationSnapshot, "/crab/observations", qos)
            self.diagnostics = self.create_publisher(DiagnosticArray, "/crab/diagnostics", qos)
            self.last_snapshot, self.error = None, ""
            self.timer = self.create_timer(.5, self.publish_latest,
                clock=Clock(clock_type=ClockType.STEADY_TIME))

        def publish_latest(self):
            result = observer.take_latest()
            now = time.monotonic()
            if result is not None:
                self.error = result.error
                if result.snapshot is not None:
                    self.last_snapshot = result.snapshot
                    self.observations.publish(to_ros(age_for_publish(result.snapshot, now), message_types))
            aged = age_for_publish(self.last_snapshot, now) if self.last_snapshot is not None else None
            diagnostic = DiagnosticArray()
            diagnostic.header.stamp = self.get_clock().now().to_msg()
            for record in diagnostics_for(aged, observer.health(), self.error):
                status = DiagnosticStatus()
                status.name, status.message = record.name, record.message
                # diagnostic_msgs uses IDL byte, whose Humble Python API is bytes.
                status.level = bytes([record.level])
                status.hardware_id = "read-only-observer"
                status.values = [KeyValue(key=key, value=value) for key, value in record.values]
                diagnostic.status.append(status)
            self.diagnostics.publish(diagnostic)

    return ObservationNode()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publish bounded read-only Crab observations into local ROS 2; never controls hardware.")
    parser.add_argument("--base-url", required=True, help="Explicit local Pi HTTP origin; no saved configuration is read")
    parser.add_argument("--poll-hz", type=float, default=1., help="HTTP polling rate, >0 and <=5 (default 1)")
    args = parser.parse_args(argv)
    try:
        observer = HTTPObserver(args.base_url, poll_hz=args.poll_hz)
    except ValueError as exc:
        parser.error(str(exc))
    # Process-local discovery restriction; no host/network configuration changes.
    # Networked ROS discovery is a later, separately reviewed deployment step.
    os.environ["ROS_LOCALHOST_ONLY"] = "1"
    os.environ.setdefault("ROS_DOMAIN_ID", "173")
    try:
        import rclpy
        from rclpy.signals import SignalHandlerOptions
        from rclpy.executors import ExternalShutdownException
        from crab_observation_msgs import msg  # noqa: F401: require the generated overlay before starting
    except ImportError:
        print("ROS observer unavailable: source Humble and the built message overlay, using its matching Python 3.10 environment.", file=sys.stderr)
        return 2
    stopped = threading.Event()
    previous = {number: signal.signal(number, lambda *_args: stopped.set())
                for number in (signal.SIGINT, signal.SIGTERM)}
    node = None
    try:
        rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
        node = create_node(observer)
        if not observer.start():
            raise RuntimeError("read-only HTTP observer did not start")
        while not stopped.is_set() and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=.2)
        return 0
    except (KeyboardInterrupt, ExternalShutdownException):
        return 0
    finally:
        observer.close(timeout=.5)
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
