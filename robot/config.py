"""Runtime configuration for the Pi server.

Values are read from environment variables so the systemd unit (and the Jetson
operator during bring-up) can override them without editing code.
"""

from __future__ import annotations

import os

# Bind address for the FastAPI server. 0.0.0.0 so the Jetson on the LAN can reach it.
HOST: str = os.environ.get("PICRAWLER_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PICRAWLER_PORT", "8000"))

# Simulation is explicit: a missing library on a real deployment is a failure.
# Accepts 1/true/yes/on (case-insensitive).
SIMULATE: bool = os.environ.get("PICRAWLER_SIMULATE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Enabling this is a deliberate hardware acceptance step. Constructing Picrawler
# itself resets the MCU, so an unarmed server does not even construct the driver.
ACTUATORS_ENABLED: bool = os.environ.get("PICRAWLER_ACTUATORS_ENABLED", "").strip().lower() in {
    "1", "true", "yes", "on",
}
MAX_MOTION_CYCLES: int = max(1, min(2, int(os.environ.get("PICRAWLER_MAX_MOTION_CYCLES", "1"))))
MAX_MOTION_SPEED: int = max(1, min(50, int(os.environ.get("PICRAWLER_MAX_MOTION_SPEED", "40"))))
BATTERY_LOW_V: float = max(6.6, float(os.environ.get("PICRAWLER_BATTERY_LOW_V", "6.8")))
BATTERY_CRITICAL_V: float = max(6.2, min(BATTERY_LOW_V, float(os.environ.get("PICRAWLER_BATTERY_CRITICAL_V", "6.4"))))
BATTERY_LOW_SPEED: int = max(1, min(MAX_MOTION_SPEED, int(os.environ.get("PICRAWLER_BATTERY_LOW_SPEED", "20"))))

# Movement-safety tunables for the staged stand/sit (see robot/gait.py and the
# "Movement safety / brownout" section in the README). stand/sit move one leg at
# a time at STAND_SPEED with LEG_SETTLE_S between legs, so only ~3 servos draw
# current at once. Lower STAND_SPEED / raise LEG_SETTLE_S to be gentler on a
# marginal battery.
STAND_SPEED: int = max(1, min(MAX_MOTION_SPEED, int(os.environ.get("PICRAWLER_STAND_SPEED", "30"))))
LEG_SETTLE_S: float = max(0.1, float(os.environ.get("PICRAWLER_LEG_SETTLE_S", "0.2")))

# Ultrasonic distance sensor (robot_hat), used by the brain's wander/avoid loop.
# Default pins are the SunFounder PiCrawler wiring (trig=D2, echo=D3). Disable
# with PICRAWLER_ULTRASONIC_ENABLED=0. Missing support is unavailable, never clear.
ULTRASONIC_ENABLED: bool = os.environ.get("PICRAWLER_ULTRASONIC_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
ULTRASONIC_TRIG: str = os.environ.get("PICRAWLER_ULTRASONIC_TRIG", "D2")
ULTRASONIC_ECHO: str = os.environ.get("PICRAWLER_ULTRASONIC_ECHO", "D3")
# Ping attempts per read. robot_hat returns on the FIRST valid echo, so a healthy
# sensor still answers immediately — more attempts only cost time in genuinely open
# space (no echo). 2 was too few: a slightly flaky sensor missed often and read as
# "no distance". 5 is reliable while still reacting quickly. Diagnose a silent
# sensor with `python -m robot.diagnose --sonar` (Pi-local, no network).
ULTRASONIC_PINGS: int = int(os.environ.get("PICRAWLER_ULTRASONIC_PINGS", "5"))

# Battery voltage monitor (robot_hat), reported on RobotStatus so the brain can
# slow/rest the pet when it runs low. Best-effort + guarded: prefers robot_hat's own
# battery reading; missing robot_hat/getter reports unavailable. Disable with
# PICRAWLER_BATTERY_ENABLED=0. Legacy ADC fields remain readable for configuration
# recovery but do not enable an unverified ADC mapping on a real board.
BATTERY_ENABLED: bool = os.environ.get("PICRAWLER_BATTERY_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
BATTERY_ADC_CHANNEL: str = os.environ.get("PICRAWLER_BATTERY_ADC", "A4")
BATTERY_SCALE: float = float(os.environ.get("PICRAWLER_BATTERY_SCALE", "3.0"))

# Only "canned" is an accepted primitive in this milestone. A saved "custom"
# selection is preserved but rejected with an explicit result, not substituted.
GAIT_MODE: str = os.environ.get("PICRAWLER_GAIT_MODE", "canned").strip().lower()
# Legacy offline stride parameter; never amplify beyond the saved coordinates.
GAIT_STRIDE_SCALE: float = max(0.5, min(1.0, float(os.environ.get("PICRAWLER_GAIT_STRIDE_SCALE", "1.0"))))
# Custom coordinates remain available for offline work, but are not an accepted
# hardware primitive in this milestone.

# --- Movement reflex (fast on-robot obstacle stop) ----------------------------
# The walk gait is blocking, so without this the robot is blind for the whole
# stride and can nose into an obstacle it would otherwise sense. The reflex reads
# the forward ultrasonic BETWEEN gait cycles and aborts the walk the moment
# clearance drops below REFLEX_STOP_CM — a real-time safety layer that lives on
# the Pi (where the gait timing already lives). It only stops; it does NOT back up
# (reversing blindly could hit something behind). The brain then turns away.
# REFLEX_STOP_CM is an *emergency* distance — deliberately closer than the brain's
# steer-away distance, so it's a last resort, not the primary avoidance. Needs the
# ultrasonic sensor; with it unavailable forward motion is inhibited. A per-walk
# override may increase but never reduce the Pi margin.
# REFLEX_ENABLED remains readable for saved configuration compatibility; a false
# value no longer bypasses the mandatory forward-clearance check.
REFLEX_ENABLED: bool = os.environ.get("PICRAWLER_REFLEX_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
REFLEX_STOP_CM: float = max(15.0, float(os.environ.get("PICRAWLER_REFLEX_STOP_CM", "20")))

# --- Audio (mic + speaker on the Pi; STT/TTS compute on the Jetson) -----------
# The Pi captures mic PCM (streamed to the brain for Whisper) and plays back WAVs
# the brain sends (Piper TTS). Uses ALSA arecord/aplay; missing tools/device report
# unavailable. Disable with PICRAWLER_AUDIO_ENABLED=0. Set the ALSA device names
# (from `arecord -l` / `aplay -l`, e.g. "plughw:1,0") if the defaults are wrong.
AUDIO_ENABLED: bool = os.environ.get("PICRAWLER_AUDIO_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
MIC_DEVICE: str = os.environ.get("PICRAWLER_MIC_DEVICE", "")
SPEAKER_DEVICE: str = os.environ.get("PICRAWLER_SPEAKER_DEVICE", "")
SPEAKER_BACKEND: str = os.environ.get("PICRAWLER_SPEAKER_BACKEND", "alsa")
SPEAKER_VERIFIED_BOARD: str = os.environ.get("PICRAWLER_SPEAKER_VERIFIED_BOARD", "")
SPEAKER_EXPECTED_PIN: str = os.environ.get("PICRAWLER_SPEAKER_EXPECTED_PIN", "")
SPEAKER_GAIN: str | None = os.environ.get("PICRAWLER_SPEAKER_GAIN")
MIC_RATE: int = int(os.environ.get("PICRAWLER_MIC_RATE", "16000"))

# --- Whole-robot simulator (robot/simworld.py) --------------------------------
# With explicit PICRAWLER_SIMULATE=1, optionally back the
# gait/sonar/camera with a 2D world so the robot actually moves in a space and
# the ultrasonic sees real obstacles — off by default so plain simulate behavior
# is unchanged. Enable with PICRAWLER_SIM_WORLD=1. A live dashboard is served at
# /sim. See the README "Simulator" section.
SIM_WORLD: bool = os.environ.get("PICRAWLER_SIM_WORLD", "").strip().lower() in {"1", "true", "yes", "on"}
# Scenario preset (empty/room/poles/corridor/slalom) or a JSON obstacle file path.
SIM_SCENARIO: str = os.environ.get("PICRAWLER_SIM_SCENARIO", "poles")
# Distance (cm) the virtual robot advances per walk gait cycle.
SIM_STRIDE_CM: float = float(os.environ.get("PICRAWLER_SIM_STRIDE_CM", "6"))
# A labeled simulator assumption, NOT a measured angle on the physical robot.
SIM_TURN_CYCLE_DEG: float = max(1.0, min(45.0, float(os.environ.get("PICRAWLER_SIM_TURN_CYCLE_DEG", "15"))))

# Pose to gently home into when the server starts, instead of leaving the legs
# in picrawler's splayed power-on pose. One of "stand", "sit", or "none". Uses
# the same staged, low-speed motion as the stand/sit commands. "stand" only
# makes sense once every leg is calibrated (an uncalibrated leg could stall).
HOME_ON_START: str = os.environ.get("PICRAWLER_HOME_ON_START", "none").strip().lower()

# Camera (on the Pi) served as MJPEG to the Jetson. Disable with CAMERA_ENABLED=0.
# picamera2 (system package, visible to the --system-site-packages venv) captures;
# if it is missing the real camera reports unavailable. Synthetic frames require
# explicit PICRAWLER_SIMULATE=1.
CAMERA_ENABLED: bool = os.environ.get("PICRAWLER_CAMERA_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
CAMERA_WIDTH: int = int(os.environ.get("PICRAWLER_CAMERA_WIDTH", "640"))
CAMERA_HEIGHT: int = int(os.environ.get("PICRAWLER_CAMERA_HEIGHT", "480"))
CAMERA_FPS: int = int(os.environ.get("PICRAWLER_CAMERA_FPS", "15"))
CAMERA_QUALITY: int = int(os.environ.get("PICRAWLER_CAMERA_QUALITY", "80"))
