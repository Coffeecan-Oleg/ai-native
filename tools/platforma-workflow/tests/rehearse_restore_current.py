#!/usr/bin/env python3
"""Disposable schema-28 restore rehearsal on kromin. Synthetic data only.

No production/dev configuration or databases are read. All Docker objects carry
one random label, use an internal network and publish no ports. No image pulls.
"""
import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_control.storage import require as require_storage

IMAGE = 'platforma-gateway:evening-f95be3a63c65'
IMAGE_ID = 'sha256:084757324d812e1af69ee3fd00c58159ea4eef5e911662e7ea274ec838908964'
REVISION = 'f95be3a63c65417ef821f278dba7095d7098cf68'
SKILL = 'shared-' + 'b'*32


def run(args, data=None, timeout=60):
    p = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if p.returncode:
        # Only synthetic commands run here; do not print env-file contents.
        raise RuntimeError('lab_command_failed: ' + p.stderr.decode(errors='replace')[-800:])
    return p.stdout


def docker(*args, **kwargs):
    return run(['/usr/bin/docker', *args], **kwargs)


def runtime():
    result = {}
    for cid in docker('ps', '-q').decode().split():
        c = json.loads(docker('inspect', cid))[0]
        result[cid] = (c['Image'], c['State']['StartedAt'], c['RestartCount'])
    return result


def main():
    if os.geteuid() != 0 or os.uname().nodename != 'kromin':
        raise RuntimeError('dedicated_dev_host_required')
    mem = dict(x.split(':', 1) for x in Path('/proc/meminfo').read_text().splitlines())
    if int(mem['MemAvailable'].split()[0]) < 600*1024:
        raise RuntimeError('insufficient_free_memory')
    require_storage(Path('/var/lib/docker'), 512*1024**2)
    image = json.loads(docker('image', 'inspect', IMAGE))[0]
    if image['Id'] != IMAGE_ID or image['Config']['User'] != '10001:10001':
        raise RuntimeError('accepted_image_mismatch')
    pgid = json.loads(docker('image', 'inspect', 'postgres:16-alpine'))[0]['Id']
    before = runtime()
    nonce = secrets.token_hex(8)
    label = 'platforma.restore-lab=' + nonce
    prefix = 'platforma-restore-' + nonce
    base = Path(tempfile.mkdtemp(prefix=prefix+'-', dir='/var/tmp'))
    base.chmod(0o700)
    network, pg, gw, migration = (prefix+'-'+s for s in ('net', 'pg', 'gw', 'migrate'))
    names = (gw, migration, pg)
    report = {'synthetic_only': True, 'production_data_read': False,
              'production_changed': False, 'installed_dev_changed': False,
              'revision': REVISION, 'image_id': IMAGE_ID, 'postgres_image_id': pgid,
              'off_server_backup_configured': False, 'production_restore_accepted': False}
    started = time.monotonic()
    try:
        docker('network', 'create', '--internal', '--label', label, network)
        # Password and encryption keys are newly generated disposable fixtures.
        password = secrets.token_hex(16)
        pg_env = base/'pg.env'
        pg_env.write_text('POSTGRES_USER=restorelab\nPOSTGRES_PASSWORD='+password+'\nPOSTGRES_DB=restore_source\n')
        pg_env.chmod(0o600)
        docker('run', '-d', '--pull=never', '--name', pg, '--label', label,
               '--network', network, '--network-alias', 'restore-db', '--env-file', str(pg_env),
               '--memory', '160m', '--cpus', '0.35', '--pids-limit', '96',
               '--tmpfs', '/var/lib/postgresql/data:rw,nosuid,nodev,size=160m',
               pgid, 'postgres', '-c', 'shared_buffers=16MB', '-c', 'max_connections=20')
        for _ in range(40):
            try:
                docker('exec', pg, 'pg_isready', '-U', 'restorelab', '-d', 'restore_source', timeout=5)
                break
            except RuntimeError:
                time.sleep(.5)
        else:
            raise RuntimeError('disposable_postgres_not_ready')

        def sql(database, text):
            return docker('exec', '-i', pg, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-qAt',
                          '-U', 'restorelab', '-d', database, data=text.encode())

        env = ('GATEWAY_DATABASE_URL=postgresql://restorelab:'+password+'@restore-db:5432/restore_source\n'
               'GATEWAY_JWT_SECRET='+secrets.token_hex(32)+'\n'
               'GATEWAY_AUTH_ENABLED=true\nGATEWAY_LOGIN_PROVIDER=bitrix24\n'
               'GATEWAY_CORPORATE_MODE=true\nGATEWAY_UNISENDER_READONLY_ENABLED=false\n'
               'GATEWAY_PUBLIC_URL=https://restore.invalid\nGATEWAY_ISSUER_URL=https://restore.invalid\n'
               'GATEWAY_RESOURCE_URL=https://restore.invalid/mcp\nGATEWAY_RELEASE_SHA='+REVISION+'\n'
               'GATEWAY_KNOWLEDGE_VAULT_PATH=/data/knowledge\nGATEWAY_AUDIT_LOG=/data/audit/audit.jsonl\n')
        source_env = base/'source.env'; source_env.write_text(env); source_env.chmod(0o600)
        limits = ['--pull=never', '--label', label, '--network', network, '--read-only',
                  '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                  '--memory', '192m', '--cpus', '0.4', '--pids-limit', '96',
                  '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m']
        docker('run', '--rm', '--name', migration, *limits, '--env-file', str(source_env),
               IMAGE_ID, 'gateway-mcp', 'migrate', 'up', timeout=120)
        assert sql('restore_source', 'select count(*) from schema_migrations;').strip() == b'28'
        content = b'---\nname: restore-check\nversion: "1.0.0"\n---\nSynthetic restore fixture.\n'
        digest = hashlib.sha256(content).hexdigest()
        sql('restore_source', """BEGIN;
INSERT INTO platforma_shared_skills(skill_id,origin,title,title_key,version,author_subject,author_label)
VALUES ('%s','uploaded','Restore fixture','restore fixture','1.0.0','synthetic:employee','Synthetic employee');
INSERT INTO platforma_shared_skill_files(skill_id,path,content,sha256)
VALUES ('%s','SKILL.md',decode('%s','hex'),'%s');
INSERT INTO platforma_skill_workflows(skill_id,author_subject,author_label,stage,candidate_revision,published_revision)
VALUES ('%s','synthetic:employee','Synthetic employee','published',1,1);
INSERT INTO platforma_skill_versions(skill_id,revision,title,title_key,description,version,manifest,files,fingerprint,created_by)
VALUES ('%s',1,'Restore fixture','restore fixture','Synthetic only','1.0.0','{}','{}','%s','synthetic:employee');
INSERT INTO platforma_skill_workflow_events(skill_id,revision,actor_subject,action)
VALUES ('%s',1,'synthetic:employee','published');
COMMIT;""" % (SKILL, SKILL, content.hex(), digest, SKILL, SKILL, digest, SKILL))
        table_names = sql('restore_source', "select tablename from pg_tables where schemaname='public' order by tablename;").decode().split()
        assert table_names and all(name.replace('_','').isalnum() for name in table_names)

        def counts(db):
            return {name: int(sql(db, 'select count(*) from "'+name+'";')) for name in table_names}

        before_counts = counts('restore_source')
        dump = docker('exec', pg, 'pg_dump', '-U', 'restorelab', '-d', 'restore_source', '-Fc')
        assert dump.startswith(b'PGDMP') and len(dump)<8*1024**2
        (base/'synthetic.dump').write_bytes(dump); (base/'synthetic.dump').chmod(0o600)
        toc = docker('exec','-i',pg,'pg_restore','-l',data=dump)
        assert b'schema_migrations' in toc
        sql('postgres','create database restore_target owner restorelab;')
        docker('exec','-i',pg,'pg_restore','--exit-on-error','-U','restorelab','-d','restore_target',data=dump)
        assert counts('restore_target') == before_counts
        restored = sql('restore_target', "select encode(content,'hex') from platforma_shared_skill_files where skill_id='%s';" % SKILL).strip()
        assert bytes.fromhex(restored.decode()) == content
        report.update(schema=28,tables_compared=len(table_names),table_counts_match=True,
                      published_skill_bytes_match=True,dump_sha256=hashlib.sha256(dump).hexdigest())
        source=base/'source-data'; restored_dir=base/'restored-data'
        source.mkdir(); restored_dir.mkdir()
        fixtures={'knowledge/README.md':b'Synthetic company knowledge\n',
                  'requests/example.json':b'{"synthetic":true}\n','audit/example.jsonl':b'{"synthetic":true}\n'}
        for name, raw in fixtures.items():
            p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw);p.chmod(0o640)
        # Copy the three application volumes as ordinary files; never copy a live PGDATA directory.
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w:gz') as tar:
            for name in fixtures:tar.add(source/name,arcname=name)
        with tarfile.open(fileobj=io.BytesIO(archive.getvalue()),mode='r:gz') as tar:
            for m in tar:
                assert m.isfile() and m.name in fixtures
                p=restored_dir/m.name;p.parent.mkdir(parents=True,exist_ok=True)
                p.write_bytes(tar.extractfile(m).read());p.chmod(m.mode)
        for p in [restored_dir,*restored_dir.rglob('*')]:
            os.chown(p,10001,10001)
            if p.is_dir():p.chmod(0o750)
        for name,raw in fixtures.items():
            p=restored_dir/name
            assert p.read_bytes()==raw and p.stat().st_mode&0o777==0o640
        restored_env=base/'restored.env'
        restored_env.write_text(env.replace('/restore_source','/restore_target'));restored_env.chmod(0o600)
        # Explicit schema check before starting the restored application.
        docker('run','--rm','--name',migration,*limits,'--env-file',str(restored_env),IMAGE_ID,
               'gateway-mcp','migrate','up',timeout=90)
        docker('run','-d','--name',gw,*limits,'--env-file',str(restored_env),
               '-v',str(restored_dir)+':/data',IMAGE_ID)
        probe="""import json,urllib.request,urllib.error
with urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3) as r:h=json.load(r)
try:urllib.request.urlopen('http://127.0.0.1:8000/mcp',timeout=3);m=200
except urllib.error.HTTPError as e:m=e.code
print(json.dumps({'health':h,'anonymous_mcp':m}))"""
        for _ in range(45):
            try:
                observed=json.loads(docker('exec',gw,'python','-c',probe,timeout=7));break
            except RuntimeError:time.sleep(.5)
        else:raise RuntimeError('restored_gateway_unavailable')
        assert observed['health']['ok'] is True and observed['health']['auth_enabled'] is True
        assert observed['health']['postgres']['ok'] is True and observed['anonymous_mcp']==401
        assert observed['health']['release_sha']==REVISION
        for name in (pg,gw):
            c=json.loads(docker('inspect',name))[0]
            assert not c['HostConfig']['PortBindings'] and not c['State']['OOMKilled']
        assert json.loads(docker('network','inspect',network))[0]['Internal'] is True
        report.update(restored_gateway_healthy=True,anonymous_mcp_status=401,
                      application_files_match=True,application_files=3,no_published_ports=True,
                      network_internal=True,maximum_container_memory_mib=352,verified=True)
    finally:
        # Delete only this run's named and labelled disposable resources.
        for name in names:
            p=subprocess.run(['/usr/bin/docker','inspect',name],capture_output=True,timeout=10)
            if p.returncode==0:
                c=json.loads(p.stdout)[0]
                assert c['Config']['Labels'].get('platforma.restore-lab')==nonce
                docker('rm','-f',name)
        p=subprocess.run(['/usr/bin/docker','network','inspect',network],capture_output=True,timeout=10)
        if p.returncode==0:
            assert json.loads(p.stdout)[0]['Labels'].get('platforma.restore-lab')==nonce
            docker('network','rm',network)
        assert base.resolve()==base and base.parent==Path('/var/tmp') and base.name.startswith(prefix+'-')
        shutil.rmtree(base)
        report['temporary_resources_removed']=not base.exists()
        report['existing_containers_unchanged']=runtime()==before
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        print('RESULT_JSON='+json.dumps(report),flush=True)
    assert report['existing_containers_unchanged']


if __name__=='__main__':main()
