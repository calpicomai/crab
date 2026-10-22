# Hardware evidence and limits

Evidence collected on 2026-10-22 by read-only inspection. This workspace is
x86_64 Linux, not the Pi or Jetson. `picrawler`, `robot_hat`, and `picamera2` are
not installed in the robot virtual environment. No physical board revision,
calibration, speaker output, current draw, gait displacement, stop latency, or
Jetson inference performance has been verified here. Passing mocks and the
kinematic simulator do not establish those facts.

## Official sources inspected

| Source | What it establishes |
|---|---|
| [SunFounder PiCrawler README](https://github.com/sunfounder/picrawler/blob/master/README.md) | Links the [official PiCrawler manual](https://docs.sunfounder.com/projects/pi-crawler/en/latest/) and currently recommends `robot-hat` branch `2.5.x`. Branch names do not identify the installed version. |
| [PiCrawler implementation](https://github.com/sunfounder/picrawler/blob/master/picrawler/picrawler.py) | Twelve servo pins; `do_action(name, step, speed)` repeats vendor coordinate sequences; `do_single_leg` changes one leg's target through `do_step`; default calibration path `~/.config/.picrawler.config`. |
| [Robot HAT 2.5.x Robot class](https://github.com/sunfounder/robot-hat/blob/2.5.x/robot_hat/robot.py) | Initialization loads saved offsets and writes all twelve initial servo positions sequentially, with 0.15 s spacing. `servo_move` interpolates targets internally, not through a feedback sensor. |
| [Robot HAT v2.0 battery implementation](https://github.com/sunfounder/robot-hat/blob/v2.0/robot_hat/utils.py) | The vendor battery getter scales `ADC("A4").read_voltage()` by 3 for that implementation. This is not evidence that an arbitrary installed board supports a guessed ADC mapping. |
| [Current Robot HAT device mapping](https://github.com/sunfounder/robot-hat/blob/master/robot_hat/device.py) | Vendor board selection reads device-tree identity; speaker-enable mappings differ (v4x: 20, v5x: 12). Confirm the installed library and board before acting. |
| [Current Robot HAT modules](https://github.com/sunfounder/robot-hat/blob/master/robot_hat/modules.py) | Ultrasonic timeout/invalid timing uses negative sentinel values. Optional library classes such as accelerometers do not mean those sensors are fitted to this robot. |
| [Official speaker setup/troubleshooting](https://github.com/sunfounder/robot-hat/blob/2.5.x/docs/source/install_i2s_for_speaker.rst) | I2S overlay, ALSA card/routing and clock state are separate failure candidates; a missing driver cannot be assumed. See [audio acceptance](audio-acceptance.md) for source versions and board differences. |

The linked manual pages returned HTTP 403 in this environment; the official
GitHub source files were retrieved successfully. No driver or OS installation
was attempted. Source snapshots inspected for motion had SHA-256:

```
picrawler/picrawler.py (master):
41cf1ca7ec39e06ee486b1fa65af585d5cbf55d815501d7ca262ecb83082e7cf
robot_hat/robot.py (2.5.x):
2b948ac0f3e26a4338befee97273c37c8a68d39509cd6c853ab593df40fb2629
```

## Motion interpretation

The old real `turn(degrees)` executed exactly one vendor cycle for any nonzero
magnitude; the simulator rotated the requested angle. The new precise-angle
endpoint rejects nonzero angles. `turn_steps(left|right, cycles)` executes a
bounded vendor cycle and reports completed cycles. The simulator assumes 15°
per cycle by default and labels this `idealized_simulation`; real results have
no angle estimate. Neither version supplies heading feedback. Forward travel
is likewise unmeasured; simulated 6 cm strides are assumptions.

`stop` latches immediately in software but an active vendor call finishes
before the next interruption boundary. Posture changes check between legs.
Neither vendor calls nor their successful return verify actual joint motion,
ground contact, a stall, or body displacement. Vendor failures mark motion
uncertain and latch stop. A completed stage advances the motion sequence so a
consumer can invalidate stale estimates. Pose names are commanded poses.

**Hardware enablement is an actuation gate.** Upstream `Picrawler()` resets the
MCU and its base constructor writes servo positions. `HOME_ON_START=none`
alone does not make that constructor passive. This milestone defaults to
`PICRAWLER_ACTUATORS_ENABLED=0` and delays driver construction even when enabled:
the first motion must pass fresh battery and forward-clearance checks before
initialization begins. Initialization is a separately counted motion boundary
with `initialization_executed=true`, not a measured pose. After construction the
engine checks stop and sensor health again before running the requested motion.
An initialization failure marks motion uncertain, latches stop and disables
further actuation until recovery; resume alone cannot repeat it.

Enabling actuators still requires explicit approval, supported-body setup,
accessible physical power cutoff, verified calibration/config backup and battery
capability, and a review of the installed vendor initialization behavior. The
vendor controls initialization timing; software cannot preempt it or apply a
custom servo trajectory. Do not enable it simply to inspect readiness or collect
library versions.

Staged targets reduce simultaneous target changes; they do not guarantee a
three-servo-only current load. All supporting servos can hold load, and vendor
`do_single_leg` calls a whole target frame internally. Speed caps are conservative
software limits, not measured electrical safety guarantees. Custom amplified
gaits remain unaccepted for live motion in this milestone.

## Sensor and audio interpretation

The stock capability set used here is the camera, forward ultrasonic, microphone,
speaker, and twelve commanded servo channels. Battery telemetry is available
only if the installed vendor getter works. The current implementation has no
IMU, depth, foot-contact, joint-feedback, or sound-direction input. A microphone
transcript cannot establish a person's bearing; a detected label cannot establish
personal identity or familiarity.

Ultrasonic samples cover a forward cone. Camera detections are interpreted
observations with limited geometry, not depth measurements or proof of a clear
route. Neither sensor guarantees stair/cliff detection or collision avoidance.
Unknown readings remain unknown; real failures cannot produce synthetic healthy
values. Simulation requires explicit opt-in and publishes its provenance.

Speaker enablement, I2S overlay, selected ALSA output, service-account access,
direct playback, robot endpoint playback, LAN delivery, and local TTS are
independent checks. An ALSA process exit code proves only the software playback
outcome, not audible sound. Physical audibility still requires a human acceptance
check. See [audio acceptance](audio-acceptance.md) for the read-only diagnostic steps
and the separate explicitly requested playback test.

## Before hardware acceptance

Record the Pi board/HAT marking, OS/kernel, installed `robot_hat` and `picrawler`
versions/commit, service account, vendor calibration path and backup, and actual
battery getter support without instantiating the crawler. Compare those installed
sources with the upstream behavior above. Perform audio validation separately
from actuation. Only after explicit authorization should supported-body actuator
initialization, bounded posture/turn cycles, interruption latency, voltage sag,
and controlled inanimate-obstacle trials be performed. No autonomous animal
chasing or unsupervised exploration is part of acceptance.
