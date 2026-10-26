# Explicit Robot HAT v4 speaker profile

This is an opt-in application integration, not an ALSA installer or a claim of
physical acceptance. Generic `alsa` remains the default. No GPIO/vendor import
occurs at module import, speaker construction or a read-only health/self-test.
`PICRAWLER_AUDIO_ENABLED=0`, simulation and Jetson `--no-voice` never enable this
amplifier path. A real play request must pass the existing Pi audio lease/session
checks before any profile action.

## Evidence and verification boundary — 2026-10-26

The owner confirmed a **Robot HAT v4** from its label/photo and installed
**robot_hat 2.5.3**, whose selected `Devices.spk_en` was **20**. Their one-shot
test verified GPIO readback **low → high → low** and audible WAV output through
`plug:softvol`. `softvol` alone failed the channel count. The existing ALSA chain
is softvol → dmixer, stereo 44.1 kHz → speakerbonnet card 3; PulseAudio's default
instead resolves the built-in Headphones sink and does not expose this DAC.
Quiet output and a digital-peak ramp from roughly 3–25% were heard by the owner.
Those observations establish that one-shot path, not this application lifecycle.

The [vendor device source](https://github.com/sunfounder/robot-hat/blob/2.5.x/robot_hat/device.py)
selects board-specific pins, but its default pin is not proof of board identity.
Its enable helper sets the pin and invokes a 0.5-second silent fill while
discarding command failure. Crab therefore requires an explicit physical-board
attestation and expected pin, and checks the installed vendor v4 table, selected
device pin, version and contradictory EEPROM/name metadata before any GPIO write.
Unknown/wrong boards, metadata mismatch and unsupported versions fail closed.
Only 2.5.3 is covered by this profile; upgrades need another review.

No application constant guesses a pin for an unverified board. EEPROM-less v4
identity cannot be proved by software; the owner attestation is required. The
profile does not support v5 or infer a board from the vendor's pin-20 fallback.

## Lifecycle and result contract

One non-queued transaction owns the amplifier through cleanup. A thread lock and
owner-only `/tmp/crab-speaker-hat-v4-<uid>.lock` serialize instances/processes under
the same service account. Do not run independent audio processes under different
users against the same hardware. An initially high or unconfirmed GPIO is not
taken over or disabled. Close the known owner and inspect it instead.

For an accepted play request the profile:

1. Verifies metadata, the attested `plug:softvol` route and initial low GPIO output readback.
2. Sets the verified pin high and checks command success and high readback.
3. Plays 0.5 seconds of silent stereo 44.1 kHz PCM through the **same configured
   device**; errors abort the payload rather than silently use another sink.
4. Plays the bounded WAV with configurable digital attenuation.
5. Attempts low + low readback even after enable/prefill/play failure or cancel.

GPIO commands/readbacks each have a 1-second timeout, prefill 2 seconds, and
payload playback its existing duration + 3 seconds (capped at 25 seconds). The
payload remains at most 20 seconds. Cancellation terminates the owned child;
disable cleanup is separately bounded and cannot be cancelled by that flag.
Server shutdown requests stop and waits up to 5 seconds for cleanup, reporting
an error if it remains pending. SIGKILL, power failure and kernel/GPIO failure
cannot guarantee software cleanup; physical acceptance remains necessary.

Default HAT gain is **0.5**, an attenuation multiplier, with a fixed **25% PCM
peak ceiling**. Louder inputs are attenuated proportionally rather than clipped;
quiet inputs are never normalized upward. Valid gain is 0–1; zero mutes without
touching the amplifier. This is not a mixer setting or sound-pressure guarantee.
No global volume/default/routing files are changed.
This first profile covers only the owner's verified `plug:softvol` PCM; even
`plug:default` and `plug:pulse` are refused. Other routes need separate evidence
and review rather than an automatic sink fallback.

`played=true` means the payload's playback process exited zero without
cancellation; it cannot prove sound was audible. `ok=true` additionally requires
confirmed amplifier cleanup. A cleanup failure can therefore have `played=true`,
`ok=false`, `cleanup_ok=false` and HTTP 503. It inhibits later plays on that speaker
instance until operator inspection/restart. Readback failures report amplifier
state `unknown`, never `disabled`. `audible_verified` remains false in software.
Before the first explicit play, board metadata verification is pending and is
reported separately from mere configuration. Generic/simulation results and
audio stop/lease/session ordering retain their existing semantics.

## Exact opt-in configuration for the verified setup

These are owner-run examples after reviewing/updating the proposed branch. They
were **not executed on a device during development**. Keep actuators disabled and
startup homing off. Use the existing server account/venv; stop the foreground
Jetson companion and Pi server before a standalone test. Do not install drivers,
rewrite ALSA/Pulse defaults, change mixers, or run an automatic acoustic test.

From `/home/pi/crab`, read-only diagnostics (no amplifier transition or sound):

```bash
PICRAWLER_ACTUATORS_ENABLED=0 PICRAWLER_HOME_ON_START=none \
PICRAWLER_SPEAKER_BACKEND=robot_hat_v4 \
PICRAWLER_SPEAKER_VERIFIED_BOARD=robot_hat_v4 \
PICRAWLER_SPEAKER_EXPECTED_PIN=20 \
PICRAWLER_SPEAKER_DEVICE=plug:softvol PICRAWLER_SPEAKER_GAIN=0.5 \
robot/.venv/bin/python -m robot.audio
```

Only when the owner chooses to test this new integration, the same command with
`--play` emits one quiet cue and returns checked lifecycle results:

```bash
PICRAWLER_ACTUATORS_ENABLED=0 PICRAWLER_HOME_ON_START=none \
PICRAWLER_SPEAKER_BACKEND=robot_hat_v4 \
PICRAWLER_SPEAKER_VERIFIED_BOARD=robot_hat_v4 \
PICRAWLER_SPEAKER_EXPECTED_PIN=20 \
PICRAWLER_SPEAKER_DEVICE=plug:softvol PICRAWLER_SPEAKER_GAIN=0.5 \
robot/.venv/bin/python -m robot.audio --play
```

Check both `last_result` and audibility as separate observations. Stop on an
error, unexpected pin/board, already-high amplifier, or unconfirmed cleanup.
Do not repeatedly retry it or increase a system mixer to conceal the failure.

After that separate owner check, the Pi server uses the same per-process profile:

```bash
PICRAWLER_SIMULATE=0 PICRAWLER_ACTUATORS_ENABLED=0 PICRAWLER_HOME_ON_START=none \
PICRAWLER_AUDIO_ENABLED=1 PICRAWLER_SPEAKER_BACKEND=robot_hat_v4 \
PICRAWLER_SPEAKER_VERIFIED_BOARD=robot_hat_v4 \
PICRAWLER_SPEAKER_EXPECTED_PIN=20 \
PICRAWLER_SPEAKER_DEVICE=plug:softvol PICRAWLER_SPEAKER_GAIN=0.5 \
bash robot/run.sh
```

The Jetson can then request model-free cues while keeping STT, camera, local LLM
and body expressions disabled. This starts no microphone consumer on the Jetson:

```bash
bash brain/run.sh pet -- --base-url http://10.1.20.170:8000 --dashboard \
  --voice --no-stt --no-camera --no-llm
```

Type `hello` for an acknowledgement cue. Piper speech is still unavailable unless
its existing local executable/model are provisioned separately. `--no-voice`
continues to suppress all output. Do not append these values to `crab.env` or a
systemd unit as part of this test; the inline configuration preserves saved data.

## Capture remains separate

The owner's independent 5-second USB-microphone check used
`plughw:CARD=Device,DEV=0`. A raw gain of 4 (+5.95 dB) produced a higher signal and
was restored to 0 afterward. This profile neither records nor changes microphone
gain; there is no automated loopback/acoustic self-test. Camera quality, dashboard
design and sonar behavior are separate work.

## Development checks

Fake metadata, processes and GPIO readbacks exercise wrong/unknown boards,
configured routing, gain/peak bounds, enable/readback/prefill/payload errors,
timeouts, cancellation, thread/process contention, cleanup failure/inhibition,
shutdown and audio-disabled startup. No physical HAT, ALSA output or microphone
was used. Run from repository root:

```bash
brain/.venv/bin/python -m unittest discover -s tests -p test_speaker_hat.py -v
brain/.venv/bin/python -m unittest discover -s tests -p test_audio.py -v
bash test_sim.sh
```
