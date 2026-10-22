# Staged hardware acceptance — not performed

This is the next review gate, not permission to run it. No physical robot,
GPIO, playback, servo, driver, power or reboot operation was performed during
this milestone. Obtain explicit approval for each physical stage and record
actual results, installed versions, date, operator and any bounded error output.

## Gate A: inventory and read-only readiness

- Confirm Pi 4B, physical Robot HAT revision/UUID, actual microphone connection,
  speaker wiring/output and battery hardware against the
  [official-source evidence](HARDWARE_EVIDENCE.md). Do not infer mic support from
  a generic HAT product name. Record `picrawler`/`robot_hat` installed versions
  and source APIs without constructing a Picrawler.
- Keep `PICRAWLER_ACTUATORS_ENABLED=0`, startup homing `none`. Start server and
  inspect readiness/status: each capture's availability, real provenance,
  timestamp, freshness and errors. Confirm no synthetic frame, voltage or range
  appears after disabling/unplugging a real input.
- Verify battery getter for this board; compare with an approved external
  measurement before relying on thresholds. No guessed ADC scale fallback.
- Verify camera/detector capture stamps survive transport and stale/dropout
  evidence is ignored. Confirm unsupported capabilities are not claimed.
- With actuators still disabled, measure dashboard delivery on the actual LAN:
  Pi camera rate/encode time, CPU/RAM, encoded-frame age, typed-input latency,
  detector time and connection recovery. Verify clocks agree before trusting
  cross-device source ages. Exercise a slow detector and network interruption;
  old data must be marked stale and recovery must show current state. Follow
  [live-feed measurements](live-feed-performance.md). Higher frame rates require
  measured headroom; a local browser simulation is not hardware evidence.
- Confirm matching Pi/brain WebSocket protocol via read-only readiness. With
  actuators disabled, check one companion session, dashboard round-trip/error
  reporting, heartbeat timeout, Wi-Fi loss, process restart and fresh status on
  reconnect. Old inputs/leases/results must not replay or clear stop; explicit
  new resume must work after Pi reboot resets its per-boot stop epoch. Verify
  independent HTTP stop under a slow/blocked socket. Record p50/p95 event delivery,
  status capture duration and CPU/RAM using the
  [WebSocket measurement method](websocket-transport.md). Opening and closing a
  control WebSocket is an active stop boundary, not a read-only readiness probe.
- Inspect ALSA configuration/devices, service-account groups/permissions and
  board-specific amplifier enable state using the read-only audio diagnostics.
  Follow the [six-layer audio guide](audio-acceptance.md); a missing driver is
  only one possible cause. No installation/configuration changes at this gate.

## Gate B: approved audio, no actuators

- Explicitly approve short direct playback under the actual service account.
  Record selected output, subprocess exit code, bounded stderr and human audible
  confirmation separately. An exit code of zero alone is insufficient.
- Test `/audio/self-test` with `play:true` only after approval. Test local TTS
  generation, WAV validity, lease/session metadata, network response and output.
- Test disabled output, wrong ALSA device, bad WAV, player failure, network loss,
  stale/reordered audio and server restart. No response may claim sound when
  simulated/disabled/failed. Replay after stop must be rejected.
- Test speech/effects while listening; check partial buffers and completed STT
  overlapping playback are discarded. Half-duplex means voice stop may not be
  heard during playback: use dashboard/physical control. No sound-direction
  localization or acoustic echo cancellation is claimed.

## Gate C: approved Pi-local motor bring-up

- Use a controlled, supported/elevated rig as appropriate, clear legs and cables,
  charged known-good pack, visible accessible physical power cutoff and a human
  present. Verify vendor calibration separately; do not write repository offsets.
- Explicit actuator enablement permits lazy vendor initialization on the first
  authorized primitive. **The vendor constructor resets its MCU and commands
  servo positions.** It runs only after fresh power/forward-clearance checks,
  is accounted as an initialization boundary and cannot be interrupted inside
  the vendor call. Physically validate this before any ordinary pose test.
- Approve staged sit/stand individually. Record power sag, temperature, stall,
  duration and per-leg stop boundaries. Stop between stages must latch; no
  follow-on movement until explicit resume. Critical/unknown battery blocks
  movement, including gestures and initialization.
- Only then approve one forward/left/right vendor cycle in a flat controlled
  area with inanimate obstacles. Record actual displacement variability and
  worst stop latency; do not treat simulator 6cm/15° assumptions as calibration.
  No reverse, exact angle, pet chasing, stairs or cliff test is authorized.
- Test stop versus concurrent resume, missing/stale commands, network loss and
  restart. Software cannot guarantee stopping mid-cycle; the physical cutoff is
  the last resort. Failed completion is uncertain, never automatically retried.

## Gate D: Jetson budget and companion session

- Record JetPack/CUDA/kernel, model/build hashes, current approved power mode,
  resident services, CPU, total shared RAM/GPU allocation, peak process RSS,
  swap and temperatures. Do not silently change power mode.
- Measure cold/warm p50/p95 STT, TTS, detector and LLM latency, plus timeout and
  unload behavior. Budget OS/services/headroom within **8GB shared memory**.
  The delivered host benchmark measures only coordinator/simulator logic.
- Begin with one small interpreter and optional speech; do not assume VLM,
  detector, Whisper and Piper fit together. Select/load/unload residency based
  on measured peaks before enabling the full pipeline.
- Run greeting, repeated engagement, quiet companionship, play invitation,
  interruption, unknown speech, sensor failure and reconnect sessions. Confirm
  acknowledgements are sparse, body stays nearby, observations/memories remain
  inspectable and no identity is invented. Record user-observed sound/body timing.

Future supervised exploration requires a separate implementation/review: common
camera/sonar/costmap clearance, grounded expiring inanimate targets, conservative
motion estimates and bounded retreat-to-stillness. Stock sensing is not a stair,
cliff or collision guarantee. No automatic merge or deployment follows acceptance.
