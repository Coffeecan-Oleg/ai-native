"""Fixed Bitrix chat destination. A timeout is ambiguous and is not retried automatically."""
import json
import os
from pathlib import Path
import re
import stat
import threading
import urllib.request
import urllib.error

CHAT = 'chat68282'
PORTAL = 'https://akmetron.bitrix24.ru'


def configured():
    return os.getenv('GATEWAY_ACTIVITY_NOTIFICATIONS','').casefold() in {'1','true'}


def credential_url():
    path = Path(os.environ['GATEWAY_ACTIVITY_WEBHOOK_FILE'])
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 512 or (os.name=='posix' and info.st_mode & 0o037):
        # Root/service group read is allowed for a read-only container secret; no group write/world access.
        raise ValueError('credential_unavailable')
    value = path.read_text(encoding='utf-8').strip()
    if not re.fullmatch(re.escape(PORTAL)+r'/rest/[1-9][0-9]{0,11}/[A-Za-z0-9_-]{12,128}/?',value):
        raise ValueError('credential_unavailable')
    return value.rstrip('/')+'/im.message.add.json'


def send(text):
    if not isinstance(text,str) or not 1 <= len(text) <= 1800:
        return {'outcome':'rejected'}
    try:
        url = credential_url()
    except Exception:
        return {'outcome':'rejected'}
    try:
        # urllib has no INFO request-URL logger. Do not enable proxy env or redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        data=json.dumps({'DIALOG_ID':CHAT,'MESSAGE':text,'SYSTEM':'N','URL_PREVIEW':'N'},ensure_ascii=False).encode()
        try:
            response=opener.open(urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'}),timeout=6)
        except urllib.error.HTTPError as exc:
            response=exc
        with response:
                raw=response.read(16385)
                if len(raw)>16384:return {'outcome':'unknown'}
                if response.status == 429:
                    return {'outcome':'rate_limited'}
                body=json.loads(raw)
                if response.status==200 and isinstance(body,dict) and type(body.get('result')) is int and body['result']>0:
                    return {'outcome':'sent','message_id':body['result']}
                if isinstance(body,dict) and body.get('error')=='QUERY_LIMIT_EXCEEDED':
                    return {'outcome':'rate_limited'}
                if 300<=response.status<500 or isinstance(body,dict) and isinstance(body.get('error'),str):
                    return {'outcome':'rejected'}
    except Exception:
        pass
    return {'outcome':'unknown'}


def run_once(sender=None):
    from gateway_mcp.services import storage_activity_notifications as store
    from gateway_mcp.services.storage_core import _connect, ensure_schema
    from gateway_mcp.services.storage_admin_console import admin_audit_user_names
    from gateway_mcp.services.activity_events import format_batch
    if not configured():
        return {'status':'disabled'}
    ensure_schema()
    with _connect() as conn:
        locked = conn.execute('SELECT pg_try_advisory_lock(%s) AS locked',(store.WORKER_LOCK,)).fetchone()['locked']
        if not locked:
            return {'status':'busy'}
        try:
            store.recover_and_prune(conn)
            conn.commit()
            # Validate locally before moving events to sending.
            if sender is None:
                try:
                    credential_url()
                except Exception:
                    conn.execute("UPDATE platforma_activity_settings SET worker_seen_at=clock_timestamp(),worker_status='credential_unavailable' WHERE id=1")
                    conn.commit()
                    return {'status':'credential_unavailable'}
            rows,batch = store.claim(conn)
            if not rows:
                return {'status':'idle'}
            try:
                names=admin_audit_user_names([r['details']['actor_subject'] for r in rows])
            except Exception:
                names={}
            answer=(sender or send)(format_batch(rows,names,batch))
            store.finish(conn,rows,answer)
            return {'status':answer.get('outcome','unknown'),'count':len(rows)}
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)',(store.WORKER_LOCK,))
            conn.commit()


def start_worker():
    stop = threading.Event()
    def loop():
        while not stop.is_set():
            try:
                run_once()
            except Exception:
                # No exception text/stack or request URL in logs.
                pass
            stop.wait(5)
    thread=threading.Thread(target=loop,name='platforma-activity-notifications',daemon=True)
    thread.start()
    return stop,thread


class ActivityLifespan:
    def __init__(self,app):
        self.app=app

    async def __call__(self,scope,receive,send):
        if scope['type']!='lifespan':
            return await self.app(scope,receive,send)
        worker=None
        async def relay(message):
            nonlocal worker
            if message['type']=='lifespan.startup.complete' and configured():
                worker=start_worker()
            if message['type']=='lifespan.shutdown.complete' and worker:
                worker[0].set()
            await send(message)
        try:
            await self.app(scope,receive,relay)
        finally:
            if worker:
                worker[0].set()
