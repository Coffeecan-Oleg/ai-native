"""Immutable, bounded reads of the five bundled corporate skills. No execution."""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1] / 'corporate_skills'


def enabled():
    return os.getenv('GATEWAY_CORPORATE_SKILLS_ENABLED', '').casefold() == 'true'


def _catalog():
    if not enabled():
        raise PermissionError('Corporate skill pack is disabled')
    catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
    if catalog.get('schema') != 1 or catalog.get('pack') != 'akmetron-corporate':
        raise ValueError('Invalid corporate skill manifest')
    return catalog


def list_skills():
    data = _catalog()
    return {'ok': True, 'pack': data['pack'], 'version': data['version'],
            'skills': [{k: row[k] for k in ('name', 'title', 'version', 'description')}
                       | {'files': row['files']} for row in data['skills']],
            'execution': 'client-side only; this MCP never executes the skill'}


def read_skill(skill, path='SKILL.md', offset=0, limit=16384):
    catalog = _catalog()
    row = next((s for s in catalog['skills'] if s['name'] == skill), None)
    if row is None:
        raise PermissionError('Unknown corporate skill')
    if not isinstance(path, str) or not path or '\\' in path or ':' in path:
        raise PermissionError('Resource path is not allowed')
    relative = PurePosixPath(path)
    if relative.is_absolute() or '..' in relative.parts or relative.as_posix() != path or path not in row['files']:
        raise PermissionError('Resource path is not allowed')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 24576:
        raise ValueError('Invalid chunk bounds')
    base = (ROOT / row['name']).resolve()
    target = base.joinpath(*relative.parts)
    if target.is_symlink() or not target.resolve().is_relative_to(base):
        raise PermissionError('Resource path is not allowed')
    spec = row['files'][path]
    if not 0 <= spec['bytes'] <= 1024 * 1024 or target.stat().st_size != spec['bytes']:
        raise ValueError('Corporate resource size mismatch')
    blob = target.read_bytes()
    if hashlib.sha256(blob).hexdigest() != spec['sha256']:
        raise ValueError('Corporate resource hash mismatch')
    if offset > len(blob):
        raise ValueError('Offset exceeds resource length')
    result = {'ok': True, 'skill': skill, 'path': path, 'total_bytes': len(blob),
              'sha256': spec['sha256'], 'read_only': True}
    if target.suffix in {'.md', '.yaml', '.css', '.html', '.js', '.py', '.svg'} or path == 'LICENSE':
        if offset or len(blob) > 131072:
            raise ValueError('Text resources must be read in full and within 128 KiB')
        return result | {'encoding': 'utf-8', 'content': blob.decode('utf-8'), 'next_offset': None}
    chunk = blob[offset:offset + limit]
    end = offset + len(chunk)
    return result | {'encoding': 'base64', 'offset': offset, 'content': base64.b64encode(chunk).decode('ascii'),
                     'next_offset': end if end < len(blob) else None}
