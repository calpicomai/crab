"""Explicit, bounded synthetic episodes. No recorder or hardware replay service."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import platform
import time
import sys
import numpy as np

from .physics import LabWorld

MAX_EXPORT_BYTES=8*1024*1024
SPLITS={'flat':'development','low_friction':'held_out_condition','sonar_dropout':'held_out_condition'}


def run_episode(*,seed=1,scenario='flat',seconds=5.,mode='crawl'):
    if type(seconds) not in (int,float) or not math.isfinite(seconds) or not .05<=seconds<=30:
        raise ValueError('episode duration must be finite and within0.05..30seconds')
    world=LabWorld(seed=seed,scenario=scenario,mode=mode)
    first=world.snapshot()
    specs=Path(__file__).with_name('specs.json').read_bytes()
    manifest={'schema_version':1,'provenance':'simulation_only','seed':seed,'scenario':scenario,
        'split':SPLITS[scenario],'model_version':first['model_version'],
        'model_spec_sha256':hashlib.sha256(specs).hexdigest(),'engine_version':first['engine_version'],
        'controller_version':first['controller_version'],'controller_kind':'procedural_open_loop',
        'learned_policy':False,'hardware_route':False,'recording_source':'explicit local CLI',
        'dt_s':first['dt_s'],'sample_period_s':.05,'platform':platform.platform(),
        'dataset_status':'prototype sampled transitions; not a full actuator trace or training-ready dataset',
        'privacy':'synthetic state only; no real camera, microphone or interaction data',
        'units':{'length':'m','time':'s','angles':'rad','force':'N'},
        'truth_separation':'evaluation_truth is privileged; exclude it from deployed policy observations',
        'conditions':json.loads(specs),'model_fidelity':'provisional estimates, no hardware acceptance'}
    manifest.update(python_version=sys.version.split()[0],numpy_version=np.__version__,
        controller_code_sha256=hashlib.sha256(Path(__file__).with_name('controller.py').read_bytes()).hexdigest(),
        physics_code_sha256=hashlib.sha256(Path(__file__).with_name('physics.py').read_bytes()).hexdigest(),
        model_code_sha256=hashlib.sha256(Path(__file__).with_name('model.py').read_bytes()).hexdigest(),
        randomized_channels=['sonar_noise'],training_split_enforced=False)
    rows=[];peak_tilt=0.;fall=False;inhibited=0;started=time.monotonic()
    total_steps=round(seconds/first['dt_s'])
    sample_steps=max(1,round(.05/first['dt_s']))
    before=first
    for offset in range(0,total_steps,sample_steps):
        after=world.step(min(sample_steps,total_steps-offset))
        # These are sampled transitions: target_before is the issued target at
        # interval start, target_after is the final requested target. The
        # procedural controller also runs between them; no exact replay claim.
        rows.append({'time_s':before['sim_time_s'],'next_time_s':after['sim_time_s'],
            'policy_observation':before['policy_observation'],
            'requested_targets':before['targets'],'next_requested_targets':after['targets'],
            'observed_outcome':after['metrics'],'controller_status':after['status'],
            'controller_decision':after['controller_decision'],
            'intervention':None,'predictions':None,'prediction_model_version':None,
            'evaluation_truth':after['evaluation_truth']})
        peak_tilt=max(peak_tilt,after['metrics']['tilt_rad'])
        inhibited+=int(after['status']['state']=='inhibited')
        fall=fall or after['metrics']['fallen']
        before=after
        if fall:break
    elapsed=time.monotonic()-started
    summary={'seed':seed,'scenario':scenario,'mode':mode,'samples':len(rows),
        'simulated_seconds':before['sim_time_s'],'wall_seconds':round(elapsed,6),
        'simulated_seconds_per_wall_second':before['sim_time_s']/max(elapsed,1e-9),
        'benchmark_scope':'this coding host only; not Windows/Jetson performance',
        'final_metrics':before['metrics'],'peak_tilt_rad':peak_tilt,'inhibited_samples':inhibited,
        'termination':'fall' if fall else 'duration_complete','hardware_acceptance':False}
    result={'manifest':manifest,'summary':summary,'steps':rows}
    if len(json.dumps(result,allow_nan=False).encode())>MAX_EXPORT_BYTES:
        raise ValueError('episode exceeds bounded8MiB export size; choose a shorter duration')
    return result


def write_episode(value,path):
    payload=json.dumps(value,allow_nan=False,indent=2).encode()
    if len(payload)>MAX_EXPORT_BYTES:raise ValueError('episode exceeds8MiB export limit')
    # Exclusive create prevents silent overwrite. No output directory, upload,
    # live-recording flag or hardware destination is inferred.
    with Path(path).open('xb') as handle:handle.write(payload)
