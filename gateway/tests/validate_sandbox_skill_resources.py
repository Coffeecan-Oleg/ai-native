"""Read every prepared resource without executing its contents."""
import base64,hashlib,json
from pathlib import Path
from gateway_mcp.services import corporate_skills as service
service.ROOT=Path('/candidate/candidate/skill-catalog')
catalog=service.list_skills();assert len(catalog['skills'])==10
count=0
for skill in catalog['skills']:
    for name,meta in skill['files'].items():
        result=service.read_skill(skill['name'],name,limit=24576)
        if result['encoding']=='utf-8':blob=result['content'].encode('utf-8')
        else:
            parts=[base64.b64decode(result['content'])]
            while result['next_offset'] is not None:
                result=service.read_skill(skill['name'],name,offset=result['next_offset'],limit=24576)
                parts.append(base64.b64decode(result['content']))
            blob=b''.join(parts)
        assert hashlib.sha256(blob).hexdigest()==meta['sha256']
        count+=1
for path in ['../.env','/etc/passwd','scripts/../../.env','not-in-manifest.py']:
    try:service.read_skill('market-foothold',path)
    except PermissionError:pass
    else:raise AssertionError('Disallowed path read')
print(json.dumps({'ok':True,'skills':10,'resources':count,'skill_scripts_executed':False,'denied_paths':4}))
