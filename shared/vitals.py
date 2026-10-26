"""Small cached read-only Linux vitals, for observation rather than control."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time


def _read(path):
    with open(path) as source:
        return source.read(65536)


class SystemVitals:
    def __init__(self, host, *, clock=time.monotonic, wall=time.time,
                 process_clock=time.process_time, reader=_read, thermal_root="/sys/class/thermal"):
        self.host = host
        self.clock, self.wall, self.process_clock = clock, wall, process_clock
        self.reader, self.thermal_root = reader, Path(thermal_root)
        self._lock = threading.Lock()
        self._sample = None
        self._sample_at = None
        self._cpu = None
        self._process = None

    def snapshot(self):
        with self._lock:
            now = self.clock()
            if self._sample_at is None or now-self._sample_at >= 1 or now < self._sample_at:
                self._sample = self._collect(now)
                self._sample_at = now
            return {**self._sample, "reasons": dict(self._sample["reasons"]), "age_s": max(0, now-self._sample_at)}

    def _collect(self, now):
        result = {"host": self.host, "captured_at": self.wall(), "interval_s": 1,
                  "cpu_percent": None, "process_cpu_percent": None, "rss_mib": None,
                  "memory_used_mib": None, "memory_total_mib": None, "temperature_c": None,
                  "temperature_source": None, "reasons": {}}
        try:
            first = self.reader("/proc/stat").splitlines()[0].split()
            if first[0] != "cpu":
                raise ValueError("aggregate CPU counters unavailable")
            counters = tuple(int(x) for x in first[1:9])
            total, idle = sum(counters), counters[3]+counters[4]
            if self._cpu:
                delta, idle_delta = total-self._cpu[0], idle-self._cpu[1]
                if delta > 0 and 0 <= idle_delta <= delta:
                    result["cpu_percent"] = round(100*(delta-idle_delta)/delta, 1)
            self._cpu = (total, idle)
            if result["cpu_percent"] is None:
                result["reasons"]["cpu_percent"] = "warming up or counters reset"
        except (OSError, ValueError, IndexError) as exc:
            result["reasons"]["cpu_percent"] = str(exc)[:160]
        cpu_time = self.process_clock()
        if self._process and now > self._process[0] and cpu_time >= self._process[1]:
            result["process_cpu_percent"] = round(100*(cpu_time-self._process[1])/(now-self._process[0]), 1)
        self._process = (now, cpu_time)
        for path, destination, total_key, available_key in (
            ("/proc/meminfo", "memory_used_mib", "MemTotal", "MemAvailable"),
            ("/proc/self/status", "rss_mib", "VmRSS", None)):
            try:
                values = {line.split(":", 1)[0]: int(line.split(":", 1)[1].split()[0])
                          for line in self.reader(path).splitlines()
                          if ":" in line and line.split(":", 1)[0] in {total_key, available_key}}
                if available_key:
                    if not 0 <= values[available_key] <= values[total_key]:
                        raise ValueError("invalid memory counters")
                    result[destination] = round((values[total_key]-values[available_key])/1024, 1)
                    result["memory_total_mib"] = round(values[total_key]/1024, 1)
                else:
                    result[destination] = round(values[total_key]/1024, 1)
            except (OSError, ValueError, KeyError, IndexError) as exc:
                result["reasons"][destination] = str(exc)[:160]
        temperatures = []
        for zone in sorted(self.thermal_root.glob("thermal_zone*"))[:16]:
            try:
                label = self.reader(str(zone / "type")).strip()
                if "cpu" not in label.lower():
                    continue
                value = float(self.reader(str(zone / "temp")))/1000
                if -10 <= value <= 125:
                    temperatures.append((value, label))
            except (OSError, ValueError):
                continue
        if temperatures:
            result["temperature_c"], result["temperature_source"] = max(temperatures)
        else:
            result["reasons"]["temperature_c"] = "no readable CPU thermal zone"
        result["source"] = "Linux /proc and CPU thermal zones; process CPU uses one-core percent"
        result["process_id"] = os.getpid()
        return result
