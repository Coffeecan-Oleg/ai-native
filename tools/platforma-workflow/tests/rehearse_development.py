"""Temporary, synthetic deployment. Never installs /opt/platforma-dev."""
import io,json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import development as dev
from release_control.core import Store,Controller,atomic,read,sha
from release_control.linux import Linux,write_bytes,file_sha

dev.require_host()
SOURCE=Path(__file__).resolve().parents[1]
ROOT=Path(tempfile.mkdtemp(prefix='platforma-dev-rehearsal-',dir='/var/tmp'))
BASE=ROOT/'dev'
report={'production_changes':False,'permanent_development_installed':False,'root':str(ROOT),'cases':[]}
def case(name,**kwargs):
    report['cases'].append({'name':name,'successful':True,**kwargs});print(name+': OK',flush=True)
def runtime():
    values={}
    for cid in dev.docker('ps','-q').decode().split():
        x=json.loads(dev.docker('inspect',cid))[0]
        values[cid]=(x['Image'],x['State']['StartedAt'],x['RestartCount'])
    return values
def wait_health(backend):
    for _ in range(90):
        if backend.health()['ok']:return
        time.sleep(1)
    raise RuntimeError('rehearsal_health_timeout')
def cli(*args):
    return json.loads(dev.run(['/usr/bin/python3','-I',str(BASE/'code/dev_main.py'),*args],timeout=120))
original=runtime();backend=None
try:
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.discover(str(SOURCE/'tests'),pattern='test_*.py'))
    (ROOT/'unit-tests.log').write_text(stream.getvalue())
    assert result.wasSuccessful() and not result.skipped
    report['unit_tests']={'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped)}
    case('linux_unit_tests',tests=result.testsRun)
    entry=dev.plan(SOURCE,BASE,rehearsal=True);atomic(ROOT/'bootstrap-plan.json',entry)
    value=dev.apply(SOURCE,entry,entry['sha256'])
    cfg=read(BASE/'config/release.json');backend=Linux(cfg)
    assert value['technical_ready'] and value['schema_count']==26 and value['revision']==dev.REVISION
    case('cold_bootstrap_separate_database_and_schema26',revision=value['revision'])
    edge=backend.object('platformadev-caddy-1')
    assert edge['HostConfig']['PortBindings']=={'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'18081'}]}
    for service in ['gateway','postgres']:
        assert not backend.container(service)['HostConfig']['PortBindings']
    assert backend.probe({'url':'http://127.0.0.1:8080/admin'})['status']==503
    assert backend.direct_http('/mcp')[0]==401 and backend.direct_http('/metrics')[0] in [401,404]
    case('private_edge_auth_pending_and_anonymous_access_denied')
    import urllib.request
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:18081/healthz',timeout=5) as response:
            assert response.status==200
        case('loopback_port_available_for_ssh_tunnel')
    except Exception:
        report['loopback_route']='requires_host_network_review'
        raise
    env_hash=file_sha(BASE/'app/.env')
    backend.sql("create table workflow_synthetic_sentinel(value text); insert into workflow_synthetic_sentinel values('rehearsal-only')")
    backend.compose_run('restart','gateway',timeout=60);wait_health(backend)
    assert backend.sql('select value from workflow_synthetic_sentinel')=='rehearsal-only'
    assert env_hash==file_sha(BASE/'app/.env') and (BASE/'app/.env').stat().st_mode&0o777==0o600
    case('restart_preserves_synthetic_database_and_private_keys')
    # Fixture only: seed schema25 using the new CLI, then run the accepted old image.
    backend.compose_run('stop','gateway',timeout=60)
    backend.sql('DROP DATABASE "gateway_mcp_dev" WITH (FORCE)',database='postgres')
    backend.sql('CREATE DATABASE "gateway_mcp_dev" OWNER "platformadev"',database='postgres')
    fixtures=SOURCE/'fixtures/migrations25'
    if not fixtures.exists():
        import shutil
        originals=sorted((SOURCE.parents[1]/'gateway/migrations').glob('*.sql'))[:25]
        assert len(originals)==25
        fixtures=ROOT/'migrations25';fixtures.mkdir()
        for path in originals:shutil.copyfile(path,fixtures/path.name)
    assert len(list(fixtures.glob('*.sql')))==25
    dev.docker('run','--rm','--init','--network','platformadev_default','--user','10001:10001','--read-only',
      '--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','192m','--pids-limit','96',
      '--tmpfs','/tmp:rw,nosuid,nodev,size=32m','--env-file',str(BASE/'app/.env'),
      '-v',str(fixtures)+':/copy-migrations:ro','-e','GATEWAY_MIGRATIONS_DIR=/copy-migrations',
      '--entrypoint','gateway-mcp',dev.IMAGE_ID,'migrate','up',timeout=120)
    backend.sql("create table workflow_synthetic_sentinel(value text); insert into workflow_synthetic_sentinel values('before-release')")
    old=json.loads(dev.docker('image','inspect','platforma-sandbox:nonroot-candidate-24dded02eef8'))[0]['Id']
    temporary=BASE/'app/old-fixture.yaml';backend.render(backend.compose,temporary,old)
    write_bytes(backend.compose,temporary.read_bytes())
    backend.compose_run('up','-d','--no-deps','gateway',timeout=90);wait_health(backend)
    assert len(backend.versions())==25
    plan=cli('plan','evening-4b53b6d9ede2');op=cli('apply',plan['payload']['id'],plan['sha256']);oid=op['id']
    for _ in range(360):
        value=cli('status',oid)
        if value['phase'] in ['awaiting_accept','failed_closed','failed_unconfirmed','aborted']:break
        time.sleep(.5)
    assert value['phase']=='awaiting_accept',value
    assert backend.probe(cfg['caddy']['probe'])['status']==503 and len(backend.versions())==26
    assert backend.sql('select value from workflow_synthetic_sentinel')=='before-release'
    case('development_release_cli_worker_schema25_to26_gate_closed',operation_id=oid)
    assert cli('accept',oid)['phase']=='accepted'
    assert backend.probe(cfg['caddy']['probe'])['status']==200 and cli('current')=={'active':False}
    case('explicit_accept_opens_technical_edge')
    changed=dict(cfg,mode='production',project='platforma',database='gateway_mcp')
    atomic(BASE/'config/release.json',changed)
    denied=subprocess.run(['/usr/bin/python3','-I',str(BASE/'code/dev_main.py'),'current'],capture_output=True,text=True,timeout=15)
    assert denied.returncode and json.loads(denied.stdout)['reason']=='development_mode_required'
    atomic(BASE/'config/release.json',cfg)
    case('installed_dev_entry_refuses_production_scope')
    report['successful']=True
except Exception as ex:
    import traceback
    report['successful']=False;report['error']=str(ex)[:1600];report['traceback']=traceback.format_exc()[-2200:]
finally:
    receipt=read(BASE/'bootstrap.json') if (BASE/'bootstrap.json').exists() else {}
    token=receipt.get('id')
    path=Path(receipt.get('unit_path','/run/systemd/system/'+dev.UNIT))
    if receipt.get('unit_text_sha256') and path.exists() and file_sha(path)==receipt['unit_text_sha256']:
        dev.run(['/usr/bin/systemctl','stop',dev.UNIT],timeout=45)
        subprocess.run(['/usr/bin/systemctl','reset-failed',dev.UNIT],capture_output=True,timeout=10)
        path.unlink();dev.run(['/usr/bin/systemctl','daemon-reload'])
    ids=dev.docker('ps','-aq','--filter','label=com.docker.compose.project=platformadev').decode().split()
    report['oom']=[]
    for cid in ids:
        obj=json.loads(dev.docker('inspect',cid))[0]
        assert token and obj['Config']['Labels'].get('platforma.workflow.install-id')==token
        report['oom'].append(obj['State'].get('OOMKilled',False))
        if not report.get('successful'):
            p=subprocess.run(['/usr/bin/docker','logs','--tail','8',cid],capture_output=True)
            report.setdefault('copy_logs',{})[obj['Name']]=(p.stdout+p.stderr).decode(errors='replace')[-1400:]
        dev.docker('rm','-f',cid)
    for kind in ['volume','network']:
        names=dev.docker(kind,'ls','-q','--filter','label=com.docker.compose.project=platformadev').decode().split()
        for name in names:
            obj=json.loads(dev.docker(kind,'inspect',name))[0]
            assert token and (obj.get('Labels') or {}).get('platforma.workflow.install-id')==token
            dev.docker(kind,'rm',name)
    remaining=runtime()
    report['existing_runtime_unchanged']=all(remaining.get(k)==v for k,v in original.items())
    report['cleanup']=not dev.docker('ps','-aq','--filter','label=com.docker.compose.project=platformadev').strip()
    if not report['existing_runtime_unchanged'] or any(report['oom']):report['successful']=False
    (ROOT/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print('RESULT_JSON='+json.dumps(report),flush=True)
raise SystemExit(0 if report.get('successful') else 1)
