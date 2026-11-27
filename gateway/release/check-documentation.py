"""Offline consistency check of explicitly maintained corporate documentation."""
from pathlib import Path
from urllib.parse import unquote,urlsplit
import json,re,sys

ROOT=Path(__file__).resolve().parents[1]

def check(root=ROOT):
    manifest=json.loads((root/'docs/corporate-documentation.json').read_text(encoding='utf-8'))
    errors=[];links=0
    if manifest.get('release_status')!='prepared_not_deployed':errors.append('release_status_requires_review')
    if manifest.get('migration_count')!=len(list((root/'migrations').glob('*.sql'))):errors.append('migration_count_mismatch')
    for name in manifest['active_documents']:
        path=root/name
        if not path.is_file():errors.append(name+':missing');continue
        text=path.read_text(encoding='utf-8')
        if 'kromin.axion-test.akmetron.ru' in text or '195-133-14-140.sslip.io' in text:errors.append(name+':old_address')
        for target in re.findall(r'\[[^\]]+\]\(([^\s)]+)\)',text):
            links+=1;url=urlsplit(target)
            if url.scheme:
                if url.scheme!='https':errors.append(name+':unsafe_link_scheme')
                continue
            resolved=(path.parent/unquote(url.path)).resolve()
            if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():errors.append(name+':broken_relative_link:'+target)
    for name in manifest['legacy_documents']:
        if not (root/name).read_text(encoding='utf-8').startswith('> Исторический учебный материал.'):errors.append(name+':missing_legacy_label')
    return {'successful':not errors,'documents':len(manifest['active_documents']),'links_checked':links,
        'legacy_labels':len(manifest['legacy_documents']),'errors':errors,'network_calls':False,
        'published_pages_verified':False,'protected_mounts_modified':False}

if __name__=='__main__':
    result=check();print(json.dumps(result,ensure_ascii=False));sys.exit(0 if result['successful'] else 1)
