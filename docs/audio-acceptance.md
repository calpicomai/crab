# Audio diagnosis and hardware acceptance

Software failure reporting and mocked audio tests are implemented. **Audible output,
Pi ALSA/I2S setup, actual service permissions, microphone capture, Jetson Piper and
Whisper inference are hardware validation pending.** No robot was contacted, GPIO
toggled, package/driver installed or reboot performed during development.

The observed silence has several independent possible causes. The previous code
ignored `aplay`'s exit status, silently switched to simulation if ALSA was missing,
and accepted failed HTTP playback as success. Those defects are confirmed and
fixed. They do **not** establish the cause of silence on the user's Pi.

2026-10-26 follow-up: owner-provided v4/2.5.3, GPIO and `plug:softvol` one-shot
evidence supports an explicit opt-in [Robot HAT speaker profile](robot-hat-speaker.md).
Application hardware acceptance is still separate; generic output remains the
default and runtime never changes global ALSA/Pulse settings.

## Official sources checked, 2026-10-22

The SunFounder documentation host was inaccessible from this workspace, so its
maintained source was read from the official `sunfounder` GitHub repositories:

- [Speaker Audio Setup](https://github.com/sunfounder/robot-hat/blob/2.5.x/docs/source/install_i2s_for_speaker.rst), blob `e4a50381b3d3002c64a27074632402e1f2cadc07`: current guidance offers `robot_hat doctor` to distinguish overlay, sound-card, routing and I2S clock problems, `speaker test`, and `speaker setup`. Older versions use `i2samp.sh`. Availability must be checked against the installed version.
- [Official installer](https://github.com/sunfounder/robot-hat/blob/2.5.x/i2samp.sh), blob `c28a0395f8161b9e83b280a6c5b0ee595e698bdf`: uses different overlays and speaker-enable pins for older HATs versus HAT 5, and different microphone support. It changes OS configuration, volume and routing and may ask to reboot; it is **not** a diagnosis to run automatically.
- [Vendor device support](https://github.com/sunfounder/robot-hat/blob/2.5.x/robot_hat/device.py), blob `2546a11d60bfbe1e771d481f9553ecd6b4852c11`: board-specific speaker enabling, with older board pin 20 versus HAT 5 pin 12 selected by the vendor. Do not copy a guessed pin into application code.
- [Vendor Music](https://github.com/sunfounder/robot-hat/blob/2.5.x/robot_hat/music.py), blob `1c5f5b83d0c354d8f90f072c06e86816c15f4765`: enables the speaker when initialized. Crab's raw `aplay` path did not perform that initialization. A disabled amplifier is therefore another hypothesis, **not a verified diagnosis**. Crab deliberately reports amplifier state as unverified and does not silently toggle it.
- [HAT hardware description](https://github.com/sunfounder/robot-hat/blob/2.5.x/docs/source/hardware_introduction.rst), blob `3edfe0f51ed22f7b938dfaef5f0f4224bcff748f`: I2S mono output; board and installed-library revision must be recorded. Do not infer sound direction, stereo spatial hearing or microphone availability from the word “HAT.”
- [PiCrawler installation](https://github.com/sunfounder/picrawler/blob/main/README.md) points to the `robot-hat` 2.5.x branch and the [PiCrawler documentation](https://docs.sunfounder.com/projects/pi-crawler/en/latest/).

The development environment is Linux x86_64. Neither provided Python environment
contains `robot_hat`, `picrawler`, Piper or faster-whisper; `aplay` and `arecord`
are absent. No claim about the installed Pi/Jetson libraries follows from that.

## What the new diagnostics mean

`robot/.venv/bin/python -m robot.audio` reports the current user/groups, selected
sink, ALSA device-list status/errors, readable audio configuration, `/dev/snd`
access and the last playback outcome. It does not play sound by default. It does
not change routing, volume, drivers, GPIO or service accounts.

The server exposes `GET /audio/health`, `POST /audio/self-test` with
`{"play": false}` by default, and structured `/audio/play` results. Playback
accepts bounded, short 16-bit PCM WAVs; output is serialized without an audio
queue. Invalid audio, a missing player, timeout, cancellation and nonzero exit
are reported with bounded diagnostics. `ok=true, played=true` means the playback
process exited successfully; only a person listening can verify audibility.
Explicit simulation always says `played=false` and emits no sound.

Network play requires `X-Crab-Audio-Session` (a per-client opaque identifier) and
`X-Crab-Audio-Sequence` (strictly increasing positive integer). Voice supplies
these automatically. A stop carries a newer sequence, so an older play request
arriving after stop is rejected. Duplicate requests are rejected as well. The Pi
keeps at most 64 session fences and fails closed on additional sessions until
restart, rather than evicting a fence and accepting delayed audio. Local
self-tests need no session. Network play additionally requires a one-use Pi
dispatch lease in `X-Crab-Audio-Lease`, obtained from `POST /control/lease`
immediately before sending the WAV. It expires on the Pi monotonic clock after
two seconds and is invalidated by stop or restart. Voice obtains it automatically
after synthesis; it never retries a dispatched WAV. The Pi consumes the lease
under the speaker state lock so stop cannot overtake authorization and leave a
late playback starting afterward. Session fences handle request ordering and
leases reject work from an earlier server run.

`Voice.health()` separates local model readiness, effects availability,
synthesis failures and network/playback results. A successful HTTP status alone
is insufficient: the Pi must confirm real playback. Three deterministic, quiet
local cues (`acknowledge`, `settle`, `play`) work without Piper when output is
enabled. Piper and Whisper use already installed local models; Whisper cannot
download at runtime. Short speech and effects are dropped when busy, never piled
up behind stale responses.

During playback and a 0.7-second tail, Pi capture drains but masks speaker audio;
Jetson listening also drains/discards chunks and rejects transcription that
finishes across playback or takes over five seconds. This is conservative
half-duplex coordination, not acoustic echo cancellation. Spoken interruption
while the speaker is playing is not guaranteed: the explicit stop control and
accessible physical power cutoff remain necessary. Audio stop cancels pending
synthesis and terminates the owned local player, and requests Pi stop with a
bounded timeout; network loss can leave playback running until its duration cap.

## Staged hardware gates — execute only after explicit approval

Keep actuators disabled and startup homing off throughout audio testing. Record
results, commands, installed versions and timestamps; preserve existing OS and
application configuration before any approved change.

1. **Inventory and read-only diagnosis.** Record the visible HAT revision and
   installed `robot_hat`/PiCrawler version and source path. Inspect service `User`,
   supplementary groups and environment. Compare the diagnostic report under the
   interactive user and the actual service account. Inspect boot audio overlay,
   ALSA cards/default sink, `/etc/asound.conf`, device permissions and mixer mute
   state. If the installed vendor CLI supports it, inspect its help and use its
   read-only doctor. Do not use `--fix` or `speaker setup` yet.
2. **Direct playback.** With approval for sound output, use the existing known
   WAV and selected ALSA device under the service account, retaining exit status
   and stderr. Confirm actual audible output and sensible volume. If exit status
   is zero but there is silence, inspect routing, mixer state, amplifier enable
   and the I2S clock before assuming a driver is missing. Any board-enable,
   setup/driver, volume/system configuration or reboot change is a separate
   approval gate; use the installed vendor mechanism for the verified revision.
3. **Pi audio application.** Run `python -m robot.audio --play --device <verified-device>`
   with approval, then the robot endpoint self-test with `{"play": true}`. Confirm
   both honest JSON results and audible output under the server's account. No
   gait endpoint is involved. Verify missing/incorrect device errors remain
   visible and `/audio/stop` interrupts playback.
4. **LAN delivery.** From the Jetson send that same known short WAV to the
   configured Pi `/audio/play` with a fresh dispatch lease, session header and increasing sequence,
   verify status **and** `played`, and listen. Record
   round-trip latency. A network failure is a delivery failure, not TTS failure.
5. **Jetson synthesis.** Confirm Piper executable, local `.onnx` and adjacent
   `.onnx.json` model. Generate a short WAV locally, inspect its format/duration
   and result, then send through the verified Pi sink. Measure peak memory and
   synthesis latency on the actual Jetson; cloud mock timings are not inference
   measurements. Do the same for cached Whisper with `local_files_only=True`.
6. **Listening coordination.** Capture approved test speech using the actual
   microphone and verify provenance, fresh capture and transcription. Play an
   acknowledgement; it must not become a new user command. Test failure,
   interruption, tail suppression and reconnect. Record any missed user speech;
   tune the tail only from evidence. Do not claim sound-direction sensing.

## Development checks

```bash
brain/.venv/bin/python -m unittest discover -s tests -p test_audio.py -v
robot/.venv/bin/python -m robot.audio --simulate
```

These checks exercise failure statuses, payload validation, cancellation,
explicit silent simulation, subprocess errors/timeouts, HTTP rejection and
half-duplex suppression without touching hardware. They do not satisfy the
hardware gates above.
