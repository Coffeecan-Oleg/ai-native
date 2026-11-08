#!/usr/bin/env python3
"""Cross-platform client. Never transmits SSH private keys or app credentials."""
import argparse,hashlib,json,re,shlex,subprocess,sys,tarfile
from pathlib import Path,PurePosixPath
from handoff import ReceiptError,parse_receipt,read_file,read_remote

from delivery import verify_delivery


def storage_command(required):
    if type(required) is not int or not 0<required<=9*1024**3:raise ValueError('storage_budget_invalid')
    return ['ssh','-T'],shlex.join(['sudo','-n','/usr/bin/python3','-I',
        '/opt/platforma-dev/code/storage_preflight.py','import',str(required)])

def read_storage(ssh_config,required):
    prefix,command=storage_command(required)
    try:
        p=subprocess.run([*prefix,'-F',str(ssh_config),'aiport',command],capture_output=True,timeout=30)
    except (OSError,subprocess.TimeoutExpired):raise ValueError('storage_report_unavailable') from None
    if len(p.stdout)>16384:raise ValueError('storage_report_oversized')
    try:result=json.loads(p.stdout)
    except (ValueError,UnicodeError):raise ValueError('storage_report_unavailable') from None
    if not isinstance(result,dict):raise ValueError('storage_report_invalid')
    cap=result.get('capacity')
    if (result.get('environment')!='dev' or result.get('operation')!='image_import'
            or result.get('docker_load_performed') is not False or result.get('production_changed') is not False
            or not isinstance(cap,dict) or cap.get('writes_performed') is not False
            or type(cap.get('sufficient')) is not bool or cap.get('status') not in ['sufficient','insufficient','unknown']
            or (cap['sufficient'] is True)!=(cap['status']=='sufficient')):
        raise ValueError('storage_report_invalid')
    if cap['status']!='unknown' and (cap.get('required_bytes')!=required or cap.get('reserve_bytes')!=128*1024**2
            or cap.get('path')!='/var/lib/docker'):
        raise ValueError('storage_report_budget_mismatch')
    return result,p.returncode

def remote_command(environment,args):
    sizes={'plan':2,'apply':3,'current':1,'resume':2,'status':2,'report':2,'accept':2,'recover':2,'reopen-previous':2}
    if not args or args[0] not in sizes or len(args)!=sizes[args[0]]:raise ValueError('release_command_not_allowed')
    if args[0]=='plan' and not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',args[1]):raise ValueError('invalid_image_tag')
    if args[0]=='apply':
        if not re.fullmatch('plan-[a-f0-9]{32}',args[1]) or not re.fullmatch('[a-f0-9]{64}',args[2]):raise ValueError('invalid_plan_identity')
    if args[0] in ['resume','status','report','accept','recover','reopen-previous'] and not re.fullmatch('op-[a-f0-9]{32}',args[1]):raise ValueError('invalid_operation_id')
    if environment=='dev':return 'aiport',shlex.join(['sudo','-n','/usr/bin/python3','-I','/opt/platforma-dev/code/dev_main.py',*args])
    if environment=='production':return 'platforma-production',shlex.join(['release',*args])
    raise ValueError('explicit_environment_required')

def main():
    parser=argparse.ArgumentParser(description='Platforma workflow: verify CI image or call a scoped release command')
    sub=parser.add_subparsers(dest='action',required=True)
    check=sub.add_parser('verify-delivery');check.add_argument('--archive',type=Path,required=True);check.add_argument('--report',type=Path,required=True)
    storage=sub.add_parser('storage-check',help='Verify archive and read dev disk/quota capacity; never loads or deploys')
    storage.add_argument('--archive',type=Path,required=True);storage.add_argument('--report',type=Path,required=True)
    storage.add_argument('--ssh-config',type=Path,required=True)
    image=sub.add_parser('import-image',help='Verified dev image import only; does not deploy')
    image.add_argument('--environment',choices=['dev'],required=True)
    image.add_argument('--archive',type=Path,required=True);image.add_argument('--report',type=Path,required=True)
    image.add_argument('--ssh-config',type=Path,required=True)
    handoff=sub.add_parser('handoff',help='Read an operator receipt; no server changes')
    source=handoff.add_mutually_exclusive_group(required=True)
    source.add_argument('--receipt',type=Path);source.add_argument('--ssh-config',type=Path)
    handoff.add_argument('--expected-package-sha256',required=True)
    release=sub.add_parser('release');release.add_argument('--environment',choices=['dev','production'],required=True)
    release.add_argument('--ssh-config',type=Path,required=True);release.add_argument('--production-approved',action='store_true')
    release.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    if args.action=='verify-delivery':print(json.dumps(verify_delivery(args.archive,args.report)));return
    if args.action=='storage-check':
        delivery=verify_delivery(args.archive,args.report)
        # Fixed dev host/path, no server-supplied URL and no archive or secret upload.
        result,code=read_storage(args.ssh_config,delivery['import_required_bytes'])
        print(json.dumps({'delivery':delivery,'server_report':result}))
        raise SystemExit(0 if code==0 and result['capacity']['sufficient'] is True else 1)
    if args.action=='import-image':
        from import_client import import_image
        print(json.dumps(import_image(args.archive,args.report,args.ssh_config)))
        return
    if args.action=='handoff':
        if not re.fullmatch('[a-f0-9]{64}',args.expected_package_sha256):raise ReceiptError('handoff_expected_digest_invalid')
        raw=read_file(args.receipt) if args.receipt else read_remote(args.ssh_config)
        print(json.dumps(parse_receipt(raw,args.expected_package_sha256)));return
    if args.environment=='production' and args.command and args.command[0] in ['apply','resume','accept','recover','reopen-previous'] and not args.production_approved:
        raise ValueError('production_change_requires_explicit_release_approval')
    host,command=remote_command(args.environment,args.command)
    # No shell=True. The remote command is built from fixed argv and validated identifiers.
    p=subprocess.run(['ssh','-T','-F',str(args.ssh_config),host,command],check=False)
    raise SystemExit(p.returncode)

if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as ex:
        print(json.dumps({'ok':False,'reason':str(ex) if isinstance(ex,ValueError) else 'local_io_error'}));raise SystemExit(1)
