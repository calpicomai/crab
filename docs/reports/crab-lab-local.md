# Crab Lab v0 — local verification, 2026-10-27

Local branch `feat/crab-lab-prototype`, based on the read-only bridge's sanitized
tree `8d435dde327d5e7d97abf995abf335d41118d359`. Simulator remains unpublished.
No device/owner-PC operations, hardware routes, capture, media recording, ROS
installation, paid compute or training. Existing robot/brain code is unchanged.

## Verified results

| Check | Result |
| --- | --- |
| Real physics/controller/API/episode tests | **21PASS**: `.venv/bin/python -m unittest discover -s lab/tests -v`. Actual MuJoCo, no mocked dynamics. Includes gravity/drop/contact, stand/crawl, hard joint stops and torque bounds, seeded reset/chunking, missing/stale/future/expired sonar, inhibition/hold/fall reporting, engine transforms, policy/truth separation, bounded controls, failure latch/reset, deterministic sampled episodes and bounded exclusive exports. |
| Independent kinematics agreement |40joint configurations: analytic FK agrees with actual engine foot sites within1e-12m, now a regression. Reviewer independently observed maximum error8.4e-17m. |
| Eight simulated seconds, seed1 flat crawl | Forward+.0334913m, total travel.0335693m, height.0900929m, final tilt.0102327rad, peak sampled tilt.0121424rad; no fall, joint limit, target or actuator saturation. This is simulation with estimates, not measured robot performance. |
| Coding-host headless episode |8simulated seconds in.537799wall seconds (~14.875simulated-seconds/s), including20Hz sampled episode summaries. One observed run, no throughput guarantee or Windows/GPU/Jetson extrapolation. |
| Scenario review | Reviewer ran50simulated seconds each: flat/low-friction/dropout remained finite/upright; forward travel.226/.198/.097m. No physical fidelity claim. |
| Actual Chromium + real local server | Desktop1512px and narrow390px: controller selection, actual body motion, pause, step, reset/seed, sonar-dropout inhibition, no horizontal overflow, disconnect/reconnect and zero JavaScript errors. A separately injected **synthetic UI error payload** verifies Reset-only error presentation. |
| Visual inspection | Desktop crawl and narrow dropout screenshots were opened and inspected. Canvas uses actual engine geom transforms. Screenshots remain local test evidence; they were not uploaded or published. |
| Existing project regression | Shared existing brain/robot environments: `bash test_sim.sh`,383testsPASS, off-body,9scenarios, synthetic camera/HTTP, greeting/priority STOP/explicit Resume full smoke PASS. No product protection or test timeout weakened. |
| Dependency/static checks | `pip check`, Python compileall, `node --check lab/static/app.js`, `git diff --check` PASS. CPython312/win_amd64 MuJoCo3.17wheel availability verified by download only, not Windows execution. |

Runtime: coding host Debian13 amd64/Python3.12.14; MuJoCo3.17.0,
NumPy2.5.4, FastAPI0.116.1, Uvicorn0.35.0; Chromium154.0.8037.57. Physics dt2ms;
one writer with bounded latest state, no accumulated catch-up burst. Browser
polling has one in-flight request at up to10Hz. Ordinary browser view needs no
GPU. No actual Windows CPU/VRAM/driver or training throughput benchmark exists.

## Failures found and resolved

Physics proof-first tests initially failed for absent lab implementation.
Server and episode tests independently failed for absent modules before their
implementation. Real compliant-stop testing exposed ~.037rad excess under forced
torque; the provisional stop compliance was tightened and explicitly recorded
as estimated. The hard-stop test bypasses target clamping to test actual joint
constraints and force saturation, rather than merely testing the clamp itself.

Independent review found manual-step errors bypassed the runtime failure latch;
an observed failing regression now verifies paused/reset-required state. It also
found future capture times could authorize crawl; the regression failed before
the simulation-clock fence and passes after. A second review found mode changes
could mutate retained state after failure; a failing200-versus409 regression
now verifies rejection and unchanged cached state. All review findings resolved.

Initial browser launch could not find Playwright's bundled browser; the test now
uses the already-installed Chromium. No browser/system installation occurred.
An initial legacy-suite attempt in the new Lab environment had two import errors
because Pillow is intentionally absent. The appropriate existing project
environments subsequently passed the full383test smoke; no Lab dependency was
added merely to duplicate the existing media environment.

## Honest limits and next gates

Source hints are not verified CAD; masses/inertias, axes, limits, servo dynamics
and contacts are estimated. Uncertainty ranges are documented, not yet swept.
The controller is procedural/open-loop with sonar inhibition; no learned policy,
joint feedback, IMU/contact adaptation, navigation, vision/planner, balance or
recovery controller is implemented. Falls are evaluator reports, not hidden
policy observations. Episode exports are sampled prototype transitions, not full
actuator replay or a training-ready dataset. No real sensor dataset is gathered.

Next: verify geometry/servo identity/calibration mapping, expand parameter and
failure sweeps, establish a deterministic baseline, then evaluate perception,
planning and an offline learning loop with held-out cases. Physical tests need
separate authorization, measured hardware facts and supervised acceptance.
Jetson storage remains unresolved; do not install Lab on that device.

## Bridge publication handoff

The original bridge head51a8e03 and public-noreply metadata copy
`8d435dde327d5e7d97abf995abf335d41118d359` are preserved locally. GitHub initially
rejected private-email metadata; the corrected command-line push then failed
because Git credentials were unavailable. An early scan mistakenly treated an
address in the file diff as author metadata. Direct Git-object inspection
corrected that mistake: the connected app commit's **author and committer both
match the known public GitHub noreply identity on main**. No different personal
address was published, and no privacy settings or credentials were changed.

The connected API route published the identical tested tree
`0c5908555558a0603307cc5e303efc234d87be72` as
`7234dac784b62851ecdf87ddc9b11c1330ac8da4` in
[draft PR47](https://github.com/calpicomai/crab/pull/47). It includes only the
bridge/design stage, not this simulator. CI assessment is recorded separately;
no ready transition, merge or deployment is authorized. Do not rerun the earlier
publication handoff or push an older local-history commit over that branch.
