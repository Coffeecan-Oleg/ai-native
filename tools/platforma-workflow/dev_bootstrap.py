#!/usr/bin/python3
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import development
from release_control.core import Refused,read

def main():
    args=sys.argv[1:];source=Path(__file__).resolve().parent
    if args==['plan']:value=development.plan(source)
    elif len(args)==3 and args[0]=='apply':value=development.apply(source,read(args[1]),args[2])
    elif args==['check']:value=development.check(Path('/opt/platforma-dev'))
    else:raise Refused('development_bootstrap_command_not_allowed')
    print(json.dumps(value,ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as ex:
        print(json.dumps({'ok':False,'reason':str(ex) if isinstance(ex,Refused) else 'development_bootstrap_internal_error'}))
        raise SystemExit(1)
