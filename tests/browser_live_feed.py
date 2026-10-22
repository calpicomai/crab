"""Opt-in Chromium smoke check against an explicit local simulation server.

No browser package installation is needed: uses installed chromium + websockets.
Run: brain/.venv/bin/python tests/browser_live_feed.py --output-dir /tmp/crab-browser
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_url(client, url, process):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError(f"process exited before {url} was ready")
        try:
            response = client.get(url)
            response.raise_for_status()
            return response
        except httpx.HTTPError:
            time.sleep(.05)
    raise TimeoutError(url)


class CDP:
    def __init__(self, url):
        self._connection = connect(url, open_timeout=5, max_size=12 * 1024 * 1024)
        self.socket = self._connection.__enter__()
        self.ident = 0
        self.errors = []
        self.camera_inflight = set()
        self.max_camera_inflight = 0
        self.camera_requests = 0
        self.event_stream_requests = 0

    def event(self, message):
        method, params = message.get("method"), message.get("params", {})
        if method == "Runtime.exceptionThrown":
            self.errors.append(params)
        elif method == "Network.requestWillBeSent":
            url = params["request"]["url"]
            if "/camera/frame" in url:
                self.camera_requests += 1
                self.camera_inflight.add(params["requestId"])
                self.max_camera_inflight = max(self.max_camera_inflight, len(self.camera_inflight))
            if "/sim/events" in url:
                self.event_stream_requests += 1
        elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
            self.camera_inflight.discard(params["requestId"])

    def call(self, method, params=None):
        self.ident += 1
        self.socket.send(json.dumps({"id": self.ident, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.socket.recv(timeout=5))
            if message.get("id") == self.ident:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})
            self.event(message)

    def value(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def wait(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.value(predicate):
                return
            time.sleep(.03)
        raise AssertionError(f"browser condition failed: {predicate}")

    def close(self):
        self._connection.__exit__(None, None, None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/crab-browser"))
    args = parser.parse_args(argv)
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if not chromium:
        parser.error("installed Chromium is required; no installation is performed")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    server_port, browser_port = port(), port()
    base = f"http://127.0.0.1:{server_port}"
    env = {**os.environ, "PICRAWLER_SIMULATE": "1", "PICRAWLER_SIM_WORLD": "1",
           "PICRAWLER_ACTUATORS_ENABLED": "0", "PICRAWLER_HOME_ON_START": "none",
           "PICRAWLER_HOST": "127.0.0.1", "PICRAWLER_PORT": str(server_port),
           "PICRAWLER_AUDIO_ENABLED": "0", "PICRAWLER_CAMERA_ENABLED": "1"}
    server = browser = cdp = None
    report = {}
    with tempfile.TemporaryDirectory(prefix="crab-browser-profile-") as profile, \
         open(args.output_dir / "server.log", "w") as server_log, \
         open(args.output_dir / "chromium.log", "w") as browser_log, \
         httpx.Client(timeout=1, trust_env=False) as http:
        try:
            server = subprocess.Popen([sys.executable, "-m", "robot.server"], cwd=ROOT,
                                      env=env, stdout=server_log, stderr=subprocess.STDOUT)
            wait_url(http, base + "/health", server)
            browser = subprocess.Popen([chromium, "--headless", "--disable-gpu", "--no-sandbox",
                "--disable-dev-shm-usage", "--disable-background-networking", "--no-first-run",
                "--no-default-browser-check", f"--remote-debugging-port={browser_port}",
                f"--user-data-dir={profile}", "about:blank"], stdout=browser_log, stderr=subprocess.STDOUT)
            targets = wait_url(http, f"http://127.0.0.1:{browser_port}/json/list", browser).json()
            cdp = CDP(next(target["webSocketDebuggerUrl"] for target in targets if target["type"] == "page"))
            cdp.call("Runtime.enable")
            cdp.call("Network.enable")
            cdp.call("Page.enable")
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 1000,
                                                            "deviceScaleFactor": 1, "mobile": False})
            cdp.call("Page.navigate", {"url": base + "/sim"})
            cdp.wait("document.readyState === 'complete'")
            cdp.wait("document.querySelector('#cam').naturalWidth > 0")
            fixture = {"name": "Latency fixture", "mood": "curious", "episode": {"phase": "listen"},
                       "battery_v": 7.4,
                       "mode": "interaction", "last_say": "Latest state arrived.",
                       "pipeline": {"transport": {"transport": "websocket", "ready": True,
                           "connected": True, "last_rtt_ms": 4.2, "failure_reason": None}},
                       "inputs": {"camera": {"available": True, "provenance": "simulated",
                           "fresh": True, "age_s": 0, "captured_at": time.time(),
                           "max_age_s": 2, "used": True},
                           "battery": {"available": True, "provenance": "simulated",
                           "fresh": True, "age_s": 0, "captured_at": time.time(),
                           "max_age_s": 2, "used": True}},
                       "intentions": [], "movements": [], "_publisher_id": "browser-check", "_sequence": 1,
                       "_produced_at": time.time(), "_sent_at": time.time(),
                       "_publisher_started_at_ns": time.time_ns()}
            started = time.monotonic()
            response = http.post(base + "/sim/brain", json=fixture)
            response.raise_for_status()
            assert response.json()["ok"], response.text
            cdp.wait("document.querySelector('#petname').textContent.includes('Latency fixture')")
            report["post_to_visible_ms"] = round((time.monotonic()-started)*1000, 2)
            report["visible_name"] = cdp.value("document.querySelector('#petname').textContent")
            report["initial_feed_text"] = cdp.value("document.querySelector('#feed-state').textContent")
            cdp.wait("document.querySelector('#feed-stats').textContent.includes('Pi WebSocket ready')")
            report["websocket_health_visible"] = cdp.value("document.querySelector('#feed-stats').textContent.includes('round trip 4.2ms')")
            assert report["websocket_health_visible"], report
            # Keep pumping CDP so completed requests are observed, not counted as a backlog.
            end = time.monotonic()+1.2
            while time.monotonic() < end:
                cdp.value("document.querySelector('#cam').naturalWidth")
                time.sleep(.05)
            report["battery_samples_for_one_revision"] = cdp.value("battHist.length")
            assert report["battery_samples_for_one_revision"] == 1, report
            screenshot = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
            (args.output_dir / "live-dashboard.png").write_bytes(base64.b64decode(screenshot["data"]))
            cdp.wait("document.querySelector('#inputs').textContent.includes('stale')", timeout=3)
            report["capture_age_remains_stale"] = True
            # A live connection must not disguise an expired companion sample.
            cdp.wait("document.querySelector('#episode').textContent.includes('stale')", timeout=5)
            report["stale_telemetry_visible"] = True
            report["connection_while_telemetry_stale"] = cdp.value("document.querySelector('#feed-state').textContent")
            assert report["connection_while_telemetry_stale"] == "live connection", report
            # Interrupt only the browser's link, then publish during the outage.
            cdp.call("Network.emulateNetworkConditions", {"offline": True, "latency": 0,
                "downloadThroughput": -1, "uploadThroughput": -1})
            # Chromium's offline emulation doesn't terminate an already-open SSE
            # socket. Exercise the page's pause/reopen path while offline too.
            cdp.value("pauseFeeds(); connectState(); nextCamera(); true")
            cdp.wait("document.querySelector('#feed-state').textContent.includes('reconnecting')", timeout=3)
            fixture.update(name="Latest after reconnect", _sequence=2,
                           _produced_at=time.time(), _sent_at=time.time())
            assert http.post(base + "/sim/brain", json=fixture).json()["ok"]
            cdp.call("Network.emulateNetworkConditions", {"offline": False, "latency": 0,
                "downloadThroughput": -1, "uploadThroughput": -1})
            started = time.monotonic()
            cdp.wait("document.querySelector('#feed-state').textContent === 'live connection'", timeout=7)
            cdp.wait("document.querySelector('#petname').textContent === 'Latest after reconnect'")
            report["reconnect_to_latest_ms"] = round((time.monotonic()-started)*1000, 2)
            report["reconnect_latest_visible"] = True
            report.update(camera_requests=cdp.camera_requests, peak_camera_requests=cdp.max_camera_inflight,
                          event_stream_requests=cdp.event_stream_requests, javascript_errors=cdp.errors,
                          scope="Headless Chromium against explicit local simulation; no physical camera/Wi-Fi/Jetson")
            assert not cdp.errors, cdp.errors
            assert cdp.camera_requests >= 3, report
            assert cdp.max_camera_inflight <= 1, report
            assert cdp.event_stream_requests >= 1, report
            # Mobile rendering must not create horizontal overflow.
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844,
                                                            "deviceScaleFactor": 1, "mobile": True})
            report["mobile_has_horizontal_overflow"] = cdp.value("document.documentElement.scrollWidth > window.innerWidth")
            assert not report["mobile_has_horizontal_overflow"], report
            (args.output_dir / "report.json").write_text(json.dumps(report, indent=2)+"\n")
            print(json.dumps({k:v for k,v in report.items() if k != "initial_feed_text"}, indent=2))
            return 0
        finally:
            if cdp:
                cdp.close()
            for process in (browser, server):
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)


if __name__ == '__main__':
    raise SystemExit(main())
