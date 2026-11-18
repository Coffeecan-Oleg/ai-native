"""Browser-only editorial lifecycle. Draft payloads never enter the MCP library.

The existing shared-skill tables contain only published material. New additive
tables retain immutable content snapshots and CAS-protected workflow events.
Bundled resources, credentials, usage events and external systems are untouched.
"""
import hashlib
import json
import re
from uuid import UUID, uuid4

from gateway_mcp.services import corporate_access, skill_library as library
from gateway_mcp.services import platforma_onboarding as onboarding

MAX_VERSIONS = 20
ID = re.compile(r'shared-[0-9a-f]{32}\Z')
VERSION = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9.+_-]{0,63}\Z')
LABELS = {'draft':'Черновик','review':'На проверке','published':'Опубликован','archived':'В архиве'}


def enabled():
    return library.enabled() and corporate_access.enabled()


def _authorize(actor, *, owner=False):
    if not enabled():
        raise PermissionError('Skill lifecycle is disabled')
    library.authorize(actor)
    if actor.tool_allowlist is not None:
        raise PermissionError('A personal browser session is required')
    if owner:
        onboarding.require_owner(actor)


def _identifier(skill_id):
    if not isinstance(skill_id,str) or not ID.fullmatch(skill_id):
        raise ValueError('Нужен постоянный ID загруженного навыка.')


def _workflow(cur, actor, skill_id, expected=None):
    _identifier(skill_id)
    cur.execute('select * from platforma_skill_workflows where skill_id=%s for update',(skill_id,))
    row=cur.fetchone()
    if row is None:
        raise ValueError('Навык не найден в управлении версиями.')
    if not onboarding.is_owner(actor) and row['author_subject']!=actor.subject:
        raise PermissionError('Only the author and owner can read drafts')
    if expected is not None and (type(expected) is not int or row['revision']!=expected):
        raise ValueError('Версия карточки изменилась. Обновите страницу.')
    return row


def _snapshot(cur,skill_id,revision):
    cur.execute('select * from platforma_skill_versions where skill_id=%s and revision=%s',(skill_id,revision))
    row=cur.fetchone()
    if row is None:
        raise ValueError('Версия материала не найдена.')
    _verify(row)
    return row


def _verify(row):
    files=row['files'];manifest=row['manifest']
    if not isinstance(files,dict) or set(files)!=set(manifest) or 'SKILL.md' not in files or len(files)>library.MAX_FILES:
        raise ValueError('Повреждена сохранённая версия навыка.')
    size=0
    for path,text in files.items():
        library.safe_path(path)
        if not isinstance(text,str):raise ValueError('Неверный ресурс версии.')
        blob=text.encode('utf-8');library._text(blob);size+=len(blob)
        spec=manifest[path]
        if len(blob)!=spec['bytes'] or hashlib.sha256(blob).hexdigest()!=spec['sha256']:
            raise ValueError('Контрольная сумма версии не совпала.')
    if size>library.MAX_PACKAGE:raise ValueError('Версия слишком большая.')
    if _fingerprint(row)!=row['fingerprint']:raise ValueError('Метаданные версии повреждены.')


def _fingerprint(row):
    data={k:row[k] for k in ('title','description','version','manifest','files')}
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _payload(title,parsed,version=None):
    title=library.clean_title(title)
    version=parsed['version'] if version is None else version
    if not isinstance(version,str) or not VERSION.fullmatch(version):
        raise ValueError('Версия: до 64 латинских букв, цифр, точек, +, _ и -.')
    result={'title':title,'description':parsed['description'],'version':version,
            'manifest':parsed['manifest'],'files':{p:b.decode('utf-8') for p,b in parsed['files'].items()}}
    result['fingerprint']=_fingerprint(result)
    _verify(result)
    return result


def _insert_snapshot(cur,skill_id,revision,payload,actor):
    cur.execute('select count(*) n from platforma_skill_versions where skill_id=%s',(skill_id,))
    if cur.fetchone()['n']>=MAX_VERSIONS:
        raise ValueError('Сохранено 20 версий. Обратитесь к владельцу; история не удаляется автоматически.')
    cur.execute('''insert into platforma_skill_versions
        (skill_id,revision,title,title_key,description,version,manifest,files,fingerprint,created_by)
        values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)''',
        (skill_id,revision,payload['title'],library.title_key(payload['title']),payload['description'],
         payload['version'],json.dumps(payload['manifest']),json.dumps(payload['files']),payload['fingerprint'],actor.subject))


def _event(cur,row,actor,action):
    cur.execute('insert into platforma_skill_workflow_events(skill_id,revision,actor_subject,action) values (%s,%s,%s,%s)',
                (row['skill_id'],row['revision'],actor.subject,action))


def _advance(cur,row,stage,*,candidate=None,published=None):
    cur.execute('''update platforma_skill_workflows set stage=%s,revision=revision+1,
        candidate_revision=%s,published_revision=%s,updated_at=now() where skill_id=%s returning *''',
        (stage,candidate if candidate is not None else row['candidate_revision'],
         published if published is not None else row['published_revision'],row['skill_id']))
    return cur.fetchone()


def _check_title(cur,title,skill_id,bundled):
    library._check_name(title,skill_id,bundled,library._rows(cur))
    cur.execute('''select w.skill_id,v.title_key from platforma_skill_workflows w
        join platforma_skill_versions v on v.skill_id=w.skill_id and v.revision=w.candidate_revision''')
    if any(r['skill_id']!=skill_id and library.title_key(title) in {r['skill_id'],r['title_key']} for r in cur.fetchall()):
        raise ValueError('Такое название уже используется.')


def create_draft(actor,*,title,filename,content,upload_key,bundled):
    _authorize(actor)
    try:key=UUID(upload_key)
    except (ValueError,TypeError,AttributeError):raise ValueError('Обновите форму загрузки.') from None
    payload=_payload(title,library.parse_upload(filename,content))
    with library._db() as conn,conn.cursor() as cur:
        library._lock(cur)
        cur.execute('select * from platforma_skill_workflows where upload_key=%s',(key,))
        old=cur.fetchone()
        if old:
            if old['author_subject']!=actor.subject or old['upload_fingerprint']!=payload['fingerprint']:
                raise ValueError('Форма уже использована для другой загрузки.')
            return {'name':old['skill_id'],'revision':old['revision'],'replayed':True}
        cur.execute('''select skill_id,author_subject from platforma_shared_skills where origin='uploaded'
            union select skill_id,author_subject from platforma_skill_workflows''')
        rows=cur.fetchall()
        if len(rows)>=library.MAX_TOTAL or sum(r['author_subject']==actor.subject for r in rows)>=library.MAX_PER_AUTHOR:
            raise ValueError('Достигнут лимит навыков автора или Платформы.')
        skill_id='shared-'+uuid4().hex
        _check_title(cur,payload['title'],skill_id,bundled)
        cur.execute('''insert into platforma_skill_workflows
            (skill_id,author_subject,author_label,stage,candidate_revision,upload_key,upload_fingerprint)
            values (%s,%s,%s,'draft',1,%s,%s) returning *''',
            (skill_id,actor.subject,(actor.login or 'Сотрудник')[:100],key,payload['fingerprint']))
        row=cur.fetchone();_insert_snapshot(cur,skill_id,1,payload,actor);_event(cur,row,actor,'draft_created')
    return {'name':skill_id,'revision':1,'replayed':False}


def adopt_published(actor,*,skill_id,expected_revision):
    """Explicit POST only; legacy published material is not re-approved implicitly."""
    _authorize(actor);_identifier(skill_id)
    with library._db() as conn,conn.cursor() as cur:
        library._lock(cur)
        cur.execute('select * from platforma_skill_workflows where skill_id=%s',(skill_id,))
        if cur.fetchone():
            row=_workflow(cur,actor,skill_id)
            return row
        cur.execute("select * from platforma_shared_skills where skill_id=%s and origin='uploaded'",(skill_id,))
        old=cur.fetchone()
        if old is None:raise ValueError('Загруженный навык не найден.')
        if not onboarding.is_owner(actor) and old['author_subject']!=actor.subject:raise PermissionError('Not the author')
        if type(expected_revision) is not int or expected_revision!=old['revision']:raise ValueError('Обновите карточку навыка.')
        cur.execute('select path,content,sha256 from platforma_shared_skill_files where skill_id=%s',(skill_id,))
        files={r['path']:bytes(r['content']) for r in cur.fetchall()}
        payload=_payload(old['title'],{'version':old['version'],'description':old['description'],'manifest':old['manifest'],'files':files})
        cur.execute('''insert into platforma_skill_workflows
            (skill_id,author_subject,author_label,stage,candidate_revision,published_revision)
            values (%s,%s,%s,'published',1,1) returning *''',(skill_id,old['author_subject'],old['author_label']))
        row=cur.fetchone();_insert_snapshot(cur,skill_id,1,payload,actor);_event(cur,row,actor,'legacy_adopted')
    return row


def update_draft(actor,*,skill_id,expected_revision,title,version,filename=None,content=None,bundled):
    _authorize(actor)
    with library._db() as conn,conn.cursor() as cur:
        library._lock(cur);row=_workflow(cur,actor,skill_id,expected_revision)
        if row['stage']=='archived':raise ValueError('Сначала восстановите навык из архива.')
        old=_snapshot(cur,skill_id,row['candidate_revision'])
        parsed=library.parse_upload(filename,content) if content is not None else {
            'version':old['version'],'description':old['description'],'manifest':old['manifest'],
            'files':{p:t.encode('utf-8') for p,t in old['files'].items()}}
        payload=_payload(title,parsed,version);_check_title(cur,payload['title'],skill_id,bundled)
        revision=row['revision']+1
        _insert_snapshot(cur,skill_id,revision,payload,actor)
        row=_advance(cur,row,'draft',candidate=revision);_event(cur,row,actor,'draft_updated')
    return row


def transition(actor,*,skill_id,expected_revision,action,bundled,review_ack=False):
    if action not in {'submit','request_changes','publish','archive','restore'}:raise ValueError('Неизвестное действие.')
    _authorize(actor,owner=action!='submit')
    with library._db() as conn,conn.cursor() as cur:
        library._lock(cur);row=_workflow(cur,actor,skill_id,expected_revision)
        if action=='submit':
            if row['stage']!='draft':raise ValueError('Отправить на проверку можно только черновик.')
            row=_advance(cur,row,'review');event='submitted'
        elif action=='request_changes':
            if row['stage']!='review':raise ValueError('Навык не находится на проверке.')
            row=_advance(cur,row,'draft');event='changes_requested'
        elif action=='publish':
            if row['stage']!='review' or review_ack is not True:
                raise ValueError('Для публикации владелец должен проверить версию и подтвердить её.')
            payload=_snapshot(cur,skill_id,row['candidate_revision'])
            _check_title(cur,payload['title'],skill_id,bundled)
            if row['published_revision'] is not None:
                previous=_snapshot(cur,skill_id,row['published_revision'])
                if previous['version']==payload['version'] and previous['fingerprint']!=payload['fingerprint']:
                    raise ValueError('Для изменённого материала укажите новую версию, например 1.0.1.')
            cur.execute('select * from platforma_shared_skills where skill_id=%s',(skill_id,))
            current=cur.fetchone()
            if current and current['origin']!='uploaded':raise ValueError('Встроенные навыки здесь не обновляются.')
            base_revision=current['revision']+1 if current else 1
            cur.execute('''insert into platforma_shared_skills
                (skill_id,origin,title,title_key,description,version,author_subject,author_label,manifest,fingerprint,revision)
                values (%s,'uploaded',%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                on conflict(skill_id) do update set title=excluded.title,title_key=excluded.title_key,
                description=excluded.description,version=excluded.version,manifest=excluded.manifest,
                fingerprint=excluded.fingerprint,revision=excluded.revision,updated_at=now()''',
                (skill_id,payload['title'],payload['title_key'],payload['description'],payload['version'],
                 row['author_subject'],row['author_label'],json.dumps(payload['manifest']),payload['fingerprint'],base_revision))
            cur.execute('delete from platforma_shared_skill_files where skill_id=%s',(skill_id,))
            for path,text in payload['files'].items():
                cur.execute('insert into platforma_shared_skill_files(skill_id,path,content,sha256) values (%s,%s,%s,%s)',
                    (skill_id,path,text.encode('utf-8'),payload['manifest'][path]['sha256']))
            row=_advance(cur,row,'published',published=row['candidate_revision']);event='published'
        elif action=='archive':
            if row['stage']=='archived':raise ValueError('Навык уже в архиве.')
            row=_advance(cur,row,'archived');event='archived'
        else:
            if row['stage']!='archived':raise ValueError('Навык не находится в архиве.')
            # A newer candidate still needs review after restoration. Only the
            # last published snapshot becomes public again, never the draft.
            stage='published' if row['published_revision']==row['candidate_revision'] else 'draft'
            row=_advance(cur,row,stage);event='restored'
        _event(cur,row,actor,event)
    return row


def states(cur):
    """Metadata only, for filtering the already-authorized public catalogue."""
    if not enabled():return {}
    cur.execute('''select w.skill_id,w.stage,w.published_revision,
        exists(select 1 from platforma_skill_workflow_events e where e.skill_id=w.skill_id and e.action='published') as reviewed
        from platforma_skill_workflows w''')
    return {r['skill_id']:r for r in cur.fetchall()}


def is_managed(skill_id):
    if not enabled():return False
    with library._db() as conn,conn.cursor() as cur:
        cur.execute('select 1 from platforma_skill_workflows where skill_id=%s',(skill_id,))
        return cur.fetchone() is not None


def readable(cur,skill_id):
    if enabled():
        cur.execute('select stage from platforma_skill_workflows where skill_id=%s',(skill_id,))
        row=cur.fetchone()
        if row and row['stage']=='archived':raise PermissionError('Archived skill is not available through MCP')


def dashboard(actor):
    _authorize(actor)
    with library._db() as conn,conn.cursor() as cur:
        cur.execute('''select w.*,v.title,v.version from platforma_skill_workflows w
            join platforma_skill_versions v on v.skill_id=w.skill_id and v.revision=w.candidate_revision
            where (%s or w.author_subject=%s) order by w.updated_at desc,w.skill_id''',
            (onboarding.is_owner(actor),actor.subject))
        return cur.fetchall()


def detail(actor,skill_id,version_revision=None):
    _authorize(actor)
    with library._db() as conn,conn.cursor() as cur:
        row=_workflow(cur,actor,skill_id)
        revision=row['candidate_revision'] if version_revision is None else version_revision
        if type(revision) is not int or revision<=0:raise ValueError('Неверный номер версии.')
        payload=_snapshot(cur,skill_id,revision)
        cur.execute('select revision,title,version,created_at from platforma_skill_versions where skill_id=%s order by revision desc',(skill_id,))
        versions=cur.fetchall()
        earlier=[item['revision'] for item in versions if item['revision']<revision]
        published=row['published_revision']
        baseline=published if published is not None and published<revision else max(earlier,default=None)
        comparison_base=_snapshot(cur,skill_id,baseline) if baseline is not None else None
        comparison_kind=('published' if baseline==published else 'previous_saved') if baseline is not None else None
        cur.execute('select revision,action,created_at from platforma_skill_workflow_events where skill_id=%s order by id desc limit 100',(skill_id,))
        return {'workflow':row,'payload':payload,'versions':versions,'events':cur.fetchall(),
                'comparison_base':comparison_base,'comparison_kind':comparison_kind}
