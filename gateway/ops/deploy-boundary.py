"""Explicit reviewed phases; no infrastructure change when imported or reviewed."""
import argparse,fcntl,hashlib,importlib.util,json,os,re,shutil,stat,subprocess,sys,time,types,zipfile
from datetime import datetime,timezone
from pathlib import Path

BASE=Path('/home/student/platforma-sandbox')
GW='platforma-sandbox-gateway-1'
PG='platforma-sandbox-postgres-1'
BASE_IMAGE='sha256:a2407a981ed0778c33d7fecef8a1b477e07b678ba7564edfab1ae53b508df818'
UNIT='akmetron-gateway-boundary.service'
GUARD=Path('/usr/local/libexec/akmetron-gateway-boundary.py')
UNIT_PATH=Path('/etc/systemd/system')/UNIT
TIMER='akmetron-gateway-boundary-rollback'

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def run(args,check=True):
    p=subprocess.run(args,cwd=BASE,capture_output=True,text=True,timeout=90)
    if check and p.returncode:raise RuntimeError('command_failed:'+args[0])
    return p.stdout.strip()

def inspect(name):return json.loads(run(['docker','inspect',name]))[0]

def write(path,content,mode=0o600):
    temp=path.with_name(path.name+'.new')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
    with os.fdopen(fd,'wb') as target:target.write(content);target.flush();os.fsync(target.fileno())
    os.replace(temp,path);os.chmod(path,mode)

def save(path,value):write(path,json.dumps(value,indent=2).encode())

def load_package(path,expected):
    if digest(path)!=expected:raise RuntimeError('approved_package_hash_mismatch')
    archive=zipfile.ZipFile(path)
    manifest=json.loads(archive.read('manifest.json'))
    expected_names=set(manifest['files'])|{'manifest.json'}
    if set(archive.namelist())!=expected_names:raise RuntimeError('package_layout_changed')
    if sum(x.file_size for x in archive.infolist())>5_000_000:raise RuntimeError('package_size_invalid')
    for info in archive.infolist():
        if info.filename.startswith('/') or '..' in Path(info.filename).parts or stat.S_ISLNK(info.external_attr>>16):raise RuntimeError('package_path_invalid')
    for name,sha in manifest['files'].items():
        if hashlib.sha256(archive.read(name)).hexdigest()!=sha:raise RuntimeError('package_member_changed')
    if manifest['base_image']!=BASE_IMAGE:raise RuntimeError('package_base_mismatch')
    core=types.ModuleType('approved_backup_core')
    exec(compile(archive.read('ops/backup_core.py'),'approved_backup_core.py','exec'),core.__dict__)
    return manifest,archive,core

def safe_roots(info):
    expected={
      '/app/gateway-policy.json':BASE/'runtime/policy.json',
      '/app/gateway-addon-profiles.json':BASE/'runtime/profiles.json',
      '/opt/sandbox/documents.json':BASE/'runtime/documents.json',
      '/usr/local/lib/python3.12/site-packages/gateway_mcp/corporate_skills':BASE/'runtime/skill-packs/participant-04b74a71fb08'}
    volumes={'platforma-sandbox_knowledge','platforma-sandbox_audit','platforma-sandbox_requests'}
    roots=[]
    for m in info['Mounts']:
        p=Path(m['Source'])
        if m['Type']=='bind':
            if expected.get(m['Destination'])!=p or m['RW']:raise RuntimeError('bind_mount_changed')
        elif m['Type']=='volume':
            if m.get('Name') not in volumes or p!=Path('/var/lib/docker/volumes')/m['Name']/'_data' or not m['RW']:raise RuntimeError('volume_mount_changed')
        else:raise RuntimeError('unexpected_mount_type')
        if p.is_symlink() or p.resolve()!=p:raise RuntimeError('mount_source_symlink')
        roots.append({'path':str(p),'writable':m['RW']})
    if len(roots)!=7:raise RuntimeError('mount_count_changed')
    return roots

def metadata(roots):
    rows=[]
    for root in roots:
        base=Path(root['path'])
        entries=[base,*base.rglob('*')] if base.is_dir() else [base]
        for path in entries:
            info=path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or path.resolve()!=path:raise RuntimeError('unsupported_permission_target')
            rows.append({'path':str(path),'uid':info.st_uid,'gid':info.st_gid,'mode':stat.S_IMODE(info.st_mode),
                         'directory':path.is_dir(),'writable':root['writable'],'sha256':digest(path) if path.is_file() else None})
    return sorted(rows,key=lambda v:v['path'])

def change_permissions(rows):
    for row in rows:
        p=Path(row['path'])
        if p.is_symlink():raise RuntimeError('permission_target_became_symlink')
        owner=10001 if row['writable'] else 0
        mode=0o700 if row['writable'] and row['directory'] else 0o600 if row['writable'] else 0o750 if row['directory'] else 0o640
        os.chown(p,owner,10001);os.chmod(p,mode)

def restore_permissions(rows):
    for row in rows:
        p=Path(row['path'])
        if not p.exists():continue
        if p.is_symlink():raise RuntimeError('rollback_permission_symlink')
        os.chown(p,row['uid'],row['gid']);os.chmod(p,row['mode'])
    # New files created by the non-root service remain intact and readable by old root.

def render_compose(text,image):
    lines=[];inside=False;images=users=ports=0
    for line in text.splitlines():
        if re.fullmatch(r'  gateway:',line):inside=True
        elif inside and re.match(r'^  [A-Za-z][^ ]*:',line):inside=False
        if inside and re.match(r'^    image:',line):
            line='    image: '+image;images+=1
            lines.append(line);lines.append('    user: "10001:10001"');users+=1;continue
        if inside and re.match(r'^    user:',line):raise RuntimeError('existing_compose_user_requires_review')
        if inside and '0.0.0.0:8000:8000' in line:
            line=line.replace('0.0.0.0:8000:8000','127.0.0.1:8000:8000');ports+=1
        lines.append(line)
    if (images,users,ports)!=(1,1,1):raise RuntimeError('compose_shape_requires_review')
    return '\n'.join(lines)+'\n'

def config(path):
    return json.loads(run(['docker','compose','--project-directory',str(BASE),'--env-file',str(BASE/'.env'),'-p','platforma-sandbox','-f',str(path),'config','--format','json']))

def validate_compose(before,after,image):
    a=json.loads(json.dumps(before));b=json.loads(json.dumps(after))
    ag=a['services']['gateway'];bg=b['services']['gateway']
    if bg['image']!=image or bg.get('user')!='10001:10001':raise RuntimeError('candidate_compose_identity_invalid')
    if len(bg['ports'])!=1 or bg['ports'][0].get('host_ip')!='127.0.0.1':raise RuntimeError('candidate_port_invalid')
    old_ports=json.loads(json.dumps(ag['ports']));new_ports=json.loads(json.dumps(bg['ports']))
    for port in old_ports:port['host_ip']='127.0.0.1'
    if old_ports!=new_ports:raise RuntimeError('unapproved_port_change')
    for key in ['image','user','ports']:
        ag.pop(key,None);bg.pop(key,None)
    if a!=b:raise RuntimeError('unapproved_compose_change')

def checked_backup(value,core):
    backup,meta=core.checked_backup(value)
    extra=json.loads((backup/'boundary.json').read_text())
    if digest(backup/'permissions.json')!=extra['permissions_sha256']:raise RuntimeError('permission_backup_changed')
    return backup,meta,extra

def preflight(manifest,core):
    current=inspect(GW)
    if current['Image']!=BASE_IMAGE:raise RuntimeError('running_image_changed')
    image=json.loads(run(['docker','image','inspect',manifest['candidate_image']]))[0]
    if image['Id']!=manifest['candidate_image'] or image['Config']['User']!='10001:10001':raise RuntimeError('tested_image_not_available')
    if image['Config']['Entrypoint']!=['/usr/local/bin/gateway-private-entrypoint']:raise RuntimeError('candidate_entrypoint_changed')
    if GUARD.exists() or UNIT_PATH.exists():raise RuntimeError('existing_guard_requires_review')
    if GUARD.parent.exists():
        info=GUARD.parent.stat()
        if GUARD.parent.is_symlink() or info.st_uid!=0 or stat.S_IMODE(info.st_mode)&0o022:raise RuntimeError('unsafe_guard_directory')
    for binary in ['iptables','ip6tables']:
        p=subprocess.run([binary,'-t','raw','-S','AKME-GW-8000'],capture_output=True,text=True)
        if p.returncode==0:raise RuntimeError('guard_chain_already_exists')
    env=dict(v.split('=',1) for v in current['Config']['Env'] if '=' in v)
    if env.get('GATEWAY_RELEASE_SHA')!=manifest['release_before']:raise RuntimeError('release_changed')
    safe_roots(current)
    return current

def timer_off():run(['systemctl','stop',TIMER+'.timer'],check=False)

def rollback(backup,meta,extra,core,automatic=False):
    if automatic and ((backup/'committed.json').exists() or (backup/'rolled-back.json').exists()):return {'skipped':True}
    run(['docker','stop','--time','20',GW],check=False)
    for name in ['.env','compose.yaml']:
        shutil.copy2(backup/name,BASE/name);os.chmod(BASE/name,0o600)
    if GUARD.exists():
        if digest(GUARD)!=extra['guard_sha256']:raise RuntimeError('rollback_guard_changed')
        run(['python3',str(GUARD),'remove'])
    run(['systemctl','disable','--now',UNIT],check=False)
    for path,key in [(GUARD,'guard_sha256'),(UNIT_PATH,'unit_sha256')]:
        if path.exists():
            if digest(path)!=extra[key]:raise RuntimeError('rollback_installed_file_changed')
            path.unlink()
    run(['systemctl','daemon-reload'])
    restore_permissions(json.loads((backup/'permissions.json').read_text()))
    core.compose('up','-d','--no-deps','gateway')
    health=core.health()
    save(backup/'rolled-back.json',{'at':datetime.now(timezone.utc).isoformat(),'restored_image':inspect(GW)['Image']})
    timer_off()
    return {'phase':'rolled_back','health':health,'database_restored':False,'existing_file_contents_restored':False}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['preflight','backup','apply','commit','rollback'])
    parser.add_argument('--package',required=True);parser.add_argument('--package-sha',required=True)
    parser.add_argument('--backup');parser.add_argument('--automatic',action='store_true');args=parser.parse_args()
    if os.geteuid()!=0 or Path.cwd().resolve()!=BASE:raise RuntimeError('wrong_execution_context')
    os.umask(0o077)
    package=Path(args.package).resolve();manifest,archive,core=load_package(package,args.package_sha)
    with (BASE/'.gateway-deploy.lock').open('a') as lock:
        os.chmod(BASE/'.gateway-deploy.lock',0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        if args.phase in {'commit','rollback'}:
            backup,meta,extra=checked_backup(args.backup,core)
            if args.phase=='rollback':print(json.dumps(rollback(backup,meta,extra,core,args.automatic)));return
            if inspect(GW)['Image']!=manifest['candidate_image']:raise RuntimeError('commit_candidate_not_running')
            if run(['systemctl','is-active',UNIT])!='active':raise RuntimeError('guard_unit_inactive')
            core.health(manifest['release_after'])
            state=json.loads(run(['python3',str(GUARD),'status']))
            if not all(state['active'].values()):raise RuntimeError('guard_not_active')
            save(backup/'committed.json',{'at':datetime.now(timezone.utc).isoformat(),'image':manifest['candidate_image']})
            timer_off();print(json.dumps({'phase':'committed','automatic_rollback_cancelled':True}));return
        before=preflight(manifest,core)
        if args.phase=='preflight':print(json.dumps({'ok':True,'live_changes':False,'mounts':len(before['Mounts'])}));return
        if args.phase=='backup':
            rows=metadata(safe_roots(before))
            backup,health=core.snapshot(before)
            if metadata(safe_roots(inspect(GW)))!=rows:raise RuntimeError('files_changed_during_backup_repeat_required')
            save(backup/'permissions.json',rows)
            save(backup/'boundary.json',{'permissions_sha256':digest(backup/'permissions.json'),'guard_sha256':manifest['files']['ops/gateway-boundary.py'],'unit_sha256':manifest['files']['ops/gateway-boundary.service'],'package_sha256':args.package_sha})
            print(json.dumps({'phase':'backup','backup':str(backup),'health':health,'permission_entries':len(rows),'dump_verified':True,'volumes_verified':3}));return
        backup,meta,extra=checked_backup(args.backup,core)
        rows=json.loads((backup/'permissions.json').read_text())
        if metadata(safe_roots(before))!=rows:raise RuntimeError('files_changed_after_backup_repeat_required')
        candidate=backup/'candidate-compose.yaml'
        write(candidate,render_compose((BASE/'compose.yaml').read_text(),manifest['candidate_image']).encode())
        validate_compose(config(BASE/'compose.yaml'),config(candidate),manifest['candidate_image'])
        # A root-owned timer survives SSH loss. Explicit commit cancels it after external acceptance.
        run(['systemd-run','--unit='+TIMER,'--on-active=600s','--timer-property=AccuracySec=1s','--property=WorkingDirectory='+str(BASE),
             '/usr/bin/python3',str(Path(__file__).resolve()),'rollback','--automatic','--package',str(package),'--package-sha',args.package_sha,'--backup',str(backup)])
        try:
            run(['docker','stop','--time','20',GW])
            if metadata(safe_roots(before))!=rows:raise RuntimeError('file_change_before_permissions')
            change_permissions(rows)
            GUARD.parent.mkdir(mode=0o755,exist_ok=True)
            write(GUARD,archive.read('ops/gateway-boundary.py'))
            write(UNIT_PATH,archive.read('ops/gateway-boundary.service'),0o644)
            run(['systemctl','daemon-reload']);run(['systemctl','enable','--now',UNIT])
            shutil.copy2(candidate,BASE/'compose.yaml');os.chmod(BASE/'compose.yaml',0o600)
            values=[line for line in (BASE/'.env').read_text().splitlines() if line.split('=',1)[0].strip()!='GATEWAY_RELEASE_SHA']
            values.append('GATEWAY_RELEASE_SHA='+manifest['release_after']);write(BASE/'.env',('\n'.join(values)+'\n').encode())
            core.compose('up','-d','--no-deps','gateway')
            health=core.health(manifest['release_after']);after=inspect(GW)
            old_env=dict(v.split('=',1) for v in before['Config']['Env'] if '=' in v);new_env=dict(v.split('=',1) for v in after['Config']['Env'] if '=' in v)
            old_env.pop('GATEWAY_RELEASE_SHA',None);new_env.pop('GATEWAY_RELEASE_SHA',None)
            if old_env!=new_env:raise RuntimeError('unexpected_environment_change')
            if after['Image']!=manifest['candidate_image'] or after['Config']['User']!='10001:10001':raise RuntimeError('process_identity_invalid')
            if run(['docker','exec',GW,'id','-u'])!='10001':raise RuntimeError('effective_uid_invalid')
            expected_ports={'8000/tcp':[{'HostIp':'127.0.0.1','HostPort':'8000'}]}
            if after['HostConfig']['PortBindings']!=expected_ports:raise RuntimeError('port_binding_invalid')
            for key in ['Memory','PidsLimit','ReadonlyRootfs','CapDrop','SecurityOpt','NetworkMode','Tmpfs']:
                if before['HostConfig'].get(key)!=after['HostConfig'].get(key):raise RuntimeError('unexpected_security_change')
            if sorted(before['Mounts'],key=lambda m:m['Destination'])!=sorted(after['Mounts'],key=lambda m:m['Destination']):raise RuntimeError('mounts_changed')
            pg=inspect(PG)
            if pg['Id']!=meta['pg_container_id'] or pg['State']['StartedAt']!=meta['pg_started_at']:raise RuntimeError('postgres_restarted')
        except Exception:
            rollback(backup,meta,extra,core);raise
        result={'phase':'applied_pending_external_acceptance','backup':str(backup),'image':after['Image'],'effective_uid':10001,'port_bind':'127.0.0.1:8000','guard_enabled':True,'automatic_rollback_seconds':600,
                'postgres_unchanged':True,'authorization_environment_unchanged':True,'mounts_unchanged':True,'migrations_added':0,'health':health}
        save(backup/'application.json',result);print(json.dumps(result))

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'ok':False,'error':str(error) if isinstance(error,RuntimeError) else type(error).__name__}));raise SystemExit(1)
