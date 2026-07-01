"""The robot pet: a little creature that roams safely, has moods, remembers, and
grows its own personality from experience. Run it with ``python -m brain.pet``.

Layers (see brain/pet/loop.py): a fast continuous reactive *body* (costmap + Pi
reflex, so it always moves and never rams anything), a slow *mind* (an
in-character LLM when available, else mood + memory + canned lines), persistent
*memory* (memory.py) and a persistent, evolving *identity* (identity.py)."""
