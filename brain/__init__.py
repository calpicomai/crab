"""Brain node — runs on the NVIDIA Jetson Orin Nano.

Off-board perception, speech, and the decision-making LLM. Sends high-level
commands to the robot over the network via RobotClient (client.py) and receives
status back. Later stages add perception, voice I/O, and the Ollama agent loop.
"""
