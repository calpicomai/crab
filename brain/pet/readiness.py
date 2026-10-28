"""Read-only readiness metadata; --offline performs no HTTP or device calls."""
from __future__ import annotations
import argparse
import json
import math
import time
from .. import diagnostics

# Runtime HTTP dependencies are imported only after arguments and configuration
# validation. --help and --offline also work without the runtime dependencies.
RobotClient = None
httpx = None


def _runtime_dependencies():
    global RobotClient, httpx
    if RobotClient is None:
        from ..client import RobotClient
    if httpx is None:
        import httpx


def _public_metadata(value, *, depth=0):
    """Project remote diagnostics onto fixed names and nonpersonal values.

    A server error or audio device description may include paths or credentials.
    Never echo arbitrary messages, identifiers, device names or response keys.
    """
    keys = {"ok", "available", "enabled", "simulate", "hardware_verified", "realtime",
            "path", "protocol_version", "motion", "motion_state", "state", "status",
            "inputs", "ultrasonic", "camera", "microphone", "speaker", "audio",
            "camera_capture", "distance_cm", "battery_v", "speed", "stopped",
            "actuators_enabled", "home_on_start", "provenance", "fresh", "age_s",
            "captured_at", "received_at", "max_age_s", "confidence", "used",
            "played", "play_requested", "checks", "player", "alsa", "tts", "piper", "service_account",
            "playback_devices", "capture_devices", "exit_code",
            "amplifier", "last_playback", "failure_reason", "reason", "detail"}
    tokens = {"real", "simulated", "unknown", "disabled", "missing", "error", "idle",
              "stopped", "moving", "unavailable", "available", "completed", "not_tested"}
    if depth > 5:
        return None
    if isinstance(value, dict):
        return {key: _public_metadata(item, depth=depth + 1) for key, item in value.items() if key in keys}
    if type(value) is float and not math.isfinite(value):
        return None
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str):
        return value if value in tokens else "reported detail withheld"
    return None


def realtime_readiness(robot_health):
    """Check the HTTP advertisement without acquiring a control session.

    Opening and closing a WebSocket is not a read-only probe: its disconnect
    latches Pi STOP. A matching advertisement is compatibility evidence only,
    not a claim that the live connection or hardware has been tested.
    """
    from shared.realtime import PROTOCOL_VERSION, WS_PATH
    advertised = robot_health.get("realtime") if isinstance(robot_health, dict) else None
    reason = None
    if not isinstance(advertised, dict):
        reason = "Pi /health is missing the realtime transport advertisement"
    elif advertised.get("available") is not True:
        reason = "Pi /health reports realtime transport unavailable"
    elif (type(advertised.get("protocol_version")) is not int
          or advertised["protocol_version"] != PROTOCOL_VERSION):
        reason = f"Pi realtime protocol is incompatible; this brain requires version {PROTOCOL_VERSION}"
    elif advertised.get("path") != WS_PATH:
        reason = f"Pi realtime endpoint is incompatible; this brain requires {WS_PATH}"
    if reason:
        reason += ". Update Pi and Jetson/brain together to a matching Crab release, then rerun readiness."
    public_advertised = _public_metadata(advertised)
    if isinstance(public_advertised, dict) and advertised.get("path") == WS_PATH:
        public_advertised["path"] = WS_PATH
    return {"compatible": reason is None, "source": "/health.realtime",
            "expected": {"available": True, "path": WS_PATH, "protocol_version": PROTOCOL_VERSION},
            "advertised": public_advertised,
            "live_connection_tested": False, "failure_reason": reason}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--offline", action="store_true", help="Local metadata only; no HTTP, control, capture, inference or playback.")
    parser.add_argument("--ros-humble", action="store_true", help="Include static Jammy arm64/Python ABI comparison; does not import ROS.")
    parser.add_argument("--profile", choices=("interaction", "quiet"), default="interaction",
                        help="Label the launcher profile being inspected; no companion starts.")
    args = parser.parse_args(argv)
    report = diagnostics.local_report(ros_humble=args.ros_humble, profile=args.profile)
    report.update(offline=args.offline, actuator_test="not requested", checks={})
    failures = list(report.pop("configuration_errors"))

    def finish():
        if failures:
            report["failure_reason"] = "; ".join(failures)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 1 if failures else 0

    if args.offline:
        report["checks"]["remote_diagnostics"] = {"state": "disabled", "failure_reason": "offline requested"}
        return finish()
    if failures:
        report["checks"]["remote_diagnostics"] = {"state": "not_tested", "failure_reason": "invalid active configuration"}
        return finish()
    try:
        _runtime_dependencies()
        from shared import AUDIO_SELF_TEST_PATH
        from shared.local import require_local_url
        if args.base_url is not None:
            require_local_url(args.base_url)
    except Exception as exc:
        failures.append(f"readiness setup failed ({type(exc).__name__}); check active link settings and dependencies")
        return finish()

    def check(name, output, operation):
        started = time.monotonic()
        try:
            value = operation()
            if not isinstance(value, dict):
                raise ValueError("diagnostic response must be an object")
            json.dumps(value, allow_nan=False)
            report[output] = _public_metadata(value)
            result = {"state": "completed", "failure_reason": None}
        except Exception as exc:
            # Exception messages can contain endpoint credentials, device paths,
            # utterances, or arbitrary remote content. Expose only the category.
            reason = f"diagnostic failed ({type(exc).__name__}); private detail withheld"
            failures.append(f"{name}: {reason}")
            result = {"state": "error", "failure_reason": reason}
        result["duration_ms"] = round((time.monotonic()-started)*1000, 1)
        report["checks"][name] = result
        return result["state"] == "completed"

    try:
        client = RobotClient(args.base_url, timeout=3)
    except Exception as exc:
        failures.append(f"robot client setup failed ({type(exc).__name__}); check active link settings")
        return finish()
    with client:
        def robot_health():
            value = client.health()
            # Do not retain derived fields until the complete response passes
            # the same finite-JSON validation as every other diagnostic.
            if not isinstance(value, dict):
                raise ValueError("diagnostic response must be an object")
            json.dumps(value, allow_nan=False)
            # Validate the actual advertisement, independently of display
            # redaction. Only the safe projection is retained in the report.
            report["realtime"] = realtime_readiness(value)
            return value

        health_ok = check("robot_health", "robot", robot_health)
        if "realtime" not in report:
            report["realtime"] = realtime_readiness(None)
        if not health_ok:
            report["realtime"]["failure_reason"] = "Pi health request failed; realtime compatibility was not tested"
            report["checks"]["realtime_advertisement"] = {"state": "not_tested", "failure_reason": report["realtime"]["failure_reason"]}
        elif not report["realtime"]["compatible"]:
            reason = report["realtime"]["failure_reason"]
            failures.append(reason)
            report["checks"]["realtime_advertisement"] = {"state": "error", "failure_reason": reason}
        else:
            report["checks"]["realtime_advertisement"] = {"state": "completed", "failure_reason": None}
        status_ok = check("robot_status", "status", lambda: client.get_status().model_dump(mode="json"))
        if status_ok and report["status"].get("ok") is not True:
            reason = "robot status request rejected"
            report["checks"]["robot_status"].update(state="error", failure_reason=reason)
            failures.append("robot_status: "+reason)

        def audio_diagnostics():
            response = httpx.post(client.base_url+AUDIO_SELF_TEST_PATH, json={"play": False}, timeout=8,
                                  trust_env=False, follow_redirects=False)
            response.raise_for_status()
            value = response.json()
            if isinstance(value, dict) and value.get("played") is True:
                raise ValueError("audio endpoint reported playback despite the read-only request")
            return value

        check("audio_diagnostics", "audio_diagnostics", audio_diagnostics)

        # Passive metadata replaces constructing the active Voice service.
        report["jetson_audio"] = report["profile"]["capabilities"]["speech_output"].copy()
        report["checks"]["jetson_audio"] = {"state": "completed", "failure_reason": None,
                                             "scope": "dependency and configured-file metadata only"}
    return finish()

if __name__ == "__main__":
    raise SystemExit(main())
