"""Persistent, bounded admission and circuit state for the read-only UniSender adapter."""
import asyncio
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from gateway_mcp.backends.common import BackendRouteError
from gateway_mcp.services import external_limits as limits

PREFIX='GATEWAY_EXTERNAL_SAFETY_'
TRANSPORTS=frozenset({'unisender-readonly'})
LEASE_GRACE_SECONDS=5
FAILURE_WINDOW_SECONDS=60

class TransientFailure(BackendRouteError):
    """Only a classified network failure or provider HTTP5xx; never raw details."""

class SafetyError(limits.LimitError):
    def response(self):
        status,message={
            'EXTERNAL_CONCURRENCY_LIMIT':(429,'Достигнут предел одновременно выполняемых обращений. Повторите позже.'),
            'EXTERNAL_CIRCUIT_OPEN':(503,'Внешний сервис временно приостановлен после сбоев. Повторите позже.'),
            'EXTERNAL_CALL_TIMEOUT':(504,'Истекло время внешнего обращения. Автоматический повтор не выполняется.'),
            'EXTERNAL_SERVICE_UNAVAILABLE':(502,'Внешний сервис временно недоступен. Автоматический повтор не выполняется.'),
            'EXTERNAL_SAFETY_UNAVAILABLE':(503,'Проверка защиты внешнего сервиса временно недоступна.')
        }[self.code]
        return {'ok':False,'status':status,'error_code':self.code,'message':message,
            'retry_after_seconds':self.retry,'limit_level':self.level,'upstream_contacted':self.contacted}

@dataclass(frozen=True,repr=False)
class Lease:
    call_id:str
    integration:str

def enabled(route):
    from gateway_mcp.services import corporate_access
    return route.get('transport') in TRANSPORTS and corporate_access.enabled()

def policy():
    result={}
    for key,default,maximum in [('USER_CONCURRENCY',2,32),('SHARED_CONCURRENCY',4,32),
                              ('FAILURE_THRESHOLD',3,10),('OPEN_SECONDS',30,300),('TIMEOUT_SECONDS',30,60)]:
        raw=os.getenv(PREFIX+key,str(default)).strip()
        if not re.fullmatch('[0-9]{1,3}',raw) or not 1<=int(raw)<=maximum:raise ValueError('invalid_external_safety_policy')
        result[key]=int(raw)
    if result['USER_CONCURRENCY']>result['SHARED_CONCURRENCY']:raise ValueError('invalid_external_concurrency')
    return result

def reserve(cur,integration,user_bucket,now):
    """Called inside the rate limiter transaction, with its integration lock held."""
    rules=policy()
    cur.execute('''insert into gateway_external_circuits(integration,updated_at) values (%s,%s)
        on conflict do nothing''',(integration,now))
    state=cur.execute('select * from gateway_external_circuits where integration=%s for update',(integration,)).fetchone()
    if state['opened_until'] and state['opened_until']>now:
        raise SafetyError('EXTERNAL_CIRCUIT_OPEN',limits._seconds(state['opened_until']-now),level='integration')
    cur.execute('delete from gateway_external_calls where integration=%s and expires_at<=%s',(integration,now))
    active=cur.execute('select user_bucket,expires_at,call_id from gateway_external_calls where integration=%s',(integration,)).fetchall()
    probe=bool(state['opened_until'])
    if probe and any(row['call_id']==state['probe_id'] for row in active):
        raise SafetyError('EXTERNAL_CIRCUIT_OPEN',1,level='integration')
    own=[row for row in active if row['user_bucket']==user_bucket]
    if len(active)>=rules['SHARED_CONCURRENCY'] or len(own)>=rules['USER_CONCURRENCY']:
        shared=len(active)>=rules['SHARED_CONCURRENCY'];rows=active if shared else own
        wait=limits._seconds(min(row['expires_at'] for row in rows)-now)
        raise SafetyError('EXTERNAL_CONCURRENCY_LIMIT',wait,level='integration' if shared else 'employee')
    cid=uuid4().hex;generation=state['generation']+int(probe)
    cur.execute('''insert into gateway_external_calls values (%s,%s,%s,%s,%s,%s)''',
        (cid,integration,user_bucket,generation,probe,now+timedelta(seconds=rules['TIMEOUT_SECONDS']+LEASE_GRACE_SECONDS)))
    cur.execute('''update gateway_external_circuits set generation=%s,probe_id=%s,updated_at=%s where integration=%s''',
        (generation,cid if probe else None,now,integration))
    return Lease(cid,integration)

def _locked(cur,lease):
    if type(lease) is not Lease:raise ValueError('invalid_external_lease')
    cur.execute('select pg_advisory_xact_lock(%s)',(limits.lock_key(lease.integration),))
    now=cur.execute('select clock_timestamp() as now').fetchone()['now']
    return now

def start(lease):
    try:
        policy()
        with limits._connect() as conn,conn.cursor() as cur:
            now=_locked(cur,lease)
            row=cur.execute('select * from gateway_external_calls where call_id=%s and integration=%s',
                (lease.call_id,lease.integration)).fetchone()
            if not row:raise ValueError('expired_external_lease')
            state=cur.execute('select generation,opened_until,probe_id from gateway_external_circuits where integration=%s',
                (lease.integration,)).fetchone()
            if row['generation']!=state['generation'] or (state['opened_until'] and state['opened_until']>now):
                raise ValueError('superseded_external_lease')
            if row['probe'] and state['probe_id']!=lease.call_id:raise ValueError('superseded_external_probe')
            remaining=(row['expires_at']-now).total_seconds()-LEASE_GRACE_SECONDS
            if remaining<=0:raise ValueError('expired_external_lease')
            return remaining
    except Exception:raise SafetyError('EXTERNAL_SAFETY_UNAVAILABLE',5,level='service') from None

def finish(lease,outcome):
    if lease is None:return
    try:
        if outcome not in {'success','failure','neutral'}:raise ValueError('invalid_external_outcome')
        rules=policy()
        with limits._connect() as conn,conn.cursor() as cur:
            now=_locked(cur,lease)
            call=cur.execute('delete from gateway_external_calls where call_id=%s and integration=%s returning *',
                (lease.call_id,lease.integration)).fetchone()
            if not call or call['expires_at']<=now:return
            state=cur.execute('select * from gateway_external_circuits where integration=%s for update',(lease.integration,)).fetchone()
            if state['generation']!=call['generation']:return
            if call['probe'] and state['probe_id']!=lease.call_id:return
            if outcome=='failure':
                recent=state['last_failure_at'] and state['last_failure_at']>=now-timedelta(seconds=FAILURE_WINDOW_SECONDS)
                failures=min(rules['FAILURE_THRESHOLD'],(state['failures'] if recent else 0)+1)
                trip=call['probe'] or failures>=rules['FAILURE_THRESHOLD']
                cur.execute('''update gateway_external_circuits set failures=%s,last_failure_at=%s,
                    opened_until=%s,probe_id=null,generation=generation+%s,updated_at=%s where integration=%s''',
                    (failures,now,now+timedelta(seconds=rules['OPEN_SECONDS']) if trip else None,int(trip),now,lease.integration))
            elif outcome=='success':
                cur.execute('''update gateway_external_circuits set failures=0,last_failure_at=null,
                    opened_until=null,probe_id=null,generation=generation+%s,updated_at=%s where integration=%s''',
                    (int(call['probe']),now,lease.integration))
    except Exception:raise SafetyError('EXTERNAL_SAFETY_UNAVAILABLE',5,level='service') from None

async def invoke(admission,call):
    """No retry. The only enabled adapter uses cancellable async HTTP and read operations."""
    lease=getattr(admission,'lease',None)
    if type(lease) is not Lease:raise SafetyError('EXTERNAL_SAFETY_UNAVAILABLE',5,level='service')
    outcome='neutral';contacted=False
    try:
        remaining=await asyncio.to_thread(start,lease)
        contacted=True
        try:
            async with asyncio.timeout(remaining):result=await call()
        except TimeoutError:
            outcome='failure'
            raise SafetyError('EXTERNAL_CALL_TIMEOUT',policy()['OPEN_SECONDS'],level='integration',contacted=True) from None
        except TransientFailure:
            outcome='failure'
            raise SafetyError('EXTERNAL_SERVICE_UNAVAILABLE',policy()['OPEN_SECONDS'],level='integration',contacted=True) from None
        if result.get('ok'):outcome='success'
        return result
    finally:
        try:await asyncio.shield(asyncio.to_thread(finish,lease,outcome))
        except SafetyError:
            raise SafetyError('EXTERNAL_SAFETY_UNAVAILABLE',5,level='service',contacted=contacted) from None
