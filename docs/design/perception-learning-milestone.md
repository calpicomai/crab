# Next milestone: timed perception evidence before learning

The current system has a read-only ROS observer, correlated dashboard outcomes
and a provisional MuJoCo Lab with procedural decisions and bounded synthetic
episodes. It has no learned locomotion policy, implemented learning pipeline,
deployed vision model, tracking/planning overlay or validated digital twin.
The next increment should connect a small, testable perception-to-decision slice
in off-device replay. It must leave physical command routing absent.

## Deliverable and boundary

Implement one perception worker behind a project-owned interface, initially
using generated scenes or an explicitly selected synthetic fixture set. Its
output feeds an observation-only dashboard overlay and a bounded goal-selection
experiment in Lab. Keep capture, inference, tracker, planner and display owners
separate; one latest-frame slot replaces stale work. No extra live Pi consumer,
model download, Jetson installation, recording or training is implied.

Each observation needs source/boot/session, frame ID, image dimensions and
coordinate convention, acquisition/receipt/processing times, clock domain and
uncertainty, model/config hash, availability/error, and original capture age.
Detection confidence is a model score, not measured probability of safety.
Tracks need their own IDs, evidence age and reset rules; they cannot imply person
identity. Preserve measured sonar separately from estimated camera geometry.
Monocular imagery cannot certify metric free space, depth, cliffs or balance.

The dashboard should draw only actual detections/tracks on their matching frame,
with age, confidence, dropped-frame count and explicit missing/stale/error states.
If the displayed frame differs, withhold or clearly age the overlay. Show the
real selected goal/candidate constraint reason and intended versus observed
outcome. Do not fabricate a path, foothold, object, world model or explanation.
Concise reason codes and evidence references are sufficient; private model
chain-of-thought is neither needed nor exposed.

## Evaluation gates

| Gate | Required evidence |
| --- | --- |
| Contract | Typed, bounded schemas reject malformed/nonfinite/oversize input; source timestamps cannot be refreshed by replay; model estimates remain distinct from measurements. |
| Timing/failure | Deterministic tests cover missing frames, delayed/out-of-order results, reboot/reconnect, queue saturation, model timeout and resolution changes; stale results cannot become current goals. |
| Perception quality | Curated synthetic labels and fixed held-out scenes measure detection precision/recall, tracking ID errors and unsupported/unknown objects. Record fixture/model versions; do not report accuracy without a dataset and denominator. |
| Runtime budget | Off-device baseline records decode/inference/track/render p50/p95, peak RSS and dropped work. Jetson acceptance later repeats under its actual 8GB shared-memory constraints; no throughput promise from a workstation GPU. |
| Decision evidence | Every selected Lab action references exact policy-visible observations and constraint reasons. Privileged pose/contact/depth truth is evaluator-only and cannot leak into a deployable policy interface. |
| Isolation | Replay/simulator cannot import the physical adapter, accept Pi command URLs, publish control topics or start capture. Dashboard overlays grant no motion authority. |

Choose the first pretrained component only after checking license, supported
runtime/ABI, model size and measured accuracy/latency against these fixtures.
The owner accepts pretrained components while Crab owns its interfaces, goals,
controller and behavior. Standard ROS/MuJoCo/media libraries remain appropriate;
a wholesale framework or Rust rewrite needs a measured bottleneck.

## Data and improvement loop

Extend the versioned synthetic episode contract with frame/observation references,
model versions, chosen bounded actions, outcome and intervention/correction
events. Record code/model/scenario hashes and reproducible seeds. Keep policy
observations separate from privileged evaluator truth. Current Lab20Hz sampled
transitions are not an every-command replay log or training-ready dataset.

Before training, implement an explicit episode manifest and deterministic
train/development/held-out split grouped by source episode/scene; adjacent frames
must not leak across splits. Run fixed baseline policies and regression suites
first. Export remains opt-in, bounded and exclusive-create. Record retention and
deletion metadata. No real camera/microphone recording by default, no automatic
upload and no hidden continuous collection. Any real-data stage requires explicit
owner opt-in, purpose, duration, byte/time cap, private storage and deletion path.

Learning is an offline, versioned candidate step: curate data, train on approved
compute, evaluate held-out scenarios and safety regressions, compare with the
procedural baseline, approve a model version and retain rollback. No online
self-modifying control or direct LLM-to-servo path. Actual arm64 perception and
sim-to-real controller acceptance remain separate hardware gates, including
model/servo fidelity, feedback availability and supervised STOP measurements.

Optional future LiveKit may carry observation media if its measured end-to-end
age/resource cost improves the current JPEG path. It is not an episode database
or learning algorithm. Any ROS Portal proposal must explicitly allowlist outbound
observations and disable incoming actuation/graph-wide forwarding; local watchdogs
and control remain independent. It is not a prerequisite for this increment.
