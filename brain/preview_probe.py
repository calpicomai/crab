"""Read-only timing of an existing preview; never starts capture or saves images."""
from __future__ import annotations

import argparse
import http.client
import json
import statistics
import time
from urllib.parse import urlsplit

from shared.local import require_local_url

LIMIT = 4 * 1024 * 1024


def probe(base_url, samples=15, interval=.1):
    parsed = urlsplit(require_local_url(base_url))
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("base URL must have no path, query or fragment")
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=2)
    observations = []
    try:
        for _ in range(samples):
            start = time.monotonic()
            connection.request("GET", "/camera/frame", headers={"Cache-Control": "no-store"})
            response = connection.getresponse()
            headers_at = time.monotonic()
            if response.status != 200:
                raise RuntimeError(f"camera HTTP {response.status}; no redirect or retry")
            payload = response.read(LIMIT + 1)
            complete = time.monotonic()
            if not payload or len(payload) > LIMIT:
                raise ValueError("preview exceeds 4 MiB bound or is empty")
            observations.append({"headers_ms": (headers_at-start)*1000,
                                 "body_ms": (complete-headers_at)*1000,
                                 "total_ms": (complete-start)*1000, "bytes": len(payload),
                                 "sequence": response.getheader("X-Crab-Sequence"),
                                 "encoded_at_pi": response.getheader("X-Crab-Captured-At"),
                                 "encoded_age_ms_at_server": response.getheader("X-Crab-Frame-Age-Ms")})
            time.sleep(max(0, interval-(complete-start)))
    finally:
        connection.close()
    return {"scope": "Existing JPEG endpoint; headers include server wait and connection setup. "
                     "Body excludes JPEG decode; encoded timestamp excludes exposure and encoding. "
                     "First request includes connection setup. No browser or physical acceptance claim.",
            "samples": observations,
            "median": {key: round(statistics.median(row[key] for row in observations), 2)
                       for key in ("headers_ms", "body_ms", "total_ms", "bytes")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--samples", type=int, default=15, choices=range(2, 51), metavar="2..50")
    args = parser.parse_args()
    print(json.dumps(probe(args.base_url, args.samples), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
