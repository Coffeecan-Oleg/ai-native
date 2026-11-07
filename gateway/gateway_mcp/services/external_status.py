"""Administrative read-only projection; no provider probes or credential reads."""
import re
from datetime import datetime
from gateway_mcp.config import read_json,tools_file
from gateway_mcp.services import external_limits as limits,external_safety as safety
from gateway_mcp.services.admin_roles import require_current_admin

LABELS={'unisender-readonly':'UniSender — чтение','bitrix24-rest':'Битрикс24 — API','caldav':'Календарь CalDAV',
 'gitlab-rest':'GitLab','google-rest':'Google','infra':'Инфраструктура','metrika-rest':'Метрика',
 'openrouter-audio':'Распознавание аудио','telegram':'Telegram','tracker-rest':'Трекер','webmaster-rest':'Вебмастер',
 'wordstat-rest':'Wordstat','yandex-disk-rest':'Яндекс Диск','yandex-mail':'Яндекс Почта','yonote-rpc':'Yonote'}
ERRORS={
 'EXTERNAL_RATE_LIMITED':'Достигнута частота обращений',
 'EXTERNAL_UPSTREAM_RATE_LIMITED':'Пауза после HTTP 429 сервиса',
 'EXTERNAL_LIMITER_UNAVAILABLE':'Проверка частоты недоступна',
 'EXTERNAL_CONCURRENCY_LIMIT':'Заняты места одновременных вызовов',
 'EXTERNAL_CIRCUIT_OPEN':'Пауза после сбоев сервиса',
 'EXTERNAL_SERVICE_UNAVAILABLE':'Сетевой сбой или HTTP 5xx',
 'EXTERNAL_CALL_TIMEOUT':'Истёк общий бюджет вызова',
 'EXTERNAL_SAFETY_UNAVAILABLE':'Проверка защиты недоступна',
 'BackendConfigError':'Ошибка настройки интеграции',
 'BackendRouteError':'Ошибка обработки вызова',
}
class StatusUnavailable(RuntimeError):pass

TIMING_SAMPLE_LIMIT=1000
OUTCOMES=('success','errors','limited','denied','other')


def _timing_projection(rows):
    groups=[]
    for outcome in OUTCOMES:
        item=next((row for row in rows if row['outcome']==outcome),{})
        groups.append({'outcome':outcome,'observed':int(item.get('observed') or 0),
            'timed':int(item.get('timed') or 0),
            **{key:int(item[key]) if item.get(key) is not None else None
               for key in ('p50_ms','p95_ms','max_ms')}})
    return {'sample_limit':TIMING_SAMPLE_LIMIT,'events_considered':sum(g['observed'] for g in groups),
        'timed_events':sum(g['timed'] for g in groups),'groups':groups}

def _registry():
    rows=read_json(tools_file(),{'tools':[]}).get('tools')
    if not isinstance(rows,list) or len(rows)>2000:raise ValueError('invalid_status_catalogue')
    result={};seen=set()
    for row in rows:
        if not isinstance(row,dict):raise ValueError('invalid_status_route')
        name=row.get('name');transport=row.get('transport')
        if isinstance(name,str):
            if name in seen:continue
            seen.add(name)
        if transport not in limits.TRANSPORTS:continue
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',name):raise ValueError('invalid_status_route_name')
        result.setdefault(name,transport) # Same first-match semantics as gateway_call_tool.
    return result

def _iso(value):
    return value.isoformat() if isinstance(value,datetime) else None

def _project(transport,names,now,rate,circuit,calls,observations,last,rate_rules,safety_rules,timings=None):
    rate_on=limits.enabled({'transport':transport});safety_on=safety.enabled({'transport':transport})
    active=[row for row in calls if row['integration']==transport and row['expires_at']>now]
    paused=[];next_seconds=0
    if rate_on and rate and rate['blocked_until'] and rate['blocked_until']>now:
        seconds=limits._seconds(rate['blocked_until']-now);next_seconds=max(next_seconds,seconds)
        paused.append({'label':'Пауза после HTTP 429 сервиса','seconds':seconds,'until':_iso(rate['blocked_until'])})
    phase='ready' if safety_on else 'not_enabled'
    if safety_on and circuit and circuit['opened_until']:
        if circuit['opened_until']>now:
            phase='paused';seconds=limits._seconds(circuit['opened_until']-now);next_seconds=max(next_seconds,seconds)
            paused.append({'label':'Пауза после сбоев сервиса','seconds':seconds,'until':_iso(circuit['opened_until'])})
        elif any(row['call_id']==circuit['probe_id'] for row in active):
            phase='probing'
            next_seconds=max(next_seconds,limits._seconds(next(row['expires_at'] for row in active if row['call_id']==circuit['probe_id'])-now))
        else:phase='awaiting_probe'
    if safety_on and len(active)>=safety_rules['SHARED_CONCURRENCY']:
        next_seconds=max(next_seconds,limits._seconds(min(row['expires_at'] for row in active)-now))
        if phase=='ready':phase='busy'
    available=None
    if rate_on:
        tat=rate['tat'] if rate else now;available=0
        wait,_=limits.advance(now,tat,rate_rules['SHARED_PER_MINUTE'],rate_rules['SHARED_BURST']);next_seconds=max(next_seconds,wait)
        for _ in range(rate_rules['SHARED_BURST']):
            wait,tat=limits.advance(now,tat,rate_rules['SHARED_PER_MINUTE'],rate_rules['SHARED_BURST'])
            if wait:break
            available+=1
        if paused:available=0
    totals={key:0 for key in ['total','success','errors','limited','denied']};successes=[]
    latest=None
    for name in names:
        row=observations.get(name,{})
        for key in totals:totals[key]+=int(row.get(key) or 0)
        if row.get('last_success'):successes.append(row['last_success'])
        candidate=last.get(name)
        if candidate and (latest is None or candidate['created_at']>latest['created_at']):latest=candidate
    problem=None
    if latest:
        code=latest.get('code');gid=latest.get('request_id')
        problem={'at':_iso(latest['created_at']),'label':ERRORS.get(code,'Отказ или ошибка вызова; подробности в журнале'),
            'code':code if code in ERRORS else None,
            'request_id':gid if isinstance(gid,str) and re.fullmatch('[a-f0-9]{16}',gid) else None}
    from gateway_mcp.services.unisender_readonly import enabled as unisender_enabled
    return {'transport':transport,'label':LABELS[transport],'route_count':len(names),
        'route_enabled':unisender_enabled() if transport=='unisender-readonly' else None,'rate_enabled':rate_on,
        'safety_enabled':safety_on,'rate_policy':rate_rules if rate_on else None,'safety_policy':safety_rules if safety_on else None,
        'phase':phase,'pauses':paused,'shared_burst_available':available,'shared_next_seconds':next_seconds,
        'active_calls':len(active) if safety_on else None,'totals':totals,
        'last_success_at':_iso(max(successes)) if successes else None,'latest_problem':problem,
        'timing':_timing_projection((timings or {}).get(transport,[]))}

def snapshot(actor):
    # Live authority precedes the catalogue and all global observations. No new access grant.
    require_current_admin(actor)
    try:
        registry=_registry();transports=sorted(set(registry.values()));names=list(registry)
        rate_rules=limits.policy();safety_rules=safety.policy()
        with limits._connect() as conn,conn.cursor() as cur:
            cur.execute('set transaction isolation level repeatable read, read only')
            now=cur.execute('select transaction_timestamp() as now').fetchone()['now']
            rates=cur.execute("select integration,tat,blocked_until from gateway_external_limits where bucket='global' and integration=any(%s)",(transports,)).fetchall()
            circuits=cur.execute('select integration,opened_until,probe_id from gateway_external_circuits where integration=any(%s)',(transports,)).fetchall()
            calls=cur.execute('select integration,call_id,expires_at from gateway_external_calls where integration=any(%s) and expires_at>%s',(transports,now)).fetchall()
            observed=cur.execute('''select payload #>> '{arguments,tool_name}' as route,
                count(*) as total,count(*) filter(where status='ok' and decision='allow') as success,
                count(*) filter(where status in ('error','failed','upstream_error')) as errors,
                count(*) filter(where status='rate_limited') as limited,
                count(*) filter(where status in ('denied','blocked') or decision='deny') as denied,
                max(created_at) filter(where status='ok' and decision='allow') as last_success
                from audit_events where created_at>=%s-interval '24 hours' and created_at<=%s
                and event='tool_call' and tool='gateway_call_tool'
                and payload #>> '{arguments,tool_name}'=any(%s) group by 1''',(now,now,names)).fetchall()
            latest=cur.execute('''select distinct on (payload #>> '{arguments,tool_name}')
                payload #>> '{arguments,tool_name}' as route,created_at,
                left(payload->>'error',80) as code,left(payload #>> '{arguments,gateway_request_id}',32) as request_id
                from audit_events where created_at>=%s-interval '24 hours' and created_at<=%s
                and event='tool_call' and tool='gateway_call_tool' and status in ('error','failed','upstream_error','rate_limited','denied','blocked')
                and payload #>> '{arguments,tool_name}'=any(%s) order by 1,created_at desc,id desc''',(now,now,names)).fetchall()
            # Limit the recent sample BEFORE validating timings. Missing old measurements
            # remain in coverage counts, never become zero milliseconds. No raw rows leave SQL.
            timing_rows=cur.execute('''with catalogue as (
                select * from unnest(%s::text[],%s::text[]) as c(route,transport)
            ), adapters as (
                select transport,array_agg(route) as names from catalogue group by transport
            ), recent as (
                select a.transport,e.status,e.decision,e.payload from adapters a cross join lateral (
                    select status,decision,payload from audit_events
                    where created_at>=%s-interval '24 hours' and created_at<=%s
                    and event='tool_call' and tool='gateway_call_tool'
                    and payload #>> '{arguments,tool_name}'=any(a.names)
                    order by created_at desc,id desc limit %s
                ) e
            ), measured as (
                select transport,case
                    when status in ('denied','blocked') or decision='deny' then 'denied'
                    when status='ok' and decision='allow' then 'success'
                    when status='rate_limited' then 'limited'
                    when status in ('error','failed','upstream_error') then 'errors'
                    else 'other' end as outcome,
                    case when payload #>> '{arguments,gateway_timing_kind}'='tool_run_v1'
                    and jsonb_typeof(payload #> '{arguments,gateway_duration_ms}')='number'
                    and (payload #>> '{arguments,gateway_duration_ms}') ~ '^[0-9]{1,8}$'
                    then case when (payload #>> '{arguments,gateway_duration_ms}')::bigint<=86400000
                        then (payload #>> '{arguments,gateway_duration_ms}')::bigint end end as ms
                from recent
            ) select transport,outcome,count(*) as observed,count(ms) as timed,
                percentile_disc(0.5) within group(order by ms) filter(where ms is not null) as p50_ms,
                percentile_disc(0.95) within group(order by ms) filter(where ms is not null) as p95_ms,
                max(ms) as max_ms from measured group by transport,outcome''',
                (names,[registry[n] for n in names],now,now,TIMING_SAMPLE_LIMIT)).fetchall()
        rate_map={r['integration']:r for r in rates};circuit_map={r['integration']:r for r in circuits}
        obs_map={r['route']:r for r in observed};last_map={r['route']:r for r in latest}
        timing_map={t:[r for r in timing_rows if r['transport']==t] for t in transports}
        rows=[_project(t,[n for n in names if registry[n]==t],now,rate_map.get(t),circuit_map.get(t),calls,obs_map,last_map,
            rate_rules,safety_rules,timing_map) for t in transports]
        return {'observed_at':_iso(now),'period_hours':24,'rows':rows,'provider_probed':False,'state_written':False}
    except Exception:raise StatusUnavailable('integration_status_unavailable') from None
