"""No network, no secrets: verify installed code, versions and packaging."""
import hashlib,importlib.metadata as metadata,json,os,re,sys
from pathlib import Path
import gateway_mcp
from gateway_mcp.services.migrations import migration_files
root=Path(gateway_mcp.__file__).resolve().parent
baseline=json.loads(Path('/usr/local/share/gateway-release/baseline.json').read_text())
assert '/site-packages/gateway_mcp' in str(root)
assert not Path('/app/gateway_mcp').exists()
assert not list(Path('/app').glob('*.py'))
assert os.getuid()==os.getgid()==10001
assert sys.version.split()[0]==baseline['python_version']
for name,expected in baseline['live_source_fingerprints'].items():
    data=(root/name).read_bytes();digest=hashlib.sha256(data).hexdigest()
    if digest!=expected['sha256']:
        digest=hashlib.sha256(data.decode('utf-8').replace('\r\n','\n').encode()).hexdigest()
        assert digest==expected['lf_sha256'], 'Source mismatch: '+name
normal=lambda name:re.sub('[-_.]+','-',name).lower()
actual={normal(d.metadata['Name']):d.version for d in metadata.distributions()}
expected={normal(k):v for k,v in baseline['runtime_versions'].items()}
assert actual==expected, 'Installed package set differs from accepted runtime'
assert len(migration_files())==baseline['migration_count']
assert hashlib.sha256((root/'help_assets/bitrix24-sdk-v1.js').read_bytes()).hexdigest()==baseline['sdk_sha256']
assert all((Path('/app')/name).is_file() for name in baseline['root_configs'])
print(json.dumps({'verified':True,'runtime_root':str(root),'uid':os.getuid(),'source_files':len(baseline['live_source_fingerprints']),'packages':len(actual),'migrations':len(migration_files()),'duplicate_app_source':False}))
