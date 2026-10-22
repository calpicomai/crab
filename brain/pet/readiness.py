"""Read-only readiness report; never executes a movement or plays sound."""
from __future__ import annotations
import argparse
import json
import httpx
from shared import AUDIO_SELF_TEST_PATH, AUDIO_PLAY_PATH
from shared.realtime import PROTOCOL_VERSION, WS_PATH
from ..client import RobotClient
from . import config
from .voice import Voice


def realtime_readiness(robot_health):
    """Check the HTTP advertisement without acquiring a control session.

    Opening and closing a WebSocket is not a read-only probe: its disconnect
    latches Pi STOP. A matching advertisement is compatibility evidence only,
    not a claim that the live connection or hardware has been tested.
    """
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
    return {"compatible": reason is None, "source": "/health.realtime",
            "expected": {"available": True, "path": WS_PATH, "protocol_version": PROTOCOL_VERSION},
            "advertised": advertised,
            "live_connection_tested": False, "failure_reason": reason}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    args = parser.parse_args(argv)
    report = {"hardware_verified": False, "actuator_test": "not requested"}
    with RobotClient(args.base_url, timeout=3) as client:
        try:
            report["robot"] = client.health()
            report["realtime"] = realtime_readiness(report["robot"])
            if not report["realtime"]["compatible"]:
                report["failure_reason"] = report["realtime"]["failure_reason"]
            report["status"] = client.get_status().model_dump(mode="json")
            response = httpx.post(client.base_url+AUDIO_SELF_TEST_PATH, json={"play": False}, timeout=8, trust_env=False, follow_redirects=False)
            response.raise_for_status()
            report["audio_diagnostics"] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            report["failure_reason"] = "; ".join(filter(None, [report.get("failure_reason"), str(exc)[:300]]))
        voice = Voice(config.PET_VOICE, config.PET_VOICE_MODEL, config.PET_VOICE_PLAYER,
                      sink=config.PET_AUDIO_SINK, play_url=client.base_url+AUDIO_PLAY_PATH)
        report["jetson_audio"] = voice.health()
    print(json.dumps(report, indent=2))
    return 1 if "failure_reason" in report else 0

if __name__ == "__main__":
    raise SystemExit(main())
