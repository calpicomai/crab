"""Robot node — runs on the Raspberry Pi 4B.

Owns all real-time servo/gait timing. Exposes high-level abilities over HTTP
(see server.py) that the Jetson brain calls. Nothing latency-critical leaves
this board.
"""
