"""Measure coordinator/scenario CPU, process RAM and latency on this host.

This deliberately does not call a robot or load a model. Run the separate
hardware acceptance measurements on the Jetson before claiming its 8GB budget
fits STT, TTS, detectors and an LLM together.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import time
from pathlib import Path

from .scenarios import SCENARIOS, run_scenario


def measure(iterations=10):
    wall_start, cpu_start = time.perf_counter(), time.process_time()
    durations = []
    for _ in range(iterations):
        for name in SCENARIOS:
            start = time.perf_counter()
            run_scenario(name)
            durations.append((time.perf_counter() - start) * 1000)
    elapsed = time.perf_counter() - wall_start
    cpu = time.process_time() - cpu_start
    # Linux reports KiB; macOS reports bytes.
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        peak_rss /= 1024
    ordered = sorted(durations)
    return {"scope": "hardware-free coordinator scenarios only", "platform": platform.platform(),
            "machine": platform.machine(), "python": platform.python_version(),
            "logical_cpus": os.cpu_count(), "scenario_runs": len(durations),
            "wall_s": round(elapsed, 4), "process_cpu_s": round(cpu, 4),
            "one_core_cpu_percent": round(cpu / elapsed * 100, 1),
            "peak_process_rss_mib": round(peak_rss / 1024, 2),
            "scenario_latency_ms": {"median": round(statistics.median(durations), 3),
                                    "p95": round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 3)},
            "model_inference_latency_ms": None,
            "jetson_8gb_acceptance": "pending: this host is not the Jetson; models not loaded",
            "hardware_motion": "not tested", "physical_audio": "not tested"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.iterations <= 1000:
        parser.error("iterations must be in 1..1000")
    data = json.dumps(measure(args.iterations), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(data)
    print(data, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
