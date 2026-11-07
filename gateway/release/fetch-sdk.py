"""Fetch official SDK at build time only; changed bytes fail the build."""
import argparse,hashlib,json,urllib.request
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root',type=Path,default=Path('/src'))
root=parser.parse_args().source_root.resolve()
provenance=root/'docs/bitrix-sdk-provenance.json'
if not provenance.is_file():
    provenance=root/'release-sdk-provenance.json' # Existing Docker build layout.
info=json.loads(provenance.read_text())
url=info['source_url']
assert url=='https://api.bitrix24.tech/api/v1/'
with urllib.request.urlopen(url,timeout=45) as response:
    data=response.read(1024*1024+1)
assert len(data)==info['bytes'], 'Unexpected official SDK length'
assert hashlib.sha256(data).hexdigest()==info['sha256'], 'Official SDK changed; review required'
target=root/'gateway_mcp/help_assets/bitrix24-sdk-v1.js'
target.write_bytes(data)
