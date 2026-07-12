"""Terminal UI for teaching the pet's world model off-body (laptop).

Browse objects, places, outcomes, and taught preferences; add new knowledge without
the robot or SimWorld. Optional natural-language teaching via a local LLM.

    pip install -r brain/requirements-world.txt
    python -m brain.pet.world_tui
    python -m brain.pet.world_tui --db ~/.picrawler_pet/world.db
"""

from __future__ import annotations

import argparse
import sys

from . import config as pet_config
from .worldmodel import DRIVES, WorldModel


def _try_textual():
    try:
        import textual  # noqa: F401
        return True
    except ImportError:
        return False


def _run_cli(wm: WorldModel) -> int:
    """Fallback menu when Textual is not installed."""
    print("World model CLI (install textual for the full TUI: pip install -r brain/requirements-world.txt)")
    print(f"DB: {wm._conn.execute('PRAGMA database_list').fetchone()[2]}")  # noqa: SLF001

    def menu() -> None:
        print("\n--- World model ---")
        print("1) Summary   2) Objects   3) Preferences   4) Places   5) Outcomes")
        print("6) Teach     7) See (simulate)   8) Record outcome   9) LLM teach")
        print("c) Concepts  p) Pending queue  k) Consolidate(sim)  j) Import JSONL  q) Quit")
        return input("> ").strip().lower()

    while True:
        choice = menu()
        if choice in {"q", "quit", "exit"}:
            break
        if choice == "1":
            print(wm.summary())
            sem = wm.semantic_summary()
            if sem:
                print(sem)
        elif choice == "2":
            for o in wm.list_objects():
                print(f"  {o['label']:20} seen={o['times_seen']:3} drive={o['drive']:8} "
                      f"interest={o['interest']:.2f} valence={o.get('valence', 0):+.1f}")
        elif choice == "3":
            for p in wm.list_preferences():
                print(f"  {p['label']:16} {p['drive']:7} w={p['weight']:.2f} v={p['valence']:+.2f}  {p.get('note','')}")
        elif choice == "4":
            for p in wm.list_places():
                print(f"  #{p['id']} visits={p['visits']}  {p['labels']}")
        elif choice == "5":
            for o in wm.list_outcomes():
                print(f"  {o['context']}/{o['action']} n={o['tries']} "
                      f"reflex~{o['reflex_p']*100:.0f}% prog~{o['progress_p']*100:.0f}%")
        elif choice == "6":
            label = input("label: ").strip()
            drive = input(f"drive {DRIVES} [approach]: ").strip() or "approach"
            try:
                val = float(input("valence -1..1 [0]: ").strip() or "0")
            except ValueError:
                val = 0.0
            aliases = [a.strip() for a in input("aliases (comma-sep): ").split(",") if a.strip()]
            note = input("note: ").strip()
            wm.teach(label, drive=drive, valence=val, aliases=aliases, note=note)
            print("OK")
        elif choice == "7":
            label = input("label: ").strip()
            try:
                bearing = float(input("bearing deg [0]: ").strip() or "0")
            except ValueError:
                bearing = 0.0
            wm.simulate_see(label, bearing_deg=bearing)
            print("OK — simulated sighting recorded")
        elif choice == "8":
            ctx = input("context (e.g. tight/curious): ").strip()
            action = input("action [walk]: ").strip() or "walk"
            reflex = input("reflex stopped? [y/N]: ").strip().lower() in {"y", "yes", "1"}
            wm.record(ctx, action, reflex=reflex, progressed=not reflex)
            print("OK")
        elif choice == "9":
            text = input("Tell the pet (natural language): ").strip()
            if not text:
                continue
            try:
                from .world_llm import apply_teaching
                spec = apply_teaching(wm, text)
                print(f"LLM taught: {spec}")
            except Exception as exc:  # noqa: BLE001
                print(f"LLM teach failed: {exc}", file=sys.stderr)
        elif choice == "c":
            for c in wm.list_concepts():
                kw = ", ".join(c.get("keywords") or [])[:60]
                print(f"  {c['canonical']:14} {c['drive']:7} {c.get('category',''):10} {kw}")
        elif choice == "p":
            for p in wm.pending_training():
                print(f"  #{p['id']} [{p['session']}] {p['kind']}: {p['payload'][:50]}")
        elif choice == "k":
            sess = input("session [all]: ").strip() or None
            n = wm.consolidate_training(sess, simulate=True)
            print(f"Consolidated {n} (simulate)")
        elif choice == "j":
            path = input("jsonl path: ").strip()
            sess = input("session [default]: ").strip() or "default"
            n = wm.queue_log_file(path, sess) if path.endswith(".jsonl") else 0
            if not n and path:
                import json as _json
                from pathlib import Path
                p = Path(path)
                if p.is_file():
                    for line in p.read_text().splitlines():
                        if line.strip():
                            wm.queue_training(sess, "jsonl", line.strip())
                            n += 1
            print(f"Queued {n} lines")
        else:
            print("Unknown option")
    return 0


def _run_textual(wm: WorldModel, db_path: str) -> int:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.widgets import DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

    class WorldTUI(App):
        TITLE = "PiCrawler World Model"
        SUB_TITLE = db_path
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
            Binding("t", "focus_teach", "Teach"),
            Binding("c", "focus_consolidate", "Consolidate"),
        ]

        def __init__(self, model: WorldModel) -> None:
            super().__init__()
            self.wm = model

        def compose(self) -> ComposeResult:
            yield Header()
            with TabbedContent():
                with TabPane("Overview", id="tab-overview"):
                    yield Static(id="overview")
                with TabPane("Objects", id="tab-objects"):
                    yield DataTable(id="objects-table")
                with TabPane("Taught", id="tab-prefs"):
                    yield DataTable(id="prefs-table")
                with TabPane("Places", id="tab-places"):
                    yield DataTable(id="places-table")
                with TabPane("Outcomes", id="tab-outcomes"):
                    yield DataTable(id="outcomes-table")
                with TabPane("Concepts", id="tab-concepts"):
                    yield DataTable(id="concepts-table")
                with TabPane("Train", id="tab-train"):
                    with Vertical():
                        yield Static("[bold]Training session[/] — queue on laptop, LLM consolidate, deploy world.db to Jetson")
                        yield Input(placeholder="session name [default]", id="in-session")
                        yield Static(id="train-status")
                        yield Input(placeholder="import jsonl path → Enter", id="in-jsonl")
                        yield Input(placeholder="capture: http://picrawler.local:8000 5", id="in-capture")
                        yield Input(placeholder="consolidate | consolidate --simulate", id="in-consolidate")
                        yield Static("")
                        yield Static("[bold]Quick teach[/] — label drive valence aliases note")
                        yield Input(placeholder="label", id="in-label")
                        yield Input(placeholder="drive: chase|approach|avoid|neutral", id="in-drive")
                        yield Input(placeholder="valence -1..1", id="in-valence")
                        yield Input(placeholder="aliases (comma-separated)", id="in-aliases")
                        yield Input(placeholder="note", id="in-note")
                        yield Input(placeholder="Enter to teach", id="in-teach-submit")
                        yield Static("")
                        yield Static("[bold]Simulate seeing[/] — label [bearing°]")
                        yield Input(placeholder="see: cat 15", id="in-see")
                        yield Static("")
                        yield Static("[bold]LLM teach[/] (local Ollama / llama-server)")
                        yield Input(placeholder="Snakes are scary, stay away", id="in-llm")
            yield Footer()

        def on_mount(self) -> None:
            for tid, cols in (
                ("objects-table", ("label", "seen", "drive", "interest", "valence", "taught")),
                ("prefs-table", ("label", "drive", "weight", "valence", "aliases", "note")),
                ("places-table", ("id", "visits", "labels")),
                ("outcomes-table", ("context", "action", "n", "reflex%", "prog%")),
                ("concepts-table", ("canonical", "drive", "category", "keywords", "session")),
            ):
                t = self.query_one(f"#{tid}", DataTable)
                t.add_columns(*cols)
            self.refresh_all()

        def refresh_all(self) -> None:
            sem = self.wm.semantic_summary(3)
            body = (
                f"[bold]Summary[/]\n{self.wm.summary()}\n\n"
                f"Objects: {self.wm.object_count()}  Places: {self.wm.place_count()}  "
                f"Concepts: {self.wm.concept_count()}  Taught: {len(self.wm.list_preferences())}  "
                f"Pending: {len(self.wm.pending_training())}"
            )
            if sem:
                body += f"\n\n{sem}"
            self.query_one("#overview", Static).update(body)
            try:
                pending = len(self.wm.pending_training())
                self.query_one("#train-status", Static).update(
                    f"Queue: {pending} pending  |  consolidate then: "
                    f"python -m brain.pet.world_train deploy --run"
                )
            except Exception:
                pass
            self._fill_table("objects-table", self.wm.list_objects(), lambda o: (
                o["label"], str(o["times_seen"]), o["drive"], f"{o['interest']:.2f}",
                f"{o.get('valence', 0):+.1f}", "yes" if o.get("taught") else "",
            ))
            self._fill_table("prefs-table", self.wm.list_preferences(), lambda p: (
                p["label"], p["drive"], f"{p['weight']:.2f}", f"{p['valence']:+.2f}",
                p.get("aliases") or "", (p.get("note") or "")[:40],
            ))
            self._fill_table("places-table", self.wm.list_places(), lambda p: (
                str(p["id"]), str(p["visits"]), p["labels"],
            ))
            self._fill_table("outcomes-table", self.wm.list_outcomes(), lambda o: (
                o["context"], o["action"], str(o["tries"]),
                f"{o['reflex_p']*100:.0f}", f"{o['progress_p']*100:.0f}",
            ))
            self._fill_table("concepts-table", self.wm.list_concepts(), lambda c: (
                c["canonical"], c["drive"], c.get("category", ""),
                ", ".join((c.get("keywords") or [])[:5]), c.get("session", ""),
            ))

        def _fill_table(self, tid: str, rows: list, fn) -> None:
            t = self.query_one(f"#{tid}", DataTable)
            t.clear()
            for row in rows:
                t.add_row(*[str(c) for c in fn(row)])

        def action_refresh(self) -> None:
            self.refresh_all()

        def action_focus_teach(self) -> None:
            self.query_one("#in-label", Input).focus()

        def action_focus_consolidate(self) -> None:
            self.query_one("#in-consolidate", Input).focus()

        def action_focus_llm(self) -> None:
            self.query_one("#in-llm", Input).focus()

        def _session(self) -> str:
            s = self.query_one("#in-session", Input).value.strip()
            return s or "default"

        def on_input_submitted(self, event: Input.Submitted) -> None:
            iid = event.input.id
            if iid == "in-teach-submit":
                self._do_teach()
            elif iid == "in-see":
                self._do_see(event.value)
            elif iid == "in-llm":
                self._do_llm(event.value)
            elif iid == "in-jsonl":
                self._do_import_jsonl(event.value)
            elif iid == "in-capture":
                self._do_capture(event.value)
            elif iid == "in-consolidate":
                self._do_consolidate(event.value)

        def _do_teach(self) -> None:
            label = self.query_one("#in-label", Input).value.strip()
            if not label:
                self.notify("Label required", severity="warning")
                return
            drive = self.query_one("#in-drive", Input).value.strip().lower() or "approach"
            try:
                valence = float(self.query_one("#in-valence", Input).value.strip() or "0")
            except ValueError:
                valence = 0.0
            aliases = [a.strip() for a in self.query_one("#in-aliases", Input).value.split(",") if a.strip()]
            note = self.query_one("#in-note", Input).value.strip()
            try:
                self.wm.teach(label, drive=drive, valence=valence, aliases=aliases, note=note)
            except ValueError as exc:
                self.notify(str(exc), severity="error")
                return
            self.notify(f"Taught {label} ({drive})")
            self.refresh_all()

        def _do_see(self, raw: str) -> None:
            parts = raw.replace("see:", "").strip().split()
            if not parts:
                return
            label = parts[0]
            bearing = float(parts[1]) if len(parts) > 1 else 0.0
            self.wm.simulate_see(label, bearing_deg=bearing)
            self.notify(f"Saw {label} @ {bearing:+.0f}°")
            self.refresh_all()

        def _do_llm(self, text: str) -> None:
            text = text.strip()
            if not text:
                return
            try:
                from .world_llm import apply_teaching
                spec = apply_teaching(self.wm, text)
                self.notify(f"LLM: {spec['label']} ({spec['drive']})")
                self.refresh_all()
            except Exception as exc:  # noqa: BLE001
                self.notify(f"LLM failed: {exc}", severity="error")

        def _do_import_jsonl(self, path: str) -> None:
            path = path.strip()
            if not path:
                return
            n = self.wm.queue_log_file(path, self._session())
            self.notify(f"Queued {n} lines from log")
            self.refresh_all()

        def _do_capture(self, raw: str) -> None:
            parts = raw.strip().split()
            if len(parts) < 1:
                return
            url = parts[0]
            count = int(parts[1]) if len(parts) > 1 else 5
            try:
                import httpx
                import time as _time
                from pathlib import Path

                sess = self._session()
                out = Path(pet_config.PET_HOME) / "train_frames" / sess
                out.mkdir(parents=True, exist_ok=True)
                saved = 0
                with httpx.Client(base_url=url.rstrip("/"), timeout=10.0) as client:
                    for i in range(count):
                        r = client.get("/camera/frame")
                        if r.status_code == 200:
                            p = out / f"frame_{int(_time.time()*1000)}_{i:03d}.jpg"
                            p.write_bytes(r.content)
                            self.wm.queue_training(sess, "image", str(p), f"capture {i}")
                            saved += 1
                        _time.sleep(0.5)
                self.notify(f"Captured {saved} frames")
                self.refresh_all()
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Capture failed: {exc}", severity="error")

        def _do_consolidate(self, raw: str) -> None:
            raw = raw.strip().lower()
            if not raw.startswith("consolidate"):
                return
            sim = "--simulate" in raw
            try:
                n = self.wm.consolidate_training(self._session(), simulate=sim)
                self.notify(f"Consolidated {n} → {self.wm.concept_count()} concepts")
                self.refresh_all()
            except Exception as exc:  # noqa: BLE001
                self.notify(f"Consolidate failed: {exc}", severity="error")

    WorldTUI(wm).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TUI for teaching the pet world model.")
    parser.add_argument("--db", default=pet_config.PET_WORLD_DB, help="SQLite world DB path.")
    parser.add_argument("--cli", action="store_true", help="Force plain CLI (no Textual).")
    args = parser.parse_args(argv)

    wm = WorldModel(args.db)
    try:
        if args.cli or not _try_textual():
            return _run_cli(wm)
        return _run_textual(wm, args.db)
    finally:
        wm.close()


if __name__ == "__main__":
    raise SystemExit(main())
