"""Perception data models — brain-internal (NOT the robot command protocol).

These live here, not in shared/protocol.py: shared/ is only the robot<->brain
command protocol (see CLAUDE.md). A PerceptionSnapshot is the "perception
snapshot" half of the future experience record (command + CommandResponse +
perception snapshot), so it is designed to be logged/consumed as-is by the
memory / mapping / learning subsystems later.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Detection(BaseModel):
    """One detected object."""

    label: str
    score: float = Field(ge=0.0, le=1.0)
    # Pixel bounding box [x1, y1, x2, y2] in the snapshot's frame.
    box: list[int] = Field(min_length=4, max_length=4)
    # Which backend produced it ("yolo", "nanoowl", "dummy").
    source: str


class PerceptionSnapshot(BaseModel):
    """A single perception result: detections from every loaded backend, fused."""

    frame_id: int
    width: int
    height: int
    detections: list[Detection] = Field(default_factory=list)
    # Backends whose output is included in this snapshot.
    backends: list[str] = Field(default_factory=list)
    # True when running on synthetic frames and/or the dummy detector.
    simulate: bool = False
    latency_ms: float = 0.0
    # Open-vocabulary prompts active for this snapshot (NanoOWL), if any.
    prompts: list[str] | None = None
