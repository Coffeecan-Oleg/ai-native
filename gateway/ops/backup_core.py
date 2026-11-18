"""Apply only the approved immutable overlay; run as root on the named sandbox."""
import argparse
from datetime import datetime, timezone
import hashlib,json,os,re,shutil,stat,subprocess,tarfile,time,urllib.request,urllib.error,zipfile
from pathlib import Path

BASE=Path('/home/student/platforma-sandbox')
GATEWAY='platforma-sandbox-gateway-1'
PG='platforma-sandbox-postgres-1'
PACKAGE_SHA='a9688cf97898db6df0273951796469e53875140231fa27a0c8bbe8d6f475b9f7'
BASE_IMAGE='sha256:a2407a981ed0778c33d7fecef8a1b477e07b678ba7564edfab1ae53b508df818'
TAG='platforma-sandbox:network-nonroot-v1'
ENV_KEYS={'GATEWAY_RELEASE_SHA'}

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def run(args,output=None):
    result=subprocess.run(args,cwd=BASE,stdout=output if output else subprocess.PIPE,
        stderr=subprocess.PIPE,text=output is None,timeout=240)
    if result.returncode:raise RuntimeError('subprocess_failed:'+args[0])
    return result.stdout if output is None else None

def inspect(name):return json.loads(run(['docker','inspect',name]))[0]

def private_write(path,text):
    temporary=path.with_name(path.name+'.new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as handle:
        handle.write(text);handle.flush();os.fsync(handle.fileno())
    os.replace(temporary,path);os.chmod(path,0o600)

def private_json(path,data):private_write(path,json.dumps(data,indent=2))

def compose(*args):return run(['docker','compose','-p','platforma-sandbox','-f',str(BASE/'compose.yaml'),*args])

def package_data(package):
    if digest(package)!=PACKAGE_SHA:raise RuntimeError('package_hash_mismatch')
    with zipfile.ZipFile(package) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        delta=json.loads(archive.read('runtime-env-delta.json'))['changes']
        files=[row for row in manifest['changed_files'] if row['runtime']]
        allowed={row['path'] for row in files}|{'Dockerfile','manifest.json','runtime-env-delta.json'}
        if set(archive.namelist())!=allowed or len(files)!=1:raise RuntimeError('package_layout_invalid')
        if manifest['base_image']!=BASE_IMAGE or manifest['candidate_tag']!=TAG:raise RuntimeError('package_target_invalid')
        if set(delta)!=ENV_KEYS:raise RuntimeError('env_delta_not_approved')
        if sum(info.file_size for info in archive.infolist())>5_000_000:raise RuntimeError('package_too_large')
        for info in archive.infolist():
            parts=Path(info.filename).parts
            if info.filename.startswith('/') or '..' in parts or stat.S_ISLNK(info.external_attr>>16):
                raise RuntimeError('package_member_invalid')
        for row in files:
            if hashlib.sha256(archive.read(row['path'])).hexdigest()!=row['after_sha256']:
                raise RuntimeError('package_file_hash_mismatch')
    return manifest,delta,files

def file_hashes(files):
    program="import hashlib,json;from pathlib import Path;root=Path('/usr/local/lib/python3.12/site-packages');names="+repr([r['path'] for r in files])+";print(json.dumps({n:hashlib.sha256((root/n).read_bytes()).hexdigest() if (root/n).is_file() else None for n in names}))"
    return json.loads(run(['docker','exec',GATEWAY,'python','-c',program]))

def prerequisite(manifest,delta,files):
    current=inspect(GATEWAY)
    if current['Image']!=BASE_IMAGE:raise RuntimeError('base_image_changed')
    if json.loads(run(['docker','image','inspect',manifest['base_tag']]))[0]['Id']!=BASE_IMAGE:
        raise RuntimeError('base_tag_changed')
    actual=file_hashes(files)
    if any(actual[r['path']]!=r['before_sha256'] for r in files):raise RuntimeError('base_source_changed')
    env=dict(row.split('=',1) for row in current['Config']['Env'] if '=' in row)
    if any(env.get(k)!=change['before'] for k,change in delta.items()):raise RuntimeError('base_config_changed')
    if env.get('GATEWAY_METRICS_ENABLED') or env.get('GATEWAY_METRICS_TOKEN_FILE'):
        raise RuntimeError('metrics_config_changed_requires_review')
    return current

def health(expected=None):
    for _ in range(35):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2) as response:data=json.load(response)
            if data.get('ok') and data.get('auth_enabled') and data.get('postgres',{}).get('ok'):
                if expected is None or data.get('release_sha')==expected:return data
        except Exception:pass
        time.sleep(.7)
    raise RuntimeError('gateway_health_timeout')

def snapshot(info):
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=BASE/'backups'/('network-nonroot-v1-'+stamp)
    backup.mkdir(mode=0o700)
    for name in ['.env','compose.yaml']:
        shutil.copy2(BASE/name,backup/name);os.chmod(backup/name,0o600)
    shutil.copytree(BASE/'runtime',backup/'runtime')
    for path in [backup/'runtime',*(backup/'runtime').rglob('*')]:
        os.chmod(path,0o700 if path.is_dir() else 0o600)
    pg=inspect(PG)
    run(['docker','stop','--time','20',GATEWAY])
    try:
        with (backup/'postgres.dump').open('wb') as output:
            run(['docker','exec',PG,'pg_dump','-U','gateway_mcp','-d','gateway_mcp','-Fc'],output)
        with (backup/'postgres.dump').open('rb') as source:
            result=subprocess.run(['docker','exec','-i',PG,'pg_restore','--list'],stdin=source,
                stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=40)
            if result.returncode:raise RuntimeError('backup_dump_verification_failed')
        volumes=[]
        for mount in info['Mounts']:
            if mount['Type']!='volume':continue
            target=backup/('volume-'+Path(mount['Destination']).name+'.tgz')
            with target.open('wb') as output:
                run(['docker','run','--rm','--network','none','--read-only','--memory','128m','--pids-limit','50',
                    '--mount','type=volume,source='+mount['Name']+',target=/source,readonly',
                    '--entrypoint','/bin/tar',info['Image'],'-czf','-','-C','/source','.'],output)
            with tarfile.open(target,'r:gz') as archive:
                for member in archive:pass
            volumes.append({'name':mount['Name'],'archive':target.name,'sha256':digest(target),'bytes':target.stat().st_size})
        metadata={'base_image':info['Image'],'base_tag':info['Config']['Image'],
            'pg_container_id':pg['Id'],'pg_started_at':pg['State']['StartedAt'],
            'config_sha256':{name:digest(backup/name) for name in ['.env','compose.yaml']},
            'dump_verified':True,'dump_sha256':digest(backup/'postgres.dump'),'volumes':volumes,
            'created_at':datetime.now(timezone.utc).isoformat()}
        private_json(backup/'backup.json',metadata)
        for path in backup.iterdir():
            if path.is_file():os.chmod(path,0o600)
    finally:
        run(['docker','start',GATEWAY])
    return backup,health()

def checked_backup(value):
    path=Path(value or '').resolve()
    if path.parent!=(BASE/'backups').resolve() or not path.name.startswith('network-nonroot-v1-'):
        raise RuntimeError('backup_target_invalid')
    metadata=json.loads((path/'backup.json').read_text())
    if metadata['base_image']!=BASE_IMAGE or not metadata['dump_verified']:raise RuntimeError('backup_metadata_invalid')
    if digest(path/'postgres.dump')!=metadata['dump_sha256']:raise RuntimeError('backup_dump_changed')
    for name,expected in metadata['config_sha256'].items():
        if digest(path/name)!=expected:raise RuntimeError('backup_config_changed')
    for volume in metadata['volumes']:
        if digest(path/volume['archive'])!=volume['sha256']:raise RuntimeError('backup_volume_changed')
    return path,metadata

def restore(backup):
    for name in ['.env','compose.yaml']:
        shutil.copy2(backup/name,BASE/name);os.chmod(BASE/name,0o600)
    compose('up','-d','--no-deps','gateway')
    return health()

def set_image():
    lines=[];inside=False;count=0
    for line in (BASE/'compose.yaml').read_text().splitlines():
        if re.fullmatch(r'  gateway:',line):inside=True
        elif inside and re.match(r'^  [A-Za-z][^ ]*:',line):inside=False
        if inside and re.match(r'^    image:',line):line='    image: '+TAG;count+=1
        lines.append(line)
    if count!=1:raise RuntimeError('gateway_image_not_unique')
    private_write(BASE/'compose.yaml','\n'.join(lines)+'\n')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['backup','apply','rollback'])
    parser.add_argument('--package');parser.add_argument('--backup');args=parser.parse_args()
    if os.geteuid()!=0 or Path.cwd().resolve()!=BASE:raise RuntimeError('wrong_execution_context')
    os.umask(0o077)
    import fcntl
    with (BASE/'.gateway-deploy.lock').open('a') as lock:
        os.chmod(BASE/'.gateway-deploy.lock',0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.phase=='rollback':
            backup,_=checked_backup(args.backup)
            print(json.dumps({'phase':'rollback','health':restore(backup),'database_restored':False}));return
        package=Path(args.package or '').resolve();manifest,delta,files=package_data(package)
        info=prerequisite(manifest,delta,files)
        if args.phase=='backup':
            backup,current=snapshot(info)
            print(json.dumps({'phase':'backup','backup':str(backup),'dump_verified':True,
                'volumes_verified':3,'gateway_restarted_with_previous_image':inspect(GATEWAY)['Image']==BASE_IMAGE,
                'health':current,'backup_mode':oct(stat.S_IMODE(backup.stat().st_mode))}));return
        backup,metadata=checked_backup(args.backup)
        if any(digest(BASE/name)!=expected for name,expected in metadata['config_sha256'].items()):
            raise RuntimeError('config_changed_after_backup')
        build=package.parent/('build-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        build.mkdir(mode=0o700)
        with zipfile.ZipFile(package) as archive:archive.extractall(build)
        run(['docker','build','--pull=false','--network=none','-t',TAG,str(build)])
        try:
            set_image()
            values={key:change['after'] for key,change in delta.items()}
            lines=[line for line in (BASE/'.env').read_text().splitlines() if line.split('=',1)[0].strip() not in values]
            lines.extend(k+'='+v for k,v in values.items())
            private_write(BASE/'.env','\n'.join(lines)+'\n')
            compose('up','-d','--no-deps','gateway')
            current=health(values['GATEWAY_RELEASE_SHA'])
            try:
                urllib.request.urlopen('http://127.0.0.1:8000/metrics',timeout=3)
                raise RuntimeError('anonymous_metrics_still_open')
            except urllib.error.HTTPError as error:
                if error.code!=404:raise RuntimeError('metrics_not_closed')
            hashes=file_hashes(files)
            if any(hashes[row['path']]!=row['after_sha256'] for row in files):raise RuntimeError('deployed_source_hash_mismatch')
            updated=inspect(GATEWAY)
            old_env=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)
            new_env=dict(v.split('=',1) for v in updated['Config']['Env'] if '=' in v)
            if {k:v for k,v in old_env.items() if k not in ENV_KEYS}!={k:v for k,v in new_env.items() if k not in ENV_KEYS}:
                raise RuntimeError('unexpected_environment_change')
            safety=['Memory','PidsLimit','ReadonlyRootfs','CapDrop','SecurityOpt','PortBindings','NetworkMode']
            if any(info['HostConfig'].get(k)!=updated['HostConfig'].get(k) for k in safety):
                raise RuntimeError('unexpected_container_security_change')
            old_mounts={(m['Type'],m.get('Name',''),m['Destination'],m['RW']) for m in info['Mounts']}
            new_mounts={(m['Type'],m.get('Name',''),m['Destination'],m['RW']) for m in updated['Mounts']}
            if old_mounts!=new_mounts:raise RuntimeError('unexpected_mount_change')
            pg=inspect(PG)
            if pg['Id']!=metadata['pg_container_id'] or pg['State']['StartedAt']!=metadata['pg_started_at']:
                raise RuntimeError('postgres_identity_changed')
        except Exception:
            restore(backup);raise
        report={'phase':'applied','backup':str(backup),'gateway_image':inspect(GATEWAY)['Image'],
            'gateway_tag':TAG,'runtime_hashes_verified':len(files),'health':current,
            'postgres_container_unchanged':True,'postgres_not_restarted':True,
            'migrations_added':0,'database_restored':False,'env_keys_changed':sorted(values),'authorization_environment_unchanged':True,'container_security_and_mounts_unchanged':True}
        private_json(backup/'application.json',report)
        print(json.dumps(report))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__}))
        raise SystemExit(1)
