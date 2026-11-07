#!/usr/bin/env python3
"""Cross-platform client. Never transmits SSH private keys or app credentials."""
import argparse,hashlib,json,re,shlex,subprocess,sys,tarfile
from pathlib import Path,PurePosixPath
from handoff import ReceiptError,parse_receipt,read_file,read_remote

def verify_delivery(archive,report):
    data=json.loads(report.read_text(encoding='utf-8'))
    required=['source_sha','image_id','tag','archive_sha256','archive_bytes']
    if any(k not in data for k in required):raise ValueError('delivery_manifest_missing_fields')
    if not re.fullmatch('[a-f0-9]{40}',data['source_sha']) or not re.fullmatch('sha256:[a-f0-9]{64}',data['image_id']):raise ValueError('delivery_identity_invalid')
    if data['tag']!='platforma-gateway:evening-'+data['source_sha'][:12]:raise ValueError('delivery_tag_mismatch')
    if archive.stat().st_size!=data['archive_bytes'] or archive.stat().st_size>2*1024**3:raise ValueError('delivery_size_invalid')
    with archive.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
    if digest!=data['archive_sha256']:raise ValueError('delivery_archive_changed')
    if any(data.get(k) is not False for k in ['production_configuration_included','database_included','private_skill_mount_included']):
        raise ValueError('delivery_isolation_not_declared')
    with tarfile.open(archive,'r:gz') as tar:
        total=10240;seen=set()
        for item in tar:
            p=PurePosixPath(item.name);total+=512+((item.size+511)//512)*512
            if (p.is_absolute() or '..' in p.parts or '\\' in item.name or item.name in seen or item.issparse()
                    or not(item.isfile() or item.isdir()) or item.size<0 or total>4*1024**3 or len(seen)>=4096):
                raise ValueError('delivery_archive_structure_invalid')
            seen.add(item.name)
        member=tar.getmember('manifest.json')
        if member.size>65536:raise ValueError('delivery_manifest_oversized')
        value=json.load(tar.extractfile(member))
        if len(value)!=1 or value[0].get('RepoTags')!=[data['tag']]:raise ValueError('delivery_unexpected_tags')
        member=tar.getmember(value[0]['Config'])
        if member.size>2*1024**2:raise ValueError('delivery_config_oversized')
        raw=tar.extractfile(member).read();config=json.loads(raw)
        if 'sha256:'+hashlib.sha256(raw).hexdigest()!=data['image_id']:raise ValueError('delivery_config_digest_mismatch')
        if config['config'].get('User')!='10001:10001' or config['config'].get('Labels',{}).get('org.opencontainers.image.revision')!=data['source_sha']:
            raise ValueError('delivery_installed_source_mismatch')
    return {'verified':True,**{k:data[k] for k in required},'deployment_performed':False,
            'expanded_archive_bytes':total,'import_required_bytes':2*total+64*1024**2,
            'import_estimate':'two_uncompressed_copies_plus_metadata; reserve checked on host'}

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
