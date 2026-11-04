"""Fetch official SDK at build time only; changed bytes fail the build."""
import hashlib,json,urllib.request
from pathlib import Path
info=json.loads(Path('/src/release-sdk-provenance.json').read_text())
url=info['source_url']
assert url=='https://api.bitrix24.tech/api/v1/'
with urllib.request.urlopen(url,timeout=45) as response:
    data=response.read(1024*1024+1)
assert len(data)==info['bytes'], 'Unexpected official SDK length'
assert hashlib.sha256(data).hexdigest()==info['sha256'], 'Official SDK changed; review required'
target=Path('/src/gateway_mcp/help_assets/bitrix24-sdk-v1.js')
target.write_bytes(data)
