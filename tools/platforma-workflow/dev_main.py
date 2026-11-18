#!/usr/bin/python3
"""Development-only entry point. Production main.py remains separately scoped."""
import json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from main import dispatch
from release_control.core import Controller,Store,Refused,read
from release_control.linux import Linux,trusted,validate_development_scope

def engine(base):
    if os.geteuid()!=0 or os.uname().nodename!='kromin':raise Refused('development_host_or_operator_required')
    base=trusted(base,True);cfg=read(trusted(base/'config/release.json'))
    validate_development_scope(cfg)
    if Path(cfg['project_dir'])!=base/'app' or Path(cfg['state_dir'])!=base/'state':raise Refused('development_layout_mismatch')
    return Controller(Store(cfg['state_dir']),Linux(cfg))

def main():
    base=Path(__file__).resolve().parents[1]
    ctl=engine(base)
    value=ctl.worker() if sys.argv[1:]==['_worker'] else dispatch(sys.argv[1:],ctl)
    print(json.dumps(value,ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as ex:
        print(json.dumps({'ok':False,'reason':str(ex) if isinstance(ex,Refused) else 'development_internal_error'}))
        raise SystemExit(1)
