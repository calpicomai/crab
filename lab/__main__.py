"""Explicit standalone laboratory; never imports the physical robot stack."""
import argparse


def main():
    parser=argparse.ArgumentParser(description='Crab Lab: provisional CPU physics, no hardware route or learned policy')
    sub=parser.add_subparsers(dest='command')
    serve=sub.add_parser('serve',help='Local browser arena (paused initially)')
    serve.add_argument('--port',type=int,default=8765)
    episode=sub.add_parser('episode',help='Run one bounded reproducible synthetic episode')
    episode.add_argument('--seed',type=int,default=1)
    episode.add_argument('--scenario',choices=['flat','low_friction','sonar_dropout'],default='flat')
    episode.add_argument('--seconds',type=float,default=5.)
    episode.add_argument('--mode',choices=['stand','crawl','stop'],default='crawl')
    episode.add_argument('--output',help='Explicit new local JSON file; existing files are never overwritten')
    evaluate=sub.add_parser('evaluate',help='Bounded synthetic regression suite; no training or hardware')
    evaluate.add_argument('--seeds',default='1',help='1..5distinct integer seeds, comma-separated')
    evaluate.add_argument('--seconds',type=float,default=5.)
    evaluate.add_argument('--output',help='Explicit new local JSON report; never overwrite')
    args=parser.parse_args()
    if args.command in ('episode','evaluate'):
        from .episodes import run_episode,write_episode
        import json
        try:
            if args.command=='evaluate':
                from .evaluation import run_evaluation
                if len(args.seeds)>64:raise ValueError('seed list exceeds bound')
                result=run_evaluation(seeds=[int(x) for x in args.seeds.split(',')],seconds=args.seconds)
            else:
                result=run_episode(seed=args.seed,scenario=args.scenario,seconds=args.seconds,mode=args.mode)
            if args.output:write_episode(result,args.output)
        except (ValueError,FileExistsError) as exc:parser.error(str(exc))
        display=result.get('summary')
        if display is None:
            display={k:v for k,v in result.items() if k!='cases'}
            display['cases']=[{k:v for k,v in case.items() if k!='manifest'} for case in result['cases']]
        print(json.dumps(display,indent=2))
        return 0 if result.get('passed',True) else 1
    else:
        import uvicorn
        from .server import create_app
        port=getattr(args,'port',8765)
        if not 1024<=port<=65535:parser.error('port must be1024..65535')
        uvicorn.run(create_app(),host='127.0.0.1',port=port,log_level='warning')


if __name__=='__main__':raise SystemExit(main())
