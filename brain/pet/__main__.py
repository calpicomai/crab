"""Entry point so `python -m brain.pet` runs the pet loop."""

from .loop import main

if __name__ == "__main__":
    raise SystemExit(main())
