"""Standalone synthetic Git and sandbox roundtrips, using disposable resources."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('suite_runner', Path(__file__).with_name('run-suite.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    url = os.environ.get('ONBOARDING_TEST_DATABASE_URL', '')
    info = conninfo_to_dict(url)
    if (info.get('dbname') != 'platforma_onboarding_test' or info.get('host') not in {'127.0.0.1','localhost','::1'}
            or info.get('hostaddr') or info.get('service')):
        raise ValueError('Only the CI disposable loopback database is accepted')
    with psycopg.connect(url, autocommit=True) as conn:
        if conn.execute("select count(*) from schema_migrations").fetchone()[0] != len(list((ROOT/'migrations').glob('*.sql'))):
            raise ValueError('Complete disposable CI migrations are required first')
        if conn.execute("select 1 from pg_database where datname='sandbox_feature_test'").fetchone():
            raise ValueError('Refuse to reuse an existing sandbox_feature_test database')
        conn.execute('create database sandbox_feature_test')
    sandbox_url = make_conninfo(url, dbname='sandbox_feature_test')
    results = []
    for name in ['factory_git_roundtrip.py', 'integration_sandbox_features.py']:
        with tempfile.TemporaryDirectory(prefix='gwi-') as temp:
            temporary = Path(temp).resolve()
            env = runner.clean_environment(temporary)
            if name == 'integration_sandbox_features.py':
                profiles = json.loads((ROOT/'addons/platforma-training/example-addon-profile.json').read_text())
                owner = profiles['profiles'].pop('test:owner')
                owner.update(subject='yandex:9001', expires_at='2099-01-01T00:00:00Z')
                for route in ['platforma.requests.create','platforma.requests.update']:
                    owner['resources'].append({'id':route,'kind':'backend_route','operations':['write']})
                owner['resources'].append({'id':'mcp:gateway_telemetry_skill_stats','kind':'tool','operations':['read']})
                owner['skill_telemetry'] = {'project':'platforma-training','agents':['codex'],'clients':['codex-desktop'],
                    'skills':{'platforma-training-service-reader':{'pack':'platforma-training','version':'0.1.0'}},
                    'scopes':['telemetry:read','telemetry:write']}
                profiles['profiles']['yandex:9001'] = owner
                profile = temporary/'profiles.json'; profile.write_text(json.dumps(profiles))
                env.update(SANDBOX_DISPOSABLE_DATABASE='true', GATEWAY_DATABASE_URL=sandbox_url,
                    GATEWAY_MIGRATIONS_DIR=str(ROOT/'migrations'), SANDBOX_TEST_PROFILES_FILE=str(profile),
                    GATEWAY_ADDON_PROFILES_FILE=str(profile), GATEWAY_TOOLS_FILE=str(ROOT/'gateway-platforma-tools.json'),
                    GATEWAY_RESOURCE_POLICY_MODE='strict', GATEWAY_KNOWLEDGE_VAULT_PATH=str(temporary/'vault'),
                    GATEWAY_PLATFORMA_SANDBOX_WRITE_ENABLED='true', GATEWAY_PLATFORMA_SANDBOX_DATA_DIR=str(temporary/'overlay'))
            program = "import importlib.util,runpy,sys;from pathlib import Path;sys.path.insert(0,str(Path.cwd()));s=importlib.util.spec_from_file_location('runner',Path('release/run-suite.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);sys.addaudithook(m.deny_external_network);runpy.run_path(sys.argv[1],run_name='__main__')"
            try:
                run = subprocess.run([sys.executable,'-c',program,str(ROOT/'tests'/name)], cwd=ROOT, env=env,
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
                code, log = run.returncode, run.stdout+run.stderr
            except subprocess.TimeoutExpired:
                code, log = 124, 'Standalone roundtrip timed out.\n'
            (output/(name+'.log')).write_text(log, encoding='utf-8')
            results.append({'script':name,'exit_code':code})
    summary = {'successful':all(row['exit_code']==0 for row in results),'scripts':results,
               'synthetic_only':True,'external_network_denied':True,'production_keys_used':False}
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary))
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
