#!/usr/bin/python3
"""Called only through the operator-owned dispatcher or systemd."""
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import json
from release_control.core import Controller,Store,Refused,read
from release_control.linux import Linux,trusted
CONFIG=Path('/etc/platforma-release.json')
def dispatch(args,controller):
    if not args:raise Refused('release_command_required')
    action=args[0]
    if action=='current' and len(args)==1:return controller.current()
    if action=='resume' and len(args)==2:return controller.resume(args[1])
    if action=='plan' and len(args)==2:return controller.plan(args[1])
    if action=='apply' and len(args)==3:return controller.apply(args[1],args[2])
    if action in ['status','report'] and len(args)==2:return controller.status(args[1])
    if action=='accept' and len(args)==2:return controller.accept(args[1])
    if action=='recover' and len(args)==2:return controller.recover(args[1])
    if action=='reopen-previous' and len(args)==2:return controller.reopen_previous(args[1])
    raise Refused('release_command_not_allowed')
def main():
    if os.geteuid()!=0:raise Refused('operator_wrapper_required')
    trusted(Path(__file__).resolve());trusted(CONFIG)
    cfg=read(CONFIG)
    if cfg['mode']!='production':raise Refused('laboratory_config_not_allowed_in_installed_cli')
    engine=Controller(Store(cfg['state_dir']),Linux(cfg))
    result=engine.worker() if sys.argv[1:]==['_worker'] else dispatch(sys.argv[1:],engine)
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':
    try:main()
    except Exception as ex:
        print(json.dumps({'ok':False,'reason':str(ex) if isinstance(ex,Refused) else 'internal_error'}))
        raise SystemExit(1)
