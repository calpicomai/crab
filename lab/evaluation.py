"""Bounded deterministic research checks, independent of training/hardware."""
import hashlib
import json
import math

from .episodes import run_episode

SCENARIOS=('flat','low_friction','sonar_dropout')


def run_evaluation(*,seeds=(1,),seconds=5.):
    if not isinstance(seeds,(tuple,list)) or not 1<=len(seeds)<=5:
        raise ValueError('evaluation requires1..5explicit seeds')
    if any(type(x) is not int or not 0<=x<=2**32-1 for x in seeds) or len(set(seeds))!=len(seeds):
        raise ValueError('evaluation seeds must be unique integers in0..4294967295')
    cases=[]
    for scenario in SCENARIOS:
        for seed in seeds:
            episode=run_episode(seed=seed,scenario=scenario,seconds=seconds,mode='crawl')
            summary=episode['summary'];metrics=summary['final_metrics']
            decisions=[row['controller_decision'] for row in episode['steps'] if row['controller_decision']]
            def safe(decision):
                if decision['state']!='crawling':return True
                observation=decision['observation'];sonar=observation['sonar']
                return (sonar['valid'] and not sonar['no_echo'] and sonar['distance_m']>=.22
                        and 0<=sonar['age_s']<=.15
                        and 0<=observation['capture_time_s']<=decision['simulation_time_s']+.002+1e-9
                        and observation['deadline_s']>=decision['simulation_time_s'])
            assertions={
                'upright':not metrics['fallen'],
                'finite_metrics':all(type(v) not in (int,float) or math.isfinite(v) for v in metrics.values()),
                'within_joint_limits':metrics['joint_limit_count']==0,
                'no_target_clipping':metrics['target_saturation_count']==0,
                'crawling_has_supported_observation':all(safe(x) for x in decisions),
            }
            if seconds>=4 and scenario=='flat':assertions['forward_progress']=metrics['forward_displacement_m']>.001
            if seconds>=.7 and scenario=='sonar_dropout':assertions['missing_echo_inhibits']=summary['inhibited_samples']>0
            cases.append({'seed':seed,'scenario':scenario,'manifest':episode['manifest'],
                'summary':summary,'assertions':assertions,'passed':all(assertions.values())})
    # Timing/host throughput are observations, not reproducibility inputs.
    deterministic=[{'seed':x['seed'],'scenario':x['scenario'],'manifest':x['manifest'],
        'metrics':x['summary']['final_metrics'],'assertions':x['assertions']} for x in cases]
    for item in deterministic:item['manifest']={k:v for k,v in item['manifest'].items() if k!='platform'}
    fingerprint=hashlib.sha256(json.dumps(deterministic,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return {'schema_version':1,'scope':'provisional physics/controller research regression',
        'hardware_acceptance':False,'learned_policy':False,'passed':all(x['passed'] for x in cases),
        'fingerprint':fingerprint,'reproducibility_scope':'same model/code/runtime versions; not bitwise cross-platform equivalence',
        'cases':cases}
