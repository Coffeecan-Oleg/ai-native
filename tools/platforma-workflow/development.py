"""Reproducible fresh dev environment. No production data or credentials."""
import contextlib,hashlib,json,os,re,secrets,shutil,socket,subprocess,time
from base64 import urlsafe_b64encode
from pathlib import Path
from release_control.core import Controller,Store,Refused,atomic,encoded,read,sha
from release_control.linux import Linux,ENV,trusted,write_bytes

PROJECT='platformadev'
PUBLIC_URL='https://platforma-dev.akmetron.ru'
UNIT='platforma-dev-release-worker.service'
CADDY='caddy@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d'
GATEWAY='platforma-gateway:evening-4b53b6d9ede2'
REVISION='4b53b6d9ede27db92daffc2bfc0f7c1e84385e55'
IMAGE_ID='sha256:deb93eaa6c50e01722b77c71d816f68ed7e3ef1a569f87bde8ce741fb7e14578'

def run(args,**kwargs):
    try:p=subprocess.run(args,capture_output=True,env=ENV,timeout=kwargs.pop('timeout',45),**kwargs)
    except (OSError,subprocess.TimeoutExpired):raise Refused('development_command_unavailable_or_timeout') from None
    if p.returncode:raise Refused('development_command_failed')
    if len(p.stdout)>2*1024**2:raise Refused('development_output_limit')
    return p.stdout

def docker(*args,**kwargs):return run(['/usr/bin/docker',*args],**kwargs)

def require_host():
    if os.geteuid()!=0 or os.uname().nodename!='kromin':raise Refused('development_host_or_operator_required')

def source_files(source):
    paths=[source/n for n in ['main.py','dev_main.py','development.py','dev_bootstrap.py']]
    paths+=sorted((source/'release_control').glob('*.py'))
    return {p.relative_to(source).as_posix():sha(p.read_bytes()) for p in paths}

def compose(images,install_id='pending'):
    return '''services:
  postgres:
    init: true
    labels: {platforma.workflow.install-id: 'INSTALL_ID'}
    image: PG_IMAGE
    environment:
      POSTGRES_USER: platformadev
      POSTGRES_DB: gateway_mcp_dev
      POSTGRES_PASSWORD: ${PLATFORMA_DEV_DB_PASSWORD}
    command: ["postgres", "-c", "shared_buffers=16MB", "-c", "max_connections=20"]
    mem_limit: 192m
    cpus: 0.5
    pids_limit: 96
    restart: unless-stopped
    volumes:
      - postgres:/var/lib/postgresql/data
  gateway:
    init: true
    labels: {platforma.workflow.install-id: 'INSTALL_ID'}
    image: GW_IMAGE
    user: "10001:10001"
    env_file: .env
    command: ["sh", "-c", "gateway-mcp migrate up && exec gateway-mcp --transport streamable-http"]
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 384m
    cpus: 1.0
    pids_limit: 96
    restart: unless-stopped
    tmpfs:
      - /tmp:rw,nosuid,nodev,size=64m
    volumes:
      - knowledge:/data/company-knowledge-vault
      - requests:/data/platforma-sandbox-requests
      - audit:/data/audit
  caddy:
    init: true
    labels: {platforma.workflow.install-id: 'INSTALL_ID'}
    image: CADDY_IMAGE
    read_only: true
    cap_drop: [ALL]
    cap_add: [NET_BIND_SERVICE]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 64m
    cpus: 0.3
    pids_limit: 96
    restart: unless-stopped
    ports:
      - "127.0.0.1:18081:8080"
    volumes:
      - ../edge:/etc/caddy:ro
    tmpfs:
      - /data:rw,nosuid,nodev,size=8m
      - /config:rw,nosuid,nodev,size=8m
volumes:
  postgres:
    labels: {platforma.workflow.install-id: 'INSTALL_ID'}
  knowledge:
    labels: {platforma.workflow.install-id: 'INSTALL_ID'}
  requests:
    labels: {platforma.workflow.install-id: 'INSTALL_ID'}
  audit:
    labels: {platforma.workflow.install-id: 'INSTALL_ID'}
networks:
  default:
    labels: {platforma.workflow.install-id: 'INSTALL_ID'}
'''.replace('PG_IMAGE',images['postgres:16-alpine']['id']).replace('GW_IMAGE',images[GATEWAY]['id']).replace('CADDY_IMAGE',images[CADDY]['id']).replace('INSTALL_ID',install_id)

def make_env(password,jwt):
    return ('PLATFORMA_DEV_DB_PASSWORD='+password+'\nGATEWAY_DATABASE_URL=postgresql://platformadev:'+password+'@postgres:5432/gateway_mcp_dev\n'+
    'GATEWAY_JWT_SECRET='+jwt+'\nGATEWAY_USER_TOKEN_ENCRYPTION_KEY='+urlsafe_b64encode(secrets.token_bytes(32)).decode()+'\n'+'''GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8000
GATEWAY_TRANSPORT=streamable-http
GATEWAY_AUTH_ENABLED=true
GATEWAY_LOGIN_PROVIDER=bitrix24
GATEWAY_BITRIX_EMBED_ENABLED=false
GATEWAY_BITRIX_PORTAL=akmetron.bitrix24.ru
GATEWAY_BITRIX_OWNER_USER_ID=3318
GATEWAY_CORPORATE_SKILLS_ENABLED=true
GATEWAY_PLATFORMA_SKILL_LIBRARY_ENABLED=true
GATEWAY_KNOWLEDGE_VAULT_PATH=/data/company-knowledge-vault
GATEWAY_PUBLIC_URL=PUBLIC_URL
GATEWAY_ISSUER_URL=PUBLIC_URL
GATEWAY_RESOURCE_URL=PUBLIC_URL/mcp
GATEWAY_AUDIT_LOG=/data/audit/development.jsonl
GATEWAY_TOOLS_FILE=/app/gateway-tools.json
'''.replace('PUBLIC_URL',PUBLIC_URL))

def configuration(base):
    return {'schema':1,'mode':'development','state_dir':str(base/'state'),'project_dir':str(base/'app'),
      'compose_file':str(base/'app/compose.yaml'),'env_file':str(base/'app/.env'),'project':PROJECT,
      'gateway_service':'gateway','postgres_service':'postgres','database':'gateway_mcp_dev','db_user':'platformadev',
      'sole_application_writer':'gateway_operator_confirmed','minimum_disk_bytes':512*1024**2,
      'maximum_volume_bytes':128*1024**2,'health_timeout_seconds':90,'worker_service':UNIT,
      'gateway_health_url':'http://127.0.0.1:8000/healthz','expected_public_url':PUBLIC_URL,
      'volume_names':[PROJECT+'_'+n for n in ['knowledge','requests','audit']],
      'caddy':{'container':PROJECT+'-caddy-1','binary':'/usr/bin/caddy','main_config':str(base/'edge/Caddyfile'),
       'container_config':'/etc/caddy/Caddyfile','vhost_file':str(base/'edge/platforma.caddy'),
       'maintenance_text':':8080 {\n respond "DEV release in progress" 503\n}\n',
       'probe':{'url':'http://127.0.0.1:8080/healthz'},
       'neighbours':[{'url':'http://127.0.0.1:8080/healthz','headers':{'Host':'control.platforma-dev.invalid'}}]}}

def plan(source,base=Path('/opt/platforma-dev'),rehearsal=False):
    require_host();base=Path(base)
    if rehearsal:
        if not base.parent.name.startswith('platforma-dev-rehearsal-') or base.parent.parent!=Path('/var/tmp'):
            raise Refused('rehearsal_path_required')
    elif base!=Path('/opt/platforma-dev'):raise Refused('development_install_path_fixed')
    if base.exists():raise Refused('development_already_exists')
    trusted(base.parent,True)
    if docker('ps','-aq','--filter','label=com.docker.compose.project='+PROJECT).strip():raise Refused('development_project_in_use')
    if docker('volume','ls','-q','--filter','label=com.docker.compose.project='+PROJECT).strip():raise Refused('development_volumes_in_use')
    unit_path=(Path('/run/systemd/system') if rehearsal else Path('/etc/systemd/system'))/UNIT
    if unit_path.exists() or (Path('/etc/systemd/system')/UNIT).exists():raise Refused('development_worker_exists')
    with socket.socket() as sock:
        try:sock.bind(('127.0.0.1',18081))
        except OSError:raise Refused('development_port_in_use') from None
    if shutil.disk_usage(base.parent).free<2*1024**3:raise Refused('development_disk_low')
    mem=dict(x.split(':',1) for x in Path('/proc/meminfo').read_text().splitlines())
    if int(mem['MemAvailable'].split()[0])<800*1024:raise Refused('development_memory_low')
    images={}
    for ref in [GATEWAY,'postgres:16-alpine',CADDY]:
        obj=json.loads(docker('image','inspect',ref))[0]
        images[ref]={'id':obj['Id'],'revision':(obj['Config'].get('Labels') or {}).get('org.opencontainers.image.revision')}
    gw=json.loads(docker('image','inspect',GATEWAY))[0]
    if images[GATEWAY]['id']!=IMAGE_ID or images[GATEWAY]['revision']!=REVISION or gw['Config'].get('User')!='10001:10001' or gw['Config'].get('Entrypoint')!=['/usr/local/bin/gateway-private-entrypoint']:
        raise Refused('development_image_not_accepted')
    payload={'schema':1,'base':str(base),'project':PROJECT,'database':'gateway_mcp_dev','images':images,
      'source_files':source_files(source),'rehearsal':rehearsal,'unit_path':str(unit_path),
      'public_url':PUBLIC_URL,'listen':'127.0.0.1:18081','maximum_memory_mib':640,
      'public_auth':'pending_separate_bitrix_application','production_data':False,
      'created':time.time(),'expires':time.time()+1800}
    return {'payload':payload,'sha256':sha(encoded(payload))}

@contextlib.contextmanager
def installation_lock():
    import fcntl
    path=Path('/var/tmp/platforma-dev-bootstrap.lock')
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        st=os.fstat(fd)
        if st.st_uid!=0 or st.st_mode&0o077:raise Refused('development_bootstrap_lock_unsafe')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Refused('development_bootstrap_busy') from None
        yield
    finally:os.close(fd)

def apply(source,entry,digest):
    require_host()
    with installation_lock():return _apply(source,entry,digest)

def _apply(source,entry,digest):
    require_host();p=entry['payload'];base=Path(p['base'])
    if digest!=entry['sha256'] or digest!=sha(encoded(p)):raise Refused('bootstrap_plan_hash_mismatch')
    if time.time()>p['expires']:raise Refused('bootstrap_plan_expired')
    current=plan(source,base,p['rehearsal'])['payload']
    if {k:v for k,v in current.items() if k not in ['created','expires']}!={k:v for k,v in p.items() if k not in ['created','expires']}:
        raise Refused('bootstrap_baseline_changed')
    base.mkdir(mode=0o700)
    for name in ['app','edge','state','config','code']:(base/name).mkdir(mode=0o700)
    nonce=secrets.token_hex(16)
    atomic(base/'bootstrap.json',{'schema':1,'id':nonce,'phase':'files_prepared','plan_sha256':digest,'rehearsal':p['rehearsal'],'public_auth':'pending'})
    for name in p['source_files']:
        target=base/'code'/name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        write_bytes(target,(source/name).read_bytes(),0o600)
    write_bytes(base/'app/.env',make_env(secrets.token_urlsafe(32),secrets.token_urlsafe(48)).encode())
    write_bytes(base/'app/compose.yaml',compose(p['images'],nonce).encode())
    write_bytes(base/'edge/Caddyfile',b'{\n auto_https off\n}\nimport /etc/caddy/platforma.caddy\nimport /etc/caddy/control.caddy\n')
    write_bytes(base/'edge/control.caddy',b'http://control.platforma-dev.invalid:8080 {\n respond "platforma-dev-control" 200\n}\n')
    write_bytes(base/'edge/platforma.caddy',b':8080 {\n @pending path / /admin* /join* /auth/bitrix24*\n respond @pending "DEV: Bitrix24 test application is not configured yet" 503\n reverse_proxy gateway:8000\n}\n')
    cfg=configuration(base);atomic(base/'config/release.json',cfg)
    backend=Linux(cfg)
    # Named volume metadata must be readable by the nonroot Gateway.
    for name in cfg['volume_names']:
        docker('volume','create','--label','com.docker.compose.project='+PROJECT,'--label','com.docker.compose.volume='+name.removeprefix(PROJECT+'_'),
               '--label','platforma.workflow.install-id='+nonce,name)
        directory=backend.volume_root(name);os.chown(directory,10001,10001);os.chmod(directory,0o700)
    unit_text='''[Unit]
Description=Platforma development release worker
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
User=root
Group=root
UMask=0077
WorkingDirectory=BASE/code
ExecStart=/usr/bin/python3 -I BASE/code/dev_main.py _worker
TimeoutStartSec=900
TimeoutStopSec=30
Restart=no
KillMode=control-group
NoNewPrivileges=true
PrivateTmp=true
'''.replace('BASE',str(base))
    # Rehearsal files are in /var/tmp, hidden by PrivateTmp.
    if p['rehearsal']:unit_text=unit_text.replace('PrivateTmp=true\n','PrivateTmp=false\n')
    write_bytes(p['unit_path'],unit_text.encode(),0o644)
    run(['/usr/bin/systemctl','daemon-reload'])
    receipt=read(base/'bootstrap.json');receipt['unit_text_sha256']=sha(unit_text.encode());receipt['unit_path']=p['unit_path'];atomic(base/'bootstrap.json',receipt)
    backend.compose_run('up','-d',timeout=120)
    end=time.monotonic()+90
    while time.monotonic()<end:
        if backend.health()['ok']:break
        time.sleep(1)
    else:raise Refused('development_bootstrap_health_timeout')
    receipt['phase']='technical_ready';atomic(base/'bootstrap.json',receipt)
    return check(base)

def check(base):
    require_host();base=trusted(base,True);cfg=read(base/'config/release.json');backend=Linux(cfg)
    health=backend.health();versions=backend.versions()
    anon=backend.direct_http('/mcp')[0]
    status,body=backend.direct_http('/.well-known/oauth-authorization-server');metadata=json.loads(body)
    value={'technical_ready':health['ok'] and anon==401 and status==200 and metadata.get('issuer')==PUBLIC_URL,
      'project':PROJECT,'schema_count':len(versions),'revision':health['release_sha'],'anonymous_mcp_status':anon,
      'public_auth':'pending_separate_bitrix_application','public_url':PUBLIC_URL,'production_data':False}
    if not value['technical_ready']:raise Refused('development_acceptance_failed')
    return value
