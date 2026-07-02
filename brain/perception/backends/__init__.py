"""Detector backends behind a common interface.

Each backend lazily imports its heavy libraries inside load(), so importing this
package is cheap and safe on any machine. The PerceptionEngine picks backends by
name from this registry.
"""

from .base import DetectorBackend
from .dummy import DummyBackend

# name -> class. yolo/nanoowl are imported lazily via build() to avoid importing
# their (heavy, JetPack-specific) module trees unless actually requested.
def build(name: str) -> DetectorBackend:
    """Instantiate a backend by name. Raises KeyError for unknown names."""
    name = name.strip().lower()
    if name == "dummy":
        return DummyBackend()
    if name == "yolo":
        from .yolo import YoloBackend

        return YoloBackend()
    if name == "nanoowl":
        from .nanoowl import NanoOwlBackend

        return NanoOwlBackend()
    if name == "simblob":
        from .simblob import SimBlobBackend

        return SimBlobBackend()
    raise KeyError(name)


AVAILABLE = ("yolo", "nanoowl", "dummy", "simblob")

__all__ = ["DetectorBackend", "DummyBackend", "build", "AVAILABLE"]
