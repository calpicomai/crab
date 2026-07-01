"""Perception smoke test — run on the Jetson (mirrors brain/test_movement.py).

Builds the in-process PerceptionEngine, runs a few detections, prints each
snapshot, and writes annotated JPEGs to config.OUTPUT_DIR (headless-friendly).

    python -m brain.test_perception                          # default backends
    python -m brain.test_perception --frames 5
    python -m brain.test_perception --backend nanoowl --prompts "a person,a ball"
    PERCEPTION_SIMULATE=1 python -m brain.test_perception    # off-hardware (dummy)

Annotation uses Pillow (not OpenCV) so it works even without cv2.
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

from .perception import PerceptionSnapshot
from .perception import config as pconfig
from .perception.engine import PerceptionEngine


def _annotate(frame_bgr, snapshot: PerceptionSnapshot) -> Image.Image:
    # frame is BGR (H,W,3) numpy; convert to RGB for PIL.
    image = Image.fromarray(frame_bgr[:, :, ::-1].copy())
    draw = ImageDraw.Draw(image)
    for det in snapshot.detections:
        x1, y1, x2, y2 = det.box
        draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=3)
        draw.text((x1 + 2, max(0, y1 - 12)), f"{det.label} {det.score:.2f} [{det.source}]", fill=(255, 0, 0))
    return image


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PiCrawler perception smoke test.")
    parser.add_argument("--frames", type=int, default=3, help="Number of frames to capture (default 3).")
    parser.add_argument("--backend", action="append", help="Backend(s) to load (repeatable). Default: config.")
    parser.add_argument("--prompts", default=None, help="Comma-separated NanoOWL prompts.")
    parser.add_argument("--out", default=pconfig.OUTPUT_DIR, help="Output dir for annotated JPEGs.")
    args = parser.parse_args(argv)

    engine = PerceptionEngine(backend_names=args.backend)
    if args.prompts:
        engine.set_prompts([p.strip() for p in args.prompts.split(",") if p.strip()])

    os.makedirs(args.out, exist_ok=True)
    print(f"Backends: {engine.loaded_backends()}  simulate={engine.simulate}")

    try:
        for i in range(max(1, args.frames)):
            snap = engine.detect()
            labels = ", ".join(f"{d.label}:{d.score:.2f}" for d in snap.detections) or "(none)"
            print(f"  frame {snap.frame_id}: {len(snap.detections)} det [{labels}] {snap.latency_ms}ms")
            path = os.path.join(args.out, f"frame_{i:03d}.jpg")
            _annotate(engine.last_frame, snap).save(path, "JPEG")
            print(f"    wrote {path}")
    finally:
        engine.close()

    print("Perception OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
