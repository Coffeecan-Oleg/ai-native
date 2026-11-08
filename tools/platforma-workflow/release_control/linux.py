"""Scoped Linux Docker/Compose/Caddy adapter. Root-owned configuration only."""
import hashlib,json,os,re,shutil,stat,subprocess,tarfile,time,urllib.request,urllib.error
from urllib.parse import urlsplit
from pathlib import Path,PurePosixPath
from .core import Refused,atomic,read,sha
from .storage import require as require_storage

ENV={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/root','LANG':'C.UTF-8',
     'DOCKER_HOST':'unix:///var/run/docker.sock'}
DOCKER='/usr/bin/docker'
def validate_development_scope(cfg):
    if cfg.get('mode')!='development':raise Refused('development_mode_required')
    if cfg.get('project')!='platformadev' or cfg.get('database')!='gateway_mcp_dev' or cfg.get('db_user')!='platformadev':
        raise Refused('development_scope_mismatch')
    if cfg.get('gateway_service')!='gateway' or cfg.get('postgres_service')!='postgres':raise Refused('development_service_scope')
    if cfg.get('worker_service')!='platforma-dev-release-worker.service':raise Refused('development_worker_scope')
    if cfg.get('caddy',{}).get('container')!='platformadev-caddy-1':raise Refused('development_edge_scope')
def trusted(path,directory=False):
    p=Path(path)
    if not p.is_absolute() or p.is_symlink() or p.resolve()!=p:raise Refused('untrusted_path')
    s=p.stat()
    if s.st_uid!=0 or s.st_mode&0o022 or (directory and not p.is_dir()):raise Refused('untrusted_owner_or_mode')
    for parent in p.parents:
        info=parent.stat()
        if info.st_uid!=0 or (info.st_mode&0o022 and not (info.st_mode&stat.S_ISVTX)):
            raise Refused('untrusted_parent')
    return p
def file_sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write_bytes(path,data,mode=0o600):
    p=Path(path)
    if p.is_symlink():raise Refused('file_symlink')
    previous=p.stat() if p.exists() else None
    if previous:
        if not stat.S_ISREG(previous.st_mode):raise Refused('file_not_regular')
        mode=stat.S_IMODE(previous.st_mode)
    temp=p.with_name(p.name+'.release-new')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
    try:
        if previous:os.fchown(fd,previous.st_uid,previous.st_gid)
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temp,p)
        d=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(d)
        finally:os.close(d)
    finally:
        if temp.exists():temp.unlink()

class Linux:
    def __init__(self,cfg):
        if os.geteuid()!=0:raise Refused('root_operator_service_required')
        self.c=cfg
        if cfg['schema']!=1 or cfg['mode'] not in ['production','laboratory','development']:raise Refused('invalid_configuration')
        if cfg['mode']=='development':validate_development_scope(cfg)
        for key in ['project','gateway_service','postgres_service','database','db_user']:
            if not re.fullmatch('[a-z][a-z0-9_]{1,62}',cfg[key]):raise Refused('invalid_scope')
        if cfg['mode']=='production' and (cfg['project']!='platforma' or cfg['database']!='gateway_mcp'):
            raise Refused('production_scope_mismatch')
        if cfg['mode']=='laboratory' and not re.fullmatch('prlab[a-f0-9]{10}',cfg['project']):
            raise Refused('laboratory_scope_mismatch')
        unit=cfg.get('worker_service','platforma-release-worker.service')
        expected={'production':'platforma-release-worker.service','development':'platforma-dev-release-worker.service'}.get(cfg['mode'],cfg['project']+'-release.service')
        if unit!=expected:raise Refused('worker_unit_scope')
        self.base=trusted(cfg['project_dir'],True)
        self.compose=trusted(cfg['compose_file'])
        self.env=trusted(cfg['env_file'])
        if self.env.stat().st_mode&0o077:raise Refused('env_permissions')
        if self.compose.parent!=self.base or self.env.parent!=self.base:raise Refused('project_paths_outside_scope')
        self.state=trusted(cfg['state_dir'],True)
        self.caddy=cfg['caddy'];trusted(self.caddy['vhost_file']);trusted(self.caddy['main_config'])
        if cfg['sole_application_writer']!='gateway_operator_confirmed':raise Refused('writers_not_confirmed')
        if not self.caddy['neighbours']:raise Refused('neighbour_probes_required')
        health_url=urlsplit(cfg['gateway_health_url'])
        if health_url.scheme!='http' or health_url.hostname!='127.0.0.1' or health_url.path!='/healthz' or health_url.query or health_url.username:
            raise Refused('health_probe_must_be_loopback')
        for probe in [self.caddy['probe'],*self.caddy['neighbours']]:
            url=urlsplit(probe['url'])
            if url.username or url.query or url.fragment or set(probe.get('headers',{}))-{'Host'}:
                raise Refused('probe_credentials_or_headers_denied')
            if cfg['mode'] in ['laboratory','development']:
                if url.hostname!='127.0.0.1' or url.scheme!='http':raise Refused('laboratory_probe_scope')
            elif url.scheme!='https' or probe.get('resolve')!=url.hostname+':443:127.0.0.1':
                raise Refused('production_probe_must_resolve_to_loopback')
    def run(self,args,*,input=None,timeout=45,output_file=None):
        try:
            if output_file:
                with Path(output_file).open('xb') as f:
                    os.chmod(output_file,0o600)
                    p=subprocess.run(args,input=input,stdout=f,stderr=subprocess.PIPE,env=ENV,timeout=timeout)
            else:p=subprocess.run(args,input=input,capture_output=True,env=ENV,timeout=timeout)
        except (OSError,subprocess.TimeoutExpired):raise Refused('command_timeout_or_unavailable')
        if p.returncode:raise Refused('command_failed')
        if output_file:return b''
        if len(p.stdout)>4*1024**2:raise Refused('command_output_limit')
        return p.stdout
    def docker(self,*args,**kw):return self.run([DOCKER,*args],**kw)
    def object(self,name):return json.loads(self.docker('inspect',name))[0]
    def container(self,service):
        # Compose ps can also return run --rm migration jobs for the same
        # service. Resolve the single ordinary container explicitly instead.
        names=self.docker('ps','--all','--quiet','--filter','label=com.docker.compose.project='+self.c['project'],
                         '--filter','label=com.docker.compose.service='+service).decode().split()
        matches=[]
        for name in names:
            if not re.fullmatch('[a-f0-9]{12,64}',name):raise Refused('invalid_container_id')
            obj=self.object(name);labels=obj['Config'].get('Labels') or {}
            if labels.get('com.docker.compose.project')!=self.c['project'] or labels.get('com.docker.compose.service')!=service:
                raise Refused('container_scope_mismatch')
            oneoff=labels.get('com.docker.compose.oneoff')
            if oneoff in ['False','false']:matches.append(obj)
            elif oneoff not in ['True','true']:raise Refused('container_oneoff_label_missing')
        if len(matches)!=1:raise Refused('service_missing_or_ambiguous')
        return matches[0]
    def compose_run(self,*args,file=None,**kw):
        return self.docker('compose','--project-directory',str(self.base),'--env-file',str(self.env),
            '-p',self.c['project'],'-f',str(file or self.compose),*args,**kw)
    def resolved(self,file=None):
        return json.loads(self.compose_run('config','--format','json',file=file))
    def image(self,ref):
        obj=json.loads(self.docker('image','inspect',ref))[0]
        if not re.fullmatch('sha256:[a-f0-9]{64}',obj['Id']):raise Refused('invalid_image_id')
        return obj
    def inventory(self,image):
        """No production env, mounts or network reach an unstarted candidate."""
        value=json.loads(self.docker('run','--rm','--network','none','--read-only',
            '--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges:true',
            '--memory','128m','--pids-limit','64','--label','com.docker.compose.project='+self.c['project'],
            '--tmpfs','/tmp:rw,nosuid,nodev,size=16m','--entrypoint','gateway-mcp',
            image,'migrate','status',timeout=60))
        pending=value.get('pending')
        if value.get('database_enabled') or not isinstance(pending,list) or not all(re.fullmatch(r'\d{4}_[a-z0-9_]+',s) for s in pending):
            raise Refused('image_inventory_invalid')
        return pending
    def sql(self,query,database=None):
        pg=self.container(self.c['postgres_service'])
        return self.docker('exec',pg['Id'],'psql','-X','-A','-t','-v','ON_ERROR_STOP=1',
                           '-U',self.c['db_user'],'-d',database or self.c['database'],'-c',query).decode().strip()
    def versions(self):
        return json.loads(self.sql("select coalesce(json_agg(version order by version),'[]'::json) from schema_migrations"))
    def health(self):
        try:
            status,body=self.direct_http('/healthz');data=json.loads(body)
            return {'ok':status==200 and data.get('ok') is True and data.get('auth_enabled') is True
                    and data.get('postgres',{}).get('enabled') is True and data['postgres'].get('ok') is True,
                    'release_sha':str(data.get('release_sha',''))[:512]}
        except Exception:return {'ok':False,'release_sha':''}
    def direct_http(self,path):
        if path not in ['/healthz','/.well-known/oauth-authorization-server','/mcp','/metrics']:
            raise Refused('http_probe_path_denied')
        program="""import base64,json,sys,urllib.request,urllib.error
try:
 r=urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:'+sys.argv[1]+sys.argv[2],timeout=4)
 status=r.status;body=r.read(65536)
except urllib.error.HTTPError as e:
 status=e.code;body=b''
print(json.dumps({'status':status,'body':base64.b64encode(body).decode()}))
"""
        gw=self.container(self.c['gateway_service'])
        value=json.loads(self.docker('exec',gw['Id'],'python','-I','-c',program,
                            str(urlsplit(self.c['gateway_health_url']).port or 8000),path,timeout=8))
        import base64
        return value['status'],base64.b64decode(value['body'])
    def probe(self,spec):
        if self.caddy.get('container'):
            obj=self.object(self.caddy['container'])
            if self.c['mode']=='development' and (obj['Config'].get('Labels') or {}).get('com.docker.compose.project')!='platformadev':
                raise Refused('development_edge_scope')
            if self.c['mode']=='laboratory' and (obj['Config'].get('Labels') or {}).get('platforma.release.lab')!=self.c['project']:
                raise Refused('caddy_scope_mismatch')
            args=[DOCKER,'exec',self.caddy['container'],'wget','-S','-O','-']
            for k,v in spec.get('headers',{}).items():args+=['--header='+k+': '+v]
            args+=[spec['url']]
            p=subprocess.run(args,capture_output=True,env=ENV,timeout=10)
            statuses=re.findall(rb'HTTP/[0-9.]+\s+([0-9]{3})',p.stderr)
            if not statuses or len(p.stdout)>1024**2:raise Refused('caddy_probe_failed')
            return {'status':int(statuses[-1]),'body_sha256':sha(p.stdout)}
        args=['/usr/bin/curl','--silent','--show-error','--noproxy','*','--max-time','8','--write-out','\n%{http_code}']
        if spec.get('resolve'):args+=['--resolve',spec['resolve']]
        for k,v in spec.get('headers',{}).items():args+=['--header',k+': '+v]
        args+=[spec['url']]
        raw=self.run(args)
        try:body,code=raw.rsplit(b'\n',1);status=int(code)
        except Exception:raise Refused('invalid_probe_response')
        return {'status':status,'body_sha256':sha(body)}
    def neighbours(self):
        return [self.probe(s) for s in self.caddy['neighbours']]
    def caddy_command(self,verb):
        args=['caddy',verb,'--config',self.caddy['container_config'],'--adapter','caddyfile']
        if self.caddy.get('container'):
            obj=self.object(self.caddy['container'])
            if self.c['mode']=='development' and (obj['Config'].get('Labels') or {}).get('com.docker.compose.project')!='platformadev':
                raise Refused('development_edge_scope')
            if self.c['mode']=='laboratory' and (obj['Config'].get('Labels') or {}).get('platforma.release.lab')!=self.c['project']:
                raise Refused('caddy_scope_mismatch')
            return self.docker('exec',self.caddy['container'],*args)
        binary=trusted(self.caddy['binary'])
        return self.run([str(binary),verb,'--config',self.caddy['main_config'],'--adapter','caddyfile'])
    def opdir(self,op):
        if not re.fullmatch('op-[a-f0-9]{32}',op['id']):raise Refused('invalid_id')
        p=self.state/'artifacts'/op['id']
        p.parent.mkdir(mode=0o700,exist_ok=True);p.mkdir(mode=0o700,exist_ok=True)
        return trusted(p,True)
    def inspect(self,tag):
        marker=self.state/'image-import-active.json'
        if self.c.get('mode')=='development' and (marker.exists() or marker.is_symlink()):
            raise Refused('image_import_unresolved')
        # Rechecked at plan/apply/worker, before an inventory container or window.
        # Do not add changing counters to the immutable baseline view.
        for location in [self.base,self.state]:require_storage(location,self.c['minimum_disk_bytes'])
        current=self.container(self.c['gateway_service']);pg=self.container(self.c['postgres_service'])
        resolved=self.resolved();gw=resolved['services'][self.c['gateway_service']]
        if gw.get('user')!='10001:10001' or not gw.get('read_only') or gw.get('privileged') or gw.get('network_mode')=='host':
            raise Refused('gateway_sandbox_contract')
        if gw.get('entrypoint'):raise Refused('custom_gateway_entrypoint')
        if 'ALL' not in gw.get('cap_drop',[]) or 'no-new-privileges:true' not in gw.get('security_opt',[]):
            raise Refused('gateway_security_contract')
        if self.c['mode']=='production':
            env=gw.get('environment',{})
            if env.get('GATEWAY_AUTH_ENABLED')!='true' or env.get('GATEWAY_LOGIN_PROVIDER')!='bitrix24':
                raise Refused('production_auth_contract')
        for m in gw.get('volumes',[]):
            target=m.get('target','')
            if target in ['/','/app','/etc','/usr/local','/var/run/docker.sock','/run/docker.sock'] or target.startswith(('/proc','/sys')):
                raise Refused('unsafe_gateway_mount')
        im=self.image('platforma-gateway:'+tag);ic=im['Config'];revision=(ic.get('Labels') or {}).get('org.opencontainers.image.revision','')
        if ic.get('User')!='10001:10001' or not re.fullmatch('[a-f0-9]{40}',revision):raise Refused('candidate_identity_contract')
        if ic.get('Entrypoint')!=['/usr/local/bin/gateway-private-entrypoint']:raise Refused('candidate_entrypoint_contract')
        versions=self.versions();health=self.health()
        old_status=json.loads(self.docker('exec',current['Id'],'gateway-mcp','migrate','status',timeout=60))
        if not isinstance(old_status.get('pending'),list):raise Refused('baseline_migration_status_invalid')
        inventory=self.inventory(im['Id'])
        if not set(versions)<=set(inventory):raise Refused('candidate_schema_older')
        pending=sorted(set(inventory)-set(versions))
        original_sha=file_sha(self.caddy['vhost_file'])
        return {'current':{'image_id':current['Image'],'configured_image':current['Config']['Image'],
                          'container_id':current['Id'],'healthy':health['ok'],'release_sha':health['release_sha']},
                'candidate':{'image_id':im['Id'],'revision':revision,'user':'10001:10001','tag':'platforma-gateway:'+tag},
                'postgres_id':pg['Id'],'postgres_started':pg['State']['StartedAt'],
                'runtime_contract':{k:current['HostConfig'].get(k) for k in
                     ['Memory','PidsLimit','ReadonlyRootfs','CapDrop','SecurityOpt','NetworkMode','Tmpfs','PortBindings','Privileged']},
                'applied':versions,'pending_before':old_status['pending'],'pending_candidate':pending,
                'fingerprints':{'compose':file_sha(self.compose),'env':file_sha(self.env),
                                'resolved':sha(json.dumps(resolved,sort_keys=True).encode()),
                                'caddy_vhost':original_sha,'caddy_main':file_sha(self.caddy['main_config'])},
                'neighbours':self.neighbours()}
    def render(self,source,destination,image):
        if not re.fullmatch('sha256:[a-f0-9]{64}',image):raise Refused('unpinned_image')
        lines=Path(source).read_text().splitlines(keepends=True);inside=False;changed=0;out=[]
        service=self.c['gateway_service']
        for line in lines:
            if re.fullmatch(r'  '+service+r':\s*',line.strip('\r\n')):inside=True
            elif re.match(r'^  [A-Za-z0-9_-]+:',line):inside=False
            if inside and re.match(r'^    image:',line):line='    image: '+image+'\n';changed+=1
            out.append(line)
        if changed!=1:raise Refused('compose_shape_requires_operator_review')
        write_bytes(destination,''.join(out).encode())
        before=self.resolved(source);after=self.resolved(destination)
        before['services'][service]['image']=image
        if before!=after:raise Refused('unexpected_compose_difference')
    def schedule(self):
        self.run(['/usr/bin/systemctl','start','--no-block',self.c.get('worker_service','platforma-release-worker.service')])
    def close_gate(self,op):
        folder=self.opdir(op);original=folder/'platforma.caddy.original'
        target=trusted(self.caddy['vhost_file'])
        if not original.exists():
            if file_sha(target)!=op['plan']['view']['fingerprints']['caddy_vhost']:raise Refused('caddy_changed')
            write_bytes(original,target.read_bytes())
        if self.neighbours()!=op['plan']['view']['neighbours']:raise Refused('neighbour_changed_before_window')
        try:
            write_bytes(target,self.caddy['maintenance_text'].encode())
            self.caddy_command('validate');self.caddy_command('reload')
            self.assert_closed(op)
        except Exception:
            # Closing precedes stopping the old app. Never leave a rejected file.
            write_bytes(target,original.read_bytes())
            self.caddy_command('validate');self.caddy_command('reload')
            raise Refused('maintenance_not_applied')
    def assert_closed(self,op):
        if self.probe(self.caddy['probe'])['status']!=503:raise Refused('maintenance_unconfirmed')
        if self.neighbours()!=op['plan']['view']['neighbours']:raise Refused('neighbour_changed')
    def ensure_closed(self,op):
        if not (self.opdir(op)/'platforma.caddy.original').exists():self.close_gate(op);return
        self.close_gate(op)
    def stop_writers(self,op):
        self.assert_closed(op)
        current=self.container(self.c['gateway_service'])
        self.docker('stop','--time','20',current['Id'])
        # A daemon-managed migration can survive the SSH/docker client. Stop only
        # the exact named jobs belonging to this persisted operation.
        for kind in ['migrate','migration-status']:
            name=self.c['project']+'-'+kind+'-'+op['id'][-12:]
            try:job=self.object(name)
            except Refused:continue
            labels=job['Config'].get('Labels') or {}
            if labels.get('com.docker.compose.project')!=self.c['project'] or labels.get('com.docker.compose.service')!=self.c['gateway_service']:
                raise Refused('migration_job_scope_mismatch')
            if job['State']['Running']:self.docker('stop','--time','20',job['Id'])
        n=int(self.sql("select count(*) from pg_stat_activity where datname=current_database() and pid<>pg_backend_pid()"))
        if n:raise Refused('database_still_has_clients')
    def volume_root(self,name):
        if name not in self.c['volume_names']:raise Refused('volume_outside_scope')
        value=json.loads(self.docker('volume','inspect',name))[0]
        if (value.get('Labels') or {}).get('com.docker.compose.project')!=self.c['project']:raise Refused('volume_scope_mismatch')
        p=Path(value['Mountpoint'])
        if p.is_symlink() or p.resolve()!=p or not p.is_dir():raise Refused('volume_path_invalid')
        return p
    def tar_volume(self,name,path):
        root=self.volume_root(name);total=0;count=0
        with tarfile.open(path,'w:gz',dereference=False) as tar:
            for p in sorted(root.rglob('*')):
                st=p.lstat()
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):raise Refused('unsupported_volume_entry')
                count+=1;total+=st.st_size if p.is_file() else 0
                if count>20000 or total>self.c['maximum_volume_bytes']:raise Refused('volume_snapshot_limit')
                tar.add(p,arcname=str(p.relative_to(root)),recursive=False)
        os.chmod(path,0o600)
        self.validate_tar(path)
    def validate_tar(self,path):
        with tarfile.open(path,'r:gz') as tar:
            members=tar.getmembers();total=0;seen=set()
            if len(members)>20000:raise Refused('snapshot_entry_limit')
            for m in members:
                p=PurePosixPath(m.name);total+=m.size
                if p.is_absolute() or '..' in p.parts or '\\' in m.name or m.name in seen or m.mode&~0o777 or m.uid not in [0,10001] or m.gid not in [0,10001] or not(m.isfile() or m.isdir()) or total>self.c['maximum_volume_bytes']:
                    raise Refused('unsafe_snapshot_entry')
                seen.add(m.name)
    def dump(self,path):
        pg=self.container(self.c['postgres_service'])
        self.docker('exec',pg['Id'],'pg_dump','-Fc','-U',self.c['db_user'],'-d',self.c['database'],
                    timeout=180,output_file=path)
        # Validate the archive catalogue with the same PostgreSQL binary.
        with Path(path).open('rb') as f:
            p=subprocess.run([DOCKER,'exec','-i',pg['Id'],'pg_restore','-l'],stdin=f,stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE,env=ENV,timeout=90)
        if p.returncode:raise Refused('dump_catalogue_invalid')
    def snapshot(self,op):
        self.assert_closed(op)
        self.assert_inputs(op)
        folder=self.opdir(op);dest=folder/'snapshot';dest.mkdir(mode=0o700)
        self.dump(dest/'database.dump')
        files={'database.dump':file_sha(dest/'database.dump')};roots={}
        for name in self.c['volume_names']:
            info=self.volume_root(name).stat()
            if info.st_uid not in [0,10001] or info.st_gid not in [0,10001] or info.st_mode&0o7000:
                raise Refused('unsupported_volume_root_metadata')
            roots[name]={'uid':info.st_uid,'gid':info.st_gid,'mode':stat.S_IMODE(info.st_mode)}
            target=dest/(name+'.tar.gz');self.tar_volume(name,target);files[target.name]=file_sha(target)
        write_bytes(dest/'compose.original',self.compose.read_bytes());files['compose.original']=file_sha(dest/'compose.original')
        write_bytes(dest/'env.original',self.env.read_bytes());files['env.original']=file_sha(dest/'env.original')
        result={'directory':str(dest),'files':files,'volume_roots':roots,'applied':self.versions()}
        atomic(dest/'snapshot.json',result)
        return result
    def candidate_compose(self,op):
        p=self.opdir(op)/'candidate.yaml'
        if not p.exists():self.render(self.compose,p,op['plan']['view']['candidate']['image_id'])
        return p
    def migrate(self,op):
        self.assert_closed(op)
        self.assert_inputs(op)
        self.compose_run('run','--rm','--no-deps','--name',self.c['project']+'-migrate-'+op['id'][-12:],
                         '--entrypoint','gateway-mcp',self.c['gateway_service'],
                         'migrate','up',file=self.candidate_compose(op),timeout=180)
        pending=json.loads(self.compose_run('run','--rm','--no-deps','--name',self.c['project']+'-migration-status-'+op['id'][-12:],
                            '--entrypoint','gateway-mcp',self.c['gateway_service'],
                            'migrate','status',file=self.candidate_compose(op),timeout=60))
        if pending.get('pending'):raise Refused('pending_migrations_remain')
    def start_candidate(self,op):
        self.assert_inputs(op)
        write_bytes(self.compose,self.candidate_compose(op).read_bytes())
        self.compose_run('up','-d','--no-deps',self.c['gateway_service'],timeout=120)
    def check_candidate(self,op):
        self.assert_closed(op)
        self.assert_inputs(op,candidate=True)
        end=time.monotonic()+self.c['health_timeout_seconds']
        while time.monotonic()<end:
            obj=self.container(self.c['gateway_service']);h=self.health()
            if h['ok'] and h['release_sha']==op['plan']['view']['candidate']['revision']:
                if obj['Config']['Image']!=op['plan']['view']['candidate']['image_id']:raise Refused('running_image_mismatch')
                if obj['Config'].get('User')!='10001:10001' or obj['State'].get('OOMKilled'):raise Refused('runtime_identity_or_oom')
                for k,v in op['plan']['view']['runtime_contract'].items():
                    if obj['HostConfig'].get(k)!=v:raise Refused('runtime_contract_changed')
                pg=self.container(self.c['postgres_service'])
                if pg['Id']!=op['plan']['view']['postgres_id'] or pg['State']['StartedAt']!=op['plan']['view']['postgres_started']:
                    raise Refused('postgres_restarted')
                if self.versions()!=sorted(op['plan']['view']['applied']+op['plan']['view']['pending_candidate']):
                    raise Refused('schema_mismatch')
                status,body=self.direct_http('/.well-known/oauth-authorization-server')
                if status!=200:raise Refused('oauth_metadata_unavailable')
                metadata=json.loads(body)
                expected=self.c['expected_public_url'].rstrip('/')
                if metadata.get('issuer')!=expected or metadata.get('authorization_endpoint')!=expected+'/oauth/authorize' or metadata.get('token_endpoint')!=expected+'/oauth/token':
                    raise Refused('oauth_metadata_changed')
                if self.direct_http('/mcp')[0]!=401:raise Refused('anonymous_mcp_not_denied')
                if self.direct_http('/metrics')[0] not in [401,404]:raise Refused('metrics_not_closed')
                return
            time.sleep(1)
        raise Refused('candidate_health_timeout')
    def assert_inputs(self,op,candidate=False):
        expected=op['plan']['view']['fingerprints']
        if file_sha(self.env)!=expected['env'] or file_sha(self.caddy['main_config'])!=expected['caddy_main']:
            raise Refused('configuration_changed_inside_window')
        compose_sha=file_sha(self.candidate_compose(op)) if candidate else expected['compose']
        if file_sha(self.compose)!=compose_sha:raise Refused('compose_changed_inside_window')
    def open_gate(self,op,previous=False):
        self.assert_closed(op)
        original=self.opdir(op)/'platforma.caddy.original'
        if file_sha(original)!=op['plan']['view']['fingerprints']['caddy_vhost']:raise Refused('original_caddy_changed')
        write_bytes(self.caddy['vhost_file'],original.read_bytes())
        self.caddy_command('validate');self.caddy_command('reload')
        if self.probe(self.caddy['probe'])['status']!=200 or self.neighbours()!=op['plan']['view']['neighbours']:
            raise Refused('opening_probe_failed')
    def snapshot_failed_state(self,op):
        folder=self.opdir(op)/('failed-'+str(time.time_ns()));folder.mkdir(mode=0o700)
        self.dump(folder/'database.dump')
        for name in self.c['volume_names']:self.tar_volume(name,folder/(name+'.tar.gz'))
    def restore(self,op):
        snap=op['snapshot'];folder=trusted(snap['directory'],True)
        if folder!=self.opdir(op)/'snapshot':raise Refused('snapshot_scope')
        for name,digest in snap['files'].items():
            p=folder/name
            if p.parent!=folder or p.is_symlink() or file_sha(p)!=digest:raise Refused('snapshot_changed')
        pg=self.container(self.c['postgres_service'])
        database=self.c['database'];user=self.c['db_user']
        self.sql('DROP DATABASE "'+database+'" WITH (FORCE)',database='postgres')
        self.sql('CREATE DATABASE "'+database+'" OWNER "'+user+'"',database='postgres')
        with (folder/'database.dump').open('rb') as f:
            p=subprocess.run([DOCKER,'exec','-i',pg['Id'],'pg_restore','--exit-on-error','--single-transaction',
                              '-U',user,'-d',database],stdin=f,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,env=ENV,timeout=180)
        if p.returncode:raise Refused('restore_failed')
        for name in self.c['volume_names']:
            path=folder/(name+'.tar.gz');self.validate_tar(path);root=self.volume_root(name)
            # Only a stopped, scope-checked Docker volume is replaced.
            for child in root.iterdir():
                if child.is_symlink() or not(child.is_file() or child.is_dir()):raise Refused('unsafe_restore_target')
                if child.is_dir():shutil.rmtree(child)
                else:child.unlink()
            with tarfile.open(path,'r:gz') as tar:tar.extractall(root,filter='fully_trusted')
            metadata=snap['volume_roots'][name]
            os.chown(root,metadata['uid'],metadata['gid']);os.chmod(root,metadata['mode'])
        write_bytes(self.env,(folder/'env.original').read_bytes())
        self.render(folder/'compose.original',self.compose,op['plan']['view']['current']['image_id'])
        self.compose_run('up','-d','--no-deps',self.c['gateway_service'],timeout=120)
    def start_previous(self,op):
        self.compose_run('up','-d','--no-deps',self.c['gateway_service'],timeout=120)
    def check_previous(self,op):
        self.assert_closed(op);end=time.monotonic()+self.c['health_timeout_seconds']
        while time.monotonic()<end:
            h=self.health()
            if h['ok'] and h['release_sha']==op['plan']['view']['current']['release_sha']:
                if self.versions()!=op['plan']['view']['applied']:raise Refused('restored_schema_mismatch')
                return
            time.sleep(1)
        raise Refused('previous_health_timeout')
