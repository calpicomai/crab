"""Inspect, export, or explicitly reset local companion data without hardware.

Examples:
  python -m brain.pet.memory_cli inspect
  python -m brain.pet.memory_cli export /tmp/crab-memories.json
  python -m brain.pet.memory_cli reset --confirm

Reset keeps identity by default. --include-identity also resets name/temperament;
originals are backed up first. Stop the companion before reset or migration.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import time

from . import config
from .identity import PetIdentity
from .memory import MemoryStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-db", default=config.PET_MEMORY_DB)
    parser.add_argument("--identity-file", default=config.PET_IDENTITY_FILE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect")
    export = sub.add_parser("export")
    export.add_argument("path")
    reset = sub.add_parser("reset")
    reset.add_argument("--confirm", action="store_true", required=True)
    reset.add_argument("--include-identity", action="store_true")
    args = parser.parse_args(argv)
    memory = MemoryStore(args.memory_db)
    try:
        # Reading/exporting memory does not create or modify an identity.
        identity = None
        if Path(args.identity_file).exists():
            identity = json.loads(Path(args.identity_file).read_text())
        data = {"identity": identity, "memory": memory.export()}
        if args.command == "inspect":
            data["memory"]["count"] = memory.count()
            data["memory"]["episodes"] = memory.recent(20)
            print(json.dumps(data, indent=2, ensure_ascii=False))
        elif args.command == "export":
            target = Path(args.path)
            if target.resolve() in {Path(args.memory_db).resolve(), Path(args.identity_file).resolve()}:
                parser.error("export path must differ from active memory and identity")
            # Avoid accidental replacement of an earlier personal-data export.
            with target.open("x") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            print(str(target))
        else:
            backups = {"memory": memory.reset()}
            if args.include_identity and identity is not None:
                backup = f"{args.identity_file}.pre-reset-{time.time_ns()}.bak"
                shutil.copy2(args.identity_file, backup)
                Path(args.identity_file).unlink()
                PetIdentity(args.identity_file)
                backups["identity"] = backup
            print(json.dumps({"reset": True, "backups": backups}))
        return 0
    finally:
        memory.close()


if __name__ == "__main__":
    raise SystemExit(main())
