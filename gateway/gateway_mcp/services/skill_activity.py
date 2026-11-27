"""Own-actor evidence of SKILL.md reads; execution remains a client report."""
import json,re,time
from uuid import uuid4
from gateway_mcp.services import corporate_access,skill_library
from gateway_mcp.services.policy import has_scope
from gateway_mcp.services.storage_audit import append_audit_event
from gateway_mcp.services.storage_core import _connect,ensure_schema,postgres_enabled

READ_ID=re.compile(r'sr-[0-9a-f]{32}\Z')
SHA=re.compile(r'[0-9a-f]{64}\Z')
TTL_HOURS=24


def enabled():return corporate_access.enabled() and skill_library.enabled()


def record_read(actor,result):
    if not enabled() or result.get('path')!='SKILL.md' or result.get('encoding')!='utf-8':return None
    rid='sr-'+uuid4().hex
    details={k:result[k] for k in ['skill','skill_version','sha256','manifest_sha256','skill_pack']}
    details['skill_read_id']=rid
    payload={'ts_ms':int(time.time()*1000),'event':'skill_resource_read','actor':{'subject':actor.subject},
        'tool':'gateway_corporate_skill_read','system':'corporate-skills','decision':'allow',
        'status':'ok','scope':'skills:read','channel':'mcp','arguments':details}
    try:stored=bool(append_audit_event(payload))
    except Exception:stored=False
    return {'id':rid if stored else None,'recorded':stored,'expires_in_hours':TTL_HOURS,
        'evidence':'server_read_of_SKILL.md_only','execution_confirmed':False}


def receipt(actor,rid,skill,pack,version):
    if not isinstance(rid,str) or not READ_ID.fullmatch(rid):raise PermissionError('invalid_skill_read_id')
    if not postgres_enabled():raise PermissionError('skill_read_storage_unavailable')
    ensure_schema()
    with _connect() as conn,conn.cursor() as cur:
        cur.execute("""select payload->'arguments' as details from audit_events
            where actor_subject=%s and event='skill_resource_read' and status='ok' and decision='allow'
            and payload->>'channel'='mcp' and payload#>>'{arguments,skill_read_id}'=%s
            and created_at>=now()-interval '24 hours' order by id desc limit 1""",(actor.subject,rid))
        row=cur.fetchone()
    d=row['details'] if row else {}
    if (not isinstance(d,dict) or (d.get('skill'),d.get('skill_pack'),d.get('skill_version'))!=(skill,pack,version)
        or not SHA.fullmatch(str(d.get('sha256',''))) or not SHA.fullmatch(str(d.get('manifest_sha256','')))):
        raise PermissionError('skill_read_does_not_match_actor_or_version')
    return d


def link_metadata(actor,skill,pack,version,metadata):
    # Never accept client-supplied evidence flags, hashes or result text.
    if set(metadata)-{'skill_read_id','read_sha256','read_manifest_sha256'}:raise PermissionError('skill_event_metadata_denied')
    if not metadata:return {}
    if 'skill_read_id' not in metadata:raise PermissionError('skill_read_id_required')
    d=receipt(actor,metadata['skill_read_id'],skill,pack,version)
    return {'skill_read_id':d['skill_read_id'],'read_sha256':d['sha256'],'read_manifest_sha256':d['manifest_sha256']}


def own_activity(actor,*,skill='',limit=50):
    if not enabled() or not has_scope(actor,'telemetry:read'):raise PermissionError('telemetry:read required')
    skill_library.authorize(actor)
    if skill and (not isinstance(skill,str) or not re.fullmatch('[a-z][a-z0-9-]{0,100}',skill)):raise ValueError('Неверный ID навыка.')
    if type(limit) is not int or not 1<=limit<=100:raise ValueError('Неверный размер страницы.')
    if not postgres_enabled():raise RuntimeError('skill_activity_unavailable')
    ensure_schema();extra=" and payload#>>'{arguments,skill}'=%s" if skill else ''
    with _connect() as conn,conn.cursor() as cur:
        cur.execute("""select created_at,payload->'arguments' as details from audit_events
            where actor_subject=%s and event='skill_resource_read' and status='ok'
            and payload->>'channel'='mcp' and created_at>=now()-interval '30 days'"""+extra+
            ' order by created_at desc,id desc limit %s',(actor.subject,*([skill] if skill else []),limit))
        reads=cur.fetchall();extra=' and skill_id=%s' if skill else ''
        cur.execute('''select created_at,event_type,skill_id,skill_pack,skill_version,correlation_id,metadata
            from assistant_skill_events where actor_subject=%s and created_at>=now()-interval '30 days' '''+extra+
            ' order by created_at desc,id desc limit %s',(actor.subject,*([skill] if skill else []),limit))
        events=cur.fetchall()
        ids=list({r['metadata']['skill_read_id'] for r in events if isinstance(r['metadata'],dict)
            and isinstance(r['metadata'].get('skill_read_id'),str) and READ_ID.fullmatch(r['metadata']['skill_read_id'])})
        proofs={}
        if ids:
            # Historical display is not limited by the 24-hour window for NEW links.
            # One bounded query replaces an individual receipt lookup per event.
            cur.execute("""select payload->'arguments' as details from audit_events
                where actor_subject=%s and event='skill_resource_read' and status='ok' and decision='allow'
                and payload->>'channel'='mcp' and created_at>=now()-interval '31 days'
                and payload#>>'{arguments,skill_read_id}'=any(%s) order by id desc limit %s""",
                (actor.subject,ids,len(ids)))
            proofs={r['details'].get('skill_read_id'):r['details'] for r in cur.fetchall() if isinstance(r['details'],dict)}
    items=[]
    for r in reads:
        d=r['details']
        if not isinstance(d,dict):continue
        items.append({'created_at':r['created_at'],'kind':'server_read','skill':d.get('skill',''),
            'version':d.get('skill_version',''),'sha256':d.get('sha256',''),'read_id':d.get('skill_read_id',''),
            'read_link_verified':True,'execution_confirmed':False})
    for r in events:
        meta=r['metadata'] if isinstance(r['metadata'],dict) else {};verified=False;sha=''
        if 'skill_read_id' in meta:
            d=proofs.get(meta['skill_read_id'],{})
            verified=(d.get('skill'),d.get('skill_pack'),d.get('skill_version'))==(r['skill_id'],r['skill_pack'],r['skill_version']) and bool(SHA.fullmatch(str(d.get('sha256',''))))
            if verified:sha=d['sha256']
        items.append({'created_at':r['created_at'],'kind':'client_'+r['event_type'],'skill':r['skill_id'],
            'version':r['skill_version'],'sha256':sha,'read_id':meta.get('skill_read_id',''),
            'read_link_verified':verified,'execution_confirmed':False})
    return {'ok':True,'period_days':30,'items':sorted(items,key=lambda r:r['created_at'],reverse=True)[:limit],
        'notice':'Сервер подтверждает чтение файла. Начало, завершение и ошибка — сообщения клиента, а не проверка качества результата.'}
