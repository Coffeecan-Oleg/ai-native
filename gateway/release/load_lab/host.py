"""Disposable, labelled load lab. Must never target a running Gateway or its DB."""
import hashlib,io,json,os,re,secrets,shutil,subprocess,sys,tarfile,tempfile,time
from pathlib import Path

MEMORY_MIB={'gateway':384,'postgres':192,'driver':96}
def guard(host,available_mib,free_mib,tag,sha):
    if host!='kromin':raise ValueError('load_lab_host_refused')
    if available_mib<800 or free_mib<2048:raise ValueError('load_lab_headroom_refused')
    if not re.fullmatch('[a-f0-9]{40}',sha) or tag!='platforma-gateway:evening-'+sha[:12]:raise ValueError('load_lab_image_refused')
def mib(raw):
    number,unit=re.fullmatch(r'([0-9.]+)([a-zA-Z]+)',raw.strip()).groups()
    return float(number)*{'B':1/1024**2,'kB':1000/1024**2,'KiB':1/1024,'MB':1000**2/1024**2,'MiB':1,'GB':1000**3/1024**2,'GiB':1024}[unit]
def mem_available():return int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))//1024

def remove_container(docker,name,nonce):
    """Docker can report an exit-event race after killing; recheck before retry."""
    for attempt in range(3):
        if not docker('ps','-aq','--filter','name=^/'+name+'$').strip():return
        c=json.loads(docker('inspect',name))[0]
        if c['Config']['Labels'].get('platforma.load.lab')!=nonce:
            raise ValueError('load_cleanup_foreign_container_refused')
        try:docker('rm','-f',name);return
        except (RuntimeError,subprocess.TimeoutExpired):
            if attempt==2:raise
            time.sleep(1)

def main():
    assert os.geteuid()==0
    tag,sha,digest,mode=sys.argv[1:];assert mode in {'full','bursts'}
    guard(os.uname().nodename,mem_available(),shutil.disk_usage('/var/lib/docker').free//1024**2,tag,sha)
    assert re.fullmatch('[a-f0-9]{64}',digest)
    env={'PATH':'/usr/bin:/bin','DOCKER_HOST':'unix:///var/run/docker.sock','LANG':'C.UTF-8'}
    def docker(*args,timeout=30):
        p=subprocess.run(['/usr/bin/docker',*args],capture_output=True,env=env,timeout=timeout)
        if p.returncode:raise RuntimeError('load_docker_command_failed')
        return p.stdout
    def runtime():
        result={}
        for cid in docker('ps','-q').decode().split():
            c=json.loads(docker('inspect',cid))[0];result[cid]=[c['Image'],c['State']['StartedAt'],c['RestartCount']]
        return result
    image=json.loads(docker('image','inspect',tag))[0]
    assert image['Config']['User']=='10001:10001' and image['Config']['Labels']['org.opencontainers.image.revision']==sha
    pg_image='sha256:81bd698b4594e751a3269e4dcd3e03a4a0ec0daf7b72e7aa1abd43cce9887542';docker('image','inspect',pg_image)
    nonce=secrets.token_hex(6);prefix='platformaload'+nonce;label='platforma.load.lab='+nonce
    base=Path(tempfile.mkdtemp(prefix='platforma-load-lab-',dir='/var/tmp'));base.chmod(0o700)
    raw=sys.stdin.buffer.read(2*1024**2+1);assert len(raw)<2*1024**2 and hashlib.sha256(raw).hexdigest()==digest
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as tar:
        allowed={'app.py','driver.py','overlay/storage_access.py','overlay/storage_oauth.py','overlay/authorization_reader.py'}
        for m in tar.getmembers():assert m.isfile() and m.name in allowed and m.size<128*1024
        tar.extractall(base,filter='data')
    for name in ['app.py','driver.py']:(base/name).chmod(0o644)
    state=base/'state';state.mkdir(mode=0o700);os.chown(state,10001,10001)
    results=base/'results';results.mkdir(mode=0o700);os.chown(results,10001,10001)
    def secret_file(name,entries):
        p=base/name;p.write_text('\n'.join(k+'='+v for k,v in entries.items())+'\n');p.chmod(0o600);return p
    password=secrets.token_hex(24);control=secrets.token_hex(24);jwt=secrets.token_hex(48)
    pg_env=secret_file('pg.env',{'POSTGRES_USER':'loadlab','POSTGRES_DB':'gateway_load_test','POSTGRES_PASSWORD':password})
    common={'PLATFORMA_LOAD_LAB':'disposable-v1','LOAD_SOURCE_SHA':sha,'LOAD_CONTROL':control,'LOAD_BURST_ONLY':'0' if mode=='full' else '1'}
    app_env=secret_file('app.env',common|{
        'GATEWAY_DATABASE_URL':'postgresql://loadlab:'+password+'@load-postgres:5432/gateway_load_test',
        'GATEWAY_AUTH_ENABLED':'true','GATEWAY_JWT_SECRET':jwt,'GATEWAY_LOGIN_PROVIDER':'bitrix24',
        'GATEWAY_BITRIX_PORTAL':'akmetron.bitrix24.ru','GATEWAY_BITRIX_CLIENT_ID':'local.synthetic.12345678',
        'GATEWAY_BITRIX_CLIENT_SECRET':'synthetic-not-real','GATEWAY_BITRIX_MEMBER_ID':'a'*32,'GATEWAY_BITRIX_OWNER_USER_ID':'20',
        'GATEWAY_BITRIX_UNISENDER_USER_IDS':','.join(str(i) for i in range(1,21)),
        'GATEWAY_UNISENDER_READONLY_ENABLED':'true','GATEWAY_PUBLIC_URL':'https://load.example.invalid',
        'GATEWAY_ISSUER_URL':'https://load.example.invalid','GATEWAY_RESOURCE_URL':'https://load.example.invalid/mcp',
        'GATEWAY_TOOLS_FILE':'/app/gateway-platforma-tools.json','GATEWAY_ADDON_PROFILES_FILE':'/state/profiles.json',
        'GATEWAY_PLATFORMA_OWNER_SUBJECT':'load-template','GATEWAY_AUDIT_LOG':'/tmp/load-audit.jsonl',
        'GATEWAY_MCP_OAUTH_SCOPES':'tools:call tools:read skills:read unisender:read knowledge:read telemetry:read telemetry:write',
        'GATEWAY_MCP_OAUTH_TOOLS':'gateway_call_tool gateway_access_profile unisender.getLists unisender.getContactCount'})
    driver_env=secret_file('driver.env',common)
    network=prefix+'-net';volume=prefix+'-pgdata';names={k:prefix+'-'+k for k in MEMORY_MIB};before=runtime();error=None;report={};samples=[]
    started=time.monotonic();minimum_available=mem_available()
    try:
        docker('network','create','--internal','--label',label,network)
        docker('volume','create','--label',label,volume)
        docker('run','-d','--name',names['postgres'],'--label',label,'--network',network,'--network-alias','load-postgres',
               '--memory','192m','--cpus','0.5','--pids-limit','96','--env-file',str(pg_env),
               '--mount','type=volume,src='+volume+',dst=/var/lib/postgresql/data',pg_image,
               '-c','shared_buffers=16MB','-c','work_mem=1MB','-c','max_connections=32')
        for _ in range(60):
            try:docker('exec',names['postgres'],'pg_isready','-U','loadlab','-d','gateway_load_test');break
            except RuntimeError:time.sleep(.5)
        else:raise RuntimeError('load_postgres_not_ready')
        for role,entry,envfile in [('gateway','app.py',app_env),('driver','driver.py',driver_env)]:
            mounts=['--mount','type=bind,src='+str(base/entry)+',dst=/lab/'+entry+',readonly',
                    '--mount','type=bind,src='+str(state)+',dst=/state',
                    '--mount','type=bind,src='+str(results)+',dst=/results']
            if role=='gateway' and (base/'overlay').is_dir():
                mounts+=['--mount','type=bind,src='+str(base/'overlay')+',dst=/lab/overlay,readonly']
            docker('run','-d','--name',names[role],'--label',label,'--network',network,
                '--network-alias','load-gateway' if role=='gateway' else 'load-driver','--memory',str(MEMORY_MIB[role])+'m',
                '--cpus','1' if role=='gateway' else '0.25','--pids-limit','128','--user','10001:10001',
                '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--tmpfs','/tmp:rw,nosuid,nodev,size=32m','--env-file',str(envfile),*mounts,
                '--entrypoint','python',tag,'-I','/lab/'+entry)
        while time.monotonic()-started<240:
            available=mem_available();minimum_available=min(minimum_available,available)
            if available<160:raise RuntimeError('load_host_memory_guard')
            if not json.loads(docker('inspect',names['gateway']))[0]['State']['Running']:raise RuntimeError('load_app_stopped')
            driver=json.loads(docker('inspect',names['driver']))[0]
            stats=docker('stats','--no-stream','--format','{{json .}}',*names.values(),timeout=10).decode()
            for line in stats.splitlines():
                v=json.loads(line);samples.append({'at_seconds':round(time.monotonic()-started,2),'container':v['Name'],
                    'memory_mib':round(mib(v['MemUsage'].split('/')[0]),2),'cpu_percent':float(v['CPUPerc'].rstrip('%'))})
            if not driver['State']['Running']:
                assert driver['State']['ExitCode']==0,'load_driver_failed'
                report=json.loads((results/'load-results.json').read_text());break
            time.sleep(1)
        else:raise RuntimeError('load_host_deadline')
        for role,name in names.items():
            c=json.loads(docker('inspect',name))[0];assert not c['State']['OOMKilled'],('load_oom',role)
        checked=json.loads(docker('exec',names['gateway'],'python','/usr/local/share/gateway-release/verify-image.py').decode())
        assert checked['verified'] and checked['source_revision']==sha
    except Exception as exc:
        error=type(exc).__name__+':'+str(exc)[:150]
        if (results/'progress.json').exists():report=json.loads((results/'progress.json').read_text())
    finally:
        checkpoint={'load':report,'samples':samples,'runtime_before':before,'error':error}
        (base/'before-cleanup.json').write_text(json.dumps(checkpoint));(base/'before-cleanup.json').chmod(0o600)
        for role,name in names.items():
            if docker('ps','-aq','--filter','name=^/'+name+'$').strip():
                c=json.loads(docker('inspect',name))[0];assert c['Config']['Labels'].get('platforma.load.lab')==nonce
                log=subprocess.run(['/usr/bin/docker','logs','--tail','35',name],capture_output=True,env=env,timeout=10)
                text=(log.stdout+log.stderr).decode(errors='replace')
                for token in [password,control,jwt]:text=text.replace(token,'[synthetic-secret-removed]')
                text=re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','[synthetic-token-removed]',text)
                (base/(role+'-diagnostic.log')).write_text(text);(base/(role+'-diagnostic.log')).chmod(0o600)
                remove_container(docker,name,nonce)
        if docker('network','ls','-q','--filter','name=^'+network+'$').strip():
            assert json.loads(docker('network','inspect',network))[0]['Labels']['platforma.load.lab']==nonce;docker('network','rm',network)
        if volume in docker('volume','ls','-q','--filter','label='+label).decode().split():
            assert json.loads(docker('volume','inspect',volume))[0]['Labels']['platforma.load.lab']==nonce;docker('volume','rm',volume)
        after=runtime();unchanged=all(after.get(cid)==v for cid,v in before.items())
        for f in [pg_env,app_env,driver_env,state/'tokens.json']:
            if f.exists():f.unlink()
        result={'successful':not error and report.get('successful',False) and unchanged,'error':error,'base':str(base),
            'source_sha':sha,'image_id':image['Id'],'harness_sha256':digest,
            'candidate_source_files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (base/'overlay').glob('*.py')},
            'elapsed_seconds':round(time.monotonic()-started,2),
            'limits_mib':MEMORY_MIB,'minimum_host_available_mib':minimum_available,'samples':samples,'load':report,
            'existing_runtime_unchanged':unchanged,'permanent_dev_changed':False,'production_changed':False,
            'temporary_containers_removed':not docker('ps','-aq','--filter','label='+label).strip(),
            'temporary_network_removed':not docker('network','ls','-q','--filter','label='+label).strip(),
            'temporary_volume_removed':not docker('volume','ls','-q','--filter','label='+label).strip(),
            'synthetic_secret_files_removed':not any(f.exists() for f in [pg_env,app_env,driver_env,state/'tokens.json'])}
        (base/'receipt.json').write_text(json.dumps(result,indent=2));(base/'receipt.json').chmod(0o600)
        print(json.dumps(result));sys.exit(0 if result['successful'] else 1)

if __name__=='__main__':main()
