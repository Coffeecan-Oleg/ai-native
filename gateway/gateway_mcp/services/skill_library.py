"""Bounded, inert shared materials. No extraction, execution, network or credentials."""
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import re
import stat
import unicodedata
from datetime import datetime, timezone
from uuid import UUID, uuid4
import zipfile

import yaml

from gateway_mcp.services import platforma_onboarding as onboarding

MAX_FILE = 128 * 1024
MAX_PACKAGE = 2 * 1024 * 1024
MAX_FILES = 32
MAX_PER_AUTHOR = 10
MAX_TOTAL = 100
PACK = 'platforma-shared'
TEXT_SUFFIXES = {'.md','.txt','.json','.yaml','.yml','.csv','.py','.js','.mjs','.sh','.ps1','.css','.html','.svg'}


def enabled():
    return os.getenv('GATEWAY_PLATFORMA_SKILL_LIBRARY_ENABLED', '').casefold() == 'true'


def authorize(actor):
    if not enabled() or actor is None:
        raise PermissionError('Skill library is disabled or a personal login is required')
    cfg = onboarding.settings()
    if cfg['until'] <= datetime.now(timezone.utc):
        raise PermissionError('Training access expired')
    if not onboarding.is_owner(actor) and onboarding.active_reader(actor.subject) is None:
        raise PermissionError('Active owner approval is required')


def title_key(value):
    return unicodedata.normalize('NFKC', value).casefold()


def clean_title(value):
    if not isinstance(value, str) or any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('Название не должно содержать скрытые или управляющие символы.')
    value = ' '.join(unicodedata.normalize('NFKC', value).split())
    if not 2 <= len(value) <= 120:
        raise ValueError('Название должно содержать от 2 до 120 символов.')
    return value


def safe_path(value):
    if not isinstance(value, str) or not value or len(value) > 240 or '\\' in value or ':' in value:
        raise ValueError('Недопустимый путь файла в архиве.')
    if any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('Недопустимый путь файла в архиве.')
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(p in {'.','..'} or p.startswith('.') for p in path.parts):
        raise ValueError('В архиве запрещены абсолютные, скрытые и родительские пути.')
    return path


def _text(blob):
    if not blob or len(blob) > MAX_FILE:
        raise ValueError('Каждый файл должен быть непустым и не больше 128 КиБ.')
    try:
        text = blob.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('В этой версии поддерживаются только текстовые файлы UTF-8.') from None
    if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
        raise ValueError('Обнаружен двоичный файл или управляющие символы.')
    return text


def parse_upload(filename, blob):
    """Validate all members before reading. Nothing is ever written to a pathname."""
    if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_PACKAGE:
        raise ValueError('Файл должен быть не больше 2 МиБ.')
    suffix = PurePosixPath(str(filename)).suffix.casefold()
    if suffix == '.md':
        files = {'SKILL.md': blob}
    elif suffix == '.zip':
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_FILES * 3:
                    raise ValueError('Слишком много элементов в архиве.')
                members, paths, size = [], set(), 0
                for item in entries:
                    raw = item.orig_filename
                    if '\x00' in raw or raw != item.filename:
                        raise ValueError('Недопустимое имя файла.')
                    path = safe_path(raw.rstrip('/') if item.is_dir() else raw)
                    if item.flag_bits & 1 or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                        raise ValueError('Зашифрованные или нестандартно сжатые архивы не поддерживаются.')
                    mode = (item.external_attr >> 16) & 0xffff
                    kind = stat.S_IFMT(mode)
                    if kind not in {0, stat.S_IFDIR, stat.S_IFREG} or (item.is_dir() and kind == stat.S_IFREG) or (not item.is_dir() and kind == stat.S_IFDIR):
                        raise ValueError('Ссылки и специальные файлы запрещены.')
                    folded = title_key(path.as_posix())
                    if folded in paths:
                        raise ValueError('В архиве повторяются пути файлов.')
                    paths.add(folded)
                    if item.is_dir():
                        continue
                    if path.suffix.casefold() not in TEXT_SUFFIXES and path.name not in {'LICENSE','LICENSE.txt'}:
                        raise ValueError('В архиве разрешены только текстовые инструкции и ресурсы; бинарные файлы не поддерживаются.')
                    if not 0 < item.file_size <= MAX_FILE:
                        raise ValueError('Каждый файл должен быть не больше 128 КиБ.')
                    size += item.file_size
                    members.append(item)
                if not members or len(members) > MAX_FILES or size > MAX_PACKAGE:
                    raise ValueError('Архив: не более 32 файлов и 2 МиБ распакованного содержимого.')
                mains = [m for m in members if PurePosixPath(m.filename).name == 'SKILL.md']
                if len(mains) != 1:
                    raise ValueError('В ZIP должен быть ровно один SKILL.md.')
                prefix = mains[0].filename[:-len('SKILL.md')]
                files = {}
                for item in members:
                    if not item.filename.startswith(prefix):
                        raise ValueError('Все файлы должны лежать в одной папке навыка, рядом с SKILL.md или внутри неё.')
                    relative = item.filename[len(prefix):]
                    safe_path(relative)
                    with archive.open(item) as handle:
                        data = handle.read(MAX_FILE + 1)
                    if len(data) != item.file_size or len(data) > MAX_FILE:
                        raise ValueError('Размер файла не совпадает с каталогом ZIP.')
                    files[relative] = data
        except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, NotImplementedError):
            raise ValueError('Не удалось прочитать ZIP. Проверь архив.') from None
    else:
        raise ValueError('Выбери Markdown (.md) или ZIP с одним SKILL.md.')
    for data in files.values():
        _text(data)
    main = _text(files['SKILL.md']).lstrip('\ufeff').replace('\r\n','\n')
    metadata = {}
    if main.startswith('---\n'):
        end = main.find('\n---\n', 4, 16384)
        if end < 0:
            raise ValueError('Некорректный или слишком большой YAML-заголовок SKILL.md.')
        header = main[4:end]
        try:
            tokens = list(yaml.scan(header))
            if len(tokens) > 1024 or any(isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for t in tokens):
                raise ValueError('Слишком сложный YAML-заголовок.')
            metadata = yaml.safe_load(header)
        except (yaml.YAMLError, RecursionError):
            raise ValueError('Некорректный YAML-заголовок SKILL.md.') from None
        if not isinstance(metadata, dict) or not all(isinstance(metadata.get(k), str) and metadata[k].strip() for k in ('name','description')):
            raise ValueError('YAML-заголовок должен содержать текстовые name и description.')
        if not main[end+5:].strip():
            raise ValueError('После заголовка должна быть инструкция.')
    elif not main.strip():
        raise ValueError('Инструкция пуста.')
    description = metadata.get('description', 'Навык участника учебной песочницы.')
    if len(description) > 1000:
        raise ValueError('Описание в SKILL.md должно быть не длиннее 1000 символов.')
    version = metadata.get('version', '1.0.0')
    if not isinstance(version, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.+_-]{0,63}', version):
        raise ValueError('Версия навыка должна быть короткой текстовой меткой.')
    manifest = {path: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'encoding':'utf-8'} for path, data in sorted(files.items())}
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return {'files': files, 'manifest': manifest, 'description': description, 'version': version, 'fingerprint':fingerprint}


def _db():
    return onboarding._db()


def _lock(cur):
    cur.execute("select pg_advisory_xact_lock(hashtextextended('APP11-shared-skills',0))")


def _rows(cur):
    cur.execute('select skill_id,origin,title,title_key,description,version,author_subject,author_label,manifest,revision,created_at,updated_at from platforma_shared_skills order by created_at,skill_id')
    return cur.fetchall()


def _check_name(title, skill_id, bundled, rows):
    key = title_key(title)
    for item in bundled:
        if item['name'] != skill_id and key in {title_key(item['name']), title_key(item['title'])}:
            raise ValueError('Такое название или постоянный ID уже используется.')
    for item in rows:
        if item['skill_id'] != skill_id and key in {title_key(item['skill_id']), item['title_key']}:
            raise ValueError('Такое название или постоянный ID уже используется.')


def _summary(row):
    return {'name':row['skill_id'], 'title':row['title'], 'version':row['version'],
            'description':row['description'], 'files':row['manifest'], 'origin':'uploaded',
            'author':row['author_label'], 'revision':row['revision'],
            'created_at':row['created_at'].isoformat(), 'updated_at':row['updated_at'].isoformat(),
            'trust':'user_uploaded_unreviewed',
            'telemetry':{'skill_id':row['skill_id'],'skill_pack':PACK,'skill_version':row['version'],'project':'platforma-training'}}


def catalogue(actor, bundled):
    authorize(actor)
    with _db() as conn, conn.cursor() as cur:
        rows = _rows(cur)
    overrides = {r['skill_id']:r for r in rows if r['origin']=='bundled'}
    result = []
    for item in bundled:
        row = overrides.get(item['name'])
        result.append(item | {'title':row['title'] if row else item['title'], 'origin':'bundled',
                             'author':'Встроенный навык', 'revision':row['revision'] if row else 0,
                             'can_rename':onboarding.is_owner(actor), 'is_author':False})
    for row in rows:
        if row['origin']=='uploaded':
            result.append(_summary(row) | {'can_rename':onboarding.is_owner(actor) or row['author_subject']==actor.subject,
                                          'is_author':row['author_subject']==actor.subject})
    return result


def upload(actor, *, title, filename, content, upload_key, bundled):
    authorize(actor)
    if actor.tool_allowlist is not None:
        raise PermissionError('Upload requires a personal browser session')
    title = clean_title(title)
    try:
        upload_key = UUID(upload_key)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Обнови страницу перед загрузкой.') from None
    parsed = parse_upload(filename, content)
    fingerprint = hashlib.sha256((title + '\n' + parsed['fingerprint']).encode()).hexdigest()
    skill_id = 'shared-' + uuid4().hex
    with _db() as conn, conn.cursor() as cur:
        _lock(cur)
        cur.execute('select * from platforma_shared_skills where upload_key=%s', (upload_key,))
        old = cur.fetchone()
        if old:
            if old['author_subject'] != actor.subject or old['fingerprint'] != fingerprint:
                raise ValueError('Эта форма уже использована для другой загрузки. Обнови страницу.')
            return _summary(old) | {'replayed':True}
        rows = _rows(cur)
        _check_name(title, skill_id, bundled, rows)
        uploads = [r for r in rows if r['origin']=='uploaded']
        if len(uploads) >= MAX_TOTAL or sum(r['author_subject']==actor.subject for r in uploads) >= MAX_PER_AUTHOR:
            raise ValueError('Лимит: 10 загрузок на автора, 100 на песочницу. Обратись к владельцу.')
        label = (actor.login or 'Участник')[:100]
        cur.execute('''insert into platforma_shared_skills
            (skill_id,origin,title,title_key,description,version,author_subject,author_label,manifest,upload_key,fingerprint)
            values (%s,'uploaded',%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) returning *''',
            (skill_id,title,title_key(title),parsed['description'],parsed['version'],actor.subject,label,json.dumps(parsed['manifest']),upload_key,fingerprint))
        row = cur.fetchone()
        for path, data in parsed['files'].items():
            cur.execute('insert into platforma_shared_skill_files(skill_id,path,content,sha256) values (%s,%s,%s,%s)',
                        (skill_id,path,data,parsed['manifest'][path]['sha256']))
        cur.execute("insert into platforma_shared_skill_events(skill_id,revision,actor_subject,action) values (%s,1,%s,'uploaded')",(skill_id,actor.subject))
    return _summary(row) | {'replayed':False}


def rename(actor, *, skill_id, title, expected_revision, bundled):
    authorize(actor)
    if actor.tool_allowlist is not None:
        raise PermissionError('Rename requires a personal browser session')
    title = clean_title(title)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('Обнови карточку перед переименованием.')
    original = next((r for r in bundled if r['name']==skill_id), None)
    with _db() as conn, conn.cursor() as cur:
        _lock(cur)
        rows = _rows(cur)
        current = next((r for r in rows if r['skill_id']==skill_id), None)
        if not current and not original:
            raise ValueError('Навык не найден.')
        is_bundled = bool(original) or (current and current['origin']=='bundled')
        if not onboarding.is_owner(actor) and (is_bundled or current['author_subject'] != actor.subject):
            raise PermissionError('Можно переименовывать только свои загрузки')
        if expected_revision != (current['revision'] if current else 0):
            raise ValueError('Название уже изменилось. Обнови карточку.')
        _check_name(title, skill_id, bundled, rows)
        if current:
            if title == current['title']:
                return {'name':skill_id,'title':title,'revision':current['revision']}
            cur.execute('update platforma_shared_skills set title=%s,title_key=%s,revision=revision+1,updated_at=now() where skill_id=%s returning revision', (title,title_key(title),skill_id))
        else:
            cur.execute('''insert into platforma_shared_skills(skill_id,origin,title,title_key,description,version,author_subject,author_label)
                values (%s,'bundled',%s,%s,%s,%s,%s,'Встроенный навык') returning revision''',
                (skill_id,title,title_key(title),original['description'],original['version'],actor.subject))
        revision = cur.fetchone()['revision']
        cur.execute("insert into platforma_shared_skill_events(skill_id,revision,actor_subject,action) values (%s,%s,%s,'renamed')",(skill_id,revision,actor.subject))
    return {'name':skill_id,'title':title,'revision':revision}


def read_resource(actor, row, path, offset=0, limit=16384):
    authorize(actor)
    safe_path(path)
    if path not in row['files']:
        raise PermissionError('Unknown uploaded resource')
    if type(offset) is not int or offset != 0 or type(limit) is not int or not 1 <= limit <= 24576:
        raise ValueError('Текст читается целиком; offset должен быть 0.')
    with _db() as conn, conn.cursor() as cur:
        cur.execute('select content,sha256 from platforma_shared_skill_files where skill_id=%s and path=%s', (row['name'],path))
        item = cur.fetchone()
    if not item:
        raise ValueError('Uploaded resource is missing')
    blob = bytes(item['content'])
    spec = row['files'][path]
    if len(blob) != spec['bytes'] or hashlib.sha256(blob).hexdigest() != spec['sha256'] or item['sha256'] != spec['sha256']:
        raise ValueError('Uploaded resource integrity mismatch')
    return {'ok':True,'skill':row['name'],'title':row['title'],'path':path,'total_bytes':len(blob),'sha256':spec['sha256'],
            'read_only':True,'encoding':'utf-8','content':_text(blob),'next_offset':None,'trust':'user_uploaded_unreviewed',
            'execution':'material only; explicit user selection; no server-side execution','telemetry':row['telemetry']}


def telemetry_descriptor(actor, skill_id):
    if not enabled() or not isinstance(skill_id, str) or not re.fullmatch(r'shared-[0-9a-f]{32}', skill_id):
        return None
    authorize(actor)
    with _db() as conn, conn.cursor() as cur:
        cur.execute("select version from platforma_shared_skills where skill_id=%s and origin='uploaded'", (skill_id,))
        row = cur.fetchone()
    return {'pack':PACK,'version':row['version']} if row else None
