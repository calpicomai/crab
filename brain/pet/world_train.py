"""Training sessions for the semantic world model (laptop → deploy to robot).

The robot **already captures images**: the Pi streams MJPEG + single JPEG frames
(``/camera/stream``, ``/camera/frame``). During a real run, log experience with
``--log session.jsonl``. On your laptop, consolidate images + notes + logs into
``world.db`` with a local LLM, then copy the DB to the Jetson.

Quick start::

    # 1. Collect from a live robot (same LAN):
    python -m brain.pet.world_train capture --robot http://picrawler.local:8000 \\
        --session kitchen --count 5 --note "living room view"

    # 2. Or add laptop photos + text:
    python -m brain.pet.world_train add --session kitchen --image cat.jpg \\
        --note "Our cat Mittens — always chase gently"
    python -m brain.pet.world_train add --session kitchen --text "Avoid the vacuum"

    # 3. Import a pet run JSONL:
    python -m brain.pet.world_train import-jsonl --session kitchen run.jsonl

    # 4. LLM consolidate (Ollama on laptop):
    python -m brain.pet.world_train consolidate --session kitchen

    # 5. Deploy to Jetson:
    python -m brain.pet.world_train deploy --host jetson.local

Runtime on the robot uses the enriched ``world.db`` only — no LLM per tick.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

from . import config as pet_config
from .worldmodel import WorldModel


def _session_name(args) -> str:
    s = getattr(args, "session", None) or os.environ.get("WORLD_TRAIN_SESSION", "default")
    return s.strip() or "default"


def cmd_status(wm: WorldModel, _args) -> int:
    print(f"DB: {pet_config.PET_WORLD_DB}")
    print(f"  objects={wm.object_count()} places={wm.place_count()} "
          f"concepts={wm.concept_count()} prefs={len(wm.list_preferences())}")
    pending = wm.pending_training()
    print(f"  training queue: {len(pending)} pending")
    if wm.list_concepts():
        print("\nConcepts:")
        for c in wm.list_concepts()[:12]:
            kw = ", ".join(c["keywords"][:4])
            print(f"  • {c['canonical']} ({c['drive']}) — {kw}")
    print(f"\n{wm.summary()}")
    sem = wm.semantic_summary(4)
    if sem:
        print(sem)
    return 0


def cmd_add(wm: WorldModel, args) -> int:
    session = _session_name(args)
    if args.text:
        wm.queue_training(session, "text", args.text, args.note or "")
        print(f"Queued text lesson (session={session})")
    if args.image:
        path = str(Path(args.image).resolve())
        wm.queue_training(session, "image", path, args.note or "")
        print(f"Queued image {path}")
    if not args.text and not args.image:
        print("Provide --text and/or --image", file=sys.stderr)
        return 1
    return 0


def cmd_import_jsonl(wm: WorldModel, args) -> int:
    session = _session_name(args)
    path = Path(args.jsonl)
    if not path.is_file():
        print(f"Not found: {path}", file=sys.stderr)
        return 1
    n = wm.queue_log_file(str(path), session)
    print(f"Queued {n} JSONL records from {path}")
    return 0


def cmd_queue_log(wm: WorldModel, args) -> int:
    session = _session_name(args)
    path = Path(args.jsonl)
    if not path.is_file():
        print(f"Not found: {path}", file=sys.stderr)
        return 1
    n = wm.queue_log_file(str(path), session)
    print(f"Queued {n} lines (session={session}) — run: consolidate --session {session}")
    return 0


def cmd_import_dir(wm: WorldModel, args) -> int:
    session = _session_name(args)
    root = Path(args.directory)
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    n = 0
    for p in sorted(root.iterdir()):
        if p.suffix.lower() in exts:
            wm.queue_training(session, "image", str(p.resolve()), args.note or p.stem)
            n += 1
    print(f"Queued {n} images from {root}")
    return 0


def cmd_capture(wm: WorldModel, args) -> int:
    session = _session_name(args)
    base = args.robot.rstrip("/")
    out_dir = Path(args.out or os.path.join(pet_config.PET_HOME, "train_frames", session))
    out_dir.mkdir(parents=True, exist_ok=True)
    count = max(1, int(args.count))
    saved = 0
    with httpx.Client(base_url=base, timeout=10.0) as client:
        health = client.get("/health").json()
        print(f"Robot {base} simulate={health.get('simulate')}")
        for i in range(count):
            r = client.get("/camera/frame")
            if r.status_code != 200:
                print(f"Frame {i} failed: HTTP {r.status_code}", file=sys.stderr)
                continue
            ts = int(time.time() * 1000)
            path = out_dir / f"frame_{ts}_{i:03d}.jpg"
            path.write_bytes(r.content)
            wm.queue_training(session, "image", str(path.resolve()), args.note or f"live frame {i}")
            saved += 1
            if i + 1 < count:
                time.sleep(max(0.0, float(args.interval)))
    print(f"Captured {saved} frames → {out_dir} (queued for session={session})")
    return 0 if saved else 1


def cmd_consolidate(wm: WorldModel, args) -> int:
    session = args.session if args.session else None
    n = wm.consolidate_training(session, simulate=args.simulate)
    print(f"Consolidated {n} training item(s) → {wm.concept_count()} concept(s)")
    if wm.list_concepts():
        print(wm.semantic_summary(8))
    return 0


def cmd_deploy(_wm: WorldModel, args) -> int:
    db = Path(args.db or pet_config.PET_WORLD_DB).resolve()
    if not db.is_file():
        print(f"World DB not found: {db}", file=sys.stderr)
        return 1
    host = args.host or os.environ.get("ROBOT_BRAIN_HOST", "jetson.local")
    user = args.user or os.environ.get("DEPLOY_USER", os.environ.get("USER", "pi"))
    remote_dir = args.remote_dir or "~/.picrawler_pet"
    remote = f"{user}@{host}:{remote_dir}/world.db"
    print("Deploy the trained world model to the Jetson brain:\n")
    print(f"  scp {db} {remote}")
    print(f"\nThen on the Jetson:")
    print(f"  export PET_WORLD_DB={remote_dir}/world.db")
    print(f"  bash brain/run.sh pet")
    if args.run:
        import subprocess
        rc = subprocess.call(["scp", str(db), remote])
        if rc == 0:
            print("Copy complete.")
        return rc
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the pet world model on a laptop, deploy to Jetson.")
    parser.add_argument("--db", default=None, help=f"World DB (default {pet_config.PET_WORLD_DB})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show world.db stats")

    p_add = sub.add_parser("add", help="Queue a text or image lesson")
    p_add.add_argument("--session", default="default")
    p_add.add_argument("--text", default=None)
    p_add.add_argument("--image", default=None)
    p_add.add_argument("--note", default="")

    p_j = sub.add_parser("import-jsonl", help="Queue lines from a pet/wander --log file")
    p_j.add_argument("jsonl")
    p_j.add_argument("--session", default="default")
    p_j.add_argument("--note", default="")

    p_q = sub.add_parser("queue-log", help="Alias: queue a pet --log JSONL for consolidate")
    p_q.add_argument("jsonl")
    p_q.add_argument("--session", default="default")

    p_d = sub.add_parser("import-dir", help="Queue all images in a folder")
    p_d.add_argument("directory")
    p_d.add_argument("--session", default="default")
    p_d.add_argument("--note", default="")

    p_c = sub.add_parser("capture", help="Pull JPEG frames from the robot camera")
    p_c.add_argument("--robot", default=None, help="Robot base URL (default brain/config BASE_URL)")
    p_c.add_argument("--session", default="default")
    p_c.add_argument("--count", type=int, default=5)
    p_c.add_argument("--interval", type=float, default=1.0)
    p_c.add_argument("--note", default="")
    p_c.add_argument("--out", default=None, help="Save frames under this dir")

    p_k = sub.add_parser("consolidate", help="Run laptop LLM on queued items")
    p_k.add_argument("--session", default=None, help="Session name (default: all pending)")
    p_k.add_argument("--simulate", action="store_true", help="No LLM — canned extraction")

    p_dep = sub.add_parser("deploy", help="Print/run scp to copy world.db to Jetson")
    p_dep.add_argument("--host", default=None)
    p_dep.add_argument("--user", default=None)
    p_dep.add_argument("--remote-dir", default=None)
    p_dep.add_argument("--db", default=None)
    p_dep.add_argument("--run", action="store_true", help="Execute scp (else print instructions)")

    args = parser.parse_args(argv)
    db = args.db or pet_config.PET_WORLD_DB
    wm = WorldModel(db)
    try:
        handlers = {
            "status": cmd_status,
            "add": cmd_add,
            "import-jsonl": cmd_import_jsonl,
            "queue-log": cmd_queue_log,
            "import-dir": cmd_import_dir,
            "capture": cmd_capture,
            "consolidate": cmd_consolidate,
            "deploy": cmd_deploy,
        }
        if args.cmd == "capture" and not args.robot:
            from .. import config as brain_config
            args.robot = brain_config.BASE_URL
        return handlers[args.cmd](wm, args)
    finally:
        wm.close()


if __name__ == "__main__":
    raise SystemExit(main())
