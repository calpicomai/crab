"""Robot abilities exposed to the LLM as OpenAI-style function tools.

One schema per high-level ability + a dispatch table onto RobotClient. Movement
goes through the reflex-protected client (walk carries a min_clearance_cm), so the
Pi's fast reflex still guards every move even if the model decides badly. The
diagnostic ``test_leg`` is intentionally not exposed — it's a maintenance op, not
an autonomous behavior.
"""

from __future__ import annotations

from shared import CommandResponse

from ..client import RobotClient
from . import config

# OpenAI function-calling tool schemas. Kept minimal and high-level.
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "walk",
            "description": "Walk forward a few gait cycles. The robot's reflex stops the "
            "stride automatically if something gets too close.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "Number of forward gait cycles (1-5).",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "turn",
            "description": "Turn in place by a signed angle. Negative = left, positive = right.",
            "parameters": {
                "type": "object",
                "properties": {
                    "degrees": {
                        "type": "number",
                        "minimum": -180,
                        "maximum": 180,
                        "description": "Turn angle; negative left, positive right.",
                    }
                },
                "required": ["degrees"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stand",
            "description": "Rise to the neutral standing pose (e.g. to look around).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sit",
            "description": "Lower to the resting/sitting pose.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "Read current pose and forward clearance without moving.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# Names the model is allowed to call.
TOOL_NAMES: set[str] = {t["function"]["name"] for t in TOOL_SCHEMAS}


def dispatch(name: str, args: dict, client: RobotClient) -> CommandResponse:
    """Execute a tool call on the robot and return its CommandResponse.

    Unknown tools / bad args fall back to a safe no-op status read rather than
    raising, so a shaky small-model tool call can't crash the loop.
    """
    args = args or {}
    if name == "walk":
        steps = int(args.get("steps", 1) or 1)
        steps = max(1, min(5, steps))
        return client.walk(steps, min_clearance_cm=config.AGENT_REFLEX_CM)
    if name == "turn":
        return client.turn(float(args.get("degrees", 0.0) or 0.0))
    if name == "stand":
        return client.stand()
    if name == "sit":
        return client.sit()
    # get_status and any unknown/no-op name: just read status.
    return client.get_status()
