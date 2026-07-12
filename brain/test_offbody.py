"""Headless tests — brain logic with no robot server, SimWorld, or LLM.

Runs pure in-process checks: costmap, world model, mock pet/agent brains,
dummy perception. Use this to develop or CI-test the "mind" without spinning
up the virtual body.

    python -m brain.test_offbody
    bash test_sim.sh --unit
"""

from __future__ import annotations

import os
import sys
import tempfile


def _ok(name: str) -> None:
    print(f"  ok  {name}")


def _fail(name: str, exc: BaseException) -> None:
    print(f"  FAIL {name}: {exc}", file=sys.stderr)


def _run(name: str, fn, failures: list[str]) -> None:
    try:
        fn()
        _ok(name)
    except Exception as exc:  # noqa: BLE001 - smoke test aggregator
        _fail(name, exc)
        failures.append(name)


def _test_costmap() -> None:
    from . import costmap

    assert costmap._self_test() == 0


def _test_worldmodel() -> None:
    from .pet import worldmodel

    assert worldmodel._self_test() == 0


def _test_mock_agent_brain() -> None:
    from .agent.llm import MockBrain

    brain = MockBrain()
    status = {"pose": "standing", "distance_cm": 100.0}
    d = brain.decide(None, "Status: open", None, status)
    assert d.tool_name in {"walk", "turn", "stand", "sit", "get_status"}
    assert d.say

    blocked = brain.decide(None, "Status: blocked", None,
                           {"pose": "standing", "distance_cm": 20.0, "reflex_stopped": True})
    assert blocked.tool_name == "turn"


def _test_mock_pet_brain() -> None:
    from .pet.brain import MockPetBrain
    from .pet.identity import PetIdentity

    with tempfile.TemporaryDirectory() as tmp:
        identity = PetIdentity(os.path.join(tmp, "id.json"), name="TestPet")
        brain = MockPetBrain()
        thought = brain.reflect(None, {"distance_cm": 120.0, "target": "cat"},
                                identity, "curious", "")
        assert thought.target == "cat"
        assert thought.mood_hint == "excited"
        evolved = brain.evolve(identity, "saw a cat twice")
        assert "TestPet" in evolved


def _test_perception_dummy() -> None:
    os.environ["PERCEPTION_SIMULATE"] = "1"
    from .test_perception import main as run_perception

    assert run_perception(["--frames", "1", "--backend", "dummy"]) == 0


def main(argv: list[str] | None = None) -> int:
    del argv  # reserved for future flags
    print("== PiCrawler off-body (in-code) tests ==")
    failures: list[str] = []
    _run("brain.costmap", _test_costmap, failures)
    _run("brain.pet.worldmodel", _test_worldmodel, failures)
    _run("MockBrain (agent)", _test_mock_agent_brain, failures)
    _run("MockPetBrain (pet)", _test_mock_pet_brain, failures)
    _run("brain.test_perception (dummy)", _test_perception_dummy, failures)
    print()
    if failures:
        print(f"Off-body tests FAILED ({len(failures)}): {', '.join(failures)}", file=sys.stderr)
        return 1
    print("Off-body tests OK — brain logic healthy without a virtual body.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
