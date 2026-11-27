#!/usr/bin/python3
"""Read-only, fixed-path capacity report on the existing development host."""
import json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from release_control.core import Refused
from release_control.storage import check,MAX_REQUIRED_BYTES

def report(args):
    if os.geteuid()!=0 or os.uname().nodename!='kromin':raise Refused('development_host_or_operator_required')
    if len(args)!=2 or args[0]!='import' or not args[1].isdigit() or len(args[1])>11:
        raise Refused('storage_command_not_allowed')
    required=int(args[1])
    if not 0<required<=MAX_REQUIRED_BYTES:raise Refused('storage_budget_invalid')
    return {'environment':'dev','operation':'image_import','docker_load_performed':False,
            'production_changed':False,'capacity':check(Path('/var/lib/docker'),required)}

if __name__=='__main__':
    try:
        result=report(sys.argv[1:]);print(json.dumps(result));raise SystemExit(0 if result['capacity']['sufficient'] else 1)
    except (Refused,OSError):
        print(json.dumps({'ok':False,'reason':'storage_command_refused','writes_performed':False}));raise SystemExit(1)
