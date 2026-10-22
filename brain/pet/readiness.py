"""Read-only readiness report; never executes a movement or plays sound."""
from __future__ import annotations
import argparse
import json
import httpx
from shared import AUDIO_SELF_TEST_PATH, AUDIO_PLAY_PATH
from ..client import RobotClient
from . import config
from .voice import Voice


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    args = parser.parse_args(argv)
    report = {"hardware_verified": False, "actuator_test": "not requested"}
    with RobotClient(args.base_url, timeout=3) as client:
        try:
            report["robot"] = client.health()
            report["status"] = client.get_status().model_dump(mode="json")
            response = httpx.post(client.base_url+AUDIO_SELF_TEST_PATH, json={"play": False}, timeout=8, trust_env=False, follow_redirects=False)
            response.raise_for_status()
            report["audio_diagnostics"] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            report["failure_reason"] = str(exc)[:300]
        voice = Voice(config.PET_VOICE, config.PET_VOICE_MODEL, config.PET_VOICE_PLAYER,
                      sink=config.PET_AUDIO_SINK, play_url=client.base_url+AUDIO_PLAY_PATH)
        report["jetson_audio"] = voice.health()
    print(json.dumps(report, indent=2))
    return 1 if "failure_reason" in report else 0

if __name__ == "__main__":
    raise SystemExit(main())
