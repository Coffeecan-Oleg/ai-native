"""Corporate outbound-adapter admission: persistent GCRA, no sleeps or retries."""
import hashlib,os,re
from datetime import timedelta
from dataclasses import dataclass,field
from threading import Lock
from gateway_mcp.backends.common import BackendRouteError

TRANSPORTS=frozenset({'unisender-readonly','bitrix24-rest','caldav','gitlab-rest','google-rest','infra',
    'metrika-rest','openrouter-audio','telegram','tracker-rest','webmaster-rest','wordstat-rest',
    'yandex-disk-rest','yandex-mail','yonote-rpc'})
PREFIX='GATEWAY_EXTERNAL_LIMITS_'

@dataclass(repr=False)
class _Permit:
    route: object
    subject: str
    used: bool=False
    lock: object=field(default_factory=Lock)
    lease: object=None

def consume(permit,route,actor):
    if type(permit) is not _Permit:raise LimitError('EXTERNAL_LIMITER_UNAVAILABLE',5,level='service')
    with permit.lock:
        if permit.used or permit.route is not route or permit.subject!=actor.subject:
            raise LimitError('EXTERNAL_LIMITER_UNAVAILABLE',5,level='service')
        permit.used=True

class LimitError(BackendRouteError):
    def __init__(self,code='EXTERNAL_RATE_LIMITED',retry=1,*,level='employee',contacted=False):
        super().__init__(code)
        self.code=code;self.retry=max(1,min(3600,int(retry)));self.level=level;self.contacted=contacted
    def response(self):
        unavailable=self.code=='EXTERNAL_LIMITER_UNAVAILABLE'
        return {'ok':False,'status':503 if unavailable else 429,'error_code':self.code,
            'message':('Проверка частоты временно недоступна. Повторите позже.' if unavailable else
                       'Достигнуто ограничение частоты. Не повторяйте вызов раньше указанного времени.'),
            'retry_after_seconds':self.retry,'limit_level':self.level,'upstream_contacted':self.contacted}

def enabled(route):
    from gateway_mcp.services import corporate_access
    return route.get('transport') in TRANSPORTS and corporate_access.enabled()

def policy():
    result={}
    for key,default,maximum in [('USER_PER_MINUTE',30,6000),('USER_BURST',5,60),
                               ('SHARED_PER_MINUTE',120,6000),('SHARED_BURST',10,60)]:
        value=os.getenv(PREFIX+key,str(default)).strip()
        if not re.fullmatch('[0-9]{1,4}',value) or not 1<=int(value)<=maximum:raise ValueError('invalid_limit_policy')
        result[key]=int(value)
    if result['USER_BURST']>result['USER_PER_MINUTE'] or result['SHARED_BURST']>result['SHARED_PER_MINUTE']:
        raise ValueError('invalid_burst_policy')
    return result

def lock_key(integration):
    return int.from_bytes(hashlib.sha256(('gateway-external-limits:v1:'+integration).encode()).digest()[:8],'big',signed=True)

def bucket(subject):
    if not isinstance(subject,str) or not subject or len(subject)>512:raise ValueError('invalid_limit_identity')
    return 'user:'+hashlib.sha256(subject.encode()).hexdigest()

def _connect():
    import psycopg
    from psycopg.rows import dict_row
    from gateway_mcp.services.storage_core import database_url
    if not database_url():raise ValueError('limiter_database_required')
    return psycopg.connect(database_url(),connect_timeout=3,row_factory=dict_row,
        options='-c statement_timeout=2000 -c lock_timeout=1000')

def _seconds(delta):
    us=(delta.days*86400+delta.seconds)*1000000+delta.microseconds
    return max(0,(us+999999)//1000000)

def advance(now,tat,rate,burst):
    interval=timedelta(microseconds=(60000000+rate-1)//rate)
    wait=_seconds(tat-interval*(burst-1)-now)
    return wait,max(now,tat)+interval

def _global(cur,integration):
    cur.execute('select pg_advisory_xact_lock(%s)',(lock_key(integration),))
    cur.execute('select clock_timestamp() as now');now=cur.fetchone()['now']
    cur.execute("""insert into gateway_external_limits(integration,bucket,tat,updated_at)
        values (%s,'global',%s,%s) on conflict do nothing""",(integration,now,now))
    cur.execute("select tat,blocked_until from gateway_external_limits where integration=%s and bucket='global' for update",(integration,))
    return now,cur.fetchone()

def require(route,actor):
    if not enabled(route):return
    try:
        rules=policy();integration=route['transport'];subject=bucket(actor.subject)
        with _connect() as conn,conn.cursor() as cur:
            now,shared=_global(cur,integration)
            if shared['blocked_until'] and shared['blocked_until']>now:
                raise LimitError('EXTERNAL_UPSTREAM_RATE_LIMITED',_seconds(shared['blocked_until']-now),level='integration')
            cur.execute('''insert into gateway_external_limits(integration,bucket,tat,updated_at)
                values (%s,%s,%s,%s) on conflict do nothing''',(integration,subject,now,now))
            cur.execute('select tat from gateway_external_limits where integration=%s and bucket=%s for update',(integration,subject))
            own=cur.fetchone()
            shared_wait,shared_tat=advance(now,shared['tat'],rules['SHARED_PER_MINUTE'],rules['SHARED_BURST'])
            user_wait,user_tat=advance(now,own['tat'],rules['USER_PER_MINUTE'],rules['USER_BURST'])
            if shared_wait or user_wait:
                raise LimitError(retry=max(shared_wait,user_wait),level='integration' if shared_wait>=user_wait else 'employee')
            from gateway_mcp.services import external_safety
            lease=external_safety.reserve(cur,integration,subject,now) if external_safety.enabled(route) else None
            for name,tat in [('global',shared_tat),(subject,user_tat)]:
                cur.execute('update gateway_external_limits set tat=%s,updated_at=%s where integration=%s and bucket=%s',
                    (tat,now,integration,name))
            # Only this new service-private table; never audit or employee records.
            cur.execute("""delete from gateway_external_limits where integration=%s and bucket in
                (select bucket from gateway_external_limits where integration=%s and bucket<>'global'
                 and updated_at<%s and tat<=%s order by updated_at limit 100)""",(integration,integration,now-timedelta(days=7),now))
        return _Permit(route,actor.subject,lease=lease)
    except LimitError:raise
    except Exception:raise LimitError('EXTERNAL_LIMITER_UNAVAILABLE',5,level='service') from None

def cooldown(route,seconds):
    if not enabled(route):return
    try:
        seconds=max(1,min(3600,int(seconds)))
        with _connect() as conn,conn.cursor() as cur:
            now,_=_global(cur,route['transport'])
            cur.execute("""update gateway_external_limits set blocked_until=greatest(coalesce(blocked_until,%s),%s),
                updated_at=%s where integration=%s and bucket='global'""",
                (now,now+timedelta(seconds=seconds),now,route['transport']))
    except Exception:raise LimitError('EXTERNAL_LIMITER_UNAVAILABLE',5,level='service',contacted=True) from None

def retry_after(value):
    return max(1,min(3600,int(value))) if isinstance(value,str) and re.fullmatch('[0-9]{1,6}',value) else 60

def release_unused(permit):
    if type(permit) is not _Permit:return
    with permit.lock:
        if permit.used:return
        permit.used=True
    if permit.lease is not None:
        from gateway_mcp.services.external_safety import finish
        finish(permit.lease,'neutral')
