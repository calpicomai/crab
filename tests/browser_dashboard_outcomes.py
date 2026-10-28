"""Native Chromium + HTTP/SSE/WS companion input outcomes on loopback simulation.

No fetch/EventSource fixtures, configured robot URL, inference, hardware or audio.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import httpx

from browser_live_feed import CDP, ROOT, port, wait_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/crab-outcome-browser"))
    args = parser.parse_args()
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if not chromium:
        parser.error("installed Chromium required")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    server_port, browser_port = port(), port()
    base = f"http://127.0.0.1:{server_port}"
    processes, cdp = [], None
    report = {"scope": "Native browser and companion on explicit loopback simulation; no physical motion or audibility evidence", "outcomes": []}
    with tempfile.TemporaryDirectory(prefix="crab-outcome-browser-") as temporary, \
         httpx.Client(timeout=2, trust_env=False) as http:
        temporary = Path(temporary)
        env = {**os.environ, "PICRAWLER_SIMULATE": "1", "PICRAWLER_SIM_WORLD": "0",
               "PICRAWLER_ACTUATORS_ENABLED": "0", "PICRAWLER_HOME_ON_START": "none",
               "PICRAWLER_AUDIO_ENABLED": "0", "PICRAWLER_CAMERA_ENABLED": "0",
               "PICRAWLER_HOST": "127.0.0.1", "PICRAWLER_PORT": str(server_port),
               "PET_HOME": str(temporary/"pet"), "PET_MEMORY_DB": str(temporary/"pet/memory.db"),
               "PET_IDENTITY_FILE": str(temporary/"pet/identity.json"), "PET_WORLD_DB": str(temporary/"pet/world.db"),
               "PET_VOICE": "0", "PET_STT": "0"}
        logs = []
        try:
            def launch(name, command):
                log = open(args.output_dir/(name+".log"), "w")
                logs.append(log)
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
                processes.append(process)
                return process

            server = launch("server", [sys.executable, "-m", "robot.server"])
            wait_url(http, base+"/health", server)
            launch("companion", [sys.executable, "-m", "brain.pet", "--sim", "--no-llm", "--no-stt",
                "--no-voice", "--no-camera", "--no-emote", "--dashboard", "--base-url", base, "--duration", "45"])
            browser = launch("chromium", [chromium, "--headless", "--disable-gpu", "--no-sandbox",
                "--disable-dev-shm-usage", "--disable-background-networking", "--no-first-run",
                f"--remote-debugging-port={browser_port}", f"--user-data-dir={temporary/'profile'}", "about:blank"])
            deadline = time.monotonic()+8
            while True:
                targets = wait_url(http, f"http://127.0.0.1:{browser_port}/json/list", browser).json()
                pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if pages:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Chromium page target unavailable")
                time.sleep(.03)
            cdp = CDP(pages[0]["webSocketDebuggerUrl"])
            for method in ("Runtime.enable", "Network.enable", "Page.enable"):
                cdp.call(method)
            cdp.call("Page.navigate", {"url": base+"/sim"})
            cdp.wait("typeof latestState!=='undefined' && latestState?.brain?.pipeline?.transport?.ready===true && telemetryAge(latestState)<1000")

            def outcome(name, action):
                old = cdp.value("trackedInput?.id||null")
                cdp.value(action+";true")
                cdp.wait("trackedInput?.id && trackedInput.id!=="+json.dumps(old)+" && trackedInput.done===true")
                observed = cdp.value("({receipt:trackedInput,event:latestState.brain.input_events.find(e=>e.event_id===trackedInput.id),label:document.querySelector('#input-result').textContent,publisher:latestState.brain._publisher_id})")
                assert observed["event"]["state"] in {"completed", "processed"}, observed
                assert "awaiting" not in observed["label"], observed
                for key, receipt_key in (("event_id", "id"), ("boot_id", "boot"), ("session_id", "session")):
                    assert observed["event"][key] == observed["receipt"][receipt_key], observed
                observed["input"] = name
                report["outcomes"].append(observed)

            outcome("Hello", "document.querySelector('[data-message=hello]').click()")
            outcome("STOP", "document.querySelector('#stop-control').click()")
            assert "Pi STOP acknowledged" in report["outcomes"][-1]["label"]
            cdp.wait("!document.querySelector('#resume-review').disabled")
            cdp.value("document.querySelector('#resume-review').click();true")
            assert cdp.value("!document.querySelector('#resume-confirm').hidden")
            outcome("Resume", "document.querySelector('#resume-confirm-button').click()")
            assert cdp.value("latestState.brain.stopped===false")

            # Exercise the real stream lifecycle; the pending input is not
            # considered complete from cached telemetry while disconnected.
            cdp.value("pauseFeeds();sendText('listen');true")
            cdp.wait("trackedInput!==null && document.querySelector('#input-result').textContent.includes('unknown')")
            report["disconnected_label"] = cdp.value("document.querySelector('#input-result').textContent")
            cdp.value("resumeFeeds();true")
            cdp.wait("streamConnected && trackedInput.done===true && document.querySelector('#input-result').textContent.includes('completed')")
            report["reconnected_label"] = cdp.value("document.querySelector('#input-result').textContent")
            report["javascript_errors"] = cdp.errors
            assert not cdp.errors, cdp.errors
            (args.output_dir/"report.json").write_text(json.dumps(report, indent=2)+"\n")
            print(json.dumps(report, indent=2))
        finally:
            if cdp:
                cdp.close()
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            for log in logs:
                log.close()


if __name__ == "__main__":
    main()
